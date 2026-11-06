# Autenticación propia — Fase 3: el servidor OAuth para MCP. Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Un cliente de IA se conecta a `/mcp` con un token que emite la propia plataforma —registro abierto o CIMD, PKCE, consentimiento de la persona en la consola, token de acceso ES256 de 10 minutos para el recurso MCP, refresh que rota— y `/mcp` deja de aceptar tokens de Keycloak.

**Architecture:** Un subpaquete nuevo, `backend/app/core/auth/oauth/`, implementa el servidor de autorización sobre lo que ya construyó la fase 1: la clave ES256 de OpenBao (`keys.sign_jwt`), la sesión por cookie (`admit()`), el limitador Redis y la protección de salida (`egress.py`). Cuatro tablas nuevas con RLS (migración **0247**) guardan clientes, códigos y familias de refresh. Las rutas de máquina (`/oauth/token`, `/oauth/register`, `/oauth/revoke`, `/.well-known/oauth-authorization-server`) van fuera de `/api/v1`; la pantalla de consentimiento es una página de la consola en `/oauth/authorize` que llama a dos rutas propias bajo `/api/v1/oauth/`. La puerta de MCP (`deps.admit`, canal MCP) verifica el token propio y la familia en cada llamada.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, PostgreSQL 16 (RLS), OpenBao (transit `auth-signing`, de la fase 1), Redis, PyJWT 2.15.1 (ES256), httpx, pytest. React + TypeScript estricto, Vite, TanStack Query, vitest. Ninguna dependencia nueva.

**Diseño:** `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` (aprobado el 2026-11-04), §5 y §8 fase 3; §5.6 (el token interno del asistente) ya lo construyó la fase 1 (su D1, `backend/app/core/auth/loopback.py`) y aquí solo se comprueba. Fuentes: `docs/superpowers/specs/2026-11-04-autenticacion-propia-fuentes.md` (citadas como [Sn]) y, para lo que el dossier no cita, las páginas que este plan leyó el 2026-11-06 y nombra con su URL en el Apéndice C.

## Global Constraints

- **Seguridad.** Invariantes 1, 1b, 1c, 2, 2a, 3, 4, 5, 5a y 6 de `CLAUDE.md` intactas, y 1c entera:
  - **1c(a):** en `/mcp` solo un token OAuth hace una sesión; ni la cookie de la consola ni el token interno del asistente valen en el canal (la fase 1 ya lo prueba; esta fase lo mantiene). El token lo emite la plataforma a la persona tras su propio inicio de sesión, con audiencia **exactamente** el recurso MCP (`<ITS_PUBLIC_CONSOLE_URL>/mcp`) y ninguna otra.
  - **1c(b):** cada llamada MCP se sigue auditando con el canal `mcp`, el cliente de IA (`app` = su `client_id`) y la herramienta.
  - **1c(c):** ningún secreto en claro llega a una respuesta MCP. Las rutas nuevas bajo `/api/v1` que emiten un código o lo consumen entran en `NEVER_EXPOSED`; las de máquina están fuera de `/api/v1`, donde ningún tool llega (`app/mcp/guard.py`).
  - **1c(d):** MCP sigue apagado hasta que un org-admin lo enciende (`mcp_enabled`); la persona necesita `mcp.use`; revocar un cliente de IA corta su acceso en la siguiente llamada.
  - **1c(e):** los límites de la API no cambian (nombres, techos de lote, sin grupos para bloquear o borrar).
  - Ningún secreto (código de autorización, refresh token, `code_verifier`) aparece en logs, ni en la base de datos en claro (se guarda su SHA-256), ni en una respuesta salvo la que lo entrega una vez. Las respuestas del servidor OAuth llevan `Cache-Control: no-store` y `Pragma: no-cache` (RFC 6749 §5.1).
  - El navegador no guarda ningún token: la consola pasa la consulta de `/oauth/authorize` a la API y navega a la redirección que la API le devuelve.
- **Migración.** Una sola, **0247** (`backend/migrations/versions/0247_oauth_server.py`, revision `"0247"`, down_revision **`"0246"`**, la de la fase 2), hacia adelante; `downgrade()` funcional (la CI corre `upgrade head → downgrade base → upgrade head`). 0245 y 0246 no se tocan. **La Task 5 no empieza hasta que 0246 esté en la rama** (Oleadas).
- **Alcance.** Las cuatro tablas nuevas tienen `org_id` + RLS forzada (`tenant_isolation`, patrón de 0245); `oauth_clients` es **G**; `oauth_codes`, `oauth_refresh_families` y `oauth_refresh_tokens` son **O(`users`, `user_id`)**, como las de 0245. Todas entran en `client_scope_catalog.CATALOG`. El único sitio que abre el alcance «todos» sobre ellas es `app/core/auth/oauth/store.py::oauth_session`, en `ALL_SCOPE_ALLOWLIST`.
- **Parámetros (diseño §5):** token de acceso 10 min (`exp - iat ≤ 600`), código 60 s, refresh que rota en cada uso y cuya familia caduca a los 30 días sin uso, registro sin uso 30 días caduca; un registro DCR por IP con límite (P3: 20 por hora y dirección); PKCE solo `S256`; `iss` (RFC 9207) en toda respuesta de autorización; CIMD de 5 KiB como máximo, por `egress.get_bounded`, caché por cabeceras acotada a [5 min, 24 h].
- **Errores del servidor OAuth:** cuerpo `{"error", "error_description"}` de RFC 6749 §5.2, en inglés y ASCII (§5.2 limita `error_description` a `%x20-21 / %x23-5B / %x5D-7E` y lo dirige al desarrollador del cliente): **nunca** pasan por `tr()`. Los textos para una **persona** (la pantalla de consentimiento, `/mcp`, Ajustes) sí van por `tr()` y `app/core/i18n/es.py`.
- **Textos de la consola:** `t()` con claves en `frontend/src/i18n/en/` y `es/`; el término es «aplicación de IA».
- **Auditoría.** Acciones nuevas, siempre como literal (`action="..."`, nunca una variable, para no mover `REVIEWED_NON_LITERAL_ACTIONS`): `mcp.app_authorized`, `mcp.refresh_reused`, `mcp.code_reused`, `mcp.grant_revoked`. Se añaden a `frontend/src/i18n/{en,es}/auditVocabulary.ts` (inglés = la derivación de `humaniseEnum`). Si una tarea toca `backend/app`, corre también `cd frontend && npx vitest run src/modules/audit src/i18n`.
- **Pruebas.** Ninguna se borra, se salta ni se debilita. Las que fijan Keycloak en la puerta de MCP se **re-apuntan** al servidor propio, y el informe de la tarea dice cuáles y por qué. Las que prueban funciones de Keycloak que siguen existiendo hasta la fase 4 (`security.validate_token`, `kc_client.revoke_consent`) no se tocan.
- **Recuentos que mueven varias tareas.** Cada tarea dice su delta **respecto de la base que ve**; el coordinador suma al fusionar y corre la prueba en el árbol fusionado. Los conocidos: llamadas de log en `tests/test_logging_discipline.py` (108 en la rama hoy; **+1** Task 3, **−3** Task 10 → **106**, más lo que añada la fase 2); `NEVER_EXPOSED` en `tests/test_mcp_channel_api.py` (36 hoy; **+1** Task 8 → **37**, más lo de la fase 2); `UNGATED_BY_DESIGN` en `tests/test_route_gating.py` (**+2** Task 8).
- **Archivos generados.** `frontend/src/lib/globalRoutes.json` no se edita a mano. Esta fase no cambia `GLOBAL_WRITES` ni `GLOBAL_READS`, así que no lo regenera; si al fusionar con la fase 2 esas listas cambian, el coordinador lo regenera en el árbol fusionado con `cd backend && .venv/bin/python -m tests.export_console_global_routes`.
- **Verificación, desde `backend/`:** `.venv/bin/pytest -q` con los archivos que nombra el paso «Las pruebas pasan» de cada tarea; `.venv/bin/mypy app`; `.venv/bin/ruff check app tests`. La suite completa la corre el coordinador en el árbol fusionado. **Desde `frontend/`:** `npx vitest run` completo, `npx tsc -b`, `npm run build`.
- **Pistas en paralelo (worktrees)**, las de la fase 1 sin cambios:
  - La worktree nace de `origin/main`: lo primero, `git merge --ff-only claude/compassionate-bardeen-vgtwus` (o `git merge` si no avanza en rápido) y `git log --oneline -1`. **La Task 5 y todo lo que viene después**, además, fusionan antes de empezar la rama de la fase 2 que contiene 0246 (el coordinador da su nombre; no es un valor que este plan pueda fijar) y comprueban que está: `ls backend/migrations/versions/0246_*.py` lista un archivo.
  - `ln -s /home/user/Novieri-RMM/backend/.venv backend/.venv` y `ln -s /home/user/Novieri-RMM/frontend/node_modules frontend/node_modules`.
  - `cd backend && export PYTHONPATH=$PWD`, y comprobar que `.venv/bin/python -c "import app; print(app.__file__)"` imprime una ruta de la worktree. `PYTHONPATH` va en cada orden que corre Python.
  - `ITS_TEST_REDIS_URL` exportada con la base de Redis que asigne el coordinador a la worktree, de 0 a 15 (por
    ejemplo, para la base 3, `export ITS_TEST_REDIS_URL=redis://localhost:6379/3`).
  - `pg_ctlcluster 16 main start 2>/dev/null; redis-cli ping >/dev/null 2>&1 || redis-server --daemonize yes`.
  - Se hace commit en la rama de la worktree; no se hace push ni se toca el árbol principal.

## Lo que hace esta fase, y lo que deja a la fase 4

**Hace (diseño §5):** §5.1 los cuatro endpoints, los metadatos RFC 8414 y el RFC 9728 apuntando a la plataforma; §5.2 la autorización con consentimiento, código de 60 s, redirects exactos salvo el puerto de loopback, `iss`; §5.3 el token ES256 de 10 min con audiencia MCP, el refresh que rota y la revocación desde Ajustes; §5.4 el registro abierto con límites y caducidad; §5.5 CIMD por la protección de salida, comprobando `client_id` y con caché; §5.6 se comprueba, no se rehace. `/mcp` deja de aceptar el token de Keycloak (pregunta P1).

**Deja a la fase 4, explícitamente (diseño §8):**
1. Los servicios `keycloak` y `keycloak-init`, `deploy/keycloak/` entero —incluida la convergencia MCP de `init-clients.py` (ámbito `mcp`, mapeador de audiencia, políticas de registro, Trusted Hosts)— y las rutas de Traefik `kc`, `kc-registration-oidc` y `kc-registration-closed`. Mientras tanto Keycloak sigue abriendo su registro anónimo con `ITS_MCP_REGISTRATION=open`, aunque la API ya no acepta sus tokens (pregunta P13).
2. `deploy/keycloak/ci_mcp_flow.py` y el paso «MCP — live registration, PKCE and audience, through Traefik» del job `deploy` de `.github/workflows/ci.yml`, **reescritos contra el servidor propio**. En esta fase siguen verdes y siguen probando solo a Keycloak: no llaman a la API (`ci_mcp_flow.py` habla con Traefik y Keycloak), así que el cambio de la puerta no los rompe. `backend/tests/test_ci_mcp_flow.py` (sus funciones puras) igual.
3. El validador de Keycloak: `app/core/security.py` (`validate_token`, `JwksCache`, `TokenClaims`, `require_person_mcp_token`), `app.state.jwks`, el parámetro `jwks_http_client` de `create_app`, la sonda `jwks` de `app/api/probes.py`, los ajustes `oidc_*` y las claves `.env` `ITS_OIDC_*`, y `tests/fake_idp.py`; también `deps.jwks`, que ya no tiene llamadas. Desde esta fase ninguna puerta los llama; sus pruebas unitarias se quedan hasta que se borren con ellos. Consecuencia visible: la caché de `JwksCache` ya no se llena, y la sonda `jwks` («Sign-in key cache») dirá `degraded` (`empty_cache`) en la página de estado hasta que la fase 4 la quite; no entra en las muestras del worker (`WORKER_SAMPLED_PROBES`), así que no avisa a nadie (pregunta P12).
4. `users.external_subject` (ninguna puerta lo lee desde esta fase) y su clave `users_org_id_external_subject_key` en `ORG_WIDE_KEYS`; `ApiEnv.create_user(..., sub=)` sigue identificando a las personas de prueba por él.
5. `kc_client.revoke_consent` y sus pruebas (sin llamadas desde esta fase), `keycloak_event_ingest` y `app/modules/keycloak_events/` (D4 de la fase 1).
6. En `deploy/README.md`, las filas y párrafos que describen a Keycloak («What keycloak-init converges», las filas `kc` de «What Traefik routes», los riesgos residuales propios de Keycloak): la Task 12 los marca «hasta la fase 4», no los borra.

## Oleadas y paralelismo

| Oleada | Tareas | En paralelo | Depende de |
|---|---|---|---|
| 1 | 1, 2, 3, 4, 11 | sí | — (**puede empezar antes de que exista 0246**: ninguna toca la base de datos) |
| 2 | 5 | — | **0246 de la fase 2 en la rama**; 1 (`policy.CLIENT_IDLE`) |
| 3 | 6, 7 | sí | 1, 2, 3, 5 |
| 4 | 8, 9 | sí | 6, 7 (la 9 también 2) |
| 5 | 10 | — | 7, 9 (sus pruebas llaman herramientas por `/mcp` con el token propio) |
| 6 | 12 | — | todas |

**La migración.** La Task 5 es la única que crea tablas y la única que no puede correr antes de 0246: con `down_revision = "0246"`, Alembic no encuentra la cadena hasta que 0246 está en la rama, y las pruebas (que migran a `head`) fallan. Si la fase 2 cambia el número o renuncia a su migración, el coordinador ajusta `down_revision` y nada más.

## Tareas

| Task | Qué | Diseño |
|---|---|---|
| 1 | Primitivas puras: `policy`, `errors`, `redirects`, `pkce`; manejador de `OAuthError` en `main.py` | §5.1–§5.3 |
| 2 | El token de acceso: `access.py` (`McpGrant`, acuñar, verificar) | §5.3, §6.3 |
| 3 | `egress.get_bounded` y el documento CIMD (`cimd.py`) | §5.5 |
| 4 | Consola: la pantalla de consentimiento `/oauth/authorize` | §5.2 |
| 5 | Migración 0247, modelos, catálogo, `store.py` y `clients.find_client` | §6.1, §6.2 |
| 6 | Registro (DCR) y clientes CIMD; metadatos RFC 8414; RFC 9728 al emisor propio; Traefik | §5.1, §5.4, §5.5 |
| 7 | Códigos y familias (`grants.py`); `/oauth/token` y `/oauth/revoke` | §5.2, §5.3 |
| 8 | El consentimiento: `authorize.py`, `consent.py`, `/api/v1/oauth/authorize/{details,decision}` | §5.2 |
| 9 | La puerta de MCP con el token propio; pruebas MCP re-apuntadas | §5.3, §6.5 |
| 10 | Ajustes → Conexiones de IA sin Keycloak; los actos de la cuenta revocan las concesiones | §5.3, §2.4 |
| 11 | Consola: Ajustes → Conexiones de IA (revocar por id, nombre del cliente) | §5.3 |
| 12 | Cierre: falsificadores de §10.6 de punta a punta, documentos, recuentos | §9, §10 |

## Notas de integración del coordinador

- **Oleada 1.** Las Tasks 1, 2 y 3 crean archivos distintos bajo `backend/app/core/auth/oauth/`; las tres crean también `backend/app/core/auth/oauth/__init__.py` **con el mismo contenido** (Task 1, Step 1): al fusionar se toma una copia. La Task 1 añade una línea en `backend/app/main.py` (el manejador); nadie más toca `main.py` en esta oleada. Las Tasks 4 y 11 tocan `frontend/src/lib/api.ts` en sitios distintos (métodos nuevos al final de la clase / el método `revokeMcpConnection` y sus tipos): unión.
- **Oleada 3, choques esperados** (orden de fusión: 6, 7):
  - `backend/app/main.py`: cada una incluye su router (`oauth_registration` la 6, `oauth_tokens` la 7): unión.
  - `backend/tests/oauth_support.py` (lo crea la Task 5): la 6 y la 7 añaden funciones al final: unión.
  - `backend/app/core/i18n/es.py`: unión de entradas.
- **Oleada 4, choques esperados** (orden de fusión: 8, 9):
  - `backend/tests/test_mcp_channel_api.py`: la 8 añade una ruta a `OWN_SIGN_IN_NEVER_EXPOSED` y sube el recuento a 37; la 9 cambia cada `mcp_token(` por `await mcp_token(` y re-apunta tres pruebas (y el docstring). Se conservan los dos cambios.
  - `backend/app/mcp/exclusions.py`: solo la 8 (y la 10 en la oleada 5).
  - `backend/app/core/i18n/es.py`: unión (la 8 añade entradas; la 9 añade una y borra dos que dejan de usarse).
  - `backend/tests/oauth_support.py` solo lo toca la 8, y `backend/tests/mcp_support.py` solo la 9.
- **Oleada 5.** La 10 parte de `backend/tests/test_mcp_admin_api.py` con los dos ayudantes que la 9 ya re-apuntó
  (`_old_token`, `_call_a_tool`) y reescribe el resto de las pruebas de revocar.
- **Firmas que una tarea fija y otras consumen:** el Apéndice A. Donde el bloque «Interfaces» de una tarea dice otra cosa, manda la tarea, que se escribió contra el código real.
- **Para el informe de despliegue** (cuando acaben los cuatro proyectos; el operador decidió el 2026-10-31 que nada se despliega antes): el `migrate` aplica 0247 en el siguiente `up -d`; la etiqueta de Traefik del servicio `api` cambia (Task 6), así que `docker compose up -d` recrea `api`; `ITS_MCP_REGISTRATION=open` pasa a leerlo también la API (si P2 se acepta), sin clave `.env` nueva; ninguna ruta nueva de OpenBao (la clave `auth-signing` es de la fase 1). La secuencia completa de despliegue la da el informe del proyecto 4; esta lista es lo que esta fase le añade.

## Puntos de encuentro con la fase 2

La fase 2 (diseño §3 contactos del portal, §4 Microsoft Entra) se planifica a la vez. Archivos que las dos fases probablemente tocan, y qué hace aquí la fase 3:

| Archivo | Qué hace la fase 3 | Cómo se fusiona |
|---|---|---|
| `backend/migrations/versions/` | 0247 con `down_revision "0246"` (Task 5) | después de 0246, nunca a la vez |
| `backend/app/models/__init__.py` | exporta `OAuthClient`, `OAuthCode`, `OAuthRefreshFamily`, `OAuthRefreshToken` (Task 5) | unión |
| `backend/app/core/client_scope_catalog.py` | cuatro entradas `oauth_*` (Task 5) | unión |
| `backend/app/core/client_scope.py` | `ALL_SCOPE_ALLOWLIST` gana `app/core/auth/oauth/store.py::oauth_session` (Task 5) | unión |
| `backend/tests/test_client_catalog.py` | `SURROGATE_COLUMNS` gana `oauth_codes.code_hash` y `oauth_refresh_tokens.token_hash`; el motivo de `users_org_id_external_subject_key` pasa a decir que ninguna puerta lo lee desde la fase 3 (Tasks 5 y 9) | unión |
| `backend/app/api/deps.py` | la rama MCP de `admit()` verifica el token propio (Task 9); la rama de la cookie no cambia | si la fase 2 toca `admit(contacts=True)` o `_admit_session`, se conservan los dos cambios; esta fase no toca la rama de la sesión |
| `backend/app/api/v1/auth.py` | **no lo toca**: el consentimiento va en `backend/app/api/v1/oauth_consent.py` | — |
| `backend/app/core/auth/flows.py`, `backend/app/core/auth/account.py` | una línea junto a cada `revoke_all_for_user(session, user.id)` (Task 10) | unión; si la fase 2 añade un acto que revoca los equipos recordados de un contacto, no necesita la línea (un contacto no tiene concesiones de IA: no pasa la puerta de la consola) |
| `backend/app/modules/user_admin/service.py` | una línea junto a cada `revoke_all_for_user` (Task 10) | unión |
| `backend/app/core/config.py` | `mcp_registration: str = ""` (Task 6) | unión (la fase 2 añade los de Entra) |
| `backend/app/main.py` | el manejador de `OAuthError` (Task 1) y tres routers (Tasks 6, 7, 8) | unión |
| `backend/app/core/i18n/es.py` | entradas nuevas (Tasks 6, 8, 9, 10) | unión |
| auditoría: `frontend/src/i18n/{en,es}/auditVocabulary.ts` | cuatro acciones `mcp.*` (Tasks 7, 8) | unión |
| `frontend/src/lib/globalRoutes.json` | no cambia | regenerado en el árbol fusionado si la fase 2 lo cambia |
| `backend/tests/test_logging_discipline.py` y el §2.2 de `docs/superpowers/specs/2026-08-14-logging-kernel-design.md` | +1 (Task 3), −3 (Task 10) | se suman los deltas de las dos fases y se escribe un párrafo «NOW N» por fase |
| `backend/app/mcp/exclusions.py` y `backend/tests/test_mcp_channel_api.py` (`NEVER_EXPOSED`) | +1 (`POST /api/v1/oauth/authorize/decision`), una clave de `NOT_FOR_MCP` nueva y otra cambiada (Tasks 8 y 10) | unión; el recuento final es 36 + 1 + lo de la fase 2 |
| `backend/tests/test_route_gating.py` (`UNGATED_BY_DESIGN`) | +2 rutas del consentimiento (Task 8) | unión |
| `deploy/.env.example` | comentario de `ITS_MCP_REGISTRATION` y de `KC_HOSTNAME` (Task 12) | unión |
| `deploy/README.md` | la sección «AI connections (MCP)» (Task 12) | unión: la fase 2 escribe la de Microsoft y el portal |
| `deploy/docker-compose.yml` | la regla de Traefik del router `api` (Task 6) | si la fase 2 también la cambia (la vuelta de Entra es una ruta de la consola, así que probablemente no), se conservan los dos `Path` |
| `.github/workflows/ci.yml` | **no lo toca** (fase 4) | — |
| `docs/03-security-model.md`, `docs/modules/console-auth/spec.md` | una frase cada uno, la de MCP (Task 12) | unión: la fase 2 cambia la de Microsoft y el portal |
| `backend/app/api/v1/me.py`, `backend/app/core/auth/__init__.py`, `backend/tests/conftest.py` | solo comentarios que decían «hasta la fase 3» (Task 12) | unión |
| `backend/tests/test_domain_gate_doors.py`, `backend/tests/test_auth_falsifiers.py`, `backend/tests/test_me_profile.py` | pruebas de la puerta de MCP re-apuntadas al token propio (Task 9) | si la fase 2 añade pruebas de la puerta de un contacto o de Microsoft, se conservan las dos; ninguna de las de aquí toca la rama de la sesión |
| `frontend/src/App.tsx`, `frontend/src/lib/session.ts`, `frontend/scripts/routes.mjs` | la ruta `/oauth/authorize`, la prop `bare` de `RequireAuth`, `EDGE_PATHS` (Task 4) | unión; `PUBLIC_PATHS` no cambia aquí |
| `frontend/src/i18n/{en,es}/auth.ts`, `frontend/src/modules/auth/__tests__/harness.tsx` | `auth.consent.*`, dos métodos en `calls` (Task 4) | unión |
| `frontend/src/lib/api.ts` | dos métodos y tipos del consentimiento; `revokeMcpConnection` y sus tipos (Tasks 4, 11) | unión |
| `frontend/src/test/secretMutations.test.ts` | `oauthAuthorizeDecision` en `SECRET_METHODS` y su sitio en `SECRET_SITES` (Task 4) | unión |

## Apéndice A — Contratos

### A.1 Módulos del backend (`backend/app/core/auth/oauth/`)

- `policy.py` (Task 1): `ACCESS_TOKEN_TTL_S = 600`, `CODE_TTL_S = 60`, `REFRESH_IDLE = timedelta(days=30)`, `CLIENT_IDLE = timedelta(days=30)`, `SCOPE = "mcp"`, `SCOPES_SUPPORTED = ("mcp", "offline_access")`, `GRANT_TYPES = ("authorization_code", "refresh_token")`, `ACCESS_TYP = "at+jwt"`, `AUTHORIZE_PATH = "/oauth/authorize"`, `TOKEN_PATH = "/oauth/token"`, `REGISTER_PATH = "/oauth/register"`, `REVOKE_PATH = "/oauth/revoke"`, `METADATA_PATH = "/.well-known/oauth-authorization-server"`, `JWKS_PATH = "/.well-known/jwks.json"`, `MAX_REDIRECT_URIS = 10`, `MAX_URI_LENGTH = 2000`, `MAX_CLIENT_NAME = 200`, `MAX_CLIENT_ID = 255`, `REGISTRATIONS_PER_HOUR = 20`, `MAX_REQUEST_BYTES = 16384`; `def issuer(settings: object) -> str | None`; `def endpoint(settings: object, path: str) -> str | None`; `def registration_open(settings: object) -> bool`; `def display_name(raw: str) -> str | None`.
- `errors.py` (Task 1): `class Problem(NamedTuple): error: str; description: str; status: int = 400`; las constantes `Problem` de la Task 1; `class OAuthError(Exception)` con `__init__(self, problem: Problem, *, headers: Mapping[str, str] | None = None)` y atributos `problem`, `headers`; `NO_STORE: dict[str, str]`; `async def oauth_error_handler(request, exc) -> JSONResponse`; `def no_store(content: Mapping[str, Any], status_code: int = 200) -> JSONResponse`.
- `redirects.py` (Task 1): `LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})`; `class RedirectRejected(ValueError)` con `reason: str`; `def check_registered(uri: object) -> str`; `def is_loopback(uri: str) -> bool`; `def matches(registered: Sequence[str], requested: str) -> bool`; `def host_of(uri: str) -> str`; `def with_params(uri: str, params: Mapping[str, str]) -> str`.
- `pkce.py` (Task 1): `def valid_challenge(value: str) -> bool`; `def valid_verifier(value: str) -> bool`; `def s256(verifier: str) -> str`; `def verify(verifier: str, challenge: str) -> bool`.
- `access.py` (Task 2): `@dataclass(frozen=True) class McpGrant(user_id: UUID, org_id: UUID, client_id: str, family_id: UUID, iat: int, exp: int, email: str = "")` con la propiedad `azp -> str` (= `client_id`); `async def mint_access_token(*, settings: object, user_id: UUID, org_id: UUID, client_id: str, family_id: UUID, now: int | None = None) -> tuple[str, int]`; `async def verify_access_token(token: str, *, settings: object) -> McpGrant | None`; `def unverified_client_id(token: str) -> str`; `REFRESH_EVERY_S = 5.0`.
- `cimd.py` (Task 3): `MAX_DOCUMENT_BYTES = 5 * 1024`, `FETCH_TIMEOUT_S = 5.0`, `MIN_CACHE = timedelta(minutes=5)`, `MAX_CACHE = timedelta(hours=24)`; `class DocumentRefused(Exception)` con `reason: Reason`; `@dataclass(frozen=True) class ClientDocument(client_id: str, client_name: str, redirect_uris: tuple[str, ...], cache_for: timedelta)`; `def is_document_client_id(client_id: str) -> bool`; `def cache_lifetime(cache_control: str) -> timedelta`; `async def fetch_document(client_id: str) -> ClientDocument`.
- `store.py` (Task 5): `async def deployment_org(app: Any) -> UUID | None`; `@asynccontextmanager async def oauth_session(org_id: UUID, engine: AsyncEngine) -> AsyncIterator[AsyncSession]`.
- `clients.py` (Task 5, ampliado por la 6): `@dataclass(frozen=True) class ClientView(id: UUID, client_id: str, kind: str, client_name: str | None, redirect_uris: tuple[str, ...])`; `def view(row: OAuthClient) -> ClientView`; `async def find_client(session, *, org_id: UUID, client_id: str, now: datetime) -> OAuthClient | None`; `def touch(row: OAuthClient, now: datetime) -> None`. Task 6: `@dataclass(frozen=True) class Registration(client_name: str | None, redirect_uris: tuple[str, ...])`; `def parse_registration(body: object) -> Registration`; `async def register(session, *, org_id: UUID, registration: Registration, now: datetime) -> OAuthClient`; `async def purge_idle(session, *, org_id: UUID, now: datetime) -> int`; `def registration_response(row: OAuthClient) -> dict[str, Any]`; `async def resolve_client(app: Any, *, org_id: UUID, client_id: str, now: datetime, fetch: Fetch | None = None) -> ClientView | None` (sin `fetch`, `cimd.fetch_document`, resuelto al llamar para que una prueba lo pueda sustituir; lanza `cimd.DocumentRefused`).
- `grants.py` (Task 7): `def hash_secret(value: str) -> bytes`; `def new_secret() -> str`; `@dataclass(frozen=True) class IssuedTokens(access_token: str, expires_in: int, refresh_token: str)`; `async def issue_code(session, *, org_id, user_id, oauth_client_id, redirect_uri, code_challenge, resource, now) -> str`; `async def open_family(session, *, org_id, user_id, oauth_client_id, resource, now) -> tuple[OAuthRefreshFamily, str]`; `async def exchange_code(app, *, org_id, client_id, code, redirect_uri, code_verifier) -> IssuedTokens`; `async def refresh_grant(app, *, org_id, client_id, refresh_token) -> IssuedTokens`; `async def revoke_presented(app, *, org_id, client_id, token) -> None`; `async def revoke_family(session, family_id, reason, now) -> bool`; `async def revoke_all_for_user(session, user_id, reason) -> int`; `async def revoke_app(session, *, oauth_client_id, user_id: UUID | None, reason, now) -> int`; `async def family_is_live(session, *, family_id, user_id, client_id: str, now) -> bool`.
- `authorize.py` (Task 8): `@dataclass(frozen=True) class AuthorizationRequest(client: ClientView, redirect_uri: str, state: str | None, code_challenge: str, resource: str)`; `class NotRedirectable(Exception)` con `error`, `detail`; `class Redirectable(Exception)` con `redirect_to`; `def parse_query(query: str) -> dict[str, list[str]]`; `async def validate(app, *, org_id: UUID, query: str, now: datetime) -> AuthorizationRequest`; `def answer(request: AuthorizationRequest, issuer: str, **params: str) -> str`.
- `consent.py` (Task 8): `async def details(request: Request, ctx: AuthContext, *, query: str) -> dict[str, Any]`; `async def decide(request: Request, ctx: AuthContext, *, query: str, approve: bool) -> dict[str, Any]`.
- `backend/app/core/egress.py` (Task 3): `@dataclass(frozen=True) class EgressBody(status_code: int, content: bytes, truncated: bool, content_type: str, cache_control: str)`; `async def get_bounded(url: str, *, max_bytes: int, timeout_s: float = 10.0, headers: Mapping[str, str] | None = None) -> EgressBody`.

### A.2 Rutas

| Método y ruta | Dónde | Quién | Cuerpo → respuesta |
|---|---|---|---|
| `GET /.well-known/oauth-authorization-server` | `app/api/oauth_registration.py` (Task 6) | público | metadatos RFC 8414 (Task 6, Step 5) |
| `POST /oauth/register` | ídem | público, por IP, solo con `ITS_MCP_REGISTRATION=open` | JSON RFC 7591 → 201 |
| `POST /oauth/token` | `app/api/oauth_tokens.py` (Task 7) | público (cliente público) | `application/x-www-form-urlencoded` → `{"access_token","token_type":"Bearer","expires_in":600,"refresh_token","scope":"mcp"}` |
| `POST /oauth/revoke` | ídem | público | form `token`, `client_id` → 200 vacío |
| `GET /oauth/authorize` | **la consola** (Task 4) | la persona con sesión | página |
| `POST /api/v1/oauth/authorize/details` | `app/api/v1/oauth_consent.py` (Task 8) | sesión de la consola | `{"query": str}` → `ConsentDetails` |
| `POST /api/v1/oauth/authorize/decision` | ídem | sesión de la consola | `{"query": str, "approve": bool}` → `ConsentDecision` |
| `POST /api/v1/mcp/connections/{app_id}/revoke` | `app/mcp/api.py` (Task 10; antes `{client_id}`) | `mcp.use` (+ `orgs.manage` para todos) | `{"scope": "me"\|"everyone"}` → `{"revoked": true, "grants_revoked": int}` |
| `GET /api/v1/mcp/connections` | ídem (Task 10) | `mcp.use` | cada app gana `id: UUID \| null` y `name: str \| null` |

`ConsentDetails` (y `ConsentDecision`, que nunca es `consent`):

```json
{"outcome": "consent", "client": {"name": "Claude", "client_id": "itms_…", "kind": "registered",
  "client_host": null, "redirect_host": "claude.ai", "loopback_only": false}, "scope": "mcp",
  "account": "ana@toeshee.com"}
{"outcome": "refused", "error": "invalid_redirect_uri", "detail": "<frase traducida>"}
{"outcome": "denied", "detail": "<frase traducida>", "redirect_to": "https://…?error=access_denied&…&iss=…"}
{"outcome": "redirect", "redirect_to": "https://…?code=…&state=…&iss=…"}
```

`kind` es `"registered"` (DCR) o `"metadata_document"` (CIMD); `client_host` es el host de `client_id` para un CIMD y `null` para un DCR.

### A.3 Tablas (migración 0247, Task 5)

- `oauth_clients` (G): `id uuid PK`, `org_id`, `client_id text` (DCR: `itms_` + 32 caracteres base64url; CIMD: la URL), `kind text CHECK in ('registered','metadata_document')`, `client_name text NULL`, `redirect_uris text[]`, `created_at`, `last_used_at`, `metadata_expires_at NULL` (solo CIMD); `UNIQUE (org_id, client_id)`.
- `oauth_refresh_families` (O users): `id`, `org_id`, `client_id` (el cliente MSP, del disparador), `user_id`, `oauth_client_id → oauth_clients ON DELETE CASCADE`, `resource`, `created_at`, `last_used_at`, `expires_at`, `revoked_at NULL`, `revoked_reason NULL` (lista cerrada `REVOKE_REASONS`); `UNIQUE (id, user_id)`.
- `oauth_refresh_tokens` (O users): `id`, `org_id`, `client_id`, `user_id`, `family_id`, `token_hash bytea UNIQUE` (32 bytes), `created_at`, `used_at NULL`; `(family_id, user_id) → oauth_refresh_families (id, user_id) ON DELETE CASCADE`.
- `oauth_codes` (O users): `id`, `org_id`, `client_id`, `user_id`, `oauth_client_id → oauth_clients ON DELETE CASCADE`, `code_hash bytea UNIQUE` (32 bytes), `redirect_uri`, `code_challenge` (43), `resource`, `created_at`, `expires_at`, `used_at NULL`, `family_id NULL → oauth_refresh_families ON DELETE SET NULL`.

### A.4 Consola

- `ApiClient.oauthAuthorizeDetails(query: string): Promise<ConsentDetails>` y `ApiClient.oauthAuthorizeDecision(query: string, approve: boolean): Promise<ConsentDecision>` (Task 4), ambos `{ global: true }`.
- `ApiClient.revokeMcpConnection(appId: string, scope: McpRevokeScope): Promise<McpRevokeOut>` (Task 11); `McpAppOut` gana `id: string | null` y `name: string | null`; `McpRevokeOut = { revoked: boolean; grants_revoked: number }`.
- `RequireAuth({ children, bare })`: con `bare`, la página sin el armazón de la consola (Task 4).

## Apéndice B — Decisiones del plan

Las que el diseño deja abiertas **y que el operador debe decidir** están en «Preguntas para el operador» (P1–P14) al final, con la opción que el plan implementa marcada; las de aquí son decisiones técnicas dentro del diseño, con su fuente.

- **D1. El emisor es `ITS_PUBLIC_CONSOLE_URL` sin barra final, y solo si es https sin ruta.** RFC 8414 §2: el emisor «is a URL that uses the "https" scheme and has no query or fragment components»; RFC 9207 §2, lo mismo para `iss`. Sin ruta, los metadatos están en `/.well-known/oauth-authorization-server` sin inserción (RFC 8414 §3), una ruta que Traefik ya manda a la API (`deploy/docker-compose.yml:139`). El recurso MCP sigue siendo `mcp_resource(settings)`: el mismo texto más `/mcp`.
- **D2. `/oauth/authorize` es una página de la consola, no de la API.** La cookie es `SameSite=Strict` (fase 1, §6.4), y un navegador no la envía en una navegación que empieza en otro sitio (claude.ai): una ruta de la API no vería la sesión. La página se sirve como cualquier otra de la consola (D19 de la fase 1: un `fetch` del mismo origen sí lleva la cookie) y pasa la consulta entera a `POST /api/v1/oauth/authorize/details` y `…/decision`, que la validan cada vez en el servidor. Traefik manda a la API solo `Path(/oauth/token)`, `Path(/oauth/register)` y `Path(/oauth/revoke)`.
- **D3. La organización de una petición sin persona es la del despliegue.** `/oauth/register`, `/oauth/token` y `/oauth/revoke` llegan sin cookie ni persona; usan `ITS_DEFAULT_ORG_SLUG`, la misma regla que el inicio de sesión de la fase 1 (`flows.login` responde 503 sin él). El id se lee por slug en el motor de administración, la consulta de arranque que `deps.admit` hacía para el token de Keycloak y que esta fase le quita (pregunta P10 para el multi-tenant).
- **D4. Solo clientes públicos.** El diseño pone `none` en `token_endpoint_auth_methods_supported` (§5.1) y Claude se registra como cliente público [S33]. Un registro que pide otro método se registra con `none` y la respuesta lo dice (RFC 7591 §3.2.1: «The authorization server MAY reject or replace any of the client's requested metadata values»); una petición al token con cabecera `Authorization` es `invalid_client` 401 con `WWW-Authenticate: Basic` (RFC 6749 §5.2). Pregunta P9.
- **D5. El token de acceso sigue RFC 9068 en `typ` y claims, firmado ES256.** `typ: at+jwt`, `iss`, `exp`, `aud`, `sub`, `client_id`, `iat`, `jti` (RFC 9068 §2.1–§2.2), más `org` y `fam`. RFC 9068 §2.1 dice además «MUST include RS256 … among their supported signature algorithms», y el diseño fija ES256 (§5.3, §6.3): pregunta P7.
- **D6. El token de acceso nombra su familia y la puerta la comprueba en cada llamada.** §6.5: «MCP valida su JWT (`iss`, `aud`, `kid`, revocación)». Con `fam`, una familia revocada (reutilización, revocación en Ajustes o por el cliente, desactivar a la persona) deja de valer en la siguiente llamada y no a los diez minutos; `mcp_revocation` se queda para lo que ya hacía (el `iat` frente a `revoked_at`, por persona u organización).
- **D7. El ámbito concedido es siempre `mcp`, y siempre hay refresh.** RFC 6749 §3.3: «The authorization server MAY fully or partially ignore the scope requested by the client»; MCP: el cliente «MUST NOT assume refresh tokens will be issued; the AS retains discretion» [S27]. Los metadatos anuncian `offline_access` porque Claude lo añade solo cuando aparece en `scopes_supported` [S33]; cualquier otro ámbito pedido (`openid`, `profile`) se ignora, y la respuesta del token lleva `scope: "mcp"`.
- **D8. Redirects (§5.2).** https a cualquier host, o http a `127.0.0.1`, `[::1]` o `localhost` (MCP: «All redirect URIs MUST be either localhost or use HTTPS» [S30]); sin fragmento (RFC 6749 §3.1.2); sin usuario ni contraseña, sin `*`, de 2000 caracteres como mucho. Comparación exacta salvo el puerto en loopback (RFC 9700 [S16]; RFC 8252 §7.3: «The authorization server MUST allow any port to be specified at the time of the request for loopback IP redirect URIs»); `localhost`, `127.0.0.1` y `[::1]` no son intercambiables. La consulta del redirect se conserva al añadir `code`, `state` e `iss` (RFC 6749 §3.1.2: «MUST be retained when adding additional query parameters»).
- **D9. Límites de registro.** De 1 a 10 redirects, nombre de 200 caracteres como mucho (sin caracteres de control), `grant_types` ⊆ {`authorization_code`, `refresh_token`} con el primero, `response_types` = {`code`}, cuerpo de 16 KiB; lo desconocido se ignora (RFC 7591 §2: «The authorization server MUST ignore any client metadata sent by the client that it does not understand»).
- **D10. La caducidad de los registros es perezosa.** Un cliente sin uso en 30 días no se encuentra (`find_client` lo trata como inexistente) y cada registro nuevo borra hasta 100 caducados de la organización (`purge_idle`), con sus familias en cascada. El único crecimiento de la tabla es el registro, así que la limpieza lo acompaña; no hace falta una tarea nueva del worker (`app/worker.py` no se toca). Lo mismo para los códigos (los de la persona caducados hace más de 10 min se borran al emitir otro) y las familias (las de la persona terminadas hace más de un día, al abrir otra).
- **D11. El consentimiento comprueba `mcp_enabled` y `mcp.use` antes de pedir nada.** El diseño pide que MCP siga apagado (§5.4, 1c(d)); la puerta lo comprueba en cada llamada, y comprobarlo también aquí evita un código que no podría usarse: la página dice por qué y ofrece volver a la aplicación con `access_denied` (RFC 6749 §4.1.2.1). Por eso las dos rutas son «sin código de permiso» en `UNGATED_BY_DESIGN`, con `mcp.use` comprobado en el servicio, y no `require_permission("mcp.use")`, cuyo 403 no dejaría construir la vuelta al cliente.
- **D12. Revocar en Ajustes es por el id de la fila del cliente.** El `client_id` de un cliente CIMD es una URL con `/`, que no cabe en un segmento de ruta; la ruta pasa a `/mcp/connections/{app_id}/revoke` (UUID) y la lista da `id` y `name`. Sin Keycloak, la revocación es una transacción: la fila `mcp_revocation`, las familias de la app (de la persona o de todos) y `mcp.app_revoked`, juntas; `keycloak_failures` desaparece de la respuesta.
- **D13. Los errores RFC no se traducen.** Son para el desarrollador del cliente (RFC 6749 §5.2) y ASCII; viven como constantes `Problem` en `errors.py`, de modo que ninguna llamada lleva un literal en inglés y la prueba del catálogo (`tests/test_i18n_catalog.py`) no necesita excepciones línea a línea. Lo que ve una persona sí va por `tr()`.
- **D14. Un `kid` desconocido no hace leer OpenBao en cada petición.** `/mcp` es público; un token con `kid` inventado forzaría `keys.jwks(refresh=True)` por petición. `access.py` vuelve a leer las claves públicas como mucho una vez cada `REFRESH_EVERY_S` (5 s) por proceso, la defensa que `JwksCache` tenía para Keycloak (`security.py`, «minimum interval between refetches»). El token interno del asistente no cambia (solo lo presenta el propio proceso).
- **D15. `ITS_MCP_REGISTRATION=open` abre el registro y el CIMD en la API** (pregunta P2). La API lee `mcp_registration` (nuevo ajuste, `env_file: .env` ya lo carga en el contenedor `api`); sin él, los metadatos no anuncian `registration_endpoint` ni `client_id_metadata_document_supported`, `POST /oauth/register` responde 403 `access_denied` y un `client_id` URL no se descarga.

## Apéndice C — Fuentes leídas por este plan (2026-11-06)

Además de [S1]–[S65] del dossier. Cada cita del plan está copiada del texto descargado ese día con `curl` del sitio del editor.

- [P-6749] RFC 6749, The OAuth 2.0 Authorization Framework. https://www.rfc-editor.org/rfc/rfc6749.txt — §3.1 y §3.2 («Parameters sent without a value MUST be treated as if they were omitted from the request. The authorization server MUST ignore unrecognized request parameters. Request and response parameters MUST NOT be included more than once.»); §3.1.2 («The endpoint URI MAY include an "application/x-www-form-urlencoded" formatted (per Appendix B) query component ([RFC3986] Section 3.4), which MUST be retained when adding additional query parameters. The endpoint URI MUST NOT include a fragment component.»); §3.3 (arriba, D7); §4.1.2 («If an authorization code is used more than once, the authorization server MUST deny the request and SHOULD revoke (when possible) all tokens previously issued based on that authorization code.»); §4.1.2.1 («If the request fails due to a missing, invalid, or mismatching redirection URI, or if the client identifier is missing or invalid, the authorization server SHOULD inform the resource owner of the error and MUST NOT automatically redirect the user-agent to the invalid redirection URI.»; los códigos `invalid_request`, `unauthorized_client`, `access_denied`, `unsupported_response_type`, `invalid_scope`, `server_error`, `temporarily_unavailable`); §4.1.3 (`redirect_uri` «REQUIRED, if the "redirect_uri" parameter was included in the authorization request … and their values MUST be identical»; `client_id` «REQUIRED, if the client is not authenticating with the authorization server»); §5.1 («The authorization server MUST include the HTTP "Cache-Control" response header field [RFC2616] with a value of "no-store" in any response containing tokens, credentials, or other sensitive information, as well as the "Pragma" response header field [RFC2616] with a value of "no-cache".»); §5.2 (`invalid_request`, `invalid_client` —«If the client attempted to authenticate via the "Authorization" request header field, the authorization server MUST respond with an HTTP 401 (Unauthorized) status code and include the "WWW-Authenticate" response header field»—, `invalid_grant`, `unsupported_grant_type`, y `error_description` limitado a `%x20-21 / %x23-5B / %x5D-7E`).
- [P-7591] RFC 7591, Dynamic Client Registration. https://www.rfc-editor.org/rfc/rfc7591.txt — §2 (`token_endpoint_auth_method`: «"none": The client is a public client … If unspecified or omitted, the default is "client_secret_basic"»; «The authorization server MUST ignore any client metadata sent by the client that it does not understand»); §3.2 («an HTTP 201 Created status code and a body of type "application/json"»); §3.2.1 (`client_id` REQUIRED, `client_id_issued_at` OPTIONAL, «the authorization server MUST return all registered metadata about this client … MAY reject or replace any of the client's requested metadata values»); §3.2.2 (`invalid_redirect_uri`, `invalid_client_metadata`).
- [P-7636] RFC 7636, PKCE. https://www.rfc-editor.org/rfc/rfc7636.txt — §4.1 (verificador de 43 a 128 caracteres `[A-Z] / [a-z] / [0-9] / "-" / "." / "_" / "~"`); §4.2 (`code_challenge = BASE64URL-ENCODE(SHA256(ASCII(code_verifier)))`); §4.3 (`code_challenge_method` «OPTIONAL, defaults to "plain" if not present in the request»); §4.4.1 (sin `code_challenge`, «the authorization endpoint MUST return the authorization error response with the "error" value set to "invalid_request"»); §4.6 (verificador que no coincide: «an error response indicating "invalid_grant" … MUST be returned»).
- [P-7009] RFC 7009, Token Revocation. https://www.rfc-editor.org/rfc/rfc7009.txt — §2.1 («The authorization server first validates the client credentials (in case of a confidential client) and then verifies whether the token was issued to the client making the revocation request. If this validation fails, the request is refused and the client is informed of the error»); §2.2 («responds with HTTP status code 200 if the token has been revoked successfully or if the client submitted an invalid token»; «An invalid token type hint value is ignored»).
- [P-8414] RFC 8414, Authorization Server Metadata. https://www.rfc-editor.org/rfc/rfc8414.txt — §2: `issuer` REQUIRED; `authorization_endpoint`, `token_endpoint`; `jwks_uri` OPTIONAL, https; `registration_endpoint` OPTIONAL; `scopes_supported` RECOMMENDED; `response_types_supported` REQUIRED; `grant_types_supported`, `token_endpoint_auth_methods_supported` (por defecto `client_secret_basic`), `revocation_endpoint`, `revocation_endpoint_auth_methods_supported`, `code_challenge_methods_supported` OPTIONAL.
- [P-9207] RFC 9207, Authorization Server Issuer Identification. https://www.rfc-editor.org/rfc/rfc9207.txt — §2 («In authorization responses to the client, including error responses, an authorization server supporting this specification MUST indicate its identity by including the iss parameter in the response.»; «Its value MUST be a URL that uses the "https" scheme without any query or fragment components.»); §2.3 (`issuer` de los metadatos idéntico a `iss`; `authorization_response_iss_parameter_supported: true`).
- [P-9068] RFC 9068, JWT Profile for OAuth 2.0 Access Tokens. https://www.rfc-editor.org/rfc/rfc9068.txt — §2.1 («Authorization servers and resource servers conforming to this specification MUST include RS256 (as defined in [RFC7518]) among their supported signature algorithms.»; «the "typ" value used SHOULD be "at+jwt"»); §2.2 (`iss`, `exp`, `aud`, `sub`, `client_id`, `iat`, `jti` REQUIRED); §4 («The resource server MUST verify that the "typ" header value is "at+jwt" or "application/at+jwt" and reject tokens carrying any other value.»).
- [P-8252] RFC 8252, OAuth 2.0 for Native Apps. https://www.rfc-editor.org/rfc/rfc8252.txt — §7.3 (arriba, D8).
- [P-8707] RFC 8707, Resource Indicators. https://www.rfc-editor.org/rfc/rfc8707.txt — §2 («Its value MUST be an absolute URI … The URI MUST NOT include a fragment component.»; «the authorization server MAY require clients to specify the resource(s) they intend to access and MAY fail requests that omit the parameter with an "invalid_target" error»).
- [P-CIMD] draft-ietf-oauth-client-id-metadata-document-00 (la revisión que cita MCP [S27]). https://www.ietf.org/archive/id/draft-ietf-oauth-client-id-metadata-document-00.txt — §3 («Client identifier URLs MUST have an "https" scheme, MUST contain a path component, MUST NOT contain single-dot or double-dot path segments, MUST NOT contain a fragment component and MUST NOT contain a username or password Client identifier URLs SHOULD NOT include a query string component, and MAY contain a port.»); §4.1 (`client_id` que «MUST match the URL of the document using simple string comparison»; «the token_endpoint_auth_method property MUST NOT include client_secret_post, client_secret_basic, client_secret_jwt, or any other method based around a shared symmetric secret»; «the client_secret and client_secret_expires_at properties MUST NOT be used»); §4.3 («If fetching the metadata document fails, the authorization server SHOULD abort the authorization request.»); §4.4 («SHOULD respect HTTP cache headers [RFC9111] when caching client metadata, but MAY define its own upper and/or lower bounds»; «MUST NOT cache error responses … MUST NOT cache documents which are invalid or malformed»); §6.4 («The authorization server SHOULD display the hostname of the client_id on the authorization interface»); §6.6 («The recommended maximum response size for client metadata documents is 5 kilobytes.»).

---

## Las tareas, una a una

### Task 1: Primitivas puras del servidor OAuth: `policy`, `errors`, `redirects`, `pkce`

Diseño §5.1–§5.3. Oleada 1, en paralelo con las Tasks 2, 3, 4 y 11. Solo backend, sin base de datos.

Las cuatro piezas que el resto de la fase consume y que no necesitan ni la base de datos ni la red: los números y las
direcciones del servidor (`policy.py`), el cuerpo de error de RFC 6749 (`errors.py`), qué redirects se registran y
cuándo coincide uno pedido (`redirects.py`) y PKCE `S256` (`pkce.py`). La tarea registra además el manejador de
`OAuthError` en `create_app`, para que las rutas de las Tasks 6, 7 y 8 solo tengan que lanzarlo.

**Files:**
- Create: `backend/app/core/auth/oauth/__init__.py`
- Create: `backend/app/core/auth/oauth/policy.py`
- Create: `backend/app/core/auth/oauth/errors.py`
- Create: `backend/app/core/auth/oauth/redirects.py`
- Create: `backend/app/core/auth/oauth/pkce.py`
- Modify: `backend/app/main.py` (junto al registro de `AuthRefusal`, `:315-320` hoy)
- Test: `backend/tests/test_oauth_primitives.py`

**Interfaces:**
- Consumes: `app.core.mcp_channel.mcp_resource(settings) -> str | None` (`backend/app/core/mcp_channel.py:60-62`);
  `starlette.responses.JSONResponse`.
- Produces: todo `policy.py`, `errors.py`, `redirects.py` y `pkce.py` del Apéndice A.1, con las constantes `Problem`
  que define el Step 3 (sus nombres los usan las Tasks 6, 7 y 8).

**Decisión de la tarea (1): un `Problem` por error, constante.** D13: el cuerpo de RFC 6749 §5.2 va en inglés y ASCII,
y no pasa por `tr()`. Cada error es una constante `Problem(error, description, status)` del módulo, y una ruta lanza
`OAuthError(BAD_CODE)`: ninguna llamada lleva un literal en inglés, que es lo que `tests/test_i18n_catalog.py` busca en
las llamadas a excepciones. El constructor comprueba el conjunto de caracteres de §5.2, así que un `Problem` con una
comilla o un acento falla al importar el módulo, no ante un cliente.

**Decisión de la tarea (2): `issuer` exige https sin ruta.** D1. Un `ITS_PUBLIC_CONSOLE_URL` con ruta, consulta,
usuario o en http no nombra un emisor (RFC 8414 §2, RFC 9207 §2), y las rutas OAuth responden 503 (Tasks 6 y 7).

- [ ] **Step 1: El paquete**

`backend/app/core/auth/oauth/__init__.py` (las Tasks 2 y 3 lo crean igual; al fusionar se toma una copia):

```python
"""The platform's own OAuth 2.1 authorization server for MCP (autenticación propia
design §5; plan 2026-11-06).

`policy` (numbers and addresses), `errors` (RFC 6749 error bodies),
`redirects` and `pkce` (pure checks), `access` (the access token), `cimd`
(client metadata documents), `store` (the one session over these rows),
`clients`, `grants`, `authorize` and `consent`.
"""
```

- [ ] **Step 2: La prueba que falla**

`backend/tests/test_oauth_primitives.py`:

```python
"""The OAuth server's pure pieces (autenticación propia plan 2026-11-06, Task 1).

What is pinned here, each with its source:

- the issuer is the console's https address and nothing else (RFC 8414 §2,
  RFC 9207 §2), and its endpoints hang off it;
- the registration switch reads `ITS_MCP_REGISTRATION=open` exactly;
- an error body is RFC 6749 §5.2's, ASCII, and never cached (§5.1);
- a redirect is https, or http to the machine itself (MCP [S30]), without a
  fragment (RFC 6749 §3.1.2), and matches exactly but for a loopback port
  (RFC 9700 [S16], RFC 8252 §7.3);
- PKCE is S256, with RFC 7636 Appendix B's own vector.
"""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.core.auth.oauth import errors, pkce, policy, redirects
from app.core.auth.oauth.errors import OAuthError, Problem


def _settings(url: str | None, registration: str = "") -> SimpleNamespace:
    return SimpleNamespace(public_console_url=url, mcp_registration=registration)


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://itms.example.com", "https://itms.example.com"),
        ("https://itms.example.com/", "https://itms.example.com"),
        ("  https://itms.example.com  ", "https://itms.example.com"),
        ("https://itms.example.com:8443", "https://itms.example.com:8443"),
        ("http://itms.example.com", None),
        ("https://itms.example.com/console", None),
        ("https://itms.example.com/?x=1", None),
        ("https://user@itms.example.com", None),
        ("https://itms.example.com:99999", None),
        ("", None),
        (None, None),
    ],
)
def test_the_issuer_is_the_consoles_https_address(url: str | None, expected: str | None) -> None:
    assert policy.issuer(_settings(url)) == expected


def test_the_endpoints_hang_off_the_issuer() -> None:
    settings = _settings("https://itms.example.com/")
    assert policy.endpoint(settings, policy.TOKEN_PATH) == "https://itms.example.com/oauth/token"
    assert policy.endpoint(settings, policy.METADATA_PATH) == (
        "https://itms.example.com/.well-known/oauth-authorization-server"
    )
    assert policy.endpoint(_settings(None), policy.TOKEN_PATH) is None


@pytest.mark.parametrize(
    ("value", "open_"), [("open", True), (" open ", True), ("OPEN", False), ("yes", False), ("", False)]
)
def test_registration_opens_only_on_the_exact_word(value: str, open_: bool) -> None:
    assert policy.registration_open(_settings("https://x.example", value)) is open_


def test_a_display_name_keeps_text_and_nothing_else() -> None:
    assert policy.display_name("  Cla\u0000ude\n  Desktop\t ") == "Claude Desktop"
    assert policy.display_name("\u0007\u0008") is None
    assert len(policy.display_name("x" * 500) or "") == policy.MAX_CLIENT_NAME


def test_the_numbers_are_the_designs() -> None:
    """§5.2: a code of 60 s; §5.3: an access token of 10 min, a refresh family
    and (§5.4) a registration that die after 30 days unused."""
    assert policy.CODE_TTL_S == 60
    assert policy.ACCESS_TOKEN_TTL_S == 600
    assert policy.REFRESH_IDLE.days == 30
    assert policy.CLIENT_IDLE.days == 30
    assert policy.SCOPE == "mcp"


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


def test_every_problem_is_ascii_as_rfc_6749_allows() -> None:
    problems = [v for v in vars(errors).values() if isinstance(v, Problem)]
    assert len(problems) >= 20
    for problem in problems:
        assert errors.ALLOWED.fullmatch(problem.error), problem
        assert errors.ALLOWED.fullmatch(problem.description), problem
        assert 400 <= problem.status < 600


def test_a_problem_outside_the_character_set_is_refused_at_construction() -> None:
    with pytest.raises(ValueError):
        OAuthError(Problem("invalid_request", 'say "no"'))
    with pytest.raises(ValueError):
        OAuthError(Problem("invalid_request", "código"))


async def test_the_handler_answers_the_rfc_body_and_is_never_cached() -> None:
    app = FastAPI()
    app.add_exception_handler(OAuthError, errors.oauth_error_handler)

    @app.post("/boom")
    async def boom() -> None:
        raise OAuthError(errors.CLIENT_AUTH, headers={"WWW-Authenticate": 'Basic realm="itms"'})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://x.example"
    ) as client:
        resp = await client.post("/boom")
    assert resp.status_code == 401
    assert resp.json() == {
        "error": "invalid_client",
        "error_description": errors.CLIENT_AUTH.description,
    }
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["pragma"] == "no-cache"
    assert resp.headers["www-authenticate"] == 'Basic realm="itms"'


def test_no_store_answers_carry_both_headers() -> None:
    resp = errors.no_store({"a": 1}, status_code=201)
    assert resp.status_code == 201
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["pragma"] == "no-cache"


# --------------------------------------------------------------------------- #
# redirects
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "uri",
    [
        "https://claude.ai/api/mcp/auth_callback",
        "http://127.0.0.1:33418/callback",
        "http://[::1]/callback",
        "http://localhost:1/callback",
        "https://app.example/cb?tenant=a",
    ],
)
def test_an_https_or_loopback_redirect_registers(uri: str) -> None:
    assert redirects.check_registered(uri) == uri


@pytest.mark.parametrize(
    ("uri", "reason"),
    [
        ("http://evil.example/cb", "scheme"),
        ("myapp://cb", "scheme"),
        ("javascript:alert(1)", "scheme"),
        ("https://claude.ai/cb#x", "fragment"),
        ("https://u:p@claude.ai/cb", "credentials"),
        ("https://*.claude.ai/cb", "malformed"),
        ("https://claude.ai/ cb", "malformed"),
        ("https://claude.ai:99999/cb", "malformed"),
        ("https://claude.ai/" + "a" * 2000, "malformed"),
        (5, "malformed"),
        ("", "malformed"),
    ],
)
def test_any_other_redirect_is_refused_with_its_reason(uri: object, reason: str) -> None:
    with pytest.raises(redirects.RedirectRejected) as refused:
        redirects.check_registered(uri)
    assert refused.value.reason == reason


def test_a_redirect_matches_exactly() -> None:
    registered = ["https://claude.ai/api/mcp/auth_callback"]
    assert redirects.matches(registered, "https://claude.ai/api/mcp/auth_callback")
    assert not redirects.matches(registered, "https://claude.ai/api/mcp/auth_callback/")
    assert not redirects.matches(registered, "https://claude.ai:444/api/mcp/auth_callback")
    assert not redirects.matches(registered, "https://CLAUDE.ai/api/mcp/auth_callback")


def test_a_loopback_redirect_matches_on_any_port_and_nothing_else() -> None:
    assert redirects.matches(["http://127.0.0.1:33418/callback"], "http://127.0.0.1:51004/callback")
    assert redirects.matches(["http://localhost/callback"], "http://localhost:51004/callback")
    assert not redirects.matches(["http://localhost/callback"], "http://127.0.0.1:51004/callback")
    assert not redirects.matches(["http://127.0.0.1:1/callback"], "http://127.0.0.1:2/other")
    assert not redirects.matches(["http://127.0.0.1:1/cb"], "http://127.0.0.1:2/cb?x=1")
    assert not redirects.matches(["http://127.0.0.1:1/cb"], "http://u@127.0.0.1:2/cb")


def test_the_answer_keeps_the_redirects_own_query() -> None:
    assert redirects.with_params("https://h.example/cb?x=1", {"code": "c d", "iss": "https://i"}) == (
        "https://h.example/cb?x=1&code=c+d&iss=https%3A%2F%2Fi"
    )


def test_the_host_shown_to_the_person() -> None:
    assert redirects.host_of("https://claude.ai/api/mcp/auth_callback") == "claude.ai"
    assert redirects.host_of("http://[::1]:5/cb") == "::1"
    assert redirects.is_loopback("http://localhost:7/cb")
    assert not redirects.is_loopback("https://localhost/cb")


# --------------------------------------------------------------------------- #
# pkce
# --------------------------------------------------------------------------- #

#: RFC 7636 Appendix B.
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_s256_is_rfc_7636s_own_example() -> None:
    assert pkce.s256(VERIFIER) == CHALLENGE
    assert pkce.verify(VERIFIER, CHALLENGE)


def test_a_wrong_or_malformed_verifier_never_verifies() -> None:
    assert not pkce.verify(VERIFIER[:-1] + "Y", CHALLENGE)
    assert not pkce.verify("x" * 42, pkce.s256("x" * 42))  # under 43 characters
    assert not pkce.verify("x" * 129, pkce.s256("x" * 129))
    assert not pkce.verify("x" * 43 + "!", CHALLENGE)


def test_a_challenge_is_43_base64url_characters() -> None:
    assert pkce.valid_challenge(CHALLENGE)
    assert not pkce.valid_challenge(CHALLENGE + "=")
    assert not pkce.valid_challenge(CHALLENGE[:-1])
    assert not pkce.valid_challenge("plain-text-is-not-a-challenge-of-any-kind!!")
```

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_primitives.py`
Expected: FAIL, `ModuleNotFoundError: No module named 'app.core.auth.oauth'`.

- [ ] **Step 3: `policy.py`, `errors.py`, `redirects.py`, `pkce.py`**

`backend/app/core/auth/oauth/policy.py`:

```python
"""The OAuth server's numbers and addresses (autenticación propia design §5.1-§5.4).

Every number is the design's:

- an access token lives 10 minutes (§5.3) and an authorization code 60
  seconds (§5.2);
- a refresh token's family dies after 30 days without use, and so does a
  registration nobody used (§5.3, §5.4; RFC 7591 lets a server "expire
  registrations for existing clients meeting certain criteria" [S19]);
- the only scope is `mcp`, the one the protected-resource metadata has always
  named (`app/mcp/server.py`). `offline_access` is advertised beside it
  because Claude asks for a refresh token only when it is (plan D7, [S33]).

THE ISSUER IS THE CONSOLE'S ADDRESS (plan D1): `ITS_PUBLIC_CONSOLE_URL` with
no trailing slash, the same base `mcp_resource` puts `/mcp` after. RFC 8414
§2 wants "a URL that uses the "https" scheme and has no query or fragment
components", RFC 9207 §2 the same of `iss`; a console address with a path
would move the metadata to a path-inserted URL Traefik does not route. So an
address that is not https, has a path, a query, a fragment or a user name
names no issuer, and every OAuth route answers 503.
"""

from datetime import timedelta
from urllib.parse import urlsplit

ACCESS_TOKEN_TTL_S = 600
CODE_TTL_S = 60
REFRESH_IDLE = timedelta(days=30)
CLIENT_IDLE = timedelta(days=30)
SCOPE = "mcp"
SCOPES_SUPPORTED = (SCOPE, "offline_access")
GRANT_TYPES = ("authorization_code", "refresh_token")
#: RFC 9068 §2.1: "the "typ" value used SHOULD be "at+jwt"" (plan D5).
ACCESS_TYP = "at+jwt"

AUTHORIZE_PATH = "/oauth/authorize"
TOKEN_PATH = "/oauth/token"
REGISTER_PATH = "/oauth/register"
REVOKE_PATH = "/oauth/revoke"
METADATA_PATH = "/.well-known/oauth-authorization-server"
JWKS_PATH = "/.well-known/jwks.json"

#: What a registration may hold (plan D9).
MAX_REDIRECT_URIS = 10
MAX_URI_LENGTH = 2000
MAX_CLIENT_NAME = 200
MAX_CLIENT_ID = 255
#: Registrations one network address may make per hour (§5.4; question P3).
REGISTRATIONS_PER_HOUR = 20
#: Bytes a registration, token or revocation request may carry.
MAX_REQUEST_BYTES = 16 * 1024


def issuer(settings: object) -> str | None:
    """The issuer identifier, or None when the console address cannot be one."""
    raw = (getattr(settings, "public_console_url", None) or "").strip().rstrip("/")
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname:
        return None
    if parts.path or parts.query or parts.fragment or parts.username is not None:
        return None
    return raw


def endpoint(settings: object, path: str) -> str | None:
    """`issuer + path`, or None with no issuer."""
    base = issuer(settings)
    return None if base is None else base + path


def registration_open(settings: object) -> bool:
    """`ITS_MCP_REGISTRATION=open`, the exact word, as `keycloak-init` reads it
    (deploy/README.md "AI connections (MCP)"; plan D15, question P2)."""
    return (getattr(settings, "mcp_registration", "") or "").strip() == "open"


def display_name(raw: str) -> str | None:
    """A client's name as a person may be shown it: printable text, runs of
    white space as one space, at most `MAX_CLIENT_NAME` characters; None when
    nothing is left."""
    printable = "".join(
        " " if ch.isspace() else ch for ch in raw if ch.isprintable() or ch.isspace()
    )
    text = " ".join(printable.split())[:MAX_CLIENT_NAME].strip()
    return text or None
```

`backend/app/core/auth/oauth/errors.py`:

```python
"""RFC 6749 error bodies for the OAuth endpoints (design §5; plan D13).

`{"error": <code>, "error_description": <sentence>}`, RFC 6749 §5.2, with
`Cache-Control: no-store` and `Pragma: no-cache` on every answer (§5.1: "in
any response containing tokens, credentials, or other sensitive
information").

NOT TRANSLATED, ON PURPOSE. §5.2 addresses `error_description` to "the client
developer" and limits it to `%x20-21 / %x23-5B / %x5D-7E`: no accent, no
quote, no backslash. So each error is a `Problem` constant here, in English,
and a route raises `OAuthError(BAD_CODE)` -- no call site holds an English
literal, and the constructor refuses a description outside the set, so a bad
one fails at import, never in front of a client. What a PERSON reads (the
consent screen, `/mcp`, Settings) goes through `tr()` as always.
"""

import re
from collections.abc import Mapping
from typing import Any, NamedTuple

from starlette.requests import Request
from starlette.responses import JSONResponse

#: RFC 6749 §5.2's character set for `error` and `error_description`.
ALLOWED = re.compile(r"[\x20-\x21\x23-\x5b\x5d-\x7e]+")
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class Problem(NamedTuple):
    error: str
    description: str
    status: int = 400


class OAuthError(Exception):
    """One RFC 6749 error, answered by `oauth_error_handler`."""

    def __init__(self, problem: Problem, *, headers: Mapping[str, str] | None = None) -> None:
        if not ALLOWED.fullmatch(problem.error) or not ALLOWED.fullmatch(problem.description):
            raise ValueError(  # i18n-exempt: programming error, never reaches a person
                "an OAuth error is ASCII without quotes or backslashes (RFC 6749 §5.2)"
            )
        super().__init__(problem.error)
        self.problem = problem
        self.headers = dict(headers or {})


def no_store(content: Mapping[str, Any], status_code: int = 200) -> JSONResponse:
    """A JSON answer that no cache keeps (RFC 6749 §5.1)."""
    return JSONResponse(dict(content), status_code=status_code, headers=NO_STORE)


async def oauth_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, OAuthError)
    return JSONResponse(
        {"error": exc.problem.error, "error_description": exc.problem.description},
        status_code=exc.problem.status,
        headers={**NO_STORE, **exc.headers},
    )


# --------------------------------------------------------------------------- #
# The errors. RFC 6749 §5.2 and RFC 7591 §3.2.2 codes; RFC 8707 §2's
# `invalid_target`. `temporarily_unavailable` and `access_denied` are §4.1.2.1's,
# reused for the two refusals RFC 7591 gives no code for.
# --------------------------------------------------------------------------- #

UNAVAILABLE = Problem(
    "temporarily_unavailable",
    "the authorization server is not configured on this installation",
    503,
)
KEYS_UNAVAILABLE = Problem(
    "temporarily_unavailable", "the signing keys are not available right now; try again shortly", 503
)
REGISTRATION_CLOSED = Problem(
    "access_denied", "client registration is closed on this server", 403
)
SLOW_DOWN = Problem(
    "temporarily_unavailable", "too many registrations from this network; try again later", 429
)
NOT_JSON = Problem("invalid_client_metadata", "the registration must be one JSON object")
NO_REDIRECTS = Problem(
    "invalid_redirect_uri", "redirect_uris must list between one and ten URIs"
)
BAD_REDIRECT = Problem(
    "invalid_redirect_uri",
    "each redirect URI must be https, or http to 127.0.0.1, [::1] or localhost, with no fragment",
)
BAD_GRANT_TYPES = Problem(
    "invalid_client_metadata",
    "grant_types must hold authorization_code, and may add refresh_token",
)
BAD_RESPONSE_TYPES = Problem("invalid_client_metadata", "response_types may hold only code")
BAD_NAME = Problem("invalid_client_metadata", "client_name must be text")
NOT_FORM = Problem("invalid_request", "send application/x-www-form-urlencoded")
MALFORMED = Problem("invalid_request", "the request is malformed")
REPEATED = Problem("invalid_request", "a parameter appears more than once")
MISSING = Problem("invalid_request", "a required parameter is missing")
CLIENT_AUTH = Problem(
    "invalid_client",
    "this server has public clients only: send client_id and no client authentication",
    401,
)
UNKNOWN_CLIENT = Problem(
    "invalid_client", "the client is not registered here, or its registration expired"
)
UNSUPPORTED_GRANT = Problem(
    "unsupported_grant_type", "only authorization_code and refresh_token are supported"
)
BAD_CODE = Problem("invalid_grant", "the authorization code is not valid")
BAD_VERIFIER = Problem(
    "invalid_grant", "the code_verifier does not match the code_challenge"
)
BAD_REFRESH = Problem("invalid_grant", "the refresh token is not valid")
WRONG_CLIENT = Problem("invalid_grant", "the token was issued to another client")
BAD_TARGET = Problem(
    "invalid_target", "the only resource this server issues tokens for is its MCP server"
)
BAD_SCOPE = Problem("invalid_scope", "the only scope this server grants is mcp")
```

`backend/app/core/auth/oauth/redirects.py`:

```python
"""Redirect URIs: which may be registered, and when a request's matches
(design §5.2; plan D8).

- https to any host, or http to the machine itself. MCP: "All redirect URIs
  MUST be either localhost or use HTTPS" [S30];
- no fragment: RFC 6749 §3.1.2, "The endpoint URI MUST NOT include a fragment
  component";
- no user name or password, no `*`, no white space or control character, at
  most `MAX_URI_LENGTH` characters;
- compared EXACTLY, as strings, "except for port numbers in localhost
  redirection URIs of native apps" (RFC 9700 [S16]); RFC 8252 §7.3: "The
  authorization server MUST allow any port to be specified at the time of the
  request for loopback IP redirect URIs". `localhost`, `127.0.0.1` and
  `[::1]` are each matched as registered, never as one another; path and
  query must be the same.

The answer keeps the redirect's own query (RFC 6749 §3.1.2: "MUST be retained
when adding additional query parameters").
"""

from collections.abc import Mapping, Sequence
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

from app.core.auth.oauth.policy import MAX_URI_LENGTH

#: `urlsplit` gives an IPv6 literal without its brackets.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class RedirectRejected(ValueError):
    """Why a redirect cannot be registered: `malformed`, `fragment`,
    `credentials` or `scheme`. A reason code, never shown to a person."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _split(uri: str) -> SplitResult:
    try:
        parts = urlsplit(uri)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError as exc:
        raise RedirectRejected("malformed") from exc  # i18n-exempt: a reason code
    return parts


def check_registered(uri: object) -> str:
    """`uri` unchanged when it may be registered; `RedirectRejected` if not."""
    if not isinstance(uri, str) or not uri or len(uri) > MAX_URI_LENGTH:
        raise RedirectRejected("malformed")  # i18n-exempt: a reason code
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in uri) or "*" in uri:
        raise RedirectRejected("malformed")  # i18n-exempt: a reason code
    if "#" in uri:
        raise RedirectRejected("fragment")  # i18n-exempt: a reason code
    parts = _split(uri)
    if parts.username is not None or parts.password is not None:
        raise RedirectRejected("credentials")  # i18n-exempt: a reason code
    host = parts.hostname or ""
    if parts.scheme == "https" and host:
        return uri
    if parts.scheme == "http" and host in LOOPBACK_HOSTS:
        return uri
    raise RedirectRejected("scheme")  # i18n-exempt: a reason code


def is_loopback(uri: str) -> bool:
    """http to `127.0.0.1`, `[::1]` or `localhost`."""
    try:
        parts = _split(uri)
    except RedirectRejected:
        return False
    return parts.scheme == "http" and (parts.hostname or "") in LOOPBACK_HOSTS


def matches(registered: Sequence[str], requested: str) -> bool:
    """`requested` is one of `registered`, or the same loopback redirect on
    another port."""
    if requested in registered:
        return True
    if not is_loopback(requested) or "#" in requested:
        return False
    want = _split(requested)
    if want.username is not None or want.password is not None:
        return False
    for uri in registered:
        if not is_loopback(uri):
            continue
        have = _split(uri)
        if (have.hostname, have.path, have.query) == (want.hostname, want.path, want.query):
            return True
    return False


def host_of(uri: str) -> str:
    """The host name, as the consent screen shows it ("" if there is none)."""
    try:
        return _split(uri).hostname or ""
    except RedirectRejected:
        return ""


def with_params(uri: str, params: Mapping[str, str]) -> str:
    """`uri` with `params` added to its query, its own query kept."""
    parts = urlsplit(uri)
    query = [*parse_qsl(parts.query, keep_blank_values=True), *params.items()]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
```

`backend/app/core/auth/oauth/pkce.py`:

```python
"""PKCE, S256 and nothing else (RFC 7636; design §5.1, [S17], [S30]).

- verifier: 43 to 128 characters of `[A-Z] / [a-z] / [0-9] / "-" / "." / "_"
  / "~"` (RFC 7636 §4.1);
- challenge: `BASE64URL-ENCODE(SHA256(ASCII(code_verifier)))` (§4.2), which is
  always 43 characters;
- never "plain": "Clients MUST NOT downgrade to "plain"" [S17], and MCP's
  clients "MUST use the S256 code challenge method" [S30]. §4.3 makes a
  missing method mean "plain", so the authorization request must SAY S256
  (Task 8).

Compared in constant time.
"""

import base64
import hashlib
import hmac
import re

_VERIFIER = re.compile(r"[A-Za-z0-9\-._~]{43,128}")
_CHALLENGE = re.compile(r"[A-Za-z0-9\-_]{43}")


def valid_verifier(value: str) -> bool:
    return isinstance(value, str) and _VERIFIER.fullmatch(value) is not None


def valid_challenge(value: str) -> bool:
    return isinstance(value, str) and _CHALLENGE.fullmatch(value) is not None


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify(verifier: str, challenge: str) -> bool:
    """RFC 7636 §4.6, S256."""
    if not valid_verifier(verifier) or not valid_challenge(challenge):
        return False
    return hmac.compare_digest(s256(verifier), challenge)
```

- [ ] **Step 4: El manejador en `create_app`**

En `backend/app/main.py`, justo después de las tres líneas que registran `AuthRefusal`:

```python
    from app.core.auth.refusals import AuthRefusal, auth_refusal_handler

    app.add_exception_handler(AuthRefusal, auth_refusal_handler)
```

añadir:

```python

    # The OAuth server's RFC 6749 error bodies (plan 2026-11-06, Task 1): the
    # token, registration and revocation routes raise `OAuthError`.
    from app.core.auth.oauth.errors import OAuthError, oauth_error_handler

    app.add_exception_handler(OAuthError, oauth_error_handler)
```

- [ ] **Step 5: Las pruebas pasan**

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_primitives.py tests/test_i18n_catalog.py`
Expected: PASS (todas).

Run: `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `Success: no issues found` y `All checks passed!`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/core/auth/oauth/__init__.py backend/app/core/auth/oauth/policy.py \
  backend/app/core/auth/oauth/errors.py backend/app/core/auth/oauth/redirects.py \
  backend/app/core/auth/oauth/pkce.py backend/app/main.py backend/tests/test_oauth_primitives.py
git commit -m "feat(oauth): the authorization server's pure pieces: policy, errors, redirects, PKCE"
```

### Task 2: El token de acceso de MCP (`access.py`)

Diseño §5.3, §6.3; D5, D6, D14. Oleada 1, en paralelo con las Tasks 1, 3, 4 y 11. Solo backend, sin base de datos.

El token que `/oauth/token` entrega (Task 7) y que la puerta de MCP verifica (Task 9): un JWT ES256 firmado con la
clave de OpenBao de la fase 1 por `keys.sign_jwt`, `typ: at+jwt`, diez minutos, audiencia exactamente el recurso MCP.
La verificación lo fija todo (RFC 8725 [S38]) y no admite el token interno del asistente, que firma la misma clave con
otro `typ`, otra audiencia y otro emisor (RFC 8725 §3.12: «the validation rules for those JWTs MUST be written such
that they are mutually exclusive»).

**Files:**
- Create: `backend/app/core/auth/oauth/__init__.py` (el mismo contenido que la Task 1, Step 1)
- Create: `backend/app/core/auth/oauth/access.py`
- Test: `backend/tests/test_oauth_access_token.py`

**Interfaces:**
- Consumes: `backend/app/core/auth/keys.py` (fase 1): `async def sign_jwt(claims: Mapping[str, Any], *, typ: str =
  "JWT") -> str` (pone `alg`, `kid` y `typ` en la cabecera firmada), `async def jwks(*, refresh: bool = False) ->
  dict[str, Any]`, `class AuthKeysUnavailable(RuntimeError)`, `class LocalAuthKeys` (`generate()`,
  `__init__(*, pepper, signing)`), `set_backend(backend)`; `app.core.mcp_channel.mcp_resource(settings)`;
  `app.core.auth.oauth.policy` (Task 1): `issuer`, `ACCESS_TOKEN_TTL_S`, `ACCESS_TYP`, `SCOPE`. **Si la Task 1 aún no
  está en la rama**, esta tarea crea `policy.py` con el contenido exacto de la Task 1, Step 3 (el coordinador toma una
  copia al fusionar; las dos son idénticas). PyJWT 2.15.1: `jwt.get_unverified_header`, `jwt.decode(token, key=,
  algorithms=, audience=, issuer=, options={"require": [...]})`, `jwt.PyJWKSet.from_dict(doc)[kid]` (lo usa
  `loopback.py:268-279`). Comprobado contra el código del árbol el 2026-11-06: PyJWT acepta en `aud` **una lista** que
  contenga la audiencia, así que la exactitud se comprueba aparte (Step 3).
- Produces: `McpGrant`, `mint_access_token`, `verify_access_token`, `unverified_client_id`, `REFRESH_EVERY_S` (Apéndice
  A.1). Las Tasks 7 y 9 los consumen.

**Decisión de la tarea (1): `McpGrant.azp`.** El resto del código de MCP ya llama `azp` al cliente de IA (el marcador
del canal, `McpCall.azp`; la fila de auditoría, `"app": principal.claims.azp`). `McpGrant` guarda `client_id` (RFC 9068
§2.2) y lo expone también como `azp`, para que la Task 9 cambie el tipo de `AuthContext.claims` sin tocar cada lector.

**Decisión de la tarea (2): el correo no va en el token.** La puerta lo lee de la fila `users` (Task 9) y lo pone en
`McpGrant.email` con `dataclasses.replace`. Un token que no lleva el correo no puede quedarse desfasado de él, y no
expone el correo a quien lo lea.

- [ ] **Step 1: La prueba que falla**

`backend/tests/test_oauth_access_token.py`:

```python
"""The MCP access token (autenticación propia design §5.3, §6.3; plan
2026-11-06, Task 2).

What is pinned here:

- it is ES256, `typ: at+jwt`, ten minutes, audience EXACTLY the MCP resource,
  issuer the console address, and it names the person, the organization, the
  AI app and the grant (RFC 9068 §2.2 plus `org` and `fam`);
- verification fixes all of it (RFC 8725 [S38]): another `typ`, audience,
  issuer, algorithm or key, a longer life, a missing claim -- each is None;
- the assistant's internal token is never an MCP token, nor one it (RFC 8725
  §3.12);
- an unknown `kid` reads the public keys again at most once per
  `REFRESH_EVERY_S` (plan D14).
"""

import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest

from app.core.auth import keys, loopback
from app.core.auth.oauth import access

BASE = "https://itms.example.com"
RESOURCE = f"{BASE}/mcp"
SETTINGS = SimpleNamespace(public_console_url=BASE)


@pytest.fixture(autouse=True)
def local_keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[keys.LocalAuthKeys]:
    backend = keys.LocalAuthKeys.generate()
    keys.set_backend(backend)
    # Every test starts with the refresh throttle open (plan D14).
    monkeypatch.setattr(access, "_next_refresh_at", float("-inf"))
    yield backend
    keys.set_backend(None)


async def _mint(**overrides: Any) -> tuple[str, dict[str, Any]]:
    ids = {"user_id": uuid4(), "org_id": uuid4(), "family_id": uuid4()}
    token, expires_in = await access.mint_access_token(
        settings=SETTINGS, client_id="itms_app", **ids, **overrides
    )
    assert expires_in == 600
    return token, ids


async def _signed(claims: dict[str, Any], typ: str = "at+jwt") -> str:
    return await keys.sign_jwt(claims, typ=typ)


def _claims(token: str) -> dict[str, Any]:
    return jwt.decode(token, options={"verify_signature": False})


async def test_the_token_is_es256_at_jwt_for_the_mcp_resource() -> None:
    token, ids = await _mint()
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256"
    assert header["typ"] == "at+jwt"
    assert header["kid"] == (await keys.jwks())["keys"][0]["kid"]
    claims = _claims(token)
    assert claims["iss"] == BASE
    assert claims["aud"] == RESOURCE
    assert claims["sub"] == str(ids["user_id"])
    assert claims["org"] == str(ids["org_id"])
    assert claims["fam"] == str(ids["family_id"])
    assert claims["client_id"] == "itms_app"
    assert claims["scope"] == "mcp"
    assert claims["exp"] - claims["iat"] == 600
    assert len(claims["jti"]) >= 16
    assert "email" not in claims


async def test_a_token_verifies_back_to_its_grant() -> None:
    token, ids = await _mint()
    grant = await access.verify_access_token(token, settings=SETTINGS)
    assert grant is not None
    assert grant.user_id == ids["user_id"]
    assert grant.org_id == ids["org_id"]
    assert grant.family_id == ids["family_id"]
    assert grant.client_id == grant.azp == "itms_app"
    assert grant.exp - grant.iat == 600
    assert grant.email == ""


async def test_the_issuer_must_be_an_https_console() -> None:
    no_console = SimpleNamespace(public_console_url="http://itms.example.com")
    with pytest.raises(ValueError):
        await access.mint_access_token(
            settings=no_console, user_id=uuid4(), org_id=uuid4(), client_id="a", family_id=uuid4()
        )
    token, _ = await _mint()
    assert await access.verify_access_token(token, settings=no_console) is None


def _base_claims() -> dict[str, Any]:
    now = int(time.time())
    return {
        "iss": BASE,
        "aud": RESOURCE,
        "sub": str(uuid4()),
        "client_id": "itms_app",
        "org": str(uuid4()),
        "fam": str(uuid4()),
        "scope": "mcp",
        "jti": "j" * 22,
        "iat": now,
        "exp": now + 600,
    }


#: Each case changes the good claims; the times are relative to the good
#: token's own `iat`, computed when the test runs, never at collection.
CHANGES: dict[str, Any] = {
    "two-audiences": lambda c: {"aud": [RESOURCE, BASE]},  # the resource AND another
    "console-audience": lambda c: {"aud": BASE},
    "loopback-audience": lambda c: {"aud": "loopback"},
    "keycloak-issuer": lambda c: {"iss": "https://idp.test/realms/it-suite"},
    "loopback-issuer": lambda c: {"iss": "urn:itms:loopback"},
    "wider-scope": lambda c: {"scope": "mcp offline_access"},
    "bad-sub": lambda c: {"sub": "not-a-uuid"},
    "bad-org": lambda c: {"org": 7},
    "no-client": lambda c: {"client_id": ""},
    "too-long": lambda c: {"exp": c["iat"] + 601},
    "expired": lambda c: {"iat": c["iat"] - 700, "exp": c["iat"] - 100},
}


@pytest.mark.parametrize("case", sorted(CHANGES))
async def test_any_other_claim_is_refused(case: str) -> None:
    good = _base_claims()
    assert await access.verify_access_token(await _signed(good), settings=SETTINGS) is not None
    bad = {**good, **CHANGES[case](good)}
    assert await access.verify_access_token(await _signed(bad), settings=SETTINGS) is None


@pytest.mark.parametrize("missing", ["iss", "aud", "sub", "client_id", "org", "fam", "scope", "jti", "iat", "exp"])
async def test_every_claim_is_required(missing: str) -> None:
    claims = _base_claims()
    del claims[missing]
    assert await access.verify_access_token(await _signed(claims), settings=SETTINGS) is None


@pytest.mark.parametrize("typ", ["JWT", "loopback+jwt", "application/jwt", "at+JWT"])
async def test_another_typ_is_refused(typ: str) -> None:
    """RFC 9068 §4: "reject tokens carrying any other value"."""
    assert await access.verify_access_token(await _signed(_base_claims(), typ=typ), settings=SETTINGS) is None


async def test_another_algorithm_is_refused() -> None:
    kid = (await keys.jwks())["keys"][0]["kid"]
    unsigned = jwt.encode(
        _base_claims(), key=None, algorithm="none", headers={"typ": "at+jwt", "kid": kid}
    )
    hmac = jwt.encode(
        _base_claims(), key="k" * 32, algorithm="HS256", headers={"typ": "at+jwt", "kid": kid}
    )
    assert await access.verify_access_token(unsigned, settings=SETTINGS) is None
    assert await access.verify_access_token(hmac, settings=SETTINGS) is None
    assert await access.verify_access_token("not.a.jwt", settings=SETTINGS) is None


async def test_a_signature_by_another_key_is_refused(local_keys: keys.LocalAuthKeys) -> None:
    keys.set_backend(keys.LocalAuthKeys.generate())
    stranger = await _signed(_base_claims())
    keys.set_backend(local_keys)
    assert await access.verify_access_token(stranger, settings=SETTINGS) is None


async def test_the_assistants_internal_token_is_never_an_mcp_token() -> None:
    principal = SimpleNamespace(
        factor_done=True, user_id=uuid4(), org_id=uuid4(), session_id=uuid4(), kind="staff"
    )
    internal = await loopback.mint_loopback(principal)  # type: ignore[arg-type]
    assert await access.verify_access_token(internal, settings=SETTINGS) is None
    # ...nor an MCP token the internal one (its verifier fixes typ, aud and iss).
    token, _ = await _mint()
    assert await loopback._verified_claims(token) is None


async def test_an_unknown_kid_reads_the_keys_again_at_most_once_per_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refreshes = 0
    real = keys.jwks

    async def counting(*, refresh: bool = False) -> dict[str, Any]:
        nonlocal refreshes
        refreshes += refresh
        return await real(refresh=refresh)

    clock = [1000.0]
    monkeypatch.setattr(keys, "jwks", counting)
    monkeypatch.setattr(access, "_monotonic", lambda: clock[0])
    forged = jwt.encode(
        _base_claims(), key="k" * 32, algorithm="HS256", headers={"typ": "at+jwt", "kid": "nope"}
    )
    # The header check refuses HS256 before any key is read: use a real ES256
    # signature under an unknown kid instead.
    backend = keys.LocalAuthKeys.generate()
    keys.set_backend(backend)
    unknown = await _signed(_base_claims())
    keys.set_backend(keys.LocalAuthKeys.generate())
    for _ in range(50):
        assert await access.verify_access_token(unknown, settings=SETTINGS) is None
    assert refreshes == 1
    clock[0] += access.REFRESH_EVERY_S
    assert await access.verify_access_token(unknown, settings=SETTINGS) is None
    assert refreshes == 2
    assert await access.verify_access_token(forged, settings=SETTINGS) is None
    assert refreshes == 2


async def test_a_rotated_key_is_read_again_before_the_token_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another api process signed with the new version; this one's copy of the
    public keys is older. One forced read finds it."""
    token, _ = await _mint()
    real = keys.jwks

    async def stale(*, refresh: bool = False) -> dict[str, Any]:
        return await real() if refresh else {"keys": []}

    monkeypatch.setattr(keys, "jwks", stale)
    grant = await access.verify_access_token(token, settings=SETTINGS)
    assert grant is not None
    assert isinstance(grant.family_id, UUID)


def test_the_unverified_client_id_is_a_label_only() -> None:
    token = jwt.encode({"client_id": "itms_x"}, key="k" * 32, algorithm="HS256")
    assert access.unverified_client_id(token) == "itms_x"
    assert access.unverified_client_id(jwt.encode({"client_id": 5}, key="k" * 32)) == ""
    assert access.unverified_client_id("garbage") == ""
```

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_access_token.py`
Expected: FAIL, `ImportError: cannot import name 'access' from 'app.core.auth.oauth'`.

- [ ] **Step 2: `access.py`**

`backend/app/core/auth/oauth/access.py`:

```python
"""The MCP access token (autenticación propia design §5.3, §6.3; plan D5, D6, D14).

A JWT, ES256, signed by the platform's key in OpenBao through
`app.core.auth.keys.sign_jwt` (the assistant's internal token's signer), ten
minutes:

    header  {"alg": "ES256", "kid": <RFC 7638 thumbprint>, "typ": "at+jwt"}
    payload {"iss": <issuer>, "aud": <the MCP resource>, "sub": <users.id>,
             "client_id": <the AI app>, "org": <organizations.id>,
             "fam": <oauth_refresh_families.id>, "scope": "mcp",
             "jti": <random>, "iat": <now>, "exp": <now + 600>}

RFC 9068 §2.2's claims, plus `org` (the door pins the tenant from it, as it
does from the internal token's `org`) and `fam` (the grant the token came
from: revoking it stops the token at the next call, plan D6). No address and
no name: the door reads them from the person's row (Task 9).

VERIFIED WITH EVERYTHING FIXED (RFC 8725 [S38]): `alg` ES256 and nothing else
(§3.1); `typ` exactly `at+jwt` (§3.11; RFC 9068 §4: "reject tokens carrying
any other value"), so the internal token (`loopback+jwt`) never passes for
one, nor one for it (§3.12); `kid` naming a published key; `iss` this
platform (§3.8); `aud` EXACTLY the MCP resource, a string and not a list
holding it with others (§3.9; 1c(a): "carrying the MCP audience and no other
platform audience"; MCP: servers "MUST validate that access tokens were
issued specifically for them" [S27]); no longer than ten minutes; every claim
present and of its type. Any failure is None, and the door answers one 401.

AN UNKNOWN `kid` IS NOT A FREE READ OF OPENBAO (plan D14). `/mcp` is public:
a token with an invented `kid` would force `keys.jwks(refresh=True)` per
request. The keys are read again at most once per `REFRESH_EVERY_S` per
process -- `JwksCache`'s defence for Keycloak's keys (`app/core/security.py`)
-- which is still at once for a token signed just after a rotation.
"""

import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import jwt

from app.core.auth import keys
from app.core.auth.oauth.policy import ACCESS_TOKEN_TTL_S, ACCESS_TYP, SCOPE, issuer
from app.core.mcp_channel import mcp_resource

_REQUIRED = ["iss", "aud", "sub", "client_id", "org", "fam", "scope", "jti", "iat", "exp"]
#: Seconds between two forced re-reads of the public keys (plan D14).
REFRESH_EVERY_S = 5.0
_next_refresh_at = float("-inf")


@dataclass(frozen=True)
class McpGrant:
    """An MCP access token, as the door verified it."""

    user_id: UUID
    org_id: UUID
    client_id: str
    family_id: UUID
    iat: int
    exp: int
    #: The person's address, from their `users` row once the door has read it
    #: (Task 9). Never from the token, which carries none.
    email: str = field(default="", compare=False)

    @property
    def azp(self) -> str:
        """The AI app, by the name the rest of the MCP code uses for it (the
        channel marker's `azp`, the audit rows' `app`)."""
        return self.client_id


def _now() -> int:
    """A named seam, so a test can mint a token in the past."""
    return int(time.time())


def _monotonic() -> float:
    """A named seam, so a test can move the refresh throttle's clock."""
    return time.monotonic()


async def mint_access_token(
    *,
    settings: object,
    user_id: UUID,
    org_id: UUID,
    client_id: str,
    family_id: UUID,
    now: int | None = None,
) -> tuple[str, int]:
    """The token and its life in seconds (`expires_in`, RFC 6749 §5.1)."""
    iss, aud = issuer(settings), mcp_resource(settings)
    if iss is None or aud is None:
        raise ValueError(  # i18n-exempt: a wiring defect; the routes answer 503 first
            "no issuer: ITS_PUBLIC_CONSOLE_URL is not an https address without a path"
        )
    issued = _now() if now is None else now
    claims: dict[str, Any] = {
        "iss": iss,
        "aud": aud,
        "sub": str(user_id),
        "client_id": client_id,
        "org": str(org_id),
        "fam": str(family_id),
        "scope": SCOPE,
        "jti": secrets.token_urlsafe(16),
        "iat": issued,
        "exp": issued + ACCESS_TOKEN_TTL_S,
    }
    return await keys.sign_jwt(claims, typ=ACCESS_TYP), ACCESS_TOKEN_TTL_S


def unverified_client_id(token: str) -> str:
    """The AI app a token claims to be for, WITHOUT verifying it: only the
    label `/mcp` puts on its scope marker before the door verifies the same
    token (`app/mcp/server.py`), and compared with the verified one after."""
    try:
        value = jwt.decode(token, options={"verify_signature": False}).get("client_id")
    except Exception:  # noqa: BLE001 - a label, never a decision; validation follows
        return ""
    return value if isinstance(value, str) else ""


def _find(document: dict[str, Any], kid: str) -> jwt.PyJWK | None:
    try:
        return jwt.PyJWKSet.from_dict(document)[kid]
    except (KeyError, jwt.PyJWKSetError):
        return None


async def _key_for(kid: str) -> jwt.PyJWK | None:
    global _next_refresh_at
    key = _find(await keys.jwks(), kid)
    if key is not None:
        return key
    now = _monotonic()
    if now < _next_refresh_at:
        return None
    _next_refresh_at = now + REFRESH_EVERY_S
    return _find(await keys.jwks(refresh=True), kid)


async def verify_access_token(token: str, *, settings: object) -> McpGrant | None:
    """The grant a genuine MCP access token names, or None. Raises only
    `keys.AuthKeysUnavailable` (OpenBao down: the app's 503)."""
    iss, aud = issuer(settings), mcp_resource(settings)
    if iss is None or aud is None:
        return None
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError:
        return None
    kid = header.get("kid")
    if header.get("alg") != "ES256" or header.get("typ") != ACCESS_TYP:
        return None
    if not isinstance(kid, str) or not kid:
        return None
    key = await _key_for(kid)
    if key is None:
        return None
    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=["ES256"],
            audience=aud,
            issuer=iss,
            options={"require": _REQUIRED},
        )
    except jwt.InvalidTokenError:
        return None
    try:
        iat, exp = int(claims["iat"]), int(claims["exp"])
        client_id = claims["client_id"]
        if claims["aud"] != aud or claims["scope"] != SCOPE or exp - iat > ACCESS_TOKEN_TTL_S:
            return None
        if not isinstance(client_id, str) or not client_id:
            return None
        if not isinstance(claims["org"], str) or not isinstance(claims["fam"], str):
            return None
        return McpGrant(
            user_id=UUID(str(claims["sub"])),
            org_id=UUID(claims["org"]),
            client_id=client_id,
            family_id=UUID(claims["fam"]),
            iat=iat,
            exp=exp,
        )
    except (KeyError, TypeError, ValueError):
        return None
```

- [ ] **Step 3: Las pruebas pasan**

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_access_token.py tests/test_loopback.py tests/test_i18n_catalog.py`
Expected: PASS. (`test_loopback.py` comprueba que el token interno sigue igual: esta tarea no toca `loopback.py`.)

Run: `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `Success: no issues found` y `All checks passed!`.

- [ ] **Step 4: Commit**

```bash
git add backend/app/core/auth/oauth/__init__.py backend/app/core/auth/oauth/access.py \
  backend/tests/test_oauth_access_token.py
git commit -m "feat(oauth): the MCP access token: ES256 at+jwt for the MCP resource, verified with everything fixed"
```

### Task 3: `egress.get_bounded` y el documento CIMD (`cimd.py`)

Diseño §5.5. Oleada 1, en paralelo con las Tasks 1, 2, 4 y 11. Solo backend, sin base de datos.

Un cliente CIMD se nombra con una URL https en vez de registrarse, y el servidor descarga el documento de esa URL
[S29]. Descargar una URL que elige un desconocido es un SSRF por construcción; el diseño exige hacerlo «por la
protección de salida existente (`app/core/egress.py`)». Hoy `egress.py` solo tiene `post_json` (POST, devuelve un
extracto de 500 caracteres imprimibles): esta tarea le añade `get_bounded` (GET, devuelve los bytes hasta un tope) con
las mismas cinco propiedades, y escribe `cimd.py`, que valida el documento según CIMD-00 y MCP.

**Files:**
- Create: `backend/app/core/auth/oauth/__init__.py` (el mismo contenido que la Task 1, Step 1)
- Modify: `backend/app/core/egress.py` (al final: `EgressBody` y `get_bounded`; nada de lo que hay cambia)
- Create: `backend/app/core/auth/oauth/cimd.py`
- Modify: `backend/tests/test_logging_discipline.py` (recuento y párrafo «NOW») y
  `docs/superpowers/specs/2026-08-14-logging-kernel-design.md` (§2.2, el párrafo «NOW»)
- Test: `backend/tests/test_egress_get_bounded.py`, `backend/tests/test_oauth_cimd.py`

**Interfaces:**
- Consumes: de `backend/app/core/egress.py`, `EgressBlocked(reason, message)`, `check_host(host, resolved, *,
  allowlisted) -> str`, `resolve(host, port) -> list[str]` (y su costura `_resolve_all`), `_platform_layer1_denies(host,
  port, allowlisted_hosts)`, `_platform_layer2_denies(addr, port)`, `_build_transport(pinned_ip)` (la costura que
  parchean las pruebas), `_CONNECT_TIMEOUT_S`, `_platform_addr_cache`. De la Task 1, `redirects.check_registered`,
  `RedirectRejected`, `policy.MAX_REDIRECT_URIS`, `policy.MAX_CLIENT_ID`, `policy.display_name`: **si la Task 1 aún no
  está en la rama**, esta tarea crea `policy.py` y `redirects.py` con el contenido exacto de la Task 1, Step 3 (copias
  idénticas; el coordinador toma una).
- Produces: `EgressBody`, `get_bounded`; `cimd.MAX_DOCUMENT_BYTES`, `FETCH_TIMEOUT_S`, `MIN_CACHE`, `MAX_CACHE`,
  `DocumentRefused`, `ClientDocument`, `is_document_client_id`, `cache_lifetime`, `fetch_document` (Apéndice A.1).

**Decisión de la tarea (1): `get_bounded` no lanza razones nuevas.** `tests/test_notification_delivery.py` lee del
código cada razón de `EgressBlocked` y exige que sea terminal para los avisos. `get_bounded` solo usa las que ya
existen (`scheme_not_allowed`, `invalid_url`, `port_not_allowed`, `blocked_platform_host`, `dns_failure`,
`blocked_private`, `redirect_refused`); el tamaño y el tipo los juzga quien lee (`truncated`, `content_type`), aquí
`cimd.py`.

**Decisión de la tarea (2): https, puerto 443 y ninguna lista blanca.** Un documento CIMD lo nombra un desconocido;
nada que configure un administrador puede abrirle una dirección privada (`allowlisted=False` siempre). CIMD-00 §3
permite un puerto («MAY contain a port»); este servidor solo descarga en el 443 por defecto, como `post_json` para un
host que no está en la lista: un cliente con otro puerto no puede usar CIMD aquí y puede registrarse.

**Decisión de la tarea (3): una línea de log por documento rechazado.** Sin ella, una conexión que falla porque el
documento de un cliente no vale sería invisible para el operador. Plantilla fija, y en `extra` la razón (una de
`Reason`) y el host, nunca la URL completa ni el cuerpo. **+1** al recuento del barrido (108 → 109 sobre la base que
ve esta tarea).

- [ ] **Step 1: El paquete**

`backend/app/core/auth/oauth/__init__.py`: el contenido de la Task 1, Step 1.

- [ ] **Step 2: Las pruebas que fallan**

`backend/tests/test_egress_get_bounded.py`:

```python
"""`egress.get_bounded`: a GET under the egress guard's five properties
(autenticación propia plan 2026-11-06, Task 3; design §5.5).

Same seams as `tests/test_egress_guard.py`: `_resolve_all` answers DNS and
`_build_transport` the connection, so nothing leaves the machine.
"""

from pathlib import Path
from typing import Any

import httpx
import pytest

import app.core.egress as egress_module
from app.core.config import Settings, override_settings
from app.core.egress import EgressBlocked, get_bounded

PUBLIC = "93.184.216.34"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        database_url="postgresql+asyncpg://u:p@db.internal:5432/x",
        database_admin_url="postgresql+asyncpg://u:p@db.internal:5432/x",
        oidc_issuer="https://issuer.example.com",
        oidc_jwks_url="https://issuer.example.com/jwks",
        ca_dir=Path("/tmp/its-egress-test-ca"),
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _baseline_settings():
    egress_module._platform_addr_cache.clear()
    with override_settings(_settings()):
        yield


def _serve(monkeypatch: pytest.MonkeyPatch, handler: Any, *, resolved: list[str] | None = None):
    seen: list[httpx.Request] = []

    def transport(_ip: str) -> httpx.AsyncBaseTransport:
        def wrapped(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        return httpx.MockTransport(wrapped)

    monkeypatch.setattr(egress_module, "_resolve_all", lambda host, port: resolved or [PUBLIC])
    monkeypatch.setattr(egress_module, "_build_transport", transport)
    return seen


async def test_a_document_is_read_through_the_pinned_address(monkeypatch) -> None:
    seen = _serve(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            content=b'{"client_id": "x"}',
            headers={"Content-Type": "application/json", "Cache-Control": "max-age=600"},
        ),
    )
    body = await get_bounded(
        "https://client.example/oauth/metadata.json", max_bytes=5120, headers={"Accept": "application/json"}
    )
    assert body.status_code == 200
    assert body.content == b'{"client_id": "x"}'
    assert body.truncated is False
    assert body.content_type == "application/json"
    assert body.cache_control == "max-age=600"
    (request,) = seen
    assert request.method == "GET"
    assert request.url.host == PUBLIC  # pinned: the validated answer is the one dialled
    assert request.url.path == "/oauth/metadata.json"
    assert request.headers["host"] == "client.example"
    assert request.headers["accept"] == "application/json"


async def test_a_body_over_the_cap_is_cut_and_says_so(monkeypatch) -> None:
    _serve(monkeypatch, lambda r: httpx.Response(200, content=b"x" * 9000))
    body = await get_bounded("https://client.example/m.json", max_bytes=5120)
    assert body.truncated is True
    assert len(body.content) == 5120


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://client.example/m.json", "scheme_not_allowed"),
        ("ftp://client.example/m.json", "scheme_not_allowed"),
        ("https://client.example:8443/m.json", "port_not_allowed"),
        ("https://client.example:99999/m.json", "invalid_url"),
        ("https:///m.json", "invalid_url"),
        ("https://db.internal/m.json", "blocked_platform_host"),
    ],
)
async def test_what_the_guard_refuses_before_any_connection(monkeypatch, url: str, reason: str) -> None:
    seen = _serve(monkeypatch, lambda r: httpx.Response(200))
    with pytest.raises(EgressBlocked) as refused:
        await get_bounded(url, max_bytes=10)
    assert refused.value.reason == reason
    assert seen == []


@pytest.mark.parametrize("address", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "::1"])
async def test_a_private_answer_is_never_dialled(monkeypatch, address: str) -> None:
    seen = _serve(monkeypatch, lambda r: httpx.Response(200), resolved=[PUBLIC, address])
    with pytest.raises(EgressBlocked) as refused:
        await get_bounded("https://client.example/m.json", max_bytes=10)
    assert refused.value.reason == "blocked_private"
    assert seen == []


async def test_a_redirect_is_refused_and_never_followed(monkeypatch) -> None:
    seen = _serve(
        monkeypatch, lambda r: httpx.Response(302, headers={"Location": "https://169.254.169.254/"})
    )
    with pytest.raises(EgressBlocked) as refused:
        await get_bounded("https://client.example/m.json", max_bytes=10)
    assert refused.value.reason == "redirect_refused"
    assert len(seen) == 1


async def test_the_whole_exchange_has_one_deadline(monkeypatch) -> None:
    def slow(_host: str, _port: int) -> list[str]:
        import time

        time.sleep(0.5)
        return [PUBLIC]

    monkeypatch.setattr(egress_module, "_resolve_all", slow)
    with pytest.raises(TimeoutError):
        await get_bounded("https://client.example/m.json", max_bytes=10, timeout_s=0.1)
```

`backend/tests/test_oauth_cimd.py`:

```python
"""Client ID Metadata Documents (autenticación propia design §5.5; plan
2026-11-06, Task 3; draft-ietf-oauth-client-id-metadata-document-00, MCP
[S29]).

`egress.get_bounded` is replaced by a fake that answers what each case needs:
the guard itself is `tests/test_egress_get_bounded.py`'s.
"""

import json
import logging
from datetime import timedelta
from typing import Any

import pytest

from app.core import egress
from app.core.auth.oauth import cimd
from app.core.egress import EgressBlocked, EgressBody

URL = "https://claude.example/oauth/client-metadata.json"
GOOD: dict[str, Any] = {
    "client_id": URL,
    "client_name": "Claude Code",
    "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"],
    "token_endpoint_auth_method": "none",
    "grant_types": ["authorization_code", "refresh_token"],
}


def _answer(
    monkeypatch: pytest.MonkeyPatch,
    document: object = GOOD,
    *,
    status: int = 200,
    truncated: bool = False,
    cache_control: str = "max-age=3600",
    raw: bytes | None = None,
) -> list[str]:
    asked: list[str] = []

    async def fake(url: str, **kwargs: Any) -> EgressBody:
        asked.append(url)
        assert kwargs["max_bytes"] == cimd.MAX_DOCUMENT_BYTES
        content = raw if raw is not None else json.dumps(document).encode()
        return EgressBody(
            status_code=status,
            content=content,
            truncated=truncated,
            content_type="application/json",
            cache_control=cache_control,
        )

    monkeypatch.setattr(egress, "get_bounded", fake)
    return asked


@pytest.mark.parametrize(
    ("client_id", "ok"),
    [
        (URL, True),
        ("https://claude.example/c", True),
        ("http://claude.example/c.json", False),
        ("https://claude.example", False),
        ("https://claude.example/", False),
        ("https://claude.example/a/../c.json", False),
        ("https://claude.example/a/%2e%2e/c.json", False),
        ("https://claude.example/./c.json", False),
        ("https://claude.example/c.json#x", False),
        ("https://claude.example/c.json?x=1", False),
        ("https://user@claude.example/c.json", False),
        ("https://claude.example/c json", False),
        ("itms_0123456789abcdef0123456789abcd", False),
        ("https://claude.example/" + "a" * 300, False),
    ],
)
def test_which_client_ids_name_a_document(client_id: str, ok: bool) -> None:
    """CIMD-00 §3: https, a path, no dot segment, no fragment, no user name;
    a query is refused here (§3: "SHOULD NOT")."""
    assert cimd.is_document_client_id(client_id) is ok


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("max-age=3600", 3600),
        ("public, max-age=60", 300),  # the 5-minute floor
        ("max-age=604800", 86400),  # the 24-hour ceiling
        ('max-age="900"', 900),
        ("max-age=0", 0),
        ("no-store", 0),
        ("no-cache, max-age=3600", 0),
        ("", 0),
        ("max-age=abc", 0),
    ],
)
def test_the_cache_follows_the_documents_headers_within_bounds(header: str, seconds: int) -> None:
    assert cimd.cache_lifetime(header) == timedelta(seconds=seconds)


async def test_a_valid_document_is_its_client(monkeypatch) -> None:
    asked = _answer(monkeypatch)
    document = await cimd.fetch_document(URL)
    assert asked == [URL]
    assert document.client_id == URL
    assert document.client_name == "Claude Code"
    assert document.redirect_uris == ("http://localhost/callback", "http://127.0.0.1/callback")
    assert document.cache_for == timedelta(hours=1)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"client_id": URL + "x"}, "mismatch"),
        ({"client_id": None}, "mismatch"),
        ({"client_name": ""}, "incomplete"),
        ({"client_name": None}, "incomplete"),
        ({"redirect_uris": []}, "incomplete"),
        ({"redirect_uris": "http://localhost/cb"}, "incomplete"),
        ({"redirect_uris": [f"https://c.example/{n}" for n in range(11)]}, "incomplete"),
        ({"redirect_uris": ["http://evil.example/cb"]}, "bad_redirect"),
        ({"token_endpoint_auth_method": "client_secret_basic"}, "secret"),
        ({"token_endpoint_auth_method": "private_key_jwt"}, "secret"),
        ({"client_secret": "s3cret"}, "secret"),
        ({"client_secret_expires_at": 0}, "secret"),
    ],
)
async def test_a_document_this_server_cannot_use_is_refused(
    monkeypatch, changes: dict[str, Any], reason: str
) -> None:
    _answer(monkeypatch, {**GOOD, **changes})
    with pytest.raises(cimd.DocumentRefused) as refused:
        await cimd.fetch_document(URL)
    assert refused.value.reason == reason


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"status": 404}, "not_ok"),
        ({"status": 500}, "not_ok"),
        ({"truncated": True}, "too_large"),
        ({"raw": b"<html>"}, "not_json"),
        ({"raw": b"[1, 2]"}, "not_json"),
        ({"raw": b"\xff\xfe"}, "not_json"),
    ],
)
async def test_an_answer_that_is_not_a_document_is_refused(
    monkeypatch, kwargs: dict[str, Any], reason: str
) -> None:
    _answer(monkeypatch, **kwargs)
    with pytest.raises(cimd.DocumentRefused) as refused:
        await cimd.fetch_document(URL)
    assert refused.value.reason == reason


async def test_a_url_the_guard_refuses_is_unreachable(monkeypatch) -> None:
    async def blocked(url: str, **kwargs: Any) -> EgressBody:
        raise EgressBlocked("blocked_private", "no")

    monkeypatch.setattr(egress, "get_bounded", blocked)
    with pytest.raises(cimd.DocumentRefused) as refused:
        await cimd.fetch_document(URL)
    assert refused.value.reason == "unreachable"


async def test_a_client_id_that_names_no_document_is_never_fetched(monkeypatch) -> None:
    asked = _answer(monkeypatch)
    with pytest.raises(cimd.DocumentRefused) as refused:
        await cimd.fetch_document("http://claude.example/c.json")
    assert refused.value.reason == "not_a_document_url"
    assert asked == []


async def test_a_refusal_is_logged_once_with_the_reason_and_the_host_only(
    monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    _answer(monkeypatch, {**GOOD, "client_secret": "s3cret"})
    with caplog.at_level(logging.WARNING, logger="its.auth.oauth.cimd"):
        with pytest.raises(cimd.DocumentRefused):
            await cimd.fetch_document(URL)
    (record,) = [r for r in caplog.records if r.name == "its.auth.oauth.cimd"]
    assert record.getMessage() == "client metadata document refused"
    assert record.__dict__["reason"] == "secret"
    assert record.__dict__["host"] == "claude.example"
    assert "s3cret" not in caplog.text
    assert URL not in caplog.text
```

Run: `cd backend && .venv/bin/pytest -q tests/test_egress_get_bounded.py tests/test_oauth_cimd.py`
Expected: FAIL, `ImportError: cannot import name 'get_bounded' from 'app.core.egress'`.

- [ ] **Step 3: `get_bounded`**

Al final de `backend/app/core/egress.py`, sin tocar nada de lo que hay:

```python


# --------------------------------------------------------------------------- #
# Reading a document (own authentication phase 3, design §5.5)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EgressBody:
    """A GET's answer, read no further than `max_bytes` (`get_bounded`).
    `truncated` says there was more; the caller decides what that means."""

    status_code: int
    content: bytes
    truncated: bool
    content_type: str
    cache_control: str


async def get_bounded(
    url: str,
    *,
    max_bytes: int,
    timeout_s: float = 10.0,
    headers: Mapping[str, str] | None = None,
) -> EgressBody:
    """GET `url` under `post_json`'s five properties, for a document the
    platform must READ: an OAuth client's metadata document, whose address a
    stranger chose (own authentication design §5.5).

    STRICTER THAN `post_json`, ON PURPOSE: https only, the scheme's default
    port only, and NO allowlist -- nothing an administrator configured can
    open a private address to a URL somebody else named. The platform's own
    hosts are refused by name and by address; every DNS answer is validated
    and the connection pinned to it; no redirect; at most `max_bytes` read off
    the wire; one deadline for all of it, DNS included.

    Only reasons `post_json` already raises: `tests/test_notification_
    delivery.py` reads every one from this file and holds it terminal."""
    parts = urlsplit(url)
    host = (parts.hostname or "").rstrip(".")
    if parts.scheme != "https":
        raise EgressBlocked("scheme_not_allowed",  # i18n-exempt: machine code, the console words it
            f"scheme {parts.scheme!r}")
    try:
        explicit_port = parts.port
    except ValueError as exc:
        raise EgressBlocked("invalid_url",  # i18n-exempt: machine code, the console words it
            "malformed port") from exc
    if not host:
        raise EgressBlocked("invalid_url",  # i18n-exempt: machine code, the console words it
            "no host")
    port = explicit_port or _DEFAULT_PORT_FOR_SCHEME["https"]
    if port != _DEFAULT_PORT_FOR_SCHEME["https"]:
        raise EgressBlocked("port_not_allowed",  # i18n-exempt: machine code
            "only the scheme's default port is permitted")
    if _platform_layer1_denies(host, port, set()):
        raise EgressBlocked("blocked_platform_host",  # i18n-exempt: machine code
            "refused: this is one of the platform's own hosts")
    timeout = httpx.Timeout(timeout_s, connect=min(_CONNECT_TIMEOUT_S, timeout_s))

    async def _do_request() -> EgressBody:
        pinned = check_host(host, await resolve(host, port), allowlisted=False)
        if await _platform_layer2_denies(pinned, port):
            raise EgressBlocked("blocked_platform_host",  # i18n-exempt: machine code
                "refused: resolves to one of the platform's own hosts")
        pinned_netloc = f"[{pinned}]" if ":" in pinned else pinned
        pinned_url = parts._replace(netloc=f"{pinned_netloc}:{port}").geturl()
        request_headers = {**(headers or {}), "Host": host}
        async with httpx.AsyncClient(
            transport=_build_transport(pinned), timeout=timeout, follow_redirects=False
        ) as client:
            async with client.stream(
                "GET", pinned_url, headers=request_headers, extensions={"sni_hostname": host}
            ) as response:
                if 300 <= response.status_code < 400:
                    raise EgressBlocked("redirect_refused",  # i18n-exempt: machine code
                        f"destination returned {response.status_code}")
                chunks = bytearray()
                truncated = False
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > max_bytes:
                        truncated = True
                        break
                return EgressBody(
                    status_code=response.status_code,
                    content=bytes(chunks[:max_bytes]),
                    truncated=truncated,
                    content_type=response.headers.get("content-type", ""),
                    cache_control=response.headers.get("cache-control", ""),
                )

    return await asyncio.wait_for(_do_request(), timeout=timeout_s)
```

Y en los imports de `egress.py`: `from collections.abc import Mapping` (junto a `from dataclasses import dataclass`).

- [ ] **Step 4: `cimd.py`**

`backend/app/core/auth/oauth/cimd.py`:

```python
"""OAuth Client ID Metadata Documents (autenticación propia design §5.5; MCP
[S29]; draft-ietf-oauth-client-id-metadata-document-00, the revision MCP
cites, https://www.ietf.org/archive/id/draft-ietf-oauth-client-id-metadata-document-00.txt).

A client that names itself by an https URL instead of registering. The
platform fetches the document at that URL through `egress.get_bounded` (the
SSRF guard, design §5.5; CIMD-00 §6.5) and uses it only when:

- the URL is a client identifier CIMD-00 §3 allows: "MUST have an "https"
  scheme, MUST contain a path component, MUST NOT contain single-dot or
  double-dot path segments, MUST NOT contain a fragment component and MUST
  NOT contain a username or password" -- and no query, which §3 says SHOULD
  NOT be there and this server refuses;
- the answer is a 200 JSON object of at most 5 KiB (§6.6: "The recommended
  maximum response size for client metadata documents is 5 kilobytes");
- its `client_id` is the URL, exactly (§4.1: "MUST match the URL of the
  document using simple string comparison"; MCP: "MUST validate" [S29]);
- it has `client_name` and `redirect_uris` (MCP: "MUST include at least the
  following properties: client_id, client_name, redirect_uris" [S29]), each
  redirect one this server would register (`redirects.check_registered`);
- it names no shared secret: `token_endpoint_auth_method` absent or `none`,
  no `client_secret`, no `client_secret_expires_at` (§4.1). This server has
  public clients only (plan D4), so `private_key_jwt` is refused too.

CACHED BY ITS OWN HEADERS, WITHIN BOUNDS (§4.4: "SHOULD respect HTTP cache
headers ... but MAY define its own upper and/or lower bounds"): `max-age`
clamped to [`MIN_CACHE`, `MAX_CACHE`]; `no-store`, `no-cache`, `max-age=0` or
none at all caches nothing, and the next authorization reads it again. A
refused document is never cached (§4.4: "MUST NOT cache error responses ...
documents which are invalid or malformed"): the caller stores only what
`fetch_document` returned (`clients.resolve_client`, Task 6).

A REFUSAL IS ONE LOG LINE, the reason and the host -- never the URL's path,
never the body -- so an operator can tell why an AI app cannot connect.
"""

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from urllib.parse import unquote, urlsplit

import httpx

from app.core import egress
from app.core.auth.oauth.policy import MAX_CLIENT_ID, MAX_REDIRECT_URIS, display_name
from app.core.auth.oauth.redirects import RedirectRejected, check_registered

logger = logging.getLogger("its.auth.oauth.cimd")

MAX_DOCUMENT_BYTES = 5 * 1024
FETCH_TIMEOUT_S = 5.0
MIN_CACHE = timedelta(minutes=5)
MAX_CACHE = timedelta(hours=24)

Reason = Literal[
    "not_a_document_url",
    "unreachable",
    "not_ok",
    "too_large",
    "not_json",
    "mismatch",
    "incomplete",
    "bad_redirect",
    "secret",
]


class DocumentRefused(Exception):
    """The document cannot be this client's. `reason` is a code, never shown."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason: Reason = reason


@dataclass(frozen=True)
class ClientDocument:
    client_id: str
    client_name: str
    redirect_uris: tuple[str, ...]
    cache_for: timedelta


def is_document_client_id(client_id: str) -> bool:
    """CIMD-00 §3, and no query."""
    if not isinstance(client_id, str) or len(client_id) > MAX_CLIENT_ID:
        return False
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in client_id):
        return False
    if "#" in client_id or "?" in client_id:
        return False
    try:
        parts = urlsplit(client_id)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    if parts.path in ("", "/"):
        return False
    return not any(unquote(segment) in (".", "..") for segment in parts.path.split("/"))


def cache_lifetime(cache_control: str) -> timedelta:
    """How long a valid document may be reused (module docstring)."""
    directives: dict[str, str] = {}
    for part in cache_control.split(","):
        name, _, value = part.strip().partition("=")
        if name:
            directives[name.strip().lower()] = value.strip().strip('"')
    if "no-store" in directives or "no-cache" in directives:
        return timedelta(0)
    raw = directives.get("max-age", "")
    if not raw.isdigit() or int(raw) == 0:
        return timedelta(0)
    return min(max(timedelta(seconds=int(raw)), MIN_CACHE), MAX_CACHE)


async def _read(client_id: str) -> ClientDocument:
    if not is_document_client_id(client_id):
        raise DocumentRefused("not_a_document_url")  # i18n-exempt: a reason code
    try:
        answer = await egress.get_bounded(
            client_id,
            max_bytes=MAX_DOCUMENT_BYTES,
            timeout_s=FETCH_TIMEOUT_S,
            headers={"Accept": "application/json"},
        )
    except (egress.EgressBlocked, httpx.HTTPError, TimeoutError, OSError) as exc:
        raise DocumentRefused("unreachable") from exc  # i18n-exempt: a reason code
    if answer.status_code != 200:
        raise DocumentRefused("not_ok")  # i18n-exempt: a reason code
    if answer.truncated:
        raise DocumentRefused("too_large")  # i18n-exempt: a reason code
    try:
        document = json.loads(answer.content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise DocumentRefused("not_json") from exc  # i18n-exempt: a reason code
    if not isinstance(document, dict):
        raise DocumentRefused("not_json")  # i18n-exempt: a reason code
    if document.get("client_id") != client_id:
        raise DocumentRefused("mismatch")  # i18n-exempt: a reason code
    name = document.get("client_name")
    uris = document.get("redirect_uris")
    shown = display_name(name) if isinstance(name, str) else None
    if shown is None or not isinstance(uris, list) or not 1 <= len(uris) <= MAX_REDIRECT_URIS:
        raise DocumentRefused("incomplete")  # i18n-exempt: a reason code
    try:
        checked = tuple(dict.fromkeys(check_registered(uri) for uri in uris))
    except RedirectRejected as exc:
        raise DocumentRefused("bad_redirect") from exc  # i18n-exempt: a reason code
    if document.get("token_endpoint_auth_method", "none") != "none" or (
        "client_secret" in document or "client_secret_expires_at" in document
    ):
        raise DocumentRefused("secret")  # i18n-exempt: a reason code
    return ClientDocument(
        client_id=client_id,
        client_name=shown,
        redirect_uris=checked,
        cache_for=cache_lifetime(answer.cache_control),
    )


async def fetch_document(client_id: str) -> ClientDocument:
    """The client `client_id` names, or `DocumentRefused` (logged once)."""
    try:
        return await _read(client_id)
    except DocumentRefused as refused:
        try:
            host = urlsplit(client_id).hostname or ""
        except ValueError:
            host = ""
        logger.warning(
            "client metadata document refused", extra={"reason": refused.reason, "host": host}
        )
        raise
```

- [ ] **Step 5: El recuento de logs**

En `backend/tests/test_logging_discipline.py`, en el docstring de la prueba del recuento, antes del párrafo «NOW 108,
own authentication phase 1, fix 3 review Minor 2», añadir:

```text
    NOW 109, own authentication phase 3 (plan 2026-11-06, Task 3): one static
    WARNING in `app/core/auth/oauth/cimd.py::fetch_document`, "client metadata
    document refused", when an AI app's client metadata document cannot be its
    client (unreachable, not JSON, a mismatched client_id, a secret...). Without
    it a connection that fails on the document is invisible to the operator.
    `extra` = the reason code and the document's host; never the URL's path,
    never the body, which a stranger wrote.

```

y cambiar «NOW 108, own authentication phase 1, fix 3 review Minor 2» por «BEFORE THAT, 108, own authentication phase
1, fix 3 review Minor 2», y `assert total == 108` / `expected 108` por `109`. En
`docs/superpowers/specs/2026-08-14-logging-kernel-design.md`, §2.2, antes de «**NOW 108, own authentication phase 1,
fix 3 review (Minor 2).**», un párrafo nuevo con el mismo texto que el anterior en prosa
(«**NOW 109, own authentication phase 3 (plan 2026-11-06, Task 3).** One site. …»), y «**NOW 108,» pasa a
«**BEFORE THAT, 108,». Si al fusionar la base ya no es 108 (la fase 2), el coordinador ajusta el número: el delta de
esta tarea es **+1**.

- [ ] **Step 6: Las pruebas pasan**

Run: `cd backend && .venv/bin/pytest -q tests/test_egress_get_bounded.py tests/test_oauth_cimd.py tests/test_egress_guard.py tests/test_notification_delivery.py tests/test_logging_discipline.py tests/test_i18n_catalog.py`
Expected: PASS.

Run: `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `Success: no issues found` y `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/core/egress.py backend/app/core/auth/oauth/__init__.py backend/app/core/auth/oauth/cimd.py \
  backend/tests/test_egress_get_bounded.py backend/tests/test_oauth_cimd.py \
  backend/tests/test_logging_discipline.py docs/superpowers/specs/2026-08-14-logging-kernel-design.md
git commit -m "feat(oauth): read client metadata documents through the egress guard"
```

### Task 4: Consola — la pantalla de consentimiento `/oauth/authorize`

Diseño §5.2; D2, D11. Oleada 1, en paralelo con las Tasks 1, 2, 3 y 11: se escribe contra el contrato del Apéndice A.2
y la Task 8 lo implementa. Solo consola.

Un cliente de IA manda a la persona a `<consola>/oauth/authorize?response_type=code&client_id=…&redirect_uri=…
&code_challenge=…&code_challenge_method=S256&state=…&resource=…`. Esa dirección es una página de la consola (D2: con la
cookie `SameSite=Strict`, una navegación que llega desde claude.ai no lleva la sesión hasta la API, y un `fetch` del
mismo origen sí). La página:
1. exige sesión (`RequireAuth`), sin el armazón de la consola: sin sesión, al inicio de sesión con `?next=` y la
   consulta entera, y de vuelta aquí;
2. manda la consulta, tal cual, a `POST /api/v1/oauth/authorize/details`, que la valida en el servidor;
3. enseña el nombre del cliente, **el dominio de su redirect**, el host de su documento si es CIMD (CIMD-00 §6.4), un
   aviso si solo vuelve a este equipo (MCP [S30]), qué concede y con qué cuenta;
4. con «Aprobar» o «Rechazar» llama a `…/decision` y navega a la redirección que el servidor devuelve. Esa
   redirección lleva el código de autorización: es un secreto, así que la mutación es `useSecretMutation` y la página
   no la guarda en ningún sitio.

**Files:**
- Create: `frontend/src/modules/auth/consent.ts`
- Create: `frontend/src/modules/auth/ConsentPage.tsx`
- Modify: `frontend/src/lib/api.ts` (tipos `ConsentClient`, `ConsentAnswer`, `ConsentDetails` junto a los del
  inicio de sesión, y dos métodos al final del bloque de `/auth/*`, después de `getRecovery`/`acceptRecovery`)
- Modify: `frontend/src/App.tsx` (`RequireAuth` gana `bare`; la ruta `/oauth/authorize` después de las públicas)
- Modify: `frontend/src/lib/session.ts` (`EDGE_PATHS`)
- Modify: `frontend/scripts/routes.mjs` (`EXCLUDED`)
- Modify: `frontend/src/i18n/en/auth.ts`, `frontend/src/i18n/es/auth.ts` (`consent`)
- Modify: `frontend/src/modules/auth/__tests__/harness.tsx` (dos métodos en `calls`, sus valores por defecto y la
  ruta)
- Modify: `frontend/src/test/secretMutations.test.ts` (`SECRET_METHODS`, `SECRET_SITES`)
- Modify: `frontend/src/lib/session.test.ts`
- Test: `frontend/src/modules/auth/__tests__/consent.test.tsx`, `frontend/src/modules/auth/__tests__/consentGuard.test.tsx`

**Interfaces:**
- Consumes: el contrato de `POST /api/v1/oauth/authorize/details` y `…/decision` (Apéndice A.2); de la consola,
  `ApiClient.request<T>(path, init, { global: true })` (como `getMySecurity`, `frontend/src/lib/api.ts:2149`),
  `useSecretMutation` (`frontend/src/lib/secretMutation.ts:48`), `navigation.assign` (`frontend/src/lib/session.ts`),
  `AuthCard`, `AuthAlert` (`frontend/src/modules/auth/AuthCard.tsx`), `Btn`, `KIND_ICON` (`frontend/src/shell/components`),
  `Icon` (`frontend/src/shell/icons`), `describeSubmitError` (`frontend/src/components/form`), `useT`.
- Produces: `ApiClient.oauthAuthorizeDetails(query)`, `ApiClient.oauthAuthorizeDecision(query, approve)`, los tipos
  `ConsentClient`, `ConsentAnswer`, `ConsentDetails`; `RequireAuth({ children, bare })`; `ConsentPage`;
  `authorizationQuery(search)`.

**Decisión de la tarea (1): la consulta viaja entera y sin interpretar.** La página no lee `client_id` ni
`redirect_uri`: manda `location.search` sin el `?`, y el servidor la valida (Task 8) cada vez, también al decidir. Así
la consola no tiene una segunda copia de las reglas, y una consulta manipulada se rechaza en el mismo sitio que una
legítima se acepta.

**Decisión de la tarea (2): `RequireAuth bare`.** El consentimiento es una tarjeta, como el inicio de sesión
(`AuthCard`), pero necesita sesión. `RequireAuth` con `bare` hace todo lo que hace hoy (sin sesión, al inicio de
sesión con `?next=`; una sesión que termina, oculta y vaciada) y, con sesión, pinta solo la página: sin barra lateral,
sin selector de cliente (una concesión de IA no es de ningún cliente).

- [ ] **Step 1: Las pruebas que fallan**

En `frontend/src/modules/auth/__tests__/harness.tsx`, añadir a `calls`:

```ts
  oauthAuthorizeDetails: vi.fn(),
  oauthAuthorizeDecision: vi.fn(),
```

al final de `installDefaults()`:

```ts
  calls.oauthAuthorizeDetails.mockResolvedValue(CONSENT);
  calls.oauthAuthorizeDecision.mockResolvedValue({ outcome: "redirect", redirect_to: APPROVED });
```

estas constantes después de `OTPAUTH`:

```ts
/** What `POST /oauth/authorize/details` answers for a registered app (plan 2026-11-06, Appendix A.2). */
export const CONSENT = {
  outcome: "consent",
  client: {
    name: "Claude",
    client_id: "itms_Zm9vYmFyYmF6cXV4cXV1eGNvcmdlZ3JhdWx0",
    kind: "registered",
    client_host: null,
    redirect_host: "claude.ai",
    loopback_only: false,
  },
  scope: "mcp",
  account: "dana@example.test",
} as const;

/** The redirect an approval answers: it carries the one-time code. */
export const APPROVED =
  "https://claude.ai/api/mcp/auth_callback?code=c0de-never-kept&state=st4te&iss=https%3A%2F%2Fitms.example";
```

el import `import { ConsentPage } from "../ConsentPage";` y, en `renderAuth`, la ruta
`<Route path="/oauth/authorize" element={<ConsentPage />} />` antes de la de `*`.

`frontend/src/modules/auth/__tests__/consent.test.tsx`:

```tsx
/**
 * `/oauth/authorize` — an AI app asks the person to approve it (own-auth design
 * §5.2; plan 2026-11-06, Task 4).
 *
 * The page never reads the request itself: it sends the query, as the browser
 * has it, to the server, shows what the server says, and goes where the
 * server's answer says. The approval's redirect carries the authorization
 * code, which must not stay in the mutation cache.
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { navigation } from "../../../lib/session";
import { APPROVED, CONSENT, authApi, calls, installDefaults, mutationCacheText, renderAuth } from "./harness";

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => authApi() }));

const QUERY =
  "response_type=code&client_id=itms_x&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback" +
  "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM&code_challenge_method=S256&state=st4te";
const PAGE = `/oauth/authorize?${QUERY}`;
const DENIED = "https://claude.ai/api/mcp/auth_callback?error=access_denied&state=st4te&iss=https%3A%2F%2Fitms.example";

let assign: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  installDefaults();
  assign = vi.spyOn(navigation, "assign").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("approving an AI app", () => {
  it("asks the server about the query exactly as the browser has it, and names the app, its return and the account", async () => {
    renderAuth(PAGE);
    expect(await screen.findByRole("heading", { name: "Connect Claude" })).toBeInTheDocument();
    expect(calls.oauthAuthorizeDetails).toHaveBeenCalledWith(QUERY);
    expect(screen.getByText("Your answer goes back to claude.ai.")).toBeInTheDocument();
    expect(screen.getByText("You are signed in as dana@example.test.")).toBeInTheDocument();
    expect(
      screen.getByText(
        "If you approve, it can act as you in ITMS: everything your role allows, including running actions on managed computers.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(`App id: ${CONSENT.client.client_id}`)).toBeInTheDocument();
    // A registered app publishes no document, and returns to an https site.
    expect(screen.queryByText(/Its description is published by/)).toBeNull();
    expect(screen.queryByText(/only answers on this computer/)).toBeNull();
  });

  it("names the host that publishes a client metadata document", async () => {
    calls.oauthAuthorizeDetails.mockResolvedValue({
      ...CONSENT,
      client: { ...CONSENT.client, kind: "metadata_document", client_id: "https://claude.example/m.json", client_host: "claude.example" },
    });
    renderAuth(PAGE);
    expect(await screen.findByText("Its description is published by claude.example.")).toBeInTheDocument();
  });

  it("warns when the app only returns to this computer", async () => {
    calls.oauthAuthorizeDetails.mockResolvedValue({
      ...CONSENT,
      client: { ...CONSENT.client, redirect_host: "localhost", loopback_only: true },
    });
    renderAuth(PAGE);
    expect(
      await screen.findByText(
        "This app only answers on this computer (localhost). Approve it only if you started it yourself, on this computer, just now.",
      ),
    ).toBeInTheDocument();
  });

  it("says when the app has no name", async () => {
    calls.oauthAuthorizeDetails.mockResolvedValue({ ...CONSENT, client: { ...CONSENT.client, name: null } });
    renderAuth(PAGE);
    expect(await screen.findByRole("heading", { name: "Connect An AI app with no name" })).toBeInTheDocument();
  });

  it("approves, goes where the server says, and keeps no code in the mutation cache", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderAuth(PAGE);
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith(APPROVED));
    expect(calls.oauthAuthorizeDecision).toHaveBeenCalledWith(QUERY, true);
    expect(mutationCacheText(queryClient)).not.toContain("c0de-never-kept");
  });

  it("declines and goes back to the app with the server's refusal", async () => {
    calls.oauthAuthorizeDecision.mockResolvedValue({ outcome: "redirect", redirect_to: DENIED });
    const user = userEvent.setup();
    renderAuth(PAGE);
    await user.click(await screen.findByRole("button", { name: "Decline" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith(DENIED));
    expect(calls.oauthAuthorizeDecision).toHaveBeenCalledWith(QUERY, false);
  });

  it("answers once: both buttons are disabled while the decision is on its way", async () => {
    calls.oauthAuthorizeDecision.mockReturnValue(new Promise(() => undefined));
    const user = userEvent.setup();
    renderAuth(PAGE);
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    expect(screen.getByRole("button", { name: "Working…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Decline" })).toBeDisabled();
    expect(calls.oauthAuthorizeDecision).toHaveBeenCalledTimes(1);
  });
});

describe("a request the page cannot approve", () => {
  it("shows the server's sentence and offers no way back to an address it does not trust", async () => {
    calls.oauthAuthorizeDetails.mockResolvedValue({
      outcome: "refused",
      error: "invalid_redirect_uri",
      detail: "The address this AI app asked to go back to is not one it registered.",
    });
    renderAuth(PAGE);
    expect(await screen.findByRole("heading", { name: "This connection request cannot be used" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("The address this AI app asked to go back to is not one it registered.");
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Back to the app" })).toBeNull();
    expect(assign).not.toHaveBeenCalled();
  });

  it("says why the person cannot connect AI apps, and goes back to the app only when asked", async () => {
    calls.oauthAuthorizeDetails.mockResolvedValue({
      outcome: "denied",
      detail: "requires mcp.use",
      redirect_to: DENIED,
    });
    const user = userEvent.setup();
    renderAuth(PAGE);
    expect(await screen.findByRole("heading", { name: "You cannot connect AI apps here" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("requires mcp.use");
    expect(assign).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Back to the app" }));
    expect(assign).toHaveBeenCalledWith(DENIED);
  });

  it("goes straight back to the app when the server says the request is the app's to hear", async () => {
    calls.oauthAuthorizeDetails.mockResolvedValue({ outcome: "redirect", redirect_to: DENIED });
    renderAuth(PAGE);
    await waitFor(() => expect(assign).toHaveBeenCalledWith(DENIED));
    expect(assign).toHaveBeenCalledTimes(1);
  });

  it("shows the server's refusal of the decision and lets the person try nothing else", async () => {
    calls.oauthAuthorizeDecision.mockResolvedValue({
      outcome: "refused",
      error: "invalid_client",
      detail: "This AI app is not registered here, or its registration expired. Connect it again from the app.",
    });
    const user = userEvent.setup();
    renderAuth(PAGE);
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("This AI app is not registered here");
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(assign).not.toHaveBeenCalled();
  });
});
```

`frontend/src/modules/auth/__tests__/consentGuard.test.tsx`:

```tsx
/**
 * `/oauth/authorize` needs a session and draws no console around it
 * (`RequireAuth bare`, plan 2026-11-06 Task 4). With no session the person is
 * sent to sign in and brought back to the same request, query and all.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { navigation } from "../../../lib/session";

const { ApiProvider } = await import("../../../lib/ApiContext");
const { RequireAuth } = await import("../../../App");

const ME = {
  user_id: "u1",
  display_name: "Ada Lovelace",
  email: "ada@example.test",
  org_id: "org-1",
  roles: ["org-admin"],
  permissions: ["devices.read", "mcp.use"],
};
const PAGE = "/oauth/authorize?client_id=itms_x&state=s";

function answerMe(status: number, body: unknown): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) =>
      String(input).endsWith("/api/v1/me")
        ? new Response(JSON.stringify(body), { status })
        : new Response("{}", { status: 404 }),
    ),
  );
}

function bare() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={queryClient}>
      <ApiProvider>
        <MemoryRouter initialEntries={[PAGE]}>
          <RequireAuth bare>
            <p>the consent card</p>
          </RequireAuth>
        </MemoryRouter>
      </ApiProvider>
    </QueryClientProvider>
  );
}

let assign: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  assign = vi.spyOn(navigation, "assign").mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the consent page's guard", () => {
  it("draws only the page for a signed-in person: no rail, no top bar", async () => {
    answerMe(200, ME);
    render(bare());
    expect(await screen.findByText("the consent card")).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(screen.queryByRole("banner")).toBeNull();
  });

  it("sends a visitor to sign in and back to the same request, query and all", async () => {
    answerMe(401, { code: "auth_required" });
    render(bare());
    await waitFor(() => expect(assign).toHaveBeenCalledTimes(1));
    expect(assign).toHaveBeenCalledWith(`/sign-in?next=${encodeURIComponent(PAGE)}`);
    expect(screen.queryByText("the consent card")).toBeNull();
  });
});
```

En `frontend/src/lib/session.test.ts`, dentro del `describe` de `safeNext` (después de la prueba de `:92-95`):

```ts
  it("keeps the consent page and its query, and refuses the OAuth server's own endpoints", () => {
    expect(safeNext("/oauth/authorize?client_id=itms_x&state=s")).toBe("/oauth/authorize?client_id=itms_x&state=s");
    for (const edge of ["/oauth/token", "/oauth/register", "/oauth/revoke"]) {
      expect(safeNext(edge)).toBe(DEFAULT_AFTER_SIGN_IN);
      expect(safeNext(`${edge}?x=1`)).toBe(DEFAULT_AFTER_SIGN_IN);
    }
  });
```

Run: `cd frontend && npx vitest run src/modules/auth src/lib/session.test.ts`
Expected: FAIL (`Cannot find module '../ConsentPage'`; `RequireAuth` no acepta `bare`; `safeNext("/oauth/token")` devuelve
`/oauth/token`).

- [ ] **Step 2: Los textos**

En `frontend/src/i18n/en/auth.ts`, una clave nueva al final de `auth` (después de `passkey`):

```ts
  consent: {
    loading: "Checking the request…",
    title: "Connect {app}",
    unnamed: "An AI app with no name",
    intro: "{app} asks to use ITMS as you.",
    grants:
      "If you approve, it can act as you in ITMS: everything your role allows, including running actions on managed computers.",
    account: "You are signed in as {email}.",
    returnsTo: "Your answer goes back to {host}.",
    publishedBy: "Its description is published by {host}.",
    appId: "App id: {id}",
    loopbackOnly:
      "This app only answers on this computer (localhost). Approve it only if you started it yourself, on this computer, just now.",
    approve: "Approve",
    deny: "Decline",
    refusedTitle: "This connection request cannot be used",
    deniedTitle: "You cannot connect AI apps here",
    back: "Back to the app",
  },
```

En `frontend/src/i18n/es/auth.ts`, en el mismo sitio:

```ts
  consent: {
    loading: "Comprobando la solicitud…",
    title: "Conectar {app}",
    unnamed: "Una aplicación de IA sin nombre",
    intro: "{app} pide usar ITMS en tu nombre.",
    grants:
      "Si la apruebas, podrá actuar como tú en ITMS: todo lo que permite tu rol, incluido ejecutar acciones en equipos gestionados.",
    account: "Has iniciado sesión como {email}.",
    returnsTo: "Tu respuesta vuelve a {host}.",
    publishedBy: "Su descripción la publica {host}.",
    appId: "Id de la aplicación: {id}",
    loopbackOnly:
      "Esta aplicación solo responde en este equipo (localhost). Apruébala solo si la abriste tú, en este equipo, ahora mismo.",
    approve: "Aprobar",
    deny: "Rechazar",
    refusedTitle: "Esta solicitud de conexión no se puede usar",
    deniedTitle: "No puedes conectar aplicaciones de IA aquí",
    back: "Volver a la aplicación",
  },
```

El texto de `grants` es el de la pantalla de consentimiento de Keycloak que sustituye (`deploy/keycloak/ci_mcp_flow.py`,
`CONSENT_TEXT`): lo que aprobar entrega no cambia de palabras con el emisor.

- [ ] **Step 3: El contrato en `ApiClient`**

En `frontend/src/lib/api.ts`, junto a los tipos del inicio de sesión (`InviteDetails`, `RecoveryLink`):

```ts
/**
 * The AI app a consent screen names (`backend/app/core/auth/oauth/consent.py`,
 * plan 2026-11-06 Appendix A.2). `client_host` is the host that publishes a
 * client metadata document (`kind: "metadata_document"`), null for an app that
 * registered; `redirect_host` is where the answer goes; `loopback_only` when
 * every address it registered is this computer.
 */
export interface ConsentClient {
  name: string | null;
  client_id: string;
  kind: "registered" | "metadata_document";
  client_host: string | null;
  redirect_host: string;
  loopback_only: boolean;
}

/**
 * What the server answers about an authorization request, other than "ask
 * the person": `refused` is shown and never redirected (RFC 6749 §4.1.2.1: a
 * bad client or redirect), `denied` is shown with a way back to the app,
 * `redirect` is where to go now -- with the code, after an approval.
 */
export type ConsentAnswer =
  | { outcome: "refused"; error: string; detail: string }
  | { outcome: "denied"; detail: string; redirect_to: string }
  | { outcome: "redirect"; redirect_to: string };

/** `POST /oauth/authorize/details`: `consent` asks the person; the rest is `ConsentAnswer`. */
export type ConsentDetails =
  | { outcome: "consent"; client: ConsentClient; scope: string; account: string }
  | ConsentAnswer;
```

y en la clase, después de `acceptRecovery`:

```ts
  /**
   * `POST /oauth/authorize/details`: what an AI app's authorization request
   * asks, for the consent screen (own-auth design §5.2). `query` is
   * `/oauth/authorize`'s, as the browser has it, without the `?`; the server
   * validates it, and validates it again at the decision.
   */
  oauthAuthorizeDetails(query: string): Promise<ConsentDetails> {
    return this.request<ConsentDetails>(
      "/oauth/authorize/details",
      { method: "POST", body: JSON.stringify({ query }) },
      { global: true },
    );
  }

  /**
   * `POST /oauth/authorize/decision`: the person's answer. An approval's
   * `redirect_to` carries a one-time authorization code -- a secret, so the
   * page's mutation is `useSecretMutation` (`src/test/secretMutations.test.ts`).
   */
  oauthAuthorizeDecision(query: string, approve: boolean): Promise<ConsentAnswer> {
    return this.request<ConsentAnswer>(
      "/oauth/authorize/decision",
      { method: "POST", body: JSON.stringify({ query, approve }) },
      { global: true },
    );
  }
```

- [ ] **Step 4: La página**

`frontend/src/modules/auth/consent.ts`:

```ts
/**
 * The consent page's one rule about the request: it does not read it. The
 * query goes to the server as the browser has it (plan 2026-11-06, Task 4,
 * decision 1), so `/oauth/authorize?a=1` sends `a=1`.
 */
export function authorizationQuery(search: string): string {
  return search.startsWith("?") ? search.slice(1) : search;
}
```

`frontend/src/modules/auth/ConsentPage.tsx`:

```tsx
/**
 * `/oauth/authorize` — where an AI app sends a person to approve it (own-auth
 * design §5.2; plan 2026-11-06, Task 4).
 *
 * A CONSOLE PAGE, NOT AN API ROUTE (plan D2). The session cookie is
 * `SameSite=Strict`, so a navigation that starts on the AI app's site does not
 * carry it to the API; a same-origin `fetch` from this page does. So the page
 * hands the request's query to `POST /api/v1/oauth/authorize/details` and
 * `/decision`, which validate it every time, and goes where they say.
 *
 * WHAT THE PERSON IS SHOWN, from the server's answer: the app's name (any
 * app may call itself anything), the host its answer goes back to (MCP: the
 * server "MUST clearly display the redirect URI hostname" [S30]), the host
 * that publishes its description when it has one (CIMD-00 §6.4), a warning
 * when it only answers on this computer [S30], what approving hands over, and
 * the account it is approved for.
 *
 * THE APPROVAL'S REDIRECT CARRIES THE AUTHORIZATION CODE. The decision is a
 * `useSecretMutation` (`gcTime: 0`, reset once it settles), the redirect is
 * read once, in the handler, and the document leaves for it at once.
 *
 * NOTHING HERE DECIDES ANYTHING (invariant 6): a request the page shows is a
 * request the server accepted a moment ago, and the server accepts it again,
 * or not, at the decision.
 */
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useLocation } from "react-router-dom";

import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import type { ConsentAnswer } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useSecretMutation } from "../../lib/secretMutation";
import { navigation } from "../../lib/session";
import { Btn, KIND_ICON } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { AuthAlert, AuthCard } from "./AuthCard";
import { authorizationQuery } from "./consent";

const LIST = { margin: 0, paddingLeft: 18, listStyle: "disc outside" } as const;

export function ConsentPage() {
  const t = useT();
  const api = useApi();
  const { search } = useLocation();
  const query = authorizationQuery(search);
  // ASKED ONCE, and the query is not part of the key: it is the request's,
  // not a cache entry's, and it leaves with the page.
  const details = useQuery({
    queryKey: ["oauth", "consent"],
    queryFn: () => api.oauthAuthorizeDetails(query),
    retry: false,
    staleTime: "static",
    gcTime: 0,
    refetchOnWindowFocus: false,
  });
  const decision = useSecretMutation({
    mutationFn: (approve: boolean) => api.oauthAuthorizeDecision(query, approve),
  });
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [answered, setAnswered] = useState<ConsentAnswer | null>(null);
  const inFlight = useRef(false);

  // A request the server says is the app's to hear (a bad PKCE challenge, an
  // unsupported response type): straight back, with nothing to ask.
  const goBack = details.data?.outcome === "redirect" ? details.data.redirect_to : null;
  useEffect(() => {
    if (goBack !== null) navigation.assign(goBack);
  }, [goBack]);

  async function answer(approve: boolean) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setFailure(null);
    try {
      const outcome = await decision.mutateAsync(approve);
      if (outcome.outcome === "redirect") {
        // The document leaves; the buttons stay disabled until it has.
        navigation.assign(outcome.redirect_to);
        return;
      }
      setAnswered(outcome);
    } catch (cause: unknown) {
      setFailure(describeSubmitError(cause));
    }
    inFlight.current = false;
    setBusy(false);
  }

  if (details.isPending || goBack !== null) {
    return (
      <AuthCard title={t("auth.consent.loading")}>
        <p className="note" style={{ margin: 0 }}>
          {t("auth.common.working")}
        </p>
      </AuthCard>
    );
  }
  if (details.isError) {
    return (
      <AuthCard title={t("auth.consent.refusedTitle")}>
        <AuthAlert text={describeSubmitError(details.error)} />
      </AuthCard>
    );
  }
  const shown = answered ?? details.data;
  if (shown.outcome === "refused") {
    return (
      <AuthCard title={t("auth.consent.refusedTitle")}>
        <AuthAlert text={shown.detail} />
      </AuthCard>
    );
  }
  if (shown.outcome === "denied") {
    return (
      <AuthCard title={t("auth.consent.deniedTitle")}>
        <AuthAlert text={shown.detail} />
        <div className="form-actions">
          <Btn label={t("auth.consent.back")} onClick={() => navigation.assign(shown.redirect_to)} />
        </div>
      </AuthCard>
    );
  }
  if (shown.outcome !== "consent") return null;
  const { client, account } = shown;
  const app = client.name ?? t("auth.consent.unnamed");
  return (
    <AuthCard title={t("auth.consent.title", { app })} intro={t("auth.consent.intro", { app })}>
      <p className="note" style={{ margin: 0 }}>
        {t("auth.consent.grants")}
      </p>
      {client.loopback_only ? (
        <p role="note" className="notice gold" style={{ margin: 0 }}>
          <Icon name={KIND_ICON.warn} size={16} />
          <span>{t("auth.consent.loopbackOnly")}</span>
        </p>
      ) : null}
      <ul className="note" style={LIST}>
        <li>{t("auth.consent.returnsTo", { host: client.redirect_host })}</li>
        {client.client_host ? <li>{t("auth.consent.publishedBy", { host: client.client_host })}</li> : null}
        <li className="mono" style={{ overflowWrap: "anywhere" }}>
          {t("auth.consent.appId", { id: client.client_id })}
        </li>
        <li>{t("auth.consent.account", { email: account })}</li>
      </ul>
      <AuthAlert text={failure} />
      <div className="form-actions">
        <Btn
          variant="primary"
          label={busy ? t("auth.common.working") : t("auth.consent.approve")}
          disabled={busy}
          onClick={() => void answer(true)}
        />
        <Btn label={t("auth.consent.deny")} disabled={busy} onClick={() => void answer(false)} />
      </div>
    </AuthCard>
  );
}
```

- [ ] **Step 5: `RequireAuth bare` y la ruta**

En `frontend/src/App.tsx`, la firma de `RequireAuth` pasa a:

```tsx
export function RequireAuth({ children, bare = false }: { children: React.ReactNode; bare?: boolean }) {
```

el párrafo nuevo al final de su docstring (antes de `*/`):

```tsx
 *
 * `bare` (plan 2026-11-06, Task 4): the same guard, and with a session the
 * page alone -- no shell, no client selector. For `/oauth/authorize`, a card
 * like the sign-in pages that still needs a session: an AI app's grant is the
 * person's, no client's.
```

y el bloque que pinta el armazón:

```tsx
  if (session.me !== undefined) {
    if (bare) return <>{children}</>;
    return (
      <ClientScopeProvider>
        <Shell>{children}</Shell>
      </ClientScopeProvider>
    );
  }
```

El import `import { ConsentPage } from "./modules/auth/ConsentPage";` junto a los de `./modules/auth/`, y la ruta justo
después de `<Route path="/recover" element={<RecoverPage />} />`, con su comentario:

```tsx
            {/* An AI app's authorization request (plan 2026-11-06, Task 4):
                a session, no shell. NOT a public path: with no session
                `RequireAuth` sends the person to sign in and back here, query
                and all. */}
            <Route path="/oauth/authorize" element={<RequireAuth bare><ConsentPage /></RequireAuth>} />
```

En `frontend/src/lib/session.ts`, después de `EDGE_PREFIXES`:

```ts
/**
 * Exact paths the edge sends to the api (`deploy/docker-compose.yml`, plan
 * 2026-11-06 Task 6): the OAuth server's machine endpoints. `/oauth/authorize`
 * is the console's own consent page and stays a page.
 */
const EDGE_PATHS: readonly string[] = ["/oauth/token", "/oauth/register", "/oauth/revoke"];
```

y `isEdgePath` pasa a:

```ts
function isEdgePath(pathname: string): boolean {
  return (
    EDGE_PATHS.includes(pathname) ||
    EDGE_PREFIXES.some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`))
  );
}
```

En `frontend/scripts/routes.mjs`, `EXCLUDED` gana `"/oauth/authorize"`, y el docstring de arriba, tras «…a page whose
only job is to consume an authorization code the gate does not have)», añade: «, the OAuth consent page
(`/oauth/authorize`, plan 2026-11-06: it needs an AI app's authorization request the gate does not have)».

- [ ] **Step 6: El inventario de secretos**

En `frontend/src/test/secretMutations.test.ts`, en `SECRET_METHODS`, junto a `acceptRecovery`:

```ts
  oauthAuthorizeDecision: "the answer's redirect carries a one-time authorization code",
```

y en `SECRET_SITES`, junto a la de `RecoverPage`:

```ts
  // `useSecretMutation`; its test reads the mutation cache after an approval.
  "modules/auth/ConsentPage.tsx oauthAuthorizeDecision": { reviewed: "modules/auth/__tests__/consent.test.tsx" },
```

- [ ] **Step 7: Las pruebas pasan**

Run: `cd frontend && npx vitest run`
Expected: PASS, todo (incluidos `src/test/routes-covered.test.ts`, `src/modules/auth/__tests__/routes.test.ts`,
`src/test/secretMutations.test.ts` y las pruebas de paridad de `src/i18n`).

Run: `cd frontend && npx tsc -b && npm run build`
Expected: sin errores.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/modules/auth/consent.ts frontend/src/modules/auth/ConsentPage.tsx frontend/src/lib/api.ts \
  frontend/src/App.tsx frontend/src/lib/session.ts frontend/scripts/routes.mjs frontend/src/i18n/en/auth.ts \
  frontend/src/i18n/es/auth.ts frontend/src/modules/auth/__tests__/harness.tsx \
  frontend/src/modules/auth/__tests__/consent.test.tsx frontend/src/modules/auth/__tests__/consentGuard.test.tsx \
  frontend/src/test/secretMutations.test.ts frontend/src/lib/session.test.ts
git commit -m "feat(console): the AI app consent page at /oauth/authorize"
```

### Task 5: Migración 0247, modelos, catálogo, `store.py` y `clients.find_client`

Diseño §6.1, §6.2; D3, D10. Oleada 2, **sola y solo cuando 0246 (fase 2) está en la rama**. Backend.

Las cuatro tablas del servidor OAuth (Apéndice A.3), su lugar en el catálogo de alcance por cliente, el único punto que
abre el alcance «todos» sobre ellas y la consulta de arranque de la organización del despliegue. Y las dos funciones
de lectura de `clients.py` que las Tasks 6, 7 y 8 consumen.

**Files:**
- Create: `backend/migrations/versions/0247_oauth_server.py`
- Create: `backend/app/models/oauth.py`
- Modify: `backend/app/models/__init__.py` (import y `__all__`, junto a los de `app.models.auth`)
- Modify: `backend/app/core/client_scope_catalog.py` (`CATALOG`, junto a las entradas de 0245)
- Modify: `backend/app/core/client_scope.py` (`ALL_SCOPE_ALLOWLIST`, grupo (e), después de `recover.py`)
- Create: `backend/app/core/auth/oauth/store.py`
- Create: `backend/app/core/auth/oauth/clients.py`
- Modify: `backend/tests/test_client_catalog.py` (`SURROGATE_COLUMNS`)
- Create: `backend/tests/oauth_support.py`
- Test: `backend/tests/test_oauth_schema.py`, `backend/tests/test_client_catalog.py`, `backend/tests/test_migrations.py`

**Interfaces:**
- Consumes: 0246 (fase 2) como `down_revision`; las funciones de 0231 `its_client_from_parent` e
  `its_client_to_children` (las usa 0245 igual); `app.models.base.Base`, `uuid7_pk`, `client_org_fk`
  (`backend/app/models/base.py:22-39`); `org_session(org_id, *, clients, engine)` (`backend/app/core/tenancy.py:24`);
  `ClientScope.all()`; `app.models.Organization`; `policy.CLIENT_IDLE` (Task 1).
- Produces: las tablas y los modelos `OAuthClient`, `OAuthCode`, `OAuthRefreshFamily`, `OAuthRefreshToken` con
  `CLIENT_KINDS = ("registered", "metadata_document")` y `REVOKE_REASONS` (Step 3); `store.deployment_org`,
  `store.oauth_session`; `clients.ClientView`, `clients.view`, `clients.find_client`, `clients.touch`; en
  `tests/oauth_support.py`, `BASE`, `RESOURCE`, `CLAUDE_CALLBACK`, `LOOPBACK_CALLBACK`, `oauth_org`, `seed_client`,
  `oauth_http` (las Tasks 6–10 los usan y añaden funciones al final).

**Decisión de la tarea (1): `client_id` es el del cliente MSP en las tablas O, y el del cliente OAuth solo en
`oauth_clients`.** Las tablas de una persona llevan la columna `client_id` del patrón 0231 (el disparador la copia de
`users`). Por eso la referencia al cliente OAuth se llama `oauth_client_id` en las tres, y `client_id` solo significa
«client id de OAuth» en `oauth_clients`, que es G, como ya ocurre en `mcp_revocation` (`client_scope_catalog.py:9`).

**Decisión de la tarea (2): una familia y un código solo nombran una app de su propia organización.** Una clave
foránea ignora RLS; `(oauth_client_id, org_id) → oauth_clients (id, org_id)` impide, como `client_org_fk`, que una fila
de la organización A apunte a la app de la B. Los nombres terminan en `_app_org` y no en `_client_org`, para no caer
en el barrido de `test_the_client_constraints_are_declared_on_the_models`, que es el de los clientes MSP.

**Decisión de la tarea (3): `REVOKE_REASONS` es una lista cerrada en un CHECK.** Una familia revocada dice por qué:
`refresh_reused`, `code_reused` (Task 7), `revoked_by_app` (RFC 7009, Task 7), `revoked_in_settings` (Task 10),
`signed_out_everywhere`, `password_changed`, `factors_reset`, `disabled`, `deleted` (los actos de la cuenta, Task 10) y
`person_inactive` (la persona dejó de estar activa antes de un refresh, Task 7).

- [ ] **Step 1: La prueba que falla**

`backend/tests/oauth_support.py`:

```python
"""Shared helpers for the OAuth server's tests (autenticación propia plan
2026-11-06). Tasks 6 to 10 add their own helpers at the end of this file.

`oauth_org` makes an organization the deployment signs in to (`ITS_DEFAULT_
ORG_SLUG`, plan D3) at the console address the MCP tests already use, with
MCP switched on; `seed_client` writes an AI app's registration directly;
`oauth_http` is a network-shaped client: no cookie, no console headers.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.oauth.store import oauth_session
from app.models import Organization
from app.models.oauth import OAuthClient

#: `tests.mcp_support.MCP_BASE`, the console the MCP gate answers at.
BASE = "https://itms.example.com"
RESOURCE = f"{BASE}/mcp"
#: Anthropic: the hosted Claude apps "register exactly this redirect URI" [S33].
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
LOOPBACK_CALLBACK = "http://127.0.0.1:33418/callback"


async def oauth_org(env: Any, prefix: str = "oauth", *, mcp_enabled: bool = True) -> tuple[UUID, str]:
    org_id = await env.create_org(prefix)
    if mcp_enabled:
        async with AsyncSession(env.admin_engine) as session:
            await session.execute(
                update(Organization)
                .where(Organization.id == org_id)
                .values(settings={"mcp_enabled": True})
            )
            await session.commit()
    slug = await env.org_slug(org_id)
    env.settings.public_console_url = BASE
    env.settings.default_org_slug = slug
    return org_id, slug


async def seed_client(
    env: Any,
    org_id: UUID,
    *,
    client_id: str | None = None,
    redirect_uris: tuple[str, ...] = (CLAUDE_CALLBACK,),
    name: str | None = "Claude",
    kind: str = "registered",
    last_used_at: datetime | None = None,
    metadata_expires_at: datetime | None = None,
) -> OAuthClient:
    now = datetime.now(UTC)
    if kind == "metadata_document" and metadata_expires_at is None:
        metadata_expires_at = now + timedelta(hours=1)
    async with oauth_session(org_id, env.app_engine) as session:
        row = OAuthClient(
            org_id=org_id,
            client_id=client_id or f"itms_{uuid4().hex}",
            kind=kind,
            client_name=name,
            redirect_uris=list(redirect_uris),
            created_at=now,
            last_used_at=last_used_at or now,
            metadata_expires_at=metadata_expires_at if kind == "metadata_document" else None,
        )
        session.add(row)
        await session.flush()
    return row


def oauth_http(env: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url=BASE)
```

`backend/tests/test_oauth_schema.py`:

```python
"""The OAuth server's tables (migration 0247; autenticación propia design
§6.1; plan 2026-11-06, Task 5).

- all four carry `org_id` under forced RLS: one organization's grants do not
  exist for another (invariant 5, falsifier 1);
- a technician's grant rows are global rows (O under `users`): written only
  under an every-client scope;
- a code and a refresh token are stored as 32-byte digests, never as the
  value;
- a grant names an AI app of its own organization: a foreign key RLS cannot
  see is held by a composite key;
- the deployment's organization is `ITS_DEFAULT_ORG_SLUG`'s, and a
  registration unused for 30 days is not found.
"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.oauth import clients
from app.core.auth.oauth.store import deployment_org, oauth_session
from app.core.client_scope import ClientScope
from app.core.global_rows import GlobalRowNotWritable
from app.core.tenancy import org_session
from app.models import Client
from app.models.oauth import OAuthClient, OAuthCode, OAuthRefreshFamily, OAuthRefreshToken
from tests.oauth_support import CLAUDE_CALLBACK, RESOURCE, oauth_org, seed_client
from tests.session_support import user_id_of

TABLES = ("oauth_clients", "oauth_codes", "oauth_refresh_families", "oauth_refresh_tokens")


async def test_the_four_tables_are_tenant_scoped_and_forced(admin_engine) -> None:
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = ANY(:names)"
                ),
                {"names": list(TABLES)},
            )
        ).all()
    assert {r.relname: (r.relrowsecurity, r.relforcerowsecurity) for r in rows} == {
        t: (True, True) for t in TABLES
    }


async def _grant(env, org_id, user_id, app: OAuthClient) -> None:
    """One family, one refresh token and one code of `user_id` for `app`."""
    now = datetime.now(UTC)
    async with oauth_session(org_id, env.app_engine) as session:
        family = OAuthRefreshFamily(
            org_id=org_id,
            user_id=user_id,
            oauth_client_id=app.id,
            resource=RESOURCE,
            created_at=now,
            last_used_at=now,
            expires_at=now + timedelta(days=30),
        )
        session.add(family)
        await session.flush()
        session.add(
            OAuthRefreshToken(
                org_id=org_id,
                user_id=user_id,
                family_id=family.id,
                token_hash=sha256(uuid4().bytes).digest(),
                created_at=now,
            )
        )
        session.add(
            OAuthCode(
                org_id=org_id,
                user_id=user_id,
                oauth_client_id=app.id,
                code_hash=sha256(uuid4().bytes).digest(),
                redirect_uri=CLAUDE_CALLBACK,
                code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
                resource=RESOURCE,
                created_at=now,
                expires_at=now + timedelta(seconds=60),
            )
        )


async def test_one_organizations_grants_do_not_exist_for_another(api_env) -> None:
    a_id, _ = await oauth_org(api_env, "oauth-a")
    b_id = await api_env.create_org("oauth-b")
    sub = await api_env.create_user(a_id, "org-admin")
    app = await seed_client(api_env, a_id)
    await _grant(api_env, a_id, await user_id_of(api_env, sub), app)
    models = (OAuthClient, OAuthCode, OAuthRefreshFamily, OAuthRefreshToken)
    async with oauth_session(a_id, api_env.app_engine) as session:
        seen = [len((await session.execute(select(m))).scalars().all()) for m in models]
    assert seen == [1, 1, 1, 1]
    async with oauth_session(b_id, api_env.app_engine) as session:
        seen = [len((await session.execute(select(m))).scalars().all()) for m in models]
    assert seen == [0, 0, 0, 0]


async def test_a_technicians_grant_is_a_global_row(api_env) -> None:
    org_id, _ = await oauth_org(api_env)
    sub = await api_env.create_user(org_id, "org-admin")
    user_id = await user_id_of(api_env, sub)
    app = await seed_client(api_env, org_id)
    await _grant(api_env, org_id, user_id, app)
    async with oauth_session(org_id, api_env.app_engine) as session:
        family = (await session.execute(select(OAuthRefreshFamily))).scalar_one()
    assert family.client_id is None  # its_client_from_parent copied the technician's NULL
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        client = Client(org_id=org_id, name="X", kind="customer")
        session.add(client)
        await session.commit()
    now = datetime.now(UTC)
    with pytest.raises(GlobalRowNotWritable):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.only([client.id])
        ) as session:
            session.add(
                OAuthRefreshFamily(
                    org_id=org_id,
                    user_id=user_id,
                    oauth_client_id=app.id,
                    resource=RESOURCE,
                    created_at=now,
                    last_used_at=now,
                    expires_at=now + timedelta(days=30),
                )
            )


async def test_a_code_is_stored_as_a_32_byte_digest(api_env) -> None:
    org_id, _ = await oauth_org(api_env)
    sub = await api_env.create_user(org_id, "org-admin")
    app = await seed_client(api_env, org_id)
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError):
        async with oauth_session(org_id, api_env.app_engine) as session:
            session.add(
                OAuthCode(
                    org_id=org_id,
                    user_id=await user_id_of(api_env, sub),
                    oauth_client_id=app.id,
                    code_hash=b"the-code-itself",
                    redirect_uri=CLAUDE_CALLBACK,
                    code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
                    resource=RESOURCE,
                    created_at=now,
                    expires_at=now + timedelta(seconds=60),
                )
            )


async def test_a_grant_names_an_app_of_its_own_organization(api_env) -> None:
    a_id, _ = await oauth_org(api_env, "oauth-a")
    b_id = await api_env.create_org("oauth-b")
    sub = await api_env.create_user(a_id, "org-admin")
    other_app = await seed_client(api_env, b_id)
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError):
        # The admin engine skips RLS, as a foreign key check does: only the
        # composite key stands between org A's grant and org B's app.
        async with AsyncSession(api_env.admin_engine) as session:
            session.add(
                OAuthRefreshFamily(
                    org_id=a_id,
                    user_id=await user_id_of(api_env, sub),
                    oauth_client_id=other_app.id,
                    resource=RESOURCE,
                    created_at=now,
                    last_used_at=now,
                    expires_at=now + timedelta(days=30),
                )
            )
            await session.commit()


async def test_a_registration_unused_for_30_days_is_not_found(api_env) -> None:
    org_id, _ = await oauth_org(api_env)
    now = datetime.now(UTC)
    fresh = await seed_client(api_env, org_id)
    idle = await seed_client(api_env, org_id, last_used_at=now - timedelta(days=30, minutes=1))
    async with oauth_session(org_id, api_env.app_engine) as session:
        found = await clients.find_client(session, org_id=org_id, client_id=fresh.client_id, now=now)
        gone = await clients.find_client(session, org_id=org_id, client_id=idle.client_id, now=now)
        assert found is not None and gone is None
        clients.touch(found, now + timedelta(days=29))
    async with oauth_session(org_id, api_env.app_engine) as session:
        again = await clients.find_client(
            session, org_id=org_id, client_id=fresh.client_id, now=now + timedelta(days=58)
        )
    assert again is not None
    view = clients.view(again)
    assert (view.id, view.client_id, view.kind) == (fresh.id, fresh.client_id, "registered")
    assert view.redirect_uris == (CLAUDE_CALLBACK,)


async def test_the_deployment_org_is_the_default_slugs(api_env) -> None:
    org_id, _ = await oauth_org(api_env)
    assert await deployment_org(api_env.app) == org_id
    api_env.settings.default_org_slug = None
    assert await deployment_org(api_env.app) is None
    api_env.settings.default_org_slug = f"no-such-{uuid4().hex[:6]}"
    assert await deployment_org(api_env.app) is None
```

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_schema.py`
Expected: FAIL, `ModuleNotFoundError: No module named 'app.core.auth.oauth.store'`.

- [ ] **Step 2: La migración**

`backend/migrations/versions/0247_oauth_server.py`:

```python
"""Own authentication, phase 3: the OAuth server's tables
(`docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §5, §6.1;
plan 2026-11-06, Task 5).

FOUR TABLES. `oauth_clients` holds the AI apps this organization knows: each
one that registered itself (RFC 7591, `kind = 'registered'`, a client id the
platform made) and each client metadata document read (`kind =
'metadata_document'`, whose URL is the client id, cached until
`metadata_expires_at`). It is the organization's (G): no person, no client.
`oauth_refresh_families` holds one grant of one person to one app (design
§5.3: reusing a refresh token "revoca toda la familia"); `oauth_refresh_tokens`
every refresh token a family issued, so a used one is recognised when it comes
back; `oauth_codes` the one-time authorization codes. Those three are a
person's (O under `users` through `user_id`), with 0245's policies and
triggers verbatim, so a technician's rows are global rows.

`client_id` IN THE THREE O TABLES IS THE MSP CLIENT the trigger copies; the AI
app is `oauth_client_id`. Only `oauth_clients.client_id` is an OAuth client id,
as in `mcp_revocation` (both G).

A GRANT NAMES AN APP OF ITS OWN ORGANIZATION: `(oauth_client_id, org_id) ->
oauth_clients (id, org_id)`, because a foreign key ignores RLS (the 0231
reason for `fk_*_client_org`). An app's grants and codes go with it (ON
DELETE CASCADE): an expired registration takes its grants along (plan D10).

NO SECRET IN CLEAR. A code and a refresh token are stored as the SHA-256 of a
random 256-bit value (`code_hash`, `token_hash`, CHECKs of 32 bytes); the
access token is never stored.

The table and column names are written here, not imported: a migration that
imports application code runs against whatever that code has become.

Forward-only. `downgrade()` has never run in production (rollback is
restore-from-backup). CI runs upgrade->downgrade->upgrade.

Revision ID: 0247
Revises: 0246
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0247"
down_revision: str | None = "0246"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"

# 0231's client predicates, copied: a migration imports nothing.
VISIBLE = "its_client_visible(client_id)"
SCOPE = "current_setting('app.client_scope', true)"
SCOPE_PINNED = (
    f"({SCOPE} = 'all' OR ({SCOPE} = 'list' AND "
    "nullif(current_setting('app.current_clients', true), '') IS NOT NULL))"
)
NULL_PINNED = f"(client_id IS NULL AND {SCOPE_PINNED})"
NULL_ALL = f"(client_id IS NULL AND {SCOPE} = 'all')"

#: A person's tables, in creation order. Each is O under `users` through `user_id`.
PERSON_TABLES = ("oauth_refresh_families", "oauth_refresh_tokens", "oauth_codes")

#: Why a family stopped (plan Task 5, decision 3).
REVOKE_REASONS = (
    "refresh_reused",
    "code_reused",
    "revoked_by_app",
    "revoked_in_settings",
    "signed_out_everywhere",
    "password_changed",
    "factors_reset",
    "disabled",
    "deleted",
    "person_inactive",
)


def _uuid(name: str, *args: object, nullable: bool = False, **kw: object) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), *args, nullable=nullable, **kw)


def _when(name: str, *, nullable: bool = False, now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if now else None,
    )


def _owner() -> list[sa.Column]:
    """`org_id`, `client_id` (the trigger's) and `user_id`: whose row it is."""
    return [
        _uuid("org_id", sa.ForeignKey("organizations.id")),
        _uuid("client_id", sa.ForeignKey("clients.id"), nullable=True),
        _uuid("user_id", sa.ForeignKey("users.id", ondelete="CASCADE")),
    ]


def _app_of_the_org(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["oauth_client_id", "org_id"],
        ["oauth_clients.id", "oauth_clients.org_id"],
        name=f"fk_{table}_app_org",
        ondelete="CASCADE",
    )


def _tenant(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
    )


def _person_scope(table: str) -> None:
    """O under `users` (0231, as 0245 applies it): index, org-consistency key,
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
        "oauth_clients",
        _uuid("id", primary_key=True),
        _uuid("org_id", sa.ForeignKey("organizations.id")),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("client_name", sa.Text(), nullable=True),
        sa.Column("redirect_uris", postgresql.ARRAY(sa.Text()), nullable=False),
        _when("created_at", now=True),
        _when("last_used_at", now=True),
        _when("metadata_expires_at", nullable=True),
        sa.UniqueConstraint("org_id", "client_id", name="uq_oauth_clients_org_client"),
        # The key the grants' `_app_org` foreign keys reference.
        sa.UniqueConstraint("id", "org_id", name="uq_oauth_clients_id_org"),
        sa.CheckConstraint(
            "kind IN ('registered', 'metadata_document')", name="ck_oauth_clients_kind"
        ),
        sa.CheckConstraint(
            "char_length(client_id) BETWEEN 1 AND 255", name="ck_oauth_clients_client_id"
        ),
        sa.CheckConstraint(
            "client_name IS NULL OR char_length(client_name) BETWEEN 1 AND 200",
            name="ck_oauth_clients_client_name",
        ),
        sa.CheckConstraint(
            "cardinality(redirect_uris) BETWEEN 1 AND 10", name="ck_oauth_clients_redirect_uris"
        ),
        sa.CheckConstraint(
            "(kind = 'metadata_document') = (metadata_expires_at IS NOT NULL)",
            name="ck_oauth_clients_metadata",
        ),
    )
    op.create_index(
        "ix_oauth_clients_org_last_used", "oauth_clients", ["org_id", "last_used_at"]
    )
    _tenant("oauth_clients")

    reasons = ", ".join(f"'{r}'" for r in REVOKE_REASONS)
    op.create_table(
        "oauth_refresh_families",
        _uuid("id", primary_key=True),
        *_owner(),
        _uuid("oauth_client_id"),
        sa.Column("resource", sa.Text(), nullable=False),
        _when("created_at", now=True),
        _when("last_used_at", now=True),
        _when("expires_at"),
        _when("revoked_at", nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        _app_of_the_org("oauth_refresh_families"),
        # The key `fk_oauth_refresh_tokens_family_user` references: a token is
        # its own family's person's.
        sa.UniqueConstraint("id", "user_id", name="uq_oauth_refresh_families_id_user"),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_reason IS NULL)",
            name="ck_oauth_refresh_families_revoked",
        ),
        sa.CheckConstraint(
            f"revoked_reason IS NULL OR revoked_reason IN ({reasons})",
            name="ck_oauth_refresh_families_reason",
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_oauth_refresh_families_expiry"),
    )
    op.create_index(
        "ix_oauth_refresh_families_user_id", "oauth_refresh_families", ["user_id"]
    )
    op.create_index(
        "ix_oauth_refresh_families_oauth_client_id", "oauth_refresh_families", ["oauth_client_id"]
    )
    op.create_table(
        "oauth_refresh_tokens",
        _uuid("id", primary_key=True),
        *_owner(),
        _uuid("family_id"),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        _when("created_at", now=True),
        _when("used_at", nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_oauth_refresh_tokens_token_hash"),
        sa.ForeignKeyConstraint(
            ["family_id", "user_id"],
            ["oauth_refresh_families.id", "oauth_refresh_families.user_id"],
            name="fk_oauth_refresh_tokens_family_user",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32", name="ck_oauth_refresh_tokens_token_hash"
        ),
    )
    op.create_index("ix_oauth_refresh_tokens_user_id", "oauth_refresh_tokens", ["user_id"])
    op.create_index("ix_oauth_refresh_tokens_family_id", "oauth_refresh_tokens", ["family_id"])
    op.create_table(
        "oauth_codes",
        _uuid("id", primary_key=True),
        *_owner(),
        _uuid("oauth_client_id"),
        sa.Column("code_hash", sa.LargeBinary(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("code_challenge", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        _when("created_at", now=True),
        _when("expires_at"),
        _when("used_at", nullable=True),
        _uuid(
            "family_id",
            sa.ForeignKey("oauth_refresh_families.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _app_of_the_org("oauth_codes"),
        sa.UniqueConstraint("code_hash", name="uq_oauth_codes_code_hash"),
        sa.CheckConstraint("octet_length(code_hash) = 32", name="ck_oauth_codes_code_hash"),
        sa.CheckConstraint(
            "char_length(code_challenge) = 43", name="ck_oauth_codes_code_challenge"
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_oauth_codes_expiry"),
    )
    op.create_index("ix_oauth_codes_user_id", "oauth_codes", ["user_id"])
    for table in PERSON_TABLES:
        _person_scope(table)


def downgrade() -> None:
    for table in reversed(PERSON_TABLES):
        # The trigger lives on `users`; dropping the child leaves it behind
        # (0245's reason).
        op.execute(f"DROP TRIGGER IF EXISTS its_client_to_{table} ON users")
    op.drop_table("oauth_codes")
    op.drop_table("oauth_refresh_tokens")
    op.drop_table("oauth_refresh_families")
    op.drop_table("oauth_clients")
```

- [ ] **Step 3: Los modelos**

`backend/app/models/oauth.py`:

```python
"""The OAuth server's tables (migration 0247; autenticación propia design §5,
§6.1).

`OAuthClient` is an organization's AI app (G); `OAuthRefreshFamily`,
`OAuthRefreshToken` and `OAuthCode` are a person's (O under `users` through
`user_id`): `client_id` on those three is the MSP client the trigger copies
from `users`, never sent; the AI app is `oauth_client_id`. Only
`OAuthClient.client_id` is an OAuth client id.

NOTHING HERE HOLDS A SECRET IN CLEAR: `code_hash` and `token_hash` are the
SHA-256 of a random 256-bit value (`app.core.auth.oauth.grants.hash_secret`);
the access token is never stored.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    FetchedValue,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, client_org_fk, uuid7_pk

#: `oauth_clients.kind`: registered (RFC 7591) or a client metadata document.
CLIENT_KINDS = ("registered", "metadata_document")
#: `oauth_refresh_families.revoked_reason` (migration 0247's CHECK).
REVOKE_REASONS = (
    "refresh_reused",
    "code_reused",
    "revoked_by_app",
    "revoked_in_settings",
    "signed_out_everywhere",
    "password_changed",
    "factors_reset",
    "disabled",
    "deleted",
    "person_inactive",
)


def _client_id() -> Mapped[UUID | None]:
    """The trigger's: copied from `users.client_id`, never sent
    (`app.models.auth._client_id`, the same column)."""
    return mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        server_onupdate=FetchedValue(),
    )


def _app_of_the_org(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["oauth_client_id", "org_id"],
        ["oauth_clients.id", "oauth_clients.org_id"],
        name=f"fk_{table}_app_org",
        ondelete="CASCADE",
    )


class OAuthClient(Base):
    """An AI app this organization knows (design §5.4, §5.5)."""

    __tablename__ = "oauth_clients"
    __table_args__ = (
        UniqueConstraint("org_id", "client_id", name="uq_oauth_clients_org_client"),
        UniqueConstraint("id", "org_id", name="uq_oauth_clients_id_org"),
        CheckConstraint("kind IN ('registered', 'metadata_document')", name="ck_oauth_clients_kind"),
        CheckConstraint(
            "char_length(client_id) BETWEEN 1 AND 255", name="ck_oauth_clients_client_id"
        ),
        CheckConstraint(
            "client_name IS NULL OR char_length(client_name) BETWEEN 1 AND 200",
            name="ck_oauth_clients_client_name",
        ),
        CheckConstraint(
            "cardinality(redirect_uris) BETWEEN 1 AND 10", name="ck_oauth_clients_redirect_uris"
        ),
        CheckConstraint(
            "(kind = 'metadata_document') = (metadata_expires_at IS NOT NULL)",
            name="ck_oauth_clients_metadata",
        ),
        Index("ix_oauth_clients_org_last_used", "org_id", "last_used_at"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    client_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    client_name: Mapped[str | None] = mapped_column(Text, default=None)
    redirect_uris: Mapped[list[str]] = mapped_column(ARRAY(Text))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(server_default=func.now())
    metadata_expires_at: Mapped[datetime | None] = mapped_column(default=None)


class OAuthRefreshFamily(Base):
    """One person's grant to one AI app: its refresh tokens rotate inside it,
    and reusing one ends it (design §5.3)."""

    __tablename__ = "oauth_refresh_families"
    __table_args__ = (
        client_org_fk("oauth_refresh_families"),
        _app_of_the_org("oauth_refresh_families"),
        UniqueConstraint("id", "user_id", name="uq_oauth_refresh_families_id_user"),
        CheckConstraint(
            "(revoked_at IS NULL) = (revoked_reason IS NULL)",
            name="ck_oauth_refresh_families_revoked",
        ),
        CheckConstraint(
            "revoked_reason IS NULL OR revoked_reason IN ("
            + ", ".join(f"'{r}'" for r in REVOKE_REASONS)
            + ")",
            name="ck_oauth_refresh_families_reason",
        ),
        CheckConstraint("expires_at > created_at", name="ck_oauth_refresh_families_expiry"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    client_id: Mapped[UUID | None] = _client_id()
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    oauth_client_id: Mapped[UUID] = mapped_column(index=True)
    resource: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column()
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    revoked_reason: Mapped[str | None] = mapped_column(Text, default=None)


class OAuthRefreshToken(Base):
    """A refresh token a family issued, as its hash. `used_at` set: rotated,
    and presenting it again ends the family."""

    __tablename__ = "oauth_refresh_tokens"
    __table_args__ = (
        client_org_fk("oauth_refresh_tokens"),
        UniqueConstraint("token_hash", name="uq_oauth_refresh_tokens_token_hash"),
        ForeignKeyConstraint(
            ["family_id", "user_id"],
            ["oauth_refresh_families.id", "oauth_refresh_families.user_id"],
            name="fk_oauth_refresh_tokens_family_user",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "octet_length(token_hash) = 32", name="ck_oauth_refresh_tokens_token_hash"
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    client_id: Mapped[UUID | None] = _client_id()
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    family_id: Mapped[UUID] = mapped_column(index=True)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    used_at: Mapped[datetime | None] = mapped_column(default=None)


class OAuthCode(Base):
    """A one-time authorization code (design §5.2: 60 s), as its hash, bound to
    the app, the exact redirect it was asked for and the PKCE challenge."""

    __tablename__ = "oauth_codes"
    __table_args__ = (
        client_org_fk("oauth_codes"),
        _app_of_the_org("oauth_codes"),
        UniqueConstraint("code_hash", name="uq_oauth_codes_code_hash"),
        CheckConstraint("octet_length(code_hash) = 32", name="ck_oauth_codes_code_hash"),
        CheckConstraint(
            "char_length(code_challenge) = 43", name="ck_oauth_codes_code_challenge"
        ),
        CheckConstraint("expires_at > created_at", name="ck_oauth_codes_expiry"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    client_id: Mapped[UUID | None] = _client_id()
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    oauth_client_id: Mapped[UUID] = mapped_column()
    code_hash: Mapped[bytes] = mapped_column(LargeBinary)
    redirect_uri: Mapped[str] = mapped_column(Text)
    code_challenge: Mapped[str] = mapped_column(Text)
    resource: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column()
    used_at: Mapped[datetime | None] = mapped_column(default=None)
    family_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("oauth_refresh_families.id", ondelete="SET NULL"), default=None
    )
```

En `backend/app/models/__init__.py`, después del bloque `from app.models.auth import (...)`:

```python
from app.models.oauth import OAuthClient, OAuthCode, OAuthRefreshFamily, OAuthRefreshToken
```

y los cuatro nombres en `__all__`, en orden alfabético junto a los demás.

- [ ] **Step 4: Catálogo, lista revisada y claves sustitutas**

En `backend/app/core/client_scope_catalog.py`, después de `"account_mail_settings": G,`:

```python
    # own authentication phase 3 (migration 0247): the AI apps an organization
    # knows are the org's (G: `client_id` there is the OAuth client id); a
    # person's grants, codes and refresh tokens follow their `users` row.
    "oauth_clients": G, "oauth_codes": O("users", "user_id"),
    "oauth_refresh_families": O("users", "user_id"),
    "oauth_refresh_tokens": O("users", "user_id"),
```

y en el docstring del módulo, «an OAuth client on `mcp_revocation` and `email_oauth_app`» pasa a «an OAuth client on
`mcp_revocation`, `oauth_clients` and `email_oauth_app`».

En `backend/app/core/client_scope.py`, en `ALL_SCOPE_ALLOWLIST`, después de la entrada de `recover.py`:

```python
    "app/core/auth/oauth/store.py::oauth_session": (
        "the platform's own OAuth server (P2 phase 3 design §5): an AI app's registration (G) "
        "and a technician's grants, codes and refresh tokens -- global rows only an every-client "
        "scope may write, read by a code or token hash before anybody's scope exists, or by "
        "the id of the person the console session or the verified access token named. Also "
        "holds Settings -> AI connections' revoke (Task 10): the caller's own grants, or with "
        "orgs.manage on every client everyone's"
    ),
```

En `backend/tests/test_client_catalog.py`, en `SURROGATE_COLUMNS`, después de `("auth_passkeys", "credential_id")`:

```python
    # Own authentication phase 3 (migration 0247): the SHA-256 of a random
    # 256-bit value the AI app holds, found by it before anybody's scope exists.
    ("oauth_codes", "code_hash"): "the SHA-256 of a random 256-bit authorization code",
    ("oauth_refresh_tokens", "token_hash"): "the SHA-256 of a random 256-bit refresh token",
```

- [ ] **Step 5: `store.py` y `clients.py`**

`backend/app/core/auth/oauth/store.py`:

```python
"""Where the OAuth server's rows are read and written (design §5, §6.1-§6.2;
plan D3).

THE ORGANIZATION OF A REQUEST WITH NO PERSON IS THE DEPLOYMENT'S. A
registration, a token request and a revocation arrive with no cookie and no
person; the only organization they can belong to is the one this
installation signs people in to, `ITS_DEFAULT_ORG_SLUG` -- the rule
`flows.login` follows (phase 1). Its id is read by slug on the admin engine:
the read-only bootstrap query of `organizations` that `deps.admit` made for
Keycloak's token until this phase (Task 9 removes it there), and nothing
else. A slug that names no organization is None, and the routes answer 503.

EVERY CLIENT, AND ONLY HERE. A technician's grant rows are global rows (O
under `users`, client NULL) that only an every-client scope may write
(migration 0231), and they are read by a hash before anybody's scope exists.
`oauth_session` is the one function that opens that scope over them
(`ALL_SCOPE_ALLOWLIST`); a route file never does.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Organization


async def deployment_org(app: Any) -> UUID | None:
    slug = app.state.settings.default_org_slug
    if not slug:
        return None
    async with AsyncSession(app.state.admin_engine) as session:
        found: UUID | None = (
            await session.execute(select(Organization.id).where(Organization.slug == slug))
        ).scalar_one_or_none()
    return found


@asynccontextmanager
async def oauth_session(org_id: UUID, engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """One transaction in `org_id` over every client (module docstring)."""
    async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
        yield session
```

`backend/app/core/auth/oauth/clients.py`:

```python
"""The AI apps an organization knows (design §5.4, §5.5).

`find_client` is the one lookup: an app this organization has, used in the
last `CLIENT_IDLE` (§5.4: "Un registro sin uso en 30 días caduca"). An idle
one is not found, whatever its row says, and the next registration removes it
(`purge_idle`, Task 6; plan D10). `touch` is every use: an authorization, a
token request.

`ClientView` is what leaves a session: a plain copy, so nothing holds an ORM
object across transactions.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.oauth.policy import CLIENT_IDLE
from app.models.oauth import OAuthClient


@dataclass(frozen=True)
class ClientView:
    id: UUID
    client_id: str
    kind: str
    client_name: str | None
    redirect_uris: tuple[str, ...]


def view(row: OAuthClient) -> ClientView:
    return ClientView(
        id=row.id,
        client_id=row.client_id,
        kind=row.kind,
        client_name=row.client_name,
        redirect_uris=tuple(row.redirect_uris),
    )


async def find_client(
    session: AsyncSession, *, org_id: UUID, client_id: str, now: datetime
) -> OAuthClient | None:
    row = (
        await session.execute(
            select(OAuthClient).where(
                OAuthClient.org_id == org_id, OAuthClient.client_id == client_id
            )
        )
    ).scalar_one_or_none()
    if row is None or row.last_used_at <= now - CLIENT_IDLE:
        return None
    return row


def touch(row: OAuthClient, now: datetime) -> None:
    row.last_used_at = now
```

- [ ] **Step 6: Las pruebas pasan**

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_schema.py tests/test_client_catalog.py tests/test_migrations.py`
Expected: PASS (incluidas `test_every_table_is_classified`, `test_scoped_tables_carry_the_restrictive_policy`,
`test_a_parents_client_change_reaches_every_child`, `test_natural_keys_on_per_client_tables_are_per_client`,
`test_client_scope_all_only_in_reviewed_functions` y `test_no_index_in_the_database_is_unknown_to_the_models`).

Run (el ciclo de la CI, contra una base vacía): `cd backend && .venv/bin/alembic upgrade head && .venv/bin/alembic downgrade base && .venv/bin/alembic upgrade head`
Expected: tres veces sin error. (Si la worktree no tiene un Postgres de trabajo configurado para `alembic`, esta
orden la corre el coordinador; las pruebas de arriba ya migran a `head` en su clúster.)

Run: `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `Success: no issues found` y `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/migrations/versions/0247_oauth_server.py backend/app/models/oauth.py backend/app/models/__init__.py \
  backend/app/core/client_scope_catalog.py backend/app/core/client_scope.py backend/app/core/auth/oauth/store.py \
  backend/app/core/auth/oauth/clients.py backend/tests/test_client_catalog.py backend/tests/oauth_support.py \
  backend/tests/test_oauth_schema.py
git commit -m "feat(oauth): migration 0247: AI apps, codes and refresh families under RLS"
```

### Task 6: Registro (DCR), clientes CIMD, metadatos RFC 8414 y la ruta de Traefik

Diseño §5.1, §5.4, §5.5; D1, D3, D4, D9, D10, D15. Oleada 3, en paralelo con la Task 7. Backend y `deploy/`.

Lo que un cliente de IA ve antes de que haya una persona: los metadatos del servidor
(`/.well-known/oauth-authorization-server`), el registro abierto (`POST /oauth/register`) y, para un cliente CIMD, la
lectura de su documento cuando alguien inicia su autorización (`clients.resolve_client`, que la Task 8 llama). Y el RFC
9728 del servidor MCP pasa a nombrar a la plataforma como servidor de autorización (§5.1: «El servidor MCP sigue
publicando RFC 9728, apuntando a la plataforma»).

**Files:**
- Modify: `backend/app/core/config.py` (`mcp_registration`, junto a `public_console_url`)
- Modify: `backend/app/core/auth/oauth/clients.py` (de la Task 5: funciones nuevas al final)
- Create: `backend/app/api/oauth_registration.py`
- Modify: `backend/app/main.py` (incluir el router, junto a `well_known_router`)
- Modify: `backend/app/mcp/server.py` (`protected_resource`, `:412-429`)
- Modify: `backend/app/core/i18n/es.py` (una entrada, junto a `"MCP is not available: ITS_PUBLIC_CONSOLE_URL is not set."`)
- Modify: `deploy/docker-compose.yml` (la regla y el comentario del router `api`, `:126-139`)
- Modify: `backend/tests/oauth_support.py` (`register`, `forget_registration_limit` al final)
- Modify: `backend/tests/test_deploy_mcp_route.py`, `backend/tests/test_mcp_discovery_fallbacks.py`,
  `backend/tests/test_mcp_server.py` (`test_the_protected_resource_metadata_is_served_at_both_paths`, `:140-153`)
- Test: `backend/tests/test_oauth_registration.py`

**Interfaces:**
- Consumes: Task 1 (`policy`, `errors`, `redirects`), Task 3 (`cimd.fetch_document`, `cimd.is_document_client_id`,
  `cimd.DocumentRefused`, `ClientDocument`), Task 5 (`OAuthClient`, `store.deployment_org`, `store.oauth_session`,
  `clients.find_client`, `clients.view`, `clients.touch`, `ClientView`, `tests/oauth_support.py`). Existentes:
  `app.core.rate_limit.hit(redis, key, *, limit, window_s, fail_closed=False) -> bool` e `ip_key(request, bucket)`
  (`backend/app/core/rate_limit.py`), `app.core.http.read_bounded_body(request, max_bytes)`,
  `app.core.mcp_channel.mcp_resource`, `uuid_extensions.uuid7`.
- Produces: `Settings.mcp_registration: str`; `clients.Registration`, `parse_registration`, `register`, `purge_idle`,
  `PURGE_BATCH = 100`, `registration_response`, `resolve_client(app, *, org_id, client_id, now, fetch=None)` (Apéndice
  A.1; `fetch` es `cimd.fetch_document` por defecto, leído al llamar); las rutas `GET METADATA_PATH` y
  `POST REGISTER_PATH`; en `tests/oauth_support.py`, `async def register(env, **metadata) -> dict` y
  `async def forget_registration_limit(env) -> None`.

**Decisión de la tarea (1): un CIMD solo se lee con el registro abierto** (D15, pregunta P2). `resolve_client` no
descarga nada si `registration_open(settings)` es falso, y los metadatos no anuncian `client_id_metadata_document_
supported`. Un cliente CIMD ya guardado y en caché sigue sirviendo, como un registro ya hecho: cerrar el registro corta
a los nuevos, no a los que ya están (lo mismo que dice `deploy/README.md` de Keycloak hoy).

**Decisión de la tarea (2): el límite por dirección falla cerrado.** Sin Redis no hay ningún otro tope sobre el
registro anónimo (a diferencia del inicio de sesión, que tiene los contadores por cuenta), así que `fail_closed=True`,
como el tope de correos de restablecimiento (`rate_limit.py`).

**Pruebas existentes que cambian a propósito.** `tests/test_mcp_discovery_fallbacks.py`: `/.well-known/oauth-
authorization-server` deja de ser un 404 de reserva y pasa a ser el documento; la prueba pasa a afirmar las dos cosas
(las otras tres rutas siguen siendo un 404 JSON, la de la raíz es el documento). `tests/test_mcp_server.py::
test_the_protected_resource_metadata_is_served_at_both_paths`: `authorization_servers` pasa de
`settings.oidc_issuer` a la dirección de la consola.

- [ ] **Step 1: Las pruebas que fallan**

Al final de `backend/tests/oauth_support.py`:

```python
#: `oauth_registration`'s per-address bucket (`rate_limit.ip_key`): every
#: in-process request comes from 127.0.0.1, so one file's registrations would
#: spend the next file's hour.
REGISTER_BUCKET = "ratelimit:oauth-register:ip:*"


async def forget_registration_limit(env: Any) -> None:
    redis = env.app.state.redis
    async for key in redis.scan_iter(REGISTER_BUCKET):
        await redis.delete(key)


async def register(env: Any, **metadata: Any) -> dict[str, Any]:
    """`POST /oauth/register` as an AI app sends it; the 201's body."""
    env.settings.mcp_registration = "open"
    await forget_registration_limit(env)
    body = {"redirect_uris": [CLAUDE_CALLBACK], "client_name": "Claude", **metadata}
    async with oauth_http(env) as client:
        resp = await client.post("/oauth/register", json=body)
    assert resp.status_code == 201, resp.text
    answer: dict[str, Any] = resp.json()
    return answer
```

`backend/tests/test_oauth_registration.py`:

```python
"""The OAuth server before there is a person: its metadata (RFC 8414), open
registration (RFC 7591) and client metadata documents (autenticación propia
design §5.1, §5.4, §5.5; plan 2026-11-06, Task 6).
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

import app.api.oauth_registration as registration_routes
from app.core.auth.oauth import cimd, clients
from app.core.auth.oauth.store import oauth_session
from app.models.oauth import OAuthClient
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


async def test_closed_registration_advertises_neither_way_in(env) -> None:
    env.settings.mcp_registration = ""
    body = (await _metadata(env)).json()
    assert "registration_endpoint" not in body
    assert "client_id_metadata_document_supported" not in body
    assert body["token_endpoint_auth_methods_supported"] == ["none"]


@pytest.mark.parametrize("url", [None, "http://itms.example.com", "https://itms.example.com/console"])
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
    answer = await register(env, token_endpoint_auth_method="client_secret_basic", scope="openid mcp")
    assert answer["client_id"].startswith("itms_")
    assert len(answer["client_id"]) == len("itms_") + 32
    # RFC 7591 §3.2.1: "MAY reject or replace" -- replaced, and said (plan D4).
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
            await session.execute(select(OAuthClient).where(OAuthClient.client_id == answer["client_id"]))
        ).scalar_one()
    assert (row.kind, row.metadata_expires_at) == ("registered", None)


async def _org(env) -> Any:
    from app.core.auth.oauth.store import deployment_org

    return await deployment_org(env.app)


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
        ({"redirect_uris": [CLAUDE_CALLBACK], "grant_types": ["client_credentials"]}, "invalid_client_metadata"),
        ({"redirect_uris": [CLAUDE_CALLBACK], "grant_types": ["refresh_token"]}, "invalid_client_metadata"),
        ({"redirect_uris": [CLAUDE_CALLBACK], "grant_types": ["authorization_code", "password"]}, "invalid_client_metadata"),
        ({"redirect_uris": [CLAUDE_CALLBACK], "response_types": ["token"]}, "invalid_client_metadata"),
        ({"redirect_uris": [CLAUDE_CALLBACK], "client_name": 7}, "invalid_client_metadata"),
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


async def test_unknown_metadata_is_ignored_and_a_name_is_cleaned(env) -> None:
    answer = await register(env, client_name="  Claude\u0000\n Code ", logo_uri="https://x.example/l.png")
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


async def test_one_address_may_register_only_so_often(env, monkeypatch) -> None:
    monkeypatch.setattr(registration_routes, "REGISTRATIONS_PER_HOUR", 2)
    async with oauth_http(env) as client:
        answers = [
            await client.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]})
            for _ in range(3)
        ]
    assert [a.status_code for a in answers] == [201, 201, 429]
    assert answers[2].json()["error"] == "temporarily_unavailable"
    assert answers[2].headers["retry-after"] == "3600"


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
    idle = await seed_client(env, org_id, last_used_at=datetime.now(UTC) - timedelta(days=31))
    alive = await seed_client(env, org_id)
    await register(env)
    async with oauth_session(org_id, env.app_engine) as session:
        ids = set((await session.execute(select(OAuthClient.id))).scalars())
    assert idle.id not in ids
    assert alive.id in ids
    assert len(ids) == 2


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


async def test_a_document_client_is_read_once_and_cached_by_its_headers(env) -> None:
    org_id, now = await _org(env), datetime.now(UTC)
    fetch = FakeFetch()
    first = await clients.resolve_client(env.app, org_id=org_id, client_id=DOCUMENT_URL, now=now, fetch=fetch)
    again = await clients.resolve_client(
        env.app, org_id=org_id, client_id=DOCUMENT_URL, now=now + timedelta(minutes=59), fetch=fetch
    )
    assert fetch.calls == 1
    assert first is not None and again is not None
    assert first.id == again.id
    assert (first.kind, first.client_name, first.redirect_uris) == (
        "metadata_document",
        "Claude Code",
        ("http://localhost/callback",),
    )
    await clients.resolve_client(
        env.app, org_id=org_id, client_id=DOCUMENT_URL, now=now + timedelta(minutes=61), fetch=fetch
    )
    assert fetch.calls == 2


async def test_a_document_that_may_not_be_cached_is_read_every_time(env) -> None:
    org_id, now = await _org(env), datetime.now(UTC)
    fetch = FakeFetch(_document(cache_for=timedelta(0)))
    for _ in range(2):
        assert await clients.resolve_client(env.app, org_id=org_id, client_id=DOCUMENT_URL, now=now, fetch=fetch)
    assert fetch.calls == 2


async def test_a_refused_document_is_never_kept(env) -> None:
    org_id = await _org(env)
    with pytest.raises(cimd.DocumentRefused):
        await clients.resolve_client(
            env.app, org_id=org_id, client_id=DOCUMENT_URL, now=datetime.now(UTC), fetch=FakeFetch(refused="secret")
        )
    async with oauth_session(org_id, env.app_engine) as session:
        assert (await session.execute(select(OAuthClient))).scalars().all() == []


async def test_no_document_is_read_while_registration_is_closed(env) -> None:
    env.settings.mcp_registration = ""
    fetch = FakeFetch()
    found = await clients.resolve_client(
        env.app, org_id=await _org(env), client_id=DOCUMENT_URL, now=datetime.now(UTC), fetch=fetch
    )
    assert found is None
    assert fetch.calls == 0


async def test_a_registered_client_is_found_and_an_unknown_one_is_not(env) -> None:
    org_id, now = await _org(env), datetime.now(UTC)
    app = await seed_client(env, org_id)
    fetch = FakeFetch()
    found = await clients.resolve_client(env.app, org_id=org_id, client_id=app.client_id, now=now, fetch=fetch)
    unknown = await clients.resolve_client(env.app, org_id=org_id, client_id="itms_nobody", now=now, fetch=fetch)
    assert found is not None and found.id == app.id
    assert unknown is None
    assert fetch.calls == 0
```

En `backend/tests/test_deploy_mcp_route.py`, después de `MCP_PATHS`:

```python
#: The OAuth server's machine endpoints (plan 2026-11-06, Task 6): the api's,
#: by exact path. `/oauth/authorize` is a console page (plan D2).
OAUTH_PATHS = ("/oauth/token", "/oauth/register", "/oauth/revoke")
```

`MCP_PATHS` gana `"/.well-known/oauth-authorization-server"` (el documento de la plataforma, que también va a la API);
las demás entradas se quedan, la de reserva `…/realms/it-suite` incluida, porque tiene que seguir dando un 404 JSON y
no la página de la consola. Y dos pruebas nuevas al final:

```python
def test_the_oauth_endpoints_reach_the_api_by_exact_path() -> None:
    rule = str(_api()["labels"]["traefik.http.routers.api.rule"])
    for path in OAUTH_PATHS:
        assert f"Path(`{path}`)" in rule, path
        assert _winner(path) == "api", (path, _winner(path))
    # Exact paths: nothing below them, and nothing beside them, moves.
    assert _winner("/oauth/token/x") == "web"
    assert _winner("/oauth/tokens") == "web"


def test_the_consent_page_stays_the_consoles() -> None:
    assert _winner("/oauth/authorize") == "web"
    routes = ROUTES.read_text()
    for path in OAUTH_PATHS:
        assert f'"{path}"' not in routes and f"'{path}'" not in routes, path
```

`backend/tests/test_mcp_discovery_fallbacks.py` pasa a:

```python
"""The discovery fallbacks Traefik routes to the api answer 404 JSON, and the
platform's own authorization server metadata is the document.

Until own authentication phase 3, `authorization_servers` named the realm, and
an MCP client tried `/.well-known/oauth-authorization-server/realms/it-suite`
and `/.well-known/openid-configuration/realms/it-suite` before the realm's
own documents: both had to be a JSON 404, never the console's 200 page. Since
phase 3 the platform is its own issuer at the console's address (plan
2026-11-06, D1), so `/.well-known/oauth-authorization-server` at the root is
its RFC 8414 document; the other three stay a JSON 404, because a client
that tries them first must still move on rather than parse the console's
HTML. `test_deploy_mcp_route.py` holds the routing.
"""

import pytest

FALLBACKS = (
    "/.well-known/oauth-authorization-server/realms/it-suite",
    "/.well-known/openid-configuration/realms/it-suite",
    "/.well-known/openid-configuration",
)


@pytest.mark.parametrize("path", FALLBACKS)
async def test_a_discovery_fallback_is_a_json_404(api_env, path: str) -> None:
    api_env.settings.public_console_url = "https://itms.example.com"

    resp = await api_env.client.get(path)

    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {"detail": "Not Found"}


async def test_the_root_metadata_is_the_platforms_own_document(api_env) -> None:
    api_env.settings.public_console_url = "https://itms.example.com"

    resp = await api_env.client.get("/.well-known/oauth-authorization-server")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["issuer"] == "https://itms.example.com"
```

En `backend/tests/test_mcp_server.py`, `test_the_protected_resource_metadata_is_served_at_both_paths` pasa a esperar
`"authorization_servers": ["https://itms.example.com"]` y su última línea a
`assert resp.json()["authorization_servers"] == ["https://itms.example.com"]` (la plataforma, no
`settings.oidc_issuer`); el docstring de cabecera del archivo, «the metadata (RFC 9728) names the resource and the
realm», pasa a «…names the resource and the platform as its authorization server».

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_registration.py tests/test_deploy_mcp_route.py tests/test_mcp_discovery_fallbacks.py tests/test_mcp_server.py::test_the_protected_resource_metadata_is_served_at_both_paths`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.api.oauth_registration'`, y las de Traefik y RFC 9728).

- [ ] **Step 2: El ajuste**

En `backend/app/core/config.py`, después de `public_console_url`:

```python
    # The operator's opt-in to AI apps connecting themselves (deploy/README.md
    # "AI connections (MCP)"): only the exact word `open` lets an app register
    # (RFC 7591) or name itself by a client metadata document. Read since own
    # authentication phase 3 by the platform's own authorization server
    # (`app/core/auth/oauth/policy.py::registration_open`); keycloak-init reads
    # the same `.env` line until phase 4 retires it. Unset, nobody new gets in;
    # apps already known keep working, as before.
    mcp_registration: str = ""
```

- [ ] **Step 3: El registro y los clientes CIMD**

Al final de `backend/app/core/auth/oauth/clients.py` (y sus imports pasan a los del bloque de abajo):

```python
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.core.auth.oauth import cimd
from app.core.auth.oauth.errors import (
    BAD_GRANT_TYPES,
    BAD_NAME,
    BAD_REDIRECT,
    BAD_RESPONSE_TYPES,
    NO_REDIRECTS,
    NOT_JSON,
    OAuthError,
)
from app.core.auth.oauth.policy import (
    CLIENT_IDLE,
    GRANT_TYPES,
    MAX_REDIRECT_URIS,
    SCOPE,
    display_name,
    registration_open,
)
from app.core.auth.oauth.redirects import RedirectRejected, check_registered
from app.core.auth.oauth.store import oauth_session
from app.models.oauth import OAuthClient
```

```python
#: How many idle registrations one registration removes (plan D10).
PURGE_BATCH = 100


@dataclass(frozen=True)
class Registration:
    client_name: str | None
    redirect_uris: tuple[str, ...]


def parse_registration(body: object) -> Registration:
    """A registration request this server can honour (plan D4, D9), or the
    RFC 7591 §3.2.2 error. What it does not understand is ignored (§2)."""
    if not isinstance(body, dict):
        raise OAuthError(NOT_JSON)
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not 1 <= len(uris) <= MAX_REDIRECT_URIS:
        raise OAuthError(NO_REDIRECTS)
    try:
        checked = tuple(dict.fromkeys(check_registered(uri) for uri in uris))
    except RedirectRejected as exc:
        raise OAuthError(BAD_REDIRECT) from exc
    grants = body.get("grant_types", list(GRANT_TYPES))
    if (
        not isinstance(grants, list)
        or "authorization_code" not in grants
        or not set(grants) <= set(GRANT_TYPES)
    ):
        raise OAuthError(BAD_GRANT_TYPES)
    responses = body.get("response_types", ["code"])
    if not isinstance(responses, list) or set(responses) != {"code"}:
        raise OAuthError(BAD_RESPONSE_TYPES)
    name = body.get("client_name")
    if name is not None and not isinstance(name, str):
        raise OAuthError(BAD_NAME)
    return Registration(
        client_name=display_name(name) if name else None, redirect_uris=checked
    )


async def purge_idle(session: AsyncSession, *, org_id: UUID, now: datetime) -> int:
    """Remove up to `PURGE_BATCH` registrations unused for `CLIENT_IDLE`; their
    grants and codes go with them (ON DELETE CASCADE)."""
    idle = (
        select(OAuthClient.id)
        .where(OAuthClient.org_id == org_id, OAuthClient.last_used_at <= now - CLIENT_IDLE)
        .limit(PURGE_BATCH)
    )
    removed = await session.execute(
        delete(OAuthClient).where(OAuthClient.id.in_(idle)).returning(OAuthClient.id)
    )
    return len(removed.all())


async def register(
    session: AsyncSession, *, org_id: UUID, registration: Registration, now: datetime
) -> OAuthClient:
    row = OAuthClient(
        org_id=org_id,
        # 24 random bytes, 32 base64url characters: no two apps share one
        # (RFC 7591 §3.2.1: "SHOULD NOT be currently valid for any other
        # registered client"), and none is an https URL, so a registered id
        # never reads as a client metadata document.
        client_id="itms_" + secrets.token_urlsafe(24),
        kind="registered",
        client_name=registration.client_name,
        redirect_uris=list(registration.redirect_uris),
        created_at=now,
        last_used_at=now,
        metadata_expires_at=None,
    )
    session.add(row)
    await session.flush()
    return row


def registration_response(row: OAuthClient) -> dict[str, Any]:
    """RFC 7591 §3.2.1: the client id and every registered value, including
    the ones this server replaced (plan D4)."""
    answer: dict[str, Any] = {
        "client_id": row.client_id,
        "client_id_issued_at": int(row.created_at.timestamp()),
        "redirect_uris": list(row.redirect_uris),
        "grant_types": list(GRANT_TYPES),
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": SCOPE,
    }
    if row.client_name:
        answer["client_name"] = row.client_name
    return answer


Fetch = Callable[[str], Awaitable[cimd.ClientDocument]]


async def resolve_client(
    app: Any,
    *,
    org_id: UUID,
    client_id: str,
    now: datetime,
    fetch: Fetch | None = None,
) -> ClientView | None:
    """The app an authorization request names, or None. A registered app is
    looked up; a client metadata document is read (outside any transaction:
    the network is not held under a database connection) when the cached
    copy has expired, and cached (CIMD-00 §4.4). Raises `cimd.DocumentRefused`
    for a document that cannot be the client, which is never stored.

    `fetch` defaults to `cimd.fetch_document`, looked up at call time so a
    test can replace it on the module."""
    engine = app.state.app_engine
    async with oauth_session(org_id, engine) as session:
        row = await find_client(session, org_id=org_id, client_id=client_id, now=now)
        fresh = row is not None and (
            row.kind == "registered"
            or (row.metadata_expires_at is not None and row.metadata_expires_at > now)
        )
        if row is not None and fresh:
            touch(row, now)
            return view(row)
    if not cimd.is_document_client_id(client_id) or not registration_open(app.state.settings):
        return None
    document = await (fetch or cimd.fetch_document)(client_id)
    async with oauth_session(org_id, engine) as session:
        upsert = pg_insert(OAuthClient).values(
            id=uuid7(),
            org_id=org_id,
            client_id=client_id,
            kind="metadata_document",
            client_name=document.client_name,
            redirect_uris=list(document.redirect_uris),
            created_at=now,
            last_used_at=now,
            metadata_expires_at=now + document.cache_for,
        )
        await session.execute(
            upsert.on_conflict_do_update(
                constraint="uq_oauth_clients_org_client",
                set_={
                    "client_name": upsert.excluded.client_name,
                    "redirect_uris": upsert.excluded.redirect_uris,
                    "last_used_at": upsert.excluded.last_used_at,
                    "metadata_expires_at": upsert.excluded.metadata_expires_at,
                },
            )
        )
        stored = (
            await session.execute(
                select(OAuthClient).where(
                    OAuthClient.org_id == org_id, OAuthClient.client_id == client_id
                )
            )
        ).scalar_one()
        return view(stored)
```

(El bloque de imports de arriba sustituye al de la Task 5, que era un subconjunto; `ClientView`, `view`, `find_client` y
`touch` siguen donde estaban. El docstring del módulo gana: «`parse_registration`, `register`, `purge_idle` and
`registration_response` are RFC 7591 (Task 6); `resolve_client` is the authorization request's lookup, a registered
app or a client metadata document, read and cached (CIMD-00 §4.4).»)

- [ ] **Step 4: Las rutas**

`backend/app/api/oauth_registration.py`:

```python
"""The OAuth server's public face before there is a person (design §5.1,
§5.4; plan 2026-11-06, Task 6):

    GET  /.well-known/oauth-authorization-server   RFC 8414 metadata
    POST /oauth/register                           RFC 7591 open registration

PUBLIC BY NECESSITY, outside `/api/v1` like `/.well-known/jwks.json`: neither
is an MCP tool nor behind the console's door, and Traefik routes both to the
api (`deploy/docker-compose.yml`). Registering grants nothing (§5.4): without
a person's consent there is no token.

THE LIMITS ON REGISTERING (§5.4): only with `ITS_MCP_REGISTRATION=open`
(plan D15), per network address (`REGISTRATIONS_PER_HOUR`, failing closed),
16 KiB of JSON, at most ten redirects, and each registration removes up to a
hundred idle ones (plan D10).

THE METADATA (RFC 8414 §2) names the platform as its own issuer (plan D1);
`none` in `token_endpoint_auth_methods_supported` and
`client_id_metadata_document_supported` are what Claude needs to use a client
metadata document instead of registering [S33]; `S256` alone in
`code_challenge_methods_supported` [S17, S30]; and RFC 9207's
`authorization_response_iss_parameter_supported`.
"""

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.auth.oauth import clients
from app.core.auth.oauth.errors import (
    NOT_JSON,
    REGISTRATION_CLOSED,
    SLOW_DOWN,
    UNAVAILABLE,
    OAuthError,
    no_store,
)
from app.core.auth.oauth.policy import (
    AUTHORIZE_PATH,
    GRANT_TYPES,
    JWKS_PATH,
    MAX_REQUEST_BYTES,
    METADATA_PATH,
    REGISTER_PATH,
    REGISTRATIONS_PER_HOUR,
    REVOKE_PATH,
    SCOPES_SUPPORTED,
    TOKEN_PATH,
    issuer,
    registration_open,
)
from app.core.auth.oauth.store import deployment_org, oauth_session
from app.core.http import read_bounded_body
from app.core.rate_limit import hit, ip_key

router = APIRouter()

#: The metadata changes only when the deployment's settings do.
CACHE_CONTROL = "public, max-age=300"


@router.get(METADATA_PATH, include_in_schema=False)
async def authorization_server_metadata(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    iss = issuer(settings)
    if iss is None:
        raise OAuthError(UNAVAILABLE)
    document: dict[str, object] = {
        "issuer": iss,
        "authorization_endpoint": iss + AUTHORIZE_PATH,
        "token_endpoint": iss + TOKEN_PATH,
        "revocation_endpoint": iss + REVOKE_PATH,
        "jwks_uri": iss + JWKS_PATH,
        "scopes_supported": list(SCOPES_SUPPORTED),
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": list(GRANT_TYPES),
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "authorization_response_iss_parameter_supported": True,
    }
    if registration_open(settings):
        document["registration_endpoint"] = iss + REGISTER_PATH
        document["client_id_metadata_document_supported"] = True
    return JSONResponse(document, headers={"Cache-Control": CACHE_CONTROL})


@router.post(REGISTER_PATH, include_in_schema=False)
async def register_client(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    if issuer(settings) is None:
        raise OAuthError(UNAVAILABLE)
    if not registration_open(settings):
        raise OAuthError(REGISTRATION_CLOSED)
    org_id = await deployment_org(request.app)
    if org_id is None:
        raise OAuthError(UNAVAILABLE)
    within = await hit(
        request.app.state.redis,
        ip_key(request, "oauth-register"),
        limit=REGISTRATIONS_PER_HOUR,
        window_s=3600,
        fail_closed=True,
    )
    if not within:
        raise OAuthError(SLOW_DOWN, headers={"Retry-After": "3600"})
    media = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media != "application/json":
        raise OAuthError(NOT_JSON)
    try:
        body = json.loads(await read_bounded_body(request, MAX_REQUEST_BYTES))
    except (UnicodeDecodeError, ValueError) as exc:
        raise OAuthError(NOT_JSON) from exc
    registration = clients.parse_registration(body)
    now = datetime.now(UTC)
    async with oauth_session(org_id, request.app.state.app_engine) as session:
        await clients.purge_idle(session, org_id=org_id, now=now)
        row = await clients.register(session, org_id=org_id, registration=registration, now=now)
        answer = clients.registration_response(row)
    return no_store(answer, status_code=201)
```

En `backend/app/main.py`, justo después de `app.include_router(well_known_router)`:

```python
    # The platform's own OAuth server (plan 2026-11-06, Task 6): its RFC 8414
    # metadata and RFC 7591 registration. Public, outside `/api/v1`.
    from app.api.oauth_registration import router as oauth_registration_router

    app.include_router(oauth_registration_router)
```

- [ ] **Step 5: RFC 9728 apunta a la plataforma**

En `backend/app/mcp/server.py`, `protected_resource` pasa a:

```python
@metadata_router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@metadata_router.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
async def protected_resource(request: Request) -> JSONResponse:
    """Public by necessity: it is how an OAuth client learns where to sign in.
    It names the resource and the platform as its authorization server (own
    authentication design §5.1: "El servidor MCP sigue publicando RFC 9728,
    apuntando a la plataforma"), both of which the 401 challenge and the
    platform's own metadata already publish."""
    settings = request.app.state.settings
    resource = mcp_resource(settings)
    if resource is None:
        raise HTTPException(503, tr("MCP is not available: ITS_PUBLIC_CONSOLE_URL is not set."))
    authority = issuer(settings)
    if authority is None:
        raise HTTPException(
            503,
            tr("MCP is not available: ITS_PUBLIC_CONSOLE_URL is not an https address without a path."),
        )
    return JSONResponse(
        {
            "resource": resource,
            "authorization_servers": [authority],
            "scopes_supported": ["mcp"],
            "bearer_methods_supported": ["header"],
        }
    )
```

con `from app.core.auth.oauth.policy import issuer` entre los imports de `app.core`. En `backend/app/core/i18n/es.py`,
después de la entrada de `"MCP is not available: ITS_PUBLIC_CONSOLE_URL is not set."`:

```python
    "MCP is not available: ITS_PUBLIC_CONSOLE_URL is not an https address without a path.": (
        "MCP no está disponible: ITS_PUBLIC_CONSOLE_URL no es una dirección https sin ruta."
    ),
```

- [ ] **Step 6: Traefik**

En `deploy/docker-compose.yml`, la regla del router `api` termina en
`… || Path(`/.well-known/jwks.json`) || Path(`/oauth/token`) || Path(`/oauth/register`) || Path(`/oauth/revoke`)`, y
el comentario que la precede gana, después del párrafo de `/.well-known/jwks.json`:

```yaml
      # The platform's own OAuth server (own authentication phase 3, plan
      # 2026-11-06 Task 6): its three machine endpoints, by exact `Path`.
      # `/.well-known/oauth-authorization-server` (above) is now its RFC 8414
      # document rather than a 404 fallback. `/oauth/authorize` is NOT here: it
      # is the console's consent page, served by `web`, because the
      # SameSite=Strict session cookie reaches the api only from a same-origin
      # fetch (plan D2).
```

- [ ] **Step 7: Las pruebas pasan**

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_registration.py tests/test_oauth_schema.py tests/test_deploy_mcp_route.py tests/test_deploy_keycloak_registration_edge.py tests/test_mcp_discovery_fallbacks.py tests/test_mcp_server.py tests/test_i18n_catalog.py tests/test_client_catalog.py`
Expected: PASS.

Run: `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `Success: no issues found` y `All checks passed!`.

- [ ] **Step 8: Commit**

```bash
git add backend/app/core/config.py backend/app/core/auth/oauth/clients.py backend/app/api/oauth_registration.py \
  backend/app/main.py backend/app/mcp/server.py backend/app/core/i18n/es.py deploy/docker-compose.yml \
  backend/tests/oauth_support.py backend/tests/test_oauth_registration.py backend/tests/test_deploy_mcp_route.py \
  backend/tests/test_mcp_discovery_fallbacks.py backend/tests/test_mcp_server.py
git commit -m "feat(oauth): metadata, open registration and client metadata documents; RFC 9728 names the platform"
```

### Task 7: Códigos y familias de refresh (`grants.py`); `/oauth/token` y `/oauth/revoke`

Diseño §5.2, §5.3; D3, D4, D6, D7, D10. Oleada 3, en paralelo con la Task 6. Backend.

El corazón del servidor: emitir un código (la Task 8 lo llama al aprobar), canjearlo por el par de tokens, rotar el
refresh, detectar su reutilización y revocar. Y las dos rutas de máquina que lo exponen.

**Files:**
- Create: `backend/app/core/auth/oauth/grants.py`
- Create: `backend/app/api/oauth_tokens.py`
- Modify: `backend/app/main.py` (incluir el router, junto al de la Task 6)
- Modify: `frontend/src/i18n/en/auditVocabulary.ts`, `frontend/src/i18n/es/auditVocabulary.ts` (tres acciones)
- Modify: `backend/tests/oauth_support.py` (`pkce_pair`, `seed_code`, `token_request` al final)
- Test: `backend/tests/test_oauth_token.py`

**Interfaces:**
- Consumes: Task 1 (`errors`: `BAD_CODE`, `BAD_REFRESH`, `BAD_SCOPE`, `BAD_TARGET`, `BAD_VERIFIER`, `CLIENT_AUTH`,
  `KEYS_UNAVAILABLE`, `MALFORMED`, `MISSING`, `NOT_FORM`, `REPEATED`, `UNAVAILABLE`, `UNKNOWN_CLIENT`,
  `UNSUPPORTED_GRANT`, `WRONG_CLIENT`, `NO_STORE`, `OAuthError`, `Problem`, `no_store`; `policy`: `CODE_TTL_S`,
  `REFRESH_IDLE`, `SCOPE`, `SCOPES_SUPPORTED`, `MAX_REQUEST_BYTES`, `TOKEN_PATH`, `REVOKE_PATH`, `issuer`; `pkce.verify`),
  Task 2 (`mint_access_token`, `verify_access_token`), Task 5 (modelos, `oauth_session`, `deployment_org`,
  `find_client`, `touch`, `tests/oauth_support.py`). Existentes: `audit_service.record(session, *, org_id, actor_type,
  actor_id, action, target_type, target_id, metadata)` (`backend/app/core/audit.py:157`), `ACTIVE_STATUS`, `STAFF_KIND`
  (`backend/app/core/identity/__init__.py:99-106`), `keys.AuthKeysUnavailable`, `read_bounded_body`, `mcp_resource`.
- Produces: todo `grants.py` del Apéndice A.1 (con `CODE_KEPT`, `FAMILY_KEPT`); las rutas `POST TOKEN_PATH` y
  `POST REVOKE_PATH`; las acciones de auditoría `mcp.code_reused`, `mcp.refresh_reused`, `mcp.grant_revoked`; en
  `tests/oauth_support.py`, `pkce_pair() -> tuple[str, str]`, `async def seed_code(env, org_id, sub, *, app,
  challenge, redirect_uri=CLAUDE_CALLBACK, now=None) -> str`, `async def token_request(env, **form) -> httpx.Response`.

**Decisión de la tarea (1): cada negativa del token es `invalid_grant`, y la misma para lo que no existe y lo que es de
otro cliente.** RFC 6749 §5.2 pone «issued to another client» dentro de `invalid_grant`; decir «es de otro cliente»
aparte confirmaría que un refresh robado es válido. Solo `/oauth/revoke` dice `WRONG_CLIENT`, porque RFC 7009 §2.1
pide informar al cliente y quien revoca ya tiene un token válido.

**Decisión de la tarea (2): un `code_verifier` equivocado no gasta el código.** Adivinar un verificador de 256 bits no
es posible, y gastarlo dejaría al cliente legítimo sin su conexión por un fallo de otro. El código sigue siendo de un
solo uso y de 60 s.

**Decisión de la tarea (3): quien rompe una familia es «system».** Una reutilización o la revocación que pide la app
(RFC 7009) las hace un tercero o la app, no la persona; la fila de auditoría lleva `actor_type="system"`, la persona
como objetivo y la app en `metadata.app`.

**Decisión de la tarea (4): OpenBao caído es 503 `temporarily_unavailable` y no gasta nada.** El token se firma dentro
de la transacción; `AuthKeysUnavailable` la deshace, y la ruta lo traduce al cuerpo de RFC 6749.

- [ ] **Step 1: Las pruebas que fallan**

Al final de `backend/tests/oauth_support.py`:

```python
def pkce_pair() -> tuple[str, str]:
    """`(verifier, S256 challenge)`; 48 random bytes, a 64-character verifier."""
    import secrets

    from app.core.auth.oauth import pkce

    verifier = secrets.token_urlsafe(48)
    return verifier, pkce.s256(verifier)


async def seed_code(
    env: Any,
    org_id: UUID,
    sub: str,
    *,
    app: OAuthClient,
    challenge: str,
    redirect_uri: str = CLAUDE_CALLBACK,
    now: datetime | None = None,
) -> str:
    """A code the person `sub` approved for `app`, as the consent route issues it."""
    from app.core.auth.oauth import grants
    from tests.session_support import user_id_of

    user_id = await user_id_of(env, sub)
    async with oauth_session(org_id, env.app_engine) as session:
        return await grants.issue_code(
            session,
            org_id=org_id,
            user_id=user_id,
            oauth_client_id=app.id,
            redirect_uri=redirect_uri,
            code_challenge=challenge,
            resource=RESOURCE,
            now=now or datetime.now(UTC),
        )


async def token_request(env: Any, **form: str) -> httpx.Response:
    """`POST /oauth/token`, form-encoded, as an AI app sends it [S33]."""
    async with oauth_http(env) as client:
        return await client.post("/oauth/token", data=form)
```

`backend/tests/test_oauth_token.py`:

```python
"""The token and revocation endpoints (autenticación propia design §5.2,
§5.3; plan 2026-11-06, Task 7).

- a code is single-use, 60 s, bound to its app, its exact redirect and its
  PKCE challenge; reused, it ends the grant it opened (RFC 6749 §4.1.2);
- a refresh token rotates on every use; a used one ends its family (design
  §5.3, falsifier 6), and so does a person who is no longer active;
- every refusal of a grant is `invalid_grant` [S33]; the request is RFC 6749's
  (form-encoded, public client, no parameter twice);
- RFC 7009 revocation ends the family, and answers 200 to a token it does not
  know;
- nothing is stored in clear, and OpenBao down spends nothing.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import keys
from app.core.auth.oauth import access, grants
from app.core.auth.oauth.store import oauth_session
from app.models import AuditEvent, User
from app.models.oauth import OAuthClient, OAuthCode, OAuthRefreshFamily, OAuthRefreshToken
from tests.oauth_support import (
    CLAUDE_CALLBACK,
    LOOPBACK_CALLBACK,
    RESOURCE,
    oauth_http,
    oauth_org,
    pkce_pair,
    seed_client,
    seed_code,
    token_request,
)
from tests.session_support import user_id_of


class Env:
    def __init__(self, api: Any, org_id: UUID, sub: str, app: OAuthClient) -> None:
        self.api, self.org_id, self.sub, self.app = api, org_id, sub, app


@pytest.fixture()
async def env(api_env) -> Env:
    org_id, _ = await oauth_org(api_env)
    sub = await api_env.create_user(org_id, "org-admin")
    app = await seed_client(api_env, org_id, redirect_uris=(CLAUDE_CALLBACK, "http://127.0.0.1/callback"))
    return Env(api_env, org_id, sub, app)


async def _code(env: Env, **kw: Any) -> tuple[str, str]:
    verifier, challenge = pkce_pair()
    code = await seed_code(env.api, env.org_id, env.sub, app=env.app, challenge=challenge, **kw)
    return code, verifier


async def _exchange(env: Env, code: str, verifier: str, **extra: str) -> Any:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CLAUDE_CALLBACK,
        "client_id": env.app.client_id,
        "code_verifier": verifier,
        **extra,
    }
    return await token_request(env.api, **form)


async def _tokens(env: Env) -> dict[str, Any]:
    code, verifier = await _code(env)
    resp = await _exchange(env, code, verifier)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _refresh(env: Env, refresh_token: str, **extra: str) -> Any:
    return await token_request(
        env.api,
        grant_type="refresh_token",
        refresh_token=refresh_token,
        client_id=env.app.client_id,
        **extra,
    )


async def _families(env: Env) -> list[OAuthRefreshFamily]:
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        return list((await session.execute(select(OAuthRefreshFamily))).scalars())


async def _events(env: Env, action: str) -> list[AuditEvent]:
    async with AsyncSession(env.api.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.org_id == env.org_id, AuditEvent.action == action)
                )
            ).scalars()
        )


# --------------------------------------------------------------------------- #
# The code
# --------------------------------------------------------------------------- #


async def test_a_code_is_exchanged_for_an_mcp_token_and_a_refresh_token(env: Env) -> None:
    code, verifier = await _code(env)
    resp = await _exchange(env, code, verifier, resource=RESOURCE)
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["pragma"] == "no-cache"
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 600
    assert body["scope"] == "mcp"
    assert len(body["refresh_token"]) == 43
    grant = await access.verify_access_token(body["access_token"], settings=env.api.settings)
    assert grant is not None
    assert grant.user_id == await user_id_of(env.api, env.sub)
    assert grant.client_id == env.app.client_id
    (family,) = await _families(env)
    assert grant.family_id == family.id
    assert family.resource == RESOURCE
    assert family.revoked_at is None
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        stored = (await session.execute(select(OAuthCode))).scalar_one()
        token_row = (await session.execute(select(OAuthRefreshToken))).scalar_one()
    assert stored.used_at is not None and stored.family_id == family.id
    # Stored as digests: neither value is in its row.
    assert stored.code_hash == grants.hash_secret(code) and code.encode() not in stored.code_hash
    assert token_row.token_hash == grants.hash_secret(body["refresh_token"])


async def test_a_reused_code_is_refused_and_ends_the_grant_it_opened(env: Env) -> None:
    code, verifier = await _code(env)
    first = await _exchange(env, code, verifier)
    again = await _exchange(env, code, verifier)
    assert first.status_code == 200
    assert again.status_code == 400
    assert again.json()["error"] == "invalid_grant"
    (family,) = await _families(env)
    assert family.revoked_reason == "code_reused"
    (event,) = await _events(env, "mcp.code_reused")
    assert event.actor_type == "system"
    assert event.meta["app"] == env.app.client_id
    # The grant's refresh token is dead too.
    assert (await _refresh(env, first.json()["refresh_token"])).json()["error"] == "invalid_grant"


async def test_a_wrong_verifier_is_refused_and_leaves_the_code_usable(env: Env) -> None:
    code, verifier = await _code(env)
    wrong, _ = pkce_pair()
    refused = await _exchange(env, code, wrong)
    assert refused.status_code == 400
    assert refused.json() == {
        "error": "invalid_grant",
        "error_description": "the code_verifier does not match the code_challenge",
    }
    assert (await _exchange(env, code, verifier)).status_code == 200


@pytest.mark.parametrize(
    "change",
    ["expired", "other-redirect", "other-port", "unknown", "other-client"],
)
async def test_a_code_outside_its_bounds_is_invalid_grant(env: Env, change: str) -> None:
    if change == "expired":
        code, verifier = await _code(env, now=datetime.now(UTC) - timedelta(seconds=61))
        resp = await _exchange(env, code, verifier)
    elif change == "other-redirect":
        code, verifier = await _code(env)
        resp = await _exchange(env, code, verifier, redirect_uri="http://127.0.0.1/callback")
    elif change == "other-port":
        code, verifier = await _code(env, redirect_uri="http://127.0.0.1:51004/callback")
        resp = await _exchange(env, code, verifier, redirect_uri="http://127.0.0.1:51005/callback")
    elif change == "unknown":
        _, verifier = await _code(env)
        resp = await _exchange(env, "c" * 43, verifier)
    else:
        other = await seed_client(env.api, env.org_id)
        code, verifier = await _code(env)
        resp = await token_request(
            env.api,
            grant_type="authorization_code",
            code=code,
            redirect_uri=CLAUDE_CALLBACK,
            client_id=other.client_id,
            code_verifier=verifier,
        )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"
    assert await _families(env) == []


async def test_a_loopback_code_is_redeemed_at_the_port_it_was_asked_for(env: Env) -> None:
    code, verifier = await _code(env, redirect_uri="http://127.0.0.1:51004/callback")
    resp = await _exchange(env, code, verifier, redirect_uri="http://127.0.0.1:51004/callback")
    assert resp.status_code == 200, resp.text


async def test_an_unknown_client_is_invalid_client(env: Env) -> None:
    code, verifier = await _code(env)
    resp = await token_request(
        env.api,
        grant_type="authorization_code",
        code=code,
        redirect_uri=CLAUDE_CALLBACK,
        client_id="itms_nobody",
        code_verifier=verifier,
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client"


async def test_another_resource_is_invalid_target(env: Env) -> None:
    code, verifier = await _code(env)
    resp = await _exchange(env, code, verifier, resource="https://itms.example.com")
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_target"


async def test_a_person_disabled_since_the_approval_gets_nothing(env: Env) -> None:
    code, verifier = await _code(env)
    async with AsyncSession(env.api.admin_engine) as session:
        await session.execute(
            update(User).where(User.id == await user_id_of(env.api, env.sub)).values(status="disabled")
        )
        await session.commit()
    resp = await _exchange(env, code, verifier)
    assert resp.json()["error"] == "invalid_grant"
    assert await _families(env) == []


# --------------------------------------------------------------------------- #
# The refresh token
# --------------------------------------------------------------------------- #


async def test_a_refresh_rotates_and_the_old_token_ends_the_family(env: Env) -> None:
    first = await _tokens(env)
    rotated = await _refresh(env, first["refresh_token"])
    assert rotated.status_code == 200, rotated.text
    second = rotated.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert await access.verify_access_token(second["access_token"], settings=env.api.settings)
    # The old one again: the family ends, the new one with it (falsifier 6).
    reused = await _refresh(env, first["refresh_token"])
    assert reused.status_code == 400
    assert reused.json()["error"] == "invalid_grant"
    (family,) = await _families(env)
    assert family.revoked_reason == "refresh_reused"
    assert (await _refresh(env, second["refresh_token"])).json()["error"] == "invalid_grant"
    (event,) = await _events(env, "mcp.refresh_reused")
    assert event.actor_type == "system"
    assert event.meta["family"] == str(family.id)


async def test_a_refresh_moves_the_idle_limit_and_an_idle_family_is_dead(env: Env) -> None:
    first = await _tokens(env)
    (family,) = await _families(env)
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        await session.execute(
            update(OAuthRefreshFamily)
            .where(OAuthRefreshFamily.id == family.id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    resp = await _refresh(env, first["refresh_token"])
    assert resp.json()["error"] == "invalid_grant"


async def test_a_refresh_extends_the_family_by_thirty_days(env: Env) -> None:
    first = await _tokens(env)
    before = datetime.now(UTC)
    assert (await _refresh(env, first["refresh_token"])).status_code == 200
    (family,) = await _families(env)
    assert family.expires_at >= before + timedelta(days=30)


async def test_a_refresh_by_another_client_is_invalid_grant(env: Env) -> None:
    first = await _tokens(env)
    other = await seed_client(env.api, env.org_id)
    resp = await token_request(
        env.api, grant_type="refresh_token", refresh_token=first["refresh_token"], client_id=other.client_id
    )
    assert resp.json()["error"] == "invalid_grant"
    (family,) = await _families(env)
    assert family.revoked_at is None  # not a reuse: the owner's token still works
    assert (await _refresh(env, first["refresh_token"])).status_code == 200


async def test_a_disabled_person_cannot_refresh_and_the_family_ends(env: Env) -> None:
    first = await _tokens(env)
    async with AsyncSession(env.api.admin_engine) as session:
        await session.execute(
            update(User).where(User.id == await user_id_of(env.api, env.sub)).values(status="disabled")
        )
        await session.commit()
    assert (await _refresh(env, first["refresh_token"])).json()["error"] == "invalid_grant"
    (family,) = await _families(env)
    assert family.revoked_reason == "person_inactive"


@pytest.mark.parametrize(("scope", "ok"), [("mcp", True), ("mcp offline_access", True), ("openid admin", False)])
async def test_a_refresh_may_ask_for_no_more_than_mcp(env: Env, scope: str, ok: bool) -> None:
    first = await _tokens(env)
    resp = await _refresh(env, first["refresh_token"], scope=scope)
    if ok:
        assert resp.status_code == 200 and resp.json()["scope"] == "mcp"
    else:
        assert resp.json()["error"] == "invalid_scope"


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("grant_type", ["client_credentials", "password", "implicit", "urn:x"])
async def test_only_two_grant_types_exist(env: Env, grant_type: str) -> None:
    """No machine-to-machine token: every one names a person who consented
    (invariant 1c(a))."""
    resp = await token_request(env.api, grant_type=grant_type, client_id=env.app.client_id)
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_grant_type"


async def test_client_authentication_is_refused_with_its_challenge(env: Env) -> None:
    async with oauth_http(env.api) as client:
        resp = await client.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": "x"},
            headers={"Authorization": "Basic aXRtc194OnNlY3JldA=="},
        )
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_client"
    assert resp.headers["www-authenticate"] == 'Basic realm="itms"'


async def test_the_request_is_one_form_with_no_parameter_twice(env: Env) -> None:
    async with oauth_http(env.api) as client:
        as_json = await client.post("/oauth/token", json={"grant_type": "refresh_token"})
        twice = await client.post(
            "/oauth/token",
            content=b"grant_type=refresh_token&refresh_token=a&refresh_token=b&client_id=x",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        missing = await client.post("/oauth/token", data={"grant_type": "refresh_token"})
    for resp in (as_json, twice, missing):
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_request"


async def test_without_the_deployments_organization_the_server_is_unavailable(env: Env) -> None:
    env.api.settings.default_org_slug = None
    resp = await token_request(env.api, grant_type="refresh_token", refresh_token="x", client_id="y")
    assert resp.status_code == 503
    assert resp.json()["error"] == "temporarily_unavailable"


async def test_openbao_down_is_503_and_spends_nothing(env: Env, monkeypatch) -> None:
    code, verifier = await _code(env)

    async def down(*args: Any, **kwargs: Any) -> str:
        raise keys.AuthKeysUnavailable("down")

    monkeypatch.setattr(keys, "sign_jwt", down)
    resp = await _exchange(env, code, verifier)
    assert resp.status_code == 503
    assert resp.json()["error"] == "temporarily_unavailable"
    monkeypatch.undo()
    assert (await _exchange(env, code, verifier)).status_code == 200


# --------------------------------------------------------------------------- #
# RFC 7009
# --------------------------------------------------------------------------- #


async def _revoke(env: Env, token: str, client_id: str | None = None, **extra: str) -> Any:
    async with oauth_http(env.api) as client:
        return await client.post(
            "/oauth/revoke",
            data={"token": token, "client_id": client_id or env.app.client_id, **extra},
        )


@pytest.mark.parametrize("which", ["refresh_token", "access_token"])
async def test_revoking_either_token_ends_the_grant(env: Env, which: str) -> None:
    tokens = await _tokens(env)
    resp = await _revoke(env, tokens[which], token_type_hint="not-a-real-hint")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    (family,) = await _families(env)
    assert family.revoked_reason == "revoked_by_app"
    assert (await _refresh(env, tokens["refresh_token"])).json()["error"] == "invalid_grant"
    (event,) = await _events(env, "mcp.grant_revoked")
    assert event.actor_type == "system"


async def test_revoking_a_token_nobody_issued_is_still_200(env: Env) -> None:
    assert (await _revoke(env, "nonsense")).status_code == 200
    assert (await _revoke(env, "a.b.c")).status_code == 200


async def test_revoking_another_clients_token_is_refused(env: Env) -> None:
    tokens = await _tokens(env)
    other = await seed_client(env.api, env.org_id)
    resp = await _revoke(env, tokens["refresh_token"], client_id=other.client_id)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"
    (family,) = await _families(env)
    assert family.revoked_at is None


async def test_revoking_for_an_unknown_client_is_invalid_client(env: Env) -> None:
    resp = await _revoke(env, "x", client_id="itms_nobody")
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client"
```

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_token.py`
Expected: FAIL, `ImportError: cannot import name 'grants' from 'app.core.auth.oauth'`.

- [ ] **Step 2: `grants.py`**

`backend/app/core/auth/oauth/grants.py`:

```python
"""Authorization codes and refresh-token families (design §5.2, §5.3; plan
D6, D10).

THE CODE (§5.2): 256 random bits, stored as its SHA-256, 60 seconds, single
use, bound to the app, the exact redirect the request named (a loopback port
included: the token request must repeat it, RFC 6749 §4.1.3), the PKCE
challenge and the resource. Used twice, it is refused and the grant the first
use opened ends: RFC 6749 §4.1.2, "MUST deny the request and SHOULD revoke
(when possible) all tokens previously issued based on that authorization
code".

THE FAMILY (§5.3): one person's grant to one app. Its refresh tokens are 256
random bits stored as SHA-256 and ROTATE on every use (RFC 9700: "the
authorization server issues a new refresh token with every access token
refresh response. The previous refresh token is invalidated, but information
about the relationship is retained" [S16]; MCP: "MUST rotate refresh tokens"
[S30]). Presenting a used one ends the family; so does a person no longer
active; the family dies after `REFRESH_IDLE` without use. Every refusal is
`invalid_grant` (Anthropic: "Return RFC 6749-compliant error codes when a
refresh token is no longer valid: invalid_grant" [S33]).

ONE TRANSACTION PER REQUEST, SIGNED INSIDE IT (`oauth_session`): the access
token is minted before the commit, so OpenBao down rolls back and spends
nothing. A refusal that follows a write -- a reuse that ended a family -- is
returned as a `Problem` and raised AFTER the block has committed, so the write
stays (the `flows` pattern, phase 1).

LOCKED: the code or the refresh token is read `FOR UPDATE`, so two requests
with the same one are serialised and the second sees it used.

BULK STATEMENTS ON O TABLES (`update`, `delete`) are invisible to
`app.core.global_rows`'s listener; every one here runs under `oauth_session`
or a caller's every-client session (the lifecycle acts, Task 10), where
writing a technician's global rows is allowed.
"""

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.auth.oauth import pkce
from app.core.auth.oauth.access import mint_access_token, verify_access_token
from app.core.auth.oauth.clients import find_client, touch
from app.core.auth.oauth.errors import (
    BAD_CODE,
    BAD_REFRESH,
    BAD_VERIFIER,
    UNKNOWN_CLIENT,
    WRONG_CLIENT,
    OAuthError,
    Problem,
)
from app.core.auth.oauth.policy import CODE_TTL_S, REFRESH_IDLE
from app.core.auth.oauth.store import oauth_session
from app.core.identity import ACTIVE_STATUS, STAFF_KIND
from app.models import User
from app.models.oauth import OAuthClient, OAuthCode, OAuthRefreshFamily, OAuthRefreshToken

#: A person's codes that expired this long ago go when the next is issued (plan D10).
CODE_KEPT = timedelta(minutes=10)
#: A person's families that ended this long ago go when the next opens (plan D10).
FAMILY_KEPT = timedelta(days=1)


def hash_secret(value: str) -> bytes:
    """SHA-256: a random 256-bit value needs no salt and no pepper."""
    return sha256(value.encode("utf-8")).digest()


def new_secret() -> str:
    """256 random bits, base64url: 43 characters."""
    return secrets.token_urlsafe(32)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str = field(repr=False)
    expires_in: int
    refresh_token: str = field(repr=False)


async def _person_is_active(session: AsyncSession, user_id: UUID) -> bool:
    user = await session.get(User, user_id)
    return user is not None and user.status == ACTIVE_STATUS and user.kind == STAFF_KIND


async def _audit_broken(
    session: AsyncSession, *, org_id: UUID, user_id: UUID, action: str, app: str, family_id: UUID
) -> None:
    """A grant somebody other than the person ended: a reuse, or the app's own
    RFC 7009 request (Task 7, decision 3)."""
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="system",
        actor_id=None,
        action=action,
        target_type="user",
        target_id=str(user_id),
        metadata={"app": app, "family": str(family_id)},
    )


async def issue_code(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    oauth_client_id: UUID,
    redirect_uri: str,
    code_challenge: str,
    resource: str,
    now: datetime,
) -> str:
    """A one-time code for what the person just approved (Task 8)."""
    await session.execute(
        delete(OAuthCode).where(OAuthCode.user_id == user_id, OAuthCode.expires_at < now - CODE_KEPT)
    )
    code = new_secret()
    session.add(
        OAuthCode(
            org_id=org_id,
            user_id=user_id,
            oauth_client_id=oauth_client_id,
            code_hash=hash_secret(code),
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            resource=resource,
            created_at=now,
            expires_at=now + timedelta(seconds=CODE_TTL_S),
        )
    )
    await session.flush()
    return code


async def _new_refresh_token(session: AsyncSession, family: OAuthRefreshFamily, now: datetime) -> str:
    secret = new_secret()
    session.add(
        OAuthRefreshToken(
            org_id=family.org_id,
            user_id=family.user_id,
            family_id=family.id,
            token_hash=hash_secret(secret),
            created_at=now,
        )
    )
    await session.flush()
    return secret


async def open_family(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    oauth_client_id: UUID,
    resource: str,
    now: datetime,
) -> tuple[OAuthRefreshFamily, str]:
    """A new grant and its first refresh token."""
    await session.execute(
        delete(OAuthRefreshFamily).where(
            OAuthRefreshFamily.user_id == user_id,
            or_(
                OAuthRefreshFamily.revoked_at < now - FAMILY_KEPT,
                OAuthRefreshFamily.expires_at < now - FAMILY_KEPT,
            ),
        )
    )
    family = OAuthRefreshFamily(
        org_id=org_id,
        user_id=user_id,
        oauth_client_id=oauth_client_id,
        resource=resource,
        created_at=now,
        last_used_at=now,
        expires_at=now + REFRESH_IDLE,
    )
    session.add(family)
    await session.flush()
    return family, await _new_refresh_token(session, family, now)


async def revoke_family(session: AsyncSession, family_id: UUID, reason: str, now: datetime) -> bool:
    """End one grant; False when it had already ended."""
    ended = await session.execute(
        update(OAuthRefreshFamily)
        .where(OAuthRefreshFamily.id == family_id, OAuthRefreshFamily.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=reason)
        .returning(OAuthRefreshFamily.id)
    )
    return ended.first() is not None


async def revoke_all_for_user(session: AsyncSession, user_id: UUID, reason: str) -> int:
    """End every live grant of a person (the lifecycle acts, Task 10); how many."""
    ended = await session.execute(
        update(OAuthRefreshFamily)
        .where(OAuthRefreshFamily.user_id == user_id, OAuthRefreshFamily.revoked_at.is_(None))
        .values(revoked_at=_now(), revoked_reason=reason)
        .returning(OAuthRefreshFamily.id)
    )
    return len(ended.all())


async def revoke_app(
    session: AsyncSession,
    *,
    oauth_client_id: UUID,
    user_id: UUID | None,
    reason: str,
    now: datetime,
) -> int:
    """End an app's live grants: one person's, or (`user_id` None) everyone's
    in the session's organization (Task 10); how many."""
    query = update(OAuthRefreshFamily).where(
        OAuthRefreshFamily.oauth_client_id == oauth_client_id,
        OAuthRefreshFamily.revoked_at.is_(None),
    )
    if user_id is not None:
        query = query.where(OAuthRefreshFamily.user_id == user_id)
    ended = await session.execute(
        query.values(revoked_at=now, revoked_reason=reason).returning(OAuthRefreshFamily.id)
    )
    return len(ended.all())


async def family_is_live(
    session: AsyncSession, *, family_id: UUID, user_id: UUID, client_id: str, now: datetime
) -> bool:
    """The grant an access token names still stands, for that person and that
    app (the MCP door, Task 9; plan D6)."""
    found = (
        await session.execute(
            select(OAuthRefreshFamily.id)
            .join(OAuthClient, OAuthClient.id == OAuthRefreshFamily.oauth_client_id)
            .where(
                OAuthRefreshFamily.id == family_id,
                OAuthRefreshFamily.user_id == user_id,
                OAuthClient.client_id == client_id,
                OAuthRefreshFamily.revoked_at.is_(None),
                OAuthRefreshFamily.expires_at > now,
            )
        )
    ).scalar_one_or_none()
    return found is not None


# --------------------------------------------------------------------------- #
# The token endpoint's two grants and RFC 7009
# --------------------------------------------------------------------------- #


async def _exchange(
    session: AsyncSession,
    settings: Any,
    *,
    org_id: UUID,
    client_id: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    now: datetime,
) -> IssuedTokens | Problem:
    client = await find_client(session, org_id=org_id, client_id=client_id, now=now)
    if client is None:
        return UNKNOWN_CLIENT
    row = (
        await session.execute(
            select(OAuthCode).where(OAuthCode.code_hash == hash_secret(code)).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None or row.oauth_client_id != client.id:
        return BAD_CODE
    if row.used_at is not None:
        if row.family_id is not None and await revoke_family(session, row.family_id, "code_reused", now):
            await _audit_broken(
                session,
                org_id=org_id,
                user_id=row.user_id,
                action="mcp.code_reused",
                app=client.client_id,
                family_id=row.family_id,
            )
        return BAD_CODE
    if row.expires_at <= now or row.redirect_uri != redirect_uri:
        return BAD_CODE
    if not pkce.verify(code_verifier, row.code_challenge):
        return BAD_VERIFIER  # the code stays usable (Task 7, decision 2)
    row.used_at = now
    if not await _person_is_active(session, row.user_id):
        return BAD_CODE
    family, refresh_token = await open_family(
        session,
        org_id=org_id,
        user_id=row.user_id,
        oauth_client_id=client.id,
        resource=row.resource,
        now=now,
    )
    row.family_id = family.id
    touch(client, now)
    access_token, expires_in = await mint_access_token(
        settings=settings,
        user_id=row.user_id,
        org_id=org_id,
        client_id=client.client_id,
        family_id=family.id,
    )
    return IssuedTokens(access_token, expires_in, refresh_token)


async def exchange_code(
    app: Any, *, org_id: UUID, client_id: str, code: str, redirect_uri: str, code_verifier: str
) -> IssuedTokens:
    async with oauth_session(org_id, app.state.app_engine) as session:
        outcome = await _exchange(
            session,
            app.state.settings,
            org_id=org_id,
            client_id=client_id,
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
            now=_now(),
        )
    if isinstance(outcome, Problem):
        raise OAuthError(outcome)
    return outcome


async def _refresh(
    session: AsyncSession,
    settings: Any,
    *,
    org_id: UUID,
    client_id: str,
    refresh_token: str,
    now: datetime,
) -> IssuedTokens | Problem:
    client = await find_client(session, org_id=org_id, client_id=client_id, now=now)
    if client is None:
        return UNKNOWN_CLIENT
    found = (
        await session.execute(
            select(OAuthRefreshToken, OAuthRefreshFamily)
            .join(OAuthRefreshFamily, OAuthRefreshFamily.id == OAuthRefreshToken.family_id)
            .where(OAuthRefreshToken.token_hash == hash_secret(refresh_token))
            .with_for_update()
        )
    ).one_or_none()
    if found is None:
        return BAD_REFRESH
    token, family = found
    if family.oauth_client_id != client.id:
        return BAD_REFRESH  # another app's: not a reuse (Task 7, decision 1)
    if family.revoked_at is not None or family.expires_at <= now:
        return BAD_REFRESH
    if token.used_at is not None:
        if await revoke_family(session, family.id, "refresh_reused", now):
            await _audit_broken(
                session,
                org_id=org_id,
                user_id=family.user_id,
                action="mcp.refresh_reused",
                app=client.client_id,
                family_id=family.id,
            )
        return BAD_REFRESH
    if not await _person_is_active(session, family.user_id):
        await revoke_family(session, family.id, "person_inactive", now)
        return BAD_REFRESH
    token.used_at = now
    family.last_used_at = now
    family.expires_at = now + REFRESH_IDLE
    rotated = await _new_refresh_token(session, family, now)
    touch(client, now)
    access_token, expires_in = await mint_access_token(
        settings=settings,
        user_id=family.user_id,
        org_id=org_id,
        client_id=client.client_id,
        family_id=family.id,
    )
    return IssuedTokens(access_token, expires_in, rotated)


async def refresh_grant(app: Any, *, org_id: UUID, client_id: str, refresh_token: str) -> IssuedTokens:
    async with oauth_session(org_id, app.state.app_engine) as session:
        outcome = await _refresh(
            session,
            app.state.settings,
            org_id=org_id,
            client_id=client_id,
            refresh_token=refresh_token,
            now=_now(),
        )
    if isinstance(outcome, Problem):
        raise OAuthError(outcome)
    return outcome


async def revoke_presented(app: Any, *, org_id: UUID, client_id: str, token: str) -> None:
    """RFC 7009: a refresh token or an access token of this client ends its
    grant; a token nobody issued is not an error (§2.2)."""
    now = _now()
    grant = (
        await verify_access_token(token, settings=app.state.settings)
        if token.count(".") == 2
        else None
    )
    refused: Problem | None = None
    async with oauth_session(org_id, app.state.app_engine) as session:
        client = await find_client(session, org_id=org_id, client_id=client_id, now=now)
        if client is None:
            refused = UNKNOWN_CLIENT
        else:
            family = (
                await session.execute(
                    select(OAuthRefreshFamily)
                    .join(OAuthRefreshToken, OAuthRefreshToken.family_id == OAuthRefreshFamily.id)
                    .where(OAuthRefreshToken.token_hash == hash_secret(token))
                )
            ).scalar_one_or_none()
            if family is None and grant is not None and grant.org_id == org_id:
                family = await session.get(OAuthRefreshFamily, grant.family_id)
            if family is not None:
                if family.oauth_client_id != client.id:
                    refused = WRONG_CLIENT
                elif await revoke_family(session, family.id, "revoked_by_app", now):
                    await _audit_broken(
                        session,
                        org_id=org_id,
                        user_id=family.user_id,
                        action="mcp.grant_revoked",
                        app=client.client_id,
                        family_id=family.id,
                    )
    if refused is not None:
        raise OAuthError(refused)
```

- [ ] **Step 3: Las rutas**

`backend/app/api/oauth_tokens.py`:

```python
"""The OAuth server's token and revocation endpoints (design §5.3; plan
2026-11-06, Task 7):

    POST /oauth/token    authorization_code | refresh_token   RFC 6749 §4.1.3, §6
    POST /oauth/revoke   RFC 7009

THE REQUEST IS RFC 6749's: `application/x-www-form-urlencoded` (Anthropic:
"Your /token endpoint must accept Content-Type:
application/x-www-form-urlencoded per RFC 6749 section 4.1.3" [S33]), at most
`MAX_REQUEST_BYTES`, no parameter twice ("Request and response parameters
MUST NOT be included more than once", §3.2) -- `resource` excepted, which RFC
8707 §2 lets repeat -- and an empty one is absent ("Parameters sent without a
value MUST be treated as if they were omitted"). Every `resource` must be the
MCP resource (`invalid_target`).

PUBLIC CLIENTS ONLY (plan D4): `client_id` in the body, never an
`Authorization` header; one that sends it is `invalid_client` with a 401 and
`WWW-Authenticate` (RFC 6749 §5.2).

NO GRANT BUT THESE TWO: no `client_credentials`, no `password` (RFC 9700: "MUST
NOT be used" [S16]), no implicit. Every token names a person who consented
(invariant 1c(a)).

Outside `/api/v1`: no MCP tool reaches it, no cookie is read, and the CSRF rule
(`app/core/auth/csrf.py`, `/api/v1/` only) has nothing to say about a request
that carries no ambient credential.
"""

from urllib.parse import parse_qsl
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.core.auth.keys import AuthKeysUnavailable
from app.core.auth.oauth import grants
from app.core.auth.oauth.errors import (
    BAD_SCOPE,
    BAD_TARGET,
    CLIENT_AUTH,
    KEYS_UNAVAILABLE,
    MALFORMED,
    MISSING,
    NO_STORE,
    NOT_FORM,
    REPEATED,
    UNAVAILABLE,
    UNSUPPORTED_GRANT,
    OAuthError,
    no_store,
)
from app.core.auth.oauth.policy import (
    MAX_REQUEST_BYTES,
    REVOKE_PATH,
    SCOPE,
    SCOPES_SUPPORTED,
    TOKEN_PATH,
    issuer,
)
from app.core.auth.oauth.store import deployment_org
from app.core.http import read_bounded_body
from app.core.mcp_channel import mcp_resource

router = APIRouter()

Form = dict[str, list[str]]
_GRANTS = frozenset({"authorization_code", "refresh_token"})


async def _ready(request: Request) -> UUID:
    settings = request.app.state.settings
    if issuer(settings) is None or mcp_resource(settings) is None:
        raise OAuthError(UNAVAILABLE)
    org_id = await deployment_org(request.app)
    if org_id is None:
        raise OAuthError(UNAVAILABLE)
    return org_id


async def _form(request: Request) -> Form:
    if "authorization" in request.headers:
        raise OAuthError(CLIENT_AUTH, headers={"WWW-Authenticate": 'Basic realm="itms"'})
    media = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media != "application/x-www-form-urlencoded":
        raise OAuthError(NOT_FORM)
    raw = await read_bounded_body(request, MAX_REQUEST_BYTES)
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise OAuthError(MALFORMED) from exc
    form: Form = {}
    for name, value in parse_qsl(text, keep_blank_values=True):
        if value:
            form.setdefault(name, []).append(value)
    return form


def _optional(form: Form, name: str) -> str | None:
    values = form.get(name, [])
    if len(values) > 1:
        raise OAuthError(REPEATED)
    return values[0] if values else None


def _one(form: Form, name: str) -> str:
    value = _optional(form, name)
    if value is None:
        raise OAuthError(MISSING)
    return value


def _check_resource(form: Form, request: Request) -> None:
    resource = mcp_resource(request.app.state.settings)
    if any(value != resource for value in form.get("resource", [])):
        raise OAuthError(BAD_TARGET)


def _check_scope(form: Form) -> None:
    scope = _optional(form, "scope")
    if scope is not None and not set(scope.split()) <= set(SCOPES_SUPPORTED):
        raise OAuthError(BAD_SCOPE)


@router.post(TOKEN_PATH, include_in_schema=False)
async def token(request: Request) -> JSONResponse:
    org_id = await _ready(request)
    form = await _form(request)
    grant_type = _one(form, "grant_type")
    if grant_type not in _GRANTS:
        raise OAuthError(UNSUPPORTED_GRANT)
    client_id = _one(form, "client_id")
    _check_resource(form, request)
    try:
        if grant_type == "authorization_code":
            issued = await grants.exchange_code(
                request.app,
                org_id=org_id,
                client_id=client_id,
                code=_one(form, "code"),
                redirect_uri=_one(form, "redirect_uri"),
                code_verifier=_one(form, "code_verifier"),
            )
        else:
            _check_scope(form)
            issued = await grants.refresh_grant(
                request.app,
                org_id=org_id,
                client_id=client_id,
                refresh_token=_one(form, "refresh_token"),
            )
    except AuthKeysUnavailable as exc:
        raise OAuthError(KEYS_UNAVAILABLE) from exc
    return no_store(
        {
            "access_token": issued.access_token,
            "token_type": "Bearer",
            "expires_in": issued.expires_in,
            "refresh_token": issued.refresh_token,
            "scope": SCOPE,
        }
    )


@router.post(REVOKE_PATH, include_in_schema=False)
async def revoke(request: Request) -> Response:
    org_id = await _ready(request)
    form = await _form(request)
    # `token_type_hint` is read by nobody: "An invalid token type hint value
    # is ignored" (RFC 7009 §2.2), and so is a valid one.
    client_id = _one(form, "client_id")
    presented = _one(form, "token")
    try:
        await grants.revoke_presented(
            request.app, org_id=org_id, client_id=client_id, token=presented
        )
    except AuthKeysUnavailable as exc:
        raise OAuthError(KEYS_UNAVAILABLE) from exc
    return Response(status_code=200, headers=NO_STORE)
```

En `backend/app/main.py`, después de `app.include_router(oauth_registration_router)` (Task 6):

```python
    from app.api.oauth_tokens import router as oauth_tokens_router

    app.include_router(oauth_tokens_router)
```

(Si la Task 6 aún no está en la rama, las dos líneas van justo después de `app.include_router(well_known_router)`; al
fusionar, el coordinador deja las dos inclusiones juntas.)

- [ ] **Step 4: El vocabulario de auditoría**

En `frontend/src/i18n/en/auditVocabulary.ts`, en orden alfabético dentro de `auditActions`:

```ts
  mcp_code_reused: "MCP code reused",
  mcp_grant_revoked: "MCP grant revoked",
  mcp_refresh_reused: "MCP refresh reused",
```

y en `frontend/src/i18n/es/auditVocabulary.ts`:

```ts
  mcp_code_reused: "Código de autorización de MCP reutilizado",
  mcp_grant_revoked: "Concesión de MCP revocada por la aplicación",
  mcp_refresh_reused: "Token de renovación de MCP reutilizado",
```

- [ ] **Step 5: Las pruebas pasan**

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_token.py tests/test_oauth_schema.py tests/test_i18n_catalog.py tests/test_client_catalog.py tests/test_logging_discipline.py`
Expected: PASS.

Run: `cd frontend && npx vitest run src/modules/audit src/i18n`
Expected: PASS (`auditVocabulary.coverage.test.ts` encuentra las tres acciones nuevas con su entrada en los dos
idiomas; `auditVocabulary.english.test.ts`, la derivación).

Run: `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `Success: no issues found` y `All checks passed!`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/core/auth/oauth/grants.py backend/app/api/oauth_tokens.py backend/app/main.py \
  backend/tests/oauth_support.py backend/tests/test_oauth_token.py \
  frontend/src/i18n/en/auditVocabulary.ts frontend/src/i18n/es/auditVocabulary.ts
git commit -m "feat(oauth): the token and revocation endpoints: one-time codes, rotating refresh families"
```

### Task 8: El consentimiento: `authorize.py`, `consent.py` y `/api/v1/oauth/authorize/{details,decision}`

Diseño §5.2; D2, D7, D8, D11. Oleada 4, en paralelo con la Task 9. Backend.

Las dos rutas que la página de la Task 4 llama con la sesión de la consola. Las dos validan la petición de
autorización entera (RFC 6749 §4.1.2.1 en sus dos mitades: lo que no se puede devolver porque la dirección no es de
fiar se enseña a la persona; lo demás vuelve a la app con `error`, `state` e `iss`), comprueban que la persona puede
usar MCP (D11) y, al aprobar, emiten el código de 60 s (Task 7) y lo auditan.

**Files:**
- Modify: `backend/app/core/auth/oauth/errors.py` (cuatro `Problem` del punto de autorización, al final)
- Create: `backend/app/core/auth/oauth/authorize.py`
- Create: `backend/app/core/auth/oauth/consent.py`
- Create: `backend/app/api/v1/oauth_consent.py`
- Modify: `backend/app/main.py` (incluir el router, junto a `session_settings_router`)
- Modify: `backend/app/mcp/exclusions.py` (`NEVER_EXPOSED_REASONS` +1, `NOT_FOR_MCP` +1)
- Modify: `backend/app/core/i18n/es.py`
- Modify: `frontend/src/i18n/en/auditVocabulary.ts`, `frontend/src/i18n/es/auditVocabulary.ts` (`mcp.app_authorized`)
- Modify: `backend/tests/test_mcp_channel_api.py` (`OWN_SIGN_IN_NEVER_EXPOSED`, recuento 36 → 37)
- Modify: `backend/tests/test_route_gating.py` (`UNGATED_BY_DESIGN` +2)
- Modify: `backend/tests/oauth_support.py` (`authorize_query`, `decide`, `code_of`, `connect` al final)
- Test: `backend/tests/test_oauth_consent.py`

**Interfaces:**
- Consumes: Task 1 (`errors`, `policy.issuer`, `policy.SCOPE`, `redirects.matches`, `redirects.with_params`,
  `redirects.host_of`, `redirects.is_loopback`, `pkce.valid_challenge`), Task 3 (`cimd.DocumentRefused`), Task 5
  (`oauth_session`, `ClientView`, `touch`), Task 6 (`resolve_client`; en `tests/oauth_support.py`, `register`), Task 7
  (`issue_code`; en `tests/oauth_support.py`, `pkce_pair`, `token_request`). Existentes: `AuthContext` (con
  `principal`, `email`, `clients`), `bound_auth_context` (`backend/app/api/deps.py`), `app.core.auth.guards.sign_in_again()`
  (el 401 que usa `api/v1/auth.py::_console_ctx`), `codes_held_anywhere(session, user_id)` (`app/core/rbac.py`, como
  `mcp/server.py::_gate`), `audit_service.record`, `tr`, `mcp_resource`.
- Produces: `authorize.AuthorizationRequest`, `NotRedirectable`, `Redirectable`, `parse_query`, `validate`, `answer`;
  `consent.details`, `consent.decide`; las rutas `POST /api/v1/oauth/authorize/details` y `…/decision` con el contrato
  del Apéndice A.2; la acción `mcp.app_authorized`; en `tests/oauth_support.py`, `authorize_query(client_id, *,
  challenge, redirect_uri=CLAUDE_CALLBACK, state="st4te", **extra) -> str`, `async def decide(env, org_id, sub, query,
  *, approve=True) -> httpx.Response`, `code_of(redirect_to) -> str`, `async def connect(env, org_id, sub, *,
  client_id=None) -> dict` (registra si no se da `client_id`, aprueba y canjea: `{"client_id", "access_token",
  "refresh_token", …}`).

**Decisión de la tarea (1): sin `resource` es el recurso MCP** (pregunta P6). RFC 8707 §2 deja al servidor exigirlo
(«MAY fail requests that omit the parameter with an "invalid_target" error»); MCP dice que el cliente «MUST» mandarlo
[S27], pero que Claude lo mande está SIN VERIFICAR (dossier, UNVERIFIED 3). Este servidor emite tokens para un solo
recurso, así que tomar su ausencia por él no ensancha nada; un `resource` presente y distinto es `invalid_target`.

**Decisión de la tarea (2): el consentimiento no pide confirmación reciente del segundo factor** (pregunta P14). §2.5
da la lista cerrada de actos que la piden y el consentimiento no está. Si el operador la quiere, es una línea en
`consent.decide` (Step 4, el comentario lo marca): `require_step_up(ctx)` antes de emitir el código, y la Task 4 ya
sabe pedirla (el 403 `step_up_required` de la consola).

**Decisión de la tarea (3): un rechazo no se audita; una aprobación sí.** §7: «consentimientos y revocaciones de
clientes de IA». Rechazar no da nada a nadie.

- [ ] **Step 1: Las pruebas que fallan**

Al final de `backend/tests/oauth_support.py`:

```python
def authorize_query(
    client_id: str,
    *,
    challenge: str,
    redirect_uri: str = CLAUDE_CALLBACK,
    state: str = "st4te",
    **extra: str,
) -> str:
    """The query of `/oauth/authorize` as an MCP client sends it."""
    from urllib.parse import urlencode

    return urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "resource": RESOURCE,
            **extra,
        }
    )


async def decide(env: Any, org_id: UUID, sub: str, query: str, *, approve: bool = True) -> httpx.Response:
    """The consent page's decision, with the person's console session."""
    return await env.client.post(
        "/api/v1/oauth/authorize/decision",
        json={"query": query, "approve": approve},
        headers=await env.headers(org_id, sub),
    )


def code_of(redirect_to: str) -> str:
    from urllib.parse import parse_qs, urlsplit

    return parse_qs(urlsplit(redirect_to).query)["code"][0]


async def connect(env: Any, org_id: UUID, sub: str, *, client_id: str | None = None) -> dict[str, Any]:
    """An AI app connected by the person, the whole way: registered (unless
    `client_id` names one), approved, its code exchanged."""
    if client_id is None:
        client_id = (await register(env))["client_id"]
    verifier, challenge = pkce_pair()
    approved = await decide(env, org_id, sub, authorize_query(client_id, challenge=challenge))
    assert approved.status_code == 200, approved.text
    resp = await token_request(
        env,
        grant_type="authorization_code",
        code=code_of(approved.json()["redirect_to"]),
        redirect_uri=CLAUDE_CALLBACK,
        client_id=client_id,
        code_verifier=verifier,
    )
    assert resp.status_code == 200, resp.text
    return {"client_id": client_id, **resp.json()}
```

`backend/tests/test_oauth_consent.py`:

```python
"""The consent screen's two routes (autenticación propia design §5.2; plan
2026-11-06, Task 8).

- the request is validated in RFC 6749 §4.1.2.1's two halves: a bad client or
  redirect is shown, never sent back; anything else goes back to the app with
  `state` and `iss` (RFC 9207 §2);
- the consent names the app, the host its answer goes to, the host of its
  metadata document, and warns when it only returns to this computer [S30];
- MCP switched off, or no `mcp.use`, is said before anything is approved, with
  a way back to the app (`access_denied`);
- approving issues a 60-second code the token endpoint accepts, audited
  `mcp.app_authorized`; declining issues nothing;
- the routes need the console's own session: no cookie, no MCP, no CSRF.
"""

from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.oauth import cimd
from app.core.auth.oauth.store import oauth_session
from app.models import AuditEvent
from app.models.oauth import OAuthCode
from tests.mcp_support import marked_client
from tests.oauth_support import (
    BASE,
    CLAUDE_CALLBACK,
    LOOPBACK_CALLBACK,
    authorize_query,
    code_of,
    decide,
    oauth_org,
    pkce_pair,
    seed_client,
    token_request,
)

DOCUMENT_URL = "https://claude.example/oauth/client-metadata.json"


class Env:
    def __init__(self, api: Any, org_id: UUID, sub: str) -> None:
        self.api, self.org_id, self.sub = api, org_id, sub


@pytest.fixture()
async def env(api_env) -> Env:
    org_id, _ = await oauth_org(api_env)
    api_env.settings.mcp_registration = "open"
    sub = await api_env.create_user(org_id, "org-admin")
    return Env(api_env, org_id, sub)


async def _details(env: Env, query: str, sub: str | None = None) -> Any:
    return await env.api.client.post(
        "/api/v1/oauth/authorize/details",
        json={"query": query},
        headers=await env.api.headers(env.org_id, sub or env.sub),
    )


def _params(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


async def _registered(env: Env, *uris: str) -> Any:
    return await seed_client(env.api, env.org_id, redirect_uris=uris or (CLAUDE_CALLBACK,))


# --------------------------------------------------------------------------- #
# What the person is asked
# --------------------------------------------------------------------------- #


async def test_the_consent_names_the_app_its_return_and_the_account(env: Env) -> None:
    app = await _registered(env)
    _, challenge = pkce_pair()
    resp = await _details(env, authorize_query(app.client_id, challenge=challenge))
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json() == {
        "outcome": "consent",
        "client": {
            "name": "Claude",
            "client_id": app.client_id,
            "kind": "registered",
            "client_host": None,
            "redirect_host": "claude.ai",
            "loopback_only": False,
        },
        "scope": "mcp",
        "account": f"{env.sub}@test.local",
    }


async def test_an_app_that_only_returns_to_this_computer_is_marked(env: Env) -> None:
    app = await _registered(env, "http://127.0.0.1/callback", "http://localhost/callback")
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge, redirect_uri="http://127.0.0.1:51004/callback")
    body = (await _details(env, query)).json()
    assert body["client"]["loopback_only"] is True
    assert body["client"]["redirect_host"] == "127.0.0.1"


async def test_a_metadata_document_client_names_its_host(env: Env, monkeypatch) -> None:
    async def document(client_id: str) -> cimd.ClientDocument:
        assert client_id == DOCUMENT_URL
        return cimd.ClientDocument(
            client_id=DOCUMENT_URL,
            client_name="Claude Code",
            redirect_uris=("http://localhost/callback",),
            cache_for=cimd.MIN_CACHE,
        )

    monkeypatch.setattr(cimd, "fetch_document", document)
    _, challenge = pkce_pair()
    query = authorize_query(DOCUMENT_URL, challenge=challenge, redirect_uri="http://localhost:6123/callback")
    client = (await _details(env, query)).json()["client"]
    assert client["kind"] == "metadata_document"
    assert client["client_host"] == "claude.example"
    assert client["name"] == "Claude Code"


# --------------------------------------------------------------------------- #
# RFC 6749 §4.1.2.1: shown, never sent back
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "case", ["no-client", "unknown-client", "two-clients", "no-redirect", "unregistered-redirect", "two-redirects"]
)
async def test_a_bad_client_or_redirect_is_shown_and_never_redirected(env: Env, case: str) -> None:
    app = await _registered(env)
    _, challenge = pkce_pair()
    good = authorize_query(app.client_id, challenge=challenge)
    query = {
        "no-client": good.replace(f"client_id={app.client_id}&", ""),
        "unknown-client": authorize_query("itms_nobody", challenge=challenge),
        "two-clients": good + f"&client_id={app.client_id}",
        "no-redirect": good.replace("redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback&", ""),
        "unregistered-redirect": authorize_query(
            app.client_id, challenge=challenge, redirect_uri="https://evil.example/cb"
        ),
        "two-redirects": good + "&redirect_uri=https%3A%2F%2Fevil.example%2Fcb",
    }[case]
    for route in ("details", "decision"):
        resp = await env.api.client.post(
            f"/api/v1/oauth/authorize/{route}",
            json={"query": query, **({"approve": True} if route == "decision" else {})},
            headers=await env.api.headers(env.org_id, env.sub),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "refused", (route, body)
        assert "redirect_to" not in body
        assert body["detail"]
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        assert (await session.execute(select(OAuthCode))).scalars().all() == []


async def test_an_unreadable_metadata_document_is_shown(env: Env, monkeypatch) -> None:
    async def refused(client_id: str) -> cimd.ClientDocument:
        raise cimd.DocumentRefused("mismatch")

    monkeypatch.setattr(cimd, "fetch_document", refused)
    _, challenge = pkce_pair()
    body = (await _details(env, authorize_query(DOCUMENT_URL, challenge=challenge))).json()
    assert body["outcome"] == "refused"
    assert body["error"] == "invalid_client"
    assert "claude.example" in body["detail"]


async def test_a_refusal_is_in_the_persons_language(env: Env) -> None:
    _, challenge = pkce_pair()
    resp = await env.api.client.post(
        "/api/v1/oauth/authorize/details",
        json={"query": authorize_query("itms_nobody", challenge=challenge)},
        headers={**await env.api.headers(env.org_id, env.sub), "Accept-Language": "es"},
    )
    assert resp.json()["detail"] == (
        "esta aplicación de IA no está registrada aquí o su registro caducó. Vuelve a "
        "conectarla desde la aplicación."
    )


# --------------------------------------------------------------------------- #
# Everything else goes back to the app, with state and iss
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"code_challenge": "short"}, "invalid_request"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"resource": "https://itms.example.com"}, "invalid_target"),
    ],
)
async def test_a_bad_request_goes_back_to_the_app_with_state_and_iss(
    env: Env, change: dict[str, str], error: str
) -> None:
    app = await _registered(env)
    _, challenge = pkce_pair()
    body = (await _details(env, authorize_query(app.client_id, challenge=challenge, **change))).json()
    assert body["outcome"] == "redirect"
    assert body["redirect_to"].startswith(CLAUDE_CALLBACK + "?")
    params = _params(body["redirect_to"])
    assert params["error"] == [error]
    assert params["state"] == ["st4te"]
    assert params["iss"] == [BASE]
    assert "code" not in params


async def test_a_missing_method_is_plain_and_refused(env: Env) -> None:
    """RFC 7636 §4.3: no `code_challenge_method` means "plain"."""
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge).replace("&code_challenge_method=S256", "")
    params = _params((await _details(env, query)).json()["redirect_to"])
    assert params["error"] == ["invalid_request"]


async def test_a_request_with_no_resource_is_for_the_mcp_server(env: Env) -> None:
    """Plan Task 8, decision 1 (question P6)."""
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge).replace(
        "&resource=https%3A%2F%2Fitms.example.com%2Fmcp", ""
    )
    assert (await _details(env, query)).json()["outcome"] == "consent"


async def test_a_parameter_twice_goes_back_without_a_state_it_cannot_trust(env: Env) -> None:
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge) + "&state=other"
    params = _params((await _details(env, query)).json()["redirect_to"])
    assert params["error"] == ["invalid_request"]
    assert "state" not in params


# --------------------------------------------------------------------------- #
# Who may connect (plan D11)
# --------------------------------------------------------------------------- #


async def test_with_mcp_off_the_person_is_told_and_offered_the_way_back(api_env) -> None:
    org_id, _ = await oauth_org(api_env, mcp_enabled=False)
    sub = await api_env.create_user(org_id, "org-admin")
    app = await seed_client(api_env, org_id)
    _, challenge = pkce_pair()
    env = Env(api_env, org_id, sub)
    body = (await _details(env, authorize_query(app.client_id, challenge=challenge))).json()
    assert body["outcome"] == "denied"
    assert body["detail"].startswith("MCP is turned off for this organization.")
    assert _params(body["redirect_to"])["error"] == ["access_denied"]
    approved = (await decide(api_env, org_id, sub, authorize_query(app.client_id, challenge=challenge))).json()
    assert approved["outcome"] == "denied"


async def test_without_mcp_use_the_person_is_told(env: Env) -> None:
    technician = await env.api.create_user(env.org_id, "technician")
    app = await _registered(env)
    _, challenge = pkce_pair()
    body = (await _details(env, authorize_query(app.client_id, challenge=challenge), sub=technician)).json()
    assert body["outcome"] == "denied"
    assert body["detail"] == "requires mcp.use"


# --------------------------------------------------------------------------- #
# The decision
# --------------------------------------------------------------------------- #


async def test_approving_issues_a_code_the_token_endpoint_accepts(env: Env) -> None:
    app = await _registered(env)
    verifier, challenge = pkce_pair()
    resp = await decide(env.api, env.org_id, env.sub, authorize_query(app.client_id, challenge=challenge))
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["outcome"] == "redirect"
    params = _params(body["redirect_to"])
    assert params["state"] == ["st4te"] and params["iss"] == [BASE]
    token = await token_request(
        env.api,
        grant_type="authorization_code",
        code=code_of(body["redirect_to"]),
        redirect_uri=CLAUDE_CALLBACK,
        client_id=app.client_id,
        code_verifier=verifier,
    )
    assert token.status_code == 200, token.text
    async with AsyncSession(env.api.admin_engine) as session:
        (event,) = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == env.org_id, AuditEvent.action == "mcp.app_authorized"
                )
            )
        ).scalars()
    assert event.actor_type == "user"
    assert event.meta["app"] == app.client_id
    assert event.meta["redirect_host"] == "claude.ai"
    assert code_of(body["redirect_to"]) not in str(event.meta)


async def test_a_loopback_approval_returns_to_the_port_that_asked(env: Env) -> None:
    app = await _registered(env, "http://127.0.0.1/callback")
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge, redirect_uri="http://127.0.0.1:51004/callback")
    body = (await decide(env.api, env.org_id, env.sub, query)).json()
    assert body["redirect_to"].startswith("http://127.0.0.1:51004/callback?code=")


async def test_declining_goes_back_with_access_denied_and_issues_nothing(env: Env) -> None:
    app = await _registered(env)
    _, challenge = pkce_pair()
    body = (
        await decide(env.api, env.org_id, env.sub, authorize_query(app.client_id, challenge=challenge), approve=False)
    ).json()
    params = _params(body["redirect_to"])
    assert params["error"] == ["access_denied"]
    assert "code" not in params
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        assert (await session.execute(select(OAuthCode))).scalars().all() == []
    async with AsyncSession(env.api.admin_engine) as session:
        found = (
            await session.execute(select(AuditEvent).where(AuditEvent.action == "mcp.app_authorized"))
        ).scalars().all()
    assert found == []


# --------------------------------------------------------------------------- #
# Only the console's own session
# --------------------------------------------------------------------------- #


async def test_without_a_session_both_routes_are_401(env: Env) -> None:
    app = await _registered(env)
    _, challenge = pkce_pair()
    for route in ("details", "decision"):
        resp = await env.api.client.post(
            f"/api/v1/oauth/authorize/{route}",
            json={"query": authorize_query(app.client_id, challenge=challenge), "approve": True}
            if route == "decision"
            else {"query": authorize_query(app.client_id, challenge=challenge)},
            headers={"X-ITMS-Request": "1", "Origin": BASE},
        )
        assert resp.status_code == 401, (route, resp.text)


async def test_a_decision_without_the_consoles_header_is_csrf(env: Env) -> None:
    app = await _registered(env)
    _, challenge = pkce_pair()
    headers = {k: v for k, v in (await env.api.headers(env.org_id, env.sub)).items() if k != "X-ITMS-Request"}
    resp = await env.api.client.post(
        "/api/v1/oauth/authorize/decision",
        json={"query": authorize_query(app.client_id, challenge=challenge), "approve": True},
        headers=headers,
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "csrf"


async def test_an_ai_app_can_never_approve_an_ai_app(env: Env) -> None:
    """1c: the decision mints a code that becomes the person's MCP tokens, so it
    is never exposed; the details need a console session the channel never has."""
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge)
    async with marked_client(env.api) as client:
        decision = await client.post(
            "/api/v1/oauth/authorize/decision", json={"query": query, "approve": True}
        )
        details = await client.post("/api/v1/oauth/authorize/details", json={"query": query})
    assert decision.status_code == 403
    assert decision.json()["detail"] == "this action is never available to an AI app"
    assert details.status_code == 401
```

En `backend/tests/test_mcp_channel_api.py`, `OWN_SIGN_IN_NEVER_EXPOSED` gana, después de las dos de invitaciones:

```python
    # Own authentication phase 3 (plan 2026-11-06, Task 8): approving an AI app
    # answers its one-time authorization code.
    ("POST", "/api/v1/oauth/authorize/decision"),
```

y `assert len(NEVER_EXPOSED) == 36` pasa a `37` (delta **+1** sobre la base que vea esta tarea).

En `backend/tests/test_route_gating.py`, `UNGATED_BY_DESIGN` gana, después de `("DELETE", "/api/v1/me/sessions/{session_id}")`:

```python
        # THE CONSENT SCREEN (P2 phase 3, plan 2026-11-06 Task 8): the caller's
        # own console session (`oauth_consent.py::_console_ctx`); `mcp.use` and
        # the org switch are checked in the service (`consent._refusal`, plan
        # D11), so a person without them is told so and sent back to the AI
        # app with `access_denied` instead of a 403 that could not be.
        ("POST", "/api/v1/oauth/authorize/details"),
        # The same, and the only one that mints anything: a 60-second code for
        # what the person just approved. Never exposed over MCP.
        ("POST", "/api/v1/oauth/authorize/decision"),
```

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_consent.py tests/test_mcp_channel_api.py tests/test_route_gating.py`
Expected: FAIL (`404` en las rutas; `NEVER_EXPOSED` 36; `UNGATED_BY_DESIGN` no casa con el router).

- [ ] **Step 2: Los errores del punto de autorización**

Al final de `backend/app/core/auth/oauth/errors.py`:

```python
# The authorization endpoint's (RFC 6749 §4.1.2.1), sent back to the app in
# the redirect (Task 8).
AUTHZ_REPEATED = Problem("invalid_request", "a parameter appears more than once")
AUTHZ_RESPONSE_TYPE = Problem(
    "unsupported_response_type", "only response_type=code is supported"
)
AUTHZ_CHALLENGE = Problem(
    "invalid_request", "code_challenge is required: 43 base64url characters, method S256"
)
AUTHZ_METHOD = Problem("invalid_request", "code_challenge_method must be S256")
PERSON_CANNOT = Problem("access_denied", "this person cannot connect AI apps here")
DECLINED = Problem("access_denied", "the person declined the request")
```

- [ ] **Step 3: `authorize.py`**

`backend/app/core/auth/oauth/authorize.py`:

```python
"""An authorization request, as the consent screen hands it over (design
§5.2; plan D2, D8).

The console page `/oauth/authorize` passes the request's query here exactly
as the browser has it (Task 4). It is validated on EVERY call -- the details
and the decision both -- in RFC 6749 §4.1.2.1's two halves:

- what cannot be sent back because the address to send it to is not to be
  trusted: no client, an unknown client, a metadata document that cannot be
  this client, no redirect or one the client did not register ("MUST NOT
  automatically redirect the user-agent to the invalid redirection URI").
  `NotRedirectable`, worded for the person;
- everything else goes back to the app as an error redirect, carrying
  `state` (§4.1.2) and `iss` (RFC 9207 §2: "including error responses").
  `Redirectable`.

WHAT IS CHECKED: `client_id` and `redirect_uri` once each; the redirect one
the app registered (`redirects.matches`: exact, but for a loopback port);
then no other parameter twice (§3.1: "MUST NOT be included more than once"),
`resource` excepted (RFC 8707 §2 lets it repeat); `response_type=code`;
`code_challenge` of 43 base64url characters WITH `code_challenge_method=S256`
said out loud (RFC 7636 §4.3: a missing method "defaults to "plain""; §4.4.1:
`invalid_request`); every `resource` the MCP resource (`invalid_target`), and
none meaning it (Task 8, decision 1). `scope` is ignored beyond `mcp` (plan
D7): the token's scope is `mcp` whatever was asked.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from app.core.auth.oauth import cimd
from app.core.auth.oauth.clients import ClientView, resolve_client
from app.core.auth.oauth.errors import (
    AUTHZ_CHALLENGE,
    AUTHZ_METHOD,
    AUTHZ_REPEATED,
    AUTHZ_RESPONSE_TYPE,
    BAD_TARGET,
    Problem,
)
from app.core.auth.oauth.pkce import valid_challenge
from app.core.auth.oauth.policy import issuer
from app.core.auth.oauth.redirects import matches, with_params
from app.core.i18n import tr
from app.core.mcp_channel import mcp_resource

#: The parameter RFC 8707 §2 lets repeat.
_REPEATABLE = frozenset({"resource"})


@dataclass(frozen=True)
class AuthorizationRequest:
    client: ClientView
    redirect_uri: str
    state: str | None
    code_challenge: str
    resource: str


class NotRedirectable(Exception):
    """Shown to the person; never sent to the redirect. `error` is RFC 6749's
    code, `detail` a translated sentence."""

    def __init__(self, error: str, detail: str) -> None:
        super().__init__(error)
        self.error = error
        self.detail = detail


class Redirectable(Exception):
    """An error the app must hear: `redirect_to` is its redirect with `error`,
    `error_description`, `state` and `iss`."""

    def __init__(self, redirect_to: str) -> None:
        super().__init__("redirect")
        self.redirect_to = redirect_to


def parse_query(query: str) -> dict[str, list[str]]:
    """Every parameter's values; an empty one is absent (RFC 6749 §3.1)."""
    params: dict[str, list[str]] = {}
    for name, value in parse_qsl(query, keep_blank_values=True):
        if value:
            params.setdefault(name, []).append(value)
    return params


def answer(redirect_uri: str, state: str | None, iss: str, **params: str) -> str:
    """The redirect back to the app: its own query kept, then `params`, then
    `state` when the request had one, then `iss`."""
    extra = dict(params)
    if state is not None:
        extra["state"] = state
    extra["iss"] = iss
    return with_params(redirect_uri, extra)


def _host(client_id: str) -> str:
    try:
        return urlsplit(client_id).hostname or client_id
    except ValueError:
        return client_id


async def validate(app: Any, *, org_id: UUID, query: str, now: datetime) -> AuthorizationRequest:
    settings = app.state.settings
    iss, resource = issuer(settings), mcp_resource(settings)
    assert iss is not None and resource is not None  # consent._ready refused it otherwise
    params = parse_query(query)

    def once(name: str) -> str | None:
        values = params.get(name, [])
        return values[0] if len(values) == 1 else None

    if len(params.get("client_id", [])) > 1 or len(params.get("redirect_uri", [])) > 1:
        raise NotRedirectable(
            "invalid_request",
            tr("this connection request names its AI app or its return address twice"),
        )
    client_id = once("client_id")
    if client_id is None:
        raise NotRedirectable("invalid_request", tr("this connection request names no AI app"))
    try:
        client = await resolve_client(app, org_id=org_id, client_id=client_id, now=now)
    except cimd.DocumentRefused as exc:
        raise NotRedirectable(
            "invalid_client",
            tr("this AI app's description could not be read from {host}", host=_host(client_id)),
        ) from exc
    if client is None:
        raise NotRedirectable(
            "invalid_client",
            tr(
                "this AI app is not registered here, or its registration expired. Connect it "
                "again from the app."
            ),
        )
    redirect_uri = once("redirect_uri")
    if redirect_uri is None:
        raise NotRedirectable(
            "invalid_request", tr("this connection request names no return address")
        )
    if not matches(client.redirect_uris, redirect_uri):
        raise NotRedirectable(
            "invalid_redirect_uri",
            tr("the address this AI app asked to go back to is not one it registered"),
        )

    # From here on the answer goes back to the app.
    state = once("state")

    def back(problem: Problem) -> Redirectable:
        return Redirectable(
            answer(
                redirect_uri,
                state,
                iss,
                error=problem.error,
                error_description=problem.description,
            )
        )

    if any(len(values) > 1 for name, values in params.items() if name not in _REPEATABLE):
        state = None  # two states: neither is the app's to trust
        raise back(AUTHZ_REPEATED)
    if once("response_type") != "code":
        raise back(AUTHZ_RESPONSE_TYPE)
    challenge = once("code_challenge")
    if challenge is None or not valid_challenge(challenge):
        raise back(AUTHZ_CHALLENGE)
    if once("code_challenge_method") != "S256":
        raise back(AUTHZ_METHOD)
    if any(value != resource for value in params.get("resource", [])):
        raise back(BAD_TARGET)
    return AuthorizationRequest(
        client=client,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=challenge,
        resource=resource,
    )
```

(`back` cierra sobre `state`, así que poner `state = None` justo antes de lanzar el error de parámetros repetidos es
lo que hace que esa respuesta no lleve `state`; `test_a_parameter_twice_goes_back_without_a_state_it_cannot_trust` lo
fija.)

- [ ] **Step 4: `consent.py`**

`backend/app/core/auth/oauth/consent.py`:

```python
"""The consent screen's two questions (design §5.2; plan D11).

`details`: what the person is asked, or why there is nothing to ask.
`decide`: their answer. Both validate the request again (`authorize.validate`)
and both say first whether this person may connect AI apps here at all:
MCP switched on for the organization (`mcp_enabled`, `is True`) and `mcp.use`
held on some client -- the two checks `app/mcp/server.py::_gate` makes on
every MCP call, made here too so nobody approves a code that could never be
used. A refusal is `denied`, with the way back to the app (`access_denied`,
RFC 6749 §4.1.2.1).

AN APPROVAL ISSUES A 60-SECOND CODE (Task 7's `issue_code`) bound to the app,
the exact redirect, the PKCE challenge and the resource, and is audited
`mcp.app_authorized` (§7: "consentimientos ... de clientes de IA"), naming
the app and the host it returns to, never the code. Declining issues nothing
and is not audited (Task 8, decision 3).

WHAT THE PERSON IS SHOWN (`_client_out`): the app's name as it gave it, the
host its answer goes back to (MCP: "MUST clearly display the redirect URI
hostname" [S30]), the host that publishes its metadata document (CIMD-00
§6.4: "SHOULD display the hostname of the client_id"), and whether every
address it registered is this computer ([S30]: "SHOULD display additional
warnings for localhost-only redirect URIs").
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from sqlalchemy import select

from app.core.audit import audit_service
from app.core.auth.oauth.authorize import (
    AuthorizationRequest,
    NotRedirectable,
    Redirectable,
    answer,
    validate,
)
from app.core.auth.oauth.clients import touch
from app.core.auth.oauth.errors import DECLINED, PERSON_CANNOT
from app.core.auth.oauth.grants import issue_code
from app.core.auth.oauth.policy import SCOPE, issuer
from app.core.auth.oauth.redirects import host_of, is_loopback
from app.core.auth.oauth.store import oauth_session
from app.core.i18n import tr
from app.core.mcp_channel import mcp_resource
from app.core.rbac import codes_held_anywhere
from app.core.tenancy import org_session
from app.models import Organization
from app.models.oauth import OAuthClient

if TYPE_CHECKING:
    # The door's context, as the route hands it over; a type only, so the
    # core never imports the API layer at run time.
    from app.api.deps import AuthContext


def _ready(request: Request) -> str:
    settings = request.app.state.settings
    iss = issuer(settings)
    if iss is None or mcp_resource(settings) is None:
        raise HTTPException(
            503,
            tr("MCP is not available: ITS_PUBLIC_CONSOLE_URL is not an https address without a path."),
        )
    return iss


async def _checked(request: Request, ctx: AuthContext, query: str) -> AuthorizationRequest | dict[str, Any]:
    try:
        return await validate(
            request.app, org_id=ctx.org_id, query=query, now=datetime.now(UTC)
        )
    except NotRedirectable as refused:
        return {"outcome": "refused", "error": refused.error, "detail": refused.detail}
    except Redirectable as back:
        return {"outcome": "redirect", "redirect_to": back.redirect_to}


async def _refusal(request: Request, ctx: AuthContext) -> str | None:
    """Why this person may not connect AI apps here, or None (plan D11)."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        org_settings = (
            await session.execute(select(Organization.settings).where(Organization.id == ctx.org_id))
        ).scalar_one_or_none()
        if (org_settings or {}).get("mcp_enabled") is not True:
            return tr(
                "MCP is turned off for this organization. An org-admin turns it on in "
                "Settings → AI connections."
            )
        if "mcp.use" not in await codes_held_anywhere(session, ctx.user_id):
            return tr("requires {code}", code="mcp.use")
    return None


def _denied(checked: AuthorizationRequest, iss: str, detail: str) -> dict[str, Any]:
    return {
        "outcome": "denied",
        "detail": detail,
        "redirect_to": answer(
            checked.redirect_uri,
            checked.state,
            iss,
            error=PERSON_CANNOT.error,
            error_description=PERSON_CANNOT.description,
        ),
    }


def _client_out(checked: AuthorizationRequest) -> dict[str, Any]:
    client = checked.client
    return {
        "name": client.client_name,
        "client_id": client.client_id,
        "kind": client.kind,
        "client_host": urlsplit(client.client_id).hostname
        if client.kind == "metadata_document"
        else None,
        "redirect_host": host_of(checked.redirect_uri),
        "loopback_only": all(is_loopback(uri) for uri in client.redirect_uris),
    }


async def details(request: Request, ctx: AuthContext, *, query: str) -> dict[str, Any]:
    iss = _ready(request)
    checked = await _checked(request, ctx, query)
    if isinstance(checked, dict):
        return checked
    refusal = await _refusal(request, ctx)
    if refusal is not None:
        return _denied(checked, iss, refusal)
    return {"outcome": "consent", "client": _client_out(checked), "scope": SCOPE, "account": ctx.email}


async def decide(request: Request, ctx: AuthContext, *, query: str, approve: bool) -> dict[str, Any]:
    iss = _ready(request)
    checked = await _checked(request, ctx, query)
    if isinstance(checked, dict):
        return checked
    if not approve:
        return {
            "outcome": "redirect",
            "redirect_to": answer(
                checked.redirect_uri,
                checked.state,
                iss,
                error=DECLINED.error,
                error_description=DECLINED.description,
            ),
        }
    refusal = await _refusal(request, ctx)
    if refusal is not None:
        return _denied(checked, iss, refusal)
    # Question P14: `require_step_up(ctx)` here if the operator wants a recent
    # confirmation of the second factor before an AI app is approved.
    now = datetime.now(UTC)
    async with oauth_session(ctx.org_id, request.app.state.app_engine) as session:
        client = await session.get(OAuthClient, checked.client.id)
        if client is None:  # removed as idle between the details and the press
            code = None
        else:
            code = await issue_code(
                session,
                org_id=ctx.org_id,
                user_id=ctx.user_id,
                oauth_client_id=client.id,
                redirect_uri=checked.redirect_uri,
                code_challenge=checked.code_challenge,
                resource=checked.resource,
                now=now,
            )
            touch(client, now)
            await audit_service.record(
                session,
                org_id=ctx.org_id,
                actor_type="user",
                actor_id=ctx.user_id,
                action="mcp.app_authorized",
                target_type="user",
                target_id=str(ctx.user_id),
                metadata={
                    "app": client.client_id,
                    "name": client.client_name or "",
                    "redirect_host": host_of(checked.redirect_uri),
                },
            )
    if code is None:
        return {
            "outcome": "refused",
            "error": "invalid_client",
            "detail": tr(
                "this AI app is not registered here, or its registration expired. Connect it "
                "again from the app."
            ),
        }
    return {
        "outcome": "redirect",
        "redirect_to": answer(checked.redirect_uri, checked.state, iss, code=code),
    }
```

- [ ] **Step 5: Las rutas**

`backend/app/api/v1/oauth_consent.py`:

```python
"""The consent screen's routes (own authentication design §5.2; plan
2026-11-06, Task 8):

    POST /api/v1/oauth/authorize/details    what an AI app's request asks
    POST /api/v1/oauth/authorize/decision   the person's answer: a code, or access_denied

UNGATED BY DESIGN (`tests/test_route_gating.py::UNGATED_BY_DESIGN`): the
caller's own console session -- not a token, not the assistant's internal
token (`_console_ctx`) -- and `mcp.use` and the org switch checked in the
service, so a refusal can go back to the AI app (`consent._refusal`, plan
D11). The CSRF rule (`app/core/auth/csrf.py`) holds both: a POST under
`/api/v1` on the session cookie.

NEVER AN AI APP'S (1c): the decision answers a one-time code that becomes the
person's MCP tokens, so it is in `NEVER_EXPOSED` and refused on the MCP
channel twice; the details are not a tool (`NOT_FOR_MCP`), and need the
console session the channel never has.

THE HANDLERS ARE THIN: the rules live in `app.core.auth.oauth.consent`, which
holds the every-client scope a route file may not (`ALL_SCOPE_ALLOWLIST`).
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AuthContext, bound_auth_context
from app.core.auth.guards import sign_in_again
from app.core.auth.oauth import consent
from app.core.auth.oauth.errors import no_store

router = APIRouter(prefix="/oauth", tags=["oauth"])


class ConsentQueryIn(BaseModel):
    """The query of `/oauth/authorize`, as the browser has it, without the `?`."""

    model_config = ConfigDict(extra="forbid")
    query: str = Field(max_length=8192)


class ConsentDecisionIn(ConsentQueryIn):
    approve: bool


async def _console_ctx(ctx: AuthContext = Depends(bound_auth_context)) -> AuthContext:
    """The door admitted somebody; these routes need it to have been the
    console session (`ctx.principal`), as `/me/security` does."""
    if ctx.principal is None:
        raise sign_in_again()
    return ctx


@router.post("/authorize/details")
async def authorize_details(
    body: ConsentQueryIn, request: Request, ctx: AuthContext = Depends(_console_ctx)
) -> JSONResponse:
    """What an AI app's authorization request asks of the signed-in person."""
    return no_store(await consent.details(request, ctx, query=body.query))


@router.post("/authorize/decision")
async def authorize_decision(
    body: ConsentDecisionIn, request: Request, ctx: AuthContext = Depends(_console_ctx)
) -> JSONResponse:
    """The person's answer: back to the app with a one-time code, or with
    `access_denied`."""
    return no_store(await consent.decide(request, ctx, query=body.query, approve=body.approve))
```

En `backend/app/main.py`, junto a los demás `from app.api.v1… import router as …_router` y después de
`app.include_router(session_settings_router, prefix="/api/v1")`:

```python
    from app.api.v1.oauth_consent import router as oauth_consent_router

    app.include_router(oauth_consent_router, prefix="/api/v1")
```

En `backend/app/mcp/exclusions.py`, en `NEVER_EXPOSED_REASONS`, después de la entrada de
`("POST", "/api/v1/users/{user_id}/invite/resend")`:

```python
    # The consent screen (P2 phase 3): approving an AI app answers the code
    # that becomes the person's MCP tokens -- an AI app must never approve
    # one, itself or another (1c(c)).
    ("POST", "/api/v1/oauth/authorize/decision"): (
        "approves an AI app and returns its one-time authorization code, which becomes the "
        "person's MCP tokens"
    ),
```

y en `NOT_FOR_MCP`, después de las de My security:

```python
    # The consent screen's question (P2 phase 3): the person's, in the console
    # with their session (`api/v1/oauth_consent.py::_console_ctx`).
    ("POST", "/api/v1/oauth/authorize/details"): (
        "the consent screen is the person's, asked in the console with their own session"
    ),
```

En `backend/app/core/i18n/es.py`, junto a la entrada de `"the assistant's internal token was refused"`:

```python
    "this connection request names its AI app or its return address twice": (
        "esta solicitud de conexión nombra dos veces su aplicación de IA o su dirección de vuelta"
    ),
    "this connection request names no AI app": (
        "esta solicitud de conexión no nombra ninguna aplicación de IA"
    ),
    "this AI app's description could not be read from {host}": (
        "no se pudo leer la descripción de esta aplicación de IA en {host}"
    ),
    "this AI app is not registered here, or its registration expired. Connect it again from "
    "the app.": (
        "esta aplicación de IA no está registrada aquí o su registro caducó. Vuelve a "
        "conectarla desde la aplicación."
    ),
    "this connection request names no return address": (
        "esta solicitud de conexión no nombra ninguna dirección de vuelta"
    ),
    "the address this AI app asked to go back to is not one it registered": (
        "la dirección a la que esta aplicación de IA pidió volver no es una de las que registró"
    ),
```

En `frontend/src/i18n/en/auditVocabulary.ts`, en orden alfabético: `mcp_app_authorized: "MCP app authorized",`; y en
`frontend/src/i18n/es/auditVocabulary.ts`: `mcp_app_authorized: "Aplicación de IA autorizada",`.

- [ ] **Step 6: Las pruebas pasan**

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_consent.py tests/test_mcp_channel_api.py tests/test_route_gating.py tests/test_mcp_catalog.py tests/test_mcp_descriptions.py tests/test_ai_tool_surface.py tests/test_i18n_catalog.py tests/test_client_catalog.py tests/test_security_headers.py`
Expected: PASS.

Run: `cd frontend && npx vitest run src/modules/audit src/i18n`
Expected: PASS.

Run: `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `Success: no issues found` y `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/core/auth/oauth/errors.py backend/app/core/auth/oauth/authorize.py \
  backend/app/core/auth/oauth/consent.py backend/app/api/v1/oauth_consent.py backend/app/main.py \
  backend/app/mcp/exclusions.py backend/app/core/i18n/es.py backend/tests/oauth_support.py \
  backend/tests/test_oauth_consent.py backend/tests/test_mcp_channel_api.py backend/tests/test_route_gating.py \
  frontend/src/i18n/en/auditVocabulary.ts frontend/src/i18n/es/auditVocabulary.ts
git commit -m "feat(oauth): the consent screen's routes: validate, ask, approve with a one-time code"
```

### Task 9: La puerta de MCP con el token propio; pruebas MCP re-apuntadas

Diseño §5.3, §6.5; D6. Oleada 4, en paralelo con la Task 8. Backend.

`deps.admit`, en el canal MCP, deja de validar el token de Keycloak y verifica el token de acceso propio (Task 2), la
persona por su id y la concesión que el token nombra, en cada llamada (D6). No escribe nada: ni fila `users` nueva
(el autoaprovisionamiento por `sub`), ni `auth.login` de primera vez, ni el correo refrescado desde el claim, ni
`last_login_at`. La persona tuvo que iniciar sesión en la consola y aprobar la app (§5.2), así que su fila ya existe, y
el token no lleva correo, nombre ni slug de los que aprovisionar (Task 2, decisión 2). `/mcp` sigue igual por fuera
(el 401 con el reto, el interruptor, `mcp.use`, `mcp_revocation`); solo cambia quién emitió el token.

Como el cambio de la puerta deja sin valor todo token de Keycloak en el canal, **la misma tarea** cambia el ayudante de
las pruebas (`tests/mcp_support.py::mcp_token`, que pasa a ser `async`) y re-apunta cada prueba que fijaba el token de
Keycloak en la puerta de MCP. Las que prueban funciones de Keycloak que siguen existiendo (`security.validate_token`,
`require_person_mcp_token`, `kc_client.revoke_consent`) no se tocan.

**Files:**
- Modify: `backend/app/api/deps.py` (la rama MCP de `admit`; los tipos de `AuthContext.claims` y `Admission.claims`)
- Modify: `backend/app/mcp/server.py` (`McpPrincipal.claims`, la etiqueta de `McpAsgi`, el 503 sin emisor, docstrings)
- Modify: `backend/app/core/i18n/es.py` (una entrada nueva, dos que dejan de usarse)
- Modify: `backend/tests/mcp_support.py` (`mcp_token` asíncrono; `mcp_app`, `mcp_claims`, `signed_mcp_token`)
- Create: `backend/tests/test_mcp_own_tokens.py`
- Modify (re-apuntadas, Step 6): `backend/tests/test_mcp_server.py`, `backend/tests/test_mcp_channel_api.py`,
  `backend/tests/test_token_origin.py`, `backend/tests/test_domain_gate_doors.py`, `backend/tests/test_domain_policy.py`,
  `backend/tests/test_clients_api.py`, `backend/tests/test_me_profile.py`, `backend/tests/test_auth_falsifiers.py`,
  `backend/tests/test_mcp_admin_api.py` (solo `_old_token` y `_call_a_tool`; el resto es de la Task 10)
- Modify (mecánicas, Step 7): `backend/tests/test_mcp_tools.py`, `backend/tests/test_deploy_window.py`,
  `backend/tests/test_backup_policy_window.py`, `backend/tests/test_client_scoped_config.py`,
  `backend/tests/test_auth_reset_needs_recovery.py`
- Modify: `backend/tests/test_route_session_discipline.py` (`RAW_SESSIONS_BY_DESIGN` pierde `api/deps.py::admit`)
- Modify: `backend/tests/test_client_catalog.py` (el motivo de `users_org_id_external_subject_key` en `ORG_WIDE_KEYS`)

**Interfaces:**
- Consumes: Task 1 (`policy.issuer`, `policy.ACCESS_TYP`), Task 2 (`access.verify_access_token`, `access.mint_access_token`,
  `access.unverified_client_id`, `McpGrant` con `azp`, `iat`, `email`), Task 5 (`OAuthClient`, `oauth_session`),
  Task 6 (la entrada de `es.py` «MCP is not available: ITS_PUBLIC_CONSOLE_URL is not an https address without a path.»
  y el import de `issuer` en `app/mcp/server.py`), Task 7 (`grants.open_family`, `grants.family_is_live`,
  `grants.revoke_family`). Existentes: `keys.sign_jwt(claims, *, typ)`, `tests.session_support.user_id_of(env, sub)`,
  `ApiEnv.create_user` (correo `f"{sub}@test.local"`), `tests.mcp_support.mcp_org`, `marked_client`, `mcp_client`,
  `running_mcp`.
- Produces: `AuthContext.claims: McpGrant | None` y `Admission.claims: McpGrant | None` (el nombre del campo no cambia:
  sus lectores usan `.azp`, `.iat` y `.email`, que `McpGrant` tiene); `McpPrincipal.claims: McpGrant`; la etiqueta de
  puerta `"mcp"` en `log_admission_refusal`; en `tests/mcp_support.py`: `async def mcp_token(api_env, org_slug: str,
  sub: str, azp: str = "claude", *, iat: int | None = None) -> str`, `async def mcp_app(api_env, org_id: UUID,
  client_id: str = "claude") -> UUID` (la Task 10 lo usa), `def mcp_claims(token: str) -> dict[str, Any]`,
  `async def signed_mcp_token(claims: Mapping[str, Any]) -> str`.

**Decisión de la tarea (1): la puerta no aprovisiona ni refresca.** §6.1: «`users.external_subject` deja de identificar
a nadie»; §5.2: «La persona tiene que haber iniciado sesión en la consola». El token nombra a la persona por
`users.id`; una persona sin fila es un 401, nunca una fila nueva. El refresco del correo desde el claim (falsificadores
13 y 14 de `docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §7.2) no tiene claim del que leer: la
fila es la única fuente, la misma que la regla de dominio consulta, que era lo que §7.2 pedía. Las cinco pruebas de
`test_me_profile.py` se re-apuntan a eso (Step 6) y la Task 12 enmienda §7.2.

**Decisión de la tarea (2): las reglas de la cuenta, en el orden de `_admit_session`, con la etiqueta `"mcp"`.** La
puerta de MCP lee la fila como la de la sesión: dominio (no a un contacto), `users.status`, contacto (salvo
`contacts=True`). Se copia el orden y no se comparte el código, para no tocar la rama de la sesión (Puntos de encuentro
con la fase 2). El log de un rechazo dice `door="mcp"` y `sub="user:<id>"`, como la sesión; antes decía `"http"` y el
`sub` de Keycloak.

**Decisión de la tarea (3): el marcador del canal tiene que nombrar la app del token.** Una llamada en proceso
(`app/mcp/invoke.py`) marca `azp=principal.claims.azp`, ya verificado; `McpAsgi` compara su etiqueta después. La puerta
lo comprueba también (`call.azp != grant.client_id` es el 401 «the MCP channel marker is malformed»), para que el
`mcp_app` de cada evento de auditoría escrito durante la llamada sea el de la concesión.

**Decisión de la tarea (4): `JwksCache` y `deps.jwks` se quedan.** Desde aquí ninguna puerta los llama; los borra la
fase 4 con `app.state.jwks` («Lo que deja», punto 3). Efecto visible: la sonda `jwks` lee una caché que ya no se llena y
la página de estado dirá «degradado» hasta la fase 4 (pregunta P12).

- [ ] **Step 1: Los ayudantes de prueba**

En `backend/tests/mcp_support.py`, los imports pasan a ser:

```python
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import httpx2
import jwt
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import keys
from app.core.auth.oauth import grants
from app.core.auth.oauth.access import mint_access_token
from app.core.auth.oauth.policy import ACCESS_TYP
from app.core.auth.oauth.store import oauth_session
from app.core.mcp_channel import EXTENSION_KEY, mcp_resource
from app.models import Organization
from app.models.oauth import OAuthClient
from tests.session_support import user_id_of
```

y `mcp_token` se sustituye por:

```python
#: Anthropic: the hosted Claude apps "register exactly this redirect URI" [S33].
_CALLBACK = "https://claude.ai/api/mcp/auth_callback"


async def mcp_app(api_env: Any, org_id: UUID, client_id: str = "claude") -> UUID:
    """The organization's AI app `client_id`, registered once -- what
    `POST /oauth/register` leaves behind -- as its `oauth_clients.id`."""
    now = datetime.now(UTC)
    async with oauth_session(org_id, api_env.app_engine) as session:
        found: UUID | None = (
            await session.execute(
                select(OAuthClient.id).where(
                    OAuthClient.org_id == org_id, OAuthClient.client_id == client_id
                )
            )
        ).scalar_one_or_none()
        if found is not None:
            return found
        row = OAuthClient(
            org_id=org_id,
            client_id=client_id,
            kind="registered",
            client_name=client_id,
            redirect_uris=[_CALLBACK],
            created_at=now,
            last_used_at=now,
        )
        session.add(row)
        await session.flush()
        return row.id


async def mcp_token(
    api_env: Any, org_slug: str, sub: str, azp: str = "claude", *, iat: int | None = None
) -> str:
    """What `/oauth/token` hands the AI app `azp` after `sub` approved it
    (autenticación propia design §5.3; plan 2026-11-06, Task 9): the app's
    registration, an open grant, and the ten-minute access token naming both,
    issued at `iat` (now when None). The consent screen and the code exchange
    are `tests/test_oauth_consent.py` and `tests/test_oauth_token.py`; this is
    their result, for the tests of what an MCP call may do."""
    async with AsyncSession(api_env.admin_engine) as session:
        org_id: UUID = (
            await session.execute(select(Organization.id).where(Organization.slug == org_slug))
        ).scalar_one()
    user_id = await user_id_of(api_env, sub)
    app_id = await mcp_app(api_env, org_id, azp)
    resource = mcp_resource(api_env.settings)
    assert resource is not None, "set ITS_PUBLIC_CONSOLE_URL before minting an MCP token"
    async with oauth_session(org_id, api_env.app_engine) as session:
        family, _ = await grants.open_family(
            session,
            org_id=org_id,
            user_id=user_id,
            oauth_client_id=app_id,
            resource=resource,
            now=datetime.now(UTC),
        )
        family_id = family.id
    token, _ = await mint_access_token(
        settings=api_env.settings,
        user_id=user_id,
        org_id=org_id,
        client_id=azp,
        family_id=family_id,
        now=iat,
    )
    return token


def mcp_claims(token: str) -> dict[str, Any]:
    """A token's claims, unverified: where a test that changes one starts."""
    claims: dict[str, Any] = jwt.decode(token, options={"verify_signature": False})
    return claims


async def signed_mcp_token(claims: Mapping[str, Any]) -> str:
    """`claims` signed by the platform's own key as an MCP access token: for
    the tests of what the door checks beyond the signature."""
    return await keys.sign_jwt(claims, typ=ACCESS_TYP)
```

`MCP_AUD` se queda: varias pruebas lo usan para fabricar el token **de Keycloak** que la puerta tiene que rechazar.

- [ ] **Step 2: La prueba que falla**

`backend/tests/test_mcp_own_tokens.py`:

```python
"""The MCP door with the platform's own access token (autenticación propia
design §5.3, §6.5; plan 2026-11-06, Task 9).

What is pinned here:

- a token the platform issued after the person's consent is admitted at
  `/mcp` and on an in-process call, as that person, with their row's address;
- a Keycloak token is refused whatever its audience (§5.3: "otro token se
  rechaza"), and so is one of ours for any audience but the MCP resource;
- the grant the token names is read on every call (plan D6): ending it stops
  the next call, not the next token; another app's or another person's grant
  admits nobody;
- the channel marker must name the token's app;
- the door provisions nobody and writes nothing about the person;
- with no https console there is no issuer, and the channel says so (503).
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from mcp.types import LATEST_PROTOCOL_VERSION
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.oauth import grants
from app.core.auth.oauth.access import verify_access_token
from app.core.auth.oauth.store import oauth_session
from app.models import AuditEvent, User
from tests.mcp_support import (
    MCP_AUD,
    MCP_BASE,
    marked_client,
    mcp_claims,
    mcp_client,
    mcp_org,
    mcp_token,
    running_mcp,
    signed_mcp_token,
)
from tests.session_support import user_id_of

METADATA_URL = f"{MCP_BASE}/.well-known/oauth-protected-resource/mcp"
REFUSED_CHALLENGE = f'Bearer resource_metadata="{METADATA_URL}", error="invalid_token"'
REFUSED_EN = "this access token was refused"
REFUSED_ES = "se rechazó este token de acceso"
REVOKED_EN = "this AI app's access was revoked; connect it again to continue"
MARKER_EN = "the MCP channel marker is malformed"
NO_ISSUER_EN = (
    "MCP is not available: ITS_PUBLIC_CONSOLE_URL is not an https address without a path."
)
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": LATEST_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0"},
    },
}


@pytest.fixture()
async def mcp_env(api_env):
    """`api_env` with the console the MCP gate answers at (`MCP_BASE`)."""
    api_env.settings.public_console_url = MCP_BASE
    return api_env


async def _admin(env: Any) -> tuple[UUID, str, str]:
    org_id, slug = await mcp_org(env)
    return org_id, slug, await env.create_user(org_id, "org-admin")


async def _initialize(env: Any, token: str, **extra: str) -> Any:
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {token}", **extra}
    async with mcp_client(env) as client:
        return await client.post("/mcp", json=INITIALIZE, headers=headers)


async def _me(env: Any, token: str, *, azp: str = "claude") -> Any:
    """The in-process call a tool makes (`app/mcp/invoke.py`), marked for `azp`."""
    async with marked_client(env, azp=azp) as client:
        return await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})


async def _users(env: Any, org_id: UUID) -> int:
    async with AsyncSession(env.admin_engine) as session:
        return (
            await session.execute(
                select(func.count()).select_from(User).where(User.org_id == org_id)
            )
        ).scalar_one()


async def _auth_events(env: Any, org_id: UUID) -> list[str]:
    async with AsyncSession(env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent.action).where(
                        AuditEvent.org_id == org_id, AuditEvent.action.like("auth.%")
                    )
                )
            ).scalars()
        )


async def test_a_token_the_platform_issued_is_admitted_as_the_person(mcp_env) -> None:
    org_id, slug, sub = await _admin(mcp_env)
    token = await mcp_token(mcp_env, slug, sub)
    async with running_mcp(mcp_env):
        initialized = await _initialize(mcp_env, token)
    me = await _me(mcp_env, token)
    assert initialized.status_code == 200, initialized.text
    assert me.status_code == 200, me.text
    assert me.json()["user_id"] == str(await user_id_of(mcp_env, sub))
    assert me.json()["email"] == f"{sub}@test.local"


async def test_a_keycloak_token_is_refused_whatever_its_audience(mcp_env) -> None:
    """Both carry a `client_id` claim, so the gate's label is not what refuses
    them: the signature is not the platform's."""
    _, slug, sub = await _admin(mcp_env)
    for token in (
        mcp_env.idp.token(
            sub, slug, aud=MCP_AUD, azp="claude", email=f"{sub}@test.local",
            extra={"client_id": "claude"},
        ),
        mcp_env.idp.token(sub, slug, email=f"{sub}@test.local", extra={"client_id": "claude"}),
    ):
        resp = await _initialize(mcp_env, token)
        resp_es = await _initialize(mcp_env, token, **{"Accept-Language": "es"})
        me = await _me(mcp_env, token)
        assert resp.status_code == 401, resp.text
        assert resp.headers["www-authenticate"] == REFUSED_CHALLENGE
        assert resp.json()["detail"] == REFUSED_EN
        assert resp_es.json()["detail"] == REFUSED_ES
        assert (me.status_code, me.json()["detail"]) == (401, REFUSED_EN)


@pytest.mark.parametrize(
    "aud",
    [MCP_BASE, [MCP_AUD, MCP_BASE], "https://elsewhere.example/mcp"],
    ids=["the-console", "two-audiences", "another-server"],
)
async def test_one_of_our_tokens_for_another_audience_is_refused(mcp_env, aud: Any) -> None:
    _, slug, sub = await _admin(mcp_env)
    genuine = await mcp_token(mcp_env, slug, sub)
    claims = mcp_claims(genuine)
    claims["aud"] = aud
    async with running_mcp(mcp_env):
        refused = await _initialize(mcp_env, await signed_mcp_token(claims))
        # Non-vacuity: the same claims, re-signed unchanged, are admitted.
        control = await _initialize(mcp_env, await signed_mcp_token(mcp_claims(genuine)))
    assert refused.status_code == 401
    assert refused.json()["detail"] == REFUSED_EN
    assert control.status_code == 200, control.text


async def test_ending_the_grant_stops_the_next_call(mcp_env) -> None:
    org_id, slug, sub = await _admin(mcp_env)
    token = await mcp_token(mcp_env, slug, sub)
    family_id = UUID(mcp_claims(token)["fam"])
    async with running_mcp(mcp_env):
        before = await _initialize(mcp_env, token)
        async with oauth_session(org_id, mcp_env.app_engine) as session:
            assert await grants.revoke_family(
                session, family_id, "revoked_in_settings", datetime.now(UTC)
            )
        after = await _initialize(mcp_env, token)
    me = await _me(mcp_env, token)
    assert before.status_code == 200, before.text
    assert after.status_code == 401
    assert after.headers["www-authenticate"] == REFUSED_CHALLENGE
    assert after.json()["detail"] == REVOKED_EN
    assert (me.status_code, me.json()["detail"]) == (401, REVOKED_EN)
    # The token is still a genuine one: what ended is the grant it names.
    assert await verify_access_token(token, settings=mcp_env.settings) is not None


async def test_another_apps_or_another_persons_grant_admits_nobody(mcp_env) -> None:
    org_id, slug, sub = await _admin(mcp_env)
    colleague = await mcp_env.create_user(org_id, "org-admin")
    other_app = mcp_claims(await mcp_token(mcp_env, slug, sub, "cursor"))
    theirs = mcp_claims(await mcp_token(mcp_env, slug, colleague))
    for fam in (other_app["fam"], theirs["fam"]):
        claims = mcp_claims(await mcp_token(mcp_env, slug, sub))
        claims["fam"] = fam
        resp = await _initialize(mcp_env, await signed_mcp_token(claims))
        assert resp.status_code == 401
        assert resp.json()["detail"] == REVOKED_EN


async def test_the_channel_marker_must_name_the_tokens_app(mcp_env) -> None:
    _, slug, sub = await _admin(mcp_env)
    token = await mcp_token(mcp_env, slug, sub)
    other = await _me(mcp_env, token, azp="cursor")
    own = await _me(mcp_env, token)
    assert (other.status_code, other.json()["detail"]) == (401, MARKER_EN)
    assert own.status_code == 200, own.text


async def test_a_token_for_nobody_provisions_nobody(mcp_env) -> None:
    org_id, slug, sub = await _admin(mcp_env)
    claims = mcp_claims(await mcp_token(mcp_env, slug, sub))
    claims["sub"] = str(uuid4())
    token = await signed_mcp_token(claims)
    before = await _users(mcp_env, org_id)
    resp = await _initialize(mcp_env, token)
    me = await _me(mcp_env, token)
    assert (resp.status_code, resp.json()["detail"]) == (401, REFUSED_EN)
    assert (me.status_code, me.json()["detail"]) == (401, REFUSED_EN)
    assert await _users(mcp_env, org_id) == before
    assert await _auth_events(mcp_env, org_id) == []


async def test_an_mcp_call_writes_nothing_about_the_person(mcp_env) -> None:
    org_id, slug, sub = await _admin(mcp_env)
    token = await mcp_token(mcp_env, slug, sub)

    async def row() -> tuple[Any, ...]:
        async with AsyncSession(mcp_env.admin_engine) as session:
            user = (
                await session.execute(select(User).where(User.external_subject == sub))
            ).scalar_one()
            return (user.email, user.display_name, user.last_login_at, user.status)

    seen = await row()
    async with running_mcp(mcp_env):
        for _ in range(2):
            assert (await _initialize(mcp_env, token)).status_code == 200
    assert (await _me(mcp_env, token)).status_code == 200
    assert await row() == seen
    assert await _auth_events(mcp_env, org_id) == []


async def test_without_an_https_console_there_is_no_issuer_and_mcp_is_503(mcp_env) -> None:
    _, slug, sub = await _admin(mcp_env)
    token = await mcp_token(mcp_env, slug, sub)
    mcp_env.settings.public_console_url = "http://itms.example.com"
    resp = await _initialize(mcp_env, token)
    me = await _me(mcp_env, token)
    assert (resp.status_code, resp.json()["detail"]) == (503, NO_ISSUER_EN)
    assert (me.status_code, me.json()["detail"]) == (503, NO_ISSUER_EN)
```

Run: `cd backend && .venv/bin/pytest -q tests/test_mcp_own_tokens.py`
Expected: FAIL (la puerta aún valida contra Keycloak: el token propio es 401 «signature verification failed» o
parecido, y los rechazos esperados dicen otra cosa).

- [ ] **Step 3: La puerta**

En `backend/app/api/deps.py`:

1. Imports. `from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession` pasa a `from sqlalchemy.ext.asyncio import
   AsyncEngine`; se quita `from app.core.audit import audit_service`; `from app.models import Organization, User` pasa a
   `from app.models import User`; el bloque de `app.core.security` pasa a `from app.core.security import JwksCache` (lo
   usa `jwks()`, decisión 4); y se añaden, en su orden alfabético entre los de `app.core`:

```python
from app.core.auth.oauth import access, grants
from app.core.auth.oauth.access import McpGrant
from app.core.auth.oauth.policy import issuer
```

2. En `AuthContext`, el comentario y el tipo del campo:

```python
    # HOW THE PERSON GOT IN -- exactly one of the two (P2 design §6.5). The
    # console's own session (`principal`, the `__Host-itms_session` cookie), or
    # on the MCP channel the grant an access token of the platform's own OAuth
    # server names (`claims`, own authentication phase 3: `McpGrant`, with the
    # address of the person's row). Read the person's address through `email`,
    # which works for both (D14).
    claims: McpGrant | None
```

y en el docstring de `email`: «the `users` row's for a session, and for an MCP grant too (the door copies it from the
row; the token carries none)». En `Admission`, `claims: McpGrant | None`.

3. En `admit`, desde `call = _mcp_call(request)` hasta el `return Admission(...)` final de la rama MCP (todo lo que
   sigue a `if loopback.claims_loopback(request): ...` en el canal, incluida la consulta por slug en el motor de
   administración), el cuerpo pasa a ser:

```python
    call = _mcp_call(request)
    settings = request.app.state.settings
    if call is not None:
        if mcp_resource(settings) is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                tr("MCP is not available: ITS_PUBLIC_CONSOLE_URL is not set."),
            )
        # The platform's OAuth server has no issuer without an https console
        # with no path (plan D1): no token of it can exist, and the channel says
        # so as `/mcp` and its metadata do.
        if issuer(settings) is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                tr(
                    "MCP is not available: ITS_PUBLIC_CONSOLE_URL is not an https address "
                    "without a path."
                ),
            )
        # The second of the two never-exposed refusals; `McpNeverExposedGuard`
        # is the first, and also covers the routes that never authenticate.
        if id(request.scope.get("route")) in request.app.state.mcp_never_route_ids:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, tr("this action is never available to an AI app")
            )
    # THE CONSOLE'S OWN SESSION (P2 design §6.5), and never on the MCP
    # channel: a cookie the browser holds is not a grant an AI app was given
    # (invariant 1c(a)). Nor is the assistant's internal token, which
    # `_session_principal` takes first and `verify_loopback` refuses on an MCP
    # call anyway.
    if call is None:
        # Off the MCP channel only the session admits (own authentication
        # phase 1, Task 13): the console no longer sends a bearer, and the door
        # no longer reads one.
        principal = await _session_principal(request)
        if principal is None:
            raise AuthRefusal(
                status.HTTP_401_UNAUTHORIZED, code=AUTH_REQUIRED, detail=tr("sign in to continue")
            )
        return await _admit_session(request, principal, contacts=contacts)
    if loopback.claims_loopback(request):
        # The two in-process channels never meet (1c(a): on MCP, only OAuth
        # makes a session).
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, tr("the assistant's internal token was refused")
        )

    # ON THE MCP CHANNEL, AN ACCESS TOKEN THE PLATFORM ISSUED (own authentication
    # design §5.3, §6.5; plan 2026-11-06, Task 9). `verify_access_token` fixes
    # everything (RFC 8725): the platform's key, `typ` at+jwt, the console as
    # issuer, the MCP resource as the ONLY audience (1c(a): "carrying the MCP
    # audience and no other platform audience"), ten minutes at most. A token
    # anybody else issued -- Keycloak's included -- reaches no table.
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise AuthRefusal(
            status.HTTP_401_UNAUTHORIZED, code=AUTH_REQUIRED, detail=tr("missing bearer token")
        )
    grant = await access.verify_access_token(
        authorization.removeprefix("Bearer "), settings=settings
    )
    if grant is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, tr("this access token was refused"))
    # The marker names the app every audit event of this call is attributed to
    # (`mcp_app`); it must be the app the grant was given to (Task 9, decision 3).
    if call.azp != grant.client_id:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, tr("the MCP channel marker is malformed")
        )

    # Read before the tenant session: a malformed header is the caller's
    # mistake, answered before anything is read on their behalf.
    requested = requested_client(request)

    # EVERY CLIENT, AND ONLY FOR THIS (spec §2.2, "Resolver quién es el
    # técnico"): the person's row, their grant and their grants of roles are
    # read before their scope exists, because the scope is computed from
    # exactly these rows. The organization is the token's, which the platform
    # signed: no slug, no admin engine.
    #
    # NOTHING IS WRITTEN (Task 9, decision 1). No `users` row is provisioned --
    # a grant exists only for a person who signed in to the console and
    # approved the app (design §5.2) -- no address is refreshed from a claim
    # the token does not carry, and no login is recorded: the consent was one.
    async with org_session(
        grant.org_id, engine=request.app.state.app_engine, clients=ClientScope.all()
    ) as session:
        user = (
            await session.execute(select(User).where(User.id == grant.user_id))
        ).scalar_one_or_none()
        if user is None:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, tr("this access token was refused")
            )
        # THE GRANT, ON EVERY CALL (plan D6; design §6.5: "MCP valida su JWT
        # (`iss`, `aud`, `kid`, revocación)"). A grant ended by a reused refresh
        # token, by Settings, by the app (RFC 7009) or by an act on the account
        # stops this call, not the next token. 401, so the app runs OAuth again.
        if not await grants.family_is_live(
            session,
            family_id=grant.family_id,
            user_id=user.id,
            client_id=grant.client_id,
            now=datetime.now(UTC),
        ):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                tr("this AI app's access was revoked; connect it again to continue"),
            )
        # ADMISSION, DOOR 1 OF 3 (spec §3.2), in `_admit_session`'s order and
        # from the same row: the console's email-domain rule (staff only), then
        # `users.status`, then a contact refused unless `contacts`. 403, not
        # 401: the credential is fine, the identity is not permitted.
        who = f"user:{user.id}"
        reasons = [
            None
            if user.kind == CONTACT_KIND
            else domain_refusal(
                user.email, await resolve_console_domain(session, grant.org_id, settings)
            ),
            status_refusal(user.status),
            None if contacts else contact_refusal(user.kind),
        ]
        for reason in reasons:
            if reason is not None:
                log_admission_refusal("mcp", sub=who, email=user.email, reason=reason)
                raise HTTPException(status.HTTP_403_FORBIDDEN, tr_marked(reason))
        if user.kind == CONTACT_KIND:
            scope = ClientScope.only([] if user.client_id is None else [user.client_id])
        else:
            scope = await clients_holding(session, user.id, None)
        email = user.email

    # The capture middleware's copy (Task 3, error monitoring): a PLAIN
    # attribute on `request.state`, never a contextvar, because that
    # middleware's `except` runs in another asyncio Task. `_admit_session`
    # says the same.
    request.state.org_id = str(grant.org_id)
    request.state.user_id = str(grant.user_id)

    return Admission(
        org_id=grant.org_id,
        user_id=grant.user_id,
        claims=dataclasses.replace(grant, email=email),
        clients=scope,
        requested_client=requested,
    )
```

y el docstring de `admit` cambia «on it by the MCP OAuth token» por «on it by an access token of the platform's own
OAuth server (`McpGrant`)», y «the self-heal» desaparece de la lista de lo compartido («the channel, the origin, the
org, the domain rule, `users.status`»).

En `backend/app/core/i18n/es.py`, en el bloque `# MCP channel (app/api/deps.py, app/mcp/guard.py)`:

```python
    "this access token was refused": "se rechazó este token de acceso",
```

y se borran las entradas `"this token also grants the console and is not accepted over MCP"` y `"unknown
organization"`, que ningún `tr()` usa ya (`tests/test_i18n_catalog.py::test_no_stale_catalogue_entries`). Si al fusionar
la fase 2 ha añadido un `tr("unknown organization")`, el coordinador conserva esa entrada.

- [ ] **Step 4: `/mcp`**

En `backend/app/mcp/server.py`:

1. Se quita `import jwt` y `from app.core.security import TokenClaims`; se añade
   `from app.core.auth.oauth.access import McpGrant, unverified_client_id` (la Task 6 ya importa `issuer`).
2. En el docstring del módulo, los pasos 1 y 4 pasan a:

```text
1. no MCP resource (`ITS_PUBLIC_CONSOLE_URL` unset), or no issuer (it is not
   an https address without a path) -> 503;
```

```text
4. `auth_context` on a scope this gate marks itself: an access token the
   platform's own OAuth server issued for the MCP resource, the grant it
   names still standing, and the console's admission rules (email domain,
   user status) read from the person's row -> 401 / 403 / 503 as it decides;
```

3. `McpPrincipal.claims: McpGrant`.
4. `_unverified_azp` se borra.
5. En `McpAsgi.__call__`, después del 503 de `resource is None`:

```python
        if issuer(settings) is None:
            detail = tr(
                "MCP is not available: ITS_PUBLIC_CONSOLE_URL is not an https address "
                "without a path."
            )
            return await _json(send, 503, {"detail": detail})
```

y el bloque de la etiqueta pasa a:

```python
        # REUSE THE CONSOLE'S ADMISSION PATH (the grant, the person's row, the
        # email domain, the user status) by marking the scope the way an
        # in-process MCP call is marked.
        #
        # THE APP LABEL IS READ FROM THE TOKEN UNVERIFIED, and that is safe: it
        # only labels the marker, and `admit` verifies the same token --
        # signature, issuer, MCP audience, expiry -- and refuses a marker that
        # names another app than the verified one before it reads anything else.
        # The label is compared with the verified `azp` below as well.
        label = unverified_client_id(authorization.removeprefix("Bearer "))
        # Revocation is keyed on the app: a token that names none could not be
        # revoked, so it is not admitted at all, before admission.
        if not label:
            return await _unauthorised(
                send, resource, detail=tr("this token does not name the AI app it was issued to")
            )
```

El resto de `McpAsgi`, `_gate` (que lee `claims.iat` y `claims.azp`, los dos de `McpGrant`) y los handlers no cambian.

- [ ] **Step 5: Las listas de revisión**

En `backend/tests/test_route_session_discipline.py`, se borra la entrada `"api/deps.py::admit"` de
`RAW_SESSIONS_BY_DESIGN`: `admit` ya no abre `AsyncSession(admin_engine)`, y la prueba falla con una entrada que no
describe código (`stale`). `test_the_scan_is_not_vacuous` sigue viendo 4 sitios (los dos de `agent_distribution/api.py`
y los dos de `api/v1/errors.py`), su mínimo.

En `backend/tests/test_client_catalog.py`, el motivo de `"users_org_id_external_subject_key"` en `ORG_WIDE_KEYS` pasa a:

```python
    "users_org_id_external_subject_key": (
        "an identity, not a name: one subject of the identity provider was one person in "
        "the organisation. No door reads it since own authentication phase 3 (the MCP token "
        "names the person by id); the Keycloak event ingest still does until phase 4 removes "
        "both. Per client, one login could hold a row in each client. Written only under "
        "scope all (`POST /users`, a contact's placeholder), and the value is an opaque id"
    ),
```

- [ ] **Step 6: Las pruebas re-apuntadas**

Cada una dice qué fijaba y por qué cambia. Ninguna se borra.

**`backend/tests/test_mcp_server.py`.** Import: `from tests.mcp_support import MCP_AUD, MCP_BASE, mcp_claims, mcp_client,
mcp_token, running_mcp, signed_mcp_token`. Constante nueva `REFUSED_EN = "this access token was refused"`. En el docstring
del módulo, la viñeta del 401 añade «a token is refused when the platform did not issue it, for the MCP resource».

- `_token_issued_at` pasa a `async def _token_issued_at(env, slug, sub, iat, azp="claude") -> str: return await
  mcp_token(env, slug, sub, azp, iat=iat)`, y cada llamada lleva `await`.
- Cada `mcp_token(mcp_env, …)` pasa a `await mcp_token(mcp_env, …)`.
- `test_a_console_token_is_401` → `test_a_keycloak_token_is_401_whatever_its_audience`: fijaba que el token del cliente
  de la consola no entra; desde esta fase ningún token de Keycloak entra. Mismo cuerpo, con los dos tokens:

```python
async def test_a_keycloak_token_is_401_whatever_its_audience(mcp_env) -> None:
    """The console client's token, and one Keycloak issued for the MCP
    resource itself: since own authentication phase 3 only the platform's own
    OAuth server issues MCP tokens (design §5.3: "otro token se rechaza")."""
    org_id, slug = await _org(mcp_env, mcp_enabled=True)
    sub = await mcp_env.create_user(org_id, "org-admin")
    for token in (
        mcp_env.idp.token(sub, slug, email=f"{sub}@test.local", extra={"client_id": "claude"}),
        mcp_env.idp.token(
            sub, slug, aud=MCP_AUD, azp="claude", email=f"{sub}@test.local",
            extra={"client_id": "claude"},
        ),
    ):
        async with mcp_client(mcp_env) as client:
            resp = await client.post("/mcp", json=INITIALIZE, headers=_bearer(token))
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == REFUSED_CHALLENGE
        assert resp.json()["detail"] == REFUSED_EN
```

- `test_a_token_with_both_audiences_is_401`: fijaba 1c(a) con un token de Keycloak de dos audiencias; ahora con uno
  propio:

```python
async def test_a_token_with_both_audiences_is_401(mcp_env) -> None:
    org_id, slug = await _org(mcp_env, mcp_enabled=True)
    sub = await mcp_env.create_user(org_id, "org-admin")
    claims = mcp_claims(await mcp_token(mcp_env, slug, sub))
    claims["aud"] = [MCP_AUD, MCP_BASE]
    async with mcp_client(mcp_env) as client:
        resp = await client.post(
            "/mcp", json=INITIALIZE, headers=_bearer(await signed_mcp_token(claims))
        )
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == REFUSED_CHALLENGE
    assert resp.json()["detail"] == REFUSED_EN
```

- `test_a_token_that_names_no_app_is_401`: misma propiedad (la revocación va por app), con un token propio sin
  `client_id`:

```python
async def test_a_token_that_names_no_app_is_401(mcp_env) -> None:
    """Revocation is keyed on the app. A token that names none could not be
    revoked before it expired, so it is not admitted at all."""
    org_id, slug = await _org(mcp_env, mcp_enabled=True)
    sub = await mcp_env.create_user(org_id, "org-admin")
    claims = mcp_claims(await mcp_token(mcp_env, slug, sub))
    del claims["client_id"]
    async with mcp_client(mcp_env) as client:
        resp = await client.post(
            "/mcp", json=INITIALIZE, headers=_bearer(await signed_mcp_token(claims))
        )
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == REFUSED_CHALLENGE
    assert resp.json()["detail"] == "this token does not name the AI app it was issued to"
```

- `test_a_token_without_an_org_claim_is_401_in_multi_tenant_mode` → `test_a_token_without_an_org_claim_is_401`: fijaba
  que un token sin organización no cae en la del despliegue; el token propio nombra la organización por id y sin `org`
  no verifica, sea cual sea el modo:

```python
async def test_a_token_without_an_org_claim_is_401(mcp_env) -> None:
    """The organization is the token's `org`, which the platform signed. Without
    it the token does not verify, whatever `ITS_DEFAULT_ORG_SLUG` and the
    multi-tenant switch say."""
    org_id, slug = await _org(mcp_env, mcp_enabled=True)
    sub = await mcp_env.create_user(org_id, "org-admin")
    mcp_env.settings.default_org_slug = slug
    claims = mcp_claims(await mcp_token(mcp_env, slug, sub))
    del claims["org"]
    for multi_tenant in (False, True):
        mcp_env.settings.multi_tenant = multi_tenant
        async with mcp_client(mcp_env) as client:
            resp = await client.post(
                "/mcp", json=INITIALIZE, headers=_bearer(await signed_mcp_token(claims))
            )
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == REFUSED_CHALLENGE
```

- `test_a_first_seen_sign_in_over_mcp_is_audited_as_via_mcp` → `test_a_token_for_nobody_provisions_nobody`: fijaba que
  la fila que la puerta aprovisionaba quedaba auditada como MCP; la puerta ya no aprovisiona (decisión 1), así que lo
  que se fija es que no queda ni fila ni `auth.login`:

```python
async def test_a_token_for_nobody_provisions_nobody(mcp_env) -> None:
    """The MCP door provisioned a first-seen person until own authentication
    phase 3 (and audited it as `via: "mcp"`). It provisions nobody now: a token
    for a person with no row is a 401, and nothing is written."""
    org_id, slug = await _org(mcp_env, mcp_enabled=True)
    sub = await mcp_env.create_user(org_id, "org-admin")
    claims = mcp_claims(await mcp_token(mcp_env, slug, sub))
    nobody = uuid4()
    claims["sub"] = str(nobody)
    async with mcp_client(mcp_env) as client:
        resp = await client.post(
            "/mcp", json=INITIALIZE, headers=_bearer(await signed_mcp_token(claims))
        )
    assert resp.status_code == 401
    assert resp.json()["detail"] == REFUSED_EN
    async with AsyncSession(mcp_env.admin_engine) as session:
        provisioned = (
            await session.execute(select(User.id).where(User.id == nobody))
        ).scalar_one_or_none()
        logins = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "auth.login"
                )
            )
        ).scalars().all()
    assert provisioned is None
    assert logins == []
```

  con `from uuid import UUID, uuid4`.
- `test_one_organizations_revocation_never_reaches_another`: el cuerpo no cambia (con `await`); el docstring pasa a «A
  metadata-document client has the same `client_id` in every organization. Org A revoking "claude" says nothing about
  org B's users.»

**`backend/tests/test_mcp_channel_api.py`.** Import: `from tests.mcp_support import MCP_BASE, marked_client, mcp_claims,
mcp_token, signed_mcp_token`. La primera viñeta del docstring pasa a «the token is one the platform's own OAuth server
issued for the MCP resource, never the console's or Keycloak's, and an MCP token is no credential off the channel».
Cada `mcp_token(` pasa a `await mcp_token(` (en una expresión `_bearer(mcp_token(…))`, `_bearer(await mcp_token(…))`).

- `test_a_marked_request_without_a_console_url_is_503`: el token se acuña **antes** del `monkeypatch` (sin consola no
  hay emisor con el que acuñarlo); la propiedad es la misma:

```python
async def test_a_marked_request_without_a_console_url_is_503(api_env, monkeypatch) -> None:
    # `api_env` has a console since own authentication Task 13 (the CSRF rule
    # needs one); this is the deployment without it. The token is minted first:
    # without a console there is no issuer to mint it with.
    _, slug, sub = await _org_admin(api_env)
    token = await mcp_token(api_env, slug, sub)
    monkeypatch.setattr(api_env.settings, "public_console_url", None)
    async with marked_client(api_env) as client:
        resp = await client.get(
            "/api/v1/me", headers=_bearer(token, **{"Accept-Language": "es"})
        )
    assert resp.status_code == 503
    assert resp.json()["detail"] == (
        "MCP no está disponible: ITS_PUBLIC_CONSOLE_URL no está configurado."
    )
```

- `test_an_admission_event_during_an_mcp_call_is_audited_as_via_mcp` → `test_an_mcp_call_writes_no_admission_event`:
  fijaba que el `auth.email_changed` escrito en la admisión llevaba el canal; la admisión ya no escribe (decisión 1). Que
  un evento escrito durante una llamada MCP lleva el canal lo siguen fijando `test_a_marked_write_is_audited_as_via_mcp`
  y `test_a_marked_portal_ticket_is_audited_as_via_mcp`:

```python
async def test_an_mcp_call_writes_no_admission_event(mcp_env) -> None:
    """Until own authentication phase 3 the door refreshed `users.email` from
    the token's claim and audited it, as MCP. The platform's token carries no
    address: the door reads the row and writes nothing."""
    org_id, slug, sub = await _org_admin(mcp_env)
    async with marked_client(mcp_env) as client:
        resp = await client.get("/api/v1/me", headers=_bearer(await mcp_token(mcp_env, slug, sub)))
    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == f"{sub}@test.local"
    assert await _audit(mcp_env.admin_engine, org_id, "auth.email_changed") == []
    assert await _audit(mcp_env.admin_engine, org_id, "auth.login") == []
```

- `test_a_token_with_both_audiences_is_refused_on_and_off_the_channel`: misma propiedad (1c(a), y que una concesión MCP
  no vale contra la consola), con un token propio:

```python
async def test_a_token_with_both_audiences_is_refused_on_and_off_the_channel(
    mcp_env,
) -> None:
    # Off the channel no bearer is read at all, so an MCP grant can never be
    # replayed against the console API; ON the channel 1c(a) admits only a
    # token with the MCP audience "and no other platform audience".
    _, slug, sub = await _org_admin(mcp_env)
    claims = mcp_claims(await mcp_token(mcp_env, slug, sub))
    claims["aud"] = [MCP_BASE, f"{MCP_BASE}/mcp"]
    token = await signed_mcp_token(claims)
    resp = await mcp_env.client.get("/api/v1/me", headers=_bearer(token))
    assert resp.status_code == 401
    async with marked_client(mcp_env) as client:
        resp = await client.get("/api/v1/me", headers=_bearer(token))
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "this access token was refused"
```

- `test_a_console_token_is_refused_on_a_marked_request` no cambia: un token de Keycloak sigue siendo 401 en el canal.

**`backend/tests/test_token_origin.py`.** Import: `from tests.mcp_support import MCP_AUD, mcp_client, mcp_token,
running_mcp`. Constantes nuevas: `NO_APP_EN = "this token does not name the AI app it was issued to"`, `NO_APP_ES = "este
token no indica la aplicación de IA para la que se emitió"`, `REFUSED_EN = "this access token was refused"`, `REFUSED_ES
= "se rechazó este token de acceso"`. La tercera viñeta del docstring pasa a «`/mcp` refuses every token the platform's
own OAuth server did not issue -- Keycloak's for the console's client, a service account's, an ID token -- before the
door reads anything (own authentication phase 3), and provisions nobody».

- `test_an_mcp_token_off_the_channel_is_no_credential`: el token es el propio (`token = await mcp_token(mcp_env, slug,
  sub)`); la aserción no cambia.
- El barrido se generaliza y gana su gemelo para el token propio. `_unruled_validations` pasa a:

```python
def _unruled_validations(
    source: str, call: str = "validate_token", rules: frozenset[str] = ORIGIN_RULES
) -> list[int]:
    """Lines of `call(` sites whose enclosing function calls none of `rules`
    after them."""
    tree = ast.parse(source)
    found: list[int] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        calls = [n for n in ast.walk(fn) if _called(n) is not None]
        ruled = [n.lineno for n in calls if _called(n) in rules]
        for site in calls:
            if _called(site) == call and not any(r >= site.lineno for r in ruled):
                found.append(site.lineno)
    return found
```

  y `test_every_validate_token_call_in_app_is_followed_by_an_origin_rule` cambia su aserción de recuento, de `sites ==
  1` a:

```python
    # None since own authentication phase 3 (plan 2026-11-06, Task 9): the MCP
    # door verifies the platform's own token. A new one is a decision.
    assert sites == 0, f"{sites}: a door validates a Keycloak token again; that is a decision"
```

  Después, las pruebas nuevas:

```python
#: Every caller of `verify_access_token(` in `app/`, outside its definition.
#: A door (it admits a person) must read the grant the token names after it.
VERIFY_SITES = {
    "app/api/deps.py": "the MCP door: admit() reads the grant (family_is_live) after it",
    "app/core/auth/oauth/grants.py": (
        "RFC 7009: revoke_presented ends the grant a token names; it admits nobody"
    ),
}
GRANT_RULES = frozenset({"family_is_live"})


def test_the_grant_sweep_catches_a_verification_with_no_grant_check() -> None:
    bad = "async def door(t):\n    grant = await verify_access_token(t)\n    return grant\n"
    good = (
        "async def door(t, s):\n    grant = await verify_access_token(t)\n"
        "    await family_is_live(s, grant)\n"
    )
    assert _unruled_validations(bad, "verify_access_token", GRANT_RULES) == [2]
    assert _unruled_validations(good, "verify_access_token", GRANT_RULES) == []


def test_every_door_that_verifies_an_mcp_token_reads_its_grant() -> None:
    callers: set[str] = set()
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        if path == APP / "core" / "auth" / "oauth" / "access.py":
            continue  # the definition
        source = path.read_text()
        if "verify_access_token(" not in source:
            continue
        rel = str(path.relative_to(APP.parent))
        callers.add(rel)
        if rel == "app/api/deps.py":
            offenders += [
                f"{rel}:{line}"
                for line in _unruled_validations(source, "verify_access_token", GRANT_RULES)
            ]
    assert callers == set(VERIFY_SITES), callers
    assert offenders == [], f"verify_access_token( with no family_is_live( after it: {offenders}"
```

- `test_a_persons_mcp_token_is_admitted`: `token = await mcp_token(mcp_env, slug, sub)`.
- `test_a_token_no_person_signed_in_for_is_refused_at_mcp`: fijaba que ningún token sin persona entra y que nada se
  aprovisiona. Los cinco tokens de Keycloak siguen siendo los casos; lo que cambia es por qué se rechazan. Los que no
  llevan claim `client_id` no pasan la etiqueta de `McpAsgi` (`NO_APP`); el que lo lleva llega a la puerta, que no
  verifica su firma (`REFUSED`). La parametrización pasa a `("kw", "detail", "detail_es")`:

```python
@pytest.mark.parametrize(
    ("kw", "detail", "detail_es"),
    [
        ({"azp": "claude", "extra": {"preferred_username": "service-account-x"}}, NO_APP_EN, NO_APP_ES),
        ({"azp": "claude", "extra": {"client_id": "claude"}}, REFUSED_EN, REFUSED_ES),
        ({"azp": "it-suite-web"}, NO_APP_EN, NO_APP_ES),
        ({"azp": "claude", "typ": "ID"}, NO_APP_EN, NO_APP_ES),
        ({"azp": "claude", "sid": None}, NO_APP_EN, NO_APP_ES),
    ],
    ids=["service-account-username", "client-id-claim", "console-azp", "id-token", "no-sid"],
)
async def test_a_token_no_person_signed_in_for_is_refused_at_mcp(
    mcp_env, kw: dict[str, Any], detail: str, detail_es: str
) -> None:
```

  y en el cuerpo, `if detail == NOT_A_PERSON_EN: assert resp_es.json()["detail"] == NOT_A_PERSON_ES` pasa a `assert
  resp_es.json()["detail"] == detail_es`. El comentario «a refusal must come before admission, which would otherwise
  provision one» pasa a «a sub with no `users` row: nothing may be provisioned for it».
- `test_a_legacy_client_id_claim_is_refused_at_mcp`: `assert resp.json()["detail"] == NO_APP_EN` (el claim es
  `clientId`, no `client_id`: no nombra app ninguna). El resto no cambia.
- `test_require_person_mcp_token`, `test_the_legacy_client_id_claim_marks_a_service_account` y las pruebas de
  `validate_token` no cambian (fase 4).

**`backend/tests/test_domain_gate_doors.py`.** Import: `from tests.mcp_support import MCP_BASE, marked_client, mcp_claims,
mcp_token, signed_mcp_token` (`MCP_AUD` deja de usarse aquí). `_mcp_token` pasa a:

```python
async def _mcp_token(api_env, sub: str, slug: str, email: str) -> str:
    """The platform's own MCP token for `sub`, whose row now holds `email`: the
    token carries no address since own authentication phase 3, and the door
    reads the row's, as for a session."""
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(update(User).where(User.external_subject == sub).values(email=email))
        await s.commit()
    return await mcp_token(api_env, slug, sub)
```

En `_one_refusal`, «`sub` is what survives in Keycloak for an MCP token, `user:<id>` for a session» pasa a «`sub` is
`user:<id>` at every door that reads the person's row».

- `test_http_refusal_does_not_create_a_user_row`: fijaba que un rechazo no deja fila; la puerta ya no aprovisiona, y un
  token para nadie es 401 sin fila:

```python
async def test_http_refusal_does_not_create_a_user_row(api_env) -> None:
    """A refused token leaves no `users` row -- otherwise every refused token
    would leave a provisioned account, and the preflight's count would grow on
    attack traffic. Since own authentication phase 3 the MCP door provisions
    nobody at all: the token names a person by id, and a person with no row is
    a 401, never a self-heal."""
    _over_mcp(api_env)
    api_env.settings.console_email_domain = BASE_DOMAIN
    org_id = await api_env.create_org("dom")
    slug = await api_env.org_slug(org_id)
    sub = await api_env.create_user(org_id, "read-only")
    claims = mcp_claims(await mcp_token(api_env, slug, sub))
    nobody = uuid4()
    claims["sub"] = str(nobody)
    token = await signed_mcp_token(claims)

    async with marked_client(api_env) as client:
        response = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    async with AsyncSession(api_env.admin_engine) as s:
        found = (await s.execute(select(User.id).where(User.id == nobody))).scalar_one_or_none()
        rows = (await s.execute(select(User.id).where(User.org_id == org_id))).scalars().all()
    assert found is None
    assert len(rows) == 1
```

- `test_mcp_logs_the_domain_refusal`: la dirección va en la fila y el registro dice `door="mcp"`, `sub="user:<id>"`:

```python
    email = _addr("u", f"evil{BASE_DOMAIN}")
    token = await _mcp_token(api_env, sub, slug, email)

    with _capture_refusals() as records:
        async with marked_client(api_env) as client:
            response = await client.get(
                "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
            )
    assert response.status_code == 403
    who = f"user:{await _user_id_of(api_env, sub)}"
    record = _one_refusal(records, door="mcp", sub=who, email=email)
    assert record.reason == response.json()["detail"]
```

  (en lugar de `email = f"u@evil{BASE_DOMAIN}"` y la llamada dentro de la cabecera; el docstring cambia «(`admit`'s
  token branch)» por «(`admit`'s branch for the platform's own MCP token)»).
- `test_http_logs_the_status_refusal`: igual, `token = await _mcp_token(api_env, sub, slug, email)` antes del
  `_capture_refusals()`, y `record = _one_refusal(records, door="mcp", sub=f"user:{await _user_id_of(api_env, sub)}",
  email=email)`. `email` pasa a `_addr("u", BASE_DOMAIN)`, porque ahora se escribe en la fila (el docstring de `_addr`
  dice por qué).
- `test_http_org_setting_refusal_still_creates_no_user_row`: fijaba que el rechazo dentro de la transacción del tenant no
  deja nada escrito. Sin aprovisionamiento, lo que queda por escribir es el registro de una entrada; se fija con una
  persona que existe y cuya dirección la organización no admite:

```python
async def test_http_org_setting_refusal_still_creates_no_user_row(api_env) -> None:
    """THE PROPERTY THE MOVE COULD HAVE COST: the refusal is the first thing
    inside the tenant transaction and raises, so nothing commits. The MCP door
    provisions nobody since own authentication phase 3, so what it could still
    write is a login: the refused person's row is exactly as it was."""
    _over_mcp(api_env)
    org_id = await api_env.create_org("dom-orgset-row")
    slug = await api_env.org_slug(org_id)
    await _set_org_domain(api_env, org_id, BASE_DOMAIN)
    sub = await api_env.create_user(org_id, "read-only")
    bad = await _mcp_token(api_env, sub, slug, _addr("u", f"evil{BASE_DOMAIN}"))

    async def row() -> tuple[Any, ...]:
        async with AsyncSession(api_env.admin_engine) as s:
            user = (await s.execute(select(User).where(User.external_subject == sub))).scalar_one()
            return (user.email, user.last_login_at, user.status)

    seen = await row()
    async with marked_client(api_env) as client:
        response = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {bad}"})
    assert response.status_code == 403
    assert await row() == seen
```

  (`Any` ya está importado en el archivo; si no, `from typing import Any`).
- Las dos llamadas de las líneas 816 y 841 (`_mcp_token(api_env, sub, slug, email)` dentro de la cabecera) quedan
  cubiertas por los dos puntos anteriores.

**`backend/tests/test_domain_policy.py`** (la prueba del orden, hacia la línea 580). Fijaba que «tu token no es genuino»
(401) y «tu identidad no se admite» (403) se distinguen, con el slug desconocido como caso. El token propio nombra la
organización por un id firmado: el caso pasa a un token genuino de una organización que no existe. Import: `from
tests.mcp_support import MCP_BASE, marked_client, mcp_claims, mcp_token, signed_mcp_token`. El párrafo final del
docstring («ON THE MCP CHANNEL since own authentication phase 1…») pasa a «ON THE MCP CHANNEL: since own authentication
phase 3 the organization is a signed id in the platform's own token, so the unknown slug has no oracle left; what stays
is the order -- a token naming no organization this platform has is a 401 before any domain rule.» El cuerpo:

```python
    # The MCP resource is `MCP_AUD` there (tests/mcp_support.py).
    api_env.settings.public_console_url = MCP_BASE

    org_id = await api_env.create_org("policy-order")
    slug = await api_env.org_slug(org_id)
    await _set_org_domain(api_env, org_id, BASE_DOMAIN)

    async def token_for(address: str) -> str:
        sub = await api_env.create_user(org_id)
        async with AsyncSession(api_env.admin_engine) as s:
            await s.execute(update(User).where(User.external_subject == sub).values(email=address))
            await s.commit()
        return await mcp_token(api_env, slug, sub)

    stranger = f"eve-{uuid4().hex[:8]}@elsewhere.example"
    known = await token_for(stranger)
    claims = mcp_claims(known)
    claims["org"] = str(uuid4())
    unknown = await signed_mcp_token(claims)

    # The PLATFORM DEFAULT is set too, and that is what makes this an ordering
    # test rather than a coincidence: a door that refused on the domain FIRST
    # would have a value to refuse this address with even when the org is
    # unknown, and would answer 403 below instead of 401.
    api_env.settings.console_email_domain = BASE_DOMAIN
    try:
        async with marked_client(api_env) as client:
            unknown_org = await client.get(
                "/api/v1/orgs/current", headers={"Authorization": f"Bearer {unknown}"}
            )
            refused_domain = await client.get(
                "/api/v1/orgs/current", headers={"Authorization": f"Bearer {known}"}
            )
    finally:
        api_env.settings.console_email_domain = None

    assert unknown_org.status_code == 401, unknown_org.text
    assert unknown_org.json()["detail"] == "this access token was refused"

    assert refused_domain.status_code == 403, refused_domain.text
    assert "email domain not permitted" in refused_domain.json()["detail"]

    # The two answers are DIFFERENT, which is the whole subject.
    assert unknown_org.status_code != refused_domain.status_code

    # Non-vacuity: with the allowlist satisfied, a person of the same org is
    # admitted -- so the 403 above is the domain rule answering and not the route.
    admitted_token = await token_for(f"ada-{uuid4().hex[:8]}@{BASE_DOMAIN}")
    async with marked_client(api_env) as client:
        admitted = await client.get(
            "/api/v1/orgs/current", headers={"Authorization": f"Bearer {admitted_token}"}
        )
    assert admitted.status_code == 200, admitted.text
```

  con `from app.models import User` si el archivo no lo importa ya.

**`backend/tests/test_clients_api.py`** (`test_a_technician_signing_in_with_a_contacts_address_is_never_refused`). Fijaba
que I1 es de un solo sentido en la puerta que escribía una dirección de un IdP. Esa escritura ya no existe; lo que queda
es que la puerta de MCP admite a un técnico cuya fila tiene la dirección de un contacto. Import: `from tests.mcp_support
import MCP_BASE, marked_client, mcp_token`. El docstring pasa a «I1 is ONE-DIRECTIONAL: it holds a contact's address
against every other user, never a staff row against a contact's. Since own authentication phase 3 no door writes an
address an identity provider asserted; a technician whose row holds a contact's address is still admitted over MCP.» El
final del cuerpo, desde `slug = await env.org_slug(org)`:

```python
    slug = await env.org_slug(org)
    # The MCP resource is `MCP_AUD` there (tests/mcp_support.py).
    env.settings.public_console_url = MCP_BASE
    sub = await env.create_user(org, "technician")
    async with AsyncSession(env.admin_engine) as s:
        await s.execute(update(User).where(User.external_subject == sub).values(email="dual@x.co"))
        await s.commit()
    token = await mcp_token(env, slug, sub)
    async with marked_client(env) as client:
        me = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
```

  y la comprobación de `kinds == ["contact", "staff"]` sigue igual.

**`backend/tests/test_me_profile.py`.** Import: `from tests.mcp_support import MCP_BASE, marked_client, mcp_claims,
mcp_token`. El docstring del módulo, segundo párrafo, pasa a «`PATCH /api/v1/me` gives ITMS a writable `display_name`;
since own authentication phase 3 ITMS owns the address too: no token carries one, and every door reads the row's. What
§7.2 asked for -- the security decision and the displayed fact read from ONE source -- holds by construction now, and
the tests below hold it over the MCP channel, the door that used to refresh from a claim.» `_headers` pierde la frase
sobre el refresco. `_mcp_me` pasa a:

```python
async def _mcp_me(env: Any, org_id: UUID, sub: str) -> httpx.Response:
    """`GET /api/v1/me` on the MCP channel with the platform's own token, which
    carries no address: the row's is the only one there is."""
    env.settings.public_console_url = MCP_BASE
    token = await mcp_token(env, await env.org_slug(org_id), sub)
    async with marked_client(env) as client:
        return await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
```

El encabezado «Falsifier 13 -- a changed email claim reaches the row, and is audited» pasa a «Falsifier 13 -- the address
the door tests is the address the row holds». Las cinco pruebas:

```python
async def test_an_address_changed_in_itms_is_the_one_mcp_sees(api_env) -> None:
    org_id = await api_env.create_org("stale")
    sub = await api_env.create_user(org_id, "technician")
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(
            update(User).where(User.external_subject == sub).values(email="ada.new@toeshee.com")
        )
        await s.commit()

    response = await _mcp_me(api_env, org_id, sub)
    assert response.status_code == 200
    assert response.json()["email"] == "ada.new@toeshee.com"
    assert (await _row(api_env.admin_engine, sub)).email == "ada.new@toeshee.com"


async def test_an_mcp_call_records_no_identity_change(api_env) -> None:
    org_id = await api_env.create_org("stale")
    sub = await api_env.create_user(org_id, "technician")

    await _mcp_me(api_env, org_id, sub)

    assert await _audit(api_env.admin_engine, org_id, "auth.email_changed") == []
    assert (await _row(api_env.admin_engine, sub)).email == f"{sub}@test.local"


async def test_repeated_mcp_calls_write_nothing(api_env) -> None:
    """Every request re-runs admission. A door that wrote on admission would
    append an audit event per HTTP call and fill the chain with noise."""
    org_id = await api_env.create_org("stale")
    sub = await api_env.create_user(org_id, "technician")

    for _ in range(3):
        await _mcp_me(api_env, org_id, sub)

    assert await _audit(api_env.admin_engine, org_id, "auth.email_changed") == []
    assert await _audit(api_env.admin_engine, org_id, "auth.login") == []


async def test_a_display_name_set_in_itms_survives_mcp_calls(api_env) -> None:
    org_id = await api_env.create_org("override")
    sub = await api_env.create_user(org_id, "technician")
    headers = await _headers(api_env, org_id, sub)

    await api_env.client.patch("/api/v1/me", headers=headers, json={"display_name": "Ada L."})
    body = (await _mcp_me(api_env, org_id, sub)).json()

    assert body["display_name"] == "Ada L."
    assert (await _row(api_env.admin_engine, sub)).display_name == "Ada L."
    assert body["email"] == f"{sub}@test.local"


async def test_an_mcp_token_carries_no_address_and_blanks_nothing(api_env) -> None:
    """DATA LOSS ON THE SHIPPED DEFAULT, as it was: a token with no address
    must never blank the stored one. The platform's token never carries one, so
    the property is now that the door does not read an address from a token."""
    org_id = await api_env.create_org("noclaim")
    sub = await api_env.create_user(org_id, "technician")
    stored = f"{sub}@test.local"
    api_env.settings.public_console_url = MCP_BASE
    token = await mcp_token(api_env, await api_env.org_slug(org_id), sub)
    assert "email" not in mcp_claims(token)

    response = await _mcp_me(api_env, org_id, sub)

    assert response.status_code == 200
    assert (await _row(api_env.admin_engine, sub)).email == stored
    assert await _audit(api_env.admin_engine, org_id, "auth.email_changed") == []
```

  con `from sqlalchemy import select, update` y `from app.models import User` si faltan.

**`backend/tests/test_auth_falsifiers.py`.** `bearer = {"Authorization": f"Bearer {await mcp_token(api_env, await
api_env.org_slug(org_id), sub)}"}`. En `test_require_step_up_admits_no_request_without_a_session`, el `AuthContext` sin
sesión lleva ahora la concesión de MCP: `claims = None if principal else McpGrant(user_id=uuid4(), org_id=uuid4(),
client_id="claude", family_id=uuid4(), iat=0, exp=600)`, con `from app.core.auth.oauth.access import McpGrant` en lugar
de `from app.core.security import TokenClaims`.

**`backend/tests/test_mcp_admin_api.py`** (solo los dos ayudantes; la Task 10 reescribe el resto). Import: `from
tests.mcp_support import mcp_client, mcp_token, running_mcp` (`MCP_AUD` deja de usarse).

```python
async def _old_token(env: Any, slug: str, sub: str, azp: str = "claude") -> str:
    """An MCP access token issued a minute ago, before any revocation below."""
    return await mcp_token(env.api, slug, sub, azp, iat=int(time.time()) - 60)
```

y en `_call_a_tool`, `token = await mcp_token(env.api, slug, sub, azp)`; cada `_old_token(` pasa a `await _old_token(`
(cinco llamadas, en `_initialize(env, await _old_token(...))`).

- [ ] **Step 7: Las llamadas mecánicas**

`mcp_token(` → `await mcp_token(` en: `backend/tests/test_mcp_tools.py` (las líneas 210 —`return org_id, sub, await
mcp_token(env, slug, sub)`— y 277), `backend/tests/test_deploy_window.py:135`, `backend/tests/test_backup_policy_window.py:222`,
`backend/tests/test_client_scoped_config.py:1171`, `backend/tests/test_auth_reset_needs_recovery.py:373`. Comprobación de
que no queda ninguna sin `await`:

Run: `cd backend && grep -rn "mcp_token(" tests | grep -v "await mcp_token(\|def mcp_token(\|_mcp_token(\|signed_mcp_token(\|require_person_mcp_token("`
Expected: sin salida.

Run: `cd backend && grep -rn "aud=MCP_AUD" tests | grep -v "test_security.py\|test_mcp_own_tokens.py\|test_mcp_server.py\|test_token_origin.py"`
Expected: sin salida (los que quedan fabrican, a propósito, el token de Keycloak que la puerta rechaza).

- [ ] **Step 8: Las pruebas pasan**

Run: `cd backend && .venv/bin/pytest -q tests/test_mcp_own_tokens.py tests/test_mcp_server.py tests/test_mcp_channel_api.py tests/test_token_origin.py tests/test_domain_gate_doors.py tests/test_domain_policy.py tests/test_clients_api.py tests/test_me_profile.py tests/test_auth_falsifiers.py tests/test_mcp_admin_api.py tests/test_mcp_tools.py tests/test_deploy_window.py tests/test_backup_policy_window.py tests/test_client_scoped_config.py tests/test_auth_reset_needs_recovery.py tests/test_route_session_discipline.py tests/test_client_catalog.py tests/test_i18n_catalog.py tests/test_loopback.py tests/test_auth_session_door.py tests/test_auth_csrf.py tests/test_security.py`
Expected: PASS.

Run: `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `Success: no issues found` y `All checks passed!`.

- [ ] **Step 9: Commit**

```bash
git add backend/app/api/deps.py backend/app/mcp/server.py backend/app/core/i18n/es.py \
  backend/tests/mcp_support.py backend/tests/test_mcp_own_tokens.py backend/tests/test_mcp_server.py \
  backend/tests/test_mcp_channel_api.py backend/tests/test_token_origin.py backend/tests/test_domain_gate_doors.py \
  backend/tests/test_domain_policy.py backend/tests/test_clients_api.py backend/tests/test_me_profile.py \
  backend/tests/test_auth_falsifiers.py backend/tests/test_mcp_admin_api.py backend/tests/test_mcp_tools.py \
  backend/tests/test_deploy_window.py backend/tests/test_backup_policy_window.py \
  backend/tests/test_client_scoped_config.py backend/tests/test_auth_reset_needs_recovery.py \
  backend/tests/test_route_session_discipline.py backend/tests/test_client_catalog.py
git commit -m "feat(mcp): the MCP door admits only the platform's own access token, and reads its grant on every call"
```

El informe de la tarea lista las pruebas re-apuntadas del Step 6 con su porqué (está escrito arriba, prueba por prueba).

### Task 10: Ajustes → Conexiones de IA sin Keycloak; los actos de la cuenta revocan las concesiones

Diseño §5.3 («Revocar un cliente de IA en Ajustes corta su acceso en la siguiente llamada (1c(d))»), §2.4; D12.
Oleada 5, sola. Backend.

Revocar una app en Ajustes deja de pedir a Keycloak que borre un consentimiento: termina las concesiones de la app (de
la persona o de toda la organización), escribe la fila `mcp_revocation` y el evento `mcp.app_revoked`, **en una sola
transacción**. La ruta pasa a nombrar la app por el id de su fila (D12: el `client_id` de un cliente CIMD es una URL con
`/`). Y los siete actos de la cuenta que ya olvidan los equipos recordados de una persona terminan también sus
concesiones de IA, con el acto como motivo.

**Files:**
- Modify: `backend/app/mcp/api.py`
- Modify: `backend/app/mcp/exclusions.py` (la clave de la ruta de revocar en `NOT_FOR_MCP`)
- Modify: `backend/app/core/i18n/es.py` (una entrada nueva, una que deja de usarse)
- Modify: `backend/app/modules/user_admin/service.py` (`set_enabled`, `reset_factors`, `delete_user`)
- Modify: `backend/app/core/auth/account.py` (`change_password`)
- Modify: `backend/app/core/auth/flows.py` (`logout_everywhere`, `reset_password`, `accept_recovery`)
- Modify: `backend/tests/test_mcp_admin_api.py`
- Modify: `backend/tests/test_global_writes.py` (la clave de `NOT_GLOBAL` y la URL de su prueba)
- Modify: `backend/tests/test_logging_discipline.py` y `docs/superpowers/specs/2026-08-14-logging-kernel-design.md` (§2.2)
- Create: `backend/tests/test_mcp_grant_lifecycle.py`

**Interfaces:**
- Consumes: Task 5 (`OAuthClient`, `OAuthRefreshFamily`, `oauth_session`), Task 7 (`grants.revoke_app(session, *,
  oauth_client_id, user_id, reason, now) -> int`, `grants.revoke_all_for_user(session, user_id, reason) -> int`,
  `grants.open_family`; los motivos `revoked_in_settings`, `disabled`, `deleted`, `factors_reset`, `password_changed`,
  `signed_out_everywhere` de `REVOKE_REASONS`), Task 9 (`tests.mcp_support.mcp_app`, `mcp_token` asíncrono, y
  `_old_token`/`_call_a_tool` ya re-apuntados en `test_mcp_admin_api.py`). Existentes: `require_all_clients`,
  `has_permission`, `audit_service.record`, `McpRevocation`; en las pruebas, `tests.auth_support` (`signed_in`,
  `console_ready`, `capture_mail`, `browser`, `link_token`) y `app.core.auth.action_tokens.issue`.
- Produces: `POST /api/v1/mcp/connections/{app_id}/revoke` → `{"revoked": true, "grants_revoked": int}`; en `GET
  /api/v1/mcp/connections`, cada app con `id: UUID | null` y `name: str | null` (Apéndice A.2; la Task 11 lo consume);
  `mcp.app_revoked` con `metadata = {"client_id", "scope", "grants_revoked"}`; `mcp_api.revoke_app_access(session, *,
  org_id, actor_id, app_id, scope, now) -> int`.

**Decisión de la tarea (1): una transacción.** Sin Keycloak no hay mitad «de mejor esfuerzo»: la fila, las familias y
el evento se escriben juntos o nada (D12). Desaparecen `remove_consents`, `_record_incomplete`, `keycloak_failures`, las
constantes `KEYCLOAK_*`, `FIRST_PARTY_CLIENTS` y `CLIENT_ID_PATTERN` (una app del platform nunca tiene fila en
`oauth_clients`, y un id que no es un UUID es 422 antes de llegar a nada). `kc_client.revoke_consent` y sus pruebas se
quedan hasta la fase 4. El vocabulario de auditoría conserva `mcp.app_revoke_incomplete` (los eventos ya escritos se
siguen leyendo); la prueba de cobertura solo exige el sentido backend → vocabulario.

**Decisión de la tarea (2): la lista sigue saliendo de las llamadas** (diseño de MCP §3.4, el docstring del módulo): una
app aparece cuando ha llamado a una herramienta en la organización. Gana `id` y `name` de su fila de `oauth_clients`
cuando la tiene; sin fila (una app de antes de esta fase, o un registro caducado y purgado) el `id` es `null` y la
consola no ofrece revocarla (Task 11), porque no queda concesión que terminar.

**Decisión de la tarea (3): los actos de la cuenta son los que ya olvidan los equipos recordados** (pregunta P8). §2.4
nombra uno: «Desactivar a una persona corta sus sesiones, sus tokens de IA y sus equipos recordados al instante». Los
otros seis los toma el plan de la misma lista de los equipos: borrar, restablecer los factores (el administrador o el
enlace de recuperación), cambiar la contraseña (la persona o el enlace por correo) y «cerrar sesión en todas partes». Que un administrador cierre las sesiones de alguien
no los olvida (`user_admin.service.end_sessions`, «their remembered devices stay»), y tampoco termina sus concesiones.
Una línea junto a cada `revoke_all_for_user(session, user.id)` de `app.core.auth.devices`, con el nombre del módulo
(`grants.revoke_all_for_user`) para no chocar con el de `devices`.

- [ ] **Step 1: Las pruebas que fallan**

`backend/tests/test_mcp_grant_lifecycle.py`:

```python
"""An act on the account ends the person's AI connections (autenticación
propia design §2.4, §5.3; plan 2026-11-06, Task 10).

The acts that forget a person's remembered browsers end their grants too, with
the act as the reason: being disabled or deleted, a factor reset (by an
administrator or by the recovery link), a password change (their own, or by
the mail link) and "sign out everywhere". An administrator ending the person's
sessions forgets no browser, and ends no grant (question P8).
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import action_tokens
from app.core.auth.oauth import grants
from app.core.auth.oauth.store import oauth_session
from app.core.mcp_channel import mcp_resource
from app.models.oauth import OAuthRefreshFamily
from tests.auth_support import browser, capture_mail, console_ready, link_token, signed_in
from tests.mcp_support import mcp_app

NEW_PASSWORD = "Pelican-Orbit-Lantern-58"


@pytest.fixture()
def lifecycle_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    return api_env


async def _connected(env: Any, org_id: UUID, user_id: UUID) -> UUID:
    """A live grant of the person to the AI app "claude": what approving it on
    the consent screen leaves (Task 8)."""
    resource = mcp_resource(env.settings)
    assert resource is not None
    app_id = await mcp_app(env, org_id)
    async with oauth_session(org_id, env.app_engine) as session:
        family, _ = await grants.open_family(
            session,
            org_id=org_id,
            user_id=user_id,
            oauth_client_id=app_id,
            resource=resource,
            now=datetime.now(UTC),
        )
        family_id: UUID = family.id
    return family_id


async def _reason(env: Any, family_id: UUID) -> str | None:
    async with AsyncSession(env.admin_engine) as session:
        reason: str | None = (
            await session.execute(
                select(OAuthRefreshFamily.revoked_reason).where(OAuthRefreshFamily.id == family_id)
            )
        ).scalar_one()
    return reason


async def test_disabling_a_person_ends_their_ai_connections(lifecycle_env: Any) -> None:
    env = lifecycle_env
    org = await env.create_org("grant-disable")
    admin = await signed_in(env, org, "org-admin")
    member = await signed_in(env, org, "technician")
    theirs = await _connected(env, org, member.user_id)
    mine = await _connected(env, org, admin.user_id)

    resp = await env.client.post(f"/api/v1/users/{member.user_id}/disable", headers=admin.headers())

    assert resp.status_code == 200, resp.text
    assert await _reason(env, theirs) == "disabled"
    # Only that person's: the administrator's own connection stands.
    assert await _reason(env, mine) is None


async def test_deleting_a_person_ends_their_ai_connections(lifecycle_env: Any) -> None:
    env = lifecycle_env
    org = await env.create_org("grant-delete")
    admin = await signed_in(env, org, "org-admin")
    member = await signed_in(env, org, "technician")
    family = await _connected(env, org, member.user_id)

    resp = await env.client.delete(f"/api/v1/users/{member.user_id}", headers=admin.headers())

    assert resp.status_code == 204, resp.text
    assert await _reason(env, family) == "deleted"


async def test_an_administrators_factor_reset_ends_their_ai_connections(
    lifecycle_env: Any,
) -> None:
    env = lifecycle_env
    org = await env.create_org("grant-reset")
    admin = await signed_in(env, org, "org-admin")
    member = await signed_in(env, org, "technician")
    family = await _connected(env, org, member.user_id)

    resp = await env.client.post(
        f"/api/v1/users/{member.user_id}/factors/reset", headers=admin.headers()
    )

    assert resp.status_code == 204, resp.text
    assert await _reason(env, family) == "factors_reset"


async def test_changing_my_password_ends_my_ai_connections(lifecycle_env: Any) -> None:
    env = lifecycle_env
    org = await env.create_org("grant-password")
    who = await signed_in(env, org, "technician")
    family = await _connected(env, org, who.user_id)

    resp = await env.client.put(
        "/api/v1/me/password",
        headers=who.headers(),
        json={"current": who.password, "new": NEW_PASSWORD},
    )

    assert resp.status_code == 204, resp.text
    assert await _reason(env, family) == "password_changed"


async def test_signing_out_everywhere_ends_my_ai_connections(lifecycle_env: Any) -> None:
    env = lifecycle_env
    org = await env.create_org("grant-everywhere")
    who = await signed_in(env, org, "technician")
    family = await _connected(env, org, who.user_id)

    resp = await env.client.post("/api/v1/auth/logout-everywhere", headers=who.headers())

    assert resp.status_code == 204, resp.text
    assert await _reason(env, family) == "signed_out_everywhere"


async def test_a_password_reset_by_mail_ends_the_ai_connections(lifecycle_env: Any) -> None:
    env = lifecycle_env
    org = await env.create_org("grant-mail")
    who = await signed_in(env, org, "technician")
    family = await _connected(env, org, who.user_id)

    forgot = await env.client.post(
        "/api/v1/auth/password/forgot", headers=browser(), json={"email": who.email}
    )
    assert forgot.status_code == 202, forgot.text
    (mail,) = [m for m in env.mail.sent if "reset" in m.subject.lower()]
    resp = await env.client.post(
        "/api/v1/auth/password/reset",
        headers=browser(),
        json={"token": link_token(mail.text), "password": NEW_PASSWORD},
    )

    assert resp.status_code == 204, resp.text
    assert await _reason(env, family) == "password_changed"


async def test_the_recovery_link_ends_the_ai_connections(lifecycle_env: Any) -> None:
    env = lifecycle_env
    org = await env.create_org("grant-recover")
    who = await signed_in(env, org, "technician")
    family = await _connected(env, org, who.user_id)
    async with AsyncSession(env.admin_engine) as session:
        token = await action_tokens.issue(
            session,
            org_id=org,
            user_id=who.user_id,
            purpose=action_tokens.RECOVER,
            ttl=action_tokens.RECOVER_TTL,
            created_by=None,
        )
        await session.commit()

    resp = await env.client.post(
        "/api/v1/auth/recover/accept", headers=browser(), json={"token": token}
    )

    assert resp.status_code == 200, resp.text
    assert await _reason(env, family) == "factors_reset"


async def test_an_administrator_ending_sessions_leaves_the_ai_connections(
    lifecycle_env: Any,
) -> None:
    env = lifecycle_env
    org = await env.create_org("grant-sessions")
    admin = await signed_in(env, org, "org-admin")
    member = await signed_in(env, org, "technician")
    family = await _connected(env, org, member.user_id)

    resp = await env.client.delete(
        f"/api/v1/users/{member.user_id}/sessions", headers=admin.headers()
    )

    assert resp.status_code == 204, resp.text
    assert await _reason(env, family) is None
```

En `backend/tests/test_mcp_admin_api.py`, lo que fija el Step 4 (abajo, con el código). Para ver el rojo antes de
escribir el código basta con este archivo:

Run: `cd backend && .venv/bin/pytest -q tests/test_mcp_grant_lifecycle.py`
Expected: FAIL (seis de ocho: `_reason` es `None` después de cada acto; las dos que esperan `None` o el reparto por
persona pasan ya).

- [ ] **Step 2: `app/mcp/api.py`**

El docstring del módulo pasa a:

```python
"""The console's side of the MCP server: Settings → AI connections
(MCP server design §3.4, §7, §8; own authentication phase 3, plan 2026-11-06
Task 10).

    GET  /api/v1/mcp/connections                   `mcp.use`
    POST /api/v1/mcp/connections/{app_id}/revoke   `mcp.use`; "everyone"
                                                   also needs `orgs.manage`

Neither route is a tool (`app/mcp/exclusions.py::NOT_FOR_MCP`): the person
manages their AI connections in the console, not through one of them.

THE LIST IS DERIVED, NOT REGISTERED. An app "has access in this organization"
when it has acted here: the apps are the `app` (the token's `client_id`) of
this org's `mcp.tool_called` audit events, read under `org_session`, so RLS
keeps another tenant's use out as well as the `WHERE` clause does. Each one
carries the `id` and `name` of its registration here (`oauth_clients`, G) when
it has one; revoking names that `id`, because a metadata-document client's
`client_id` is a URL, which is no path segment (plan D12).

THE READ IS BOUNDED TO THE LAST 90 DAYS (`USAGE_WINDOW`) and to `MAX_APPS`
rows, AND INDEXED. `audit_events` is the largest table in the platform and
grows forever, and its only indexes are `(org_id, id)` and
`(org_id, target_type, target_id)` -- neither serves a `ts` window, so a window
alone would still read the org's whole audit history. Migration 0220 adds
`ix_audit_mcp_tool_called`, a partial index on `(org_id, ts) WHERE action =
'mcp.tool_called'`: this read is a range scan of one org's tool calls in the
window, and the index carries no other action's rows. The predicate here is
an inline SQL literal, never a bound parameter, so the planner can prove the
partial index applies even under a generic prepared-statement plan.

REVOKE IS ONE TRANSACTION (plan D12). The app's grants end -- the person's for
"me", everyone's in the organization for "everyone" -- together with the
`mcp_revocation` row and the `mcp.app_revoked` event, or none of them does:

- a grant that ended stops every token it issued at its next call, and its
  refresh token mints nothing (`deps.admit` reads the grant on every call,
  plan D6; `grants.refresh_grant`);
- the ROW keeps doing what it did (`app/mcp/server.py::_gate`): any token of
  that app issued before it is refused, and the listing says the app was
  revoked until it is used again.

No identity provider is called: the platform issued the grants, and ends them
itself. One organization's revocation never reaches another's users -- the
grants are read under its own `org_id` and RLS.
"""
```

Los imports pasan a:

```python
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_all_clients, require_permission
from app.core.audit import audit_service
from app.core.auth.oauth import grants
from app.core.auth.oauth.store import oauth_session
from app.core.i18n import tr
from app.core.mcp_channel import mcp_resource
from app.core.rbac import has_permission
from app.core.tenancy import org_session
from app.models import AuditEvent, Organization
from app.models.mcp import McpRevocation
from app.models.oauth import OAuthClient
```

(se van `asyncio`, `logging`, `httpx`, `Path`, `Settings`, `User`, el bloque de `kc_client` y `logger`). Se borran
`CLIENT_ID_PATTERN`, `FIRST_PARTY_CLIENTS`, `KEYCLOAK_CONCURRENCY`, `KEYCLOAK_BUDGET_SECONDS`, `record_revocation`,
`remove_consents` y `_record_incomplete`.

`McpAppOut` gana dos campos, después de `client_id`, cuya descripción cambia:

```python
    client_id: str = Field(
        description=(
            "The app's OAuth client id: the one this platform issued when it registered, "
            "or the address of its client metadata document."
        )
    )
    id: UUID | None = Field(
        description=(
            "The app's registration in this organization, which revoking names; null when it "
            "has none (an app last used before it registered here, or a registration that "
            "expired), and then there is nothing left to revoke."
        )
    )
    name: str | None = Field(
        description="The name the app registered with, as the app gave it; null when it gave none."
    )
```

`RevokeOut` pasa a:

```python
class RevokeOut(BaseModel):
    """The revocation is in force: the app's tokens stop at their next call."""

    revoked: bool = Field(
        description="Always true: the revocation is recorded and the app's grants have ended."
    )
    grants_revoked: int = Field(
        description=(
            "How many of the app's grants ended: yours for `me`, everyone's in this "
            "organization for `everyone`."
        )
    )
```

En `list_connections`, antes de `apps: list[McpAppOut] = []`:

```python
    registered = {
        client_id: (app_id, name)
        for client_id, app_id, name in (
            await session.execute(
                select(OAuthClient.client_id, OAuthClient.id, OAuthClient.client_name).where(
                    OAuthClient.org_id == org_id,
                    OAuthClient.client_id.in_([row[0] for row in usage]),
                )
            )
        ).all()
    }
```

y la construcción de cada `McpAppOut` gana `id=registered.get(client_id, (None, None))[0]` y
`name=registered.get(client_id, (None, None))[1]`.

El servicio nuevo, en lugar de `record_revocation` y `remove_consents`:

```python
async def revoke_app_access(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    app_id: UUID,
    scope: Literal["me", "everyone"],
    now: datetime,
) -> int:
    """End the app's grants -- the actor's for "me", everyone's in the
    organization for "everyone" -- and write the revocation row and its audit
    event, in the caller's one transaction (plan D12). How many grants ended.
    404 when this organization has no such app: the caller's transaction then
    writes nothing."""
    app = (
        await session.execute(
            select(OAuthClient).where(OAuthClient.id == app_id, OAuthClient.org_id == org_id)
        )
    ).scalar_one_or_none()
    if app is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, tr("there is no such AI app in this organization")
        )
    whose = None if scope == "everyone" else actor_id
    ended = await grants.revoke_app(
        session, oauth_client_id=app.id, user_id=whose, reason="revoked_in_settings", now=now
    )
    session.add(
        McpRevocation(org_id=org_id, user_id=whose, client_id=app.client_id, revoked_by=actor_id)
    )
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="mcp.app_revoked",
        metadata={"client_id": app.client_id, "scope": scope, "grants_revoked": ended},
    )
    return ended
```

La ruta:

```python
@router.post("/connections/{app_id}/revoke", response_model=RevokeOut)
async def revoke_connection(
    body: RevokeIn,
    request: Request,
    app_id: UUID,
    ctx: AuthContext = _MCP_USE,
) -> RevokeOut:
    """Revoke an AI app for yourself, or (with orgs.manage) for everyone in
    this organization: its grants end, and the tokens it holds stop at their
    next call."""
    # The route is gated on `mcp.use`, which "me" needs; "everyone" also needs
    # `orgs.manage`, and -- because it writes the organization's revocation
    # list, which is no one client's -- a scope of every client (MSP clients
    # spec §2.2). Refused before anything is written.
    if body.scope == "everyone":
        require_all_clients(ctx)
    # A person's grants are global rows (O under `users`, client NULL) that only
    # an every-client scope may write: `oauth_session`, the one place that opens
    # it over them (Task 5).
    async with oauth_session(ctx.org_id, request.app.state.app_engine) as session:
        # NO CLIENT, BY DESIGN (plan 2026-10-26 Task 14 keeps it): revoking
        # everyone writes the organisation's revocation list, which is no one
        # client's, so `orgs.manage` is asked on every client.
        if body.scope == "everyone" and not await has_permission(
            session, ctx.user_id, "orgs.manage"
        ):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, tr("requires {code}", code="orgs.manage")
            )
        ended = await revoke_app_access(
            session,
            org_id=ctx.org_id,
            actor_id=ctx.user_id,
            app_id=app_id,
            scope=body.scope,
            now=datetime.now(UTC),
        )
    return RevokeOut(revoked=True, grants_revoked=ended)
```

En `backend/app/mcp/exclusions.py`, la clave `("POST", "/api/v1/mcp/connections/{client_id}/revoke")` pasa a
`("POST", "/api/v1/mcp/connections/{app_id}/revoke")`. En `backend/app/core/i18n/es.py`, en el bloque `# MCP console
admin API (app/mcp/api.py)`, la entrada `"{client_id} is part of the platform, not an AI app, and cannot be revoked"`
se sustituye por:

```python
    "there is no such AI app in this organization": (
        "no existe esa aplicación de IA en esta organización"
    ),
```

- [ ] **Step 3: Los actos de la cuenta**

En `backend/app/modules/user_admin/service.py`, `backend/app/core/auth/account.py` y `backend/app/core/auth/flows.py`,
`from app.core.auth.oauth import grants` entre los imports de `app.core.auth`, y una línea después de cada
`await revoke_all_for_user(session, user.id)`, con el mismo sangrado:

| Archivo y función | Línea nueva |
|---|---|
| `user_admin/service.py::set_enabled` (dentro de `if not enabled:`) | `await grants.revoke_all_for_user(session, user.id, "disabled")` |
| `user_admin/service.py::reset_factors` | `await grants.revoke_all_for_user(session, user.id, "factors_reset")` |
| `user_admin/service.py::delete_user` | `await grants.revoke_all_for_user(session, user.id, "deleted")` |
| `core/auth/account.py::change_password` | `await grants.revoke_all_for_user(session, user.id, "password_changed")` |
| `core/auth/flows.py::logout_everywhere` | `await grants.revoke_all_for_user(session, user.id, "signed_out_everywhere")` |
| `core/auth/flows.py::reset_password` | `await grants.revoke_all_for_user(session, user.id, "password_changed")` |
| `core/auth/flows.py::accept_recovery` | `await grants.revoke_all_for_user(session, user.id, "factors_reset")` |

Son los siete sitios que `grep -rn "await revoke_all_for_user(session, user.id)" backend/app` lista hoy; si la fase 2
añade otro (un contacto: no tiene concesiones de IA, no pasa la puerta de la consola), no necesita la línea.

- [ ] **Step 4: Las pruebas de Ajustes, re-apuntadas**

En `backend/tests/test_mcp_admin_api.py`:

- El docstring del módulo: las viñetas segunda y tercera pasan a «`POST /api/v1/mcp/connections/{app_id}/revoke` ends
  the app's grants -- the person's, or the organization's -- and writes the revocation row and its audit event in ONE
  transaction; no identity provider is called (own authentication phase 3, plan 2026-11-06 Task 10)»; el párrafo de
  Keycloak pasa a «Keycloak is still `httpx.MockTransport`: for `kc_client.revoke_consent`'s own tests, which stay
  until phase 4, and to prove that revoking sends it nothing.»
- Imports: se añade `from app.models.oauth import OAuthClient, OAuthRefreshFamily`, `from app.core.auth.oauth import
  grants`, `from app.core.auth.oauth.store import oauth_session`, `from app.core.mcp_channel import mcp_resource` y
  `mcp_app` a los de `tests.mcp_support`.
- `ROUTES`: `("POST", "/api/v1/mcp/connections/{app_id}/revoke")`.
- Ayudantes nuevos y `_revoke`:

```python
async def _revoke(
    env: Any, org_id: UUID, sub: str, scope: str, app: UUID | str | None = None
) -> httpx.Response:
    """Revoke `app` (the org's "claude" when None: registered here if it was
    not yet) as `sub`."""
    target = app if app is not None else await mcp_app(env.api, org_id)
    return await env.api.client.post(
        f"/api/v1/mcp/connections/{target}/revoke",
        headers=await env.api.headers(org_id, sub),
        json={"scope": scope},
    )


async def _families(env: Any, org_id: UUID) -> list[OAuthRefreshFamily]:
    async with AsyncSession(env.api.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(OAuthRefreshFamily)
                    .where(OAuthRefreshFamily.org_id == org_id)
                    .order_by(OAuthRefreshFamily.created_at)
                )
            ).scalars()
        )


async def _live(env: Any, org_id: UUID, sub: str) -> int:
    user_id = await _user_id(env, sub)
    return sum(
        1 for f in await _families(env, org_id) if f.user_id == user_id and f.revoked_at is None
    )
```

- `test_two_people_using_one_app_list_as_one_app_with_two_people`: después de `assert app["client_id"] == "claude"`,
  `assert app["id"] == str(await mcp_app(env.api, org_id))` y `assert app["name"] == "claude"`.
- `test_revoke_for_me_cuts_off_my_old_token_and_not_my_colleagues`: la respuesta es `{"revoked": True,
  "grants_revoked": 1}`; las líneas de `env.kc.deleted` pasan a `assert env.kc.requests == []`; el evento, `{"client_id":
  "claude", "scope": "me", "grants_revoked": 1}`; y antes de los `_initialize`, `assert await _live(env, org_id, me) ==
  0` y `assert await _live(env, org_id, colleague) == 1`, con
  `assert {f.revoked_reason for f in await _families(env, org_id) if f.revoked_at} == {"revoked_in_settings"}`.
- `test_revoke_for_everyone_without_orgs_manage_is_403_and_writes_nothing`: `assert env.kc.deleted == []` pasa a
  `assert env.kc.requests == []`, y se añade `assert all(f.revoked_at is None for f in await _families(env, org_id))`.
- `test_revoke_for_everyone_cuts_off_the_whole_org_and_no_other`: dentro de `running_mcp`, antes de revocar, cada uno
  abre su concesión (`await _call_a_tool(env, slug, admin)`, `… colleague`, `… other_slug, stranger`); la respuesta es
  `{"revoked": True, "grants_revoked": 2}`; las cuatro líneas de Keycloak (`deleted`, `token_requests`, `clients`)
  pasan a `assert env.kc.requests == []`; el evento, `{"client_id": "claude", "scope": "everyone", "grants_revoked":
  2}`; y **antes** de los `_initialize` (cada `_old_token` abre una concesión nueva), `assert await _live(env, org_id,
  admin) == 0`, `… colleague) == 0` y `assert await _live(env, other_id, stranger) == 1`; el comentario «Registered apps are realm-wide» pasa a «A metadata-document client has the same
  `client_id` in every org: the other org's grant and token are untouched.»
- `test_keycloak_down_still_writes_the_revocation` → `test_revoking_calls_no_identity_provider`: fijaba que la
  revocación vale aunque Keycloak no conteste; ya no se le llama:

```python
async def test_revoking_calls_no_identity_provider(env: Any) -> None:
    org_id, slug = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    env.kc.raise_connect = True

    resp = await _revoke(env, org_id, me, "me")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"revoked": True, "grants_revoked": 0}
    assert env.kc.requests == []
    (row,) = await _rows(env, org_id)
    assert row.user_id == await _user_id(env, me)
    assert await _events(env, org_id, "mcp.app_revoke_incomplete") == []
    async with running_mcp(env.api):
        assert await _initialize(env, await _old_token(env, slug, me)) == 401
```

- `test_a_keycloak_refusal_counts_as_a_failure` → `test_a_revocation_is_written_whole_or_not_at_all`: fijaba qué queda
  escrito cuando una mitad falla; ahora no hay mitades:

```python
@pytest.mark.parametrize("fails", ["grants", "audit"])
async def test_a_revocation_is_written_whole_or_not_at_all(
    env: Any, monkeypatch: pytest.MonkeyPatch, fails: str
) -> None:
    """The grants, the row and the event are one transaction (plan D12): if
    ending the grants or writing the event fails, nothing is written."""
    from app.core.audit import audit_service
    from app.mcp import api as mcp_api

    org_id, slug = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    async with running_mcp(env.api):
        await _call_a_tool(env, slug, me)

    async def broken(*args: Any, **kw: Any) -> Any:
        raise OSError("database went away")

    if fails == "grants":
        monkeypatch.setattr(mcp_api.grants, "revoke_app", broken)
    else:
        real_record = audit_service.record

        async def flaky_record(session: Any, **kw: Any) -> Any:
            if kw.get("action") == "mcp.app_revoked":
                raise OSError("database went away")
            return await real_record(session, **kw)

        monkeypatch.setattr(audit_service, "record", flaky_record)
    app_id = await mcp_app(env.api, org_id)
    transport = httpx.ASGITransport(app=env.api.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as http:
        resp = await http.post(
            f"/api/v1/mcp/connections/{app_id}/revoke",
            headers={**await env.api.headers(org_id, me), "Origin": BASE},
            json={"scope": "me"},
        )

    assert resp.status_code == 500
    assert await _rows(env, org_id) == []
    assert await _events(env, org_id, "mcp.app_revoked") == []
    assert await _live(env, org_id, me) == 1
```

- `test_without_a_provisioning_client_every_person_is_a_failure` → `test_revoking_needs_no_provisioning_client`: fijaba
  que sin cliente de aprovisionamiento la revocación se queda a medias; ya no depende de él:

```python
async def test_revoking_needs_no_provisioning_client(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        KeycloakAdminConfig, "from_settings", classmethod(lambda cls, settings: None)
    )
    org_id, slug = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    async with running_mcp(env.api):
        await _call_a_tool(env, slug, me)
    resp = await _revoke(env, org_id, me, "me")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"revoked": True, "grants_revoked": 1}
    assert len(await _rows(env, org_id)) == 1
    assert env.kc.requests == []
```

- `test_consent_removal_is_bounded_in_concurrency_and_time` → `test_revoking_for_a_large_organization_is_one_statement`:
  fijaba que una organización grande no cuesta una llamada por persona; ahora es un `UPDATE`:

```python
async def test_revoking_for_a_large_organization_is_one_statement(env: Any) -> None:
    """Forty people with a grant each: one revocation ends all forty, inside
    the request, with no call per person."""
    org_id, _ = await _org(env)
    admin = await env.api.create_user(org_id, "org-admin")
    app_id = await mcp_app(env.api, org_id)
    resource = mcp_resource(env.api.settings)
    assert resource is not None
    people = [await env.api.create_user(org_id, "technician") for _ in range(40)]
    async with oauth_session(org_id, env.api.app_engine) as session:
        for sub in people:
            await grants.open_family(
                session,
                org_id=org_id,
                user_id=await _user_id(env, sub),
                oauth_client_id=app_id,
                resource=resource,
                now=datetime.now(UTC),
            )

    started = time.monotonic()
    resp = await _revoke(env, org_id, admin, "everyone")

    assert time.monotonic() - started < 5
    assert resp.json() == {"revoked": True, "grants_revoked": 40}
    assert all(f.revoked_at is not None for f in await _families(env, org_id))
    assert env.kc.requests == []
```

  con `from datetime import UTC, datetime` arriba (`_user_id` abre su propia sesión del motor de administración; se
  llama antes de entrar en `oauth_session` si el revisor lo prefiere, el resultado es el mismo).
- `test_a_dot_segment_client_id_is_422_and_reaches_nothing` → `test_a_dot_segment_is_no_app_id_and_reaches_nothing`:
  misma parametrización; `resp = await _revoke(env, org_id, admin, "everyone", app=dots)`; las aserciones finales pasan a
  `assert await _rows(env, org_id) == []`, `assert await _events(env, org_id, "mcp.app_revoked") == []` y `assert
  env.kc.requests == []`. El docstring: «A path segment that is not one UUID names no app: 422 before anything is read
  or written. Until own authentication phase 3 a dot segment here reached Keycloak's URL and deleted identities.»
- `test_a_bare_dot_reaches_nothing`: `app="."`; lo demás igual.
- `test_the_platforms_own_clients_cannot_be_revoked` → `test_the_platforms_own_clients_cannot_even_be_named`: misma
  lista; `resp = await _revoke(env, org_id, admin, "everyone", app=first_party)`, `assert resp.status_code == 422`, sin
  la aserción del `detail`; el docstring: «They have no registration in `oauth_clients`, and a name is no app id: 422,
  nothing written, nothing sent.»
- `test_the_first_party_refusal_is_in_the_persons_language` → `test_an_unknown_app_is_404_in_the_persons_language`:

```python
async def test_an_unknown_app_is_404_in_the_persons_language(env: Any) -> None:
    org_id, _ = await _org(env)
    admin = await env.api.create_user(org_id, "org-admin")
    resp = await env.api.client.post(
        f"/api/v1/mcp/connections/{uuid4()}/revoke",
        headers={**await env.api.headers(org_id, admin), "Accept-Language": "es"},
        json={"scope": "me"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "no existe esa aplicación de IA en esta organización"
    assert await _rows(env, org_id) == []
```

- `test_a_failed_incomplete_audit_never_fails_a_committed_revoke` → `test_another_organizations_app_is_404_and_writes_nothing`:
  fijaba que la revocación nunca queda a medias por un fallo posterior; eso lo fija ya la prueba de una transacción, y
  esta fija lo que la ruta nueva tiene que añadir: que un id de otra organización no termina nada:

```python
async def test_another_organizations_app_is_404_and_writes_nothing(env: Any) -> None:
    org_id, _ = await _org(env)
    admin = await env.api.create_user(org_id, "org-admin")
    other_id, other_slug = await _org(env)
    stranger = await env.api.create_user(other_id, "org-admin")
    async with running_mcp(env.api):
        await _call_a_tool(env, other_slug, stranger)
    theirs = await mcp_app(env.api, other_id)

    resp = await _revoke(env, org_id, admin, "everyone", app=theirs)

    assert resp.status_code == 404, resp.text
    assert await _rows(env, org_id) == [] and await _rows(env, other_id) == []
    assert await _live(env, other_id, stranger) == 1
```

- `test_a_malformed_client_id_is_422_and_writes_nothing` → `test_a_malformed_app_id_is_422_and_writes_nothing`: misma
  parametrización, `app=bad`, y `assert env.kc.deleted == []` pasa a `assert env.kc.requests == []`.
- `test_a_client_id_cannot_add_a_path_segment` → `test_an_app_id_cannot_add_a_path_segment`: `app="a%2F..%2Fb"`, ídem.
- `test_a_malformed_body_is_422`: la URL pasa a `f"/api/v1/mcp/connections/{await mcp_app(env.api, org_id)}/revoke"`
  (antes `claude`, que hoy ya sería 422 por la ruta y no por el cuerpo).
- `test_revoking_needs_mcp_use`, las pruebas de `revoke_consent` (Keycloak, fase 4), la del interruptor, la de
  exclusiones y las del índice no cambian.

En `backend/tests/test_global_writes.py`: la clave de `NOT_GLOBAL` pasa a `("POST",
"/api/v1/mcp/connections/{app_id}/revoke")`, y en `test_revoking_an_ai_app_for_everyone_needs_every_client`, `url =
f"/api/v1/mcp/connections/{await mcp_app(env, org_id)}/revoke"`, con `from tests.mcp_support import mcp_app` dentro de
la función, junto al `from sqlalchemy import update` que ya tiene.

- [ ] **Step 5: El recuento de logs**

En `backend/tests/test_logging_discipline.py`, en el docstring de la prueba del recuento, antes del párrafo «NOW 109, own
authentication phase 3 (plan 2026-11-06, Task 3)», añadir:

```text
    NOW 106, own authentication phase 3 (plan 2026-11-06, Task 10): three
    sites fewer, all in `app/mcp/api.py` -- the two WARNINGs of
    `remove_consents` and the one of `_record_incomplete`. Revoking an AI app
    no longer calls the identity provider: its grants end in the same
    transaction as the revocation row and its audit event, so there is no
    best-effort half left to report.

```

y cambiar «NOW 109, own authentication phase 3 (plan 2026-11-06, Task 3)» por «BEFORE THAT, 109, own authentication
phase 3 (plan 2026-11-06, Task 3)», y `assert total == 109` / `expected 109` por `106`. En
`docs/superpowers/specs/2026-08-14-logging-kernel-design.md`, §2.2, antes de «**NOW 109, own authentication phase 3
(plan 2026-11-06, Task 3).**», el mismo texto en prosa («**NOW 106, own authentication phase 3 (plan 2026-11-06, Task
10).** Three sites fewer…»), y «**NOW 109,» pasa a «**BEFORE THAT, 109,». El delta de esta tarea es **−3**; si al
fusionar la base no es 109 (la fase 2), el coordinador ajusta el número.

- [ ] **Step 6: Las pruebas pasan**

Run: `cd backend && .venv/bin/pytest -q tests/test_mcp_grant_lifecycle.py tests/test_mcp_admin_api.py tests/test_global_writes.py tests/test_logging_discipline.py tests/test_i18n_catalog.py tests/test_mcp_channel_api.py tests/test_mcp_catalog.py tests/test_ai_tool_surface.py tests/test_user_lifecycle.py tests/test_auth_my_security.py tests/test_auth_password_reset.py tests/test_auth_notice_order.py tests/test_route_gating.py`
Expected: PASS.

Run: `cd frontend && npx vitest run src/modules/audit src/i18n`
Expected: PASS (ninguna acción nueva; `mcp.app_revoked` ya está en el vocabulario).

Run: `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `Success: no issues found` y `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/mcp/api.py backend/app/mcp/exclusions.py backend/app/core/i18n/es.py \
  backend/app/modules/user_admin/service.py backend/app/core/auth/account.py backend/app/core/auth/flows.py \
  backend/tests/test_mcp_admin_api.py backend/tests/test_global_writes.py backend/tests/test_mcp_grant_lifecycle.py \
  backend/tests/test_logging_discipline.py docs/superpowers/specs/2026-08-14-logging-kernel-design.md
git commit -m "feat(mcp): revoking an AI app ends its grants in one transaction; account acts end them too"
```

### Task 11: Consola — Ajustes → Conexiones de IA: revocar por id, el nombre del cliente

Diseño §5.3; D12. Oleada 1, en paralelo con las Tasks 1–4: se escribe contra el contrato del Apéndice A.2 y la Task 10
lo implementa en el servidor. Solo consola.

Hoy la pestaña revoca por `client_id` en la ruta (`/mcp/connections/{client_id}/revoke`) y avisa cuando Keycloak no
pudo retirar el consentimiento de alguien (`keycloak_failures`). Con el servidor propio, el `client_id` de un cliente CIMD
es una URL que no cabe en un segmento de ruta, y la revocación es una sola transacción sin mitad que pueda fallar. Así
que: la fila trae `id` (el de la fila del cliente; `null` si ya no tiene registro aquí) y `name` (el nombre que el
cliente dio), se revoca por `id`, y el resultado es `{revoked, grants_revoked}`.

**Files:**
- Modify: `frontend/src/lib/api.ts` (`McpAppOut`, `McpRevokeOut`, `revokeMcpConnection` y sus comentarios,
  `:1004-1043` y `:5996-6014` hoy; el comentario de `RequestRoute.global`, `:1633-1640`)
- Modify: `frontend/src/modules/settings/api.ts` (`useRevokeMcpConnection`, `:239-256`)
- Modify: `frontend/src/modules/settings/AiConnectionsTab.tsx`
- Modify: `frontend/src/modules/settings/fixtures.ts` (`MCP_APPS`, `revokeMcpConnection`, `:425-448`)
- Modify: `frontend/src/modules/settings/__tests__/harness.tsx` (`MCP_APP_FIXTURE`, `:209-215`; `:648`)
- Modify: `frontend/src/i18n/en/settings.ts`, `frontend/src/i18n/es/settings.ts` (`aiConnections`)
- Test: `frontend/src/modules/settings/__tests__/aiConnections.test.tsx`, `frontend/src/modules/settings/settings.es.test.tsx`
  (`:880-895`), `frontend/src/lib/api.test.ts` (`:38-53`), `frontend/src/lib/api.clientScope.test.ts` (`:108-152`)

**Interfaces:**
- Consumes: el contrato del Apéndice A.2 (`GET /api/v1/mcp/connections` con `id` y `name` por app;
  `POST /api/v1/mcp/connections/{app_id}/revoke` → `{"revoked": true, "grants_revoked": int}`).
- Produces: `McpAppOut.id: string | null`, `McpAppOut.name: string | null`, `McpRevokeOut = { revoked: boolean;
  grants_revoked: number }`, `ApiClient.revokeMcpConnection(appId: string, scope: McpRevokeScope)`,
  `useRevokeMcpConnection()` con variables `{ appId: string; label: string; scope: McpRevokeScope }`.

**Pruebas que cambian a propósito (re-apuntadas, no borradas).** Las tres de `aiConnections.test.tsx` sobre
`keycloak_failures` («warns when sign-in for some people could not be withdrawn…», «words a one-person failure in the
singular», «words a failed revoke for me as being about you…») y la de `settings.es.test.tsx` («words the warning when
sign-in could not be withdrawn…») probaban la mitad de Keycloak, que desaparece. Pasan a probar lo que la ocupa: que
una revocación nunca anuncia un resultado parcial (siempre la frase de revocada, cuente `grants_revoked` lo que cuente),
que una app sin registro no ofrece revocar, y que se nombra por su nombre con su id debajo, en los dos idiomas. Las de
`api.test.ts` y `api.clientScope.test.ts` cambian la ruta de ejemplo a un UUID.

- [ ] **Step 1: Las pruebas que fallan**

En `frontend/src/modules/settings/__tests__/harness.tsx`, `MCP_APP_FIXTURE` pasa a:

```ts
/** The `oauth_clients` row of `MCP_APP_FIXTURE` (`id`): what a revoke names. */
export const MCP_APP_ID = "7a1c2e9b-4f3d-4c55-9a0e-2b6d8f1e3c70";

export const MCP_APP_FIXTURE = {
  id: MCP_APP_ID,
  client_id: "claude",
  name: null,
  people: 3,
  last_used_at: "2026-10-01T09:30:00Z",
  revoked_for_me: false,
  revoked_for_everyone: false,
};
```

(el comentario de arriba gana: «`name` null, so the row reads its client id, as an app that registered no name»), y
la línea `:648` pasa a `calls.revokeMcpConnection.mockResolvedValue({ revoked: true, grants_revoked: 1 });`.

En `frontend/src/modules/settings/__tests__/aiConnections.test.tsx`: el import gana `MCP_APP_ID`; cada
`toHaveBeenCalledWith("claude", "everyone")` y `toHaveBeenCalledWith("claude", "me")` pasa a
`toHaveBeenCalledWith(MCP_APP_ID, "everyone")` y `(MCP_APP_ID, "me")`; y las tres pruebas de `keycloak_failures` se
sustituyen por estas tres:

```tsx
  it("never announces a partial revoke: the app is revoked, however many sign-ins it held", async () => {
    for (const grants of [0, 2]) {
      calls.revokeMcpConnection.mockResolvedValue({ revoked: true, grants_revoked: grants });
      const user = userEvent.setup();
      const { unmount } = openTab();
      await user.click(await screen.findByRole("button", { name: /Revoke for everyone: claude/ }));
      await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Revoke for everyone" }));
      expect(await screen.findByText("claude is revoked for everyone in this organization.")).toBeInTheDocument();
      expect(screen.queryByText(/reconnect without asking/)).toBeNull();
      unmount();
    }
  });

  it("names an app by the name it registered, with its client id beneath", async () => {
    calls.getMcpConnections.mockResolvedValue({
      ...MCP_CONNECTIONS_FIXTURE,
      apps: [{ ...MCP_APP_FIXTURE, name: "Claude", client_id: "itms_Zm9vYmFy" }],
    });
    const user = userEvent.setup();
    openTab();
    const row = (await screen.findByText("Claude")).closest("tr") as HTMLElement;
    expect(within(row).getByText("itms_Zm9vYmFy")).toBeInTheDocument();
    await user.click(within(row).getByRole("button", { name: "Revoke for me: Claude" }));
    await waitFor(() => expect(calls.revokeMcpConnection).toHaveBeenCalledWith(MCP_APP_ID, "me"));
    expect(await screen.findByText("Claude is revoked for you.")).toBeInTheDocument();
  });

  it("offers no revoke for an app that no longer has a registration here", async () => {
    calls.getMcpConnections.mockResolvedValue({
      ...MCP_CONNECTIONS_FIXTURE,
      apps: [{ ...MCP_APP_FIXTURE, id: null }],
    });
    openTab();
    const row = (await screen.findByText("claude")).closest("tr") as HTMLElement;
    expect(within(row).getByText("No longer registered here, so it holds no access.")).toBeInTheDocument();
    expect(within(row).queryByRole("button")).toBeNull();
  });
```

En `frontend/src/modules/settings/settings.es.test.tsx`, la prueba «words the warning when sign-in could not be
withdrawn, for one person and for you» se sustituye por:

```tsx
  it("words a revoke and an app with no registration, in Spanish", async () => {
    calls.revokeMcpConnection.mockResolvedValue({ revoked: true, grants_revoked: 1 });
    const user = userEvent.setup();
    const { container, unmount } = await openIn("ai-connections");
    await user.click(await screen.findByRole("button", { name: "Revocar para todos: claude" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Revocar para todos" }));
    expect(
      await screen.findByText("Se revocó claude para todas las personas de esta organización."),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Revocar para mí: claude" }));
    expect(await screen.findByText("Se revocó claude para ti.")).toBeInTheDocument();
    expectNoEnglish(container, [...RECORDED, "claude"]);
    unmount();

    calls.getMcpConnections.mockResolvedValue({
      ...MCP_CONNECTIONS_FIXTURE,
      apps: [{ ...MCP_APP_FIXTURE, id: null }],
    });
    const again = await openIn("ai-connections");
    expect(await screen.findByText("Ya no está registrada aquí, así que no tiene acceso.")).toBeInTheDocument();
    expectNoEnglish(again.container, [...RECORDED, "claude"]);
  });
```

(`openIn` devuelve lo que devuelve `renderSettings`, que extiende el resultado de `render`, así que trae `unmount`; el
import del archivo gana `MCP_APP_FIXTURE` junto a `MCP_CONNECTIONS_FIXTURE`.)

En `frontend/src/lib/api.test.ts`, la prueba de `:40-53` pasa a:

```ts
  // The app is named by its row id, one UUID path segment: a client id can be
  // an https URL (a client metadata document), which no path segment holds.
  it("puts the AI app's id in the revoke path, with the scope in the body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ revoked: true, grants_revoked: 2 }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient(vi.fn());
    const result = await client.revokeMcpConnection("7a1c2e9b-4f3d-4c55-9a0e-2b6d8f1e3c70", "everyone");
    expect(result).toEqual({ revoked: true, grants_revoked: 2 });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/mcp/connections/7a1c2e9b-4f3d-4c55-9a0e-2b6d8f1e3c70/revoke");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ scope: "everyone" });
  });
```

En `frontend/src/lib/api.clientScope.test.ts:108-152`, `"some-ai-app"` pasa a `"7a1c2e9b-4f3d-4c55-9a0e-2b6d8f1e3c70"`
en las llamadas y en las rutas, y el docstring dice `/mcp/connections/{app_id}/revoke`. Lo que prueban no cambia.

Run: `cd frontend && npx vitest run src/modules/settings src/lib/api.test.ts src/lib/api.clientScope.test.ts`
Expected: FAIL (el tipo `McpAppOut` no tiene `id`; la pestaña revoca por `client_id`; no existe
`apps.unregistered`).

- [ ] **Step 2: Los tipos y el método**

En `frontend/src/lib/api.ts`, `McpAppOut` pasa a:

```ts
/**
 * One AI app that called tools in this organization in the last 90 days
 * (`backend/app/mcp/api.py::McpAppOut`).
 */
export interface McpAppOut {
  /** Its registration here (`oauth_clients`), what a revoke names; null when it has none any more. */
  id: string | null;
  /** The app's OAuth client id: what it registered, or its metadata document's URL. */
  client_id: string;
  /** The name the app gave itself, or null. Anyone can call an app anything. */
  name: string | null;
  /** How many people of this organization used it in the last 90 days. */
  people: number;
  /** When anyone here last called a tool through it. */
  last_used_at: string | null;
  /** You revoked it for yourself after your last use of it. */
  revoked_for_me: boolean;
  /** It was revoked for the organization after its last use. */
  revoked_for_everyone: boolean;
}
```

`McpRevokeOut`:

```ts
/**
 * `POST /mcp/connections/{app_id}/revoke`. In force when it returns: every
 * sign-in the app held for whom it was revoked is ended in the same
 * transaction (own-auth plan 2026-11-06, D12), so there is no half that can
 * fail. `grants_revoked` counts them.
 */
export interface McpRevokeOut {
  revoked: boolean;
  grants_revoked: number;
}
```

`revokeMcpConnection`:

```ts
  /**
   * Revoke an AI app for the caller or for the whole organization, by its
   * registration's id (a UUID: a client id can be a URL).
   *
   * FOR EVERYONE IT IS GLOBAL, BY ITS BODY (phase 3 design §1.2): the server
   * refuses `scope: "everyone"` narrowed by `X-Client-Id` (403
   * ALL_CLIENTS_REQUIRED), and `globalRoutes.json` cannot tell the two scopes
   * apart, so this request says it. `scope: "me"` is narrowed like any other.
   */
  revokeMcpConnection(appId: string, scope: McpRevokeScope): Promise<McpRevokeOut> {
    return this.request<McpRevokeOut>(
      `/mcp/connections/${encodeURIComponent(appId)}/revoke`,
      { method: "POST", body: JSON.stringify({ scope }) },
      { global: scope === "everyone" },
    );
  }
```

y en el comentario de `RequestRoute.global`, `POST /mcp/connections/{client_id}/revoke` pasa a
`POST /mcp/connections/{app_id}/revoke`.

- [ ] **Step 3: El hook, la pestaña y los textos**

En `frontend/src/modules/settings/api.ts`:

```ts
/**
 * A revoke. The result is kept on the mutation (`data`, `variables`) and
 * worded at render, so the outcome re-words on a language switch. `label` is
 * the app as the row shows it, for that sentence.
 */
export function useRevokeMcpConnection() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<McpRevokeOut, ApiError, { appId: string; label: string; scope: McpRevokeScope }>({
    mutationFn: ({ appId, scope }) => api.revokeMcpConnection(appId, scope),
    onSuccess: () => {
      // The row's "revoked for you / for everyone" flags moved.
      void queryClient.invalidateQueries({ queryKey: [MCP_CONNECTIONS_KEY] });
    },
  });
}
```

En `frontend/src/modules/settings/AiConnectionsTab.tsx`:

1. En el comentario de cabecera, el párrafo «A REVOKE IS IN FORCE WHEN IT RETURNS, AND THE SCREEN SAYS WHAT IS NOT.»
   se sustituye por:

   ```tsx
   // A REVOKE IS IN FORCE WHEN IT RETURNS. The server records the revocation
   // and ends every sign-in the app holds for whom it was revoked, in one
   // transaction (own-auth plan 2026-11-06, D12): there is no second half that
   // can fail, so the screen never says "may still reconnect". The app is
   // named by its registration's id; one that has none any more holds no
   // access and offers no revoke.
   ```

   y la primera línea pasa a «`POST /mcp/connections/{app_id}/revoke`».
2. `revokeFor` pasa a:

   ```tsx
     function revokeFor(app: McpAppOut, scope: "me" | "everyone") {
       if (app.id !== null) revoke.mutate({ appId: app.id, label: app.name ?? app.client_id, scope });
     }
   ```

   `confirming` pasa a guardar la app (`useState<McpAppOut | null>(null)`), la `ConfirmDialog` de «para todos» usa
   `subject={confirming ? confirming.name ?? confirming.client_id : ""}` y `revokeFor(target, "everyone")`, y la fila
   llama `onRevokeForMe={() => revokeFor(app, "me")}` y `onRevokeForEveryone={() => setConfirming(app)}`.
3. `<RevokeOutcome ... />` pasa a `label={revoke.variables?.label}` y `scope={revoke.variables?.scope}` y
   `done={revoke.data !== undefined}`, sin `clientId` ni `failures`; y `RevokeOutcome` a:

   ```tsx
   /**
    * What the last revoke did, worded from the data the server returned.
    *
    * Nothing while one is in flight: an outcome from the previous revoke beside a
    * request that has not answered yet would describe the wrong act.
    */
   function RevokeOutcome({
     pending,
     error,
     label,
     scope,
     done,
   }: {
     pending: boolean;
     error: unknown;
     label: string | undefined;
     scope: "me" | "everyone" | undefined;
     done: boolean;
   }) {
     const t = useT();
     if (pending || label === undefined || scope === undefined) return null;
     if (error != null) {
       return (
         <p role="alert" className="notice red" style={{ margin: "16px 0" }}>
           <Icon name={KIND_ICON.crit} size={16} />
           <span>{describeSubmitError(error)}</span>
         </p>
       );
     }
     if (!done) return null;
     return (
       <p role="status" className="notice" style={{ margin: "16px 0" }}>
         <Icon name={KIND_ICON.ok} size={16} />
         <span>
           {scope === "everyone"
             ? t("settings.aiConnections.revoked.everyone", { app: label })
             : t("settings.aiConnections.revoked.me", { app: label })}
         </span>
       </p>
     );
   }
   ```
4. `AppRow`: la etiqueta es `const label = app.name ?? app.client_id;`; la celda del nombre:

   ```tsx
         <Td lead={<Icon name="sparkles" size={18} />}>
           {/* What the app registered: recorded, not translated. */}
           <div className="cell-title">{label}</div>
           {app.name !== null ? <div className="cell-sub mono">{app.client_id}</div> : null}
           {state ? <div className="cell-sub">{state}</div> : null}
           {app.id === null ? (
             <div className="cell-sub">{t("settings.aiConnections.apps.unregistered")}</div>
           ) : null}
         </Td>
   ```

   los `aria-label` usan `{ app: label }`, y la celda de acciones solo pinta los botones con `app.id !== null`
   (`{app.id !== null ? (<div className="flex flex-wrap gap-2">…</div>) : null}`). El comentario «A revoke is offered
   again after one: it is how a revoke whose sign-in half failed for some people is retried.» pasa a «A revoke is
   offered again after one: an app that reconnected since is revoked again the same way.». La `key` de la fila sigue
   siendo `app.client_id`.

En `frontend/src/i18n/en/settings.ts`, dentro de `aiConnections.apps`, después de `revokeEveryoneAria`:

```ts
      unregistered: "No longer registered here, so it holds no access.",
```

y se quitan `partialMe` y `partial` de `aiConnections`. En `frontend/src/i18n/es/settings.ts`, lo mismo:

```ts
      unregistered: "Ya no está registrada aquí, así que no tiene acceso.",
```

y se quitan `partialMe` y `partial`.

En `frontend/src/modules/settings/fixtures.ts`:

```ts
const MCP_APPS: McpAppOut[] = [
  {
    id: "0b6f3c1e-8d2a-4e7b-9c5f-1a2b3c4d5e6f",
    client_id: "itms_Q2xhdWRlRGVza3RvcEZpeHR1cmU",
    name: "Claude",
    people: 4,
    last_used_at: ago(12),
    revoked_for_me: false,
    revoked_for_everyone: false,
  },
  {
    id: "5d4c3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a",
    client_id: "https://helpdesk.example/oauth/client.json",
    name: "Helpdesk assistant",
    people: 1,
    last_used_at: ago(3 * 24 * 60),
    revoked_for_me: true,
    revoked_for_everyone: false,
  },
];
```

y

```ts
const revokeMcpConnection: FixtureMap["revokeMcpConnection"] = (appId, scope) => {
  const app = MCP_APPS.find((candidate) => candidate.id === appId);
  if (!app) throw notFound("AI app");
  if (scope === "everyone") app.revoked_for_everyone = true;
  else app.revoked_for_me = true;
  return { revoked: true, grants_revoked: app.people };
};
```

- [ ] **Step 4: Las pruebas pasan**

Run: `cd frontend && npx vitest run`
Expected: PASS, todo.

Run: `cd frontend && npx tsc -b && npm run build`
Expected: sin errores.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/api.ts frontend/src/modules/settings/api.ts frontend/src/modules/settings/AiConnectionsTab.tsx \
  frontend/src/modules/settings/fixtures.ts frontend/src/modules/settings/__tests__/harness.tsx \
  frontend/src/modules/settings/__tests__/aiConnections.test.tsx frontend/src/modules/settings/settings.es.test.tsx \
  frontend/src/i18n/en/settings.ts frontend/src/i18n/es/settings.ts frontend/src/lib/api.test.ts \
  frontend/src/lib/api.clientScope.test.ts
git commit -m "feat(console): AI connections revoke an app by its registration, with no identity-provider half"
```

### Task 12: Cierre: el falsificador 6 de §10 de punta a punta, documentos y recuentos

Diseño §9, §10 (falsificador 6); §8 fase 3. Oleada 6, sola, sobre el árbol con todas las demás fusionadas. Backend y
documentos.

Lo que ninguna tarea anterior fija sola: el falsificador 6 de §10 («Reutilizar un refresh token revoca su familia; un
token MCP con otra audiencia se rechaza; el token interno no vale en rutas públicas») sobre las rutas reales, de punta
a punta; la conexión entera tal como la hace Claude, con un técnico que la plataforma invitó (el D5 de la fase 1: hasta
esta fase no podía conectar una app); el runbook y los documentos que dicen «hasta la fase 3»; y los recuentos que
mueven varias tareas, comprobados en el árbol fusionado.

**Files:**
- Create: `backend/tests/test_oauth_falsifiers.py`
- Modify: `deploy/README.md` (la sección «AI connections (MCP)»; tres frases de otras secciones; una fila de la tabla
  del cliente de aprovisionamiento)
- Modify: `deploy/.env.example` (los comentarios de `KC_HOSTNAME` y de `ITS_MCP_REGISTRATION`)
- Modify: `docs/superpowers/specs/2026-10-09-mcp-server-design.md` (§3, enmienda fechada)
- Modify: `docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` (§7.2, enmienda fechada)
- Modify: `docs/03-security-model.md` (línea 161), `docs/modules/console-auth/spec.md` (líneas 98–99)
- Modify (comentarios): `backend/app/modules/user_admin/service.py`, `backend/app/modules/user_admin/__init__.py`,
  `backend/app/api/v1/me.py`, `backend/app/core/auth/__init__.py`, `backend/tests/test_user_admin_api.py`,
  `backend/tests/conftest.py`

**Interfaces:**
- Consumes: Task 6 (`tests.oauth_support.register`), Task 7 (`token_request`), Task 8 (`connect`), Task 9
  (`tests.mcp_support.mcp_claims`, `signed_mcp_token`), Task 5 (`oauth_org`, `oauth_http`, `BASE`, `RESOURCE`).
  Existentes: `ApiEnv.loopback_bearer(org_id, sub)`, `ApiEnv.create_user(..., sub=)`, `tests.mcp_support.mcp_session`,
  `marked_client`, `mcp_client`, `running_mcp`, `app.modules.user_admin.service.LOCAL_SUBJECT_PREFIX`.
- Produces: nada que otra tarea consuma.

- [ ] **Step 1: Las pruebas**

`backend/tests/test_oauth_falsifiers.py`:

```python
"""Falsifier 6 of autenticación propia design §10, end to end over the real
routes (plan 2026-11-06, Task 12), and an AI app connected the whole way.

§10.6: "Reutilizar un refresh token revoca su familia; un token MCP con otra
audiencia se rechaza; el token interno no vale en rutas públicas." Each clause
is a test that goes red when its protection goes:

- drop the family check from `grants.refresh_grant`, and the reused refresh
  token mints a token; drop `family_is_live` from `deps.admit`, and the
  family's newest access token still works;
- drop the exact-audience check from `access.verify_access_token`, and a token
  of ours for two audiences is admitted;
- let `/mcp`, `/oauth/token` or the MCP door read the assistant's internal
  token, and it is admitted.

And the whole way, as Claude does it -- register, consent in the console,
exchange the code, call a tool -- by a technician the platform invited, whose
subject no identity provider issued (phase 1's D5, closed by this phase).
"""

from typing import Any
from uuid import uuid4

import httpx
import pytest
from mcp.types import LATEST_PROTOCOL_VERSION
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent
from app.modules.user_admin.service import LOCAL_SUBJECT_PREFIX
from tests.mcp_support import (
    marked_client,
    mcp_claims,
    mcp_client,
    mcp_session,
    running_mcp,
    signed_mcp_token,
)
from tests.oauth_support import (
    BASE,
    RESOURCE,
    connect,
    oauth_http,
    oauth_org,
    register,
    token_request,
)
from tests.session_support import user_id_of

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": LATEST_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0"},
    },
}


async def _initialize(env: Any, authorization: str) -> int:
    async with mcp_client(env) as client:
        resp = await client.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": authorization}
        )
    return resp.status_code


async def _refresh(env: Any, client_id: str, refresh_token: str) -> httpx.Response:
    return await token_request(
        env, grant_type="refresh_token", refresh_token=refresh_token, client_id=client_id
    )


async def test_reusing_a_refresh_token_ends_its_family_and_what_it_issued(api_env) -> None:
    org_id, _ = await oauth_org(api_env)
    sub = await api_env.create_user(org_id, "org-admin")
    first = await connect(api_env, org_id, sub)
    async with running_mcp(api_env):
        assert await _initialize(api_env, f"Bearer {first['access_token']}") == 200
        rotated = await _refresh(api_env, first["client_id"], first["refresh_token"])
        assert rotated.status_code == 200, rotated.text
        second = rotated.json()
        assert await _initialize(api_env, f"Bearer {second['access_token']}") == 200

        reused = await _refresh(api_env, first["client_id"], first["refresh_token"])

        assert (reused.status_code, reused.json()["error"]) == (400, "invalid_grant")
        # The whole family ended: its newest access token stops at the next
        # call, not in ten minutes (plan D6) ...
        assert await _initialize(api_env, f"Bearer {second['access_token']}") == 401
    # ... and its newest refresh token mints nothing.
    after = await _refresh(api_env, first["client_id"], second["refresh_token"])
    assert (after.status_code, after.json()["error"]) == (400, "invalid_grant")


@pytest.mark.parametrize(
    "aud",
    [BASE, [RESOURCE, BASE], "https://elsewhere.example/mcp"],
    ids=["the-console", "two-audiences", "another-server"],
)
async def test_an_mcp_token_for_another_audience_is_refused(api_env, aud: Any) -> None:
    org_id, _ = await oauth_org(api_env)
    sub = await api_env.create_user(org_id, "org-admin")
    issued = await connect(api_env, org_id, sub)
    claims = mcp_claims(issued["access_token"])
    claims["aud"] = aud
    forged = await signed_mcp_token(claims)
    async with running_mcp(api_env):
        assert await _initialize(api_env, f"Bearer {forged}") == 401
        # Non-vacuity: the token the server issued is admitted.
        assert await _initialize(api_env, f"Bearer {issued['access_token']}") == 200
    async with marked_client(api_env, azp=issued["client_id"]) as client:
        me = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {forged}"})
    assert me.status_code == 401


async def test_the_internal_token_is_nothing_on_a_public_route(api_env) -> None:
    """The assistant's internal token (design §5.6) is admitted only on the
    assistant's own in-process call, which says so (`X-ITMS-Loopback`). On
    `/mcp`, on the OAuth server and on an MCP call it is nothing."""
    org_id, _ = await oauth_org(api_env)
    sub = await api_env.create_user(org_id, "org-admin")
    app = await register(api_env)
    bearer = await api_env.loopback_bearer(org_id, sub)

    async with running_mcp(api_env):
        mcp = await _initialize(api_env, bearer)
    async with oauth_http(api_env) as client:
        token = await client.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": "x", "client_id": app["client_id"]},
            headers={"Authorization": bearer},
        )
    async with marked_client(api_env, azp=app["client_id"]) as client:
        marked = await client.get("/api/v1/me", headers={"Authorization": bearer})
    unmarked = await api_env.client.get("/api/v1/me", headers={"Authorization": bearer})

    assert mcp == 401
    assert (token.status_code, token.json()["error"]) == (401, "invalid_client")
    assert (marked.status_code, marked.json()["detail"]) == (401, "this access token was refused")
    assert unmarked.status_code == 401


async def test_an_invited_technician_connects_an_ai_app_the_whole_way(api_env) -> None:
    """Phase 1 left an invited technician -- whose subject no provider issued
    (`LOCAL_SUBJECT_PREFIX`) -- unable to connect an AI app until this phase
    (its D5). Registered, approved in the console, exchanged, and a tool
    called: as that person, audited with the app's client id."""
    org_id, _ = await oauth_org(api_env)
    sub = await api_env.create_user(org_id, "org-admin", sub=f"{LOCAL_SUBJECT_PREFIX}{uuid4()}")
    issued = await connect(api_env, org_id, sub)

    async with running_mcp(api_env), mcp_session(api_env, issued["access_token"]) as session:
        result = await session.call_tool("me_get", {})

    assert result.is_error is False, result
    async with AsyncSession(api_env.admin_engine) as session:
        (event,) = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "mcp.tool_called"
                )
            )
        ).scalars().all()
    assert event.actor_id == await user_id_of(api_env, sub)
    assert event.meta["app"] == issued["client_id"]
    assert issued["client_id"].startswith("itms_")
```

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_falsifiers.py`
Expected: PASS sobre el árbol fusionado (las Tasks 1–11 ya construyeron lo que prueba). Para ver cada falsificador en
rojo una vez, antes de dar la tarea por buena: quitar la línea `if not await grants.family_is_live(...)` de
`deps.admit` (la primera prueba falla en `== 401`), devolverla; cambiar `claims["aud"] != aud` por `aud not in
claims["aud"]` en `access.verify_access_token` (falla `two-audiences`), devolverlo. El informe de la tarea dice las dos
salidas rojas.

- [ ] **Step 2: El runbook**

En `deploy/README.md`:

1. Línea 32, «Keycloak still signs MCP clients in until phase 3.» pasa a «Since phase 3 AI apps connect over MCP
   through the platform's own OAuth server too; Keycloak stays deployed, out of every sign-in path, until phase 4
   removes it.»
2. Líneas 1204–1205, «What follows is Keycloak's login page, which only MCP clients meet until own authentication phase
   3.» pasa a «What follows is Keycloak's login page, which nobody meets since own authentication phase 3 (AI apps
   approve on the console's own consent page); it goes in phase 4.»
3. Líneas 1438–1440, «so the claim is now only in the access tokens MCP clients obtain, and the devtools one-liner…»
   pasa a «and since own authentication phase 3 no token Keycloak issues is accepted anywhere, so no token carries the
   claim to check; the devtools one-liner…» (el resto de la frase igual).
4. En la tabla de mitigaciones del cliente de aprovisionamiento (línea 1974), «remove one AI app's consent for a user
   (revoking an app under Settings → AI connections; needs nothing beyond `manage-users`)» pasa a «remove one AI app's
   consent for a user (no caller since own authentication phase 3, when revoking an app stopped involving Keycloak; it
   goes with Keycloak in phase 4)». «The code calls exactly six operations» no cambia: lo fija
   `tests/test_console_auth_realm.py`, y `kc_client.py` sigue definiendo las seis.
5. La sección `## AI connections (MCP)`, desde su encabezado hasta el final de `### What Traefik routes` y desde
   `### Revoking` hasta el final de `### Residual risks, stated plainly`, pasa a lo que sigue. `### What is never
   reachable over MCP` no cambia y queda en su sitio.

````markdown
## AI connections (MCP)

An AI app — Claude, or any client that speaks the Model Context Protocol —
can use ITMS **as the person who connected it**: it sees what that person
sees and can do what their role allows, every call is in the audit log, and
the app's own settings decide which tools need the person's approval. The
server is `/mcp` on the `api` container, and the platform is its own OAuth
2.1 authorization server; there is no new service. Design:
`docs/superpowers/specs/2026-10-09-mcp-server-design.md`, and for the OAuth
server `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §5.

**It is off by default, four times over.** No AI app connects until all
four hold:

1. the deployment has a public https address with no path
   (`ITS_PUBLIC_CONSOLE_URL`) — without it `/mcp` answers 503 and the
   platform has no OAuth issuer;
2. the operator has opted in to AI apps registering themselves
   (`ITS_MCP_REGISTRATION=open` in `.env`) — without it the platform refuses
   every registration and reads no app's client metadata document, so no app
   can get as far as the consent page, whatever else is set;
3. an org-admin has turned on **Settings → AI connections → Allow AI apps to
   connect over MCP** for the organization;
4. the person holds the `mcp.use` permission.

The third and fourth are checked on every call, not only at connection time,
and the consent page checks them too before it asks anything. The first two
are deployment settings the `api` container reads when it starts. Closing
registration again stops NEW apps registering; an app that registered while
it was open keeps its registration until it goes unused for 30 days, and is
stopped by the third and fourth or by revoking it (below).

### Turning it on

1. **`ITS_PUBLIC_CONSOLE_URL` must be set in `.env`** to the https address
   people open the console at, with no path (for this deployment,
   `https://itms.toeshee.com`). It may already be: the console's sign-in,
   Email intake and the install link use the same setting. Check with:

   ```sh
   grep '^ITS_PUBLIC_CONSOLE_URL=' .env
   ```

   If that prints nothing or an empty value, edit the line in `.env` by hand
   so it reads `ITS_PUBLIC_CONSOLE_URL=` followed by your address. Keep a
   single line: a second `ITS_PUBLIC_CONSOLE_URL=` further down silently wins.
   The MCP address is this value plus `/mcp`, and the platform's tokens are
   issued for exactly that address, so do not add a trailing path.
2. **Opt in to AI-app registration.** Read "Residual risks, stated plainly"
   below first: they are the price of this line, and most of them exist only
   while it is set. From the directory holding `docker-compose.yml` and
   `.env`, this removes any earlier `ITS_MCP_REGISTRATION` line and adds the
   one that opens registration:

   ```sh
   sed -i '/^ITS_MCP_REGISTRATION=/d' .env && echo 'ITS_MCP_REGISTRATION=open' >> .env
   ```

   Only the exact word `open` opens it; any other value, or no line at all,
   keeps it closed.
3. **Deploy** as in "Upgrading an existing deployment". The `api` container
   reads the setting when it starts.
4. **Check it.** The platform's OAuth metadata names the console as its
   issuer, and offers registration only when you opted in:

   ```sh
   curl -s "$(sed -n 's/^ITS_PUBLIC_CONSOLE_URL=//p' .env)/.well-known/oauth-authorization-server"
   ```

   The answer is JSON whose `issuer` is your console address. With the
   opt-in it also carries `registration_endpoint` and
   `"client_id_metadata_document_supported": true`; without it, neither. A
   503 with `temporarily_unavailable` means `ITS_PUBLIC_CONSOLE_URL` is not an
   https address without a path (step 1).
5. **Settings → AI connections:** an org-admin turns on **Allow AI apps to
   connect over MCP**. The tab shows the connection address.
6. **Settings → Roles:** grant `mcp.use` to the roles that may connect.
   Org-admins hold `mcp.use` by default; nobody else does until you grant it.
7. **Connect an AI app.** In the app (in Claude: add a custom connector),
   give it the connection address. The app registers itself and opens the
   console's consent page; the person signs in to the console first if they
   are not (password and second factor, as always). The page names the app,
   the address its answer goes back to and, for an app that only answers on
   the person's own computer, says so. Approving lets the app act as the
   person. Then, in the app's settings for that connector, set each tool to
   *Always allow* or *Needs approval*; tools that change something are safest
   on *Needs approval*.

### What keycloak-init still converges (until phase 4)

Since own authentication phase 3 none of this is in MCP's path: `/mcp`
accepts only the platform's own access tokens, and refuses every token
Keycloak issues, whatever its audience. keycloak-init keeps converging it on
every run until phase 4 removes Keycloak, **and while
`ITS_MCP_REGISTRATION=open` it still opens Keycloak's own anonymous
registration** (residual risk 7 below). In this order, so that a run that
dies part-way leaves registration closed:

1. reserved client ids, disabled and unusable: `it-suite` (the console's own
   token audience) and the MCP address itself. If `ITS_OIDC_AUDIENCE` names a
   client the platform or Keycloak needs (`it-suite-web`, the two service
   clients, `account`, `admin-cli`, `realm-management` and the other
   built-ins), the job refuses before it changes anything, says so, and exits
   5;
2. the `mcp` client scope with its consent-screen line, then its mappers — the
   MCP audience (`<ITS_PUBLIC_CONSOLE_URL>/mcp`), the `org` claim and `sub` —
   then the scope offered to apps as an optional scope;
3. the client profiles and policies for every app that registers itself in
   Keycloak, or later updates itself: PKCE S256 required, no implicit flow,
   no password grant, redirect URIs without wildcards;
4. the anonymous registration policies: consent required, scopes limited to
   `mcp`, `openid` and `profile`, no protocol mappers of the app's own, full
   scope off, at most 200 clients in the realm;
5. **last**, Trusted Hosts is removed, which is what opens Keycloak's
   anonymous registration — and only when `ITS_PUBLIC_CONSOLE_URL` is set
   **and** `ITS_MCP_REGISTRATION=open`. Otherwise Trusted Hosts stays (and is
   put back first if missing), and the job's log names the missing setting.

`docker compose logs --tail=20 keycloak-init | grep 'mcp converged'` still
prints `identity init: mcp converged=nothing` on a converged realm.

### What Traefik routes

| Path | Goes to |
|---|---|
| `/mcp` | `api` — the MCP endpoint |
| `/.well-known/oauth-protected-resource`, `…/mcp` | `api` — tells an app that the platform is its authorization server |
| `/.well-known/oauth-authorization-server` | `api` — the platform's OAuth metadata (RFC 8414) |
| `/oauth/token`, `/oauth/register`, `/oauth/revoke`, each by exact path | `api` — the platform's OAuth server |
| `/oauth/authorize` | the console — the consent page, behind the console's own sign-in |
| `/.well-known/openid-configuration`, and either document with a path after it | `api`, which answers 404 on purpose: apps look there first and move on after a 404, but give up on the console's 200 page |
| `/realms/<realm>/clients-registrations/openid-connect` and `…/openid-connect/<client-id>` | Keycloak, until phase 4 — its registration path, which no longer leads to MCP |
| any other `clients-registrations` path, in any letter case, and any identity-provider path with `;`, `\`, `..` or an encoded `;` `\` `.` `/` NUL | refused with 403 at Traefik, before Keycloak sees it (until phase 4) |
| `/realms/<realm>/.well-known/…` and Keycloak's pages | Keycloak, unchanged until phase 4 |

The `deploy` CI job still proves Keycloak's half of this live
(`deploy/keycloak/ci_mcp_flow.py`, through the real Traefik and Keycloak
containers): the 403s above, Keycloak's registration closed until both
settings are set, its PKCE sign-in and its audience. It does not yet run the
platform's own OAuth server against the deployed containers: phase 4 rewrites
it for that, when Keycloak goes (design §8). The platform's server is proved
by the backend suite (`backend/tests/test_oauth_*.py`, `test_mcp_own_tokens.py`).

### Revoking

- **One app:** Settings → AI connections → **Revoke for me**, or, for an
  org-admin, **Revoke for everyone**, which needs both `mcp.use` and
  `orgs.manage` (an org-admin holds both). The app's grants end — yours, or
  everyone's in this organization — in the same transaction as the record of
  the revocation: every token the app holds stops at its next call, and its
  refresh tokens mint nothing. No identity provider is involved, so there is
  no second half that can fail. It never affects another organization. An app
  listed without a **Revoke** button last acted here before it registered
  with the platform, or its registration expired: it holds no grant here.
- **Revoking is not a block-list.** It ends the grants the app holds now; it
  does not stop the app coming back. A person can approve the same app again,
  and any app can register again under a new client id with any name it
  likes. To keep AI apps out, use one of the stops below.
- **One person:** take `mcp.use` away from their role. Their next call is
  refused. Disabling or deleting the person, resetting their factors, a
  password change and "Sign out everywhere" end all their AI grants too.
- **The whole organization — the emergency stop:** Settings → AI
  connections → turn **Allow AI apps to connect over MCP** off. Every app is
  refused from its next call, whatever it registered as.
- **New apps, deployment-wide:** delete the `ITS_MCP_REGISTRATION=` line
  from `.env` and redeploy. The platform refuses every new registration and
  reads no client metadata document; apps already registered are
  unaffected, so pair it with one of the stops above or below.
- **The whole deployment — the deployment-wide stop:** empty
  `ITS_PUBLIC_CONSOLE_URL` and redeploy. `/mcp` answers 503 and the platform
  has no OAuth issuer. This also turns off everything else that uses the
  address — the console's own sign-in, Email intake's Microsoft sign-in, the
  install link — so it is a last resort, not a switch.
````

Y la sección de riesgos residuales:

````markdown
### Residual risks, stated plainly

**Risks 1, 2, 3 and 7 come from self-registration, and exist only while it is
open** (`ITS_MCP_REGISTRATION=open`). Closing it — delete that line from `.env`
and redeploy — ends them for new apps; an app that registered while it was
open keeps its registration until it goes unused for 30 days.

1. **Consent phishing.** Anyone can register an app with any display name and
   send a person a link to the consent page; that page is the only check
   between the link and the person's full authority. It says what approving
   hands over — "If you approve, it can act as you in ITMS: everything your
   role allows, including running actions on managed computers" — and the
   address the answer goes back to, which the app cannot disguise; but it
   names the app by whatever name the app registered. Org-admins should
   approve only apps they connected themselves.
2. **An app known by its client metadata document makes the platform fetch
   that address.** The fetch goes through the platform's outbound guard (no
   private, loopback or link-local addresses, no redirects), is cut at 5 KiB
   and 5 seconds, and its answer is cached as its own cache headers say,
   bounded to between 5 minutes and 24 hours (and not at all when they forbid
   caching, or when the document is refused). A stranger can still make the `api`
   container send one bounded request to a public address of their choosing
   each time it needs the document.
3. **Registrations can be made by strangers.** Registering needs no account.
   One network address may register 20 apps an hour; a registration nobody
   uses for 30 days disappears at the next registration. Nothing is exposed by
   it — registering grants nothing without a person's consent — but the table
   grows with whatever the limit lets through.
4. **http redirects are allowed only to the person's own computer**
   (`127.0.0.1`, `[::1]`, `localhost`, any port), which desktop AI apps use.
   Whatever answers on that port of the person's computer receives the
   authorization code; it is bound to the app's PKCE verifier and cannot be
   redeemed without it. The consent page says when an app only answers
   there.
5. **Free text can hold a secret under an innocent name.** The scan blocks
   results by field *name*; a password someone typed into a documentation
   record's notes field is text like any other and reaches the app as the
   person could read it.
6. **A stolen access token works for at most ten minutes, or until its grant
   ends.** It names its grant, which the API reads on every call: revoking the
   app, the person's account acts above, or the app's refresh token being used
   twice (a sign it was copied) end it at once.
7. **Until phase 4, Keycloak's own anonymous registration opens with the same
   setting.** keycloak-init still converges it (above). Its tokens are refused
   at `/mcp` and everywhere else, so it leads nowhere; but its own exposure
   stays until Keycloak goes: a registering app can make Keycloak fetch URLs it
   names (`sector_identifier_uri`, `jwks_uri`, `request_uris`) from
   Keycloak's network, and can fill its 200-client cap.
8. **Realm names must be lowercase** for Keycloak's registration to be routed
   (see the table above), until phase 4.

Separately from those, and by design: the platform cannot tell whether the
person or the AI app approved a call — approval happens in the app. A
compromised app holds the person's full authority, bounded by their role, their
per-tool settings, the audit trail and revocation.

The rules this all answers to are **CLAUDE.md invariant 1c** (an AI connected
over MCP acts as the person who connected it), added with this feature, and
the design's §9.
````

- [ ] **Step 3: `.env.example`**

En `deploy/.env.example`, el punto de `KC_HOSTNAME` pasa a:

```text
#   * Keycloak, as KC_HOSTNAME: the address of its own pages, which no
#     sign-in uses since own authentication phase 3 (Keycloak goes in phase
#     4). It no longer emails anyone: since own authentication phase 1 the
#     platform sends invitations and password resets itself, through
#     Settings -> Email.
```

y el bloque de `ITS_MCP_REGISTRATION`:

```text
# --- AI apps over MCP: self-registration (off unless you opt in) ---
# Uncommented, this opens AI-app self-registration: any MCP client may then
# register itself with the platform, or be known by its client metadata
# document (it still needs a person to sign in to the console and approve
# it). Read deploy/README.md "AI connections (MCP)" first. The api container
# reads it when it starts, and it needs ITS_PUBLIC_CONSOLE_URL too. Until
# phase 4 removes Keycloak, keycloak-init also reads it and opens Keycloak's
# own registration, whose tokens nothing accepts any more.
# ITS_MCP_REGISTRATION=open
```

(`tests/test_keycloak_init_job.py::test_the_env_example_documents_the_opt_in_and_leaves_it_off` exige la línea
`# ITS_MCP_REGISTRATION=open` y el texto «AI connections (MCP)»: los dos siguen.)

- [ ] **Step 4: Los documentos de diseño y los comentarios**

`docs/superpowers/specs/2026-10-09-mcp-server-design.md`, justo después de `## 3. OAuth, and only OAuth`:

```markdown
> **Amended 2026-11-06 (autenticación propia, phase 3; plan
> `docs/superpowers/plans/2026-11-06-autenticacion-propia-fase3.md`).** The
> authorization server is the platform itself, not the Keycloak realm
> (`docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §5).
> The protected-resource metadata names the console's address in
> `authorization_servers`; an app registers at the platform's
> `POST /oauth/register` or is known by its client metadata document; the
> person signs in to the console and approves on its consent page
> (`/oauth/authorize`); the access token is the platform's ES256 `at+jwt`, for
> exactly the MCP resource, ten minutes, naming its grant, which `/mcp` reads
> on every call. §3.1 steps 2-4, §3.2 and §3.3 below describe the Keycloak
> arrangement this replaced, kept as the record until phase 4 removes
> Keycloak. In §3.4, revoking now ends the app's grants in the platform's own
> tables, in one transaction with the `mcp_revocation` row and its audit
> event; nothing is removed in Keycloak.
```

`docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md`, justo después de `### 7.2 The staleness bug this
uncovered, which is not cosmetic`:

```markdown
> **Amended 2026-11-06 (autenticación propia, phase 3).** There is no claim
> to refresh from any more: the console signs in by session since phase 1,
> and since phase 3 the MCP door admits only the platform's own token, which
> carries no address. The `users` row is the one source of the address, for
> the domain rule and for every screen alike -- what this section asked for.
> Falsifiers 13 and 14 hold it over the MCP channel
> (`backend/tests/test_me_profile.py`).
```

`docs/03-security-model.md:161`, «MCP clients still authenticate through Keycloak until phase 3.» pasa a «MCP clients
authenticate on the platform's own OAuth 2.1 server since phase 3: PKCE S256, consent in the console, a ten-minute
ES256 access token for exactly the MCP resource, and a refresh token that rotates and whose reuse ends the grant.»

`docs/modules/console-auth/spec.md:98-99`, «MCP keeps signing in at Keycloak until own authentication phase 3, and the
realm is removed in phase 4.» pasa a «Since own authentication phase 3 MCP signs in on the platform's own OAuth server
too; the realm is removed in phase 4.»

Comentarios del código que dicen «hasta la fase 3»:

- `backend/app/modules/user_admin/service.py:28-33`: «so until phase 3 moves the MCP server onto the platform's own
  sign-in they cannot connect an AI app over MCP (plan decision D5, accepted: nothing is deployed before the four
  projects end).» pasa a «and since phase 3, which moved the MCP server onto the platform's own OAuth server, that is
  no obstacle: they connect an AI app like anybody else (`tests/test_oauth_falsifiers.py`).»
- `backend/app/modules/user_admin/__init__.py:9-10`: «`kc_client.py` is used by `app/mcp/api.py` (AI clients still
  sign in through Keycloak until phase 3), and `realm_config.py` has no caller.» pasa a «neither has a caller since
  phase 3 (revoking an AI app no longer asks Keycloak).» Comprobación: `grep -rn "kc_client" backend/app --include=*.py
  | grep -v "modules/user_admin/kc_client.py"` no lista ningún import.
- `backend/app/api/v1/me.py:166-167`: «(On the MCP channel `deps.admit` still refreshes it from the Keycloak token's
  claim until phase 3.)» pasa a «(Since phase 3 the MCP door reads it from the row too: the platform's token carries
  no address.)»
- `backend/app/core/auth/__init__.py:4`: «Replaces Keycloak on the console's path; MCP stays on Keycloak until phase
  3.» pasa a «Replaces Keycloak on the console's path; MCP's OAuth server is `app.core.auth.oauth` (phase 3).»
- `backend/tests/test_user_admin_api.py:628-630`: «-- which is also why this person cannot reach MCP until phase 3.»
  pasa a «(since phase 3 that is no obstacle to MCP: `tests/test_oauth_falsifiers.py`).»
- `backend/tests/conftest.py:329-330`: «`idp` and `jwks_client(idp)` stay: MCP still signs in at Keycloak until own
  authentication phase 3 (coordinator D12).» pasa a «`idp` and `jwks_client(idp)` stay until phase 4: the tests of
  `security.validate_token`, and those that prove a Keycloak token is refused everywhere, still mint one.»

Run: `cd "$(git rev-parse --show-toplevel)" && grep -rn "until phase 3\|until own authentication phase 3" deploy backend/app backend/tests docs/03-security-model.md docs/modules/console-auth`
Expected: sin salida (las otras «phase 3» del repositorio son del proyecto de clientes MSP, no de este).

- [ ] **Step 5: Los recuentos y la suite, en el árbol fusionado**

Run: `cd backend && .venv/bin/pytest -q tests/test_logging_discipline.py tests/test_mcp_channel_api.py tests/test_route_gating.py tests/test_client_catalog.py tests/test_route_session_discipline.py tests/test_i18n_catalog.py tests/test_keycloak_init_job.py tests/test_console_auth_realm.py tests/test_deploy_env_documentation.py`
Expected: PASS, con el recuento de logs en **106 + el delta de la fase 2**, `NEVER_EXPOSED` en **37 + lo de la fase 2** y
`UNGATED_BY_DESIGN` con las dos rutas del consentimiento. Si un número no cuadra, se corrige el número del árbol fusionado
y su párrafo «NOW», no el código.

Run: `cd backend && .venv/bin/pytest -q`
Expected: PASS, la suite entera.

Run: `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `Success: no issues found` y `All checks passed!`.

Run: `cd frontend && npx vitest run && npx tsc -b && npm run build`
Expected: PASS, sin errores de tipos, y el build termina (`✓ built in …`).

Run: `cd backend && .venv/bin/python -m tests.export_console_global_routes && git diff --exit-code ../frontend/src/lib/globalRoutes.json`
Expected: sin diferencias (esta fase no cambia `GLOBAL_WRITES` ni `GLOBAL_READS`; si la fase 2 los cambió, el
coordinador ya lo regeneró al fusionar).

Run: `cd "$(git rev-parse --show-toplevel)" && git log --format=%s claude/compassionate-bardeen-vgtwus..HEAD -- .github/workflows/ci.yml deploy/keycloak/`
Expected: ningún commit de las Tasks 1–12 (si aparece alguno, es de la fase 2; el paso MCP de la CI y `ci_mcp_flow.py` son de la fase 4).

- [ ] **Step 6: Commit**

```bash
git add backend/tests/test_oauth_falsifiers.py deploy/README.md deploy/.env.example \
  docs/superpowers/specs/2026-10-09-mcp-server-design.md \
  docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md docs/03-security-model.md \
  docs/modules/console-auth/spec.md backend/app/modules/user_admin/service.py \
  backend/app/modules/user_admin/__init__.py backend/app/api/v1/me.py backend/app/core/auth/__init__.py \
  backend/tests/test_user_admin_api.py backend/tests/conftest.py
git commit -m "test(oauth): falsifier 6 end to end; the runbook and the docs say the platform is MCP's OAuth server"
```

---

## Autorrevisión

Hecha sobre el plan entero, contra el diseño, el 2026-11-06.

**Cobertura del diseño.**

| Diseño | Dónde |
|---|---|
| §5.1 los cuatro endpoints; metadatos RFC 8414 con `S256`, `registration_endpoint`, `client_id_metadata_document_supported` y `none`; RFC 9728 apuntando a la plataforma | Tasks 6 (metadatos, registro, RFC 9728, Traefik), 7 (token, revocación), 4 y 8 (autorizar) |
| §5.2 sesión de la consola; nombre del cliente y dominio del redirect; aviso de solo loopback; código de un uso y 60 s; redirects exactos salvo el puerto de loopback; `iss` | Tasks 4 y 8 (pantalla y rutas), 1 (`redirects`, `policy.CODE_TTL_S`), 7 (código de un uso) |
| §5.3 ES256, 10 min, audiencia el recurso MCP, otro token rechazado; `iss`/`aud`/`kid`/alg fijados; refresh opaco con hash que rota; reutilizar revoca la familia; 30 días sin uso; `invalid_grant`; revocar en Ajustes corta en la siguiente llamada | Tasks 2 (token), 7 (refresh y familias), 9 (puerta: otro token, familia en cada llamada), 10 y 11 (Ajustes) |
| §5.4 registrar no da acceso; límite por IP; 30 días sin uso; MCP apagado hasta el org-admin | Task 6 (límite, caducidad perezosa D10), Task 8 (D11: interruptor y `mcp.use` en el consentimiento), la puerta de siempre |
| §5.5 CIMD por `egress.py`, `client_id` igual a la URL, caché por cabeceras | Tasks 3 y 6 |
| §5.6 el token interno | fase 1; Task 12 comprueba que no vale en las rutas públicas |
| §6.1 clientes OAuth, códigos, familias, con `org_id` y RLS y en el catálogo | Task 5 |
| §6.3 ES256 con la clave de OpenBao y JWKS con `kid` | fase 1 (`keys.py`, `/.well-known/jwks.json`); Task 2 lo usa, Task 6 lo publica en `jwks_uri` |
| §6.5 «MCP valida su JWT (`iss`, `aud`, `kid`, revocación)» | Task 9 |
| §7 consentimientos y revocaciones de clientes de IA auditados | Tasks 8 (`mcp.app_authorized`), 7 (`mcp.code_reused`, `mcp.refresh_reused`, `mcp.grant_revoked`), 10 (`mcp.app_revoked`) |
| §8 fase 3 | todo el plan; lo que queda para la fase 4, en «Lo que hace esta fase, y lo que deja a la fase 4» |
| §9 documentos | Task 12 (`docs/03-security-model.md`, el diseño de MCP §3, el de RBAC §7.2, `docs/modules/console-auth/spec.md`, el runbook) |
| §10 falsificador 6 | Task 7 (unidad) y Task 12 (de punta a punta); el 1 para estas tablas, Task 5; el 7 para la decisión, Task 8; el 10 (ninguna pantalla dice «Keycloak»), la guarda existente sobre los textos de la Task 4 |

**Marcadores.** Buscado en el plan: `TBD`, `TODO`, «rellenar», «similar a la Task», «añadir manejo de errores»; ninguno.
Cada paso que cambia código trae el código; los cambios mecánicos (`await mcp_token(`) dicen cada archivo y línea y traen
la orden que comprueba que no queda ninguno. Ningún comando lleva un valor que haya que sustituir.

**Coherencia de nombres y tipos**, comprobada entre tareas:

- `McpGrant(user_id, org_id, client_id, family_id, iat, exp, email="")` con `azp` (Task 2) es lo que la Task 9 pone en
  `AuthContext.claims`; `_gate` lee `claims.iat` y `claims.azp`, `invoke.py` y el audit leen `claims.azp`: los tres
  existen.
- `grants.family_is_live(session, *, family_id, user_id, client_id, now)`, `grants.revoke_app(session, *,
  oauth_client_id, user_id, reason, now)` y `grants.revoke_all_for_user(session, user_id, reason)` (Task 7) son las
  firmas que llaman las Tasks 9 y 10; los motivos que usa la 10 están en `REVOKE_REASONS` (Task 5). Las llamadas de la
  Task 10 van con el módulo delante (`grants.revoke_all_for_user`) porque `devices.revoke_all_for_user` ya ocupa el
  nombre en esos archivos.
- `clients.resolve_client(..., fetch: Fetch | None = None)` (Task 6) resuelve `cimd.fetch_document` al llamar; el
  Apéndice A.1 lo dice igual.
- `tests/mcp_support.mcp_token` es asíncrono desde la Task 9; la Task 10 usa `mcp_app` y la 12 `mcp_claims` y
  `signed_mcp_token`, los tres de la 9. `tests/oauth_support` crece al final en las Tasks 5, 6, 7 y 8 y la 12 solo lee.
- La respuesta de revocar es `{"revoked", "grants_revoked"}` en la Task 10, el Apéndice A.2 y la Task 11; la lista gana
  `id` y `name` en las tres.
- Recuentos: logs 108 → 109 (Task 3) → 106 (Task 10); `NEVER_EXPOSED` 36 → 37 (Task 8); `UNGATED_BY_DESIGN` +2 (Task
  8); la Task 12 los comprueba en el árbol fusionado.

**Lo que se corrigió en esta revisión:** la firma de `resolve_client` en el Apéndice A.1 (decía `fetch=cimd.fetch_document`,
que fija la función al definirla y no deja sustituirla en una prueba); la pregunta P12 y la nota de «Lo que deja»
sobre la sonda `jwks`, que nadie había visto; la colocación de las aserciones de `_live` en la prueba de «revoke for
everyone» de la Task 10 (cada `_old_token` abre una concesión nueva, así que van antes); y dos órdenes de la Task 12
que daban una ruta absoluta del árbol principal.

---

## Preguntas para el operador

Decisiones que el diseño deja abiertas, o en las que una fuente dice otra cosa que el diseño. Para cada una: las
opciones, la que el plan implementa, la recomendación y el texto del diseño del que sale. Cambiar la implementada es,
en todos los casos, cambiar la tarea que se nombra y nada más.

**P1. ¿`/mcp` deja de aceptar el token de Keycloak en esta fase, o acepta los dos hasta la fase 4?**
- (a) Corte en la fase 3: la puerta solo admite el token propio (Task 9). **Implementada.**
- (b) Los dos hasta la fase 4: dos validadores en la puerta, dos emisores para 1c(a).
- Recomendación: (a). §5.3: «otro token se rechaza [S27, S30]»; §8: la fase 3 es «Servidor OAuth para MCP». Con (b)
  la puerta seguiría aprovisionando personas desde un claim, que §6.1 retira («`users.external_subject` deja de
  identificar a nadie»). Nada se despliega antes de la fase 4 (decisión del 2026-10-31), así que el corte no rompe
  ninguna conexión real; una conexión existente recibiría un 401 `invalid_token` y su app volvería a conectar sola.

**P2. ¿`ITS_MCP_REGISTRATION=open` abre también el registro y el CIMD del servidor propio?**
- (a) La misma clave abre los dos; sin ella, los metadatos no anuncian registro ni CIMD, `POST /oauth/register` es 403 y
  un `client_id` URL no se descarga (D15). **Implementada.**
- (b) El servidor propio siempre abierto, como dice §5.4 a secas («Registro abierto, con límites»).
- (c) Una clave nueva, solo para el servidor propio.
- Recomendación: (a). §5.4 no menciona el opt-in, pero el operador lo decidió el 2026-10-15 para el registro anónimo
  (diseño de MCP §3.3: «Only on an explicit opt-in»), y (a) lo conserva sin clave `.env` nueva. Fuente que matiza: MCP
  2026-07-28 declara DCR «deprecated» y Claude solo usa CIMD si el servidor anuncia los dos valores; si no, cae a DCR
  [S27, S29, S33]: por eso (a) abre o cierra los dos juntos.

**P3. El límite de registros por dirección.**
- (a) 20 por hora y dirección (`policy.REGISTRATIONS_PER_HOUR`, Task 6). **Implementada.**
- (b) Más estricto (5 por hora).
- (c) Por día (100 por día).
- Recomendación: (a). §5.4 dice «Límite de registros por IP» sin número. Anthropic avisa de que con registro abierto
  Claude crea un cliente en cada conexión nueva [S33]: una oficina detrás de una sola dirección en la que varias personas
  conectan Claude la misma mañana no debe toparse con el límite, y los registros sin uso caducan a los 30 días.

**P4. Las cotas de la caché de un documento CIMD.**
- (a) Lo que digan sus cabeceras, acotado a [5 min, 24 h]; nada si prohíben la caché o el documento se rechaza (Tasks 3
  y 6). **Implementada.**
- (b) Solo las cabeceras, sin cotas.
- (c) Otro intervalo.
- Recomendación: (a). §5.5: «se guarda en caché según sus cabeceras». El borrador CIMD §4.4 lo permite: «SHOULD respect
  HTTP cache headers [RFC9111] when caching client metadata, but MAY define its own upper and/or lower bounds» [P-CIMD].
  Sin cota inferior, un documento con `max-age=1` haría a la API descargarlo en cada petición; sin superior, un cambio
  de redirects del cliente tardaría lo que el cliente quisiera.

**P5. ¿Una concesión tiene vida máxima, además de los 30 días sin uso?**
- (a) Solo la inactividad: cada refresh mueve el límite 30 días (Task 7). **Implementada.**
- (b) Además un máximo absoluto (por ejemplo 90 días), tras el que la persona vuelve a aprobar la app.
- Recomendación: (a), que es el texto del diseño (§5.3: «caduca a los 30 días sin uso»). (b) obligaría a reaprobar cada
  cierto tiempo una app usada a diario; si el operador lo quiere, es una columna más en la familia (su fin absoluto,
  fijado al abrirla) y una comparación más en `grants._refresh` y en `grants.family_is_live`.

**P6. Una petición de autorización sin `resource`.**
- (a) Se toma por el recurso MCP, el único para el que este servidor emite tokens; un `resource` distinto es
  `invalid_target` (Task 8, decisión 1). **Implementada.**
- (b) Exigirlo: sin él, `invalid_target`.
- Recomendación: (a). **Contradicción a la vista:** MCP dice que el cliente «MUST» mandarlo [S27], pero que Claude lo
  mande está sin verificar (dossier, UNVERIFIED 3). RFC 8707 §2 deja al servidor elegir: «MAY require clients to specify
  the resource(s) … and MAY fail requests that omit the parameter» [P-8707]. (a) no ensancha nada, porque no hay otro
  recurso, y no deja a Claude fuera si no lo manda.

**P7. RFC 9068 pide RS256; el diseño fija ES256.**
- (a) Solo ES256, con el `typ` y los claims de RFC 9068 (D5). **Implementada.**
- (b) Publicar también una clave RS256 y firmar con ella.
- Recomendación: (a). **Contradicción:** RFC 9068 §2.1: «Authorization servers and resource servers conforming to this
  specification MUST include RS256 … among their supported signature algorithms» [P-9068]; el diseño: «JWT ES256»
  (§5.3) y «Tokens de MCP y del asistente firmados con ES256» (§6.3). El único servidor de recursos de estos tokens es
  la propia plataforma, que verifica ES256, así que nadie necesita RS256 para interoperar; el plan no afirma conformidad
  con RFC 9068, solo sigue su forma.

**P8. ¿Qué actos de la cuenta terminan las conexiones de IA de una persona?**
- (a) Los siete que ya olvidan sus equipos recordados: desactivar, borrar, restablecer los factores (el administrador o
  el enlace de recuperación), cambiar la contraseña (la persona o el enlace por correo) y «cerrar sesión en todas
  partes» (Task 10). **Implementada.**
- (b) Además, que un administrador cierre sus sesiones.
- (c) Solo desactivar y borrar.
- Recomendación: (a). §2.4 nombra un acto: «Desactivar a una persona corta sus sesiones, sus tokens de IA y sus equipos
  recordados al instante»; los otros seis son del plan, que los toma de los que ya olvidan los equipos recordados. El
  código ya trata «cerrar sesiones» por un administrador como algo que deja los equipos
  (`user_admin.service.end_sessions`). (a) hace que una conexión de IA valga lo mismo que un equipo recordado: la persona
  que sospecha de su cuenta y la cierra en todas partes corta también las apps.

**P9. Un registro que pide un método de autenticación de cliente confidencial.**
- (a) Se registra como cliente público (`none`) y la respuesta lo dice (D4). **Implementada.**
- (b) Rechazarlo con `invalid_client_metadata`.
- Recomendación: (a). §5.1 pone `none` en los metadatos y Claude se registra como público [S33]. RFC 7591 §3.2.1 lo
  permite: «MAY reject or replace any of the client's requested metadata values» [P-7591]. **Matiz de fuente:** RFC 7591
  §2 dice que sin `token_endpoint_auth_method` el valor por defecto es `client_secret_basic`; (b) rechazaría a todo
  cliente que omita el campo, y (a) lo registra como público, que es lo único que este servidor sabe ser.

**P10. Una instalación con varias organizaciones.**
- (a) El servidor OAuth sirve a la organización del despliegue (`ITS_DEFAULT_ORG_SLUG`), como el inicio de sesión de la
  fase 1 (D3): en modo multi-tenant, una persona de otra organización no puede conectar una app. **Implementada.**
- (b) Un emisor por organización (`<consola>/o/<slug>`), con los metadatos en la ruta insertada de RFC 8414 §3.
- Recomendación: (a). El diseño no habla de varias organizaciones en §5; la instalación es de una (Toeshee) y el modo
  multi-tenant es «cloud-ready later» (`CLAUDE.md`). (b) cambia D1 (el emisor ya no sería el origen sin ruta) y cada
  ruta de esta fase; conviene decidirlo con el proyecto que abra la nube.

**P11. El nombre de una app registrada por DCR lo pone la propia app.**
- (a) La pantalla muestra el nombre, el host al que vuelve la respuesta y, si solo vuelve a este equipo, el aviso; el
  host es lo que la app no puede disfrazar (Tasks 4 y 8). **Implementada.**
- (b) Además, una marca junto al nombre («nombre que la aplicación se dio»).
- Recomendación: (a), que es el texto del diseño (§5.2: «Pantalla de consentimiento con el nombre del cliente de IA y
  **el dominio de su redirect**; aviso extra si solo hay redirects locales»). El riesgo queda escrito en el runbook
  (riesgo residual 1, Task 12). (b) es una línea de texto en la Task 4 si el operador la quiere.

**P12. La sonda `jwks` («Sign-in key cache») dirá «degradado» desde esta fase hasta la fase 4.**
- (a) Dejarla: ninguna puerta llena ya la caché de Keycloak, y la fase 4 borra la sonda con el validador («Lo que deja»,
  punto 3). No entra en las muestras del worker, así que no avisa a nadie. **Implementada.**
- (b) Quitarla en esta fase (`app/api/probes.py`, `app/core/health/probes.py`, sus etiquetas y sus pruebas).
- Recomendación: (a). §8 pone el retiro de Keycloak entero en la fase 4, y nada se despliega antes (decisión del
  2026-10-31): el estado intermedio no lo ve ningún operador.

**P13. El registro anónimo de Keycloak sigue abriéndose con `ITS_MCP_REGISTRATION=open` hasta la fase 4.**
- (a) Dejarlo: `keycloak-init` sigue convergiendo el realm como hoy; sus tokens no valen en ninguna puerta desde la Task
  9. **Implementada.** El runbook lo dice como riesgo residual 7 (Task 12).
- (b) Que `keycloak-init` deje de abrirlo ya (toca `deploy/keycloak/init-clients.py`, sus pruebas y lo que
  `ci_mcp_flow.py` espera).
- Recomendación: (a). §8 pone `deploy/keycloak/`, `ci_mcp_flow.py` y su paso de la CI en la fase 4; (b) reescribiría ya
  media fase 4, y nada se despliega antes.

**P14. ¿Aprobar una app de IA pide la confirmación reciente del segundo factor?**
- (a) No: basta la sesión de la consola, completa (Task 8, decisión 2). **Implementada**, porque §2.5 da la lista
  cerrada de actos que la piden y el consentimiento no está.
- (b) Sí: `require_step_up(ctx)` en `consent.decide` antes de emitir el código (una línea, marcada en el código); la
  Task 4 ya sabe pedirla.
- Recomendación: **(b)**, y es la única en la que la recomendación no es lo implementado. Aprobar entrega a la app toda
  la autoridad de la persona durante 30 días renovables, más que una consulta de la bóveda, que sí la pide; y en un
  equipo recordado (§2.6, sin segundo factor durante 15 días, que no cumple AAL2) hoy basta con abrir el navegador.
  Como cambia la lista de §2.5, es decisión del operador y no del plan.
