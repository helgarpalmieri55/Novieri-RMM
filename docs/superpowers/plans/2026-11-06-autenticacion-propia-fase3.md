# Autenticación propia — Fase 3: el servidor OAuth para MCP. Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Un cliente de IA se conecta a `/mcp` con un token que emite la propia plataforma —conocido por su documento CIMD siempre que MCP esté encendido, o por registro dinámico solo con la opción del operador; PKCE; consentimiento de la persona en la consola con el segundo factor recién confirmado; token de acceso ES256 de 10 minutos para el recurso MCP; refresh que rota— y `/mcp` deja de aceptar tokens de Keycloak.

**Architecture:** Un subpaquete nuevo, `backend/app/core/auth/oauth/`, implementa el servidor de autorización sobre lo que ya construyó la fase 1: la clave ES256 de OpenBao (`keys.sign_jwt`), la sesión por cookie (`admit()`), la confirmación reciente del segundo factor (`require_step_up`), los avisos por correo (`notices.py`), el limitador Redis y la protección de salida (`egress.py`). Cuatro tablas nuevas con RLS (migración **0247**) guardan clientes, códigos y familias de refresh; la retención diaria de la fase 2 las barre. Las rutas de máquina (`/oauth/token`, `/oauth/register`, `/oauth/revoke`, `/.well-known/oauth-authorization-server`) van fuera de `/api/v1`; la pantalla de consentimiento es una página de la consola en `/oauth/authorize`, que no se deja enmarcar y llama a dos rutas propias bajo `/api/v1/oauth/`. La puerta de MCP (`deps.admit`, canal MCP) verifica el token propio y la familia en cada llamada.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, PostgreSQL 16 (RLS), OpenBao (transit `auth-signing`, de la fase 1), Redis, PyJWT 2.15.1 (ES256), httpx, pytest. React + TypeScript estricto, Vite, TanStack Query, vitest. nginx (la consola). Ninguna dependencia nueva.

**Diseño:** `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` (aprobado el 2026-11-04), §5 y §8 fase 3; §5.6 (el token interno del asistente) ya lo construyó la fase 1 (su D1, `backend/app/core/auth/loopback.py`) y aquí solo se comprueba. Fuentes: `docs/superpowers/specs/2026-11-04-autenticacion-propia-fuentes.md` (citadas como [Sn]) y, para lo que el dossier no cita, las páginas que este plan y su revisión leyeron el 2026-11-06 y nombra con su URL en el Apéndice C. **Revisión del 2026-11-06:** tres revisiones previas al arranque y una consolidación (`decisions-dossier.md`) encontraron 73 defectos (A3-01…A3-73) y 33 elementos que una regla decide (B3-01…B3-33, Apéndice B); el operador decidió C1 y C2 (abajo). Esta versión los aplica todos.

## Global Constraints

- **Decisiones del operador del 2026-11-06 (vinculantes).**
  - **C1 — Aprobar una aplicación de IA pide el segundo factor confirmado en los últimos 10 minutos, también en un equipo recordado, y avisa por correo.** El servidor rechaza la aprobación sin él (`require_step_up`, 403 `step_up_required`; Task 8); la página de consentimiento lo pide con `useStepUp` y reintenta una vez (Task 4); rechazar no lo pide. Tras aprobar, la persona recibe «Has conectado <host de la app>» («You connected <host>»), con el host que encabeza la pantalla (Task 8, `notices.py`). El diseño §2.5 y §2.7 se enmiendan (Task 12).
  - **C2 — CIMD siempre que MCP esté encendido; registro dinámico solo con `ITS_MCP_REGISTRATION=open`.** Un `client_id` que es la URL de un documento CIMD se acepta sin la opción (lo lee el consentimiento de una persona con sesión, Task 8, y la protección de salida, Task 3); `POST /oauth/register` solo con la opción. Los metadatos RFC 8414 anuncian `client_id_metadata_document_supported: true` siempre que hay emisor, y `registration_endpoint` solo con el registro abierto y MCP encendido (Task 6). El diseño §5.4 se enmienda (Task 12); el runbook y `.env.example` lo dicen (Task 12).
- **Seguridad.** Invariantes 1, 1b, 1c, 2, 2a, 3, 4, 5, 5a y 6 de `CLAUDE.md` intactas, y 1c entera:
  - **1c(a):** en `/mcp` solo un token OAuth hace una sesión; ni la cookie de la consola ni el token interno del asistente valen en el canal (la fase 1 ya lo prueba; esta fase lo mantiene). El token lo emite la plataforma a la persona tras su propio inicio de sesión, con audiencia **exactamente** el recurso MCP (`<ITS_PUBLIC_CONSOLE_URL>/mcp`) y ninguna otra.
  - **1c(b):** cada llamada MCP se sigue auditando con el canal `mcp`, el cliente de IA (`app` = su `client_id`) y la herramienta.
  - **1c(c):** ningún secreto en claro llega a una respuesta MCP. La ruta bajo `/api/v1` que emite un código (`…/authorize/decision`) entra en `NEVER_EXPOSED` y en `NO_STORE_ROUTES`; las de máquina están fuera de `/api/v1`, donde ningún tool llega (`app/mcp/guard.py`). Una app nunca aprueba a otra.
  - **1c(d):** MCP sigue apagado hasta que un org-admin lo enciende (`mcp_enabled`); la persona necesita `mcp.use`; con MCP apagado no se registra nada ni se descarga ningún documento (B3-02, A3-31); revocar un cliente de IA corta su acceso en la siguiente llamada, también el de una app aprobada que aún no llamó nada (B3-28).
  - **1c(e):** los límites de la API no cambian (nombres, techos de lote, sin grupos para bloquear o borrar).
  - Ningún secreto (código de autorización, refresh token, `code_verifier`) aparece en logs, ni en la base de datos en claro (se guarda su SHA-256), ni en una respuesta salvo la que lo entrega una vez. Las respuestas del servidor OAuth llevan `Cache-Control: no-store` y `Pragma: no-cache` (RFC 6749 §5.1).
  - El navegador no guarda ningún token: la consola pasa la consulta de `/oauth/authorize` a la API y navega a la redirección que la API le devuelve, y **solo** a la de una aprobación; un error, un rechazo o un «no puedes» enseñan el host y esperan el clic (B3-15). La consola no se deja enmarcar (`frame-ancestors 'none'` y `X-Frame-Options: DENY` en `deploy/nginx.conf`; B3-16).
  - Hosts ASCII: un redirect o un `client_id` CIMD con un carácter por encima de 0x7E se rechaza, para que lo que la pantalla enseña sea lo que el navegador usará (B3-10, A3-01).
- **Migración.** Una sola, **0247** (`backend/migrations/versions/0247_oauth_server.py`, revision `"0247"`, down_revision **`"0246"`**, la de la fase 2), hacia adelante; `downgrade()` funcional (la CI corre `upgrade head → downgrade base → upgrade head`, y la Task 5 tiene su prueba de ida y vuelta en una base propia). 0245 y 0246 no se tocan. **La Task 5 no empieza hasta que 0246 esté en la rama** (Oleadas).
- **Alcance.** Las cuatro tablas nuevas tienen `org_id` + RLS forzada (`tenant_isolation`, patrón de 0245); `oauth_clients` es **G**; `oauth_codes`, `oauth_refresh_families` y `oauth_refresh_tokens` son **O(`users`, `user_id`)**, como las de 0245. Todas entran en `client_scope_catalog.CATALOG`. El único sitio que abre el alcance «todos» sobre ellas es `app/core/auth/oauth/store.py::oauth_session`, en `ALL_SCOPE_ALLOWLIST`, y solo lo llaman los archivos del núcleo que lista `tests/test_client_catalog.py::OAUTH_SESSION_USERS` (ninguna ruta; B3-31). Ningún motor de administración en `app/core/auth/oauth/`: la organización del despliegue se lee con `lookup.org_of_slug`, la función `SECURITY DEFINER` de la fase 2 (B3-13).
- **Parámetros (diseño §5):** token de acceso 10 min (`exp - iat ≤ 600`), código 60 s, refresh que rota en cada uso y cuya familia caduca a los 30 días sin uso, sin vida máxima absoluta (B3-04); una app sin uso en 30 días caduca; registro dinámico solo con la opción (C2), 20 por hora y red (IPv6 por /64), 200 registros vivos y cerrado con MCP apagado (B3-02); `/oauth/token` y `/oauth/revoke`, 600 peticiones por minuto y red (B3-24); PKCE solo `S256`; `iss` (RFC 9207) en toda respuesta de autorización; CIMD de 5 KiB como máximo, por `egress.get_bounded` (solo direcciones públicas, sin las de uso especial de RFC 6890, puerto 443), caché por cabeceras acotada a [5 min, 24 h], CIMD-03 donde es más estricto que la -00 (B3-14).
- **Errores del servidor OAuth:** cuerpo `{"error", "error_description"}` de RFC 6749 §5.2, en inglés y ASCII (§5.2 limita `error_description` a `%x20-21 / %x23-5B / %x5D-7E` y lo dirige al desarrollador del cliente): **nunca** pasan por `tr()`. Los textos para una **persona** (la pantalla de consentimiento, `/mcp`, Ajustes, el aviso por correo) sí van por `tr()` y `app/core/i18n/es.py`.
- **Textos de la consola:** `t()` con claves en `frontend/src/i18n/en/` y `es/`; el término es «aplicación de IA».
- **Auditoría.** Acciones nuevas, siempre como literal (`action="..."`, nunca una variable, para no mover `REVIEWED_NON_LITERAL_ACTIONS`): `mcp.app_authorized` (en inglés «AI app authorized», en `HAND_WORDED`), `mcp.code_reused`, `mcp.refresh_reused`, `mcp.grant_revoked`, `mcp.grant_ended`. Se añaden a `frontend/src/i18n/{en,es}/auditVocabulary.ts` (inglés = la derivación de `humaniseEnum`, salvo la primera). No se auditan el registro ni el refresh (B3-22); los siete actos de la cuenta que terminan concesiones lo dicen en sus metadatos (`ai_grants_revoked`, B3-30). Si una tarea toca `backend/app`, corre también `cd frontend && npx vitest run src/modules/audit src/i18n`.
- **Pruebas.** Ninguna se borra, se salta ni se debilita. Las que fijan Keycloak en la puerta de MCP se **re-apuntan** al servidor propio, y el informe de la tarea dice cuáles y por qué. Las que prueban funciones de Keycloak que siguen existiendo hasta la fase 4 (`security.validate_token`, `kc_client.revoke_consent`) no se tocan. Donde un ayudante pasa a ser asíncrono (Tasks 9, 10, 12), pytest corre con `-W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning`, que hacen fallo de un `await` olvidado.
- **Recuentos que mueven varias tareas.** Cada tarea dice su delta **respecto de la base que ve**; el coordinador suma al fusionar y corre la prueba en el árbol fusionado. Los conocidos: llamadas de log en `tests/test_logging_discipline.py` (108 en la rama hoy; **+1** Task 3, **+1** Task 7, **−3** Task 10 → **107**, más lo que añada la fase 2); `NEVER_EXPOSED` en `tests/test_mcp_channel_api.py` (36 hoy; **+1** Task 8 → **37**, más lo de la fase 2); `UNGATED_BY_DESIGN` en `tests/test_route_gating.py` (**+2** Task 8); `SECRET_ONCE`/`NO_STORE_ROUTES` (**+1** Task 8).
- **Archivos generados.** `frontend/src/lib/globalRoutes.json` no se edita a mano. Esta fase no cambia `GLOBAL_WRITES` ni `GLOBAL_READS`, así que no lo regenera; si al fusionar con la fase 2 esas listas cambian, el coordinador lo regenera en el árbol fusionado con `cd backend && PYTHONPATH=$PWD .venv/bin/python -m tests.export_console_global_routes`.
- **Verificación, desde `backend/`:** `.venv/bin/pytest -q` con los archivos que nombra el paso «Las pruebas pasan» de cada tarea; `.venv/bin/mypy app`; `.venv/bin/ruff check app tests`. La suite completa la corre el coordinador en el árbol fusionado. **Desde `frontend/`:** `npx vitest run` completo, `npx tsc -b`, `npm run build`.
- **Pistas en paralelo (worktrees)**, las de la fase 1 sin cambios:
  - La worktree nace de `origin/main`: lo primero, `git merge --ff-only claude/compassionate-bardeen-vgtwus` (o `git merge` si no avanza en rápido) y `git log --oneline -1`. **La Task 5 y todo lo que viene después**, además, fusionan antes de empezar la rama de la fase 2 que contiene 0246 y `lookup.org_of_slug` (el coordinador da su nombre; no es un valor que este plan pueda fijar) y comprueban que está: `ls backend/migrations/versions/0246_*.py` lista un archivo. **La Task 13**, también la de `backend/app/core/auth/retention.py` (la Task 3 de la fase 2): `ls backend/app/core/auth/retention.py` lo lista.
  - `ln -s /home/user/Novieri-RMM/backend/.venv backend/.venv` y `ln -s /home/user/Novieri-RMM/frontend/node_modules frontend/node_modules`.
  - `cd backend && export PYTHONPATH=$PWD`, y comprobar que `.venv/bin/python -c "import app; print(app.__file__)"` imprime una ruta de la worktree. `PYTHONPATH` va en cada orden que corre Python.
  - `ITS_TEST_REDIS_URL` exportada con la base de Redis que asigne el coordinador a la worktree, de 0 a 15 (por
    ejemplo, para la base 3, `export ITS_TEST_REDIS_URL=redis://localhost:6379/3`).
  - `pg_ctlcluster 16 main start 2>/dev/null; redis-cli ping >/dev/null 2>&1 || redis-server --daemonize yes`.
  - Se hace commit en la rama de la worktree; no se hace push ni se toca el árbol principal.

## Lo que hace esta fase, y lo que deja a la fase 4

**Hace (diseño §5):** §5.1 los cuatro endpoints, los metadatos RFC 8414 y el RFC 9728 apuntando a la plataforma; §5.2 la autorización con consentimiento (segundo factor reciente y aviso, C1), código de 60 s, redirects exactos salvo el puerto de loopback, `iss`; §5.3 el token ES256 de 10 min con audiencia MCP, el refresh que rota y la revocación desde Ajustes; §5.4 el registro dinámico con la opción del operador y sus límites, y la caducidad de las apps sin uso (C2, B3-02); §5.5 CIMD por la protección de salida, comprobando `client_id` y con caché; §5.6 se comprueba, no se rehace. `/mcp` deja de aceptar el token de Keycloak (B3-01). Las filas que ya no sirven se van con la retención diaria de la fase 2 (Task 13).

**Deja a la fase 4, explícitamente (diseño §8):**
1. Los servicios `keycloak` y `keycloak-init`, `deploy/keycloak/` entero —incluida la convergencia MCP de `init-clients.py` (ámbito `mcp`, mapeador de audiencia, políticas de registro, Trusted Hosts)— y las rutas de Traefik `kc`, `kc-registration-oidc` y `kc-registration-closed`. Mientras tanto Keycloak sigue abriendo su registro anónimo con `ITS_MCP_REGISTRATION=open`, aunque la API ya no acepta sus tokens. Ese estado no llega a ningún host: nada se despliega antes de que la fase 4 retire Keycloak (regla del operador del 2026-10-31; B3-12).
2. `deploy/keycloak/ci_mcp_flow.py` y el paso «MCP — live registration, PKCE and audience, through Traefik» del job `deploy` de `.github/workflows/ci.yml`, **reescritos contra el servidor propio**. En esta fase siguen verdes y siguen probando solo a Keycloak: no llaman a la API (`ci_mcp_flow.py` habla con Traefik y Keycloak), así que el cambio de la puerta no los rompe. `backend/tests/test_ci_mcp_flow.py` (sus funciones puras) igual.
3. El validador de Keycloak: `app/core/security.py` (`validate_token`, `JwksCache`, `TokenClaims`, `require_person_mcp_token`), `app.state.jwks`, el parámetro `jwks_http_client` de `create_app`, la sonda `jwks` de `app/api/probes.py`, los ajustes `oidc_*` y las claves `.env` `ITS_OIDC_*`, y `tests/fake_idp.py`; también `deps.jwks`, que ya no tiene llamadas. Desde esta fase ninguna puerta los llama; sus pruebas unitarias se quedan hasta que se borren con ellos. Consecuencia visible: la sonda `jwks` («Sign-in key cache») ya decía `degraded` (`empty_cache`) tras cada reinicio hasta la primera validación; desde esta fase lo dice siempre, hasta que la fase 4 la quite; no entra en las muestras del worker (`WORKER_SAMPLED_PROBES`), así que no avisa a nadie (B3-11).
4. `users.external_subject` (ninguna puerta lo lee desde esta fase) y su clave `users_org_id_external_subject_key` en `ORG_WIDE_KEYS`; `ApiEnv.create_user(..., sub=)` sigue identificando a las personas de prueba por él.
5. `kc_client.revoke_consent` y sus pruebas (sin llamadas desde esta fase), `keycloak_event_ingest` y `app/modules/keycloak_events/` (D4 de la fase 1).
6. En `deploy/README.md`, las filas y párrafos que describen a Keycloak («What keycloak-init converges», las filas `kc` de «What Traefik routes», los riesgos residuales propios de Keycloak): la Task 12 los marca «hasta la fase 4», no los borra.

## Oleadas y paralelismo

| Oleada | Tareas | En paralelo | Depende de |
|---|---|---|---|
| 1 | 1, 2, 3, 4, 11 | sí | — (**puede empezar antes de que exista 0246**: ninguna toca la base de datos) |
| 2 | 5 | — | **0246 y `lookup.org_of_slug` de la fase 2 en la rama**; 1 (`policy.CLIENT_IDLE`) |
| 3 | 6, 7 | sí | 1, 2, 3, 5 |
| 4 | 8, 9 | sí | 6, 7 (la 9 también 2) |
| 5 | 10, 13 | sí | la 10: 7, 8, 9 (sus pruebas llaman herramientas por `/mcp` con el token propio y aprueban con `connect`); la 13: 5, 7 y **`retention.py` de la fase 2 en la rama** |
| 6 | 12 | — | todas |

La Task 13 lleva el último número porque nació en la revisión del 2026-11-06 (las filas de OAuth en la retención de la fase 2); corre en la oleada 5, antes del cierre.

**La migración.** La Task 5 es la única que crea tablas y la única que no puede correr antes de 0246: con `down_revision = "0246"`, Alembic no encuentra la cadena hasta que 0246 está en la rama, y las pruebas (que migran a `head`) fallan. Si la fase 2 cambia el número o renuncia a su migración, el coordinador ajusta `down_revision` y nada más.

## Tareas

| Task | Qué | Diseño |
|---|---|---|
| 1 | Primitivas puras: `policy`, `errors`, `redirects`, `pkce`; manejador de `OAuthError` en `main.py` | §5.1–§5.3 |
| 2 | El token de acceso: `access.py` (`McpGrant`, acuñar, verificar) | §5.3, §6.3 |
| 3 | `egress.get_bounded` y el documento CIMD (`cimd.py`) | §5.5 |
| 4 | Consola: la pantalla de consentimiento `/oauth/authorize` (segundo factor reciente, sin enmarcar, solo la aprobación navega sola) | §5.2 |
| 5 | Migración 0247, modelos, catálogo, `store.py`, `clients.find_client` y la guarda `OAUTH_SESSION_USERS` | §6.1, §6.2 |
| 6 | Registro (DCR, con la opción) y clientes CIMD; metadatos RFC 8414; RFC 9728 al emisor propio; Traefik | §5.1, §5.4, §5.5 |
| 7 | Códigos y familias (`grants.py`); `/oauth/token` y `/oauth/revoke` | §5.2, §5.3 |
| 8 | El consentimiento: `authorize.py`, `consent.py`, `/api/v1/oauth/authorize/{details,decision}`, el aviso | §5.2, §2.5, §2.7 |
| 9 | La puerta de MCP con el token propio; pruebas MCP re-apuntadas | §5.3, §6.5 |
| 10 | Ajustes → Conexiones de IA sin Keycloak (`connections.py`); los actos de la cuenta revocan las concesiones | §5.3, §2.4 |
| 11 | Consola: Ajustes → Conexiones de IA (revocar por id, titulada por el host) | §5.3 |
| 12 | Cierre: falsificadores de §10.6 de punta a punta, documentos, recuentos, el informe | §9, §10 |
| 13 | Las filas de OAuth en la retención diaria de la fase 2 | §5.2–§5.4 |

## Notas de integración del coordinador

- **Oleada 1.** Las Tasks 1, 2 y 3 crean archivos distintos bajo `backend/app/core/auth/oauth/`; las tres crean también `backend/app/core/auth/oauth/__init__.py` **con el mismo contenido** (Task 1, Step 1): al fusionar se toma una copia. La Task 1 añade una línea en `backend/app/main.py` (el manejador); nadie más toca `main.py` en esta oleada. Las Tasks 4 y 11 tocan `frontend/src/lib/api.ts` en sitios distintos (la 4: dos métodos nuevos después de `acceptRecovery`, con sus tipos en `frontend/src/modules/auth/types.ts`; la 11:
el método `revokeMcpConnection` y sus tipos): unión. La Task 4 toca `deploy/nginx.conf`; nadie más.
- **Oleada 3, choques esperados** (orden de fusión: 6, 7):
  - `backend/app/main.py`: cada una incluye su router (`oauth_registration` la 6, `oauth_tokens` la 7): unión.
  - `backend/tests/oauth_support.py` (lo crea la Task 5): la 6 y la 7 añaden funciones al final: unión.
  - `backend/tests/test_client_catalog.py`: cada una añade su archivo a `OAUTH_SESSION_USERS`: unión.
  - `backend/tests/test_logging_discipline.py` y el §2.2 del diseño del kernel de logs: la 7 añade su párrafo «NOW 110» sobre el de la 3; la 6 no los toca.
  - `backend/app/core/i18n/es.py`: solo la 6 en esta oleada.
- **Oleada 4, choques esperados** (orden de fusión: 8, 9):
  - `backend/tests/test_mcp_channel_api.py`: la 8 añade una ruta a `OWN_SIGN_IN_NEVER_EXPOSED` y sube el recuento en 1 (el número que veas + 1: 55 → 56 con la fase 2 de `3a214fd`); la 9 cambia cada `mcp_token(` por `await mcp_token(` y re-apunta tres pruebas (y el docstring). Se conservan los dos cambios.
  - `backend/tests/test_client_catalog.py`: la 8 añade `consent.py` a `OAUTH_SESSION_USERS`; la 9 cambia el motivo de `users_org_id_external_subject_key`: unión.
  - `backend/app/mcp/exclusions.py`: solo la 8 (y la 10 en la oleada 5).
  - `backend/app/core/i18n/es.py`: unión (la 8 añade entradas, el aviso incluido; la 9 añade una y borra dos que dejan de usarse).
  - `backend/tests/oauth_support.py` solo lo toca la 8, y `backend/tests/mcp_support.py` solo la 9.
- **Oleada 5.** La 10 parte de `backend/tests/test_mcp_admin_api.py` con los dos ayudantes que la 9 ya re-apuntó (`_old_token`, `_call_a_tool`) y reescribe el resto de las pruebas de revocar; baja el recuento de logs de 110 a 107 (−3). La 13 solo toca `backend/app/core/auth/retention.py` y `backend/tests/test_auth_retention.py` (de la fase 2) y crea `backend/tests/test_oauth_retention.py`: no choca con la 10.
- **Firmas que una tarea fija y otras consumen:** el Apéndice A. Donde el bloque «Interfaces» de una tarea dice otra cosa, manda la tarea, que se escribió contra el código real.
- **Para el informe de despliegue** (cuando acaben los cuatro proyectos; el operador decidió el 2026-10-31 que nada se despliega antes): el `migrate` aplica 0247 en el siguiente `up -d`; la etiqueta de Traefik del servicio `api` cambia (Task 6), así que `docker compose up -d` recrea `api`; la imagen `web` cambia (`deploy/nginx.conf`, Task 4); `ITS_MCP_REGISTRATION=open` pasa a leerlo también la API y abre solo el registro dinámico (C2), sin clave `.env` nueva; ninguna ruta nueva de OpenBao (la clave `auth-signing` es de la fase 1). La secuencia completa de despliegue la da el informe del proyecto 4, y empieza por `docker compose exec openbao bao status` (Task 12, Step 7); esta lista es lo que esta fase le añade.

## Puntos de encuentro con la fase 2

La fase 2 (diseño §3 contactos del portal, §4 Microsoft Entra) se planifica a la vez. Archivos que las dos fases tocan o de los que esta depende, y qué hace aquí la fase 3:

| Archivo | Qué hace la fase 3 | Cómo se fusiona |
|---|---|---|
| `backend/migrations/versions/` | 0247 con `down_revision "0246"` (Task 5) | después de 0246, nunca a la vez |
| `backend/app/core/auth/lookup.py` | **no lo toca**: consume `org_of_slug(engine, slug)` y su función `its_auth_org` (`SECURITY DEFINER`), de la Task 1 de la fase 2 (Task 5, `store.deployment_org`; B3-13) | la fase 2 primero |
| `backend/app/core/auth/retention.py`, `backend/tests/test_auth_retention.py` | tres pasos al final de `STEPS` (`oauth_codes`, `oauth_grants`, `oauth_apps`), sus dos ventanas, tres viñetas al final de «WHAT GOES, AND AFTER HOW LONG», el párrafo del alcance («over these tables only», decisión 4) y las tres claves a cero en la prueba de los metadatos exactos (Task 13). El párrafo de `STEPS` **no lo toca**: la fase 2 ya lo termina con «Phase 3 adds its authorization codes, its grants (refresh-token families, whose tokens cascade) and its idle apps (`oauth_clients`) here.» (su X4), que dice lo que esta fase añade (N-M7) | después de la Task 3 de la fase 2; si la fase 2 cambia el registro o esa prueba en su revisión, se conserva lo suyo y se añaden los tres pasos. Su fila dice que, si esta fase reescribe esa frase, la busque por su texto nuevo: no la reescribe, así que no hay nada que buscar |
| `backend/app/models/__init__.py` | exporta `OAuthClient`, `OAuthCode`, `OAuthRefreshFamily`, `OAuthRefreshToken` junto a `McpRevocation` (Task 5) | unión |
| `backend/app/core/client_scope_catalog.py` | cuatro entradas `oauth_*` (Task 5) | unión |
| `backend/app/core/client_scope.py` | `ALL_SCOPE_ALLOWLIST` gana `app/core/auth/oauth/store.py::oauth_session` (Task 5) | unión (la fase 2 añade `retention.py::sweep_org` y `entra_flows.py::_entra_session`) |
| `backend/tests/test_client_catalog.py` | `SURROGATE_COLUMNS` gana `oauth_codes.code_hash` y `oauth_refresh_tokens.token_hash`; la guarda nueva `OAUTH_SESSION_USERS` (Tasks 5–10); el motivo de `users_org_id_external_subject_key` (Task 9) | unión |
| `backend/app/api/deps.py` | la rama MCP de `admit()` verifica el token propio y devuelve su `Admission` sin `principal` (Task 9; decisión 6, la fila siguiente); sus imports pierden `AsyncSession`, `audit_service`, `Organization` y lo de Keycloak salvo `JwksCache`; la rama de la cookie y `require_step_up` no cambian | si la fase 2 toca `admit(contacts=True)` o `_admit_session`, se conservan los dos cambios; esta fase no toca la rama de la sesión. Tras la fusión, la puerta de la consola responde a un contacto 403 `portal_only` sin log, y la de MCP sigue como en la fase 1 (su log y un 403 sin código): a propósito, porque un contacto no llega a tener concesión (Task 9, decisión 2; re-revisión f3-3, X10). La Task 14 de la fase 2 añade `require_permission_on_every_client_with_step_up` al final del archivo, lejos de `admit()`: unión. Esta fase no quita ningún nombre que usen los cambios de la fase 2 (la Task 14 usa `Callable`, `Coroutine`, `Any` y `Depends`) |
| El canal MCP nunca cuenta como confirmación reciente (el punto 4 de los «Puntos de encuentro con la fase 3» de la fase 2; su Task 14, decisión 5) | la rama MCP de `admit()` no lleva `principal`, así que `require_step_up` responde 403 `step_up_required` a toda llamada del canal (Task 9, decisión 6, y el comentario de su `return`). Lo fija `tests/test_mcp_own_tokens.py::test_an_mcp_call_never_counts_as_a_recent_confirmation`: la persona acaba de confirmar su factor en la consola, y `factors/reset` por el canal sigue pidiéndolo | se sostiene sin tocar nada de la fase 2. En el árbol fusionado lo prueban también las dos pruebas MCP de `tests/test_step_up_widened.py` (los tenants por `clients_update` y el interruptor por `PATCH /orgs/current`), re-apuntadas como dice su fila. La decisión del consentimiento (Task 8, C1) pide la confirmación a la sesión de la consola; por el canal está en `NEVER_EXPOSED` |
| `backend/app/api/v1/auth.py` | **no lo toca**: el consentimiento va en `backend/app/api/v1/oauth_consent.py` | — |
| `backend/app/core/auth/notices.py` | el aviso `app_connected` y el parámetro `host` de `_compose` y `tell` (Task 8) | unión: la fase 2 no añade tipos de `Notice` (sus avisos reutilizan `factor_added`, `factor_removed` y `password_changed`); cambia `reset_mail`, `contact_invitation_mail` y la rama `"microsoft"` de `_factor_name`, otras líneas (re-revisión f3-3, X7) |
| `backend/app/core/auth/flows.py`, `backend/app/core/auth/account.py` | el import `grants as ai_grants`, una línea junto a cada `revoke_all_for_user(session, user.id)` y `ai_grants_revoked` en el evento del acto; en `account.py`, el tipo de `_record` (Task 10) | unión; si la fase 2 añade un acto que revoca los equipos recordados de un contacto, no necesita la línea (un contacto no tiene concesiones de IA: no pasa la puerta de la consola) |
| `backend/app/modules/user_admin/service.py` | lo mismo, en `set_enabled`, `reset_factors` y `delete_user` (Task 10) | unión |
| `backend/tests/test_auth_signin.py`, `backend/tests/test_auth_password_reset.py`, `backend/tests/test_auth_recover_command.py` | cinco aserciones de metadatos exactos ganan `ai_grants_revoked` (Task 10) | unión; si la fase 2 cambia los mismos metadatos, las claves de las dos |
| `backend/app/core/config.py` | `mcp_registration: str = ""` (Task 6) | sin choque: la fase 2 no añade campos a `Settings` (con su C4, la credencial de Microsoft vive en la bóveda; re-revisión f3-3, X5) |
| `backend/app/main.py` | el manejador de `OAuthError` (Task 1) y tres routers (Tasks 6, 7, 8) | unión |
| `backend/app/core/i18n/es.py` | entradas nuevas (Tasks 6, 8, 9, 10) | unión |
| `backend/app/api/security_headers.py`, `backend/tests/test_security_headers.py` | `NO_STORE_ROUTES` y `SECRET_ONCE` ganan la decisión del consentimiento, `POST /api/v1/oauth/authorize/decision` (Task 8) | unión: la fase 2 añade a los dos `POST /api/v1/clients/{client_id}/contacts/{contact_id}/invite` y `POST /api/v1/clients/{client_id}/contacts/{contact_id}/access/reset` (pueden devolver un enlace de invitación); en el árbol fusionado los dos conjuntos llevan las tres rutas, y `test_every_never_exposed_route_that_returns_a_secret_is_listed` lo exige (re-revisión f3-3, X2) |
| auditoría: `frontend/src/i18n/{en,es}/auditVocabulary.ts`, `frontend/src/modules/audit/auditVocabulary.english.test.ts` | cinco acciones `mcp.*` y una en `HAND_WORDED` (Tasks 7, 8) | unión |
| `frontend/src/lib/globalRoutes.json` | no cambia | regenerado en el árbol fusionado si la fase 2 lo cambia |
| `backend/tests/test_logging_discipline.py` y el §2.2 de `docs/superpowers/specs/2026-08-14-logging-kernel-design.md` | +1 (Task 3), +1 (Task 7), −3 (Task 10) | se suman los deltas de las dos fases y se escribe un párrafo «NOW N» por cambio. **La línea `assert total == …` (y el «expected N» de su mensaje) la cambian las dos fases igual, 108 → 109 (la Task 5 de la fase 2 y la Task 3), así que git las une sin avisar y deja 109 donde la cuenta es 110: se escribe a mano 108 + 1 (T5 de la fase 2) + 1 (Task 3) = 110, y la Task 7 lo comprueba antes de sumar la suya.** Los párrafos del docstring, en cambio, anclan los dos sobre «NOW 108»: ahí git sí choca y se conservan los dos |
| `backend/app/mcp/exclusions.py` y `backend/tests/test_mcp_channel_api.py` (`NEVER_EXPOSED`) | **+1**: `POST /api/v1/oauth/authorize/decision`, en `NEVER_EXPOSED_REASONS` tras `POST /users/{user_id}/invite/resend` y en `OWN_SIGN_IN_NEVER_EXPOSED` tras las dos de invitaciones (Task 8); una clave de `NOT_FOR_MCP` nueva y otra cambiada (Tasks 8 y 10); en la prueba, `await mcp_token(` y tres pruebas re-apuntadas (Task 9) | unión. La fase 2 suma sus entradas, las de su Task 14 incluidas: `_RECENT_FACTOR`, un párrafo del docstring y su bloque tras `POST /me/recovery-codes` —las cinco rutas de S1 y las cuatro de S5 (`POST /roles`, `PUT /roles/{role_id}/permissions`, `DELETE /roles/{role_id}`, `POST /users/{user_id}/enable`)—, y en la prueba `RECENT_FACTOR_NEVER_EXPOSED` tras el `}` de `OWN_SIGN_IN_NEVER_EXPOSED` y la igualdad con los tres conjuntos, que la entrada de esta fase sigue cumpliendo. Ningún número fusionado se fija aquí: la línea `assert len(NEVER_EXPOSED) == …`, que cambian las dos, se escribe a mano como 36 (la base) + el delta de la fase 2, el que diga su plan al fusionar (+19 en `3a214fd`), + 1 (esta fase). Si entradas de las dos fases caen en el mismo sitio (p. ej. las dos de contactos de la Task 8 de la fase 2, que su plan no sitúa dentro del conjunto), a mano, conservando todas; nunca se resta una |
| `backend/app/mcp/descriptions/part_d.py` | **no lo toca**: ninguna ruta nueva de esta fase es herramienta (la decisión del consentimiento está en `NEVER_EXPOSED`; sus detalles y la de revocar, en `NOT_FOR_MCP`), así que ninguna lleva descripción | nada que fusionar. La Task 14 de la fase 2 quita las descripciones de las rutas que salen de las herramientas y reescribe las de `PATCH /orgs/current` y `PATCH /clients/{client_id}`; `tests/test_mcp_descriptions.py`, que corren las Tasks 8 y 10, lee lo que haya en la rama |
| `backend/tests/test_mcp_catalog.py` | **no lo toca**; lo corren las Tasks 8 y 10 | la Task 14 de la fase 2 cambia su ejemplo `users_roles_create` por `clients_sites_create`: nada que fusionar. Ninguna prueba de esta fase espera que sea herramienta una ruta que la Task 14 saca de las herramientas, de S1 o de S5 |
| `backend/tests/test_step_up_widened.py` (lo crea la Task 14 de la fase 2) | sus dos pruebas de MCP, re-apuntadas como toda prueba de MCP (Task 9, Step 7): `await mcp_token(...)`, y en `test_on_the_mcp_channel_switching_ai_access_on_is_refused` el token en su propia línea, porque con el `await` dentro de la f-string la de `bearer` pasaría de 100 columnas. Lo que afirman no cambia: la negativa es la de `require_step_up` (la fila del canal MCP, arriba) | lo hace la Task 9 si el archivo ya está en su rama; si no, quien fusione después la Task 14 de la fase 2, con el mismo cambio. La fila de la fase 2 pide `await mcp_token(...)`: es eso, con la línea partida |
| `backend/tests/test_mcp_tools.py` | `await mcp_token(` en `:210` (`_admin`) y `:277`, y los dos `McpPrincipal` con `McpGrant` (Task 9, Step 7) | unión. La Task 14 de la fase 2 (S5: `POST /roles` deja de ser herramienta) pasa de `roles_create` a `roles_update` `test_a_technician_does_not_see_or_call_an_org_admin_tool` (desde `:280`, debajo de la `:277` de esta fase) y `test_a_409_is_the_apis_detail` (`:438-:447`). Entre `:277` y `:280` quedan dos líneas sin tocar (el `async with` y `catalog = …`), así que git une los dos cambios; si no, a mano, conservando los dos (`await mcp_token(...)` arriba, `roles_update` debajo). Ninguna prueba de esta fase llama a `roles_create`, `roles_permissions_set`, `roles_delete` ni `users_enable` |
| Las pruebas de esta fase y las rutas de la Task 14 de la fase 2 (S1, S3 y S5) | ninguna prueba que esta fase crea o reescribe llama con una sesión a `PUT /settings/email`, `PUT /settings/sessions`, `PUT /settings/microsoft-sign-in`, `POST /users/invite`, `POST /users/{user_id}/invite/resend`, `POST /users/{user_id}/roles`, `DELETE /users/{user_id}/roles/{assignment_id}`, `PATCH /clients/{client_id}`, `PATCH /orgs/current`, `POST /roles`, `DELETE /roles/{role_id}`, `PUT /roles/{role_id}/permissions` ni `POST /users/{user_id}/enable`: el interruptor de MCP se fija por SQL (`oauth_org`, `mcp_org`), y el técnico invitado de la Task 12 lo siembra `create_user`. Las rutas que ya pedían la confirmación (`factors/reset`, `/me/password`, la decisión del consentimiento) las llaman sesiones frescas (`signed_in`, `sign_in(..., step_up=True)`, `connect`). Las pruebas MCP de esta fase llaman `me_get`, ninguna herramienta de esas rutas | una prueba que se añada y llame a una de ellas con una sesión usa `tests.session_support.sign_in(env, org_id, sub, step_up=True)` —lo que hace `ApiEnv.headers`— o, para una sesión que ya existe, `app.core.auth.sessions.mark_step_up(session, session_id)`. Es lo que dice la fila de la fase 2 |
| `backend/tests/test_route_gating.py` (`UNGATED_BY_DESIGN`) | +2 rutas del consentimiento (Task 8) | unión |
| `backend/tests/test_auth_phase2_falsifiers.py` | **no lo toca**: lo crea la Task 13 de la fase 2 y recorre toda `/api/v1`; las rutas nuevas de esta fase lo pasan sin excepción (las del consentimiento, por `bound_auth_context`, que a un contacto le responde `portal_only`; la de revocar, por `require_permission`). La Task 8 lo corre si la worktree lo tiene (re-revisión f3-2, M-7) | la suite completa de la Task 12, Step 5, lo corre en el árbol fusionado |
| `deploy/nginx.conf`, `backend/tests/test_deploy_frame_headers.py` | `frame-ancestors 'none'` y `X-Frame-Options: DENY` en cada `location` (Task 4) | si la fase 2 añade un `location` (la vuelta de Entra es una ruta de la consola, así que probablemente no), lleva las dos cabeceras: la prueba lo exige |
| `deploy/.env.example` | comentario de `ITS_MCP_REGISTRATION` (C2) y de `KC_HOSTNAME` (Task 12) | unión |
| `deploy/README.md` | la sección «AI connections (MCP)» (tres sustituciones), tres frases sueltas, una fila de una tabla, una viñeta al final de la lista «Refused twice» de `### What is never reachable over MCP` y, en «Status of this runbook», solo la línea `:32` (Task 12) | unión: la fase 2 escribe la de Microsoft y el portal, y en «Status of this runbook» solo la frase «Three things phase 1 changed…», que en `3a1ba1c` está en la línea 36, cuatro más abajo (su fila dice `:35`, «dos líneas más abajo»); ninguna de las dos refluye ese párrafo, o la fusión choca (re-revisión f3-3, X11). En `### What is never reachable over MCP`, el párrafo de su Task 14 («The acts that ask for your second factor again», entre «**Not tools:** …» y «Every result is also scanned …») queda entre las dos ediciones de esta fase, la viñeta de «Refused twice» arriba y la sustitución desde `### Residual risks, stated plainly` abajo, con líneas sin tocar a cada lado: ninguna lo toca. Lo que escribe esta fase no lo contradice: apagar MCP, revocar una app y desactivar a una persona no piden la confirmación en ninguna de las dos, y de lo que la Task 14 sí ampara (encender MCP, quitar un rol) no dice que no la pida |
| `deploy/docker-compose.yml` | la regla de Traefik del router `api` (Task 6) | si la fase 2 también la cambia, se conservan los dos `Path` |
| `.github/workflows/ci.yml` | **no lo toca** (fase 4) | — |
| `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` | enmiendas fechadas en §2.5, §2.7 (C1) y §5.4 (C2) (Task 12); la de §2.5, al final de la sección, detrás de la línea en blanco que sigue al párrafo «Exigida en los últimos 10 minutos para: …» y antes de `### 2.6` | unión: la fase 2 enmienda §3, §4, §9 y los «Límites» de §2.6 (su Task 13), y reescribe ese párrafo de §2.5 con la ampliación del 2026-11-08 sin tocar la línea en blanco que lo sigue (su Task 14). Esa línea queda entre las dos ediciones, así que git las une: en el árbol fusionado §2.5 lleva las dos, la de la Task 14 primero. La enmienda C1 no contradice la lista ampliada: aprobar una aplicación de IA no está en ella ni en lo que deja fuera, y rechazarla, como revocarla, no pide nada en las dos. En §9 no hay nada de esta fase que corregir (re-revisión f3-3, X6) |
| `docs/03-security-model.md` | la frase de MCP de la viñeta de la autenticación (Task 12, Step 4): en `3a1ba1c`, «MCP clients still authenticate through Keycloak until phase 3.» (`:161`) | **no es una unión, aunque las filas de las dos fases lo decían**: la fase 2 reescribe esa misma frase (su Task 13: «Not built: … (out of scope).» y «MCP clients still authenticate through Keycloak; phase 3 replaces that, and phase 4 removes Keycloak.», con sus viñetas de contactos y de Microsoft detrás) y añade una fila a la tabla «Threat model», debajo de «A console left signed in on an unattended computer» (su Task 14). Con la Task 13 de la fase 2 en la rama, la Task 12 sustituye la frase de MCP de la fase 2 por la suya (Step 4, con su `grep`). Si la fase 2 se fusiona después, `:161` choca: queda la frase de esta fase en lugar de las dos de MCP, y todo lo demás de la fase 2. La fila de la Task 14 no la toca esta fase, y no dice nada que la frase nueva contradiga. La Task 12 (Step 4) también alarga la viñeta «Retention:» que escribe la Task 13 de la fase 2, con los códigos, las concesiones y las aplicaciones de la Task 13 de esta fase, y lo comprueba con su `grep -c` |
| `docs/modules/console-auth/spec.md` | una frase, la de MCP (`:98-99`; Task 12) | unión: la fase 2 añade su párrafo «Superseded again on 2026-11-06» debajo del de `:6` (su Task 13), lejos de `:98`. Nada de lo que escribe esta fase dice que la fase 3 retire Keycloak: lo retira la fase 4 (diseño §8); la búsqueda de «until phase 3» de la Task 12, Step 4, lo comprueba en el árbol fusionado (re-revisión f3-3, X1) |
| `backend/app/api/v1/me.py`, `backend/app/core/auth/__init__.py`, `backend/tests/conftest.py` | solo comentarios que decían «hasta la fase 3» (Task 12) | unión |
| `backend/tests/test_domain_gate_doors.py`, `backend/tests/test_auth_falsifiers.py`, `backend/tests/test_me_profile.py` | pruebas de la puerta de MCP re-apuntadas al token propio (Task 9) | si la fase 2 añade pruebas de la puerta de un contacto o de Microsoft, se conservan las dos; ninguna de las de aquí toca la rama de la sesión |
| `frontend/src/App.tsx`, `frontend/src/lib/session.ts`, `frontend/scripts/routes.mjs`, `frontend/src/test/routes-covered.test.ts` | la ruta `/oauth/authorize`, la prop `bare` de `RequireAuth`, `EDGE_PATHS`, `EXCLUDED` (Task 4) | unión; `PUBLIC_PATHS` no cambia aquí. En `App.tsx`, esta fase y la fase 2 añaden una ruta pública o de sesión tras el mismo ancla (`<Route path="/recover" …>`): mismo ancla, git choca; se conservan las dos |
| `frontend/src/i18n/{en,es}/auth.ts`, `frontend/src/modules/auth/__tests__/harness.tsx` | `auth.consent.*`, los métodos del consentimiento y del segundo factor en `calls`, la ruta en `renderAuth` (Task 4) | unión; la fase 2 también toca `calls`, `installDefaults` y `renderAuth` del arnés y los dos `auth.ts`: mismo ancla, git choca; se conservan las dos |
| `frontend/src/components/stepUp.tsx` | **no lo toca**: `ConsentPage` usa `useStepUp` (C1, Task 4) | la Task 14 de la fase 2 añade `sendOnce` y amplía el docstring; `useStepUp`, `run` y `StepUpRun` no cambian, así que la página de consentimiento y sus pruebas (Task 4) siguen igual |
| `frontend/src/lib/api.ts` | dos métodos del consentimiento, después de `acceptRecovery` (sus tipos van en `frontend/src/modules/auth/types.ts`); `revokeMcpConnection` y sus tipos (Tasks 4, 11) | unión |
| `frontend/src/modules/settings/AiConnectionsTab.tsx`, `frontend/src/modules/settings/api.ts` | en la pestaña, la ruta de la primera frase de la cabecera (`{app_id}`), el párrafo «A REVOKE IS IN FORCE …», `revokeFor`, `confirming`, el `subject` de la `ConfirmDialog` de «para todos», `RevokeOutcome` y `AppRow`; en `api.ts`, `useRevokeMcpConnection` (Task 11) | unión: la Task 14 de la fase 2 añade, en la pestaña, un párrafo antes de «TURNING IT ON ASKS FIRST», el import de `useStepUp`, `useUpdateCurrentOrgMutation(stepUp.run)` y `{stepUp.dialog}` detrás de la última `ConfirmDialog`; en `api.ts`, `sendOnce` en el import de `stepUp` y `run` en doce hooks (cuatro de S5). Entre cada cambio de una fase y el más cercano de la otra queda al menos una línea sin tocar, y la Task 11 ancla por texto. `useRevokeMcpConnection` sigue sin `run`: revocar solo quita acceso (S4) |
| `frontend/src/modules/settings/__tests__/harness.tsx` | `MCP_APP_ID`, `MCP_APP_FIXTURE` con `id`, `name`, `host` y `kind`, y el `mockResolvedValue` de `calls.revokeMcpConnection` (Task 11) | unión: la fase 2 añade sus fixtures y llamadas de Microsoft; la Task 11 ancla cada edición por su texto, no por la línea (re-revisión f3-3, X8) |
| `frontend/src/modules/settings/fixtures.ts` | `MCP_APPS`, su comentario y `revokeMcpConnection` (Task 11) | unión: la fase 2 añade `getMicrosoftSettings` y `putMicrosoftSettings` |
| `frontend/src/modules/settings/SettingsPage.tsx` (la tabla de rutas de la cabecera) | la fila «AI connections»: `POST /mcp/connections/{app_id}/revoke` (Task 11) | unión: la fase 2 añade su fila (Microsoft); cada fase cambia solo la suya |
| `frontend/src/i18n/{en,es}/settings.ts` | en `aiConnections.apps`: `calledItself`, `selfRegistered`, `unregistered`, `subtitle` y `empty`; se van `partialMe` y `partial` (Task 11) | unión: la fase 2 añade `page.tabs.microsoft` y la sección `microsoft` |
| `frontend/src/test/secretMutations.test.ts` | `oauthAuthorizeDecision` en `SECRET_METHODS` y su sitio en `SECRET_SITES`, por `stepUp.run` (Task 4) | unión; la fase 2 también añade sitios a `SECRET_SITES`, tras `acceptRecovery`: mismo ancla, git choca; se conservan las dos |

## Apéndice A — Contratos

### A.1 Módulos del backend (`backend/app/core/auth/oauth/` y los que esta fase toca fuera)

- `policy.py` (Task 1): `ACCESS_TOKEN_TTL_S = 600`, `CODE_TTL_S = 60`, `REFRESH_IDLE = timedelta(days=30)`, `CLIENT_IDLE = timedelta(days=30)`, `SCOPE = "mcp"`, `SCOPES_SUPPORTED = ("mcp", "offline_access")`, `GRANT_TYPES = ("authorization_code", "refresh_token")`, `RESPONSE_TYPES = ("code",)`, `ACCESS_TYP = "at+jwt"`, `AUTHORIZE_PATH = "/oauth/authorize"`, `TOKEN_PATH = "/oauth/token"`, `REGISTER_PATH = "/oauth/register"`, `REVOKE_PATH = "/oauth/revoke"`, `METADATA_PATH = "/.well-known/oauth-authorization-server"`, `JWKS_PATH = "/.well-known/jwks.json"`, `MAX_REDIRECT_URIS = 10`, `MAX_URI_LENGTH = 2000`, `MAX_CLIENT_NAME = 200`, `MAX_CLIENT_ID = 255`, `REGISTRATIONS_PER_HOUR = 20`, `MAX_REGISTRATIONS = 200`, `IPV6_NETWORK_BITS = 64`, `TOKEN_REQUESTS_PER_MINUTE = 600`, `MAX_REQUEST_BYTES = 16384`; `def issuer(settings: object) -> str | None`; `def endpoint(settings: object, path: str) -> str | None`; `def registration_open(settings: object) -> bool` (solo el registro dinámico, C2); `def network_of(address: IPv4Address | IPv6Address | None) -> str` (`"ip:<v4>"`, `"net:<v6>/64"`, `"ip:unknown"`); `def display_name(raw: str) -> str | None`.
- `errors.py` (Task 1, ampliado por la 8): `class Problem(NamedTuple): error: str; description: str; status: int = 400`; las constantes `Problem` de la Task 1 (`UNAVAILABLE`, `KEYS_UNAVAILABLE`, `REGISTRATION_CLOSED`, `REGISTRATIONS_FULL`, `SLOW_DOWN`, `BUSY`, `TOO_LARGE`, `NOT_JSON`, `NO_REDIRECTS`, `BAD_REDIRECT`, `BAD_GRANT_TYPES`, `BAD_RESPONSE_TYPES`, `BAD_NAME`, `NOT_FORM`, `MALFORMED`, `REPEATED`, `MISSING`, `CLIENT_AUTH`, `UNKNOWN_CLIENT`, `UNSUPPORTED_GRANT`, `BAD_CODE`, `BAD_VERIFIER`, `BAD_REFRESH`, `WRONG_CLIENT`, `BAD_TARGET`, `BAD_SCOPE`) y las seis de la Task 8 (`AUTHZ_REPEATED`, `AUTHZ_RESPONSE_TYPE`, `AUTHZ_CHALLENGE`, `AUTHZ_METHOD`, `PERSON_CANNOT`, `DECLINED`); `class OAuthError(Exception)` con `__init__(self, problem: Problem, *, headers: Mapping[str, str] | None = None)` y atributos `problem`, `headers`; `NO_STORE: dict[str, str]`; `async def oauth_error_handler(request, exc) -> JSONResponse`; `def no_store(content: Mapping[str, Any], status_code: int = 200) -> JSONResponse`.
- `redirects.py` (Task 1): `LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})`; `RESPONSE_PARAMETERS` (`code`, `state`, `iss`, `error`, `error_description`, `error_uri`); `class RedirectRejected(ValueError)` con `reason: str`; `def check_registered(uri: object) -> str` (solo ASCII; sin parámetros de la respuesta en su consulta); `def is_loopback(uri: str) -> bool`; `def stays_on_this_computer(uri: str) -> bool`; `def matches(registered: Sequence[str], requested: str) -> bool`; `def host_of(uri: str) -> str`; `def with_params(uri: str, params: Mapping[str, str]) -> str` (conserva la consulta registrada tal como está escrita).
- `pkce.py` (Task 1): `def valid_challenge(value: str) -> bool`; `def valid_verifier(value: str) -> bool`; `def s256(verifier: str) -> str`; `def verify(verifier: str, challenge: str) -> bool`.
- `access.py` (Task 2): `@dataclass(frozen=True) class McpGrant(user_id: UUID, org_id: UUID, client_id: str, family_id: UUID, iat: int, exp: int, email: str = "")` con la propiedad `azp -> str` (= `client_id`); `async def mint_access_token(*, settings: object, user_id: UUID, org_id: UUID, client_id: str, family_id: UUID, now: int | None = None) -> tuple[str, int]`; `async def verify_access_token(token: str, *, settings: object) -> McpGrant | None`; `def unverified_client_id(token: str) -> str`; `REFRESH_EVERY_S = 5.0`.
- `cimd.py` (Task 3): `MAX_DOCUMENT_BYTES = 5 * 1024`, `FETCH_TIMEOUT_S = 5.0`, `MIN_CACHE = timedelta(minutes=5)`, `MAX_CACHE = timedelta(hours=24)`; `Reason` (`not_a_document_url`, `own_origin`, `blocked`, `unavailable`, `not_ok`, `too_large`, `encoded`, `not_json`, `mismatch`, `incomplete`, `bad_redirect`, `secret`, `unsupported`), `TRANSIENT = frozenset({"unavailable"})`; `class DocumentRefused(Exception)` con `reason: Reason` y la propiedad `transient`; `@dataclass(frozen=True) class ClientDocument(client_id: str, client_name: str, redirect_uris: tuple[str, ...], cache_for: timedelta)`; `def is_document_client_id(client_id: str) -> bool` (CIMD-03 §3, ASCII); `def on_origin(client_id: str, issuer: str) -> bool`; `def cache_lifetime(cache_control: str) -> timedelta` (`clamp(max-age o 0, 5 min, 24 h)`); `async def fetch_document(client_id: str) -> ClientDocument` (un WARNING por minuto como mucho).
- `store.py` (Task 5): `async def deployment_org(app: Any) -> UUID | None` (`lookup.org_of_slug(app.state.app_engine, slug)`, de la fase 2); `@asynccontextmanager async def oauth_session(org_id: UUID, engine: AsyncEngine) -> AsyncIterator[AsyncSession]`.
- `clients.py` (Task 5, ampliado por la 6): `TOUCH_EVERY = timedelta(hours=1)`; `@dataclass(frozen=True) class ClientView(id: UUID, client_id: str, kind: str, client_name: str | None, redirect_uris: tuple[str, ...])`; `def view(row: OAuthClient) -> ClientView`; `async def find_client(session, *, org_id: UUID, client_id: str, now: datetime) -> OAuthClient | None`; `def touch(row: OAuthClient, now: datetime) -> None` (escribe como mucho una vez por `TOUCH_EVERY`). Task 6: `PURGE_BATCH = 100`; `REGISTRATIONS_PER_HOUR`; `@dataclass(frozen=True) class Registration(client_name: str | None, redirect_uris: tuple[str, ...])`; `def parse_registration(body: object) -> Registration`; `async def mcp_switched_on(session, org_id: UUID) -> bool`; `async def registration_offered(app: Any) -> bool`; `async def purge_idle(session, *, org_id: UUID, now: datetime) -> int`; `async def register(session, *, org_id: UUID, registration: Registration, now: datetime) -> OAuthClient`; `def registration_response(row: OAuthClient) -> dict[str, Any]`; `async def register_new(app: Any, *, org_id: UUID, registration: Registration, now: datetime) -> dict[str, Any]` (el interruptor, la purga, el techo y la escritura en una transacción); `Fetch = Callable[[str], Awaitable[cimd.ClientDocument]]`; `async def resolve_client(app: Any, *, org_id: UUID, client_id: str, now: datetime, fetch: Fetch | None = None, record_use: bool = True) -> ClientView | None` (sin `fetch`, `cimd.fetch_document`, resuelto al llamar; lanza `cimd.DocumentRefused`, que no se guarda ni borra la copia guardada; un fallo transitorio la conserva; con `record_use=False` no escribe nada, re-revisión f3-2, M-3).
- `grants.py` (Task 7): `def hash_secret(value: str) -> bytes`; `def new_secret() -> str`; `def names_resource(value: str, resource: str) -> bool`; `@dataclass(frozen=True) class IssuedTokens(access_token: str, expires_in: int, refresh_token: str)`; `async def issue_code(session, *, org_id, user_id, oauth_client_id, redirect_uri, code_challenge, resource, now) -> str`; `async def open_family(session, *, org_id, user_id, oauth_client_id, resource, now) -> tuple[OAuthRefreshFamily, str]`; `async def exchange_code(app, *, org_id, client_id, code, redirect_uri: str | None, code_verifier) -> IssuedTokens`; `async def refresh_grant(app, *, org_id, client_id, refresh_token) -> IssuedTokens`; `async def revoke_presented(app, *, org_id, client_id, token) -> None`; `async def revoke_family(session, family_id, reason, now) -> bool`; `async def revoke_all_for_user(session, user_id, reason) -> int`; `async def revoke_app(session, *, oauth_client_id, user_id: UUID | None, reason, now) -> int`; `async def family_is_live(session, *, family_id, user_id, client_id: str, now) -> bool`. Ya no `CODE_KEPT` ni `FAMILY_KEPT` (las ventanas son de la Task 13).
- `authorize.py` (Task 8): `@dataclass(frozen=True) class AuthorizationRequest(client: ClientView, redirect_uri: str, state: str | None, code_challenge: str, resource: str)`; `class NotRedirectable(Exception)` con `__init__(self, detail: str, *, error: str)`; `class Redirectable(Exception)` con `__init__(self, redirect_uri: str, state: str | None, problem: Problem)`; `def parse_query(query: str) -> dict[str, list[str]]`; `def answer(redirect_uri: str, state: str | None, iss: str, **params: str) -> str`; `async def validate(app, *, org_id: UUID, query: str, now: datetime, fetch: Fetch | None = None, record_use: bool = True) -> AuthorizationRequest` (los dos, a `resolve_client`).
- `consent.py` (Task 8): `async def details(request: Request, ctx: AuthContext, *, query: str) -> dict[str, Any]`; `async def decide(request: Request, ctx: AuthContext, *, query: str, approve: bool) -> dict[str, Any]` (la ruta ya pidió el segundo factor reciente si `approve`).
- `connections.py` (Task 10): `@dataclass(frozen=True) class Registration(id: UUID, client_id: str, name: str | None, kind: str, host: str)`; `async def registrations(engine, *, org_id: UUID, client_ids: Sequence[str]) -> dict[str, Registration]`; `@dataclass(frozen=True) class HeldApp(registration: Registration, mine: bool)` (`mine`: uno de los permisos vivos es de quien pregunta; re-revisión f3-3, N4); `async def held_apps(engine, *, org_id: UUID, user_id: UUID, now: datetime, limit: int) -> list[HeldApp]`; `async def revoke_app_access(engine, *, org_id: UUID, actor_id: UUID, app_id: UUID, scope: Literal["me", "everyone"], now: datetime) -> int`.
- `backend/app/core/egress.py` (Task 3): `@dataclass(frozen=True) class EgressBody(status_code: int, content: bytes, truncated: bool, content_type: str, cache_control: str, content_encoding: str = "")`; `async def get_bounded(url: str, *, max_bytes: int, timeout_s: float = 10.0, headers: Mapping[str, str] | None = None) -> EgressBody` (toda dirección resuelta, pública: ninguna de uso especial).
- `backend/app/core/auth/notices.py` (Task 8): `Notice` gana `"app_connected"`; `tell(..., host: str | None = None)`.
- `backend/app/core/auth/retention.py` (de la fase 2; Task 13): `OAUTH_CODE_RETENTION = timedelta(minutes=10)`, `OAUTH_GRANT_RETENTION = timedelta(days=1)`; `STEPS` termina en `("oauth_codes", …)`, `("oauth_grants", …)`, `("oauth_apps", …)`.

### A.2 Rutas

| Método y ruta | Dónde | Quién | Cuerpo → respuesta |
|---|---|---|---|
| `GET /.well-known/oauth-authorization-server` | `app/api/oauth_registration.py` (Task 6) | público | metadatos RFC 8414: `client_id_metadata_document_supported: true` siempre que hay emisor; `registration_endpoint` solo con el registro abierto y MCP encendido (C2) |
| `POST /oauth/register` | ídem | público, por red (IPv6 por /64), solo con `ITS_MCP_REGISTRATION=open`, MCP encendido y menos de 200 registros vivos | JSON RFC 7591 → 201; 403 `access_denied` cerrado, 503 `temporarily_unavailable` lleno, 429 por red |
| `POST /oauth/token` | `app/api/oauth_tokens.py` (Task 7) | público (cliente público), 600 por minuto y red | `application/x-www-form-urlencoded` (`redirect_uri` y `resource` opcionales) → `{"access_token","token_type":"Bearer","expires_in":600,"refresh_token","scope":"mcp"}`; 413 RFC 6749 por encima de 16 KiB; 429 con `Retry-After: 60` |
| `POST /oauth/revoke` | ídem | público, el mismo límite | form `token`, `client_id` → 200 vacío |
| `GET /oauth/authorize` | **la consola** (Task 4) | la persona con sesión | página, nunca enmarcada |
| `POST /api/v1/oauth/authorize/details` | `app/api/v1/oauth_consent.py` (Task 8) | sesión de la consola | `{"query": str}` → `ConsentDetails` |
| `POST /api/v1/oauth/authorize/decision` | ídem | sesión de la consola; con `approve`, segundo factor de los últimos 10 minutos (C1) | `{"query": str, "approve": bool}` → `ConsentAnswer`; 403 `step_up_required` sin él |
| `POST /api/v1/mcp/connections/{app_id}/revoke` | `app/mcp/api.py` (Task 10; antes `{client_id}`) | `mcp.use` (+ `orgs.manage` para todos) | `{"scope": "me"\|"everyone"}` → `{"revoked": true, "grants_revoked": int}` |
| `GET /api/v1/mcp/connections` | ídem (Task 10) | `mcp.use` | cada app gana `id: UUID \| null`, `name: str \| null`, `host: str \| null` y `kind`; también salen las apps con una concesión viva sin uso (`people` 0, `last_used_at` null) |

`ConsentDetails` (lo que responde `…/details`, que nunca es `redirect`) y `ConsentAnswer` (lo que responde `…/decision`, que nunca es `consent`):

```json
{"outcome": "consent", "client": {"name": "Claude", "client_id": "itms_…", "kind": "registered",
  "client_host": null, "redirect_host": "claude.ai", "loopback_only": false}, "scope": "mcp",
  "account": "ana@toeshee.com"}
{"outcome": "refused", "error": "invalid_redirect_uri", "detail": "<frase traducida>"}
{"outcome": "back", "reason": "error", "detail": "<frase traducida>", "redirect_host": "claude.ai",
  "redirect_to": "https://…?error=…&error_description=…&state=…&iss=…"}
{"outcome": "redirect", "redirect_to": "https://…?code=…&state=…&iss=…"}
```

`kind` es `"registered"` (DCR) o `"metadata_document"` (CIMD); `client_host` es el host de `client_id` para un CIMD y `null` para un DCR; `loopback_only` es verdadero cuando todos los redirects registrados son de este equipo. `refused` lleva `error` `temporarily_unavailable` cuando un documento CIMD no se pudo leer ahora mismo, y `access_denied` cuando la persona no puede conectar apps y el redirect no se pudo comprobar. `back` lleva `reason` `"error"` (un error que la app debe oír), `"declined"` (la persona rechazó) o `"denied"` (no puede conectar aquí: otra organización, MCP apagado o sin `mcp.use`); la página lo sigue solo cuando la persona pulsa «Volver a <host>». Solo `redirect`, la aprobación, navega sola.

### A.3 Tablas (migración 0247, Task 5)

- `oauth_clients` (G): `id uuid PK`, `org_id`, `client_id text` (DCR: `itms_` + 32 caracteres base64url; CIMD: la URL), `kind text CHECK in ('registered','metadata_document')`, `client_name text NULL`, `redirect_uris text[]`, `created_at`, `last_used_at`, `metadata_expires_at NULL` (solo CIMD); `UNIQUE (org_id, client_id)`, `UNIQUE (id, org_id)`; índice `(org_id, last_used_at)`.
- `oauth_refresh_families` (O users): `id`, `org_id`, `client_id` (el cliente MSP, del disparador), `user_id`, `oauth_client_id` (con `(oauth_client_id, org_id) → oauth_clients (id, org_id) ON DELETE CASCADE`), `resource`, `created_at`, `last_used_at`, `expires_at`, `revoked_at NULL`, `revoked_reason NULL` (lista cerrada `REVOKE_REASONS`); `UNIQUE (id, user_id)`.
- `oauth_refresh_tokens` (O users): `id`, `org_id`, `client_id`, `user_id`, `family_id`, `token_hash bytea UNIQUE` (32 bytes), `created_at`, `used_at NULL`; `(family_id, user_id) → oauth_refresh_families (id, user_id) ON DELETE CASCADE`.
- `oauth_codes` (O users): `id`, `org_id`, `client_id`, `user_id`, `oauth_client_id` (como en las familias, `ON DELETE CASCADE`), `code_hash bytea UNIQUE` (32 bytes), `redirect_uri`, `code_challenge` (43), `resource`, `created_at`, `expires_at`, `used_at NULL`, `family_id NULL → oauth_refresh_families ON DELETE SET NULL`.

Las barre la retención diaria (Task 13): un código 10 minutos después de caducar, una familia un día después de terminar, una app a los 30 días sin uso.

### A.4 Consola

- `ApiClient.oauthAuthorizeDetails(query: string): Promise<ConsentDetails>` y `ApiClient.oauthAuthorizeDecision(query: string, approve: boolean): Promise<ConsentAnswer>` (Task 4), ambos `{ global: true }`; la decisión va por `stepUp.run` (C1) y por `useSecretMutation`.
- Los tipos (Task 4, en `frontend/src/modules/auth/types.ts`): `ConsentClient { name: string | null; client_id: string; kind: "registered" | "metadata_document"; client_host: string | null; redirect_host: string; loopback_only: boolean }`; `ConsentAnswer = { outcome: "refused"; error; detail } | { outcome: "back"; reason: "error" | "declined" | "denied"; detail; redirect_host; redirect_to } | { outcome: "redirect"; redirect_to }`; `ConsentDetails = { outcome: "consent"; client; scope; account } | Exclude<ConsentAnswer, { outcome: "redirect" }>`.
- `ApiClient.revokeMcpConnection(appId: string, scope: McpRevokeScope): Promise<McpRevokeOut>` (Task 11); `McpAppOut` gana `id: string | null`, `name: string | null`, `host: string | null` y `kind: "registered" | "metadata_document" | null`; `McpRevokeOut = { revoked: boolean; grants_revoked: number }`.
- `RequireAuth({ children, bare })`: con `bare`, la página sin el armazón de la consola (Task 4).

## Apéndice B — Decisiones del plan

La primera versión de este plan dejaba catorce preguntas al operador (P1–P14). La revisión del 2026-11-06 las resolvió todas: doce por una regla citada (tabla B.2) y dos por el operador (C1 y C2, en Global Constraints). Las D de B.1 son decisiones técnicas dentro del diseño, con su fuente.

### B.1 Decisiones técnicas

- **D1. El emisor es `ITS_PUBLIC_CONSOLE_URL` sin barra final, y solo si es https sin ruta.** RFC 8414 §2: el emisor «is a URL that uses the "https" scheme and has no query or fragment components»; RFC 9207 §2, lo mismo para `iss`. Sin ruta, los metadatos están en `/.well-known/oauth-authorization-server` sin inserción (RFC 8414 §3), una ruta que Traefik ya manda a la API (`deploy/docker-compose.yml:139`). Una consola con ruta no tiene emisor: MCP responde 503, y el runbook lo dice (B3-18). El recurso MCP sigue siendo `mcp_resource(settings)`: el mismo texto más `/mcp`.
- **D2. `/oauth/authorize` es una página de la consola, no de la API.** La cookie es `SameSite=Strict` (fase 1, §6.4), y un navegador no la envía en una navegación que empieza en otro sitio (claude.ai): una ruta de la API no vería la sesión. La página se sirve como cualquier otra de la consola (D19 de la fase 1: un `fetch` del mismo origen sí lleva la cookie) y pasa la consulta entera a `POST /api/v1/oauth/authorize/details` y `…/decision`, que la validan cada vez en el servidor. Traefik manda a la API solo `Path(/oauth/token)`, `Path(/oauth/register)` y `Path(/oauth/revoke)`. `SameSite=Strict` no protege del enmarcado por una página del mismo sitio (RFC 6265bis-22 §5.2.1): por eso `frame-ancestors 'none'` (B3-16).
- **D3. La organización de una petición sin persona es la del despliegue.** `/oauth/register`, `/oauth/token` y `/oauth/revoke` llegan sin cookie ni persona; usan `ITS_DEFAULT_ORG_SLUG`, la misma regla que el inicio de sesión de la fase 1 (`flows.login` responde 503 sin él). El id se lee con `lookup.org_of_slug(app.state.app_engine, slug)`, la función `SECURITY DEFINER` de la fase 2 que `app_user` puede llamar, y **nunca** con el motor de administración (diseño §6.2; B3-13). El consentimiento exige que la sesión sea de esa misma organización, porque el canje la usa (B3-09).
- **D4. Solo clientes públicos.** El diseño pone `none` en `token_endpoint_auth_methods_supported` (§5.1) y Claude se registra como cliente público [S33]. Un registro que pide otro método se registra con `none` y la respuesta lo dice (RFC 7591 §3.2.1: «The authorization server MAY reject or replace any of the client's requested metadata values»; B3-08); un documento CIMD con `private_key_jwt` se rechaza (B3-20); una petición al token con cabecera `Authorization` es `invalid_client` 401 con `WWW-Authenticate: Basic` (RFC 6749 §5.2).
- **D5. El token de acceso sigue RFC 9068 en `typ` y claims, firmado ES256, sin declarar conformidad con RFC 9068.** `typ: at+jwt`, `iss`, `exp`, `aud`, `sub`, `client_id`, `iat`, `jti` (RFC 9068 §2.1–§2.2), más `org` y `fam`. RFC 9068 §2.1 pide RS256 a quien declara conformidad («Authorization servers and resource servers conforming to this specification MUST include RS256»), y el diseño fija ES256 (§5.3, §6.3); los metadatos no la declaran (B3-06).
- **D6. El token de acceso nombra su familia y la puerta la comprueba en cada llamada.** §6.5: «MCP valida su JWT (`iss`, `aud`, `kid`, revocación)». Con `fam`, una familia revocada (reutilización, revocación en Ajustes o por el cliente, un acto de la cuenta) deja de valer en la siguiente llamada y no a los diez minutos; `mcp_revocation` se queda para lo que ya hacía (el `iat` frente a `revoked_at`, por persona u organización).
- **D7. El ámbito concedido es siempre `mcp`, y siempre hay refresh.** RFC 6749 §3.3: «The authorization server MAY fully or partially ignore the scope requested by the client»; MCP: el cliente «MUST NOT assume refresh tokens will be issued; the AS retains discretion» [S27]. Los metadatos anuncian `offline_access` porque Claude lo añade solo cuando aparece en `scopes_supported` [S33]; cualquier otro ámbito pedido (`openid`, `profile`) se ignora, y la respuesta del token lleva `scope: "mcp"`.
- **D8. Redirects (§5.2).** https a cualquier host, o http a `127.0.0.1`, `[::1]` o `localhost` (MCP: «All redirect URIs MUST be either localhost or use HTTPS» [S30]); solo ASCII (RFC 3986 §2: «A URI is composed from a limited set of characters»; A3-01); sin fragmento (RFC 6749 §3.1.2); sin usuario ni contraseña, sin `*`, de 2000 caracteres como mucho; sin parámetros de la respuesta en su consulta. Comparación exacta salvo el puerto en loopback (RFC 9700 [S16]; RFC 8252 §7.3: «The authorization server MUST allow any port to be specified at the time of the request for loopback IP redirect URIs»), con el esquema y el host tal como se escribieron; `localhost`, `127.0.0.1` y `[::1]` no son intercambiables, y `localhost` se acepta como excepción escrita a RFC 8252 §8.3 (B3-19). La consulta del redirect se conserva tal como está al añadir `code`, `state` e `iss` (OAuth 2.1-17 §2.3: «which MUST be retained when adding additional query parameters. The query string component MUST NOT include any parameters defined for use in the authorization response.»).
- **D9. Límites de registro y de documento.** De 1 a 10 redirects, nombre de 200 caracteres como mucho (sin caracteres de control), `grant_types` ⊆ {`authorization_code`, `refresh_token`} con el primero, `response_types` = {`code`}, cuerpo de 16 KiB; lo desconocido se ignora (RFC 7591 §2: «The authorization server MUST ignore any client metadata sent by the client that it does not understand»). Un documento CIMD se filtra por la intersección de `grant_types` y `response_types` con lo que el servidor admite, con los valores por defecto de RFC 7591 cuando faltan, y se rechaza si queda vacía (CIMD-03 §6: «MUST filter … find the intersection … MUST reject the request with invalid_client»).
- **D10. La caducidad: perezosa al leer, diaria al barrer.** Una app sin uso en 30 días no se encuentra (`find_client` la trata como inexistente); cada registro nuevo borra hasta 100 caducadas de la organización (`purge_idle`) antes de contar el techo de 200; y la retención diaria de la fase 2 barre los códigos (10 min después de caducar), las familias (un día después de terminar) y las apps sin uso, con sus familias en cascada (Task 13; B3-25). Ninguna tarea nueva del worker: el barrido es el de la fase 2.
- **D11. El consentimiento comprueba quién puede conectar antes de leer la petición.** El diseño pide que MCP siga apagado (§5.4, 1c(d)); la puerta lo comprueba en cada llamada, y comprobarlo aquí, primero, evita un código que no podría usarse y una descarga que no hacía falta (A3-31): la organización del despliegue, `mcp_enabled` y `mcp.use`. La página dice por qué y ofrece volver a la aplicación con `access_denied` (RFC 6749 §4.1.2.1). Por eso las dos rutas son «sin código de permiso» en `UNGATED_BY_DESIGN`, con `mcp.use` comprobado en el servicio, y no `require_permission("mcp.use")`, cuyo 403 no dejaría construir la vuelta al cliente.
- **D12. Revocar en Ajustes es por el id de la fila del cliente.** El `client_id` de un cliente CIMD es una URL con `/`, que no cabe en un segmento de ruta; la ruta pasa a `/mcp/connections/{app_id}/revoke` (UUID) y la lista da `id`, `name`, `host` y `kind`. Sin Keycloak, la revocación es una transacción, en `connections.py` (B3-31): la fila `mcp_revocation`, las familias de la app (de la persona o de todos) y `mcp.app_revoked`, juntas; `keycloak_failures` desaparece de la respuesta.
- **D13. Los errores RFC no se traducen.** Son para el desarrollador del cliente (RFC 6749 §5.2) y ASCII; viven como constantes `Problem` en `errors.py`, de modo que ninguna llamada lleva un literal en inglés y la prueba del catálogo (`tests/test_i18n_catalog.py`) no necesita excepciones línea a línea. Lo que ve una persona sí va por `tr()`; el código de un `NotRedirectable` va como argumento con nombre (`error=`), para que la guarda solo lea la frase.
- **D14. Un `kid` desconocido no hace leer OpenBao en cada petición.** `/mcp` es público; un token con `kid` inventado forzaría `keys.jwks(refresh=True)` por petición. `access.py` vuelve a leer las claves públicas como mucho una vez cada `REFRESH_EVERY_S` (5 s) por proceso, la defensa que `JwksCache` tenía para Keycloak (`security.py`, «minimum interval between refetches»); tras una rotación, un token con la clave nueva puede esperar hasta esos 5 s (A3-07). El token interno del asistente no cambia (solo lo presenta el propio proceso).
- **D15. `ITS_MCP_REGISTRATION=open` abre el registro dinámico, y nada más** (decisión del operador C2, 2026-11-06). La API lee `mcp_registration` (ajuste nuevo; `env_file: .env` ya lo carga en el contenedor `api`). Sin él, `POST /oauth/register` responde 403 `access_denied` y los metadatos no anuncian `registration_endpoint`; con él, sigue cerrado mientras MCP esté apagado. CIMD no depende de él: los metadatos anuncian `client_id_metadata_document_supported: true` siempre que hay emisor, y el consentimiento lee el documento siempre que MCP esté encendido. Antes de C2 la misma clave abría los dos.
- **D16. CIMD solo en el 443** (B3-17). La protección de salida no abre otro puerto para un documento que cualquiera nombra; CIMD-03 §3 lo permite («MAY contain a port»), y este servidor no lo acepta, decisión técnica escrita en el runbook.
- **D17. Un documento CIMD que se rechaza no termina las concesiones de su app** (re-revisión f3-2, M-1; de sus dos
  opciones, la segunda). CIMD-03 §6.1: «A terminal failure caused by a client_id mismatch SHOULD invalidate the
  client's outstanding refresh tokens immediately». El plan lo declina, y lo dice: un rechazo definitivo se levanta,
  no se guarda y **no borra** la copia guardada (Task 6, decisión 4), así que un fallo transitorio posterior la vuelve
  a servir. Dos razones. Terminar concesiones es de `grants.py` (Task 7), que corre en paralelo con la 6; hacerlo
  desde `resolve_client` pediría un motivo nuevo en `REVOKE_REASONS` y una escritura de concesiones en una lectura que
  hoy solo escribe la fila del cliente. Y una concesión no depende del documento: la persona aprobó un host, que el
  documento no puede cambiar (su `client_id` es su URL); la puerta exige en cada llamada que la concesión siga viva
  (D6), y Ajustes la revoca. Lo que el rechazo sí corta es lo que el documento decide: no se aprueba nada nuevo
  mientras el host responda con él.

### B.2 Decididas por una regla (revisión del 2026-11-06)

Cada fila: el elemento, la regla en que descansa (cita literal y dónde) y lo que el plan hace, con la tarea que lo hace. Las preguntas P1–P14 de la primera versión están entre ellas.

| # | Elemento | Regla | Decisión, y dónde |
|---|---|---|---|
| B3-01 | P1: cortar Keycloak en `/mcp` en esta fase | MCP 2026-07-28, Authorization: «MCP servers MUST NOT accept or transit any other tokens.» y «MCP servers MUST only accept tokens that are valid for use with their own resources.» | El corte, en la Task 9 (decisión 5). Una conexión hecha con Keycloak recibe un 401 y la persona quita y vuelve a añadir el conector; que la app vuelva a conectar sola está SIN VERIFICAR (A3-53). |
| B3-02 | P3: el límite de registros | RFC 7591 §3: «These requests MAY be rate-limited or otherwise limited to prevent a denial-of-service attack». Diseño de MCP §3.3: «Max Clients: 200 registered apps per realm.» `CLAUDE.md` 1c(d): «MCP access is off for an organization until an org-admin enables it.» | 20 por hora y red, IPv6 por /64, techo de 200 registros vivos y registro cerrado con MCP apagado (Tasks 1 y 6). La premisa de P3 era falsa (A3-71): con estos metadatos Claude usa CIMD y no se registra, y sus peticiones salen de los rangos de Anthropic (`160.79.104.0/21` y `153.61.208.0/20`), no de la oficina. |
| B3-03 | P4: las cotas de la caché CIMD | CIMD-03 §6.2: «SHOULD enforce a maximum effective cache duration regardless of the cache directives … SHOULD also enforce a minimum effective cache duration when none is otherwise indicated.» CIMD-00 §4.4: «MAY define its own upper and/or lower bounds». | `clamp(max-age o 0, 5 min, 24 h)` para todo documento válido; los errores y los inválidos no se guardan nunca (Task 3). |
| B3-04 | P5: una vida máxima absoluta del refresh | Diseño §5.3: «caduca a los 30 días sin uso». RFC 9700 §4.14.2: «Refresh tokens SHOULD expire if the client has been inactive for some time … The expiration time is at the discretion of the authorization server.» | Solo la inactividad (Task 7). |
| B3-05 | P6: una petición sin `resource` | RFC 8707 §2.1: «MAY process the request with no specific resource or by using a predefined default resource value.» MCP: «SHOULD accept uppercase scheme and host components». | Ausente = el recurso MCP, en la autorización y en el canje; esquema y host en cualquier caja (Tasks 7 y 8; A3-41). |
| B3-06 | P7: RS256 o ES256 | Diseño §5.3 y §6.3: ES256. RFC 9068 §2.1: «MUST include RS256» para quien declara conformidad. | Solo ES256, sin declarar conformidad con RFC 9068 (D5). |
| B3-07 | P8: qué actos cortan las conexiones de IA | Diseño §2.4: «Desactivar a una persona corta sus sesiones, sus tokens de IA y sus equipos recordados». RFC 9700 §4.14.2: «MAY revoke refresh tokens automatically in case of a security event, such as: password change or logout». | Los siete actos que olvidan los equipos recordados (Task 10). |
| B3-08 | P9: un registro que pide un método confidencial | Diseño §5.1: «`none` en `token_endpoint_auth_methods_supported`». RFC 7591 §3.2.1: «MAY reject or replace any of the client's requested metadata values»; §2: «If unspecified or omitted, the default is "client_secret_basic"». | Se registra como público y la respuesta lo dice (Task 6, D4). |
| B3-09 | P10: varias organizaciones | `CLAUDE.md`: «On-prem now, cloud-ready later.» | La organización del despliegue, que el consentimiento hace cumplir (Task 8; A3-43). |
| B3-10 | P11: el nombre que la app se pone | RFC 7591 §5: «the authorization server MUST treat all client metadata as self-asserted … can also present warning messages to end-users about dynamically registered clients in all cases». MCP: «MUST clearly display the redirect URI hostname during authorization». CIMD-03 §10.10: «could display only the hostname of the Client Identifier URL». Anthropic: «name the host of the client_id URL as the app asking for access, not the document's client_name». | Hosts solo ASCII (A3-01). La pantalla y la fila de Ajustes se titulan por el host (el del documento CIMD, o el del redirect); el nombre de la app va como afirmación suya, con la marca de «se registró sola» para un DCR (Tasks 4, 8, 10, 11; A3-62, A3-72: la premisa «el host es lo que la app no puede disfrazar» solo es cierta con A3-01). |
| B3-11 | P12: la sonda `jwks` dirá «degradado» | Diseño, cabecera: «No se despliega nada hasta terminar los cuatro». | Se acepta, con el hecho corregido: ya lo decía tras cada reinicio (Task 9, decisión 4; A3-69). |
| B3-12 | P13: el registro de Keycloak sigue abierto con la opción hasta la fase 4 | La regla del operador del 2026-10-31; la fase 4 retira Keycloak antes de cualquier despliegue (diseño §8). | Se acepta: el estado intermedio no llega a ningún host, y C2 no lo cambia (A3-73). El runbook lo dice (Task 12, riesgo 7). |
| B3-13 | D3: la lectura antes de conocer la organización | Diseño §6.2: «El único camino es una función `SECURITY DEFINER` … y queda en la lista revisada». | `lookup.org_of_slug` (0246), ningún motor de administración en `app/core/auth/oauth/` (Task 5; A3-25). |
| B3-14 | CIMD-00 o CIMD-03 | MCP cita la -00; la actual es la -03 (2026-11-02). `CLAUDE.md`: «A security invariant wins over convenience or speed, always.» | La -03 donde es más estricta y no cuesta nada: la intersección de tipos, las direcciones de uso especial, el origen propio, los transitorios (Tasks 3 y 6; A3-08, A3-09, A3-12, A3-14). |
| B3-15 | Volver al redirect automáticamente | RFC 9700 §4.11.2: «MUST take precautions to prevent these threats» y «SHOULD only automatically redirect the user agent if it trusts the redirection URI». MCP: «MUST take precautions to prevent redirecting user agents to untrusted URI's». | Solo la aprobación navega sola; los errores, el rechazo y el «no puedes» enseñan el host y un botón (Tasks 4 y 8; A3-19). |
| B3-16 | Clickjacking | RFC 9700 §4.16 y OAuth 2.1-17 §7.11: «Authorization servers MUST prevent clickjacking attacks.» OWASP: «frame-ancestors 'none'; This prevents any domain from framing the content.» | `frame-ancestors 'none'` y `X-Frame-Options: DENY` en cada `location` de nginx, con su prueba de despliegue, y la página se niega a pintarse enmarcada (Task 4; A3-18). |
| B3-17 | CIMD solo en el 443 | Decisión técnica (la protección de salida). | Se acepta y se documenta (D16). |
| B3-18 | Una URL de consola con ruta apaga MCP | RFC 8414 §2 y RFC 9207 §2 (D1). | Se acepta y el runbook lo dice (Task 12; A3-70). |
| B3-19 | `localhost` con cualquier puerto | Diseño §5.2; Anthropic: «Apply the same port-agnostic match to localhost so Claude Code works, even though RFC 8252 section 8.3 discourages localhost.» | Se acepta, como excepción escrita a RFC 8252 §8.3 (D8). |
| B3-20 | Un CIMD con `private_key_jwt` | Diseño §5.1 (`none`) y D4. | Se rechaza (Task 3). |
| B3-21 | Un canje fallido | RFC 6749 §10.5: «Authorization codes MUST be short lived and single-use. If the authorization server observes multiple attempts to exchange an authorization code for an access token, the authorization server SHOULD attempt to revoke all access tokens already granted». | Lo gasta y escribe un WARNING (Task 7, decisión 2; A3-34). |
| B3-22 | Qué se audita del servidor OAuth | Diseño §7: «consentimientos y revocaciones de clientes de IA». | Se audita `person_inactive` (`mcp.grant_ended`); el registro y el refresh no, y se dice por qué (Task 7, decisión 3; A3-39). |
| B3-23 | `redirect_uri` en el canje y en la autorización | MCP: «Authorization servers MUST implement OAuth 2.1». OAuth 2.1-17 §10.2: «A client following only the OAuth 2.1 recommendations will not send the redirect_uri in the token request»; §4.1.1: «OPTIONAL if only one redirect URI is registered for this client». | Opcional en el canje (idéntico si llega); opcional en la autorización con uno solo registrado (Tasks 7 y 8; A3-33, A3-46). |
| B3-24 | Sin límite en `/oauth/token` ni `/oauth/revoke` | RFC 7009 §5: «Appropriate countermeasures, which should be in place for the token endpoint as well, MUST be applied to the revocation endpoint». | 600 por minuto y red, para los dos, que falla en abierto (Task 7, decisión 8; A3-35). |
| B3-25 | Retención: 10 min para los códigos y 1 día para las familias | Decisión técnica (D10). | Se acepta, en la retención de la fase 2 (Task 13). |
| B3-26 | Sin CORS en los extremos de máquina | Diseño §6.4: «la plataforma no abre CORS [S63]». | Se acepta. Los clientes MCP que corren en un navegador quedan fuera; Claude y Claude Code no se ven afectados. |
| B3-27 | La detección de reutilización sin ventana de gracia | Diseño §5.3: «reutilizar uno ya usado revoca toda la familia». | Estricta (Task 7, decisión 7). El residuo va al runbook (Task 12, riesgo 9; A3-40). Si el operador quisiera reabrirlo: OAuth 2.1-17 §4.3.3 reconoce el fallo; Okta trae una gracia de 30 s por defecto y Auth0 la trae apagada (Apéndice C). |
| B3-28 | La lista de Ajustes solo mostraba apps que llamaron herramientas | `CLAUDE.md` 1c(d): «every registered AI client can be revoked, with effect on its next call». | Se unen las apps con familias vivas (Task 10, decisión 2; A3-55). |
| B3-29 | El uso por MCP no cuenta como entrada | Diseño §7: se auditan los «inicios de sesión correctos»; la aprobación (`mcp.app_authorized`) ya queda auditada. | Se acepta y se documenta (Task 9, decisión 1; Task 12, runbook). |
| B3-30 | Los actos de la cuenta cortaban concesiones sin dejar constancia | Diseño §7 (revocaciones de IA auditadas). | El recuento va en los metadatos del acto (`ai_grants_revoked`; Task 10, decisión 4; A3-59). |
| B3-31 | `oauth_session` llamado desde rutas | `CLAUDE.md` 5a: «Solo los procesos internos usan el alcance "todos", y cada uso está en una lista revisada.» | Una guarda (`OAUTH_SESSION_USERS`) y la lógica en el núcleo (`clients.register_new`, `connections.py`; Tasks 5, 6, 10; A3-26). |
| B3-32 | Un WARNING sin freno | Precedente C-M8 (loopback). | Frenado: uno por minuto como mucho (Task 3; A3-15). El del canje fallido no lo necesita: cada uno gasta un código (Task 7). |
| B3-33 | Los textos del consentimiento | Decisión de interfaz. | Cuánto dura una concesión y dónde se revoca; un 503 transitorio que se lee como transitorio; para un CIMD, «publicado por X, responde a Y» (Task 4; A3-24). |
| — | P2: la misma clave para el registro y CIMD | — | La decidió el operador: C2 (Global Constraints; D15). |
| — | P14: confirmar el segundo factor al aprobar | — | La decidió el operador: C1 (Global Constraints; Tasks 4 y 8). |

## Apéndice C — Fuentes leídas por este plan y su revisión (2026-11-06)

Además de [S1]–[S65] del dossier. Cada cita del plan está copiada del texto descargado ese día con `curl` del sitio del editor, o de la página convertida de HTML a texto; las de la revisión, de las copias del dossier de decisiones (`decisions-dossier.md`, sección «Fuentes»).

- [P-6749] RFC 6749, The OAuth 2.0 Authorization Framework. https://www.rfc-editor.org/rfc/rfc6749.txt — §3.1 y §3.2 («Parameters sent without a value MUST be treated as if they were omitted from the request. The authorization server MUST ignore unrecognized request parameters. Request and response parameters MUST NOT be included more than once.»); §3.1.2 («The endpoint URI MAY include an "application/x-www-form-urlencoded" formatted (per Appendix B) query component ([RFC3986] Section 3.4), which MUST be retained when adding additional query parameters. The endpoint URI MUST NOT include a fragment component.»); §3.3 (arriba, D7); §4.1.2 («If an authorization code is used more than once, the authorization server MUST deny the request and SHOULD revoke (when possible) all tokens previously issued based on that authorization code.»); §4.1.2.1 («If the request fails due to a missing, invalid, or mismatching redirection URI, or if the client identifier is missing or invalid, the authorization server SHOULD inform the resource owner of the error and MUST NOT automatically redirect the user-agent to the invalid redirection URI.»; los códigos `invalid_request`, `unauthorized_client`, `access_denied`, `unsupported_response_type`, `invalid_scope`, `server_error`, `temporarily_unavailable`); §4.1.3 (`redirect_uri` «REQUIRED, if the "redirect_uri" parameter was included in the authorization request … and their values MUST be identical»; `client_id` «REQUIRED, if the client is not authenticating with the authorization server»); §5.1 («The authorization server MUST include the HTTP "Cache-Control" response header field [RFC2616] with a value of "no-store" in any response containing tokens, credentials, or other sensitive information, as well as the "Pragma" response header field [RFC2616] with a value of "no-cache".»); §5.2 (`invalid_request`, `invalid_client` —«If the client attempted to authenticate via the "Authorization" request header field, the authorization server MUST respond with an HTTP 401 (Unauthorized) status code and include the "WWW-Authenticate" response header field»—, `invalid_grant`, `unsupported_grant_type`, y `error_description` limitado a `%x20-21 / %x23-5B / %x5D-7E`); §10.5 («Authorization codes MUST be short lived and single-use. If the authorization server observes multiple attempts to exchange an authorization code for an access token, the authorization server SHOULD attempt to revoke all access tokens already granted based on the compromised authorization code.»; B3-21).
- [P-7591] RFC 7591, Dynamic Client Registration. https://www.rfc-editor.org/rfc/rfc7591.txt — §2 (`token_endpoint_auth_method`: «"none": The client is a public client … If unspecified or omitted, the default is "client_secret_basic"»; «The authorization server MUST ignore any client metadata sent by the client that it does not understand»); §3.2 («an HTTP 201 Created status code and a body of type "application/json"»); §3.2.1 (`client_id` REQUIRED, `client_id_issued_at` OPTIONAL, «the authorization server MUST return all registered metadata about this client … MAY reject or replace any of the client's requested metadata values»); §3.2.2 (`invalid_redirect_uri`, `invalid_client_metadata`).
- [P-7636] RFC 7636, PKCE. https://www.rfc-editor.org/rfc/rfc7636.txt — §4.1 (verificador de 43 a 128 caracteres `[A-Z] / [a-z] / [0-9] / "-" / "." / "_" / "~"`); §4.2 (`code_challenge = BASE64URL-ENCODE(SHA256(ASCII(code_verifier)))`); §4.3 (`code_challenge_method` «OPTIONAL, defaults to "plain" if not present in the request»); §4.4.1 (sin `code_challenge`, «the authorization endpoint MUST return the authorization error response with the "error" value set to "invalid_request"»); §4.6 (verificador que no coincide: «an error response indicating "invalid_grant" … MUST be returned»).
- [P-7009] RFC 7009, Token Revocation. https://www.rfc-editor.org/rfc/rfc7009.txt — §2.1 («The authorization server first validates the client credentials (in case of a confidential client) and then verifies whether the token was issued to the client making the revocation request. If this validation fails, the request is refused and the client is informed of the error»); §2.2 («responds with HTTP status code 200 if the token has been revoked successfully or if the client submitted an invalid token»; «An invalid token type hint value is ignored»); §5 («Appropriate countermeasures, which should be in place for the token endpoint as well, MUST be applied to the revocation endpoint»; B3-24).
- [P-8414] RFC 8414, Authorization Server Metadata. https://www.rfc-editor.org/rfc/rfc8414.txt — §2: `issuer` REQUIRED; `authorization_endpoint`, `token_endpoint`; `jwks_uri` OPTIONAL, https; `registration_endpoint` OPTIONAL; `scopes_supported` RECOMMENDED; `response_types_supported` REQUIRED; `grant_types_supported`, `token_endpoint_auth_methods_supported` (por defecto `client_secret_basic`), `revocation_endpoint`, `revocation_endpoint_auth_methods_supported`, `code_challenge_methods_supported` OPTIONAL.
- [P-9207] RFC 9207, Authorization Server Issuer Identification. https://www.rfc-editor.org/rfc/rfc9207.txt — §2 («In authorization responses to the client, including error responses, an authorization server supporting this specification MUST indicate its identity by including the iss parameter in the response.»; «Its value MUST be a URL that uses the "https" scheme without any query or fragment components.»); §2.3 (`issuer` de los metadatos idéntico a `iss`; `authorization_response_iss_parameter_supported: true`).
- [P-9068] RFC 9068, JWT Profile for OAuth 2.0 Access Tokens. https://www.rfc-editor.org/rfc/rfc9068.txt — §2.1 («Authorization servers and resource servers conforming to this specification MUST include RS256 (as defined in [RFC7518]) among their supported signature algorithms.»; «the "typ" value used SHOULD be "at+jwt"»); §2.2 (`iss`, `exp`, `aud`, `sub`, `client_id`, `iat`, `jti` REQUIRED); §4 («The resource server MUST verify that the "typ" header value is "at+jwt" or "application/at+jwt" and reject tokens carrying any other value.»).
- [P-8252] RFC 8252, OAuth 2.0 for Native Apps. https://www.rfc-editor.org/rfc/rfc8252.txt — §7.3 (arriba, D8).
- [P-8707] RFC 8707, Resource Indicators. https://www.rfc-editor.org/rfc/rfc8707.txt — §2 («Its value MUST be an absolute URI … The URI MUST NOT include a fragment component.»; «Multiple "resource" parameters MAY be used»); **§2.1**, no §2 (A3-16): «If the client omits the "resource" parameter when requesting authorization, the authorization server MAY process the request with no specific resource or by using a predefined default resource value. Alternatively, the authorization server MAY require clients to specify the resource(s) they intend to access and MAY fail requests that omit the parameter with an "invalid_target" error.» (B3-05).
- [P-CIMD] draft-ietf-oauth-client-id-metadata-document-00 (la revisión que cita MCP [S27]). https://www.ietf.org/archive/id/draft-ietf-oauth-client-id-metadata-document-00.txt — §3 («Client identifier URLs MUST have an "https" scheme, MUST contain a path component, MUST NOT contain single-dot or double-dot path segments, MUST NOT contain a fragment component and MUST NOT contain a username or password Client identifier URLs SHOULD NOT include a query string component, and MAY contain a port.»); §4.1 (`client_id` que «MUST match the URL of the document using simple string comparison»; «the token_endpoint_auth_method property MUST NOT include client_secret_post, client_secret_basic, client_secret_jwt, or any other method based around a shared symmetric secret»; «the client_secret and client_secret_expires_at properties MUST NOT be used»); §4.3 («If fetching the metadata document fails, the authorization server SHOULD abort the authorization request.»); §4.4 («SHOULD respect HTTP cache headers [RFC9111] when caching client metadata, but MAY define its own upper and/or lower bounds»; «MUST NOT cache error responses … MUST NOT cache documents which are invalid or malformed»); §6.4 («The authorization server SHOULD display the hostname of the client_id on the authorization interface»); §6.6 («The recommended maximum response size for client metadata documents is 5 kilobytes.»).
- [P-CIMD3] draft-ietf-oauth-client-id-metadata-document-03, la revisión actual (2026-11-02, datatracker). https://www.ietf.org/archive/id/draft-ietf-oauth-client-id-metadata-document-03.txt — §3 (la URL «MAY contain a port», con una ruta distinta de `/`); §6 («The Authorization Server MUST filter the grant_types and response_types array-valued fields and find the intersection of what the Client ID Metadata Document declares and what the Authorization Servers supports. If the intersection of either of these two properties is empty, the Authorization Server MUST reject the request with invalid_client.»); §6.1 («A transient failure SHOULD NOT by itself invalidate a Client…»; «SHOULD NOT discard previously cached valid client metadata on transient failures»); §6.2 («SHOULD enforce a maximum effective cache duration regardless of the cache directives in the response … SHOULD also enforce a minimum effective cache duration when none is otherwise indicated»; «MAY implement rate limiting»); §10.1 (el origen propio); §10.7 («Authorization servers MUST NOT fetch a Client ID Metadata Document URL or any URLs contained within a Client ID Metadata Document that resolve to special-use IP addresses as defined in [RFC6890].»); §10.10 («could display only the hostname of the Client Identifier URL»). B3-14: se sigue donde es más estricta que la -00.
- [P-6890] RFC 6890, Special-Purpose IP Address Registries. https://www.rfc-editor.org/rfc/rfc6890.txt — los registros que `get_bounded` deja fuera además de los privados (Task 3; A3-09).
- [P-OAUTH21] draft-ietf-oauth-v2-1-17, la revisión actual (datatracker, versión 17). https://www.ietf.org/archive/id/draft-ietf-oauth-v2-1-17.txt — §2.3 («which MUST be retained when adding additional query parameters. The query string component MUST NOT include any parameters defined for use in the authorization response.»); §4.1.1 («"redirect_uri": OPTIONAL if only one redirect URI is registered for this client. REQUIRED if multiple redirect URIs are registered for this client.»); §4.3.3 («a failure to deliver the refresh token response … can leave the client holding only an invalidated refresh token»); §7.11 («Authorization servers MUST prevent clickjacking attacks.»); §7.13.2 (el servidor como redirector abierto); §10.2 («A client following only the OAuth 2.1 recommendations will not send the redirect_uri in the token request, and therefore will not be compatible with an authorization server that expects the parameter in the token request.»). La -13, que cita MCP (https://www.ietf.org/archive/id/draft-ietf-oauth-v2-1-13.txt), trae las mismas cláusulas.
- [P-9700] RFC 9700, OAuth 2.0 Security Best Current Practice. https://www.rfc-editor.org/rfc/rfc9700.txt — §4.11.2 («The authorization server MUST take precautions to prevent these threats.»; «SHOULD only automatically redirect the user agent if it trusts the redirection URI. If the URI is not trusted, the authorization server MAY inform the user and rely on the user to make the correct decision.»; «The attacker could, for example, register a client via dynamic client registration»); §4.14.2 (rotación; «MAY revoke refresh tokens automatically in case of a security event»; «SHOULD expire if the client has been inactive for some time»); §4.16 («Authorization servers MUST prevent clickjacking attacks.»). Ninguna guía de ventana de gracia (0 coincidencias de «grace»).
- [P-3986] RFC 3986, URI Generic Syntax. https://www.rfc-editor.org/rfc/rfc3986.txt — §2 («A URI is composed from a limited set of characters consisting of digits, letters, and a few graphic symbols.»; A3-01); §6.2.2.1 (el esquema y el host no distinguen mayúsculas; B3-05).
- [P-6265BIS] draft-ietf-httpbis-rfc6265bis-22. https://www.ietf.org/archive/id/draft-ietf-httpbis-rfc6265bis-22.txt — §5.2.1 (el *site for cookies* de un documento enmarcado por otro del mismo sitio): por qué `SameSite=Strict` no cubre el enmarcado (B3-16).
- [P-MCP] MCP 2026-07-28 (la versión actual, https://modelcontextprotocol.io/specification/versioning.md). https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization.md («Authorization servers MUST implement OAuth 2.1»; «implementations SHOULD accept uppercase scheme and host components for robustness and interoperability»; «MCP servers MUST NOT accept or transit any other tokens.»; «MCP servers MUST only accept tokens that are valid for use with their own resources.»); …/authorization/client-registration.md («Dynamic Client Registration is deprecated. New implementations should use Client ID Metadata Documents instead.»); …/authorization/security-considerations.md («MUST take precautions to prevent redirecting user agents to untrusted URI's»; «SHOULD display additional warnings for localhost-only redirect URIs»; «MUST clearly display the redirect URI hostname during authorization»).
- [P-ANTH] Anthropic. https://claude.com/docs/connectors/building/authentication.md («Claude refreshes tokens reactively on a `401` response, and proactively up to five minutes before the stored expiry», la frase entera, re-revisión f3-2, M-4; «up to 30 seconds for refresh token requests»; «If you rotate, return the new refresh token in the same response that invalidates the old one»; «Claude normally selects CIMD only when your authorization server metadata advertises both "client_id_metadata_document_supported": true and "none" in token_endpoint_auth_methods_supported»; «DCR causes Claude to register a new client on every fresh connection»; «Anthropic's outbound traffic to your server originates from 160.79.104.0/21»); https://claude.com/docs/connectors/building/lazy-authentication.md («name the host of the client_id URL as the app asking for access, not the document's client_name, because the document is self-asserted»); https://platform.claude.com/docs/en/api/ip-addresses.md («These are the stable IP addresses that Anthropic uses for outbound requests»: `160.79.104.0/21` **y** `153.61.208.0/20`, 6.144 direcciones IPv4; la página de conectores solo nombra el primero, y ninguna dice cuántas de ellas usa Anthropic de verdad, así que «pocas» no se afirma en ningún sitio del plan); https://claude.ai/oauth/claude-code-client-metadata (el documento de Claude Code: `redirect_uris` `http://localhost/callback` y `http://127.0.0.1/callback`, `token_endpoint_auth_method: "none"`, `cache-control: public, max-age=300`).
- [P-OWASP] OWASP Clickjacking Defense Cheat Sheet. https://cheatsheetseries.owasp.org/cheatsheets/Clickjacking_Defense_Cheat_Sheet.html — «Content-Security-Policy: frame-ancestors 'none'; This prevents any domain from framing the content. This setting is recommended unless a specific need has been identified for framing.»
- [P-GRACE] La gracia de la reutilización, para el operador si quisiera reabrir B3-27: Okta, https://developer.okta.com/docs/guides/refresh-tokens/main/ («The default number of seconds for the Grace period for token rotation is set to 30 seconds.»); Auth0, https://auth0.com/docs/secure/tokens/refresh-tokens/configure-refresh-token-rotation («Rotation Overlap Period … By default leeway is disabled.»).
- [P-C1] Para C1: NIST SP 800-63B-4, https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-63B-4.pdf, §5.1.1 («Cookies and similar "remember my browser" features SHALL NOT be used instead of authentication except as provided for reauthentication at AAL2»); Microsoft, https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/protect-against-consent-phishing («Consent phishing attacks trick users into granting permissions to malicious cloud applications.»); RFC 7591 §5 («a rogue client might use the name and logo of a legitimate client that it is trying to impersonate»).

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
- Modify: `backend/app/main.py` (junto al registro de `AuthRefusal`, `:312-314` hoy)
- Test: `backend/tests/test_oauth_primitives.py`

**Interfaces:**
- Consumes: `app.core.mcp_channel.mcp_resource(settings) -> str | None` (`backend/app/core/mcp_channel.py:60-62`);
  `starlette.responses.JSONResponse`.
- Produces: todo `policy.py`, `errors.py`, `redirects.py` y `pkce.py` del Apéndice A.1, con las constantes `Problem`
  que define el Step 3 (sus nombres los usan las Tasks 3, 6, 7 y 8).

**Decisión de la tarea (1): un `Problem` por error, constante.** D13: el cuerpo de RFC 6749 §5.2 va en inglés y ASCII,
y no pasa por `tr()`. Cada error es una constante `Problem(error, description, status)` del módulo, y una ruta lanza
`OAuthError(BAD_CODE)`: ninguna llamada lleva un literal en inglés, que es lo que `tests/test_i18n_catalog.py` busca en
las llamadas a excepciones. `OAuthError` comprueba el conjunto de caracteres de §5.2 al construirse, y
`test_every_problem_is_ascii_as_rfc_6749_allows` recorre todas las constantes: un `Problem` con una comilla o un acento
falla **en la suite**, no ante un cliente (un `NamedTuple` no se comprueba al importar).

**Decisión de la tarea (2): `issuer` exige https sin ruta.** D1 y B3-18. Un `ITS_PUBLIC_CONSOLE_URL` con ruta,
consulta, usuario o en http no nombra un emisor (RFC 8414 §2, RFC 9207 §2), y las rutas OAuth y `/mcp` responden 503
(Tasks 6, 7 y 9; el runbook lo dice, Task 12).

**Decisión de la tarea (3): un redirect es ASCII** (B3-10). RFC 6749 §3.1.2: «The redirection endpoint URI MUST be an
absolute URI as defined by [RFC3986] Section 4.3»; RFC 3986 §2: «A URI is composed from a limited set of characters
consisting of digits, letters, and a few graphic symbols». Un carácter por encima de 0x7E es `malformed`: así
`https://сlaude.ai/…` (con una «с» cirílica) no se registra ni se enseña como `claude.ai`, y una app con un dominio
internacional registra su forma `xn--…`, que es la que la persona ve.

**Decisión de la tarea (4): la consulta del redirect se conserva tal cual, y no lleva parámetros de la respuesta.**
OAuth 2.1-17 §2.3: «which MUST be retained when adding additional query parameters. The query string component MUST NOT
include any parameters defined for use in the authorization response.» `with_params` añade a la consulta cruda, sin
recodificarla, y un redirect registrado cuya consulta nombra `code`, `state`, `iss`, `error`, `error_description` o
`error_uri` es `query`.

**Decisión de la tarea (5): `localhost` en cualquier puerto, como excepción escrita** (B3-19). RFC 8252 §8.3: «the use
of localhost is NOT RECOMMENDED»; el diseño §5.2 lo pide («Claude Code usa `localhost`/`127.0.0.1` con cualquier
puerto») y Anthropic también: «Apply the same port-agnostic match to localhost so Claude Code works, even though RFC
8252 section 8.3 discourages localhost» [S33]. Lo único que se normaliza es el puerto: esquema, host, ruta y consulta se
comparan como cadenas (OAuth 2.1-17 §4.1.1: «simple string comparison … The only exception is loopback … variable port
numbers»).

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
- a redirect is ASCII (RFC 3986 §2), https or http to the machine itself
  (MCP [S30]), without a fragment (RFC 6749 §3.1.2) or a response parameter
  in its query (OAuth 2.1-17 §2.3), and matches exactly but for a loopback
  port (RFC 8252 §7.3; OAuth 2.1-17 §4.1.1);
- the answer adds to the redirect's own query without re-encoding it;
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
    ("value", "open_"),
    [("open", True), (" open ", True), ("OPEN", False), ("yes", False), ("", False)],
)
def test_registration_opens_only_on_the_exact_word(value: str, open_: bool) -> None:
    assert policy.registration_open(_settings("https://x.example", value)) is open_


def test_an_ipv6_address_is_counted_by_its_64() -> None:
    import ipaddress

    assert policy.network_of(ipaddress.ip_address("203.0.113.9")) == "ip:203.0.113.9"
    assert policy.network_of(ipaddress.ip_address("2001:db8:1:2::5")) == "net:2001:db8:1:2::/64"
    assert policy.network_of(ipaddress.ip_address("2001:db8:1:2:ffff::1")) == (
        "net:2001:db8:1:2::/64"
    )
    assert policy.network_of(None) == "ip:unknown"


def test_a_display_name_keeps_text_and_nothing_else() -> None:
    assert policy.display_name("  Cla\u0000ude\n  Desktop\t ") == "Claude Desktop"
    assert policy.display_name("\u0007\u0008") is None
    assert len(policy.display_name("x" * 500) or "") == policy.MAX_CLIENT_NAME


def test_the_numbers_are_the_designs() -> None:
    """§5.2: a code of 60 s; §5.3: an access token of 10 min, a refresh family
    and (§5.4) a registration that die after 30 days unused; B3-02: at most 200
    live registrations, 20 an hour from one network."""
    assert policy.CODE_TTL_S == 60
    assert policy.ACCESS_TOKEN_TTL_S == 600
    assert policy.REFRESH_IDLE.days == 30
    assert policy.CLIENT_IDLE.days == 30
    assert policy.SCOPE == "mcp"
    assert policy.MAX_REGISTRATIONS == 200
    assert policy.REGISTRATIONS_PER_HOUR == 20
    assert policy.IPV6_NETWORK_BITS == 64


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


def test_every_problem_is_ascii_as_rfc_6749_allows() -> None:
    problems = [v for v in vars(errors).values() if isinstance(v, Problem)]
    assert len(problems) >= 24
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
        "https://xn--bcher-kva.example/cb",
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
        # A Cyrillic "с" (U+0441): it looks like claude.ai and is not (RFC 3986 §2).
        ("https://сlaude.ai/api/mcp/auth_callback", "malformed"),
        ("https://bücher.example/cb", "malformed"),
        ("https://app.example/cb?code=evil", "query"),
        ("https://app.example/cb?a=1&state=x", "query"),
        ("https://app.example/cb?iss=https://evil.example", "query"),
        ("https://app.example/cb?error=x", "query"),
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
    assert redirects.matches(["http://[::1]/callback"], "http://[::1]:7/callback")
    assert not redirects.matches(["http://localhost/callback"], "http://127.0.0.1:51004/callback")
    assert not redirects.matches(["http://127.0.0.1:1/callback"], "http://127.0.0.1:2/other")
    assert not redirects.matches(["http://127.0.0.1:1/cb"], "http://127.0.0.1:2/cb?x=1")
    assert not redirects.matches(["http://127.0.0.1:1/cb"], "http://u@127.0.0.1:2/cb")
    # Only the port is normalised: the scheme and host are compared as written.
    assert not redirects.matches(["http://127.0.0.1/cb"], "HTTP://127.0.0.1:5/cb")
    assert not redirects.matches(["http://localhost/cb"], "http://LOCALHOST:5/cb")


def test_the_answer_adds_to_the_redirects_own_query_as_written() -> None:
    added = redirects.with_params("https://h.example/cb?x=1", {"code": "c d", "iss": "https://i"})
    assert added == "https://h.example/cb?x=1&code=c+d&iss=https%3A%2F%2Fi"
    # Retained, not re-encoded (OAuth 2.1-17 §2.3).
    kept = redirects.with_params("https://h.example/cb?a=b%20c&flag", {"code": "x"})
    assert kept == "https://h.example/cb?a=b%20c&flag&code=x"
    assert redirects.with_params("https://h.example/cb", {"code": "x"}) == (
        "https://h.example/cb?code=x"
    )


def test_the_host_shown_to_the_person() -> None:
    assert redirects.host_of("https://claude.ai/api/mcp/auth_callback") == "claude.ai"
    assert redirects.host_of("http://[::1]:5/cb") == "::1"
    assert redirects.is_loopback("http://localhost:7/cb")
    assert not redirects.is_loopback("https://localhost/cb")


@pytest.mark.parametrize(
    ("uri", "here"),
    [
        ("http://localhost:7/cb", True),
        ("https://localhost/cb", True),
        ("https://127.0.0.1/cb", True),
        ("http://[::1]/cb", True),
        # Any loopback address, and `localhost` with its root dot (re-review
        # f3-1, Minor 7): https to any host registers, so these can.
        ("https://127.0.0.2/cb", True),
        ("https://localhost./cb", True),
        ("https://claude.ai/cb", False),
        ("https://localhost.claude.ai/cb", False),
    ],
)
def test_an_answer_that_stays_on_this_computer_is_told_by_its_host(uri: str, here: bool) -> None:
    """MCP: "SHOULD display additional warnings for localhost-only redirect URIs"
    [S30], whatever the scheme, however the machine is spelled."""
    assert redirects.stays_on_this_computer(uri) is here


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

Every number is the design's, or a rule's (plan Appendix B):

- an access token lives 10 minutes (§5.3) and an authorization code 60
  seconds (§5.2);
- a refresh token's family dies after 30 days without use, and so does a
  registration nobody used (§5.3, §5.4; RFC 7591 lets a server "expire
  registrations for existing clients meeting certain criteria" [S19]);
- registration is limited (B3-02; RFC 7591 §3: "MAY be rate-limited or
  otherwise limited"): 20 an hour from one network, an IPv6 network being its
  /64, and at most 200 live registrations, the cap the Keycloak realm had
  (MCP design §3.3, "Max Clients: 200");
- the token and revocation endpoints are limited per network as well (B3-24;
  RFC 7009 §5), generously: hosted AI apps all call from their provider's
  addresses, and the limit is against floods, not guesses;
- the only scope is `mcp`, the one the protected-resource metadata has always
  named (`app/mcp/server.py`). `offline_access` is advertised beside it
  because Claude asks for a refresh token only when it is (plan D7, [S33]).

THE ISSUER IS THE CONSOLE'S ADDRESS (plan D1): `ITS_PUBLIC_CONSOLE_URL` with
no trailing slash, the same base `mcp_resource` puts `/mcp` after. RFC 8414
§2 wants "a URL that uses the "https" scheme and has no query or fragment
components", RFC 9207 §2 the same of `iss`; a console address with a path
would move the metadata to a path-inserted URL Traefik does not route. So an
address that is not https, has a path, a query, a fragment or a user name
names no issuer, and every OAuth route and `/mcp` answer 503 (B3-18).
"""

import ipaddress
from datetime import timedelta
from urllib.parse import urlsplit

ACCESS_TOKEN_TTL_S = 600
CODE_TTL_S = 60
REFRESH_IDLE = timedelta(days=30)
CLIENT_IDLE = timedelta(days=30)
SCOPE = "mcp"
SCOPES_SUPPORTED = (SCOPE, "offline_access")
GRANT_TYPES = ("authorization_code", "refresh_token")
RESPONSE_TYPES = ("code",)
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
#: Registrations one network may make per hour, and live registrations one
#: organization may hold (B3-02).
REGISTRATIONS_PER_HOUR = 20
MAX_REGISTRATIONS = 200
#: The prefix one IPv6 network is counted by (B3-02): a host owns its /64.
IPV6_NETWORK_BITS = 64
#: Requests to the token and revocation endpoints one network may make per
#: minute (B3-24).
TOKEN_REQUESTS_PER_MINUTE = 600
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
    """`ITS_MCP_REGISTRATION=open`, the exact word: the operator's opt-in to
    anonymous dynamic registration (`POST /oauth/register`), and to nothing
    else. A client metadata document needs no opt-in: it is read only while
    MCP is on, inside a signed-in person's consent (operator decision C2,
    2026-11-06; plan D15)."""
    return (getattr(settings, "mcp_registration", "") or "").strip() == "open"


def network_of(address: ipaddress.IPv4Address | ipaddress.IPv6Address | None) -> str:
    """The network a per-address limit counts (B3-02): an IPv4 address as
    itself, an IPv6 one by the /64 it belongs to -- a host owns its whole /64
    and could otherwise turn one bucket into 2^64 -- and "unknown" without
    one, the bucket every unplaceable request shares."""
    if address is None:
        return "ip:unknown"
    if address.version == 6:
        network = ipaddress.ip_network(f"{address}/{IPV6_NETWORK_BITS}", strict=False)
        return f"net:{network}"
    return f"ip:{address}"


def display_name(raw: str) -> str | None:
    """A client's name as a person may be shown it: printable text, runs of
    white space as one space, at most `MAX_CLIENT_NAME` characters; None when
    nothing is left. Still the app's own claim (RFC 7591 §5): the consent
    screen names the host first (plan B3-10)."""
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
literal. `OAuthError` refuses a description outside the set when it is built,
and `tests/test_oauth_primitives.py` builds every constant, so a bad one fails
in the suite, never in front of a client. What a PERSON reads (the consent
screen, `/mcp`, Settings) goes through `tr()` as always.
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
# reused for the refusals RFC 7591 gives no code for.
# --------------------------------------------------------------------------- #

UNAVAILABLE = Problem(
    "temporarily_unavailable",
    "the authorization server is not configured on this installation",
    503,
)
KEYS_UNAVAILABLE = Problem(
    "temporarily_unavailable",
    "the signing keys are not available right now; try again shortly",
    503,
)
REGISTRATION_CLOSED = Problem(
    "access_denied", "client registration is closed on this server", 403
)
REGISTRATIONS_FULL = Problem(
    "temporarily_unavailable",
    "this server holds as many client registrations as it allows; try again later",
    503,
)
SLOW_DOWN = Problem(
    "temporarily_unavailable", "too many registrations from this network; try again later", 429
)
BUSY = Problem(
    "temporarily_unavailable", "too many requests from this network; try again shortly", 429
)
TOO_LARGE = Problem("invalid_request", "the request body is too large", 413)
NOT_JSON = Problem("invalid_client_metadata", "the registration must be one JSON object")
NO_REDIRECTS = Problem(
    "invalid_redirect_uri", "redirect_uris must list between one and ten URIs"
)
BAD_REDIRECT = Problem(
    "invalid_redirect_uri",
    "each redirect URI must be ASCII, https or http to 127.0.0.1, [::1] or localhost, "
    "with no fragment and no authorization response parameter in its query",
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

- ASCII only: RFC 6749 §3.1.2 wants "an absolute URI as defined by [RFC3986]",
  and RFC 3986 §2 makes a URI of "a limited set of characters". So a host that
  only looks like another (`сlaude.ai`, with a Cyrillic "с") is not one the
  consent screen can show as `claude.ai` (plan B3-10);
- https to any host, or http to the machine itself. MCP: "All redirect URIs
  MUST be either localhost or use HTTPS" [S30];
- no fragment: RFC 6749 §3.1.2, "The endpoint URI MUST NOT include a fragment
  component";
- no response parameter in the query: OAuth 2.1-17 §2.3, "The query string
  component MUST NOT include any parameters defined for use in the
  authorization response";
- no user name or password, no `*`, no white space or control character, at
  most `MAX_URI_LENGTH` characters;
- compared EXACTLY, as strings, but for the port of a loopback redirect
  (OAuth 2.1-17 §4.1.1: "simple string comparison … The only exception is
  loopback … variable port numbers"; RFC 8252 §7.3: "MUST allow any port to be
  specified at the time of the request for loopback IP redirect URIs").
  `localhost`, `127.0.0.1` and `[::1]` are each matched as registered, never as
  one another, and `localhost` is allowed although RFC 8252 §8.3 calls it
  "NOT RECOMMENDED": Claude Code registers it (plan B3-19, [S33]).

The answer keeps the redirect's own query as written (OAuth 2.1-17 §2.3: "MUST
be retained when adding additional query parameters").
"""

import ipaddress
from collections.abc import Mapping, Sequence
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

from app.core.auth.oauth.policy import MAX_URI_LENGTH

#: `urlsplit` gives an IPv6 literal without its brackets.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
#: The parameters of an authorization response (RFC 6749 §4.1.2, §4.1.2.1;
#: RFC 9207 §2), which a registered redirect's query may not hold.
RESPONSE_PARAMETERS = frozenset(
    {"code", "state", "iss", "error", "error_description", "error_uri"}
)


class RedirectRejected(ValueError):
    """Why a redirect cannot be registered: `malformed`, `fragment`,
    `credentials`, `query` or `scheme`. A reason code, never shown to a
    person."""

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
    if any(ord(ch) <= 0x20 or ord(ch) > 0x7E for ch in uri) or "*" in uri:
        raise RedirectRejected("malformed")  # i18n-exempt: a reason code
    if "#" in uri:
        raise RedirectRejected("fragment")  # i18n-exempt: a reason code
    parts = _split(uri)
    if parts.username is not None or parts.password is not None:
        raise RedirectRejected("credentials")  # i18n-exempt: a reason code
    names = {name for name, _ in parse_qsl(parts.query, keep_blank_values=True)}
    if names & RESPONSE_PARAMETERS:
        raise RedirectRejected("query")  # i18n-exempt: a reason code
    host = parts.hostname or ""
    if parts.scheme == "https" and host:
        return uri
    if parts.scheme == "http" and host in LOOPBACK_HOSTS:
        return uri
    raise RedirectRejected("scheme")  # i18n-exempt: a reason code


def is_loopback(uri: str) -> bool:
    """http to `127.0.0.1`, `[::1]` or `localhost`: a redirect whose port may
    vary."""
    try:
        parts = _split(uri)
    except RedirectRejected:
        return False
    return parts.scheme == "http" and (parts.hostname or "") in LOOPBACK_HOSTS


def stays_on_this_computer(uri: str) -> bool:
    """The answer goes to the person's own machine, whatever the scheme and
    however it is spelled -- `localhost` (with or without its root dot) or
    any loopback address, `127.0.0.0/8` and `::1`: the consent screen's
    warning (MCP [S30]: "SHOULD display additional warnings for
    localhost-only redirect URIs")."""
    host = host_of(uri).rstrip(".")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _without_port(uri: str) -> tuple[str, str, str, str]:
    """Scheme and host AS WRITTEN (only the port goes), path and query."""
    parts = _split(uri)
    netloc = parts.netloc
    if parts.port is not None:
        netloc = netloc[: netloc.rfind(":")]
    return uri[: uri.index(":")], netloc, parts.path, parts.query


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
    shape = _without_port(requested)
    return any(is_loopback(uri) and _without_port(uri) == shape for uri in registered)


def host_of(uri: str) -> str:
    """The host name, as the consent screen shows it ("" if there is none).
    ASCII, because a registered redirect is (`check_registered`)."""
    try:
        return _split(uri).hostname or ""
    except RedirectRejected:
        return ""


def with_params(uri: str, params: Mapping[str, str]) -> str:
    """`uri` with `params` added after its own query, which is kept exactly as
    written."""
    parts = urlsplit(uri)
    added = urlencode(list(params.items()))
    query = f"{parts.query}&{added}" if parts.query else added
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
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

En `backend/app/main.py`, justo después de las tres líneas que registran `AuthRefusal` (`:312-314`):

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
  está en la rama**, esta tarea crea `policy.py` con el contenido exacto de la Task 1, Step 3, y lo incluye en su
  commit (el coordinador toma una copia al fusionar; las dos son idénticas). PyJWT 2.15.1: `jwt.get_unverified_header`, `jwt.decode(token, key=,
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

- [ ] **Step 0: El paquete y, si falta, `policy.py`** (re-revisión f3-1, Minor 9)

`backend/app/core/auth/oauth/__init__.py`: el contenido de la Task 1, Step 1. Si `backend/app/core/auth/oauth/policy.py`
no existe (la Task 1 aún no está en la rama), se crea con el contenido exacto del bloque `policy.py` de la Task 1,
Step 3. Sin este paso, el rojo del Step 1 sería `ModuleNotFoundError: No module named 'app.core.auth.oauth'` y no el
que espera.

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


@pytest.mark.parametrize(
    "missing", ["iss", "aud", "sub", "client_id", "org", "fam", "scope", "jti", "iat", "exp"]
)
async def test_every_claim_is_required(missing: str) -> None:
    claims = _base_claims()
    del claims[missing]
    assert await access.verify_access_token(await _signed(claims), settings=SETTINGS) is None


@pytest.mark.parametrize("typ", ["JWT", "loopback+jwt", "application/jwt", "at+JWT"])
async def test_another_typ_is_refused(typ: str) -> None:
    """RFC 9068 §4: "reject tokens carrying any other value"."""
    token = await _signed(_base_claims(), typ=typ)
    assert await access.verify_access_token(token, settings=SETTINGS) is None


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

The shape of RFC 9068 §2.2's claims, plus `org` (the door pins the tenant from it, as it
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
-- so a token signed just after a rotation is still admitted at once, unless
a stranger's token with an invented `kid` armed the interval first: then for
up to `REFRESH_EVERY_S` seconds. Acceptable, and said so (plan D14).

NOT A CLAIM OF RFC 9068 CONFORMANCE (plan B3-06). The token follows RFC
9068's `typ` and claims; RFC 9068 §2.1 also says "MUST include RS256" of
servers "conforming to this specification", and this one signs ES256 only
(design §5.3, §6.3). Its only resource server is this platform.
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
# Solo si la Task 1 no estaba en la rama y esta tarea creó su copia:
git add backend/app/core/auth/oauth/policy.py
git commit -m "feat(oauth): the MCP access token: ES256 at+jwt for the MCP resource, verified with everything fixed"
```

### Task 3: `egress.get_bounded` y el documento CIMD (`cimd.py`)

Diseño §5.5. Oleada 1, en paralelo con las Tasks 1, 2, 4 y 11. Solo backend, sin base de datos.

Un cliente CIMD se nombra con una URL https en vez de registrarse, y el servidor descarga el documento de esa URL
[S29]. Descargar una URL que elige un desconocido es un SSRF por construcción; el diseño exige hacerlo «por la
protección de salida existente (`app/core/egress.py`)». Hoy `egress.py` solo tiene `post_json` (POST, devuelve un
extracto de 500 caracteres imprimibles): esta tarea le añade `get_bounded` (GET, devuelve los bytes hasta un tope) con
las mismas cinco propiedades y una más, y escribe `cimd.py`, que valida el documento según la revisión actual del
borrador, -03 (B3-14), y MCP.

**Files:**
- Create: `backend/app/core/auth/oauth/__init__.py` (el mismo contenido que la Task 1, Step 1)
- Modify: `backend/app/core/egress.py` (al final: `EgressBody`, `_is_special_use` y `get_bounded`; nada de lo que hay
  cambia)
- Create: `backend/app/core/auth/oauth/cimd.py`
- Modify: `backend/tests/test_logging_discipline.py` (recuento y párrafo «NOW») y
  `docs/superpowers/specs/2026-08-14-logging-kernel-design.md` (§2.2, el párrafo «NOW»)
- Test: `backend/tests/test_egress_get_bounded.py`, `backend/tests/test_oauth_cimd.py`

**Interfaces:**
- Consumes: de `backend/app/core/egress.py`, `EgressBlocked(reason, message)`, `check_host(host, resolved, *,
  allowlisted) -> str`, `resolve(host, port) -> list[str]` (y su costura `_resolve_all`), `_platform_layer1_denies(host,
  port, allowlisted_hosts)`, `_platform_layer2_denies(addr, port)`, `_build_transport(pinned_ip)` (la costura que
  parchean las pruebas), `_ipv4_embedded_addresses(ip)`, `_CONNECT_TIMEOUT_S`, `_platform_addr_cache`. De
  `backend/app/core/auth/loopback.py` (fase 1), `RefusalThrottle` (el freno de C-M8). De la Task 1,
  `redirects.check_registered`, `RedirectRejected`, `policy.MAX_REDIRECT_URIS`, `policy.MAX_CLIENT_ID`,
  `policy.GRANT_TYPES`, `policy.RESPONSE_TYPES`, `policy.display_name`: **si la Task 1 aún no está en la rama**, esta
  tarea crea `policy.py` y `redirects.py` con el contenido exacto de la Task 1, Step 3, y los incluye en su commit
  (copias idénticas; el coordinador toma una).
- Produces: `EgressBody` (con `content_encoding`), `get_bounded`; `cimd.MAX_DOCUMENT_BYTES`, `FETCH_TIMEOUT_S`,
  `MIN_CACHE`, `MAX_CACHE`, `Reason`, `TRANSIENT`, `DocumentRefused` (con `transient`), `ClientDocument`,
  `is_document_client_id`, `on_origin`, `cache_lifetime`, `fetch_document` (Apéndice A.1).

**Decisión de la tarea (1): `get_bounded` no lanza razones nuevas.** `tests/test_notification_delivery.py` lee del
código cada razón de `EgressBlocked` y exige que sea terminal para los avisos. `get_bounded` solo usa las que ya
existen (`scheme_not_allowed`, `invalid_url`, `port_not_allowed`, `blocked_platform_host`, `dns_failure`,
`blocked_private`, `redirect_refused`); el tamaño, el tipo y la codificación los juzga quien lee (`truncated`,
`content_type`, `content_encoding`), aquí `cimd.py`.

**Decisión de la tarea (2): https, puerto 443 y ninguna lista blanca** (B3-17). Un documento CIMD lo nombra un
desconocido; nada que configure un administrador puede abrirle una dirección privada (`allowlisted=False` siempre).
CIMD-03 §3 permite un puerto («MAY contain a port»); este servidor solo descarga en el 443, como `post_json` para un
host que no está en la lista: un cliente con otro puerto no puede usar CIMD aquí. El runbook lo dice (Task 12).

**Decisión de la tarea (3): ninguna dirección de uso especial, aunque `post_json` las deje pasar** (B3-14). CIMD-03
§10.7: «Authorization servers MUST NOT fetch a Client ID Metadata Document URL or any URLs contained within a Client ID
Metadata Document that resolve to special-use IP addresses as defined in [RFC6890].» `_is_denied` deja pasar, por
ejemplo, `0.1.2.3`, `192.0.2.1`, `2001:db8::1` o `64:ff9b:1::a00:5` (el prefijo NAT64 de uso local de RFC 8215, que
`_NAT64_NET` no desenvuelve). `get_bounded`, y solo él, exige además que cada respuesta del DNS sea `is_global`, que lo
sea cada IPv4 que lleve dentro y que no esté en `64:ff9b:1::/48`. `post_json` no cambia.

**Decisión de la tarea (4): se mide lo que llega por el cable.** httpx pide `gzip, deflate` por defecto y
`aiter_bytes()` descomprime antes de que el tope de 5 KiB se mire: un trozo de 64 KiB puede crecer mil veces en
memoria. `get_bounded` manda `Accept-Encoding: identity`, cuenta con `aiter_raw()` y devuelve el `Content-Encoding`;
`cimd.py` rechaza cualquiera que no sea la identidad (`encoded`).

**Decisión de la tarea (5): un fallo transitorio no es un rechazo** (CIMD-03 §6.1: «A transient failure SHOULD NOT by
itself invalidate a Client»). Los motivos se separan: `unavailable` (tiempo agotado, error de red, 5xx o 429) es
transitorio, y también un nombre que no resolvió (`dns_failure` de la protección de salida: no se recuperó nada;
§6.1 pone «a network error or timeout» entre los transitorios); el resto (`blocked`, `not_ok`, …) es definitivo. `DocumentRefused.transient` lo dice; la Task 6 sigue
usando una copia guardada válida ante un fallo transitorio, y la pantalla de consentimiento (Task 4) dice «inténtalo
de nuevo» en vez de «no se puede usar».

**Decisión de la tarea (6): los tipos de concesión y de respuesta se cruzan** (CIMD-03 §6: «The Authorization Server
MUST filter the grant_types and response_types array-valued fields and find the intersection … If the intersection of
either of these two properties is empty, the Authorization Server MUST reject the request with invalid_client»). Sin
ellos valen los de RFC 7591 §2 (`["authorization_code"]`, `["code"]`); una intersección vacía, o una sin
`authorization_code`, es `unsupported`. La intersección decide si el documento sirve, no qué se concede: un documento
que declara solo `authorization_code` recibe igualmente un refresh token, porque este servidor lo emite siempre (D7:
el cliente «MUST NOT assume refresh tokens will be issued; the AS retains discretion» [S27]), y el refresh lo
gobiernan la rotación y la inactividad de la familia, no los metadatos que el cliente afirma de sí mismo (RFC 7591
§5). El documento de Claude Code declara los dos (re-revisión f3-1, Minor 8).

**Decisión de la tarea (7): un documento en el origen propio no se descarga** (CIMD-03 §10.1: «SHOULD reject a Client
Identifier URL that is equal … to its own issuer identifier»). `on_origin(client_id, issuer)` lo dice —el puerto 443
escrito y el punto final del nombre no lo disfrazan—, y la Task 6 lo rechaza antes de la descarga (`own_origin`).

**Decisión de la tarea (8): una línea de log por documento rechazado, con freno** (B3-32). Sin ella, una conexión que
falla porque el documento de un cliente no vale sería invisible para el operador; sin freno, cualquier persona con
sesión (o una a la que se atrae a un enlace) escribiría el log a ritmo de petición. Como el precedente C-M8 del token
interno (`loopback.RefusalThrottle`): como mucho una línea por minuto y proceso, con cuántas se callaron. Plantilla
fija, y en `extra` la razón, el host y `unlogged`; nunca la URL completa ni el cuerpo. **+1** al recuento del barrido
(108 → 109 sobre la base que ve esta tarea).

- [ ] **Step 1: El paquete**

`backend/app/core/auth/oauth/__init__.py`: el contenido de la Task 1, Step 1. Si `policy.py` o `redirects.py` no
existen en `backend/app/core/auth/oauth/` (la Task 1 aún no está en la rama), se crean con el contenido exacto de sus
bloques de la Task 1, Step 3.

- [ ] **Step 2: Las pruebas que fallan**

`backend/tests/test_egress_get_bounded.py`:

```python
"""`egress.get_bounded`: a GET under the egress guard's five properties, and
CIMD-03 §10.7's special-use addresses (autenticación propia plan 2026-11-06,
Task 3; design §5.5).

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


def _streamed(status: int, data: bytes = b"", **headers: str) -> httpx.Response:
    """An answer as a real transport gives one: a stream not yet read, so
    `aiter_raw` sees the bytes as they arrived (a `content=` response is read
    when it is built, and has no raw stream left)."""
    return httpx.Response(
        status,
        headers={k.replace("_", "-"): v for k, v in headers.items()},
        stream=httpx.ByteStream(data),
    )


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
        lambda r: _streamed(
            200,
            b'{"client_id": "x"}',
            Content_Type="application/json",
            Cache_Control="max-age=600",
        ),
    )
    body = await get_bounded(
        "https://client.example/oauth/metadata.json",
        max_bytes=5120,
        headers={"Accept": "application/json"},
    )
    assert body.status_code == 200
    assert body.content == b'{"client_id": "x"}'
    assert body.truncated is False
    assert body.content_type == "application/json"
    assert body.cache_control == "max-age=600"
    assert body.content_encoding == ""
    (request,) = seen
    assert request.method == "GET"
    assert request.url.host == PUBLIC  # pinned: the validated answer is the one dialled
    assert request.url.path == "/oauth/metadata.json"
    assert request.headers["host"] == "client.example"
    assert request.headers["accept"] == "application/json"
    # What is measured is what is sent: nothing compressed (Task 3, decision 4).
    assert request.headers["accept-encoding"] == "identity"


async def test_a_body_over_the_cap_is_cut_and_says_so(monkeypatch) -> None:
    _serve(monkeypatch, lambda r: _streamed(200, b"x" * 9000))
    body = await get_bounded("https://client.example/m.json", max_bytes=5120)
    assert body.truncated is True
    assert len(body.content) == 5120


async def test_a_compressed_body_is_counted_as_it_arrived_and_named(monkeypatch) -> None:
    import gzip

    packed = gzip.compress(b"x" * 100_000)
    _serve(
        monkeypatch,
        lambda r: _streamed(200, packed, Content_Encoding="gzip"),
    )
    body = await get_bounded("https://client.example/m.json", max_bytes=5120)
    assert body.content_encoding == "gzip"
    assert len(body.content) <= 5120
    assert body.content == packed[:5120]


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
async def test_what_the_guard_refuses_before_any_connection(
    monkeypatch, url: str, reason: str
) -> None:
    seen = _serve(monkeypatch, lambda r: httpx.Response(200))
    with pytest.raises(EgressBlocked) as refused:
        await get_bounded(url, max_bytes=10)
    assert refused.value.reason == reason
    assert seen == []


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.5",
        "127.0.0.1",
        "169.254.169.254",
        "::1",
        # CIMD-03 §10.7: RFC 6890's special-use addresses, which `post_json`'s
        # table lets through (Task 3, decision 3).
        "0.1.2.3",
        "192.0.2.1",
        "198.51.100.1",
        "203.0.113.1",
        "2001:db8::1",
        "2001:2::1",
        "2001:10::1",
        "100::1",
        "64:ff9b:1::a00:5",
        "64:ff9b::a00:5",
        # NAT64 of TEST-NET-1: `is_global` counts it as global; only the
        # embedded-IPv4 branch of `_is_special_use` refuses it (re-review
        # f3-1, Minor 2).
        "64:ff9b::c000:201",
    ],
)
async def test_a_special_use_answer_is_never_dialled(monkeypatch, address: str) -> None:
    seen = _serve(monkeypatch, lambda r: httpx.Response(200), resolved=[PUBLIC, address])
    with pytest.raises(EgressBlocked) as refused:
        await get_bounded("https://client.example/m.json", max_bytes=10)
    assert refused.value.reason == "blocked_private"
    assert seen == []


@pytest.mark.parametrize("address", ["2606:2800:220:1::1", "::ffff:93.184.216.34"])
async def test_a_global_answer_is_dialled(monkeypatch, address: str) -> None:
    seen = _serve(monkeypatch, lambda r: _streamed(200, b"{}"), resolved=[address])
    body = await get_bounded("https://client.example/m.json", max_bytes=10)
    assert body.status_code == 200
    assert len(seen) == 1


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
2026-11-06, Task 3; draft-ietf-oauth-client-id-metadata-document-03, the
current revision, where it is stricter than the -00 MCP cites [S29]).

`egress.get_bounded` is replaced by a fake that answers what each case needs:
the guard itself is `tests/test_egress_get_bounded.py`'s.
"""

import json
import logging
from datetime import timedelta
from typing import Any

import httpx
import pytest

from app.core import egress
from app.core.auth import loopback
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


@pytest.fixture(autouse=True)
def _fresh_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cimd, "_refusals", loopback.RefusalThrottle())


def _answer(
    monkeypatch: pytest.MonkeyPatch,
    document: object = GOOD,
    *,
    status: int = 200,
    truncated: bool = False,
    cache_control: str = "max-age=3600",
    encoding: str = "",
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
            content_encoding=encoding,
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
        # ASCII only, as a redirect (Task 1, decision 3): no lookalike host, and
        # no 500 from a Host header httpx cannot encode.
        ("https://сlaude.example/c.json", False),
        ("https://bücher.example/c.json", False),
        ("itms_0123456789abcdef0123456789abcd", False),
        ("https://claude.example/" + "a" * 300, False),
    ],
)
def test_which_client_ids_name_a_document(client_id: str, ok: bool) -> None:
    """CIMD-03 §3: https, a path other than "/", no dot segment, no fragment,
    no user name; a query is refused here (§3: "SHOULD NOT")."""
    assert cimd.is_document_client_id(client_id) is ok


def test_a_document_on_the_issuers_own_origin_is_known_as_such() -> None:
    """CIMD-03 §10.1: "SHOULD reject a Client Identifier URL that is equal …
    to its own issuer identifier"."""
    assert cimd.on_origin("https://itms.example.com/mcp", "https://itms.example.com")
    assert cimd.on_origin("https://ITMS.example.com/x.json", "https://itms.example.com")
    # The default port written out, and the root dot (re-review f3-1, Minor 3).
    assert cimd.on_origin("https://itms.example.com:443/x.json", "https://itms.example.com")
    assert cimd.on_origin("https://itms.example.com./x.json", "https://itms.example.com")
    assert not cimd.on_origin(URL, "https://itms.example.com")
    assert not cimd.on_origin("https://itms.example.com:8443/x", "https://itms.example.com")


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("max-age=3600", 3600),
        ("public, max-age=300", 300),  # Claude Code's own document
        ("public, max-age=60", 300),  # the 5-minute floor
        ("max-age=604800", 86400),  # the 24-hour ceiling
        ('max-age="900"', 900),
        ("max-age=0", 300),
        ("no-store", 300),
        ("no-cache, max-age=3600", 3600),
        ("", 300),
        ("max-age=abc", 300),
    ],
)
def test_a_valid_document_is_cached_within_bounds_whatever_its_headers(
    header: str, seconds: int
) -> None:
    """B3-03: `clamp(max-age or 0, 5 min, 24 h)` for every valid document
    (CIMD-03 §6.2: a maximum "regardless of the cache directives", a minimum
    "when none is otherwise indicated", and fetching "less often than
    requested by the cache headers" allowed)."""
    assert cimd.cache_lifetime(header) == timedelta(seconds=seconds)


async def test_a_valid_document_is_its_client(monkeypatch) -> None:
    asked = _answer(monkeypatch)
    document = await cimd.fetch_document(URL)
    assert asked == [URL]
    assert document.client_id == URL
    assert document.client_name == "Claude Code"
    assert document.redirect_uris == ("http://localhost/callback", "http://127.0.0.1/callback")
    assert document.cache_for == timedelta(hours=1)


async def test_without_grant_or_response_types_rfc_7591s_defaults_apply(monkeypatch) -> None:
    document = {k: v for k, v in GOOD.items() if k != "grant_types"}
    _answer(monkeypatch, document)
    assert (await cimd.fetch_document(URL)).client_id == URL


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
        ({"redirect_uris": ["https://сlaude.ai/cb"]}, "bad_redirect"),
        ({"token_endpoint_auth_method": "client_secret_basic"}, "secret"),
        ({"token_endpoint_auth_method": "private_key_jwt"}, "secret"),
        ({"client_secret": "s3cret"}, "secret"),
        ({"client_secret_expires_at": 0}, "secret"),
        ({"grant_types": ["client_credentials"]}, "unsupported"),
        ({"grant_types": ["refresh_token"]}, "unsupported"),
        ({"grant_types": "authorization_code"}, "incomplete"),
        ({"response_types": ["token"]}, "unsupported"),
    ],
)
async def test_a_document_this_server_cannot_use_is_refused(
    monkeypatch, changes: dict[str, Any], reason: str
) -> None:
    _answer(monkeypatch, {**GOOD, **changes})
    with pytest.raises(cimd.DocumentRefused) as refused:
        await cimd.fetch_document(URL)
    assert refused.value.reason == reason
    assert refused.value.transient is False


@pytest.mark.parametrize(
    ("kwargs", "reason", "transient"),
    [
        ({"status": 404}, "not_ok", False),
        ({"status": 410}, "not_ok", False),
        ({"status": 500}, "unavailable", True),
        ({"status": 503}, "unavailable", True),
        ({"status": 429}, "unavailable", True),
        ({"truncated": True}, "too_large", False),
        ({"encoding": "gzip"}, "encoded", False),
        ({"raw": b"<html>"}, "not_json", False),
        ({"raw": b"[1, 2]"}, "not_json", False),
        ({"raw": b"\xff\xfe"}, "not_json", False),
    ],
)
async def test_an_answer_that_is_not_a_document_is_refused(
    monkeypatch, kwargs: dict[str, Any], reason: str, transient: bool
) -> None:
    _answer(monkeypatch, **kwargs)
    with pytest.raises(cimd.DocumentRefused) as refused:
        await cimd.fetch_document(URL)
    assert refused.value.reason == reason
    assert refused.value.transient is transient


@pytest.mark.parametrize(
    ("guard", "reason", "transient"),
    [
        ("blocked_private", "blocked", False),
        ("port_not_allowed", "blocked", False),
        # A resolver that did not answer retrieved nothing (CIMD-03 §6.1).
        ("dns_failure", "unavailable", True),
    ],
)
async def test_what_the_guard_refuses_is_blocked_or_transient(
    monkeypatch, guard: str, reason: str, transient: bool
) -> None:
    async def blocked(url: str, **kwargs: Any) -> EgressBody:
        raise EgressBlocked(guard, "no")

    monkeypatch.setattr(egress, "get_bounded", blocked)
    with pytest.raises(cimd.DocumentRefused) as refused:
        await cimd.fetch_document(URL)
    assert (refused.value.reason, refused.value.transient) == (reason, transient)


@pytest.mark.parametrize("error", [TimeoutError(), httpx.ConnectError("down"), OSError("reset")])
async def test_a_network_failure_is_transient(monkeypatch, error: Exception) -> None:
    async def failing(url: str, **kwargs: Any) -> EgressBody:
        raise error

    monkeypatch.setattr(egress, "get_bounded", failing)
    with pytest.raises(cimd.DocumentRefused) as refused:
        await cimd.fetch_document(URL)
    assert (refused.value.reason, refused.value.transient) == ("unavailable", True)


async def test_a_client_id_that_names_no_document_is_never_fetched(monkeypatch) -> None:
    asked = _answer(monkeypatch)
    with pytest.raises(cimd.DocumentRefused) as refused:
        await cimd.fetch_document("http://claude.example/c.json")
    assert refused.value.reason == "not_a_document_url"
    assert asked == []


async def test_a_refusal_is_logged_with_the_reason_and_the_host_only(
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
    assert record.__dict__["unlogged"] == 0
    assert "s3cret" not in caplog.text
    assert URL not in caplog.text


async def test_refusals_are_logged_at_most_once_a_minute_with_a_count(
    monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """B3-32, as C-M8 for the internal token: anybody with a session can make
    the server read a document, so the line cannot be written at request rate."""
    clock = [1000.0]
    monkeypatch.setattr(loopback, "_monotonic", lambda: clock[0])
    _answer(monkeypatch, {**GOOD, "client_secret": "s3cret"})
    with caplog.at_level(logging.WARNING, logger="its.auth.oauth.cimd"):
        for _ in range(5):
            with pytest.raises(cimd.DocumentRefused):
                await cimd.fetch_document(URL)
        clock[0] += loopback.NETWORK_REFUSAL_EVERY_S
        with pytest.raises(cimd.DocumentRefused):
            await cimd.fetch_document(URL)
    records = [r for r in caplog.records if r.name == "its.auth.oauth.cimd"]
    assert [r.__dict__["unlogged"] for r in records] == [0, 4]
```

Run: `cd backend && .venv/bin/pytest -q tests/test_egress_get_bounded.py tests/test_oauth_cimd.py`
Expected: FAIL, `ImportError: cannot import name 'get_bounded' from 'app.core.egress'`.

- [ ] **Step 3: `get_bounded`**

Al final de `backend/app/core/egress.py`, sin tocar nada de lo que hay:

```python


# --------------------------------------------------------------------------- #
# Reading a document (own authentication phase 3, design §5.5)
# --------------------------------------------------------------------------- #

#: RFC 8215's local-use NAT64 prefix: on a site that uses it, `64:ff9b:1::a00:5`
#: is 10.0.0.5, and `_NAT64_NET` does not unwrap it.
_LOCAL_NAT64_NET = ipaddress.ip_network("64:ff9b:1::/48")


@dataclass(frozen=True)
class EgressBody:
    """A GET's answer, read no further than `max_bytes` off the wire
    (`get_bounded`). `truncated` says there was more; `content_encoding` is
    the answer's `Content-Encoding`, whose bytes `content` holds undecoded. The
    caller decides what each means."""

    status_code: int
    content: bytes
    truncated: bool
    content_type: str
    cache_control: str
    content_encoding: str = ""


def _is_special_use(ip_str: str) -> bool:
    """Not a global unicast address, nor carrying one inside (CIMD-03 §10.7,
    RFC 6890): `_is_denied` and then `is_global`, of the address and of every
    IPv4 it embeds, and never RFC 8215's local-use NAT64 prefix."""
    if _is_denied(ip_str):
        return True
    ip = ipaddress.ip_address(ip_str)
    if not ip.is_global:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        if ip in _LOCAL_NAT64_NET:
            return True
        return any(not embedded.is_global for embedded in _ipv4_embedded_addresses(ip))
    return False


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
    open a private address to a URL somebody else named. Every DNS answer must
    be a global address (`_is_special_use`; CIMD-03 §10.7). The platform's own
    hosts are refused by name and by address; the connection is pinned to the
    validated answer; no redirect; nothing compressed is asked for, and at most
    `max_bytes` are read off the wire, counted as they arrive (`aiter_raw`);
    one deadline for all of it, DNS included.

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
        resolved = await resolve(host, port)
        pinned = check_host(host, resolved, allowlisted=False)
        if any(_is_special_use(addr) for addr in resolved):
            raise EgressBlocked("blocked_private",  # i18n-exempt: machine code
                f"{host} resolves into special-use address space")
        if await _platform_layer2_denies(pinned, port):
            raise EgressBlocked("blocked_platform_host",  # i18n-exempt: machine code
                "refused: resolves to one of the platform's own hosts")
        pinned_netloc = f"[{pinned}]" if ":" in pinned else pinned
        pinned_url = parts._replace(netloc=f"{pinned_netloc}:{port}").geturl()
        request_headers = {**(headers or {}), "Host": host, "Accept-Encoding": "identity"}
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
                async for chunk in response.aiter_raw():
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
                    content_encoding=response.headers.get("content-encoding", "").strip().lower(),
                )

    return await asyncio.wait_for(_do_request(), timeout=timeout_s)
```

Y en los imports de `egress.py`: `from collections.abc import Mapping` (antes de `from dataclasses import dataclass`).

- [ ] **Step 4: `cimd.py`**

`backend/app/core/auth/oauth/cimd.py`:

```python
"""OAuth Client ID Metadata Documents (autenticación propia design §5.5; MCP
[S29]; draft-ietf-oauth-client-id-metadata-document-03, the current revision,
https://www.ietf.org/archive/id/draft-ietf-oauth-client-id-metadata-document-03.txt,
followed where it is stricter than the -00 MCP cites; plan B3-14).

A client that names itself by an https URL instead of registering. Accepted
whenever MCP is on (operator decision C2, 2026-11-06): no opt-in, because the
document is read only inside a signed-in person's consent (Task 8), through
`egress.get_bounded`, the SSRF guard (design §5.5; CIMD-03 §10.7). It is used
only when:

- the URL is a client identifier CIMD-03 §3 allows -- https, a path other than
  "/", no dot segment, no fragment, no user name or password -- with no query
  (§3: "SHOULD NOT") and in ASCII (as a redirect, Task 1);
- the answer is a 200 JSON object of at most 5 KiB, not compressed (§6.6;
  Task 3, decision 4);
- its `client_id` is the URL, exactly ("MUST match the URL of the document
  using simple string comparison"; MCP: "MUST validate" [S29]);
- it has `client_name` and `redirect_uris` (MCP: "MUST include at least the
  following properties: client_id, client_name, redirect_uris" [S29]), each
  redirect one this server would register (`redirects.check_registered`);
- its `grant_types` and `response_types`, RFC 7591's defaults when absent,
  meet this server's: CIMD-03 §6, "MUST filter … and find the intersection …
  If the intersection of either of these two properties is empty, the
  Authorization Server MUST reject the request with invalid_client". An
  intersection without `authorization_code` is refused too;
- it names no shared secret: `token_endpoint_auth_method` absent or `none`,
  no `client_secret`, no `client_secret_expires_at`. This server has public
  clients only (plan D4, B3-20), so `private_key_jwt` is refused too.

CACHED FOR `clamp(max-age or 0, 5 min, 24 h)`, WHATEVER ELSE THE HEADERS SAY
(plan B3-03; CIMD-03 §6.2: "SHOULD enforce a maximum effective cache duration
regardless of the cache directives … SHOULD also enforce a minimum effective
cache duration when none is otherwise indicated", and "MAY implement rate
limiting … even if that results in fetching the URL less often than requested
by the cache headers"). A refused document is never cached: the caller stores
only what `fetch_document` returned (`clients.resolve_client`, Task 6).

A TRANSIENT FAILURE IS NOT A REFUSAL (CIMD-03 §6.1: "A transient failure
SHOULD NOT by itself invalidate a Client"): `unavailable` -- a timeout, a
network error, a 5xx or a 429 -- is `transient`, and the caller keeps a valid
copy it already has. Everything else is for good.

A REFUSAL IS ONE LOG LINE, the reason and the host -- never the URL's path,
never the body -- at most one a minute per process, saying how many were held
(`loopback.RefusalThrottle`, the C-M8 precedent; plan B3-32).
"""

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from urllib.parse import SplitResult, unquote, urlsplit

import httpx

from app.core import egress
from app.core.auth.loopback import RefusalThrottle
from app.core.auth.oauth.policy import (
    GRANT_TYPES,
    MAX_CLIENT_ID,
    MAX_REDIRECT_URIS,
    RESPONSE_TYPES,
    display_name,
)
from app.core.auth.oauth.redirects import RedirectRejected, check_registered

logger = logging.getLogger("its.auth.oauth.cimd")

MAX_DOCUMENT_BYTES = 5 * 1024
FETCH_TIMEOUT_S = 5.0
MIN_CACHE = timedelta(minutes=5)
MAX_CACHE = timedelta(hours=24)

Reason = Literal[
    "not_a_document_url",
    "own_origin",
    "blocked",
    "unavailable",
    "not_ok",
    "too_large",
    "encoded",
    "not_json",
    "mismatch",
    "incomplete",
    "bad_redirect",
    "secret",
    "unsupported",
]
#: The reasons that say nothing about the document, only about now.
TRANSIENT: frozenset[Reason] = frozenset({"unavailable"})

#: Per process, as `loopback._network_refusals`.
_refusals = RefusalThrottle()


class DocumentRefused(Exception):
    """The document cannot be this client's. `reason` is a code, never shown;
    `transient` says a later attempt may succeed."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason: Reason = reason

    @property
    def transient(self) -> bool:
        return self.reason in TRANSIENT


@dataclass(frozen=True)
class ClientDocument:
    client_id: str
    client_name: str
    redirect_uris: tuple[str, ...]
    cache_for: timedelta


def is_document_client_id(client_id: str) -> bool:
    """CIMD-03 §3, in ASCII, and no query."""
    if not isinstance(client_id, str) or len(client_id) > MAX_CLIENT_ID:
        return False
    if any(ord(ch) <= 0x20 or ord(ch) > 0x7E for ch in client_id):
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


def on_origin(client_id: str, issuer: str) -> bool:
    """`client_id` is on the issuer's own origin (CIMD-03 §10.1): the MCP
    resource is, and so is anything else this server answers."""
    try:
        mine, theirs = urlsplit(issuer), urlsplit(client_id)
        return _origin(theirs) == _origin(mine)
    except ValueError:
        return False


def _origin(parts: SplitResult) -> tuple[str, str, int]:
    """Scheme, host without its root dot, and port with https's default
    filled in: `https://host:443/` and `https://host./` are `https://host/`."""
    return parts.scheme, (parts.hostname or "").rstrip("."), parts.port or 443


def cache_lifetime(cache_control: str) -> timedelta:
    """How long a valid document is reused (module docstring)."""
    seconds = 0
    for part in cache_control.split(","):
        name, _, value = part.strip().partition("=")
        if name.strip().lower() == "max-age":
            raw = value.strip().strip('"')
            seconds = int(raw) if raw.isdigit() else 0
    return min(max(timedelta(seconds=seconds), MIN_CACHE), MAX_CACHE)


def _types(document: dict[str, object], name: str, default: tuple[str, ...]) -> set[str]:
    value = document.get(name, list(default))
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DocumentRefused("incomplete")  # i18n-exempt: a reason code
    return set(value)


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
    except egress.EgressBlocked as exc:
        # A name that did not resolve retrieved nothing: transient (CIMD-03
        # §6.1, "a network error or timeout"); anything else the guard refused
        # is for good (re-review f3-1, Minor 1).
        reason: Reason = "unavailable" if exc.reason == "dns_failure" else "blocked"
        raise DocumentRefused(reason) from exc  # i18n-exempt: a reason code
    except (httpx.HTTPError, TimeoutError, OSError) as exc:
        raise DocumentRefused("unavailable") from exc  # i18n-exempt: a reason code
    if answer.status_code >= 500 or answer.status_code == 429:
        raise DocumentRefused("unavailable")  # i18n-exempt: a reason code
    if answer.status_code != 200:
        raise DocumentRefused("not_ok")  # i18n-exempt: a reason code
    if answer.truncated:
        raise DocumentRefused("too_large")  # i18n-exempt: a reason code
    if answer.content_encoding not in ("", "identity"):
        raise DocumentRefused("encoded")  # i18n-exempt: a reason code
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
    grants = _types(document, "grant_types", ("authorization_code",))
    responses = _types(document, "response_types", ("code",))
    if "authorization_code" not in grants & set(GRANT_TYPES) or not responses & set(
        RESPONSE_TYPES
    ):
        raise DocumentRefused("unsupported")  # i18n-exempt: a reason code
    return ClientDocument(
        client_id=client_id,
        client_name=shown,
        redirect_uris=checked,
        cache_for=cache_lifetime(answer.cache_control),
    )


async def fetch_document(client_id: str) -> ClientDocument:
    """The client `client_id` names, or `DocumentRefused` (logged, throttled)."""
    try:
        return await _read(client_id)
    except DocumentRefused as refused:
        unlogged = _refusals.admit()
        if unlogged is not None:
            try:
                host = urlsplit(client_id).hostname or ""
            except ValueError:
                host = ""
            logger.warning(
                "client metadata document refused",
                extra={"reason": refused.reason, "host": host, "unlogged": unlogged},
            )
        raise
```

- [ ] **Step 5: El recuento de logs**

**La base, la que tenga la worktree** (re-revisión f3-2, M-8). Sea N el número de `assert total == N` en
`backend/tests/test_logging_discipline.py` al empezar la tarea: 108 hoy, en la oleada 1, y otro si la rama ya trae la fase 2 (su Task 5
añade un sitio) o el orden de fusión fue otro. El párrafo nuevo va **encima del párrafo «NOW N» cuyo N es el del `assert`** (en `3a1ba1c`, «NOW 108, own authentication
phase 1, fix 3 review Minor 2», `:441` de la prueba y `:178` del diseño; no el «NOW 65» de `:334` ni el «NOW 70» de `:135`, que
son los de más arriba) del docstring de la prueba del recuento, que pasa a «BEFORE THAT, …» (sin cambiar nada más de él); el titular del nuevo y
el `assert` (y el «expected N» de su mensaje) dicen **N+1** (abajo, 109, con N = 108). Lo mismo en el §2.2 de
`docs/superpowers/specs/2026-08-14-logging-kernel-design.md`: el párrafo de abajo va encima del «**NOW N**» cuyo N es el
del `assert` (el mismo párrafo nombrado arriba), que pasa a «**BEFORE THAT, …**», con el mismo número en su titular. El delta de esta tarea es **+1**.

En el docstring de la prueba del recuento:

```text
    NOW 109, own authentication phase 3 (plan 2026-11-06, Task 3): one static
    WARNING in `app/core/auth/oauth/cimd.py::fetch_document`, "client metadata
    document refused", when an AI app's client metadata document cannot be its
    client (unreachable, not JSON, a mismatched client_id, a secret...). Without
    it a connection that fails on the document is invisible to the operator.
    At most one a minute per process, saying how many were held (the C-M8
    throttle, `loopback.RefusalThrottle`): any signed-in person can cause one.
    `extra` = the reason code, the document's host and `unlogged`; never the
    URL's path, never the body, which a stranger wrote.

```

En el §2.2 del diseño del kernel de logs:

```markdown
**NOW 109, own authentication phase 3 (plan 2026-11-06, Task 3).** One site. A
static WARNING in `app/core/auth/oauth/cimd.py::fetch_document`, "client
metadata document refused", when an AI app's client metadata document cannot
be its client -- unreachable, not JSON, a mismatched `client_id`, a shared
secret. Without it a connection that fails on the document is invisible to the
operator. At most one a minute per process, saying how many were held (the
C-M8 throttle, `loopback.RefusalThrottle`), because any signed-in person can
cause one. `extra` = the reason code, the document's host and `unlogged`; never
the URL's path, and never the body, which a stranger wrote.

```

- [ ] **Step 6: Las pruebas pasan**

Run: `cd backend && .venv/bin/pytest -q tests/test_egress_get_bounded.py tests/test_oauth_cimd.py tests/test_egress_guard.py tests/test_notification_delivery.py tests/test_logging_discipline.py tests/test_i18n_catalog.py tests/test_loopback.py`
Expected: PASS.

Run: `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `Success: no issues found` y `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/core/egress.py backend/app/core/auth/oauth/__init__.py backend/app/core/auth/oauth/cimd.py \
  backend/tests/test_egress_get_bounded.py backend/tests/test_oauth_cimd.py \
  backend/tests/test_logging_discipline.py docs/superpowers/specs/2026-08-14-logging-kernel-design.md
# Solo si la Task 1 no estaba en la rama y esta tarea creó sus copias:
git add backend/app/core/auth/oauth/policy.py backend/app/core/auth/oauth/redirects.py
git commit -m "feat(oauth): read client metadata documents through the egress guard (CIMD-03)"
```

### Task 4: Consola — la pantalla de consentimiento `/oauth/authorize`

Diseño §5.2; D2, D11; decisión del operador C1 (2026-11-06); B3-10, B3-15, B3-16, B3-33. Oleada 1, en paralelo con las
Tasks 1, 2, 3 y 11: se escribe contra el contrato del Apéndice A.2 y la Task 8 lo implementa. Consola y una línea de
`deploy/nginx.conf`.

Un cliente de IA manda a la persona a `<consola>/oauth/authorize?response_type=code&client_id=…&redirect_uri=…
&code_challenge=…&code_challenge_method=S256&state=…&resource=…`. Esa dirección es una página de la consola (D2: con la
cookie `SameSite=Strict`, una navegación que llega desde claude.ai no lleva la sesión hasta la API, y un `fetch` del
mismo origen sí). La página:
1. exige sesión (`RequireAuth`), sin el armazón de la consola: sin sesión, al inicio de sesión con `?next=` y la
   consulta entera, y de vuelta aquí;
2. manda la consulta, tal cual, a `POST /api/v1/oauth/authorize/details`, que la valida en el servidor;
3. **nombra primero el host**, que la app no puede disfrazar (el del redirect para una app registrada, el del
   `client_id` para un documento CIMD), y después el nombre que la app se dio, como afirmación suya; el host al que
   vuelve la respuesta; un aviso si solo vuelve a este equipo (MCP [S30]); qué concede, cuánto dura y dónde se
   termina; y con qué cuenta (B3-10, B3-33);
4. con «Aprobar» llama a `…/decision`, **pide el segundo factor si el servidor lo exige** (C1: 403
   `step_up_required`, `useStepUp`) y navega a la redirección, que lleva el código de autorización: es un secreto,
   así que la mutación es `useSecretMutation` y la página no la guarda en ningún sitio;
5. **no navega sola en ningún otro caso** (B3-15): un error de la petición, la negativa de la persona y el «no puedes
   conectar apps aquí» enseñan el host y esperan a que la persona pulse «Volver a <host>».

Y la página no se deja enmarcar (B3-16): `deploy/nginx.conf` manda `frame-ancestors 'none'` y `X-Frame-Options: DENY`
en cada respuesta de la consola, y la página, además, se niega a pintarse dentro de otra.

**Files:**
- Create: `frontend/src/modules/auth/consent.ts`
- Create: `frontend/src/modules/auth/ConsentPage.tsx`
- Modify: `frontend/src/modules/auth/types.ts` (tipos `ConsentClient`, `ConsentAnswer`, `ConsentDetails`, después de
  `RecoveryLink`)
- Modify: `frontend/src/lib/api.ts` (importa los tres tipos con los demás de `../modules/auth/types`; dos métodos
  después de `acceptRecovery`)
- Modify: `frontend/src/App.tsx` (`RequireAuth` gana `bare`; la ruta `/oauth/authorize` después de las públicas)
- Modify: `frontend/src/lib/session.ts` (`EDGE_PATHS`)
- Modify: `frontend/scripts/routes.mjs` (`EXCLUDED`), `frontend/src/test/routes-covered.test.ts` (su lista esperada)
- Modify: `frontend/src/i18n/en/auth.ts`, `frontend/src/i18n/es/auth.ts` (`consent`)
- Modify: `frontend/src/modules/auth/__tests__/harness.tsx` (siete métodos en `calls`, sus valores por defecto y la
  ruta)
- Modify: `frontend/src/test/secretMutations.test.ts` (`SECRET_METHODS`, `SECRET_SITES`)
- Modify: `frontend/src/lib/session.test.ts`
- Modify: `deploy/nginx.conf` (dos `add_header` en cada `location`)
- Test: `frontend/src/modules/auth/__tests__/consent.test.tsx`, `frontend/src/modules/auth/__tests__/consentGuard.test.tsx`,
  `frontend/src/modules/auth/__tests__/consentFramed.test.tsx`, `backend/tests/test_deploy_frame_headers.py`

**Interfaces:**
- Consumes: el contrato de `POST /api/v1/oauth/authorize/details` y `…/decision` (Apéndice A.2); de la consola,
  `ApiClient.request<T>(path, init, { global: true })` (como `getMySecurity`), `useSecretMutation`
  (`frontend/src/lib/secretMutation.ts:48`), `useStepUp` (`frontend/src/components/stepUp.tsx`: `run(action)` reintenta
  una vez tras un 403 `step_up_required` confirmado; la página pinta `stepUp.dialog`; el inventario de secretos ya
  reconoce `stepUp.run`, `src/test/secretMutations.test.ts:257`), `navigation.assign` (`frontend/src/lib/session.ts`),
  `AuthCard`, `AuthAlert` (`frontend/src/modules/auth/AuthCard.tsx`), `Btn`, `KIND_ICON` (`frontend/src/shell/components`),
  `Icon` (`frontend/src/shell/icons`), `describeSubmitError` (`frontend/src/components/form`), `ApiError`, `useT`.
- Produces: `ApiClient.oauthAuthorizeDetails(query)`, `ApiClient.oauthAuthorizeDecision(query, approve)`, los tipos
  `ConsentClient`, `ConsentAnswer`, `ConsentDetails`; `RequireAuth({ children, bare })`; `ConsentPage`;
  `authorizationQuery(search)`, `isFramed()`.

**Decisión de la tarea (1): la consulta viaja entera y sin interpretar.** La página no lee `client_id` ni
`redirect_uri`: manda `location.search` sin el `?`, y el servidor la valida (Task 8) cada vez, también al decidir. Así
la consola no tiene una segunda copia de las reglas, y una consulta manipulada se rechaza en el mismo sitio que una
legítima se acepta.

**Decisión de la tarea (2): `RequireAuth bare`.** El consentimiento es una tarjeta, como el inicio de sesión
(`AuthCard`), pero necesita sesión. `RequireAuth` con `bare` hace todo lo que hace hoy (sin sesión, al inicio de
sesión con `?next=`; una sesión que termina, oculta y vaciada) y, con sesión, pinta solo la página: sin barra lateral,
sin selector de cliente (una concesión de IA no es de ningún cliente).

**Decisión de la tarea (3): solo la aprobación navega sola** (B3-15). RFC 9700 §4.11.2 (y OAuth 2.1-17 §7.13.2)
describe a un atacante que registra un cliente con registro dinámico y manda una petición errónea a propósito, o
espera a que la persona rechace, para que el servidor lo devuelva a su sitio de phishing: «The authorization server
MUST take precautions to prevent these threats. … SHOULD only automatically redirect the user agent if it trusts the
redirection URI. If the URI is not trusted, the authorization server MAY inform the user and rely on the user to make
the correct decision.» MCP: «Authorization servers MUST take precautions to prevent redirecting user agents to
untrusted URI's». Con registro abierto ningún redirect es de confianza, así que un error, una negativa o un «no
puedes» enseñan el host y esperan el clic. La respuesta de error de RFC 6749 sigue llegando a la app: solo no llega
sola.

**Decisión de la tarea (4): la cabecera nombra el host, no el nombre** (B3-10). RFC 7591 §5: «the authorization server
MUST treat all client metadata as self-asserted … can also present warning messages to end-users about dynamically
registered clients in all cases»; MCP: «MUST clearly display the redirect URI hostname during authorization»; CIMD-03
§10.10: «could display only the hostname of the Client Identifier URL»; Anthropic: «name the host of the client_id URL
as the app asking for access, not the document's client_name, because the document is self-asserted» [S33]. El título
es «Conectar la aplicación de <host>»; el nombre que la app se dio va debajo, entre comillas, como suyo, y para una app
registrada por DCR la página dice que cualquiera puede registrar cualquier nombre. Los hosts son ASCII (Task 1,
decisión 3): lo que se ve es lo que el navegador usará.

**Decisión de la tarea (5): la confirmación reciente del segundo factor** (C1). El servidor la exige al aprobar (Task
8) y responde 403 `step_up_required`; la página manda la decisión por `stepUp.run`, que abre el diálogo de la fase 1 y
reintenta una vez. En un equipo recordado también: la sesión que empieza en uno no tiene `step_up_at`. Rechazar no la
pide.

**Decisión de la tarea (6): la página no se deja enmarcar** (B3-16; RFC 9700 §4.16 y OAuth 2.1-17 §7.11:
«Authorization servers MUST prevent clickjacking attacks»; OWASP: «Content-Security-Policy: frame-ancestors 'none';
This prevents any domain from framing the content»). La pantalla de consentimiento de Keycloak que sustituye no se
podía enmarcar; esta tampoco. `SameSite=Strict` no lo cubre: un documento enmarcado por una página del mismo sitio
(otro subdominio del dominio registrable) sigue mandando la cookie (RFC 6265bis-22 §5.2.1), y su `fetch` es del mismo
origen. En **cada** `location` de `deploy/nginx.conf` (nginx no hereda `add_header` en un `location` que declara los
suyos, y los dos declaran `Cache-Control`); la consola no se enmarca a sí misma en ninguna parte, así que `'none'` no
rompe nada y cubre también las páginas de inicio de sesión. Como respaldo para un navegador sin CSP (RFC 6819), la
página se niega a pintarse si `window.top !== window.self`.

Lo que las cabeceras no cubren, y queda escrito como riesgo (re-revisión f3-1, Minor 6; de sus dos opciones, la
segunda): el *DoubleClickjacking*. OWASP: «The target page is a top-level window, so framing restrictions such as
X-Frame-Options and CSP frame-ancestors do not prevent this attack». Una página de otro sitio abre la de
consentimiento bajo el segundo clic de un doble clic, y ese clic cae en «Aprobar». C1 lo cierra salvo en los diez
minutos que siguen a una confirmación del segundo factor (`sessions.py:342`); en esa ventana basta un clic. La
defensa que OWASP describe —el botón inactivo hasta que la página lleva un momento visible y con el foco— es, en sus
palabras, «a proposed supplementary mitigation», y aquí obligaría a esperar en cada prueba que aprueba; el plan no la
construye. La Task 12 lo escribe en el runbook (riesgo 10), con lo que lo acota: el host en el título, el correo
«Has conectado <host>» y la revocación en Ajustes.

**Decisión de la tarea (7): un fallo que pasa se lee como pasajero** (A3-24, B3-33). Si la pregunta de los detalles no
recibe respuesta, o recibe un 5xx o un 429 (la API reiniciando, la red, el límite), la tarjeta dice «Ahora mismo no se
puede comprobar esta solicitud» con «Intentarlo de nuevo», no «Esta solicitud de conexión no se puede usar»; lo mismo
cuando el servidor responde `temporarily_unavailable` a los detalles **o a la decisión** (re-revisión f3-1, Minor 4).
«Intentarlo de nuevo» descarta la respuesta de la decisión y vuelve a pedir los detalles, que el servidor valida otra
vez. Solo un rechazo de la petición en sí (un 4xx, o `refused` con otro `error`) se lee como definitivo.

- [ ] **Step 1: Las pruebas que fallan**

En `frontend/src/modules/auth/__tests__/harness.tsx`, añadir a `calls`:

```ts
  oauthAuthorizeDetails: vi.fn(),
  oauthAuthorizeDecision: vi.fn(),
  // The step-up dialog the consent page opens (C1): `components/stepUp.tsx`.
  getMySecurity: vi.fn(),
  stepUpTotp: vi.fn(),
  stepUpPasskeyOptions: vi.fn(),
  stepUpPasskey: vi.fn(),
  stepUpRecovery: vi.fn(),
```

al final de `installDefaults()`:

```ts
  calls.oauthAuthorizeDetails.mockResolvedValue(CONSENT);
  calls.oauthAuthorizeDecision.mockResolvedValue({ outcome: "redirect", redirect_to: APPROVED });
  calls.getMySecurity.mockResolvedValue({
    has_totp: true,
    passkeys: [],
    recovery_codes_left: 0,
    trusted_devices: [],
    sessions: [],
  });
  calls.stepUpTotp.mockResolvedValue(undefined);
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
 * has it, to the server, and shows what the server says. It names the host an
 * app cannot fake before the name it gave itself (B3-10), asks for the second
 * factor when the server wants it (C1), and leaves on its own ONLY with an
 * approval's code (B3-15): an error, a refusal or a decline waits for a click.
 * The approval's redirect carries the authorization code, which must not stay
 * in the mutation cache.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import { navigation } from "../../../lib/session";
import { APPROVED, CONSENT, authApi, calls, installDefaults, mutationCacheText, renderAuth } from "./harness";

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => authApi() }));

const QUERY =
  "response_type=code&client_id=itms_x&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback" +
  "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM&code_challenge_method=S256&state=st4te";
const PAGE = `/oauth/authorize?${QUERY}`;
const DENIED =
  "https://claude.ai/api/mcp/auth_callback?error=access_denied&state=st4te&iss=https%3A%2F%2Fitms.example";
const STEP_UP = () =>
  new ApiError(403, JSON.stringify({ detail: "confirm your second factor again to do this", code: "step_up_required" }));

let assign: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  installDefaults();
  assign = vi.spyOn(navigation, "assign").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("approving an AI app", () => {
  it("names the host first, the app's own name as its claim, its return and the account", async () => {
    renderAuth(PAGE);
    expect(await screen.findByRole("heading", { name: "Connect the app at claude.ai" })).toBeInTheDocument();
    expect(calls.oauthAuthorizeDetails).toHaveBeenCalledWith(QUERY);
    expect(screen.getByText("It calls itself “Claude”.")).toBeInTheDocument();
    expect(
      screen.getByText("It registered itself here, and any app can register with any name: trust the address, not the name."),
    ).toBeInTheDocument();
    expect(screen.getByText("Your answer goes back to claude.ai.")).toBeInTheDocument();
    expect(screen.getByText("You are signed in as dana@example.test.")).toBeInTheDocument();
    expect(
      screen.getByText(
        "If you approve, it can act as you in ITMS: everything your role allows, including running actions on managed computers.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "The connection lasts while the app uses it and ends after 30 days unused. You can end it at any time in Settings → AI connections.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(`App id: ${CONSENT.client.client_id}`)).toBeInTheDocument();
    // A registered app publishes no document, and returns to an https site.
    expect(screen.queryByText(/Its description is published by/)).toBeNull();
    expect(screen.queryByText(/only answers on this computer/)).toBeNull();
  });

  it("names the host that publishes a client metadata document, and where it answers", async () => {
    calls.oauthAuthorizeDetails.mockResolvedValue({
      ...CONSENT,
      client: {
        ...CONSENT.client,
        kind: "metadata_document",
        client_id: "https://claude.example/m.json",
        client_host: "claude.example",
        redirect_host: "answers.example",
      },
    });
    renderAuth(PAGE);
    expect(await screen.findByRole("heading", { name: "Connect the app at claude.example" })).toBeInTheDocument();
    expect(screen.getByText("Its description is published by claude.example.")).toBeInTheDocument();
    expect(screen.getByText("Your answer goes back to answers.example.")).toBeInTheDocument();
    expect(screen.queryByText(/registered itself here/)).toBeNull();
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

  it("says when the app gave no name", async () => {
    calls.oauthAuthorizeDetails.mockResolvedValue({ ...CONSENT, client: { ...CONSENT.client, name: null } });
    renderAuth(PAGE);
    expect(await screen.findByText("It gave no name.")).toBeInTheDocument();
  });

  it("approves, goes where the server says, and keeps no code in the mutation cache", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderAuth(PAGE);
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith(APPROVED));
    expect(calls.oauthAuthorizeDecision).toHaveBeenCalledWith(QUERY, true);
    expect(mutationCacheText(queryClient)).not.toContain("c0de-never-kept");
  });

  it("asks for the second factor when the server wants it, then sends the approval once more", async () => {
    calls.oauthAuthorizeDecision
      .mockRejectedValueOnce(STEP_UP())
      .mockResolvedValueOnce({ outcome: "redirect", redirect_to: APPROVED });
    const user = userEvent.setup();
    renderAuth(PAGE);
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm it's you" });
    expect(assign).not.toHaveBeenCalled();
    await user.type(within(dialog).getByLabelText("Code from your authenticator app"), "123456");
    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith(APPROVED));
    expect(calls.stepUpTotp).toHaveBeenCalledWith("123456");
    expect(calls.oauthAuthorizeDecision).toHaveBeenCalledTimes(2);
  });

  it("closing the confirmation approves nothing and goes nowhere", async () => {
    calls.oauthAuthorizeDecision.mockRejectedValueOnce(STEP_UP());
    const user = userEvent.setup();
    renderAuth(PAGE);
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(calls.oauthAuthorizeDecision).toHaveBeenCalledTimes(1);
    expect(assign).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
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

describe("everything else waits for the person (B3-15)", () => {
  it("a decline shows where it goes and leaves only when asked", async () => {
    calls.oauthAuthorizeDecision.mockResolvedValue({
      outcome: "back",
      reason: "declined",
      detail: "You declined. The app will be told.",
      redirect_host: "claude.ai",
      redirect_to: DENIED,
    });
    const user = userEvent.setup();
    renderAuth(PAGE);
    await user.click(await screen.findByRole("button", { name: "Decline" }));
    expect(await screen.findByRole("heading", { name: "You did not connect this app" })).toBeInTheDocument();
    expect(calls.oauthAuthorizeDecision).toHaveBeenCalledWith(QUERY, false);
    expect(assign).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Back to claude.ai" }));
    expect(assign).toHaveBeenCalledWith(DENIED);
  });

  it("an error the app should hear is shown with its host, and never followed on its own", async () => {
    calls.oauthAuthorizeDetails.mockResolvedValue({
      outcome: "back",
      reason: "error",
      detail: "This AI app's request is malformed.",
      redirect_host: "claude.ai",
      redirect_to: DENIED,
    });
    const user = userEvent.setup();
    renderAuth(PAGE);
    expect(await screen.findByRole("heading", { name: "This app's request cannot be used" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("This AI app's request is malformed.");
    // A beat later, still here: nothing navigated by itself.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(assign).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Back to claude.ai" }));
    expect(assign).toHaveBeenCalledTimes(1);
    expect(assign).toHaveBeenCalledWith(DENIED);
  });

  it("says why the person cannot connect AI apps, and goes back only when asked", async () => {
    calls.oauthAuthorizeDetails.mockResolvedValue({
      outcome: "back",
      reason: "denied",
      detail: "requires mcp.use",
      redirect_host: "claude.ai",
      redirect_to: DENIED,
    });
    const user = userEvent.setup();
    renderAuth(PAGE);
    expect(await screen.findByRole("heading", { name: "You cannot connect AI apps here" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("requires mcp.use");
    expect(assign).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Back to claude.ai" }));
    expect(assign).toHaveBeenCalledWith(DENIED);
  });

  it("a bad client or redirect is shown, with no way back to an address it does not trust", async () => {
    calls.oauthAuthorizeDetails.mockResolvedValue({
      outcome: "refused",
      error: "invalid_redirect_uri",
      detail: "The address this AI app asked to go back to is not one it registered.",
    });
    renderAuth(PAGE);
    expect(await screen.findByRole("heading", { name: "This connection request cannot be used" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("The address this AI app asked to go back to is not one it registered.");
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Back to/ })).toBeNull();
    expect(assign).not.toHaveBeenCalled();
  });

  it("a passing failure says to try again, and trying again asks once more", async () => {
    calls.oauthAuthorizeDetails
      .mockResolvedValueOnce({
        outcome: "refused",
        error: "temporarily_unavailable",
        detail: "This AI app's description could not be read just now.",
      })
      .mockResolvedValueOnce(CONSENT);
    const user = userEvent.setup();
    renderAuth(PAGE);
    expect(await screen.findByRole("heading", { name: "This request cannot be checked right now" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { name: "Connect the app at claude.ai" })).toBeInTheDocument();
    expect(calls.oauthAuthorizeDetails).toHaveBeenCalledTimes(2);
  });

  // A3-24: the server not answering is not a verdict on the request.
  it.each([
    ["a 5xx", new ApiError(503, JSON.stringify({ detail: "unavailable" }))],
    ["no answer at all", new TypeError("Failed to fetch")],
  ])("a details request that got %s says to try again", async (_what, failure) => {
    calls.oauthAuthorizeDetails.mockRejectedValueOnce(failure).mockResolvedValueOnce(CONSENT);
    const user = userEvent.setup();
    renderAuth(PAGE);
    expect(await screen.findByRole("heading", { name: "This request cannot be checked right now" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { name: "Connect the app at claude.ai" })).toBeInTheDocument();
    expect(calls.oauthAuthorizeDetails).toHaveBeenCalledTimes(2);
  });

  it("a details request the server refused is for good, with nothing to try again", async () => {
    calls.oauthAuthorizeDetails.mockRejectedValueOnce(
      new ApiError(422, JSON.stringify({ detail: "the request is not valid" })),
    );
    renderAuth(PAGE);
    expect(await screen.findByRole("heading", { name: "This connection request cannot be used" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("a decision that could not be checked just now offers to ask again", async () => {
    // Re-review f3-1, Minor 4: the transient answer of the decision too.
    calls.oauthAuthorizeDecision.mockResolvedValueOnce({
      outcome: "refused",
      error: "temporarily_unavailable",
      detail: "This AI app's description could not be read just now.",
    });
    const user = userEvent.setup();
    renderAuth(PAGE);
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("heading", { name: "This request cannot be checked right now" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { name: "Connect the app at claude.ai" })).toBeInTheDocument();
    expect(calls.oauthAuthorizeDetails).toHaveBeenCalledTimes(2);
    expect(assign).not.toHaveBeenCalled();
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

`frontend/src/modules/auth/__tests__/consentFramed.test.tsx`:

```tsx
/**
 * The consent page refuses to draw inside another page (B3-16): the
 * frame-busting fallback of RFC 6819 for a browser without CSP. The server's
 * `frame-ancestors 'none'` is `backend/tests/test_deploy_frame_headers.py`'s.
 */
import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authApi, calls, installDefaults, renderAuth } from "./harness";

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => authApi() }));
vi.mock("../consent", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../consent")>()),
  isFramed: () => true,
}));

beforeEach(() => {
  installDefaults();
});

describe("the consent page inside a frame", () => {
  it("draws no request and asks the server nothing", async () => {
    renderAuth("/oauth/authorize?client_id=itms_x");
    expect(await screen.findByRole("heading", { name: "Open this page on its own" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(calls.oauthAuthorizeDetails).not.toHaveBeenCalled();
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

`backend/tests/test_deploy_frame_headers.py`:

```python
"""The console cannot be framed (plan 2026-11-06, Task 4, decision 6; B3-16).

RFC 9700 §4.16 and OAuth 2.1-17 §7.11: "Authorization servers MUST prevent
clickjacking attacks." The consent page at `/oauth/authorize` is a console
page that nginx serves (`deploy/nginx.conf`), so the headers are nginx's. nginx
does NOT inherit `add_header` into a `location` that declares its own, so every
`location` must carry both.
"""

import re
from pathlib import Path

NGINX = Path(__file__).resolve().parents[2] / "deploy" / "nginx.conf"
CSP = """add_header Content-Security-Policy "frame-ancestors 'none'" always;"""
XFO = 'add_header X-Frame-Options "DENY" always;'


def _locations(text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for match in re.finditer(r"location\s+(\S+)\s*\{", text):
        depth, end = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(text[end], 0)
            end += 1
        blocks[match.group(1)] = text[match.end() : end - 1]
    return blocks


def test_the_parser_sees_both_locations() -> None:
    assert set(_locations(NGINX.read_text())) == {"/assets/", "/"}


def test_every_location_refuses_to_be_framed() -> None:
    for path, body in _locations(NGINX.read_text()).items():
        assert CSP in body, path
        assert XFO in body, path
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

En `frontend/src/test/routes-covered.test.ts`, la lista esperada de `excludes exactly the routes the gate cannot or
must not walk` pasa a:

```ts
    // `/oauth/authorize` (plan 2026-11-06, Task 4) needs an AI app's
    // authorization request, which the gate does not have.
    expect([...EXCLUDED].sort()).toEqual([
      "*",
      "/__preview",
      "/oauth/authorize",
      "/settings/email-intake/oauth/callback",
    ]);
```

Run: `cd frontend && npx vitest run src/modules/auth src/lib/session.test.ts src/test/routes-covered.test.ts`
Expected: FAIL (`Cannot find module '../ConsentPage'`; `RequireAuth` no acepta `bare`; `safeNext("/oauth/token")` devuelve
`/oauth/token`; `EXCLUDED` sin `/oauth/authorize`).

Run: `cd backend && .venv/bin/pytest -q tests/test_deploy_frame_headers.py`
Expected: FAIL en `test_every_location_refuses_to_be_framed` (`/assets/`).

- [ ] **Step 2: Los textos**

En `frontend/src/i18n/en/auth.ts`, una clave nueva al final de `auth` (después de `passkey`):

```ts
  consent: {
    loading: "Checking the request…",
    title: "Connect the app at {host}",
    intro: "It asks to use ITMS as you.",
    calledItself: "It calls itself “{name}”.",
    unnamed: "It gave no name.",
    selfRegistered: "It registered itself here, and any app can register with any name: trust the address, not the name.",
    grants:
      "If you approve, it can act as you in ITMS: everything your role allows, including running actions on managed computers.",
    lasts:
      "The connection lasts while the app uses it and ends after 30 days unused. You can end it at any time in Settings → AI connections.",
    account: "You are signed in as {email}.",
    returnsTo: "Your answer goes back to {host}.",
    publishedBy: "Its description is published by {host}.",
    appId: "App id: {id}",
    loopbackOnly:
      "This app only answers on this computer (localhost). Approve it only if you started it yourself, on this computer, just now.",
    approve: "Approve",
    deny: "Decline",
    refusedTitle: "This connection request cannot be used",
    unavailableTitle: "This request cannot be checked right now",
    retry: "Try again",
    deniedTitle: "You cannot connect AI apps here",
    errorTitle: "This app's request cannot be used",
    declinedTitle: "You did not connect this app",
    backTo: "Back to {host}",
    framedTitle: "Open this page on its own",
    framed: "This page approves AI apps, so it never works inside another page. Open the link directly.",
  },
```

En `frontend/src/i18n/es/auth.ts`, en el mismo sitio:

```ts
  consent: {
    loading: "Comprobando la solicitud…",
    title: "Conectar la aplicación de {host}",
    intro: "Pide usar ITMS en tu nombre.",
    calledItself: "Dice llamarse «{name}».",
    unnamed: "No ha dado ningún nombre.",
    selfRegistered:
      "Se registró ella misma aquí, y cualquier aplicación puede registrarse con cualquier nombre: fíate de la dirección, no del nombre.",
    grants:
      "Si la apruebas, podrá actuar como tú en ITMS: todo lo que permite tu rol, incluido ejecutar acciones en equipos gestionados.",
    lasts:
      "La conexión dura mientras la aplicación la use y termina tras 30 días sin uso. Puedes terminarla cuando quieras en Configuración → Conexiones de IA.",
    account: "Has iniciado sesión como {email}.",
    returnsTo: "Tu respuesta vuelve a {host}.",
    publishedBy: "Su descripción la publica {host}.",
    appId: "Id de la aplicación: {id}",
    loopbackOnly:
      "Esta aplicación solo responde en este equipo (localhost). Apruébala solo si la abriste tú, en este equipo, ahora mismo.",
    approve: "Aprobar",
    deny: "Rechazar",
    refusedTitle: "Esta solicitud de conexión no se puede usar",
    unavailableTitle: "Ahora mismo no se puede comprobar esta solicitud",
    retry: "Intentarlo de nuevo",
    deniedTitle: "No puedes conectar aplicaciones de IA aquí",
    errorTitle: "La solicitud de esta aplicación no se puede usar",
    declinedTitle: "No has conectado esta aplicación",
    backTo: "Volver a {host}",
    framedTitle: "Abre esta página sola",
    framed: "Esta página aprueba aplicaciones de IA, así que nunca funciona dentro de otra página. Abre el enlace directamente.",
  },
```

El texto de `grants` es el de la pantalla de consentimiento de Keycloak que sustituye (`deploy/keycloak/ci_mcp_flow.py`,
`CONSENT_TEXT`): lo que aprobar entrega no cambia de palabras con el emisor. «Configuración → Conexiones de IA» es el
nombre que la consola ya da a la pestaña (`frontend/src/i18n/es/settings.ts`).

- [ ] **Step 3: El contrato**

En `frontend/src/modules/auth/types.ts`, después de `RecoveryLink`:

```ts
/**
 * The AI app a consent screen names (`backend/app/core/auth/oauth/consent.py`,
 * plan 2026-11-06 Appendix A.2). `name` is the app's own claim (RFC 7591 §5);
 * `client_host` is the host of a client metadata document's URL (`kind:
 * "metadata_document"`), null for an app that registered; `redirect_host` is
 * where the answer goes; `loopback_only` when every address it registered is
 * this computer. Hosts are ASCII (plan Task 1, decision 3).
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
 * the person". `refused` is shown and never sent anywhere (RFC 6749 §4.1.2.1:
 * a bad client or redirect; `temporarily_unavailable` says to try again);
 * `back` is shown with the host it goes to, and goes there only on the
 * person's click (RFC 9700 §4.11.2): an error the app should hear, the
 * person's decline, or a person who may not connect apps; `redirect` is where
 * to go now, with the code, after an approval -- and only then.
 */
export type ConsentAnswer =
  | { outcome: "refused"; error: string; detail: string }
  | {
      outcome: "back";
      reason: "error" | "declined" | "denied";
      detail: string;
      redirect_host: string;
      redirect_to: string;
    }
  | { outcome: "redirect"; redirect_to: string };

/** `POST /oauth/authorize/details`: `consent` asks the person; the rest is `ConsentAnswer`, never `redirect`. */
export type ConsentDetails =
  | { outcome: "consent"; client: ConsentClient; scope: string; account: string }
  | Exclude<ConsentAnswer, { outcome: "redirect" }>;
```

En `frontend/src/lib/api.ts`, `ConsentAnswer` y `ConsentDetails` se añaden al `import type { … } from
"../modules/auth/types"` (en orden alfabético), y en la clase, después de `acceptRecovery`:

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
   * `POST /oauth/authorize/decision`: the person's answer. Approving needs the
   * second factor confirmed in the last 10 minutes (operator decision C1): a
   * 403 `step_up_required` the page answers with `useStepUp`. An approval's
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

/**
 * Whether this document is inside another one (Task 4, decision 6): the
 * frame-busting fallback for a browser that ignores `frame-ancestors`. A
 * cross-origin parent makes `window.top` unreadable, which is also "framed".
 */
export function isFramed(): boolean {
  try {
    return window.top !== window.self;
  } catch {
    return true;
  }
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
 * `/decision`, which validate it every time.
 *
 * WHAT THE PERSON IS SHOWN, from the server's answer: FIRST the host an app
 * cannot fake -- its metadata document's for a CIMD client, its redirect's for
 * one that registered (B3-10; MCP [S30]: "MUST clearly display the redirect
 * URI hostname"; Anthropic [S33]: "name the host of the client_id URL as the
 * app asking for access") -- then the name it gave itself, as its claim; where
 * the answer goes; a warning when it only answers on this computer [S30]; what
 * approving hands over, how long it lasts and where it ends; and the account.
 *
 * ONLY AN APPROVAL LEAVES ON ITS OWN (B3-15; RFC 9700 §4.11.2). An error the
 * app should hear, a decline and "you cannot connect apps here" show the host
 * and wait for "Back to <host>": with open registration no redirect is one the
 * platform trusts.
 *
 * APPROVING ASKS FOR THE SECOND FACTOR when the server wants it (operator
 * decision C1): the decision goes through `stepUp.run`, which answers 403
 * `step_up_required` with the phase-1 dialog and sends it once more.
 *
 * THE APPROVAL'S REDIRECT CARRIES THE AUTHORIZATION CODE. The decision is a
 * `useSecretMutation` (`gcTime: 0`, reset once it settles), the redirect is
 * read once, in the handler, and the document leaves for it at once.
 *
 * NEVER INSIDE ANOTHER PAGE (B3-16): nginx sends `frame-ancestors 'none'`, and
 * the page draws nothing when it is framed anyway.
 *
 * A FAILURE THAT PASSES READS AS ONE (B3-33, A3-24): a details request that
 * got no answer, a 5xx or a 429 (the api restarting, the network, the limit),
 * and a `temporarily_unavailable` from the server at either step, say "cannot
 * be checked right now" with "Try again", which asks the server again; only a
 * refusal of the request itself reads as for good.
 *
 * NOTHING HERE DECIDES ANYTHING (invariant 6): a request the page shows is a
 * request the server accepted a moment ago, and the server accepts it again,
 * or not, at the decision.
 */
import { useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { useLocation } from "react-router-dom";

import { describeSubmitError } from "../../components/form";
import { StepUpCancelled, useStepUp } from "../../components/stepUp";
import { useT } from "../../i18n/context";
import { ApiError } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useSecretMutation } from "../../lib/secretMutation";
import { navigation } from "../../lib/session";
import { Btn, KIND_ICON } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { AuthAlert, AuthCard } from "./AuthCard";
import { authorizationQuery, isFramed } from "./consent";
import type { ConsentAnswer } from "./types";

const LIST = { margin: 0, paddingLeft: 18, listStyle: "disc outside" } as const;
const BACK_TITLE = {
  error: "auth.consent.errorTitle",
  declined: "auth.consent.declinedTitle",
  denied: "auth.consent.deniedTitle",
} as const;

/** No answer, a 5xx or a 429: worth asking again (A3-24). */
function passes(cause: unknown): boolean {
  return !(cause instanceof ApiError) || cause.status >= 500 || cause.status === 429;
}

export function ConsentPage() {
  const t = useT();
  if (isFramed()) {
    return (
      <AuthCard title={t("auth.consent.framedTitle")}>
        <p className="note" style={{ margin: 0 }}>
          {t("auth.consent.framed")}
        </p>
      </AuthCard>
    );
  }
  return <ConsentRequest />;
}

function ConsentRequest() {
  const t = useT();
  const api = useApi();
  const stepUp = useStepUp();
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
    mutationFn: (approve: boolean) => stepUp.run(() => api.oauthAuthorizeDecision(query, approve)),
  });
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [answered, setAnswered] = useState<ConsentAnswer | null>(null);
  const inFlight = useRef(false);

  async function answer(approve: boolean) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setFailure(null);
    try {
      const outcome = await decision.mutateAsync(approve);
      if (outcome.outcome === "redirect") {
        // THE ONE NAVIGATION THE PAGE MAKES ON ITS OWN: the approval's code.
        // The buttons stay disabled until the document has left.
        navigation.assign(outcome.redirect_to);
        return;
      }
      setAnswered(outcome);
    } catch (cause: unknown) {
      // A closed confirmation approved nothing, and says nothing.
      if (!(cause instanceof StepUpCancelled)) setFailure(describeSubmitError(cause));
    }
    inFlight.current = false;
    setBusy(false);
  }

  if (details.isPending || details.isFetching) {
    return (
      <AuthCard title={t("auth.consent.loading")}>
        <p className="note" style={{ margin: 0 }}>
          {t("auth.common.working")}
        </p>
      </AuthCard>
    );
  }
  // Asking again starts from the question: an answer the decision gave is
  // dropped, and the server validates the request once more.
  function askAgain() {
    setAnswered(null);
    setFailure(null);
    void details.refetch();
  }
  if (details.isError) {
    const passing = passes(details.error);
    return (
      <AuthCard title={t(passing ? "auth.consent.unavailableTitle" : "auth.consent.refusedTitle")}>
        <AuthAlert text={describeSubmitError(details.error)} />
        {passing ? (
          <div className="form-actions">
            <Btn label={t("auth.consent.retry")} onClick={askAgain} />
          </div>
        ) : null}
      </AuthCard>
    );
  }
  const shown = answered ?? details.data;
  if (shown.outcome === "refused") {
    const passing = shown.error === "temporarily_unavailable";
    return (
      <AuthCard title={t(passing ? "auth.consent.unavailableTitle" : "auth.consent.refusedTitle")}>
        <AuthAlert text={shown.detail} />
        {passing ? (
          <div className="form-actions">
            <Btn label={t("auth.consent.retry")} onClick={askAgain} />
          </div>
        ) : null}
      </AuthCard>
    );
  }
  if (shown.outcome === "back") {
    return (
      <AuthCard title={t(BACK_TITLE[shown.reason])}>
        <AuthAlert text={shown.detail} />
        <div className="form-actions">
          <Btn
            label={t("auth.consent.backTo", { host: shown.redirect_host })}
            onClick={() => navigation.assign(shown.redirect_to)}
          />
        </div>
      </AuthCard>
    );
  }
  if (shown.outcome !== "consent") return null;
  const { client, account } = shown;
  const host = client.kind === "metadata_document" ? (client.client_host ?? client.redirect_host) : client.redirect_host;
  return (
    <AuthCard title={t("auth.consent.title", { host })} intro={t("auth.consent.intro")}>
      <p className="note" style={{ margin: 0 }}>
        <span>{client.name ? t("auth.consent.calledItself", { name: client.name }) : t("auth.consent.unnamed")}</span>
        {client.kind === "registered" ? (
          <>
            {" "}
            <span>{t("auth.consent.selfRegistered")}</span>
          </>
        ) : null}
      </p>
      <p className="note" style={{ margin: 0 }}>
        {t("auth.consent.grants")}
      </p>
      <p className="note" style={{ margin: 0 }}>
        {t("auth.consent.lasts")}
      </p>
      {client.loopback_only ? (
        <p role="note" className="notice gold" style={{ margin: 0 }}>
          <Icon name={KIND_ICON.warn} size={16} />
          <span>{t("auth.consent.loopbackOnly")}</span>
        </p>
      ) : null}
      <ul className="note" style={LIST}>
        {client.client_host ? <li>{t("auth.consent.publishedBy", { host: client.client_host })}</li> : null}
        <li>{t("auth.consent.returnsTo", { host: client.redirect_host })}</li>
        <li className="mono" style={{ overflowWrap: "anywhere" }}>
          {t("auth.consent.appId", { id: client.client_id })}
        </li>
        <li>{t("auth.consent.account", { email: account })}</li>
      </ul>
      <AuthAlert text={failure} />
      <div className="form-actions">
        <Btn
          variant="primary"
          label={busy && !stepUp.open ? t("auth.common.working") : t("auth.consent.approve")}
          disabled={busy}
          onClick={() => void answer(true)}
        />
        <Btn label={t("auth.consent.deny")} disabled={busy} onClick={() => void answer(false)} />
      </div>
      {stepUp.dialog}
    </AuthCard>
  );
}
```

- [ ] **Step 5: `RequireAuth bare`, la ruta y nginx**

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

En `deploy/nginx.conf`, en **cada uno** de los dos `location` (`/assets/` y `/`), después de su `add_header
Cache-Control …`:

```nginx
        # NEVER FRAMED (own authentication phase 3, plan 2026-11-06 Task 4):
        # the console is the OAuth server's login and consent pages, and RFC
        # 9700 §4.16 says "MUST prevent clickjacking attacks". nginx does not
        # inherit `add_header` into a location that declares its own, so each
        # location says it (tests/test_deploy_frame_headers.py).
        add_header Content-Security-Policy "frame-ancestors 'none'" always;
        add_header X-Frame-Options "DENY" always;
```

- [ ] **Step 6: El inventario de secretos**

En `frontend/src/test/secretMutations.test.ts`, en `SECRET_METHODS`, junto a `acceptRecovery`:

```ts
  oauthAuthorizeDecision: "the answer's redirect carries a one-time authorization code",
```

y en `SECRET_SITES`, junto a la de `RecoverPage`:

```ts
  // `useSecretMutation`, through `stepUp.run` (C1); its test reads the mutation
  // cache after an approval.
  "modules/auth/ConsentPage.tsx oauthAuthorizeDecision": { reviewed: "modules/auth/__tests__/consent.test.tsx" },
```

`stepUp.run` ya es un envoltorio conocido del inventario (`STEP_UP_RUNNERS`, línea 257): el sitio se lee como
`useSecretMutation` con la llamada dentro del cierre. Si la prueba del inventario no lo reconoce, el nombre del sitio que
imprima es el que va en `SECRET_SITES`.

- [ ] **Step 7: Las pruebas pasan**

Run: `cd frontend && npx vitest run`
Expected: PASS, todo (incluidos `src/test/routes-covered.test.ts`, `src/modules/auth/__tests__/routes.test.ts`,
`src/test/secretMutations.test.ts` y las pruebas de paridad de `src/i18n`).

Run: `cd frontend && npx tsc -b && npm run build`
Expected: sin errores.

Run: `cd backend && .venv/bin/pytest -q tests/test_deploy_frame_headers.py`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/modules/auth/consent.ts frontend/src/modules/auth/ConsentPage.tsx \
  frontend/src/modules/auth/types.ts frontend/src/lib/api.ts frontend/src/App.tsx frontend/src/lib/session.ts \
  frontend/scripts/routes.mjs frontend/src/test/routes-covered.test.ts frontend/src/i18n/en/auth.ts \
  frontend/src/i18n/es/auth.ts frontend/src/modules/auth/__tests__/harness.tsx \
  frontend/src/modules/auth/__tests__/consent.test.tsx frontend/src/modules/auth/__tests__/consentGuard.test.tsx \
  frontend/src/modules/auth/__tests__/consentFramed.test.tsx frontend/src/test/secretMutations.test.ts \
  frontend/src/lib/session.test.ts deploy/nginx.conf backend/tests/test_deploy_frame_headers.py
git commit -m "feat(console): the AI app consent page at /oauth/authorize, never framed, never leaving on its own"
```

### Task 5: Migración 0247, modelos, catálogo, `store.py` y `clients.find_client`

Diseño §6.1, §6.2; D3, D10; B3-13, B3-31. Oleada 2, **sola y solo cuando 0246 (fase 2) está en la rama**. Backend.

Las cuatro tablas del servidor OAuth (Apéndice A.3), su lugar en el catálogo de alcance por cliente, el único punto que
abre el alcance «todos» sobre ellas, la guarda que limita quién lo abre, y la lectura de la organización del
despliegue, que va por la función `SECURITY DEFINER` de 0246 y no por el motor de administración. Y las dos funciones
de lectura de `clients.py` que las Tasks 6, 7 y 8 consumen.

**Files:**
- Create: `backend/migrations/versions/0247_oauth_server.py`
- Create: `backend/app/models/oauth.py`
- Modify: `backend/app/models/__init__.py` (import y `__all__`, junto a los de `app.models.mcp`)
- Modify: `backend/app/core/client_scope_catalog.py` (`CATALOG`, junto a las entradas de 0245)
- Modify: `backend/app/core/client_scope.py` (`ALL_SCOPE_ALLOWLIST`, grupo (e), después de `recover.py`)
- Create: `backend/app/core/auth/oauth/store.py`
- Create: `backend/app/core/auth/oauth/clients.py`
- Modify: `backend/tests/test_client_catalog.py` (`SURROGATE_COLUMNS`; la guarda `OAUTH_SESSION_USERS`)
- Create: `backend/tests/oauth_support.py`
- Test: `backend/tests/test_oauth_schema.py`, `backend/tests/test_oauth_server_migration.py`,
  `backend/tests/test_client_catalog.py`, `backend/tests/test_migrations.py`

**Interfaces:**
- Consumes: 0246 (fase 2) como `down_revision`; las funciones de 0231 `its_client_from_parent` e
  `its_client_to_children` (las usa 0245 igual); `app.models.base.Base`, `uuid7_pk`, `client_org_fk`
  (`backend/app/models/base.py:22-39`); `org_session(org_id, *, clients, engine)` (`backend/app/core/tenancy.py:24`);
  `ClientScope.all()`; `policy.CLIENT_IDLE` (Task 1); de la fase 2 (Task 1, en la misma rama que 0246),
  `app.core.auth.lookup.org_of_slug(engine, slug) -> UUID | None`, que llama a `its_auth_org(text)` (`SECURITY
  DEFINER`, solo `app_user`); de las pruebas, `tests.conftest.PgCluster`, `_psql` y
  `tests.test_remote_supervise_migration._alembic` (el patrón de `tests/test_auth_core_migration.py`).
- Produces: las tablas y los modelos `OAuthClient`, `OAuthCode`, `OAuthRefreshFamily`, `OAuthRefreshToken` con
  `CLIENT_KINDS = ("registered", "metadata_document")` y `REVOKE_REASONS` (Step 3); `store.deployment_org`,
  `store.oauth_session`; `clients.ClientView`, `clients.view`, `clients.find_client`, `clients.touch` (escribe como
  mucho una vez cada `clients.TOUCH_EVERY`, una hora; A3-37), la guarda `OAUTH_SESSION_USERS`; en
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

**Decisión de la tarea (4): la organización antes de saber quién llama va por `lookup.org_of_slug`** (B3-13). Diseño
§6.2: «Sin organización fijada, RLS no deja leer nada, y eso es correcto. El único camino es una función `SECURITY
DEFINER` … y queda en la lista revisada de excepciones». La fase 2 añade exactamente esa función (`its_auth_org`, su
D36) y la revisa en `REVIEWED_SECURITY_DEFINER`. `deployment_org` la llama con el motor de la aplicación: ningún motor
de administración (BYPASSRLS) se abre en las tres rutas anónimas de la Task 6 y la 7, y una prueba lo fija para todo
`app/core/auth/oauth/`.

**Decisión de la tarea (5): `oauth_session` tiene una lista de usuarios** (B3-31). `CLAUDE.md` 5a: «Solo los procesos
internos usan el alcance "todos", y cada uso está en una lista revisada». `ALL_SCOPE_ALLOWLIST` revisa la función que
llama a `ClientScope.all()`, no a quien llama a esa función; por eso `tests/test_client_catalog.py` gana
`OAUTH_SESSION_USERS`, al estilo de `UNNARROWED_SCOPE_DOORS`: los archivos de `app/` que pueden importar o nombrar
`oauth_session`, cada uno con su razón. Ninguna ruta está en ella: el registro (Task 6) y la revocación de Ajustes
(Task 10) llaman a funciones del núcleo. Cada tarea que la usa añade su archivo.

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


async def oauth_org(
    env: Any, prefix: str = "oauth", *, mcp_enabled: bool = True
) -> tuple[UUID, str]:
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
- the deployment's organization is `ITS_DEFAULT_ORG_SLUG`'s, read through
  0246's SECURITY DEFINER lookup, and nothing in the OAuth server opens the
  admin engine (design §6.2; B3-13);
- a registration unused for 30 days is not found.
"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
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
        found = await clients.find_client(
            session, org_id=org_id, client_id=fresh.client_id, now=now
        )
        gone = await clients.find_client(
            session, org_id=org_id, client_id=idle.client_id, now=now
        )
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


async def test_a_use_is_written_at_most_once_an_hour(api_env) -> None:
    """A3-37: one app row is shared by everyone who connects it; a token
    request that wrote it every time would queue them all on its lock."""
    org_id, _ = await oauth_org(api_env)
    now = datetime.now(UTC)
    row = await seed_client(api_env, org_id, last_used_at=now - timedelta(minutes=59))
    clients.touch(row, now)
    assert row.last_used_at == now - timedelta(minutes=59)
    clients.touch(row, now + timedelta(minutes=1))
    assert row.last_used_at == now + timedelta(minutes=1)


async def test_the_deployment_org_is_the_default_slugs(api_env) -> None:
    org_id, _ = await oauth_org(api_env)
    assert await deployment_org(api_env.app) == org_id
    api_env.settings.default_org_slug = None
    assert await deployment_org(api_env.app) is None
    api_env.settings.default_org_slug = f"no-such-{uuid4().hex[:6]}"
    assert await deployment_org(api_env.app) is None


def test_the_oauth_server_never_opens_the_admin_engine() -> None:
    """Design §6.2: before the organization is known, only a SECURITY DEFINER
    function reads; the admin engine bypasses RLS altogether."""
    package = Path(__file__).resolve().parents[1] / "app" / "core" / "auth" / "oauth"
    offenders = [p.name for p in sorted(package.glob("*.py")) if "admin_engine" in p.read_text()]
    assert offenders == []
    assert (package / "store.py").is_file()  # non-vacuity: the scan sees the package
```

`backend/tests/test_oauth_server_migration.py`:

```python
"""Migration 0247 on a database of its own, stopped at 0246 (the pattern of
`tests/test_auth_core_migration.py`; plan 2026-11-06, Task 5).

upgrade -> downgrade -> upgrade leaves nothing behind: the four tables, their
`tenant_isolation` and `client_isolation*` policies, and the three
`its_client_to_oauth_*` triggers that live on `users`, not on the tables they
serve -- the one thing CI's `downgrade base` cannot see, because it drops
`users` too.
"""

import secrets
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.conftest import PgCluster, _psql
from tests.test_remote_supervise_migration import _alembic

TABLES = ("oauth_clients", "oauth_refresh_families", "oauth_refresh_tokens", "oauth_codes")
PERSON_TABLES = TABLES[1:]
POLICIES = {(t, "tenant_isolation") for t in TABLES} | {
    (t, p)
    for t in PERSON_TABLES
    for p in ("client_isolation", "client_isolation_update", "client_isolation_delete")
}


@pytest.fixture()
def db_at_0246(pg_cluster: PgCluster) -> Iterator[str]:
    dbname = f"its_oauth_server_{secrets.token_hex(4)}"
    _psql(f'CREATE DATABASE "{dbname}" OWNER its_admin')
    url = pg_cluster.admin_url.rsplit("/", 1)[0] + f"/{dbname}"
    try:
        _alembic(url, "upgrade", "0246")
        yield url
    finally:
        _psql(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


async def _schema(engine: AsyncEngine) -> dict[str, set[object]]:
    async with engine.connect() as conn:
        tables = {
            t
            for t in TABLES
            if (await conn.execute(text("SELECT to_regclass(:t)"), {"t": t})).scalar_one()
        }
        policies = {
            (row.tablename, row.policyname)
            for row in (
                await conn.execute(
                    text(
                        "SELECT tablename, policyname FROM pg_policies "
                        "WHERE tablename = ANY(:names)"
                    ),
                    {"names": list(TABLES)},
                )
            ).all()
        }
        triggers = set(
            (
                await conn.execute(
                    text(
                        "SELECT tgname FROM pg_trigger WHERE tgrelid = 'users'::regclass "
                        "AND tgname LIKE 'its\\_client\\_to\\_oauth\\_%'"
                    )
                )
            ).scalars()
        )
    return {"tables": tables, "policies": policies, "triggers": triggers}


async def test_0247_round_trips(db_at_0246: str) -> None:
    engine = create_async_engine(db_at_0246)
    present = {
        "tables": set(TABLES),
        "policies": POLICIES,
        "triggers": {f"its_client_to_{t}" for t in PERSON_TABLES},
    }
    absent: dict[str, set[object]] = {"tables": set(), "policies": set(), "triggers": set()}
    try:
        _alembic(db_at_0246, "upgrade", "0247")
        assert await _schema(engine) == present
        _alembic(db_at_0246, "downgrade", "0246")
        assert await _schema(engine) == absent
        _alembic(db_at_0246, "upgrade", "0247")
        assert await _schema(engine) == present
    finally:
        await engine.dispose()
```

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_schema.py tests/test_oauth_server_migration.py`
Expected: FAIL, `ImportError: cannot import name 'clients' from 'app.core.auth.oauth'` (el primer import de
`test_oauth_schema.py` que no existe), y en la segunda, `alembic` sin la revisión `0247`.

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
        CheckConstraint(
            "kind IN ('registered', 'metadata_document')", name="ck_oauth_clients_kind"
        ),
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

En `backend/app/models/__init__.py`, en su sitio alfabético, después de `from app.models.mcp import McpRevocation`
(isort, regla I de ruff):

```python
from app.models.oauth import OAuthClient, OAuthCode, OAuthRefreshFamily, OAuthRefreshToken
```

y en `__all__`, justo después de `"McpRevocation",` (la lista no está en orden estricto; los cuatro van junto a su
vecino de import):

```python
    "OAuthClient",
    "OAuthCode",
    "OAuthRefreshFamily",
    "OAuthRefreshToken",
```

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

En `backend/tests/test_client_catalog.py`, al final de `SURROGATE_COLUMNS` (después de la entrada entera de `("auth_passkeys", "credential_id")`, antes del `}` que cierra el diccionario):

```python
    # Own authentication phase 3 (migration 0247): the SHA-256 of a random
    # 256-bit value the AI app holds, found by it before anybody's scope exists.
    ("oauth_codes", "code_hash"): "the SHA-256 of a random 256-bit authorization code",
    ("oauth_refresh_tokens", "token_hash"): "the SHA-256 of a random 256-bit refresh token",
```

y, después de `test_the_unnarrowed_scope_guard_is_not_vacuous`, la guarda de B3-31:

```python
# Who may open `oauth_session`, the every-client session over the OAuth
# server's rows (own authentication phase 3, plan 2026-11-06 B3-31). CLAUDE.md
# 5a: "cada uso está en una lista revisada" -- `ALL_SCOPE_ALLOWLIST` reviews
# the function that calls `ClientScope.all()`, this list the files that call
# it. No route file is here: a route calls a core function that is.
OAUTH_SESSION_USERS: dict[str, str] = {
    "app/core/auth/oauth/store.py": "defines it",
}


def _oauth_session_uses(sources: list[tuple[str, ast.Module]]) -> list[str]:
    found: list[str] = []
    for path, tree in sources:
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            found.extend(f"{path}:{node.lineno}" for name in names if name == "oauth_session")
    return found


def test_only_the_reviewed_files_open_the_oauth_session() -> None:
    outside = [
        use
        for use in _oauth_session_uses(_app_sources())
        if use.split(":")[0] not in OAUTH_SESSION_USERS
    ]
    assert not outside, (
        "oauth_session opens every client over a person's grants; add the file to "
        f"OAUTH_SESSION_USERS with its reason, or call a function that is there: {outside}"
    )


def test_the_oauth_session_guard_is_not_vacuous() -> None:
    # The scan reads the package (its definition is a `def`, not a use) and
    # flags the shape it exists to forbid.
    assert "app/core/auth/oauth/store.py" in {path for path, _ in _app_sources()}
    bad = ast.parse("from app.core.auth.oauth.store import oauth_session\n")
    assert _oauth_session_uses([("app/api/v1/x.py", bad)]) == ["app/api/v1/x.py:1"]
```

Cada tarea que empieza a usar `oauth_session` en un archivo de `app/` añade aquí su entrada con su razón: la 6
(`clients.py`), la 7 (`grants.py`), la 8 (`consent.py`) y la 10 (`connections.py`); la 13 no la necesita, porque el
barrido de la fase 2 abre su propia sesión. Las pruebas (`tests/`) no cuentan: la guarda lee `app/`.

- [ ] **Step 5: `store.py` y `clients.py`**

`backend/app/core/auth/oauth/store.py`:

```python
"""Where the OAuth server's rows are read and written (design §5, §6.1-§6.2;
plan D3).

THE ORGANIZATION OF A REQUEST WITH NO PERSON IS THE DEPLOYMENT'S. A
registration, a token request and a revocation arrive with no cookie and no
person; the only organization they can belong to is the one this
installation signs people in to, `ITS_DEFAULT_ORG_SLUG` -- the rule
`flows.login` follows (phase 1). Its id is read through
`lookup.org_of_slug`, migration 0246's SECURITY DEFINER `its_auth_org`, on
the application engine: design §6.2, "El único camino es una función
SECURITY DEFINER". No admin engine anywhere in this package
(`tests/test_oauth_schema.py`). A slug that names no organization is None,
and the routes answer 503.

EVERY CLIENT, AND ONLY HERE. A technician's grant rows are global rows (O
under `users`, client NULL) that only an every-client scope may write
(migration 0231), and they are read by a hash before anybody's scope exists.
`oauth_session` is the one function that opens that scope over them
(`ALL_SCOPE_ALLOWLIST`), and the files that call it are listed
(`tests/test_client_catalog.py::OAUTH_SESSION_USERS`); a route file never
does.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.auth.lookup import org_of_slug
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session


async def deployment_org(app: Any) -> UUID | None:
    slug = app.state.settings.default_org_slug
    if not slug:
        return None
    return await org_of_slug(app.state.app_engine, slug)


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
(`purge_idle`, Task 6; plan D10). `touch` records a use -- an authorization,
a token request -- AT MOST ONCE AN HOUR (`TOUCH_EVERY`, A3-37): the idle limit
is counted in days, and one app row is shared by every person of the
organization who connects it (Claude's metadata document), so writing it on
every token request would have each of them wait on the same row lock.

`ClientView` is what leaves a session: a plain copy, so nothing holds an ORM
object across transactions.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.oauth.policy import CLIENT_IDLE
from app.models.oauth import OAuthClient

#: How stale `last_used_at` may be before a use writes it again (A3-37).
TOUCH_EVERY = timedelta(hours=1)


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
    """A use of the app: written when the last one recorded is `TOUCH_EVERY`
    old, so most token requests write nothing to this row."""
    if row.last_used_at <= now - TOUCH_EVERY:
        row.last_used_at = now
```

- [ ] **Step 6: Las pruebas pasan**

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_schema.py tests/test_client_catalog.py tests/test_migrations.py`
Expected: PASS (incluidas `test_every_table_is_classified`, `test_scoped_tables_carry_the_restrictive_policy`,
`test_a_parents_client_change_reaches_every_child`, `test_natural_keys_on_per_client_tables_are_per_client`,
`test_client_scope_all_only_in_reviewed_functions` y `test_no_index_in_the_database_is_unknown_to_the_models`).

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_server_migration.py`
Expected: PASS: la ida y vuelta de 0247 en una base propia, que crea y borra la prueba (`db_at_0246`).

Run: `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `Success: no issues found` y `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/migrations/versions/0247_oauth_server.py backend/app/models/oauth.py backend/app/models/__init__.py \
  backend/app/core/client_scope_catalog.py backend/app/core/client_scope.py backend/app/core/auth/oauth/store.py \
  backend/app/core/auth/oauth/clients.py backend/tests/test_client_catalog.py backend/tests/oauth_support.py \
  backend/tests/test_oauth_schema.py backend/tests/test_oauth_server_migration.py
git commit -m "feat(oauth): migration 0247: AI apps, codes and refresh families under RLS"
```

### Task 6: Registro (DCR), clientes CIMD, metadatos RFC 8414 y la ruta de Traefik

Diseño §5.1, §5.4, §5.5; D1, D3, D4, D9, D10, D15; decisión del operador C2 (2026-11-06); B3-02, B3-03, B3-08,
B3-14, B3-26, B3-31. Oleada 3, en paralelo con la Task 7. Backend y `deploy/`.

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
- Modify: `backend/tests/test_client_catalog.py` (`OAUTH_SESSION_USERS` gana `clients.py`)
- Modify: `backend/tests/test_deploy_mcp_route.py`, `backend/tests/test_mcp_discovery_fallbacks.py`,
  `backend/tests/test_mcp_server.py` (`test_the_protected_resource_metadata_is_served_at_both_paths`, `:140-153`)
- Test: `backend/tests/test_oauth_registration.py`

**Interfaces:**
- Consumes: Task 1 (`policy`, incluidos `MAX_REGISTRATIONS`, `REGISTRATIONS_PER_HOUR` y `network_of`; `errors`, incluidos
  `REGISTRATIONS_FULL` y `TOO_LARGE`; `redirects`), Task 3 (`cimd.fetch_document`, `cimd.is_document_client_id`,
  `cimd.on_origin`, `cimd.DocumentRefused` con `transient`, `ClientDocument`), Task 5 (`OAuthClient`,
  `store.deployment_org`, `store.oauth_session`, `clients.find_client`, `clients.view`, `clients.touch`, `ClientView`,
  `tests/oauth_support.py`, `tests/test_oauth_schema.py::_grant`, `OAUTH_SESSION_USERS`). Existentes:
  `app.core.rate_limit.hit(redis, key, *, limit, window_s, fail_closed=False) -> bool`,
  `app.core.client_address.client_address(request, trusted)`, `app.core.http.read_bounded_body(request, max_bytes)`
  (413 como `HTTPException`), `app.core.mcp_channel.mcp_resource`, `app.models.Organization`,
  `uuid_extensions.uuid7`.
- Produces: `Settings.mcp_registration: str`; `clients.Registration`, `parse_registration`, `register`, `purge_idle`,
  `PURGE_BATCH = 100`, `registration_response`, `mcp_switched_on(session, org_id) -> bool`,
  `registration_offered(app) -> bool`, `register_new(app, *, org_id, registration, now) -> dict`,
  `resolve_client(app, *, org_id, client_id, now, fetch=None, record_use=True)` (Apéndice A.1; `fetch` es
  `cimd.fetch_document` por defecto, leído al llamar; `record_use=False` no escribe nada); las rutas `GET METADATA_PATH` y `POST REGISTER_PATH`; en `tests/oauth_support.py`,
  `async def register(env, **metadata) -> dict` y `async def forget_registration_limit(env) -> None`.

**Decisión de la tarea (1): CIMD siempre que MCP esté encendido; el registro dinámico solo con la opción** (C2,
decisión del operador del 2026-11-06). Los metadatos anuncian `client_id_metadata_document_supported: true` siempre
que hay emisor, y `resolve_client` lee un documento CIMD sin mirar `ITS_MCP_REGISTRATION`: solo lo llama el
consentimiento de una persona con sesión, que antes comprueba el interruptor de MCP (Task 8) y descarga por la
protección de salida (Task 3). `registration_endpoint` se anuncia y `POST /oauth/register` se acepta solo con
`ITS_MCP_REGISTRATION=open` **y** MCP encendido en la organización del despliegue (B3-02: «MCP sigue apagado hasta que
un org-admin lo enciende»). Así Claude y Claude Code conectan por CIMD sin abrir el registro anónimo [S33: Claude elige
CIMD cuando el servidor anuncia los dos valores], y la decisión del 2026-10-15 queda para lo que trataba: el registro
anónimo. Cerrar el registro no toca a los clientes CIMD.

**Decisión de la tarea (2): los límites del registro limitan** (B3-02; RFC 7591 §3: «MAY be rate-limited or otherwise
limited to prevent a denial-of-service attack»). Veinte por hora y red, donde una red IPv6 es su /64
(`policy.network_of`: un host tiene su /64 entero y, contado por dirección, convertiría un cubo en 2^64); y un techo de
200 registros vivos por organización (`kind = 'registered'` y usados en los últimos 30 días), el que tenía el realm de
Keycloak (diseño de MCP §3.3, «Max Clients: 200»): `temporarily_unavailable` 503 al llegar. El límite por red falla
cerrado: sin Redis no hay otro tope sobre el registro anónimo (a diferencia del inicio de sesión, con sus contadores
por cuenta), como el tope de correos de restablecimiento (`rate_limit.py`). Un cliente alojado (Claude en la web)
registra desde las direcciones de su proveedor, no desde la oficina; con CIMD no registra nada.

**Decisión de la tarea (3): el registro es una función del núcleo** (B3-31). `clients.register_new` hace la purga,
cuenta el techo y registra en una transacción; la ruta solo lee la petición y llama. `oauth_session` queda en
`app/core/auth/oauth/`, y `clients.py` entra en `OAUTH_SESSION_USERS` con su razón.

**Decisión de la tarea (4): un documento CIMD guardado sobrevive a un fallo transitorio** (CIMD-03 §6.1: «An
authorization server SHOULD NOT discard previously cached valid client metadata on transient failures»). Si la copia
caducó y la nueva lectura falla con `transient`, `resolve_client` sigue con la guardada; un rechazo definitivo se
levanta y no se guarda nunca, pero **no borra** la copia vieja: si una lectura posterior vuelve a fallar de forma
transitoria, esa copia vuelve a servir. Un `client_id` en el origen propio no se descarga (`own_origin`, CIMD-03 §10.1).
La -03 añade, para un rechazo por `client_id` distinto, «SHOULD invalidate the client's outstanding refresh tokens
immediately»; el plan no lo hace (re-revisión f3-2, M-1; de sus dos opciones, la segunda), por dos razones escritas
en el Apéndice B (D17): terminar concesiones es de `grants.py` (Task 7, que corre en paralelo con esta), y una
concesión no depende del documento: la persona aprobó un host, la puerta exige en cada llamada que la concesión siga
viva, y Ajustes la revoca.

**Decisión de la tarea (5): sin CORS en los extremos de máquina** (B3-26; diseño §6.4: «la plataforma no abre CORS
[S63]»). Un cliente MCP que corre en un navegador no puede conectar; Claude y Claude Code no se ven afectados.

**Pruebas existentes que cambian a propósito.** `tests/test_mcp_discovery_fallbacks.py`: `/.well-known/oauth-
authorization-server` deja de ser un 404 de reserva y pasa a ser el documento; la prueba pasa a afirmar las dos cosas
(las otras tres rutas siguen siendo un 404 JSON, la de la raíz es el documento). `tests/test_mcp_server.py::
test_the_protected_resource_metadata_is_served_at_both_paths`: `authorization_servers` pasa de
`settings.oidc_issuer` a la dirección de la consola.

- [ ] **Step 1: Las pruebas que fallan**

Al final de `backend/tests/oauth_support.py`:

```python
#: `oauth_registration`'s buckets (`policy.network_of`): every in-process
#: request comes from 127.0.0.1, so one file's registrations would spend the
#: next file's hour.
REGISTER_BUCKETS = "ratelimit:oauth-register:*"


async def forget_registration_limit(env: Any) -> None:
    redis = env.app.state.redis
    async for key in redis.scan_iter(REGISTER_BUCKETS):
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

- CIMD is offered whenever there is an issuer, and read whenever asked
  (operator decision C2); dynamic registration only with
  `ITS_MCP_REGISTRATION=open` and MCP on (B3-02);
- registration is limited: 20 an hour per network, an IPv6 network being its
  /64, and 200 live registrations (B3-02);
- a stored document survives a transient failure (CIMD-03 §6.1), never a
  refusal, and none is read on the platform's own origin (§10.1).
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.oauth import cimd, clients
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
        listed = await client.post(
            "/oauth/register", json=[{"redirect_uris": [CLAUDE_CALLBACK]}]
        )
        broken = await client.post(
            "/oauth/register", content=b"{", headers={"Content-Type": "application/json"}
        )
        form = await client.post("/oauth/register", data={"redirect_uris": CLAUDE_CALLBACK})
    for resp in (listed, broken, form):
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_client_metadata"


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
Expected: FAIL: las rutas nuevas responden 404 (`app/api/oauth_registration.py` no existe), `clients.resolve_client`
y `clients.REGISTRATIONS_PER_HOUR` no existen (`AttributeError`), y las de Traefik y RFC 9728 fallan en sus
aserciones.

- [ ] **Step 2: El ajuste**

En `backend/app/core/config.py`, después de `public_console_url`:

```python
    # The operator's opt-in to AI apps registering themselves anonymously
    # (deploy/README.md "AI connections (MCP)"): only the exact word `open`
    # opens RFC 7591 registration, and only while MCP is on. A client metadata
    # document needs no opt-in (operator decision C2, 2026-11-06). Read since
    # own authentication phase 3 by the platform's own authorization server
    # (`app/core/auth/oauth/policy.py::registration_open`); keycloak-init reads
    # the same `.env` line until phase 4 retires it.
    mcp_registration: str = ""
```

- [ ] **Step 3: El registro y los clientes CIMD**

Al final de `backend/app/core/auth/oauth/clients.py` (y sus imports pasan a los del bloque de abajo):

```python
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.core.auth.oauth import cimd, policy
from app.core.auth.oauth.errors import (
    BAD_GRANT_TYPES,
    BAD_NAME,
    BAD_REDIRECT,
    BAD_RESPONSE_TYPES,
    NO_REDIRECTS,
    NOT_JSON,
    REGISTRATION_CLOSED,
    REGISTRATIONS_FULL,
    UNAVAILABLE,
    OAuthError,
)
from app.core.auth.oauth.policy import (
    CLIENT_IDLE,
    GRANT_TYPES,
    MAX_REDIRECT_URIS,
    MAX_REGISTRATIONS,
    SCOPE,
    display_name,
    issuer,
    registration_open,
)
from app.core.auth.oauth.redirects import RedirectRejected, check_registered
from app.core.auth.oauth.store import deployment_org, oauth_session
from app.models import Organization
from app.models.oauth import OAuthClient
```

```python
#: How many idle registrations one registration removes (plan D10).
PURGE_BATCH = 100
#: Registrations one network may make per hour (`policy`), read here by the
#: route so that a test lowers it in one place.
REGISTRATIONS_PER_HOUR = policy.REGISTRATIONS_PER_HOUR


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


async def mcp_switched_on(session: AsyncSession, org_id: UUID) -> bool:
    """The organization's MCP switch, as `/mcp` reads it (`is True`: a
    malformed value is off)."""
    settings = (
        await session.execute(select(Organization.settings).where(Organization.id == org_id))
    ).scalar_one_or_none()
    return (settings or {}).get("mcp_enabled") is True


async def registration_offered(app: Any) -> bool:
    """Dynamic registration is open: the operator's opt-in, a deployment
    organization, and MCP switched on there (plan Task 6, decision 1)."""
    if not registration_open(app.state.settings):
        return False
    org_id = await deployment_org(app)
    if org_id is None:
        return False
    async with oauth_session(org_id, app.state.app_engine) as session:
        return await mcp_switched_on(session, org_id)


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


async def register_new(
    app: Any, *, org_id: UUID, registration: Registration, now: datetime
) -> dict[str, Any]:
    """One registration, in one transaction (plan Task 6, decisions 2 and 3):
    the switch read again, up to `PURGE_BATCH` idle ones removed, the
    `MAX_REGISTRATIONS` ceiling counted, the row written; RFC 7591's answer."""
    async with oauth_session(org_id, app.state.app_engine) as session:
        if not await mcp_switched_on(session, org_id):
            raise OAuthError(REGISTRATION_CLOSED)
        await purge_idle(session, org_id=org_id, now=now)
        live = (
            await session.execute(
                select(func.count())
                .select_from(OAuthClient)
                .where(
                    OAuthClient.org_id == org_id,
                    OAuthClient.kind == "registered",
                    OAuthClient.last_used_at > now - CLIENT_IDLE,
                )
            )
        ).scalar_one()
        if live >= MAX_REGISTRATIONS:
            raise OAuthError(REGISTRATIONS_FULL)
        row = await register(session, org_id=org_id, registration=registration, now=now)
        return registration_response(row)


Fetch = Callable[[str], Awaitable[cimd.ClientDocument]]


async def resolve_client(
    app: Any,
    *,
    org_id: UUID,
    client_id: str,
    now: datetime,
    fetch: Fetch | None = None,
    record_use: bool = True,
) -> ClientView | None:
    """The app an authorization request names, or None. A registered app is
    looked up; a client metadata document is read whenever MCP is on (C2: the
    caller, the consent, has checked the switch) -- outside any transaction,
    so the network is not held under a database connection -- when the
    stored copy has expired, and stored for `document.cache_for` (CIMD-03
    §6.2). A transient failure keeps a stored copy (§6.1); a refusal
    (`cimd.DocumentRefused`) is raised and never stored, and a document on
    this server's own origin is never read (§10.1).

    `fetch` defaults to `cimd.fetch_document`, looked up at call time so a
    test can replace it on the module. `record_use=False` writes nothing: the
    consent of a person who may not connect AI apps here neither downloads a
    document (its `fetch`) nor keeps an idle app alive (re-review f3-2, M-3)."""
    engine = app.state.app_engine
    async with oauth_session(org_id, engine) as session:
        row = await find_client(session, org_id=org_id, client_id=client_id, now=now)
        fresh = row is not None and (
            row.kind == "registered"
            or (row.metadata_expires_at is not None and row.metadata_expires_at > now)
        )
        if row is not None and fresh:
            if record_use:
                touch(row, now)
            return view(row)
        stored = view(row) if row is not None and row.kind == "metadata_document" else None
    if not cimd.is_document_client_id(client_id):
        return None
    iss = issuer(app.state.settings)
    if iss is None:
        raise OAuthError(UNAVAILABLE)
    if cimd.on_origin(client_id, iss):
        raise cimd.DocumentRefused("own_origin")  # i18n-exempt: a reason code
    try:
        document = await (fetch or cimd.fetch_document)(client_id)
    except cimd.DocumentRefused as refused:
        if refused.transient and stored is not None:
            return stored
        raise
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
        written = (
            await session.execute(
                select(OAuthClient).where(
                    OAuthClient.org_id == org_id, OAuthClient.client_id == client_id
                )
            )
        ).scalar_one()
        return view(written)
```

(El bloque de imports de arriba sustituye al de la Task 5, que era un subconjunto; `ClientView`, `view`, `find_client` y
`touch` siguen donde estaban. `REGISTRATIONS_PER_HOUR` se copia aquí de `policy` para que la ruta y las pruebas lo lean
de un solo sitio, `clients.REGISTRATIONS_PER_HOUR`. El docstring del módulo gana: «`parse_registration`, `register`, `purge_idle`,
`registration_response` and `register_new` are RFC 7591 (Task 6), the last one the whole registration in one
transaction; `resolve_client` is the authorization request's lookup, a registered app or a client metadata document,
read and stored (CIMD-03 §6).»)

En `backend/tests/test_client_catalog.py`, `OAUTH_SESSION_USERS` gana:

```python
    "app/core/auth/oauth/clients.py": (
        "an app's row: a registration (purge, ceiling and write in one transaction) and a "
        "metadata document read in a consent; the organization's MCP switch for both"
    ),
```

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
a person's consent there is no token. No CORS (B3-26).

THE LIMITS ON REGISTERING (§5.4, B3-02): only with `ITS_MCP_REGISTRATION=open`
and MCP on (decision C2), 20 an hour per network -- an IPv6 network being its
/64 -- failing closed, 200 live registrations, 16 KiB of JSON, at most ten
redirects; each registration removes up to a hundred idle ones (plan D10).
The rules are `clients.register_new`'s; this file reads the request.

THE METADATA (RFC 8414 §2) names the platform as its own issuer (plan D1);
`none` in `token_endpoint_auth_methods_supported` and
`client_id_metadata_document_supported`, always, are what Claude needs to use
a client metadata document instead of registering [S33]; the registration
endpoint only while registration is offered; `S256` alone in
`code_challenge_methods_supported` [S17, S30]; and RFC 9207's
`authorization_response_iss_parameter_supported`. It is cached for 300
seconds, so turning MCP on or off shows in `registration_endpoint` up to five
minutes late; and a database that does not answer leaves the endpoint out
rather than turning the metadata into a 500 (re-review f3-2, M-2).

THE NETWORK'S LIMIT IS COUNTED BEFORE ANYTHING IS READ (re-review f3-2, M-2):
its key needs no organization, so a flood of registrations costs Redis, never
the database, as at the token endpoint (Task 7).
"""

import json
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.core.auth.oauth import clients
from app.core.auth.oauth.errors import (
    NOT_JSON,
    REGISTRATION_CLOSED,
    SLOW_DOWN,
    TOO_LARGE,
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
    REVOKE_PATH,
    SCOPES_SUPPORTED,
    TOKEN_PATH,
    issuer,
    network_of,
    registration_open,
)
from app.core.auth.oauth.store import deployment_org
from app.core.client_address import client_address
from app.core.http import read_bounded_body
from app.core.rate_limit import hit

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
        "client_id_metadata_document_supported": True,
    }
    try:
        offered = await clients.registration_offered(request.app)
    except (SQLAlchemyError, OSError):
        offered = False  # the metadata still answers; registering would not
    if offered:
        document["registration_endpoint"] = iss + REGISTER_PATH
    return JSONResponse(document, headers={"Cache-Control": CACHE_CONTROL})


@router.post(REGISTER_PATH, include_in_schema=False)
async def register_client(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    if issuer(settings) is None:
        raise OAuthError(UNAVAILABLE)
    if not registration_open(settings):
        raise OAuthError(REGISTRATION_CLOSED)
    network = network_of(client_address(request, request.app.state.trusted_proxies))
    within = await hit(
        request.app.state.redis,
        f"oauth-register:{network}",
        limit=clients.REGISTRATIONS_PER_HOUR,
        window_s=3600,
        fail_closed=True,
    )
    if not within:
        raise OAuthError(SLOW_DOWN, headers={"Retry-After": "3600"})
    org_id = await deployment_org(request.app)
    if org_id is None:
        raise OAuthError(UNAVAILABLE)
    media = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media != "application/json":
        raise OAuthError(NOT_JSON)
    try:
        raw = await read_bounded_body(request, MAX_REQUEST_BYTES)
    except HTTPException as exc:
        # The API's 413 is a translated `detail`; this answer is RFC 6749's.
        raise OAuthError(TOO_LARGE) from exc
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise OAuthError(NOT_JSON) from exc
    registration = clients.parse_registration(body)
    answer = await clients.register_new(
        request.app, org_id=org_id, registration=registration, now=datetime.now(UTC)
    )
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
            tr(
                "MCP is not available: ITS_PUBLIC_CONSOLE_URL is not an https address "
                "without a path."
            ),
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
  backend/tests/test_mcp_discovery_fallbacks.py backend/tests/test_mcp_server.py backend/tests/test_client_catalog.py
git commit -m "feat(oauth): metadata, limited registration and client metadata documents; RFC 9728 names the platform"
```

### Task 7: Códigos y familias de refresh (`grants.py`); `/oauth/token` y `/oauth/revoke`

Diseño §5.2, §5.3, §7; D3, D4, D6, D7, D10; B3-05, B3-21, B3-22, B3-23, B3-24, B3-27. Oleada 3, en paralelo con la
Task 6. Backend.

El corazón del servidor: emitir un código (la Task 8 lo llama al aprobar), canjearlo por el par de tokens, rotar el
refresh, detectar su reutilización y revocar. Y las dos rutas de máquina que lo exponen.

**Files:**
- Create: `backend/app/core/auth/oauth/grants.py`
- Create: `backend/app/api/oauth_tokens.py`
- Modify: `backend/app/main.py` (incluir el router, junto al de la Task 6)
- Modify: `backend/tests/oauth_support.py` (`pkce_pair`, `seed_code`, `TOKEN_BUCKETS`, `forget_token_limit`,
  `token_request` al final)
- Modify: `backend/tests/test_client_catalog.py` (`OAUTH_SESSION_USERS` gana `grants.py`)
- Modify: `frontend/src/i18n/en/auditVocabulary.ts`, `frontend/src/i18n/es/auditVocabulary.ts` (cuatro acciones)
- Modify: `backend/tests/test_logging_discipline.py` (recuento y párrafo «NOW») y
  `docs/superpowers/specs/2026-08-14-logging-kernel-design.md` (§2.2, el párrafo «NOW»)
- Test: `backend/tests/test_oauth_token.py`

**Interfaces:**
- Consumes: Task 1 (`errors`: `BAD_CODE`, `BAD_REFRESH`, `BAD_SCOPE`, `BAD_TARGET`, `BAD_VERIFIER`, `BUSY`,
  `CLIENT_AUTH`, `KEYS_UNAVAILABLE`, `MALFORMED`, `MISSING`, `NOT_FORM`, `REPEATED`, `TOO_LARGE`, `UNAVAILABLE`,
  `UNKNOWN_CLIENT`, `UNSUPPORTED_GRANT`, `WRONG_CLIENT`, `NO_STORE`, `OAuthError`, `Problem`, `no_store`; `policy`:
  `CODE_TTL_S`, `REFRESH_IDLE`, `SCOPE`, `SCOPES_SUPPORTED`, `MAX_REQUEST_BYTES`, `TOKEN_REQUESTS_PER_MINUTE`,
  `TOKEN_PATH`, `REVOKE_PATH`, `issuer`, `network_of`; `pkce.verify`, `pkce.s256`), Task 2 (`mint_access_token`,
  `verify_access_token`), Task 5 (modelos, `oauth_session`, `deployment_org`, `find_client`, `touch` —que escribe
  como mucho una vez por hora—, `tests/oauth_support.py`, `OAUTH_SESSION_USERS`). Existentes:
  `audit_service.record(session, *, org_id, actor_type, actor_id, action, target_type, target_id, metadata)`
  (`backend/app/core/audit.py:157`), `ACTIVE_STATUS`, `STAFF_KIND` (`backend/app/core/identity/__init__.py:99-106`),
  `keys.AuthKeysUnavailable`, `read_bounded_body` (`backend/app/core/http.py:68`, un 413 como `HTTPException`),
  `rate_limit.hit(redis, key, *, limit, window_s, fail_closed=False)` (`backend/app/core/rate_limit.py:53`),
  `client_address(request, trusted)` (`backend/app/core/client_address.py`), `mcp_resource`.
- Produces: todo `grants.py` del Apéndice A.1 (`names_resource` incluido; ya **no** `CODE_KEPT` ni `FAMILY_KEPT`: las
  ventanas de retención son de la Task 13); las rutas `POST TOKEN_PATH` y `POST REVOKE_PATH`; las acciones de auditoría
  `mcp.code_reused`, `mcp.refresh_reused`, `mcp.grant_revoked`, `mcp.grant_ended`; en `tests/oauth_support.py`,
  `pkce_pair() -> tuple[str, str]`, `async def seed_code(env, org_id, sub, *, app, challenge,
  redirect_uri=CLAUDE_CALLBACK, now=None) -> str`, `TOKEN_BUCKETS`, `async def forget_token_limit(env) -> None` y
  `async def token_request(env, **form) -> httpx.Response`.

**Decisión de la tarea (1): cada negativa del token es `invalid_grant`, y la misma para lo que no existe y lo que es de
otro cliente.** RFC 6749 §5.2 pone «issued to another client» dentro de `invalid_grant`; decir «es de otro cliente»
aparte confirmaría que un refresh robado es válido. Solo `/oauth/revoke` dice `WRONG_CLIENT`, porque RFC 7009 §2.1
pide informar al cliente y quien revoca ya tiene un token válido.

**Decisión de la tarea (2): un canje fallido gasta el código y lo dice (B3-21).** RFC 6749 §10.5: «Authorization codes
MUST be short lived and single-use. If the authorization server observes multiple attempts to exchange an
authorization code for an access token, the authorization server SHOULD attempt to revoke all access tokens already
granted». Un código vivo que llega con otra app, otro redirect u otro `code_verifier` se niega, queda usado y deja un
WARNING (`"authorization code refused and spent"`, con el motivo y la app; nunca el código). Un cliente legítimo no
manda ninguno de los tres mal, así que el coste es nulo para él, y quien tiene un código robado tiene un intento. El
WARNING no necesita freno (B3-32): cada uno gasta un código, y cada código es una aprobación de una persona con sesión.
Una petición de una app que no existe (`invalid_client`) no lee ningún código.

**Decisión de la tarea (3): quien rompe una familia sin ser la persona es «system», y queda auditado (B3-22).** El
diseño §7 audita los «consentimientos y revocaciones de clientes de IA». Una reutilización, la revocación que pide la
app (RFC 7009) o una persona que ya no está activa terminan una concesión que la persona no terminó: la fila de
auditoría lleva `actor_type="system"`, la persona como objetivo y la app en `metadata.app` (`mcp.code_reused`,
`mcp.refresh_reused`, `mcp.grant_revoked`, `mcp.grant_ended` con `metadata.reason="person_inactive"`). **No se
auditan, a propósito,** el refresh —continúa cada 5 a 10 minutos, mientras se use, un consentimiento ya auditado
(`mcp.app_authorized`, Task 8) y enterraría la cadena— ni el registro (Task 6), que es la afirmación de un extraño y no concede nada hasta
que una persona lo aprueba. Cada acción es un literal en su llamada (Global Constraints).

**Decisión de la tarea (4): OpenBao caído es 503 `temporarily_unavailable` y no gasta nada.** El token se firma dentro
de la transacción; `AuthKeysUnavailable` la deshace, y la ruta lo traduce al cuerpo de RFC 6749.

**Decisión de la tarea (5): `redirect_uri` es opcional en el canje (B3-23); si llega, es el mismo.** OAuth 2.1-17
§10.2: «A client following only the OAuth 2.1 recommendations will not send the redirect_uri in the token request,
and therefore will not be compatible with an authorization server that expects the parameter in the token request»;
MCP: «Authorization servers MUST implement OAuth 2.1». El código ya está atado al redirect que nombró la autorización
(con su puerto, si es loopback), y la redirección la hizo la plataforma; si la petición lo repite y es otro, el código
se niega y se gasta (decisión 2).

**Decisión de la tarea (6): `resource` falta = el recurso MCP; esquema y host en cualquier caja (B3-05).** RFC 8707
§2.1 permite «using a predefined default resource value», y el único recurso es el MCP. MCP: «implementations SHOULD
accept uppercase scheme and host components for robustness and interoperability» (RFC 3986 §6.2.2.1); la ruta se
compara exacta (`names_resource`). La familia guarda siempre el recurso canónico del código.

**Decisión de la tarea (7): la reutilización de un refresh se detecta sin ventana de gracia (B3-27).** Diseño §5.3:
«reutilizar uno ya usado revoca toda la familia». El token se lee `FOR UPDATE`; dos refresh con el mismo token se
serializan y el segundo termina la familia, igual que uno cuya respuesta la app no llegó a leer (Claude deja de
esperar a los 30 s [S33]). La persona vuelve a conectar la app; la Task 12 lo escribe en el runbook como riesgo
residual (A3-40).

**Decisión de la tarea (8): un límite por red en los dos extremos, que falla en abierto (B3-24).** RFC 7009 §5:
«Appropriate countermeasures, which should be in place for the token endpoint as well, MUST be applied to the
revocation endpoint». `TOKEN_REQUESTS_PER_MINUTE` (600) por red (`policy.network_of`: IPv6 por /64), un cubo para los
dos, 429 `temporarily_unavailable` con `Retry-After: 60`. Es alto porque Claude en la web pide los tokens de todas sus
personas desde la salida de Anthropic, que comparten todos sus clientes (Apéndice C, [P-ANTH]). La página de
conectores, https://claude.com/docs/connectors/building/authentication.md: «Anthropic's outbound traffic to your server
originates from `160.79.104.0/21`»; la de direcciones, https://platform.claude.com/docs/en/api/ip-addresses.md: «These
are the stable IP addresses that Anthropic uses for outbound requests», `160.79.104.0/21` y `153.61.208.0/20`.
**Cuántas** de esas 6.144 direcciones usa de verdad no lo publica (re-revisión f3-2, M-4); el cubo es por dirección
IPv4 o por /64, así que cuantas más use, menos comparte cada una. La cadencia, de la primera página: «Claude refreshes
tokens reactively on a `401` response, and proactively up to five minutes before the stored expiry»; con tokens de
600 s, una conexión en uso refresca cada 5 a 10 minutos. A una cada cinco minutos, 600 al minuto son unas 3.000
conexiones por dirección de salida, y una oficina con Claude Code detrás de un NAT cabe también. Falla en abierto (el valor por defecto de `rate_limit.hit`, que lo registra): lo que
estas rutas guardan es un secreto de 256 bits que ninguna avalancha adivina, y sin Redis se pierde el límite, no cada
conexión de IA. El registro (Task 6) falla en cerrado porque escribe filas que cualquiera puede crear.

**Decisión de la tarea (9): `last_used_at` de la app se escribe después de firmar, y como mucho una vez por hora
(A3-37).** Una fila de app la comparten todas las personas que la conectan (el documento de Claude); `touch` (Task 5)
solo escribe si el último uso tiene una hora, y se llama después de `mint_access_token`, para que ningún cambio futuro
la vuelque antes de que OpenBao conteste.

**Decisión de la tarea (10): aquí no se borra nada.** Los códigos gastados o caducados y las familias terminadas los
barre la retención diaria de la fase 2, con los pasos que añade la Task 13; las ventanas (10 minutos, 1 día) viven
allí.

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


#: `oauth_tokens`' bucket (B3-24): every in-process request comes from
#: 127.0.0.1, so one file's token requests would spend the next one's minute.
TOKEN_BUCKETS = "ratelimit:oauth-token:*"


async def forget_token_limit(env: Any) -> None:
    redis = env.app.state.redis
    async for key in redis.scan_iter(TOKEN_BUCKETS):
        await redis.delete(key)


async def token_request(env: Any, **form: str) -> httpx.Response:
    """`POST /oauth/token`, form-encoded, as an AI app sends it [S33], in a
    fresh minute of the network's limit."""
    await forget_token_limit(env)
    async with oauth_http(env) as client:
        return await client.post("/oauth/token", data=form)
```

`backend/tests/test_oauth_token.py`:

```python
"""The token and revocation endpoints (autenticación propia design §5.2,
§5.3; plan 2026-11-06, Task 7).

- a code is single-use, 60 s, bound to its app, its exact redirect and its
  PKCE challenge; a failed exchange spends it (RFC 6749 §10.5); reused after
  a success, it ends the grant it opened (RFC 6749 §4.1.2);
- the token request may leave out `redirect_uri` (OAuth 2.1-17 §10.2) and
  `resource` (RFC 8707 §2.1), and write the resource's scheme and host in
  capitals (MCP);
- a refresh token rotates on every use; a used one ends its family with no
  grace (design §5.3, falsifier 6), and so does a person who is no longer
  active, audited;
- every refusal of a grant is `invalid_grant` [S33]; the request is RFC 6749's
  (form-encoded, public client, no parameter twice, at most 16 KiB);
- one network is held to `TOKEN_REQUESTS_PER_MINUTE` across both endpoints,
  and Redis down lets it through (B3-24);
- RFC 7009 revocation ends the family, and answers 200 to a token it does not
  know;
- nothing is stored in clear, and OpenBao down spends nothing.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import keys
from app.core.auth.oauth import access, grants, policy
from app.core.auth.oauth.store import oauth_session
from app.models import AuditEvent, User
from app.models.oauth import OAuthClient, OAuthCode, OAuthRefreshFamily, OAuthRefreshToken
from tests.oauth_support import (
    CLAUDE_CALLBACK,
    RESOURCE,
    forget_token_limit,
    oauth_http,
    oauth_org,
    pkce_pair,
    seed_client,
    seed_code,
    token_request,
)
from tests.session_support import user_id_of

SPENT = "authorization code refused and spent"


class Env:
    def __init__(self, api: Any, org_id: UUID, sub: str, app: OAuthClient) -> None:
        self.api, self.org_id, self.sub, self.app = api, org_id, sub, app


@pytest.fixture()
async def env(api_env) -> Env:
    org_id, _ = await oauth_org(api_env)
    sub = await api_env.create_user(org_id, "org-admin")
    app = await seed_client(
        api_env, org_id, redirect_uris=(CLAUDE_CALLBACK, "http://127.0.0.1/callback")
    )
    return Env(api_env, org_id, sub, app)


async def _code(env: Env, **kw: Any) -> tuple[str, str]:
    verifier, challenge = pkce_pair()
    code = await seed_code(env.api, env.org_id, env.sub, app=env.app, challenge=challenge, **kw)
    return code, verifier


async def _exchange(
    env: Env, code: str, verifier: str, *, redirect_uri: str | None = CLAUDE_CALLBACK, **extra: str
) -> Any:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": env.app.client_id,
        "code_verifier": verifier,
        **({} if redirect_uri is None else {"redirect_uri": redirect_uri}),
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
                    select(AuditEvent).where(
                        AuditEvent.org_id == env.org_id, AuditEvent.action == action
                    )
                )
            ).scalars()
        )


async def _disable(env: Env) -> None:
    async with AsyncSession(env.api.admin_engine) as session:
        await session.execute(
            update(User)
            .where(User.id == await user_id_of(env.api, env.sub))
            .values(status="disabled")
        )
        await session.commit()


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


@pytest.mark.parametrize("redirect", ["claude", "loopback"])
async def test_the_exchange_may_leave_out_the_redirect_uri(env: Env, redirect: str) -> None:
    """OAuth 2.1-17 §10.2: an OAuth 2.1 client does not send it (B3-23)."""
    kw = {} if redirect == "claude" else {"redirect_uri": "http://127.0.0.1:51004/callback"}
    code, verifier = await _code(env, **kw)
    resp = await _exchange(env, code, verifier, redirect_uri=None)
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    "resource", [None, "HTTPS://ITMS.EXAMPLE.COM/mcp", "https://Itms.Example.com/mcp"]
)
async def test_the_resource_may_be_left_out_or_its_scheme_and_host_capitalised(
    env: Env, resource: str | None
) -> None:
    """RFC 8707 §2.1's default resource, and MCP's "SHOULD accept uppercase
    scheme and host components" (B3-05); the grant names the canonical one."""
    code, verifier = await _code(env)
    extra = {} if resource is None else {"resource": resource}
    resp = await _exchange(env, code, verifier, **extra)
    assert resp.status_code == 200, resp.text
    (family,) = await _families(env)
    assert family.resource == RESOURCE


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
    assert event.target_id == str(await user_id_of(env.api, env.sub))
    assert event.meta["app"] == env.app.client_id
    # The grant's refresh token is dead too.
    assert (await _refresh(env, first.json()["refresh_token"])).json()["error"] == "invalid_grant"


@pytest.mark.parametrize("wrong", ["verifier", "redirect", "client"])
async def test_a_failed_exchange_spends_the_code_and_says_so(
    env: Env, wrong: str, caplog: pytest.LogCaptureFixture
) -> None:
    """RFC 6749 §10.5, B3-21: a legitimate client never sends these wrong,
    so the attempt costs the code."""
    code, verifier = await _code(env)
    with caplog.at_level(logging.WARNING, logger="its.auth.oauth.grants"):
        if wrong == "verifier":
            refused = await _exchange(env, code, pkce_pair()[0])
        elif wrong == "redirect":
            refused = await _exchange(env, code, verifier, redirect_uri="http://127.0.0.1/callback")
        else:
            other = await seed_client(env.api, env.org_id)
            refused = await _exchange(env, code, verifier, client_id=other.client_id)
    assert refused.status_code == 400
    assert refused.json()["error"] == "invalid_grant"
    if wrong == "verifier":
        assert (
            refused.json()["error_description"]
            == "the code_verifier does not match the code_challenge"
        )
    (record,) = [r for r in caplog.records if r.name == "its.auth.oauth.grants"]
    assert (record.levelname, record.getMessage(), record.reason) == ("WARNING", SPENT, wrong)
    assert code not in caplog.text and verifier not in caplog.text
    # Spent: the right request after it gets nothing, and nothing was opened.
    assert (await _exchange(env, code, verifier)).json()["error"] == "invalid_grant"
    assert await _families(env) == []
    assert await _events(env, "mcp.code_reused") == []  # no grant existed to end


@pytest.mark.parametrize("change", ["expired", "other-port", "unknown"])
async def test_a_code_outside_its_bounds_is_invalid_grant(env: Env, change: str) -> None:
    if change == "expired":
        code, verifier = await _code(env, now=datetime.now(UTC) - timedelta(seconds=61))
        resp = await _exchange(env, code, verifier)
    elif change == "other-port":
        code, verifier = await _code(env, redirect_uri="http://127.0.0.1:51004/callback")
        resp = await _exchange(env, code, verifier, redirect_uri="http://127.0.0.1:51005/callback")
    else:
        _, verifier = await _code(env)
        resp = await _exchange(env, "c" * 43, verifier)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"
    assert await _families(env) == []


async def test_a_loopback_code_is_redeemed_at_the_port_it_was_asked_for(env: Env) -> None:
    code, verifier = await _code(env, redirect_uri="http://127.0.0.1:51004/callback")
    resp = await _exchange(env, code, verifier, redirect_uri="http://127.0.0.1:51004/callback")
    assert resp.status_code == 200, resp.text


async def test_an_unknown_client_is_invalid_client_and_reads_no_code(env: Env) -> None:
    code, verifier = await _code(env)
    resp = await _exchange(env, code, verifier, client_id="itms_nobody")
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client"
    assert (await _exchange(env, code, verifier)).status_code == 200


@pytest.mark.parametrize("resource", ["https://itms.example.com", "https://itms.example.com/MCP"])
async def test_another_resource_is_invalid_target(env: Env, resource: str) -> None:
    code, verifier = await _code(env)
    resp = await _exchange(env, code, verifier, resource=resource)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_target"


async def test_a_person_disabled_since_the_approval_gets_nothing(env: Env) -> None:
    code, verifier = await _code(env)
    await _disable(env)
    resp = await _exchange(env, code, verifier)
    assert resp.json()["error"] == "invalid_grant"
    assert await _families(env) == []


async def test_the_apps_last_use_is_written_at_most_once_an_hour(env: Env) -> None:
    """A3-37: the row every person of the organization shares is not
    written by every token request."""
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        recent = datetime.now(UTC) - timedelta(minutes=5)
        await session.execute(
            update(OAuthClient).where(OAuthClient.id == env.app.id).values(last_used_at=recent)
        )
    await _tokens(env)
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        row = await session.get(OAuthClient, env.app.id)
        assert row is not None and row.last_used_at == recent


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
    # The old one again, however soon: the family ends, the new one with it
    # (falsifier 6; no grace window, B3-27).
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
    now = datetime.now(UTC)
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        # Opened 31 days ago and idle since (`expires_at > created_at` holds).
        await session.execute(
            update(OAuthRefreshFamily)
            .where(OAuthRefreshFamily.id == family.id)
            .values(
                created_at=now - timedelta(days=31),
                last_used_at=now - timedelta(days=31),
                expires_at=now - timedelta(seconds=1),
            )
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
        env.api,
        grant_type="refresh_token",
        refresh_token=first["refresh_token"],
        client_id=other.client_id,
    )
    assert resp.json()["error"] == "invalid_grant"
    (family,) = await _families(env)
    assert family.revoked_at is None  # not a reuse: the owner's token still works
    assert (await _refresh(env, first["refresh_token"])).status_code == 200


async def test_a_disabled_person_cannot_refresh_and_the_end_is_audited(env: Env) -> None:
    first = await _tokens(env)
    await _disable(env)
    assert (await _refresh(env, first["refresh_token"])).json()["error"] == "invalid_grant"
    (family,) = await _families(env)
    assert family.revoked_reason == "person_inactive"
    (event,) = await _events(env, "mcp.grant_ended")
    assert event.actor_type == "system"
    assert event.meta == {
        "app": env.app.client_id,
        "family": str(family.id),
        "reason": "person_inactive",
    }


async def test_a_refresh_is_not_audited(env: Env) -> None:
    """B3-22: it continues a consent already audited."""
    first = await _tokens(env)
    assert (await _refresh(env, first["refresh_token"])).status_code == 200
    async with AsyncSession(env.api.admin_engine) as session:
        actions = set(
            (
                await session.execute(
                    select(AuditEvent.action).where(
                        AuditEvent.org_id == env.org_id, AuditEvent.action.like("mcp.%")
                    )
                )
            ).scalars()
        )
    assert actions == set()


@pytest.mark.parametrize(
    ("scope", "ok"), [("mcp", True), ("mcp offline_access", True), ("openid admin", False)]
)
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


@pytest.mark.parametrize("path", ["/oauth/token", "/oauth/revoke"])
async def test_a_body_over_16_kib_is_an_rfc_6749_413(env: Env, path: str) -> None:
    async with oauth_http(env.api) as client:
        resp = await client.post(
            path,
            content=b"token=" + b"a" * policy.MAX_REQUEST_BYTES,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    assert resp.status_code == 413
    assert resp.json() == {
        "error": "invalid_request",
        "error_description": "the request body is too large",
    }
    assert resp.headers["cache-control"] == "no-store"


async def test_one_network_is_held_to_its_requests_a_minute_on_both_endpoints(env: Env) -> None:
    """RFC 7009 §5 and B3-24: the token endpoint's countermeasures apply to
    revocation too; one bucket for both."""
    await forget_token_limit(env.api)
    redis = env.api.app.state.redis
    await redis.set("ratelimit:oauth-token:ip:127.0.0.1", policy.TOKEN_REQUESTS_PER_MINUTE, ex=60)
    async with oauth_http(env.api) as client:
        token = await client.post("/oauth/token", data={"grant_type": "refresh_token"})
        revoke = await client.post("/oauth/revoke", data={"token": "x", "client_id": "y"})
    for resp in (token, revoke):
        assert resp.status_code == 429
        assert resp.json()["error"] == "temporarily_unavailable"
        assert resp.headers["retry-after"] == "60"
    await forget_token_limit(env.api)


async def test_without_redis_the_limit_lets_the_request_through(env: Env, monkeypatch) -> None:
    from redis.exceptions import ConnectionError as RedisDown

    class Down:
        async def incr(self, _key: str) -> int:
            raise RedisDown("down")

    first = await _tokens(env)
    monkeypatch.setattr(env.api.app.state, "redis", Down())
    async with oauth_http(env.api) as client:
        resp = await client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": first["refresh_token"],
                "client_id": env.app.client_id,
            },
        )
    assert resp.status_code == 200, resp.text


async def test_without_the_deployments_organization_the_server_is_unavailable(env: Env) -> None:
    env.api.settings.default_org_slug = None
    resp = await token_request(
        env.api, grant_type="refresh_token", refresh_token="x", client_id="y"
    )
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
    await forget_token_limit(env.api)
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


def test_a_resource_is_named_by_scheme_and_host_in_any_case_and_the_rest_exactly() -> None:
    assert grants.names_resource("HTTPS://ITMS.EXAMPLE.COM/mcp", RESOURCE)
    assert not grants.names_resource("https://itms.example.com/Mcp", RESOURCE)
    assert not grants.names_resource("https://itms.example.com:443/mcp", RESOURCE)
    assert not grants.names_resource("https://[::1", RESOURCE)
```

En `backend/tests/test_client_catalog.py`, `OAUTH_SESSION_USERS` gana:

```python
    "app/core/auth/oauth/grants.py": (
        "a person's codes and refresh families: one transaction per token, revocation or "
        "consent request, and the MCP door's check that a grant still stands"
    ),
```

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_token.py`
Expected: FAIL, `ImportError: cannot import name 'grants' from 'app.core.auth.oauth'`.

- [ ] **Step 2: `grants.py`**

`backend/app/core/auth/oauth/grants.py`:

```python
"""Authorization codes and refresh-token families (design §5.2, §5.3, §7;
plan D6, D10).

THE CODE (§5.2): 256 random bits, stored as its SHA-256, 60 seconds, single
use, bound to the app, the exact redirect the authorization named (a loopback
port included), the PKCE challenge and the resource. The token request need
not repeat the redirect (OAuth 2.1-17 §10.2: "A client following only the
OAuth 2.1 recommendations will not send the redirect_uri in the token
request"; plan B3-23); when it does, it is that one. A FAILED EXCHANGE SPENDS
THE CODE (RFC 6749 §10.5: "Authorization codes MUST be short lived and
single-use"; plan B3-21): another app, another redirect or a wrong verifier is
refused, the code is used up and a WARNING says why -- a legitimate client
sends none of the three wrong. Used again after a success, the code is
refused and the grant its first use opened ends: RFC 6749 §4.1.2, "MUST deny
the request and SHOULD revoke (when possible) all tokens previously issued
based on that authorization code".

THE FAMILY (§5.3): one person's grant to one app. Its refresh tokens are 256
random bits stored as SHA-256 and ROTATE on every use (RFC 9700: "the
authorization server issues a new refresh token with every access token
refresh response. The previous refresh token is invalidated, but information
about the relationship is retained" [S16]; MCP: "MUST rotate refresh tokens"
[S30]). Presenting a used one ends the family, with NO GRACE WINDOW (§5.3:
"reutilizar uno ya usado revoca toda la familia"; plan B3-27): a refresh whose
answer the app never read, or two racing with one token, end it too, and the
person connects again. So does a person no longer active; the family dies
after `REFRESH_IDLE` without use. Every refusal is `invalid_grant`
(Anthropic: "Return RFC 6749-compliant error codes when a refresh token is no
longer valid: invalid_grant" [S33]).

WHAT IS AUDITED (§7, "consentimientos y revocaciones de clientes de IA"; plan
B3-22): every end of a grant the person did not cause -- a reused code, a
reused refresh token, the app's own RFC 7009 request, a person no longer
active -- as "system", the person as the target, the app in `metadata.app`.
NOT a refresh, which continues a consent already audited
(`mcp.app_authorized`, Task 8) every five to ten minutes while the app is in
use, and NOT a registration (Task 6), a stranger's claim that grants nothing
until a person approves it. Each action is a literal at its call: the audit
vocabulary's coverage test reads them.

ONE TRANSACTION PER REQUEST, SIGNED INSIDE IT (`oauth_session`): the access
token is minted before the commit, so OpenBao down rolls back and spends
nothing. A refusal that follows a write -- a spent code, a reuse that ended a
family -- is returned as a `Problem` and raised AFTER the block has
committed, so the write stays (the `flows` pattern, phase 1). The app's
`last_used_at` is touched after the signature, at most once an hour
(`clients.touch`; A3-37).

NOTHING IS DELETED HERE: spent codes and ended families go with the daily
retention sweep (`app/core/auth/retention.py`, Task 13).

LOCKED: the code or the refresh token is read `FOR UPDATE`, so two requests
with the same one are serialised and the second sees it used.

BULK STATEMENTS ON O TABLES (`update`) are invisible to
`app.core.global_rows`'s listener; every one here runs under `oauth_session`
or a caller's every-client session (the lifecycle acts, Task 10), where
writing a technician's global rows is allowed.
"""

import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from sqlalchemy import select, update
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

logger = logging.getLogger("its.auth.oauth.grants")


def hash_secret(value: str) -> bytes:
    """SHA-256: a random 256-bit value needs no salt and no pepper."""
    return sha256(value.encode("utf-8")).digest()


def new_secret() -> str:
    """256 random bits, base64url: 43 characters."""
    return secrets.token_urlsafe(32)


def names_resource(value: str, resource: str) -> bool:
    """`value` is `resource`, its scheme and host in any case and the rest
    exactly (MCP: "implementations SHOULD accept uppercase scheme and host
    components for robustness and interoperability"; RFC 3986 §6.2.2.1; plan
    B3-05)."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    canonical = urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, parts.fragment)
    )
    return canonical == resource


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


async def _new_refresh_token(
    session: AsyncSession, family: OAuthRefreshFamily, now: datetime
) -> str:
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


def _refusal(
    row: OAuthCode, client: OAuthClient, redirect_uri: str | None, code_verifier: str
) -> tuple[str, Problem] | None:
    """Why the exchange of a live, unused code fails, if it does (B3-21)."""
    if row.oauth_client_id != client.id:
        return "client", BAD_CODE
    if redirect_uri is not None and redirect_uri != row.redirect_uri:
        return "redirect", BAD_CODE
    if not pkce.verify(code_verifier, row.code_challenge):
        return "verifier", BAD_VERIFIER
    return None


async def _exchange(
    session: AsyncSession,
    settings: Any,
    *,
    org_id: UUID,
    client_id: str,
    code: str,
    redirect_uri: str | None,
    code_verifier: str,
    now: datetime,
) -> IssuedTokens | Problem:
    client = await find_client(session, org_id=org_id, client_id=client_id, now=now)
    if client is None:
        return UNKNOWN_CLIENT  # no app: no code is read (Task 7, decision 2)
    row = (
        await session.execute(
            select(OAuthCode).where(OAuthCode.code_hash == hash_secret(code)).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        return BAD_CODE
    if row.used_at is not None:
        if row.family_id is not None and await revoke_family(
            session, row.family_id, "code_reused", now
        ):
            await audit_service.record(
                session,
                org_id=org_id,
                actor_type="system",
                actor_id=None,
                action="mcp.code_reused",
                target_type="user",
                target_id=str(row.user_id),
                metadata={"app": client.client_id, "family": str(row.family_id)},
            )
        return BAD_CODE
    if row.expires_at <= now:
        return BAD_CODE
    refused = _refusal(row, client, redirect_uri, code_verifier)
    row.used_at = now  # spent, whatever follows (B3-21)
    if refused is not None:
        reason, problem = refused
        logger.warning(
            "authorization code refused and spent",
            extra={"reason": reason, "app": client.client_id},
        )
        return problem
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
    access_token, expires_in = await mint_access_token(
        settings=settings,
        user_id=row.user_id,
        org_id=org_id,
        client_id=client.client_id,
        family_id=family.id,
    )
    touch(client, now)  # after the signature (A3-37)
    return IssuedTokens(access_token, expires_in, refresh_token)


async def exchange_code(
    app: Any,
    *,
    org_id: UUID,
    client_id: str,
    code: str,
    redirect_uri: str | None,
    code_verifier: str,
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
            await audit_service.record(
                session,
                org_id=org_id,
                actor_type="system",
                actor_id=None,
                action="mcp.refresh_reused",
                target_type="user",
                target_id=str(family.user_id),
                metadata={"app": client.client_id, "family": str(family.id)},
            )
        return BAD_REFRESH
    if not await _person_is_active(session, family.user_id):
        if await revoke_family(session, family.id, "person_inactive", now):
            await audit_service.record(
                session,
                org_id=org_id,
                actor_type="system",
                actor_id=None,
                action="mcp.grant_ended",
                target_type="user",
                target_id=str(family.user_id),
                metadata={
                    "app": client.client_id,
                    "family": str(family.id),
                    "reason": "person_inactive",
                },
            )
        return BAD_REFRESH
    token.used_at = now
    family.last_used_at = now
    family.expires_at = now + REFRESH_IDLE
    rotated = await _new_refresh_token(session, family, now)
    access_token, expires_in = await mint_access_token(
        settings=settings,
        user_id=family.user_id,
        org_id=org_id,
        client_id=client.client_id,
        family_id=family.id,
    )
    touch(client, now)  # after the signature (A3-37)
    return IssuedTokens(access_token, expires_in, rotated)


async def refresh_grant(
    app: Any, *, org_id: UUID, client_id: str, refresh_token: str
) -> IssuedTokens:
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
                    await audit_service.record(
                        session,
                        org_id=org_id,
                        actor_type="system",
                        actor_id=None,
                        action="mcp.grant_revoked",
                        target_type="user",
                        target_id=str(family.user_id),
                        metadata={"app": client.client_id, "family": str(family.id)},
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
`MAX_REQUEST_BYTES` (an RFC 6749 413 past it), no parameter twice ("Request
and response parameters MUST NOT be included more than once", §3.2) --
`resource` excepted, which RFC 8707 §2 lets repeat -- and an empty one is
absent ("Parameters sent without a value MUST be treated as if they were
omitted"). Every `resource` must name the MCP resource (`invalid_target`); none
at all is the MCP resource (RFC 8707 §2.1; plan B3-05). `redirect_uri` is
optional (OAuth 2.1-17 §10.2; plan B3-23).

ONE NETWORK, `TOKEN_REQUESTS_PER_MINUTE` A MINUTE, ACROSS BOTH (RFC 7009 §5;
plan B3-24), counted before anything else is read; it fails open.

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

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from app.core.auth.keys import AuthKeysUnavailable
from app.core.auth.oauth import grants
from app.core.auth.oauth.errors import (
    BAD_SCOPE,
    BAD_TARGET,
    BUSY,
    CLIENT_AUTH,
    KEYS_UNAVAILABLE,
    MALFORMED,
    MISSING,
    NO_STORE,
    NOT_FORM,
    REPEATED,
    TOO_LARGE,
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
    TOKEN_REQUESTS_PER_MINUTE,
    issuer,
    network_of,
)
from app.core.auth.oauth.store import deployment_org
from app.core.client_address import client_address
from app.core.http import read_bounded_body
from app.core.mcp_channel import mcp_resource
from app.core.rate_limit import hit

router = APIRouter()

Form = dict[str, list[str]]
_GRANTS = frozenset({"authorization_code", "refresh_token"})
#: The limit's window, in seconds, and what `Retry-After` says (B3-24).
_WINDOW_S = 60


async def _within_limit(request: Request) -> None:
    """FAILS OPEN (`rate_limit.hit`'s default, which logs it): what these
    endpoints guard is a 256-bit secret no flood guesses, and without Redis
    the limit is lost, not every AI connection (Task 7, decision 8)."""
    network = network_of(client_address(request, request.app.state.trusted_proxies))
    allowed = await hit(
        request.app.state.redis,
        f"oauth-token:{network}",
        limit=TOKEN_REQUESTS_PER_MINUTE,
        window_s=_WINDOW_S,
    )
    if not allowed:
        raise OAuthError(BUSY, headers={"Retry-After": str(_WINDOW_S)})


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
    try:
        raw = await read_bounded_body(request, MAX_REQUEST_BYTES)
    except HTTPException as exc:
        # The API's 413 is a translated `detail`; this answer is RFC 6749's.
        raise OAuthError(TOO_LARGE) from exc
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
    assert resource is not None  # `_ready` refused without it
    if not all(grants.names_resource(value, resource) for value in form.get("resource", [])):
        raise OAuthError(BAD_TARGET)


def _check_scope(form: Form) -> None:
    scope = _optional(form, "scope")
    if scope is not None and not set(scope.split()) <= set(SCOPES_SUPPORTED):
        raise OAuthError(BAD_SCOPE)


@router.post(TOKEN_PATH, include_in_schema=False)
async def token(request: Request) -> JSONResponse:
    await _within_limit(request)
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
                redirect_uri=_optional(form, "redirect_uri"),
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
    await _within_limit(request)
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

En `frontend/src/i18n/en/auditVocabulary.ts`, dentro de `auditActions`, después de `mcp_app_revoked` (las dos de
encima son las del comentario «Worded by hand»; estas cuatro son la derivación de `humaniseEnum`):

```ts
  mcp_code_reused: "MCP code reused",
  mcp_grant_ended: "MCP grant ended",
  mcp_grant_revoked: "MCP grant revoked",
  mcp_refresh_reused: "MCP refresh reused",
```

y en `frontend/src/i18n/es/auditVocabulary.ts`, después de `mcp_app_revoked`:

```ts
  mcp_code_reused: "Código de autorización de MCP reutilizado",
  mcp_grant_ended: "Concesión de MCP terminada: la persona ya no está activa",
  mcp_grant_revoked: "Concesión de MCP revocada por la aplicación",
  mcp_refresh_reused: "Token de renovación de MCP reutilizado",
```

- [ ] **Step 5: El recuento de logs**

**La base, la que tenga la worktree** (re-revisión f3-2, M-8). Sea N el número de `assert total == N` en
`backend/tests/test_logging_discipline.py` al empezar la tarea, **pero no se fía de lo que muestre la worktree**: la Task 7 corre después de fusionar la fase 2 (oleada 3), cuya Task 5 y la Task 3 cambiaron esa línea igual (108 → 109) y git las unió sin avisar. La base es 108 + 1 (T5 de la fase 2) + 1 (Task 3) = **110** si las dos están en la rama; si el `assert` dice 109, se corrige a mano a 110 (y su «expected») y se comprueba que el titular del párrafo de la Task 3 dice 110 antes de seguir; con otro orden de fusión, 108 más el delta de cada una que esté. El párrafo nuevo va **encima del párrafo «NOW N» cuyo N es el del `assert`** (en `3a1ba1c`, «NOW 108, own authentication
phase 1, fix 3 review Minor 2», `:441` de la prueba y `:178` del diseño; no el «NOW 65» de `:334` ni el «NOW 70» de `:135`, que
son los de más arriba) del docstring de la prueba del recuento, que pasa a «BEFORE THAT, …» (sin cambiar nada más de él); el titular del nuevo y
el `assert` (y el «expected N» de su mensaje) dicen **N+1** (abajo, 110, con N = 109, la rama sin la fase 2; con ella, N = 110 y todo número de abajo gana +1). Lo mismo en el §2.2 de
`docs/superpowers/specs/2026-08-14-logging-kernel-design.md`: el párrafo de abajo va encima del «**NOW N**» cuyo N es el
del `assert` (el mismo párrafo nombrado arriba), que pasa a «**BEFORE THAT, …**», con el mismo número en su titular. El delta de esta tarea es **+1**.

En el docstring de la prueba del recuento:

```text
    NOW 110, own authentication phase 3 (plan 2026-11-06, Task 7): one static
    WARNING in `app/core/auth/oauth/grants.py::_exchange`, "authorization code
    refused and spent", when a live code arrives with another app, another
    redirect or a wrong PKCE verifier: the code is used up (RFC 6749 §10.5,
    plan B3-21), and a legitimate client never sends any of the three wrong,
    so the line is the trace of a stolen or forged one. No throttle: each line
    costs a code, and each code a signed-in person's approval. `extra` = the
    reason ("client", "redirect" or "verifier") and the presenting app's
    client_id; never the code or the verifier.

```

En el §2.2 del diseño del kernel de logs:

```markdown
**NOW 110, own authentication phase 3 (plan 2026-11-06, Task 7).** One site. A
static WARNING in `app/core/auth/oauth/grants.py::_exchange`, "authorization
code refused and spent", when a live code arrives with another app, another
redirect or a wrong PKCE verifier. The code is used up (RFC 6749 §10.5, plan
B3-21), and a legitimate client never sends any of the three wrong, so the line
is the trace of a stolen or forged code. No throttle: each line costs a code,
and each code a signed-in person's approval. `extra` = the reason (`client`,
`redirect` or `verifier`) and the presenting app's `client_id`; never the code
or the verifier.

```

- [ ] **Step 6: Las pruebas pasan**

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_token.py tests/test_oauth_schema.py tests/test_i18n_catalog.py tests/test_client_catalog.py tests/test_logging_discipline.py`
Expected: PASS.

Run: `cd frontend && npx vitest run src/modules/audit src/i18n`
Expected: PASS (`auditVocabulary.coverage.test.ts` encuentra las cuatro acciones nuevas con su entrada en los dos
idiomas, y `grants.py` no suma ningún `action=` que no sea literal; `auditVocabulary.english.test.ts`, la derivación).

Run: `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `Success: no issues found` y `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/core/auth/oauth/grants.py backend/app/api/oauth_tokens.py backend/app/main.py \
  backend/tests/oauth_support.py backend/tests/test_oauth_token.py backend/tests/test_client_catalog.py \
  backend/tests/test_logging_discipline.py docs/superpowers/specs/2026-08-14-logging-kernel-design.md \
  frontend/src/i18n/en/auditVocabulary.ts frontend/src/i18n/es/auditVocabulary.ts
git commit -m "feat(oauth): the token and revocation endpoints: one-time codes, rotating refresh families"
```

### Task 8: El consentimiento: `authorize.py`, `consent.py` y `/api/v1/oauth/authorize/{details,decision}`

Diseño §5.2, §2.5, §2.7; D2, D7, D8, D11; B3-05, B3-09, B3-10, B3-15, B3-23; decisión del operador C1. Oleada 4, en
paralelo con la Task 9. Backend.

Las dos rutas que la página de la Task 4 llama con la sesión de la consola. Las dos dicen primero si la persona puede
conectar apps de IA aquí (D11, B3-09), validan la petición de autorización entera (RFC 6749 §4.1.2.1 en sus dos
mitades: lo que no se puede devolver porque la dirección no es de fiar se enseña a la persona; lo demás se enseña con
su host y vuelve a la app solo con un clic), y, al aprobar con el segundo factor recién confirmado, emiten el código de
60 s (Task 7), lo auditan y se lo dicen a la persona por correo.

**Files:**
- Modify: `backend/app/core/auth/oauth/errors.py` (seis `Problem` del punto de autorización, al final)
- Create: `backend/app/core/auth/oauth/authorize.py`
- Create: `backend/app/core/auth/oauth/consent.py`
- Create: `backend/app/api/v1/oauth_consent.py`
- Modify: `backend/app/core/auth/notices.py` (el aviso `app_connected`, C1)
- Modify: `backend/app/main.py` (incluir el router, junto a `session_settings_router`)
- Modify: `backend/app/mcp/exclusions.py` (`NEVER_EXPOSED_REASONS` +1, `NOT_FOR_MCP` +1)
- Modify: `backend/app/api/security_headers.py` (`NO_STORE_ROUTES` +1)
- Modify: `backend/app/core/i18n/es.py`
- Modify: `frontend/src/i18n/en/auditVocabulary.ts`, `frontend/src/i18n/es/auditVocabulary.ts` (`mcp.app_authorized`)
- Modify: `frontend/src/modules/audit/auditVocabulary.english.test.ts` (`HAND_WORDED` +1)
- Modify: `backend/tests/test_mcp_channel_api.py` (`OWN_SIGN_IN_NEVER_EXPOSED`, recuento: el número que veas + 1, 55 → 56 con la fase 2 de `3a214fd`)
- Modify: `backend/tests/test_route_gating.py` (`UNGATED_BY_DESIGN` +2)
- Modify: `backend/tests/test_security_headers.py` (`SECRET_ONCE` +1)
- Modify: `backend/tests/test_client_catalog.py` (`OAUTH_SESSION_USERS` gana `consent.py`)
- Modify: `backend/tests/oauth_support.py` (`authorize_query`, `decide`, `code_of`, `connect` al final)
- Test: `backend/tests/test_oauth_consent.py`

**Interfaces:**
- Consumes: Task 1 (`errors`, `policy.issuer`, `policy.SCOPE`, `redirects.matches`, `redirects.with_params`,
  `redirects.host_of`, `redirects.stays_on_this_computer`, `pkce.valid_challenge`), Task 3 (`cimd.DocumentRefused` y
  su `transient`, `cimd.ClientDocument`, `cimd.MIN_CACHE`), Task 5 (`oauth_session`, `deployment_org`, `ClientView`,
  `touch`), Task 6 (`resolve_client` con su `fetch` y su `record_use`, `clients.Fetch`, `clients.mcp_switched_on`; en
  `tests/oauth_support.py`, `register`), Task 7 (`issue_code`, `names_resource`; en `tests/oauth_support.py`,
  `pkce_pair`, `token_request`). Existentes: `AuthContext` (con `principal`, `email`, `clients`), `bound_auth_context`
  y `require_step_up(ctx)` (`backend/app/api/deps.py:557`: 403 `step_up_required` sin un segundo factor en los
  últimos 10 minutos, `sessions.STEP_UP_WINDOW`), `app.core.auth.guards.sign_in_again()`, `codes_held_anywhere(session,
  user_id)` (`app/core/rbac.py`, como `mcp/server.py::_gate`), `notices.tell` (`backend/app/core/auth/notices.py:210`),
  `request.app.state.vault`, `audit_service.record`, `tr`, `mcp_resource`; en las pruebas, `capture_mail`
  (`tests/auth_support.py:246`), `sign_in`, `remembered_device`, `refusal_code` (`tests/session_support.py`).
- Produces: `authorize.AuthorizationRequest`, `NotRedirectable(detail, *, error)`, `Redirectable(redirect_uri, state, problem)`,
  `parse_query`, `answer(redirect_uri, state, iss, **params) -> str`, `validate(app, *, org_id, query, now,
  fetch=None, record_use=True)`; `consent.details`, `consent.decide`; las rutas `POST /api/v1/oauth/authorize/details` y `…/decision`
  con el contrato del Apéndice A.2 (el de `ConsentDetails` y `ConsentAnswer` de la Task 4); el aviso
  `notices.tell(..., "app_connected", host=...)`; la acción `mcp.app_authorized`; en `tests/oauth_support.py`,
  `authorize_query(client_id, *, challenge, redirect_uri=CLAUDE_CALLBACK, state="st4te", **extra) -> str`, `async def
  decide(env, org_id, sub, query, *, approve=True) -> httpx.Response`, `code_of(redirect_to) -> str`, `async def
  connect(env, org_id, sub, *, client_id=None) -> dict` (registra si no se da `client_id`, aprueba y canjea:
  `{"client_id", "access_token", "refresh_token", …}`).

**Decisión de la tarea (1): sin `resource` es el recurso MCP, y el esquema y el host van en cualquier caja** (B3-05).
RFC 8707 §2.1: «If the client omits the "resource" parameter when requesting authorization, the authorization server
MAY process the request with no specific resource or by using a predefined default resource value.» Este servidor
emite tokens para un solo recurso, así que tomar su ausencia por él no ensancha nada; uno presente se compara con
`grants.names_resource` (MCP: «SHOULD accept uppercase scheme and host components»), y uno distinto es
`invalid_target`. El código guarda siempre el recurso canónico.

**Decisión de la tarea (2): aprobar pide el segundo factor confirmado en los últimos 10 minutos, también en un equipo
recordado, y avisa por correo** (decisión del operador C1, 2026-11-06). Aprobar entrega a la app toda la autoridad de
la persona (`CLAUDE.md` 1c) mientras la app se use. La ruta llama a `require_step_up(ctx)` antes de decidir cuando
`approve` es verdadero: 403 `step_up_required`, que la página de la Task 4 contesta con el diálogo de la fase 1 y
reintenta una vez. Una sesión que empezó en un equipo recordado no tiene `step_up_at` (`sessions.start_session`), así
que también lo pide. Rechazar no lo pide. Después de aprobar, la persona recibe «Has conectado <host de la app>» («You
connected <host>»), con cómo revocarla; el host es el que encabeza la pantalla (el del `client_id` para un documento
CIMD, el del redirect para una app registrada; B3-10). El aviso es **el último acto** de la transacción (un correo no
se deshace; `tests/test_auth_notice_order.py` explica por qué). El diseño §2.5 y §2.7 se enmiendan en la Task 12.

**Decisión de la tarea (3): un rechazo no se audita; una aprobación sí.** §7: «consentimientos y revocaciones de
clientes de IA». Rechazar no da nada a nadie.

**Decisión de la tarea (4): quién puede conectar se mira antes de leer la petición, y mientras no pueda no se descarga
nada** (A3-31, B3-09). Primero: la sesión es de la organización del despliegue (`deployment_org`, la misma para la que
emite el canje; con varias organizaciones el consentimiento y el canje caerían en dos distintas), MCP está encendido
allí y la persona tiene `mcp.use`. Si no, la petición se comprueba solo contra lo guardado (`fetch=_not_read`): una
app registrada o un documento CIMD ya guardado dan el redirect y la respuesta es `back` (`access_denied`, con el host
y el botón); un documento que habría que descargar no se descarga, y la respuesta es `refused`, sin camino de vuelta.
Y no se escribe nada (`record_use=False`, re-revisión f3-2, M-3): sin él, cualquiera que no puede conectar —sin
`mcp.use`, con MCP apagado o de otra organización— abriría un enlace de consentimiento y mantendría viva una app
inactiva (`last_used_at`, que decide su retención en la Task 13).

**Decisión de la tarea (5): solo la aprobación navega sola** (B3-15, A3-19). `details` nunca responde `redirect`. Un
error que la app debe oír (`reason: "error"`), un rechazo (`"declined"`) o una persona que no puede conectar
(`"denied"`) son `back`, con el host y la URL que la página sigue cuando la persona pulsa «Volver a <host>». RFC 9700
§4.11.2: «SHOULD only automatically redirect the user agent if it trusts the redirection URI»; con registro abierto
ninguno es de fiar.

**Decisión de la tarea (6): `redirect_uri` es opcional si la app registró uno solo** (B3-23, A3-46). OAuth 2.1-17
§4.1.1: «"redirect_uri": OPTIONAL if only one redirect URI is registered for this client. REQUIRED if multiple
redirect URIs are registered for this client.» Sin él, el código queda atado al único registrado.

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


async def decide(
    env: Any, org_id: UUID, sub: str, query: str, *, approve: bool = True
) -> httpx.Response:
    """The consent page's decision, with the person's console session (its
    second factor just confirmed: `ApiEnv.headers`)."""
    return await env.client.post(
        "/api/v1/oauth/authorize/decision",
        json={"query": query, "approve": approve},
        headers=await env.headers(org_id, sub),
    )


def code_of(redirect_to: str) -> str:
    from urllib.parse import parse_qs, urlsplit

    return parse_qs(urlsplit(redirect_to).query)["code"][0]


async def connect(
    env: Any, org_id: UUID, sub: str, *, client_id: str | None = None
) -> dict[str, Any]:
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

- who may connect is asked first: the deployment's organization, MCP on,
  `mcp.use` (plan D11, B3-09); while they may not, no document is read;
- the request is validated in RFC 6749 §4.1.2.1's two halves: a bad client or
  redirect is shown and sent nowhere; an error the app should hear, a decline
  and a person who may not connect are shown with their host and go back only
  on a click (B3-15); only an approval is a `redirect`;
- the consent names the app, the host its answer goes to, the host of its
  metadata document, and warns when it only returns to this computer [S30];
- approving needs the second factor confirmed in the last ten minutes, on a
  remembered device too, issues a 60-second code the token endpoint accepts,
  is audited `mcp.app_authorized` and tells the person by mail (C1);
  declining issues, audits and mails nothing;
- the routes need the console's own session: no cookie, no MCP, no CSRF.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.oauth import cimd
from app.core.auth.oauth.store import oauth_session
from app.models import AuditEvent
from app.models.auth import AuthSession
from app.models.oauth import OAuthClient, OAuthCode
from tests.auth_support import capture_mail
from tests.mcp_support import marked_client
from tests.oauth_support import (
    BASE,
    CLAUDE_CALLBACK,
    authorize_query,
    code_of,
    decide,
    oauth_org,
    pkce_pair,
    seed_client,
    token_request,
)
from tests.session_support import refusal_code, remembered_device, sign_in

DOCUMENT_URL = "https://claude.example/oauth/client-metadata.json"
CLAUDE_REDIRECT = "redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback&"
MALFORMED = "this AI app's request is malformed; going back tells the app what was wrong"


class Env:
    def __init__(self, api: Any, org_id: UUID, sub: str) -> None:
        self.api, self.org_id, self.sub = api, org_id, sub


@pytest.fixture()
async def env(api_env, monkeypatch) -> Env:
    org_id, _ = await oauth_org(api_env)
    api_env.settings.mcp_registration = "open"
    api_env.mail = capture_mail(monkeypatch)
    sub = await api_env.create_user(org_id, "org-admin")
    return Env(api_env, org_id, sub)


async def _details(env: Env, query: str, sub: str | None = None, org_id: UUID | None = None) -> Any:
    org = org_id or env.org_id
    return await env.api.client.post(
        "/api/v1/oauth/authorize/details",
        json={"query": query},
        headers=await env.api.headers(org, sub or env.sub),
    )


def _params(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


async def _registered(env: Env, *uris: str) -> Any:
    return await seed_client(env.api, env.org_id, redirect_uris=uris or (CLAUDE_CALLBACK,))


async def _codes(env: Env) -> list[OAuthCode]:
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        return list((await session.execute(select(OAuthCode))).scalars())


async def _authorized(env: Env) -> list[AuditEvent]:
    async with AsyncSession(env.api.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == env.org_id, AuditEvent.action == "mcp.app_authorized"
                    )
                )
            ).scalars()
        )


def _document(client_id: str) -> cimd.ClientDocument:
    assert client_id == DOCUMENT_URL
    return cimd.ClientDocument(
        client_id=DOCUMENT_URL,
        client_name="Claude Code",
        redirect_uris=("http://localhost/callback",),
        cache_for=cimd.MIN_CACHE,
    )


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


@pytest.mark.parametrize(
    "uris", [("http://127.0.0.1/callback", "http://localhost/callback"), ("https://localhost/cb",)]
)
async def test_an_app_that_only_returns_to_this_computer_is_marked(
    env: Env, uris: tuple[str, ...]
) -> None:
    """Whatever the scheme (A3-05): the answer stays on this machine."""
    app = await _registered(env, *uris)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge, redirect_uri=uris[0])
    body = (await _details(env, query)).json()
    assert body["client"]["loopback_only"] is True


async def test_a_metadata_document_client_names_its_host(env: Env, monkeypatch) -> None:
    async def document(client_id: str) -> cimd.ClientDocument:
        return _document(client_id)

    monkeypatch.setattr(cimd, "fetch_document", document)
    _, challenge = pkce_pair()
    query = authorize_query(
        DOCUMENT_URL, challenge=challenge, redirect_uri="http://localhost:6123/callback"
    )
    client = (await _details(env, query)).json()["client"]
    assert client["kind"] == "metadata_document"
    assert client["client_host"] == "claude.example"
    assert client["name"] == "Claude Code"


@pytest.mark.parametrize("resource", [None, "HTTPS://ITMS.EXAMPLE.COM/mcp"])
async def test_a_request_with_no_resource_or_a_capitalised_one_is_for_the_mcp_server(
    env: Env, resource: str | None
) -> None:
    """RFC 8707 §2.1's default resource and MCP's uppercase scheme and host
    (Task 8, decision 1)."""
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge).replace(
        "&resource=https%3A%2F%2Fitms.example.com%2Fmcp", ""
    )
    if resource is not None:
        query += "&resource=" + resource.replace(":", "%3A").replace("/", "%2F")
    assert (await _details(env, query)).json()["outcome"] == "consent"


async def test_with_one_registered_redirect_the_request_may_leave_it_out(env: Env) -> None:
    """OAuth 2.1-17 §4.1.1 (Task 8, decision 6)."""
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge).replace(CLAUDE_REDIRECT, "")
    assert (await _details(env, query)).json()["outcome"] == "consent"
    body = (await decide(env.api, env.org_id, env.sub, query)).json()
    assert body["redirect_to"].startswith(CLAUDE_CALLBACK + "?code=")


# --------------------------------------------------------------------------- #
# RFC 6749 §4.1.2.1: shown, never sent back
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "case",
    [
        "no-client",
        "unknown-client",
        "two-clients",
        "no-redirect",
        "unregistered-redirect",
        "two-redirects",
    ],
)
async def test_a_bad_client_or_redirect_is_shown_and_never_redirected(env: Env, case: str) -> None:
    app = await _registered(env, CLAUDE_CALLBACK, "http://127.0.0.1/callback")
    _, challenge = pkce_pair()
    good = authorize_query(app.client_id, challenge=challenge)
    query = {
        "no-client": good.replace(f"client_id={app.client_id}&", ""),
        "unknown-client": authorize_query("itms_nobody", challenge=challenge),
        "two-clients": good + f"&client_id={app.client_id}",
        "no-redirect": good.replace(CLAUDE_REDIRECT, ""),  # two registered: required
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
    assert await _codes(env) == []


async def test_an_unreadable_metadata_document_is_shown(env: Env, monkeypatch) -> None:
    async def refused(client_id: str) -> cimd.ClientDocument:
        raise cimd.DocumentRefused("mismatch")

    monkeypatch.setattr(cimd, "fetch_document", refused)
    _, challenge = pkce_pair()
    body = (await _details(env, authorize_query(DOCUMENT_URL, challenge=challenge))).json()
    assert body["outcome"] == "refused"
    assert body["error"] == "invalid_client"
    assert "claude.example" in body["detail"]


async def test_a_document_that_cannot_be_read_just_now_says_to_try_again(
    env: Env, monkeypatch
) -> None:
    """CIMD-03 §6.1: a transient failure is not a verdict on the app."""

    async def down(client_id: str) -> cimd.ClientDocument:
        raise cimd.DocumentRefused("unavailable")

    monkeypatch.setattr(cimd, "fetch_document", down)
    _, challenge = pkce_pair()
    body = (await _details(env, authorize_query(DOCUMENT_URL, challenge=challenge))).json()
    assert (body["outcome"], body["error"]) == ("refused", "temporarily_unavailable")
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
# What the app must hear: shown with its host, sent on a click (B3-15)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"code_challenge": "short"}, "invalid_request"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"resource": "https://itms.example.com"}, "invalid_target"),
        ({"resource": "https://itms.example.com/MCP"}, "invalid_target"),
    ],
)
async def test_a_bad_request_is_shown_with_where_it_goes_and_never_followed(
    env: Env, change: dict[str, str], error: str
) -> None:
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge, **change)
    for route in ("details", "decision"):
        resp = await env.api.client.post(
            f"/api/v1/oauth/authorize/{route}",
            json={"query": query, **({"approve": True} if route == "decision" else {})},
            headers=await env.api.headers(env.org_id, env.sub),
        )
        body = resp.json()
        assert (body["outcome"], body["reason"], body["redirect_host"]) == (
            "back",
            "error",
            "claude.ai",
        )
        assert body["detail"] == MALFORMED
        assert body["redirect_to"].startswith(CLAUDE_CALLBACK + "?")
        params = _params(body["redirect_to"])
        assert params["error"] == [error]
        assert params["state"] == ["st4te"]
        assert params["iss"] == [BASE]
        assert "code" not in params
    assert await _codes(env) == []


async def test_a_missing_method_is_plain_and_refused(env: Env) -> None:
    """RFC 7636 §4.3: no `code_challenge_method` means "plain"."""
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge).replace(
        "&code_challenge_method=S256", ""
    )
    params = _params((await _details(env, query)).json()["redirect_to"])
    assert params["error"] == ["invalid_request"]


async def test_a_parameter_twice_goes_back_without_a_state_it_cannot_trust(env: Env) -> None:
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge) + "&state=other"
    params = _params((await _details(env, query)).json()["redirect_to"])
    assert params["error"] == ["invalid_request"]
    assert "state" not in params


# --------------------------------------------------------------------------- #
# Who may connect (plan D11, B3-09), asked before the request is read
# --------------------------------------------------------------------------- #


async def test_with_mcp_off_the_person_is_told_and_offered_the_way_back(api_env) -> None:
    org_id, _ = await oauth_org(api_env, mcp_enabled=False)
    sub = await api_env.create_user(org_id, "org-admin")
    app = await seed_client(api_env, org_id)
    _, challenge = pkce_pair()
    env = Env(api_env, org_id, sub)
    body = (await _details(env, authorize_query(app.client_id, challenge=challenge))).json()
    assert (body["outcome"], body["reason"], body["redirect_host"]) == (
        "back",
        "denied",
        "claude.ai",
    )
    assert body["detail"].startswith("MCP is turned off for this organization.")
    assert _params(body["redirect_to"])["error"] == ["access_denied"]
    query = authorize_query(app.client_id, challenge=challenge)
    approved = (await decide(api_env, org_id, sub, query)).json()
    assert (approved["outcome"], approved["reason"]) == ("back", "denied")


async def test_with_mcp_off_no_metadata_document_is_read_or_stored(api_env, monkeypatch) -> None:
    """A3-31: a person who may not connect makes the server fetch nothing."""

    async def never(client_id: str) -> cimd.ClientDocument:
        raise AssertionError("a document was read while MCP is off")

    monkeypatch.setattr(cimd, "fetch_document", never)
    org_id, _ = await oauth_org(api_env, mcp_enabled=False)
    sub = await api_env.create_user(org_id, "org-admin")
    env = Env(api_env, org_id, sub)
    _, challenge = pkce_pair()
    body = (await _details(env, authorize_query(DOCUMENT_URL, challenge=challenge))).json()
    assert (body["outcome"], body["error"]) == ("refused", "access_denied")
    assert body["detail"].startswith("MCP is turned off for this organization.")
    assert "redirect_to" not in body
    async with oauth_session(org_id, api_env.app_engine) as session:
        assert (await session.execute(select(OAuthClient))).scalars().all() == []


async def test_without_mcp_use_the_person_is_told(env: Env) -> None:
    technician = await env.api.create_user(env.org_id, "technician")
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge)
    body = (await _details(env, query, sub=technician)).json()
    assert (body["outcome"], body["reason"]) == ("back", "denied")
    assert body["detail"] == "requires mcp.use"


async def test_a_person_of_another_organization_cannot_connect_here(env: Env) -> None:
    """B3-09: the code would be redeemed in the deployment's organization."""
    other, _ = await oauth_org(env.api, "other")
    env.api.settings.default_org_slug = await env.api.org_slug(env.org_id)
    stranger = await env.api.create_user(other, "org-admin")
    app = await seed_client(env.api, other)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge)
    body = (await _details(env, query, sub=stranger, org_id=other)).json()
    assert (body["outcome"], body["reason"]) == ("back", "denied")
    assert body["detail"] == (
        "AI apps connect to this installation's own organization, and you are signed in to "
        "another one"
    )
    approved = (await decide(env.api, other, stranger, query)).json()
    assert (approved["outcome"], approved["reason"]) == ("back", "denied")
    async with oauth_session(other, env.api.app_engine) as session:
        assert (await session.execute(select(OAuthCode))).scalars().all() == []


async def test_a_person_who_may_not_connect_keeps_no_idle_app_alive(env: Env) -> None:
    """Re-review f3-2, M-3: without `mcp.use`, neither the question nor the
    answer writes the app's `last_used_at`, which decides its retention."""
    technician = await env.api.create_user(env.org_id, "technician")
    long_ago = datetime.now(UTC) - timedelta(days=20)
    app = await seed_client(env.api, env.org_id, last_used_at=long_ago)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge)
    asked = (await _details(env, query, sub=technician)).json()
    answered = (await decide(env.api, env.org_id, technician, query)).json()
    assert (asked["outcome"], asked["reason"]) == ("back", "denied")
    assert (answered["outcome"], answered["reason"]) == ("back", "denied")
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        row = await session.get(OAuthClient, app.id)
        assert row is not None and row.last_used_at == long_ago


async def test_a_document_app_is_approved_and_redeemed_with_registration_closed(
    env: Env, monkeypatch
) -> None:
    """Operator decision C2 at the consent route: a client metadata document
    needs no `ITS_MCP_REGISTRATION`, only `POST /oauth/register` does
    (re-review f3-2, M-6)."""
    env.api.settings.mcp_registration = ""

    async def document(client_id: str) -> cimd.ClientDocument:
        return _document(client_id)

    monkeypatch.setattr(cimd, "fetch_document", document)
    verifier, challenge = pkce_pair()
    redirect = "http://localhost:6123/callback"
    query = authorize_query(DOCUMENT_URL, challenge=challenge, redirect_uri=redirect)
    body = (await decide(env.api, env.org_id, env.sub, query)).json()
    assert body["outcome"] == "redirect", body
    token = await token_request(
        env.api,
        grant_type="authorization_code",
        code=code_of(body["redirect_to"]),
        redirect_uri=redirect,
        client_id=DOCUMENT_URL,
        code_verifier=verifier,
    )
    assert token.status_code == 200, token.text


# --------------------------------------------------------------------------- #
# The decision
# --------------------------------------------------------------------------- #


async def test_approving_issues_a_code_the_token_endpoint_accepts(env: Env) -> None:
    app = await _registered(env)
    verifier, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge)
    resp = await decide(env.api, env.org_id, env.sub, query)
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
    (event,) = await _authorized(env)
    assert event.actor_type == "user"
    assert event.meta["app"] == app.client_id
    assert event.meta["redirect_host"] == "claude.ai"
    assert code_of(body["redirect_to"]) not in str(event.meta)


async def test_approving_needs_the_second_factor_confirmed_in_the_last_ten_minutes(
    env: Env,
) -> None:
    """Operator decision C1 (2026-11-06): on a remembered device too, and a
    confirmation eleven minutes old is not recent. Declining needs none."""
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge)
    device = await remembered_device(env.api, env.org_id, env.sub)
    remembered = await sign_in(env.api, env.org_id, env.sub, trusted_device_id=device)
    stale = await sign_in(env.api, env.org_id, env.sub, step_up=True)
    async with AsyncSession(env.api.admin_engine) as session:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.id == stale.session_id)
            .values(step_up_at=datetime.now(UTC) - timedelta(minutes=11))
        )
        await session.commit()
    for signed in (remembered, stale):
        refused = await env.api.client.post(
            "/api/v1/oauth/authorize/decision",
            json={"query": query, "approve": True},
            headers=signed.headers,
        )
        assert refused.status_code == 403, refused.text
        assert refusal_code(refused) == "step_up_required"
        declined = await env.api.client.post(
            "/api/v1/oauth/authorize/decision",
            json={"query": query, "approve": False},
            headers=signed.headers,
        )
        assert (declined.json()["outcome"], declined.json()["reason"]) == ("back", "declined")
    assert await _codes(env) == []
    assert await _authorized(env) == []
    assert env.api.mail.sent == []


@pytest.mark.parametrize(
    ("language", "subject"), [("en", "You connected claude.ai"), ("es", "Has conectado claude.ai")]
)
async def test_an_approval_tells_the_person_which_app_they_connected(
    env: Env, language: str, subject: str
) -> None:
    """C1: the mail names the host the screen named, never the code."""
    app = await _registered(env)
    _, challenge = pkce_pair()
    resp = await env.api.client.post(
        "/api/v1/oauth/authorize/decision",
        json={"query": authorize_query(app.client_id, challenge=challenge), "approve": True},
        headers={**await env.api.headers(env.org_id, env.sub), "Accept-Language": language},
    )
    assert resp.json()["outcome"] == "redirect"
    (mail,) = env.api.mail.sent
    assert (mail.to, mail.subject) == (f"{env.sub}@test.local", subject)
    assert code_of(resp.json()["redirect_to"]) not in mail.text


async def test_a_document_apps_mail_names_its_documents_host(env: Env, monkeypatch) -> None:
    async def document(client_id: str) -> cimd.ClientDocument:
        return _document(client_id)

    monkeypatch.setattr(cimd, "fetch_document", document)
    _, challenge = pkce_pair()
    query = authorize_query(
        DOCUMENT_URL, challenge=challenge, redirect_uri="http://localhost:6123/callback"
    )
    assert (await decide(env.api, env.org_id, env.sub, query)).json()["outcome"] == "redirect"
    (mail,) = env.api.mail.sent
    assert mail.subject == "You connected claude.example"


async def test_a_loopback_approval_returns_to_the_port_that_asked(env: Env) -> None:
    app = await _registered(env, "http://127.0.0.1/callback")
    _, challenge = pkce_pair()
    query = authorize_query(
        app.client_id, challenge=challenge, redirect_uri="http://127.0.0.1:51004/callback"
    )
    body = (await decide(env.api, env.org_id, env.sub, query)).json()
    assert body["redirect_to"].startswith("http://127.0.0.1:51004/callback?code=")


async def test_declining_is_shown_with_its_host_and_issues_nothing(env: Env) -> None:
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge)
    body = (await decide(env.api, env.org_id, env.sub, query, approve=False)).json()
    assert (body["outcome"], body["reason"], body["redirect_host"]) == (
        "back",
        "declined",
        "claude.ai",
    )
    params = _params(body["redirect_to"])
    assert params["error"] == ["access_denied"]
    assert "code" not in params
    assert await _codes(env) == []
    assert await _authorized(env) == []
    assert env.api.mail.sent == []


# --------------------------------------------------------------------------- #
# Only the console's own session
# --------------------------------------------------------------------------- #


async def test_without_a_session_both_routes_are_401(env: Env) -> None:
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge)
    for route in ("details", "decision"):
        resp = await env.api.client.post(
            f"/api/v1/oauth/authorize/{route}",
            json={"query": query, **({"approve": True} if route == "decision" else {})},
            headers={"X-ITMS-Request": "1", "Origin": BASE},
        )
        assert resp.status_code == 401, (route, resp.text)


async def test_a_decision_without_the_consoles_header_is_csrf(env: Env) -> None:
    app = await _registered(env)
    _, challenge = pkce_pair()
    headers = {
        k: v
        for k, v in (await env.api.headers(env.org_id, env.sub)).items()
        if k != "X-ITMS-Request"
    }
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

y `assert len(NEVER_EXPOSED) == …` pasa al número que veas + 1 (55 → 56 con la fase 2 de `3a214fd`: la Task 8 corre con la fase 2 en la rama; delta **+1** sobre la base que vea esta tarea).

En `backend/tests/test_route_gating.py`, `UNGATED_BY_DESIGN` gana, después de `("DELETE", "/api/v1/me/sessions/{session_id}")`:

```python
        # THE CONSENT SCREEN (P2 phase 3, plan 2026-11-06 Task 8): the caller's
        # own console session (`oauth_consent.py::_console_ctx`); the
        # deployment's organization, `mcp.use` and the org switch are checked
        # in the service (`consent._refusal`, plan D11), so a person without
        # them is told so and offered the way back to the AI app with
        # `access_denied` instead of a 403 that could not be.
        ("POST", "/api/v1/oauth/authorize/details"),
        # The same, and the only one that mints anything: a 60-second code for
        # what the person just approved, with their second factor confirmed in
        # the last ten minutes (operator decision C1). Never exposed over MCP.
        ("POST", "/api/v1/oauth/authorize/decision"),
```

En `backend/tests/test_security_headers.py`, `SECRET_ONCE` gana, después de las dos de invitaciones (el motivo de
`NEVER_EXPOSED_REASONS` dice «returns», y `test_every_never_exposed_route_that_returns_a_secret_is_listed` lo exige):

```python
        # Own authentication phase 3: an AI app's one-time authorization code.
        ("POST", "/api/v1/oauth/authorize/decision"),
```

En `backend/tests/test_client_catalog.py`, `OAUTH_SESSION_USERS` gana:

```python
    "app/core/auth/oauth/consent.py": (
        "an approval: the person's code, its audit row and its notice in one transaction"
    ),
```

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_consent.py tests/test_mcp_channel_api.py tests/test_route_gating.py tests/test_security_headers.py`
Expected: FAIL (`404` en las rutas; `NEVER_EXPOSED` con el número que veas, sin el +1 (55 con la fase 2 de `3a214fd`); `UNGATED_BY_DESIGN` no casa con el router; `NO_STORE_ROUTES`
no es `SECRET_ONCE`).

- [ ] **Step 2: Los errores del punto de autorización**

Al final de `backend/app/core/auth/oauth/errors.py` (seis):

```python
# The authorization endpoint's (RFC 6749 §4.1.2.1), sent back to the app in
# the redirect when the person clicks (Task 8).
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
§5.2; plan D2, D8, B3-05, B3-23).

The console page `/oauth/authorize` passes the request's query here exactly
as the browser has it (Task 4). It is validated on EVERY call -- the details
and the decision both -- in RFC 6749 §4.1.2.1's two halves:

- what cannot be sent back because the address to send it to is not to be
  trusted: no client, an unknown client, a metadata document that cannot be
  this client (or cannot be read just now), no redirect where the client
  registered several, or one it did not register ("MUST NOT automatically
  redirect the user-agent to the invalid redirection URI").
  `NotRedirectable`, worded for the person;
- everything else is an error the app must hear: `Redirectable`, with the
  redirect, the state to carry and RFC 6749's problem. The consent service
  shows it with its host and sends it, with `state` (§4.1.2) and `iss` (RFC
  9207 §2: "including error responses"), only on the person's click (plan
  B3-15).

WHAT IS CHECKED: `client_id` and `redirect_uri` once each; the redirect one
the app registered (`redirects.matches`: exact, but for a loopback port), or
none when it registered exactly one (OAuth 2.1-17 §4.1.1: "OPTIONAL if only
one redirect URI is registered for this client. REQUIRED if multiple redirect
URIs are registered for this client"; plan B3-23); then no other parameter
twice (§3.1: "MUST NOT be included more than once"), `resource` excepted (RFC
8707 §2 lets it repeat); `response_type=code`; `code_challenge` of 43
base64url characters WITH `code_challenge_method=S256` said out loud (RFC 7636
§4.3: a missing method "defaults to "plain""; §4.4.1: `invalid_request`);
every `resource` the MCP resource, its scheme and host in any case
(`grants.names_resource`), and none meaning it (RFC 8707 §2.1: "MAY process
the request ... by using a predefined default resource value"; plan B3-05).
`scope` is ignored beyond `mcp` (plan D7): the token's scope is `mcp`
whatever was asked.

`fetch` and `record_use` are `resolve_client`'s: while the person may not
connect AI apps the consent passes a `fetch` that reads nothing and
`record_use=False`, so the question neither downloads a document nor keeps
an idle app alive (Task 8, decision 4; re-review f3-2, M-3).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from app.core.auth.oauth import cimd
from app.core.auth.oauth.clients import ClientView, Fetch, resolve_client
from app.core.auth.oauth.errors import (
    AUTHZ_CHALLENGE,
    AUTHZ_METHOD,
    AUTHZ_REPEATED,
    AUTHZ_RESPONSE_TYPE,
    BAD_TARGET,
    Problem,
)
from app.core.auth.oauth.grants import names_resource
from app.core.auth.oauth.pkce import valid_challenge
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
    """Shown to the person; never sent to the redirect. `detail` is a
    translated sentence; `error` RFC 6749's code (or `temporarily_unavailable`),
    a keyword so the i18n guard reads only the sentence."""

    def __init__(self, detail: str, *, error: str) -> None:
        super().__init__(error)
        self.error = error
        self.detail = detail


class Redirectable(Exception):
    """An error the app must hear: the redirect it goes to, the state to
    carry (None when the request's own cannot be trusted) and the problem."""

    def __init__(self, redirect_uri: str, state: str | None, problem: Problem) -> None:
        super().__init__(problem.error)
        self.redirect_uri = redirect_uri
        self.state = state
        self.problem = problem


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


async def validate(
    app: Any,
    *,
    org_id: UUID,
    query: str,
    now: datetime,
    fetch: Fetch | None = None,
    record_use: bool = True,
) -> AuthorizationRequest:
    resource = mcp_resource(app.state.settings)
    assert resource is not None  # consent._ready refused it otherwise
    params = parse_query(query)

    def once(name: str) -> str | None:
        values = params.get(name, [])
        return values[0] if len(values) == 1 else None

    if len(params.get("client_id", [])) > 1 or len(params.get("redirect_uri", [])) > 1:
        raise NotRedirectable(
            tr("this connection request names its AI app or its return address twice"),
            error="invalid_request",
        )
    client_id = once("client_id")
    if client_id is None:
        raise NotRedirectable(
            tr("this connection request names no AI app"), error="invalid_request"
        )
    try:
        client = await resolve_client(
            app, org_id=org_id, client_id=client_id, now=now, fetch=fetch, record_use=record_use
        )
    except cimd.DocumentRefused as exc:
        if exc.transient:
            raise NotRedirectable(
                tr(
                    "this AI app's description could not be read from {host} just now; try "
                    "again in a moment",
                    host=_host(client_id),
                ),
                error="temporarily_unavailable",
            ) from exc
        raise NotRedirectable(
            tr("this AI app's description could not be read from {host}", host=_host(client_id)),
            error="invalid_client",
        ) from exc
    if client is None:
        raise NotRedirectable(
            tr(
                "this AI app is not registered here, or its registration expired. Connect it "
                "again from the app."
            ),
            error="invalid_client",
        )
    redirect_uri = once("redirect_uri")
    if redirect_uri is None:
        if len(client.redirect_uris) != 1:
            raise NotRedirectable(
                tr(
                    "this connection request names no return address, and its AI app "
                    "registered more than one"
                ),
                error="invalid_request",
            )
        redirect_uri = client.redirect_uris[0]
    elif not matches(client.redirect_uris, redirect_uri):
        raise NotRedirectable(
            tr("the address this AI app asked to go back to is not one it registered"),
            error="invalid_redirect_uri",
        )

    # From here on the answer is the app's to hear.
    state = once("state")
    if any(len(values) > 1 for name, values in params.items() if name not in _REPEATABLE):
        raise Redirectable(redirect_uri, None, AUTHZ_REPEATED)  # two states: trust neither
    if once("response_type") != "code":
        raise Redirectable(redirect_uri, state, AUTHZ_RESPONSE_TYPE)
    challenge = once("code_challenge")
    if challenge is None or not valid_challenge(challenge):
        raise Redirectable(redirect_uri, state, AUTHZ_CHALLENGE)
    if once("code_challenge_method") != "S256":
        raise Redirectable(redirect_uri, state, AUTHZ_METHOD)
    if not all(names_resource(value, resource) for value in params.get("resource", [])):
        raise Redirectable(redirect_uri, state, BAD_TARGET)
    return AuthorizationRequest(
        client=client,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=challenge,
        resource=resource,
    )
```

- [ ] **Step 4: El aviso «Has conectado <host>»**

En `backend/app/core/auth/notices.py`: `"app_connected",` al final de `Notice`; `_compose` gana el parámetro
`host: str | None` (después de `left`) y, antes de su último `return`, esta rama:

```python
    if kind == "app_connected":
        return (
            tr("You connected {host}", host=host),
            tr(
                "On {when} you approved {host} to act for you over MCP, with everything your "
                "{product} account may do. It keeps that access for as long as it is used, until "
                "you revoke it under Settings → AI connections.\n\n"
                "If it was not you, revoke it there now and tell your administrator.",
                when=when,
                host=host,
                product=PRODUCT,
            ),
        )
```

y `tell` gana `host: str | None = None` (después de `left`) y lo pasa: `_compose(kind, factor=factor, days=days,
left=left, host=host)`.

- [ ] **Step 5: `consent.py`**

`backend/app/core/auth/oauth/consent.py`:

```python
"""The consent screen's two questions (design §5.2, §2.5, §2.7; plan D11,
B3-09, B3-10, B3-15; operator decision C1).

`details`: what the person is asked, or why there is nothing to ask.
`decide`: their answer. Both say FIRST whether this person may connect AI
apps here at all, before the request is read (A3-31): the session's
organization is the deployment's (`deployment_org`, the one the token
endpoint redeems codes in; B3-09), MCP is switched on there and the person
holds `mcp.use` on some client -- what `app/mcp/server.py::_gate` checks on
every MCP call. While they may not, the request is checked against what is
already stored and no client metadata document is read (`_not_read`).

THE ANSWERS (Appendix A.2; the console page, Task 4):
- `consent`: the question;
- `refused`: shown and sent nowhere -- a bad client or redirect (RFC 6749
  §4.1.2.1), a person refused before the redirect could be trusted, or
  `temporarily_unavailable` when a metadata document cannot be read just now;
- `back`: what the app must hear -- an error, a decline, a person who may not
  connect -- with the host it goes to; the page follows it only when the
  person clicks (RFC 9700 §4.11.2: "SHOULD only automatically redirect the
  user agent if it trusts the redirection URI"; B3-15);
- `redirect`: only an approval, whose redirect carries the code.

AN APPROVAL needs the second factor confirmed in the last ten minutes (the
route's `require_step_up`; C1). It issues a 60-second code (Task 7's
`issue_code`) bound to the app, the exact redirect, the PKCE challenge and
the resource; it is audited `mcp.app_authorized` (§7), naming the app and
the host it returns to, never the code; and, as the LAST act of the
transaction (mail does not roll back), the person is told "You connected
<host>" (`notices.tell(..., "app_connected")`). Declining issues nothing and
is neither audited nor mailed (Task 8, decision 3).

WHAT THE PERSON IS SHOWN (`_client_out`; B3-10): the app's name as it gave
it, the host its answer goes back to (MCP: "MUST clearly display the redirect
URI hostname" [S30]), the host that publishes its metadata document (CIMD-03
§10.10), and whether every address it registered is this computer ([S30]:
"SHOULD display additional warnings for localhost-only redirect URIs").
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from app.core.audit import audit_service
from app.core.auth import notices
from app.core.auth.oauth import cimd
from app.core.auth.oauth.authorize import (
    AuthorizationRequest,
    NotRedirectable,
    Redirectable,
    answer,
    validate,
)
from app.core.auth.oauth.clients import mcp_switched_on, touch
from app.core.auth.oauth.errors import DECLINED, PERSON_CANNOT, Problem
from app.core.auth.oauth.grants import issue_code
from app.core.auth.oauth.policy import SCOPE, issuer
from app.core.auth.oauth.redirects import host_of, stays_on_this_computer
from app.core.auth.oauth.store import deployment_org, oauth_session
from app.core.i18n import tr
from app.core.mcp_channel import mcp_resource
from app.core.rbac import codes_held_anywhere
from app.core.tenancy import org_session
from app.models import User
from app.models.oauth import OAuthClient

if TYPE_CHECKING:
    # The door's context, as the route hands it over; a type only, so the
    # core never imports the API layer at run time.
    from app.api.deps import AuthContext

Answer = dict[str, Any]


def _ready(request: Request) -> str:
    settings = request.app.state.settings
    iss = issuer(settings)
    if iss is None or mcp_resource(settings) is None:
        raise HTTPException(
            503,
            tr(
                "MCP is not available: ITS_PUBLIC_CONSOLE_URL is not an https address without "
                "a path."
            ),
        )
    return iss


async def _not_read(client_id: str) -> cimd.ClientDocument:
    """The fetch while the person may not connect AI apps: nothing is read
    (A3-31). A stored copy still answers (`resolve_client` keeps one on a
    transient failure), so the way back can be offered."""
    raise cimd.DocumentRefused("unavailable")  # i18n-exempt: a reason code


async def _refusal(request: Request, ctx: AuthContext) -> str | None:
    """Why this person may not connect AI apps here, or None (plan D11, B3-09)."""
    if ctx.org_id != await deployment_org(request.app):
        return tr(
            "AI apps connect to this installation's own organization, and you are signed in to "
            "another one"
        )
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        if not await mcp_switched_on(session, ctx.org_id):
            return tr(
                "MCP is turned off for this organization. An org-admin turns it on in "
                "Settings → AI connections."
            )
        if "mcp.use" not in await codes_held_anywhere(session, ctx.user_id):
            return tr("requires {code}", code="mcp.use")
    return None


def _back(
    to: AuthorizationRequest | Redirectable, iss: str, reason: str, detail: str, problem: Problem
) -> Answer:
    return {
        "outcome": "back",
        "reason": reason,
        "detail": detail,
        "redirect_host": host_of(to.redirect_uri),
        "redirect_to": answer(
            to.redirect_uri,
            to.state,
            iss,
            error=problem.error,
            error_description=problem.description,
        ),
    }


async def _question(
    request: Request, ctx: AuthContext, query: str
) -> tuple[str, AuthorizationRequest | Answer]:
    """What both answers share: who may connect, then the request."""
    iss = _ready(request)
    refusal = await _refusal(request, ctx)
    try:
        checked: AuthorizationRequest | Redirectable = await validate(
            request.app,
            org_id=ctx.org_id,
            query=query,
            now=datetime.now(UTC),
            fetch=None if refusal is None else _not_read,
            record_use=refusal is None,
        )
    except NotRedirectable as refused:
        if refusal is not None:
            return iss, {"outcome": "refused", "error": PERSON_CANNOT.error, "detail": refusal}
        return iss, {"outcome": "refused", "error": refused.error, "detail": refused.detail}
    except Redirectable as error:
        checked = error
    if refusal is not None:
        return iss, _back(checked, iss, "denied", refusal, PERSON_CANNOT)
    if isinstance(checked, Redirectable):
        detail = tr("this AI app's request is malformed; going back tells the app what was wrong")
        return iss, _back(checked, iss, "error", detail, checked.problem)
    return iss, checked


def _app_host(checked: AuthorizationRequest) -> str:
    """The host the screen's heading names (B3-10): the metadata document's,
    or the redirect's."""
    client = checked.client
    if client.kind == "metadata_document":
        return urlsplit(client.client_id).hostname or host_of(checked.redirect_uri)
    return host_of(checked.redirect_uri)


def _client_out(checked: AuthorizationRequest) -> Answer:
    client = checked.client
    return {
        "name": client.client_name,
        "client_id": client.client_id,
        "kind": client.kind,
        "client_host": urlsplit(client.client_id).hostname
        if client.kind == "metadata_document"
        else None,
        "redirect_host": host_of(checked.redirect_uri),
        "loopback_only": all(stays_on_this_computer(uri) for uri in client.redirect_uris),
    }


async def details(request: Request, ctx: AuthContext, *, query: str) -> Answer:
    _, checked = await _question(request, ctx, query)
    if isinstance(checked, dict):
        return checked
    return {
        "outcome": "consent",
        "client": _client_out(checked),
        "scope": SCOPE,
        "account": ctx.email,
    }


async def decide(request: Request, ctx: AuthContext, *, query: str, approve: bool) -> Answer:
    """The route has already asked for the recent second factor when
    `approve` (C1)."""
    iss, checked = await _question(request, ctx, query)
    if isinstance(checked, dict):
        return checked
    if not approve:
        detail = tr("you did not connect this AI app; going back tells it so")
        return _back(checked, iss, "declined", detail, DECLINED)
    now = datetime.now(UTC)
    code: str | None = None
    async with oauth_session(ctx.org_id, request.app.state.app_engine) as session:
        client = await session.get(OAuthClient, checked.client.id)
        user = await session.get(User, ctx.user_id)
        if client is not None and user is not None:  # an idle app may be gone by now
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
            # THE LAST ACT of the transaction: mail does not roll back (C1).
            await notices.tell(
                session,
                user,
                "app_connected",
                vault=request.app.state.vault,
                actor_id=ctx.user_id,
                host=_app_host(checked),
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

- [ ] **Step 6: Las rutas**

`backend/app/api/v1/oauth_consent.py`:

```python
"""The consent screen's routes (own authentication design §5.2; plan
2026-11-06, Task 8):

    POST /api/v1/oauth/authorize/details    what an AI app's request asks
    POST /api/v1/oauth/authorize/decision   the person's answer: a code, or access_denied

UNGATED BY DESIGN (`tests/test_route_gating.py::UNGATED_BY_DESIGN`): the
caller's own console session -- not a token (`_console_ctx`: the door must
have admitted a console principal) -- and the deployment's organization,
`mcp.use` and the org switch checked in the service, so a refusal can go back to the AI app
(`consent._refusal`, plan D11). The CSRF rule (`app/core/auth/csrf.py`) holds
both: a POST under `/api/v1` on the session cookie.

NOT THE ASSISTANT'S INTERNAL TOKEN, and not because of `_console_ctx`: that
token yields a console principal too (`app/api/deps.py`). It is kept out
because `verify_loopback` admits it only for GET or HEAD
(`app/core/auth/loopback.py`, its rule 4) and both routes are POST; a request
that says it is the assistant's never falls through to the cookie (re-review
f3-2, M-5).

APPROVING NEEDS A RECENT SECOND FACTOR (operator decision C1, 2026-11-06):
`require_step_up` before the decision when `approve`, on a remembered device
too; declining does not.

NEVER AN AI APP'S (1c): the decision answers a one-time code that becomes the
person's MCP tokens, so it is in `NEVER_EXPOSED` and `NO_STORE_ROUTES`, and
refused on the MCP channel twice; the details are not a tool (`NOT_FOR_MCP`),
and need the console session the channel never has.

THE HANDLERS ARE THIN: the rules live in `app.core.auth.oauth.consent`, the
only caller of `oauth_session` here (`tests/test_client_catalog.py::
OAUTH_SESSION_USERS`).
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AuthContext, bound_auth_context, require_step_up
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
    """The person's answer: back to the app with a one-time code, or shown
    with the way back and `access_denied`."""
    if body.approve:
        require_step_up(ctx)
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

y en `NOT_FOR_MCP`, después de `("DELETE", "/api/v1/me/sessions/{session_id}"): _MY_SECURITY,` (la última de My security):

```python
    # The consent screen's question (P2 phase 3): the person's, in the console
    # with their session (`api/v1/oauth_consent.py::_console_ctx`).
    ("POST", "/api/v1/oauth/authorize/details"): (
        "the consent screen is the person's, asked in the console with their own session"
    ),
```

En `backend/app/api/security_headers.py`, al final de `NO_STORE_ROUTES`:

```python
    ("POST", "/api/v1/oauth/authorize/decision"): "an AI app's one-time authorization code",
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
    "this AI app's description could not be read from {host} just now; try again in a moment": (
        "ahora mismo no se pudo leer la descripción de esta aplicación de IA en {host}; vuelve a "
        "intentarlo dentro de un momento"
    ),
    "this AI app is not registered here, or its registration expired. Connect it again from "
    "the app.": (
        "esta aplicación de IA no está registrada aquí o su registro caducó. Vuelve a "
        "conectarla desde la aplicación."
    ),
    "this connection request names no return address, and its AI app registered more than "
    "one": (
        "esta solicitud de conexión no nombra ninguna dirección de vuelta, y su aplicación de IA "
        "registró más de una"
    ),
    "the address this AI app asked to go back to is not one it registered": (
        "la dirección a la que esta aplicación de IA pidió volver no es una de las que registró"
    ),
    "this AI app's request is malformed; going back tells the app what was wrong": (
        "la solicitud de esta aplicación de IA está mal formada; al volver, la aplicación sabrá "
        "qué falló"
    ),
    "you did not connect this AI app; going back tells it so": (
        "no conectaste esta aplicación de IA; al volver, se le dirá"
    ),
    "AI apps connect to this installation's own organization, and you are signed in to "
    "another one": (
        "las aplicaciones de IA se conectan a la organización de esta instalación, y has "
        "iniciado sesión en otra"
    ),
    "You connected {host}": "Has conectado {host}",
    "On {when} you approved {host} to act for you over MCP, with everything your {product} "
    "account may do. It keeps that access for as long as it is used, until you revoke it "
    "under Settings → AI connections.\n\nIf it was not you, revoke it there now and tell your "
    "administrator.": (
        "El {when} aprobaste que {host} actúe por ti mediante MCP, con todo lo que puede hacer "
        "tu cuenta de {product}. Conserva ese acceso mientras se use, hasta que lo revoques en "
        "Configuración → Conexiones de IA.\n\nSi no fuiste tú, revócalo allí ahora y avisa a tu "
        "administrador."
    ),
```

- [ ] **Step 7: El vocabulario de auditoría**

En `frontend/src/i18n/en/auditVocabulary.ts`, dentro del comentario «Worded by hand» y antes de
`mcp_app_revoke_incomplete` (A3-45: como la pantalla de Ajustes la nombra, una aplicación de IA):

```ts
  mcp_app_authorized: "AI app authorized",
```

en `frontend/src/i18n/es/auditVocabulary.ts`, antes de `mcp_app_revoke_incomplete`:

```ts
  mcp_app_authorized: "Aplicación de IA autorizada",
```

y en `frontend/src/modules/audit/auditVocabulary.english.test.ts`, en `HAND_WORDED`, antes de `mcp_app_revoked`:

```ts
  mcp_app_authorized: "AI app authorized",
```

- [ ] **Step 8: Las pruebas pasan**

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_consent.py tests/test_mcp_channel_api.py tests/test_route_gating.py tests/test_mcp_catalog.py tests/test_mcp_descriptions.py tests/test_ai_tool_surface.py tests/test_i18n_catalog.py tests/test_client_catalog.py tests/test_security_headers.py tests/test_auth_notice_order.py tests/test_oauth_token.py`
Expected: PASS.

Run, si la worktree ya lo tiene: `cd backend && .venv/bin/pytest -q tests/test_auth_phase2_falsifiers.py`
Expected: PASS. Llega con la fase 2 (lo crea su Task 13; re-revisión f3-2, M-7) y recorre **toda** `/api/v1`: a la
sesión de un contacto, cada ruta le responde 403 `portal_only` o está en su lista exacta de excepciones. Las dos
rutas de esta tarea no están en esa lista ni deben entrar: `bound_auth_context` pasa por `_admit_session`, que a un
contacto le responde `portal_only` (la Task 4 de la fase 2). Si la rama todavía no lo trae, la suite completa de la
Task 12 (Step 5, en el árbol fusionado) lo corre.

Run: `cd frontend && npx vitest run src/modules/audit src/i18n src/modules/auth`
Expected: PASS (las pruebas de la Task 4 contra este contrato incluidas).

Run: `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `Success: no issues found` y `All checks passed!`.

- [ ] **Step 9: Commit**

```bash
git add backend/app/core/auth/oauth/errors.py backend/app/core/auth/oauth/authorize.py \
  backend/app/core/auth/oauth/consent.py backend/app/api/v1/oauth_consent.py backend/app/core/auth/notices.py \
  backend/app/main.py backend/app/mcp/exclusions.py backend/app/api/security_headers.py backend/app/core/i18n/es.py \
  backend/tests/oauth_support.py backend/tests/test_oauth_consent.py backend/tests/test_mcp_channel_api.py \
  backend/tests/test_route_gating.py backend/tests/test_security_headers.py backend/tests/test_client_catalog.py \
  frontend/src/i18n/en/auditVocabulary.ts frontend/src/i18n/es/auditVocabulary.ts \
  frontend/src/modules/audit/auditVocabulary.english.test.ts
git commit -m "feat(oauth): the consent screen's routes: ask first, show errors, approve with a fresh factor"
```

### Task 9: La puerta de MCP con el token propio; pruebas MCP re-apuntadas

Diseño §5.3, §6.5, §7; D6; B3-01, B3-11, B3-29. Oleada 4, en paralelo con la Task 8. Backend.

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
  `backend/tests/test_auth_reset_needs_recovery.py`, y `backend/tests/test_step_up_widened.py` si la rama ya tiene la
  Task 14 de la fase 2
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
`users.id`; una persona sin fila es un 401, nunca una fila nueva. El refresco del correo desde el claim (la corrección
de `docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §7.2, y sus falsificadores 13 y 14, que están en
el §7.4) no tiene claim del que leer: la fila es la única fuente, la misma que la regla de dominio consulta, que era lo
que §7.2 pedía («The security decision and the displayed fact come from different sources»). Las cinco pruebas de
`test_me_profile.py` se re-apuntan a eso y una sexta fija el lado de la decisión (Step 6); la Task 12 enmienda §7.2 y
§7.4. **Una llamada MCP tampoco cuenta como entrada** (B3-29): el diseño §7 audita los «inicios de sesión correctos»,
y el de esta persona fue el de la consola; la aprobación de la app ya quedó auditada (`mcp.app_authorized`, Task 8), y
`last_login_at` sigue diciendo cuándo entró en la consola. La Task 12 lo escribe en el runbook.

**Decisión de la tarea (2): las reglas de la cuenta, en el orden de `_admit_session`, con la etiqueta `"mcp"`.** La
puerta de MCP lee la fila como la de la sesión: dominio (no a un contacto), `users.status`, contacto (salvo
`contacts=True`). Se copia el orden y no se comparte el código, para no tocar la rama de la sesión (Puntos de encuentro
con la fase 2). El log de un rechazo dice `door="mcp"` y `sub="user:<id>"`, como la sesión; antes decía `"http"` y el
`sub` de Keycloak.

**Con la fase 2 fusionada, la puerta de la consola y esta difieren para un contacto, y queda así a propósito**
(re-revisión f3-3, X10; de sus dos caminos, explicar por qué no se alcanza). Tras la fase 2, `_admit_session` responde
a un contacto 403 con `code: "portal_only"` y **sin** log (su Task 4); esta puerta, con el orden de la fase 1, escribe
la línea de `log_admission_refusal` y responde un 403 sin código. No se iguala porque la rama no se alcanza: un
contacto no tiene concesión que presentar. La única forma de tener una es aprobar en el consentimiento, y sus dos
rutas pasan por `bound_auth_context`, que a un contacto le responde `portal_only` antes de leer la petición (Task 8,
Step 8, con `tests/test_auth_phase2_falsifiers.py`). Si un día llegara aquí un contacto con un token, no sería un
rechazo ordinario sino una anomalía, y una línea en el log es lo que se quiere; `portal_only`, además, es un código
para la consola, que ningún cliente MCP lee.

**Decisión de la tarea (3): el marcador del canal tiene que nombrar la app del token.** Una llamada en proceso
(`app/mcp/invoke.py`) marca `azp=principal.claims.azp`, ya verificado; `McpAsgi` compara su etiqueta después. La puerta
lo comprueba también (`call.azp != grant.client_id` es el 401 «the MCP channel marker is malformed»), para que el
`mcp_app` de cada evento de auditoría escrito durante la llamada sea el de la concesión.

**Decisión de la tarea (4): `JwksCache` y `deps.jwks` se quedan.** Desde aquí ninguna puerta los llama; los borra la
fase 4 con `app.state.jwks` («Lo que deja», punto 3). Efecto visible (B3-11): la sonda `jwks` ya decía «degradado»
(`empty_cache`) tras cada reinicio hasta la primera validación de un token de Keycloak; desde aquí no hay ninguna, así
que lo dice siempre, hasta que la fase 4 la quite. No está en `WORKER_SAMPLED_PROBES`: no avisa a nadie, y nada se
despliega antes de la fase 4 (regla del operador del 2026-10-31).

**Decisión de la tarea (5): `/mcp` deja de aceptar el token de Keycloak en esta fase, no en la 4** (B3-01). MCP
2026-07-28, Authorization: «MCP servers MUST NOT accept or transit any other tokens» y «MCP servers MUST only accept
tokens that are valid for use with their own resources». Una conexión hecha con Keycloak recibiría un 401
`invalid_token`; la persona quita el conector y lo vuelve a añadir (que la app vuelva a descubrir el emisor ella sola
está SIN VERIFICAR: Anthropic solo documenta el refresh tras un 401). No llega a ningún host: nada se despliega antes
de la fase 4.

**Decisión de la tarea (6): una llamada MCP nunca cuenta como confirmación reciente** (el punto 4 de los «Puntos de
encuentro con la fase 3» del plan de la fase 2; su Task 14, decisión 5). `require_step_up` (`deps.py:557-573`) rechaza
un `AuthContext` sin `principal`, y la rama MCP de `admit()` devuelve su `Admission` sin él (el campo vale `None` por
omisión): la concesión admite a la persona, nunca su segundo factor, y nada en la rama lee la sesión de la consola de
esa persona. Así lo dejó la fase 1 con el token de Keycloak, y así sigue con el propio; el `return` de la rama lo dice
en un comentario. Lo fija `test_mcp_own_tokens.py::test_an_mcp_call_never_counts_as_a_recent_confirmation`: la persona
acaba de confirmar su factor en la consola, y por el canal `POST /api/v1/users/{user_id}/factors/reset`, que lo
pregunta antes de leer nada, sigue respondiendo 403 `step_up_required`, mientras su sesión de la consola pasa la misma
pregunta y llega al 404 de una persona que no existe. (La ruta está en `NOT_FOR_MCP`, así que no es herramienta, pero
una llamada marcada como del canal llega a ella, como en
`tests/test_auth_reset_needs_recovery.py::test_the_mcp_channel_is_refused_too`.) Con la Task 14 de la fase 2 fusionada
dependen de esto las dos herramientas que piden la confirmación solo para una parte (`clients_update` con los tenants
de Microsoft y `orgs_current_update` al encender MCP), y `tests/test_step_up_widened.py` lo prueba por ellas (Step 7).

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
- the grant admits the person, never their second factor: an MCP call
  never counts as a recent confirmation (phase 2's Task 14);
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
from tests.session_support import refusal_code, sign_in, user_id_of

METADATA_URL = f"{MCP_BASE}/.well-known/oauth-protected-resource/mcp"
REFUSED_CHALLENGE = f'Bearer resource_metadata="{METADATA_URL}", error="invalid_token"'
REFUSED_EN = "this access token was refused"
REFUSED_ES = "se rechazó este token de acceso"
REVOKED_EN = "this AI app's access was revoked; connect it again to continue"
MARKER_EN = "the MCP channel marker is malformed"
NO_ISSUER_EN = (
    "MCP is not available: ITS_PUBLIC_CONSOLE_URL is not an https address without a path."
)
CONFIRM_AGAIN = "confirm your second factor again to do this"
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


async def test_an_mcp_call_never_counts_as_a_recent_confirmation(mcp_env) -> None:
    """Phase 2's Task 14 (its plan's "Puntos de encuentro con la fase 3", 4):
    the grant admits the person, never their second factor. They confirmed it
    in the console a moment ago; over MCP, `require_step_up` -- the first
    question `factors/reset` asks, before it reads anyone -- still refuses.
    Give the MCP branch of `admit` a principal with a `step_up_at`, or the
    person's console session, and this call answers what the console's does."""
    org_id, slug, sub = await _admin(mcp_env)
    console = await sign_in(mcp_env, org_id, sub, step_up=True)
    token = await mcp_token(mcp_env, slug, sub)
    reset = f"/api/v1/users/{uuid4()}/factors/reset"
    async with marked_client(mcp_env) as client:
        over_mcp = await client.post(reset, headers={"Authorization": f"Bearer {token}"})
    in_console = await mcp_env.client.post(reset, headers=console.headers)
    assert (over_mcp.status_code, refusal_code(over_mcp)) == (403, "step_up_required")
    assert over_mcp.json()["detail"] == CONFIRM_AGAIN
    # Non-vacuity: the same person's console session gets past the question,
    # to the unknown person the path names.
    assert in_console.status_code == 404, in_console.text


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

    # NO `principal` (Task 9, decision 6; phase 2's Task 14): the grant admits
    # the person, never their second factor, so `require_step_up` refuses every
    # act that asks for one -- as it refused the Keycloak token's.
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
  propio, y con su control de no vacuidad (A3-51: los mismos claims, firmados sin cambiar, entran):

```python
async def test_a_token_with_both_audiences_is_401(mcp_env) -> None:
    org_id, slug = await _org(mcp_env, mcp_enabled=True)
    sub = await mcp_env.create_user(org_id, "org-admin")
    genuine = mcp_claims(await mcp_token(mcp_env, slug, sub))
    claims = {**genuine, "aud": [MCP_AUD, MCP_BASE]}
    async with running_mcp(mcp_env), mcp_client(mcp_env) as client:
        resp = await client.post(
            "/mcp", json=INITIALIZE, headers=_bearer(await signed_mcp_token(claims))
        )
        control = await client.post(
            "/mcp", json=INITIALIZE, headers=_bearer(await signed_mcp_token(genuine))
        )
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == REFUSED_CHALLENGE
    assert resp.json()["detail"] == REFUSED_EN
    assert control.status_code == 200, control.text
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
    before = await _org_users(mcp_env, org_id)
    async with mcp_client(mcp_env) as client:
        resp = await client.post(
            "/mcp", json=INITIALIZE, headers=_bearer(await signed_mcp_token(claims))
        )
    assert resp.status_code == 401
    assert resp.json()["detail"] == REFUSED_EN
    assert await _org_users(mcp_env, org_id) == before  # A3-51: nobody, under any id
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

  con `from uuid import UUID, uuid4` y, junto a los ayudantes del archivo:

```python
async def _org_users(env: Any, org_id: UUID) -> int:
    async with AsyncSession(env.admin_engine) as session:
        return (
            await session.execute(
                select(func.count()).select_from(User).where(User.org_id == org_id)
            )
        ).scalar_one()
```

  (`from sqlalchemy import func, select` si falta `func`).
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
  aprovisiona. Los cinco tokens de Keycloak siguen siendo los casos; cada uno lleva ahora un claim `client_id` (A3-50),
  para que pase la etiqueta de `McpAsgi` y lo que lo rechace sea la puerta, que no verifica su firma (`REFUSED`): sin
  él, la etiqueta respondería antes y la prueba no tocaría la verificación. La parametrización pasa a:

```python
@pytest.mark.parametrize(
    "kw",
    [
        {
            "azp": "claude",
            "extra": {"preferred_username": "service-account-x", "client_id": "claude"},
        },
        {"azp": "claude", "extra": {"client_id": "claude"}},
        {"azp": "it-suite-web", "extra": {"client_id": "it-suite-web"}},
        {"azp": "claude", "typ": "ID", "extra": {"client_id": "claude"}},
        {"azp": "claude", "sid": None, "extra": {"client_id": "claude"}},
    ],
    ids=["service-account-username", "client-id-claim", "console-azp", "id-token", "no-sid"],
)
async def test_a_token_no_person_signed_in_for_is_refused_at_mcp(
    mcp_env, kw: dict[str, Any]
) -> None:
```

  y en el cuerpo las aserciones del detalle pasan a `assert resp.json()["detail"] == REFUSED_EN` y `assert
  resp_es.json()["detail"] == REFUSED_ES` (sin el `if`). `NO_APP_EN` y `NO_APP_ES` los usa la prueba siguiente. El comentario «a refusal must come before admission, which would otherwise
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

  y la sexta, el lado de la decisión del falsificador 13 (A3-52: la regla de dominio decide sobre la misma fila que el
  perfil enseña):

```python
async def test_the_door_decides_on_the_address_the_row_holds(api_env) -> None:
    """Falsifier 13, the decision's side (§7.2: "The security decision and the
    displayed fact come from different sources" was the bug): the domain rule
    reads the address the row holds, the one `/me` shows."""
    org_id = await api_env.create_org("decide")
    sub = await api_env.create_user(org_id, "technician")

    async def set_address(address: str) -> None:
        async with AsyncSession(api_env.admin_engine) as s:
            await s.execute(
                update(User).where(User.external_subject == sub).values(email=address)
            )
            await s.commit()

    api_env.settings.console_email_domain = "toeshee.com"
    try:
        await set_address("ada@elsewhere.example")
        refused = await _mcp_me(api_env, org_id, sub)
        await set_address("ada@toeshee.com")
        admitted = await _mcp_me(api_env, org_id, sub)
    finally:
        api_env.settings.console_email_domain = None
    assert refused.status_code == 403
    assert "email domain not permitted" in refused.json()["detail"]
    assert (admitted.status_code, admitted.json()["email"]) == (200, "ada@toeshee.com")
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
(seis llamadas, `:350`, `:351`, `:405`, `:406`, `:408` y `:443`, en `_initialize(env, await _old_token(...))`).

- [ ] **Step 7: Las llamadas mecánicas**

`mcp_token(` → `await mcp_token(` en: `backend/tests/test_mcp_tools.py` (las líneas 210 —`return org_id, sub, await
mcp_token(env, slug, sub)`— y 277), `backend/tests/test_deploy_window.py:135`, `backend/tests/test_backup_policy_window.py:222`,
`backend/tests/test_client_scoped_config.py:1171`, `backend/tests/test_auth_reset_needs_recovery.py:373`.

En `backend/tests/test_mcp_tools.py`, los dos `McpPrincipal` que se construyen a mano (`:965-978`, en
`test_the_audit_row_is_written_even_when_the_handler_is_cancelled`, y `:1003-1008`, en `_direct_call`) llevan la
concesión que `McpPrincipal.claims` tiene ahora (A3-49): `from app.core.security import TokenClaims` pasa a `from
app.core.auth.oauth.access import McpGrant`, y `claims=TokenClaims(sub=sub, email="", name="", org_slug="",
azp="claude")` pasa a

```python
        claims=McpGrant(
            user_id=user_id, org_id=org_id, client_id="claude", family_id=uuid4(), iat=0, exp=600
        ),
```

(en la primera prueba, `user_id = await _user_id(mcp_env, sub)` antes del `McpPrincipal`, y `user_id=user_id` en
él; en `_direct_call`, `user_id` ya es su parámetro; `from uuid import uuid4` si falta). `_gate` lee `.iat` y `.azp`,
los dos de `McpGrant`.

**`backend/tests/test_step_up_widened.py`** (lo crea la Task 14 de la fase 2), **si la rama ya lo tiene**: sus dos
pruebas de MCP siguen el mismo re-apuntado, como piden los «Puntos de encuentro con la fase 3» de la fase 2. En
`test_over_mcp_a_tenant_change_is_refused_and_the_rest_of_the_edit_is_not`, `token = mcp_token(mcp_env, slug, sub)`
pasa a `token = await mcp_token(mcp_env, slug, sub)`. En `test_on_the_mcp_channel_switching_ai_access_on_is_refused`,
la línea `bearer = {"Authorization": f"Bearer {mcp_token(mcp_env, await mcp_env.org_slug(org), sub)}"}` pasa a estas
dos (con el `await` dentro de la f-string tendría 102 columnas, y ruff E501 la rechaza):

```python
    token = await mcp_token(mcp_env, await mcp_env.org_slug(org), sub)
    bearer = {"Authorization": f"Bearer {token}"}
```

Nada más cambia, ni lo que afirman: su `mcp_env` pone `MCP_BASE` (una consola https sin ruta, así que hay emisor);
`mcp_org` enciende MCP por SQL; la puerta propia no lee el interruptor (lo lee `/mcp`), así que la segunda, con MCP
apagado, sigue llegando a `PATCH /orgs/current`; `marked_client` y `mcp_token` nombran la misma app, `"claude"`; y la
negativa sigue siendo la de `require_step_up` (decisión 6). Si la rama aún no lo tiene, el mismo cambio lo hace quien
fusione después la Task 14 de la fase 2 (Puntos de encuentro).

Comprobación de que no queda ninguna llamada sin `await` a los cuatro ayudantes asíncronos (`mcp_token`, `_mcp_token`,
`_token_issued_at`, `_old_token`; A3-47):

Run: `cd backend && grep -rnoP "(?:await |def )?(?<![A-Za-z0-9_])(?:mcp_token|_mcp_token|_token_issued_at|_old_token)\(" tests --include=*.py | grep -vE ":(await |def )"`
Expected: sin salida. La búsqueda es **por llamada**, no por línea (re-revisión f3-3, N1): `-o` imprime cada llamada con
su `await ` o su `def ` delante si los tiene, y solo esas se descartan. Así ve la llamada interior que perdió su
`await` en `assert await _initialize(env, _old_token(...)) == 401`, que un `grep -v "await "` por línea descartaría
entera; y no ve `signed_mcp_token(` ni `require_person_mcp_token(`. Antes de los Steps 6 y 7, en `3a1ba1c`, imprime
50 llamadas: las que esos dos pasos cambian (y dos más, las de `tests/test_step_up_widened.py`, si la rama ya lo
tiene).

Y de que solo quedan los tokens de Keycloak hechos a propósito para que la puerta los rechace, contados por archivo en
lugar de excluir archivos enteros:

Run: `cd backend && grep -rc "aud=MCP_AUD" tests | grep -v ":0$" | sort`
Expected: exactamente estas tres líneas:

```text
tests/test_mcp_own_tokens.py:1
tests/test_mcp_server.py:1
tests/test_token_origin.py:2
```

(`test_mcp_own_tokens.py::test_a_keycloak_token_is_refused_whatever_its_audience`,
`test_mcp_server.py::test_a_keycloak_token_is_401_whatever_its_audience`, y en `test_token_origin.py` la
parametrización de `test_a_token_no_person_signed_in_for_is_refused_at_mcp` y
`test_a_legacy_client_id_claim_is_refused_at_mcp`.) Un `await` olvidado que estas dos búsquedas no vean lo para el
Step 8: un coroutine sin esperar es un `RuntimeWarning` al recogerlo, y pytest lo convierte en
`PytestUnraisableExceptionWarning`; los dos `-W error` lo hacen fallo (con solo el primero, la prueba pasa: lo comprobé
con un coroutine sin esperar).

- [ ] **Step 8: Las pruebas pasan**

Run: `cd backend && .venv/bin/pytest -q -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning tests/test_mcp_own_tokens.py tests/test_mcp_server.py tests/test_mcp_channel_api.py tests/test_token_origin.py tests/test_domain_gate_doors.py tests/test_domain_policy.py tests/test_clients_api.py tests/test_me_profile.py tests/test_auth_falsifiers.py tests/test_mcp_admin_api.py tests/test_mcp_tools.py tests/test_deploy_window.py tests/test_backup_policy_window.py tests/test_client_scoped_config.py tests/test_auth_reset_needs_recovery.py tests/test_route_session_discipline.py tests/test_client_catalog.py tests/test_i18n_catalog.py tests/test_loopback.py tests/test_auth_session_door.py tests/test_auth_csrf.py tests/test_security.py`
Expected: PASS.

Run: `cd backend && test ! -e tests/test_step_up_widened.py || .venv/bin/pytest -q -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning tests/test_step_up_widened.py`
Expected: PASS, o nada que correr si la rama aún no tiene la Task 14 de la fase 2.

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
test ! -e backend/tests/test_step_up_widened.py || git add backend/tests/test_step_up_widened.py
git commit -m "feat(mcp): the MCP door admits only the platform's own access token, and reads its grant on every call"
```

El informe de la tarea lista las pruebas re-apuntadas del Step 6 con su porqué (está escrito arriba, prueba por prueba).

### Task 10: Ajustes → Conexiones de IA sin Keycloak; los actos de la cuenta revocan las concesiones

Diseño §5.3 («Revocar un cliente de IA en Ajustes corta su acceso en la siguiente llamada (1c(d))»), §2.4, §7; D12;
B3-07, B3-28, B3-30, B3-31. Oleada 5, en paralelo con la Task 13. Backend.

Revocar una app en Ajustes deja de pedir a Keycloak que borre un consentimiento: termina las concesiones de la app (de
la persona o de toda la organización), escribe la fila `mcp_revocation` y el evento `mcp.app_revoked`, **en una sola
transacción**, en una función del núcleo. La ruta pasa a nombrar la app por el id de su fila (D12: el `client_id` de un
cliente CIMD es una URL con `/`). La lista enseña también las apps con una concesión viva que aún no han llamado a
ninguna herramienta. Y los siete actos de la cuenta que ya olvidan los equipos recordados de una persona terminan
también sus concesiones de IA, con el acto como motivo y el recuento en su evento de auditoría.

**Files:**
- Create: `backend/app/core/auth/oauth/connections.py`
- Modify: `backend/app/mcp/api.py`
- Modify: `backend/app/mcp/exclusions.py` (la clave de la ruta de revocar en `NOT_FOR_MCP`)
- Modify: `backend/app/core/i18n/es.py` (una entrada nueva, una que deja de usarse)
- Modify: `backend/app/modules/user_admin/service.py` (`set_enabled`, `reset_factors`, `delete_user`)
- Modify: `backend/app/core/auth/account.py` (`change_password`)
- Modify: `backend/app/core/auth/flows.py` (`logout_everywhere`, `reset_password`, `accept_recovery`)
- Modify: `backend/tests/test_mcp_admin_api.py`
- Modify: `backend/tests/test_global_writes.py` (la clave de `NOT_GLOBAL` y la URL de su prueba)
- Modify: `backend/tests/test_client_catalog.py` (`OAUTH_SESSION_USERS` gana `connections.py`)
- Modify: `backend/tests/test_auth_signin.py`, `backend/tests/test_auth_password_reset.py`,
  `backend/tests/test_auth_recover_command.py` (cinco aserciones exactas de metadatos ganan `ai_grants_revoked`)
- Modify: `backend/tests/test_logging_discipline.py` y `docs/superpowers/specs/2026-08-14-logging-kernel-design.md` (§2.2)
- Create: `backend/tests/test_mcp_grant_lifecycle.py`

**Interfaces:**
- Consumes: Task 5 (`OAuthClient`, `OAuthRefreshFamily`, `oauth_session`, `OAUTH_SESSION_USERS`), Task 7
  (`grants.revoke_app(session, *, oauth_client_id, user_id, reason, now) -> int`, `grants.revoke_all_for_user(session,
  user_id, reason) -> int`, `grants.open_family`; los motivos `revoked_in_settings`, `disabled`, `deleted`,
  `factors_reset`, `password_changed`, `signed_out_everywhere` de `REVOKE_REASONS`; en `tests/oauth_support.py`,
  `token_request`), Task 8 (en `tests/oauth_support.py`, `connect`), Task 9 (`tests.mcp_support.mcp_app`, `mcp_token`
  asíncrono, y `_old_token`/`_call_a_tool` ya re-apuntados en `test_mcp_admin_api.py`). Existentes:
  `require_all_clients`, `has_permission(session, user_id, code)` (`app/core/rbac.py:544`), `audit_service.record`,
  `McpRevocation`; en las pruebas, `tests.auth_support` (`signed_in`, `console_ready`, `capture_mail`, `browser`,
  `link_token`) y `app.core.auth.action_tokens.issue`.
- Produces: `connections.Registration(id, client_id, name, kind, host)`, `connections.registrations(engine, *, org_id, client_ids)
  -> dict[str, Registration]`, `connections.HeldApp(registration, mine)`, `connections.held_apps(engine, *, org_id, user_id, now, limit) ->
  list[HeldApp]`,
  `connections.revoke_app_access(engine, *, org_id, actor_id, app_id, scope, now) -> int`; `POST
  /api/v1/mcp/connections/{app_id}/revoke` → `{"revoked": true, "grants_revoked": int}`; en `GET
  /api/v1/mcp/connections`, cada app con `id: UUID | null`, `name: str | null`, `host: str | null` y `kind` (B3-10:
  la fila se titula por el host), y las apps con concesión viva sin uso
  (`people` 0, `last_used_at` null) (Apéndice A.2; la Task 11 lo consume); `mcp.app_revoked` con `metadata =
  {"client_id", "scope", "grants_revoked"}`; `ai_grants_revoked` en los metadatos de los siete actos de la cuenta.

**Decisión de la tarea (1): una transacción.** Sin Keycloak no hay mitad «de mejor esfuerzo»: la fila, las familias y
el evento se escriben juntos o nada (D12). Desaparecen `remove_consents`, `_record_incomplete`, `record_revocation`,
`keycloak_failures`, las constantes `KEYCLOAK_*`, `FIRST_PARTY_CLIENTS` y `CLIENT_ID_PATTERN` (una app de la
plataforma nunca tiene fila en `oauth_clients`, y un id que no es un UUID es 422 antes de llegar a nada).
`kc_client.revoke_consent` y sus pruebas se quedan hasta la fase 4. El vocabulario de auditoría conserva
`mcp.app_revoke_incomplete` (los eventos ya escritos se siguen leyendo); la prueba de cobertura solo exige el sentido
backend → vocabulario.

**Decisión de la tarea (2): la lista sale de las llamadas y de las concesiones vivas** (B3-28). `CLAUDE.md` 1c(d):
«every registered AI client can be revoked, with effect on its next call». Una app recién aprobada solo ha hecho
`initialize` y `tools/list`, que no escriben `mcp.tool_called`; si la lista saliera solo de las llamadas, quien
sospecha de una aprobación no tendría nada que revocar en Ajustes. La lista es la de siempre (las apps que llamaron
herramientas en los últimos 90 días, con sus personas y su último uso) **más** las apps con una concesión viva en la
organización que no están en ella: las de la persona, y las de todos para quien tiene `orgs.manage`, con `people` 0 y
`last_used_at` null; `MAX_APPS` en total. Cada app lleva `id` y `name` de su fila de `oauth_clients` cuando la tiene;
sin fila (una app de antes de esta fase, o un registro caducado y barrido) el `id` es `null` y la consola no ofrece
revocarla (Task 11), porque no queda concesión que terminar.

**Decisión de la tarea (3): los actos de la cuenta son los que ya olvidan los equipos recordados** (B3-07). §2.4
nombra uno: «Desactivar a una persona corta sus sesiones, sus tokens de IA y sus equipos recordados al instante». Los
otros seis los toma el plan de la misma lista de los equipos: borrar, restablecer los factores (el administrador o el
enlace de recuperación), cambiar la contraseña (la persona o el enlace por correo) y «cerrar sesión en todas partes».
Que un administrador cierre las sesiones de alguien no los olvida (`user_admin.service.end_sessions`, «their remembered
devices stay»), y tampoco termina sus concesiones. Una línea junto a cada `revoke_all_for_user(session, user.id)` de
`app.core.auth.devices`, con el módulo importado como `ai_grants` (`from app.core.auth.oauth import grants as
ai_grants`): `user_admin/service.py` tiene variables locales `grants` en `delete_user` (`:1150`) y `cancel_invite`, que
taparían al módulo, y mypy lo vería (A3-54); el mismo alias en los tres archivos.

**Decisión de la tarea (4): el acto deja constancia de cuántas concesiones terminó** (B3-30). El diseño §7 audita las
«revocaciones de clientes de IA», y el motivo en la fila de la familia se barre al día (Task 13). Cada uno de los siete
actos ya escribe su evento (`user.disabled`, `user.deleted`, `user.factors_reset`, `auth.password_changed`,
`auth.sessions_ended`, `user.recovery_link_used`); sus metadatos ganan `"ai_grants_revoked": n`, en la misma
transacción. Un evento por concesión no añadiría nada que la persona y el acto no digan ya.

**Decisión de la tarea (5): la ruta no abre el alcance «todos»** (B3-31). Las concesiones de una persona son filas O
globales (cliente NULL) que solo un alcance de todos los clientes lee o escribe; `app/core/auth/oauth/connections.py`
lo abre (`oauth_session`) y `app/mcp/api.py` llama a sus funciones. `OAUTH_SESSION_USERS` gana ese archivo, no la
ruta.

**Decisión de la tarea (6): la fila de Ajustes se titula por el host, como la pantalla de consentimiento** (B3-10,
A3-62). RFC 7591 §5: «the authorization server MUST treat all client metadata as self-asserted». Cada app lleva `host`
—el del documento CIMD, o los de sus redirects registrados, que son ASCII (Task 1)— y `kind`; la consola (Task 11)
titula la fila por el host y enseña el nombre que la app se dio como afirmación suya, con la marca de «se registró
sola» para una app de DCR. Sin registro, los dos son `null` y la fila sigue titulada por el `client_id`.

- [ ] **Step 1: Las pruebas que fallan**

`backend/tests/test_mcp_grant_lifecycle.py`:

```python
"""An act on the account ends the person's AI connections (autenticación
propia design §2.4, §5.3, §7; plan 2026-11-06, Task 10).

The acts that forget a person's remembered browsers end their grants too, with
the act as the reason and the count in the act's own audit event: being
disabled or deleted, a factor reset (by an administrator or by the recovery
link), a password change (their own, or by the mail link) and "sign out
everywhere". An administrator ending the person's sessions forgets no browser,
and ends no grant (B3-07).
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
from app.models import AuditEvent
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


async def _counted(env: Any, org_id: UUID, action: str) -> Any:
    """`ai_grants_revoked` of the act's latest audit event (B3-30)."""
    async with AsyncSession(env.admin_engine) as session:
        event = (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.org_id == org_id, AuditEvent.action == action)
                .order_by(AuditEvent.id.desc())
                .limit(1)
            )
        ).scalar_one()
    return event.meta.get("ai_grants_revoked")


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
    assert await _counted(env, org, "user.disabled") == 1
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
    assert await _counted(env, org, "user.deleted") == 1


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
    assert await _counted(env, org, "user.factors_reset") == 1


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
    assert await _counted(env, org, "auth.password_changed") == 1


async def test_signing_out_everywhere_ends_my_ai_connections(lifecycle_env: Any) -> None:
    env = lifecycle_env
    org = await env.create_org("grant-everywhere")
    who = await signed_in(env, org, "technician")
    family = await _connected(env, org, who.user_id)

    resp = await env.client.post("/api/v1/auth/logout-everywhere", headers=who.headers())

    assert resp.status_code == 204, resp.text
    assert await _reason(env, family) == "signed_out_everywhere"
    assert await _counted(env, org, "auth.sessions_ended") == 1


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
    assert await _counted(env, org, "auth.password_changed") == 1


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
    assert await _counted(env, org, "user.recovery_link_used") == 1


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
Expected: FAIL, siete de ocho (A3-56): `_reason` es `None` después de cada acto; solo pasa
`test_an_administrator_ending_sessions_leaves_the_ai_connections`, que espera `None`.

- [ ] **Step 2: `connections.py` y `app/mcp/api.py`**

`backend/app/core/auth/oauth/connections.py`:

```python
"""Settings → AI connections, where it touches a person's grants (own
authentication design §5.3; plan 2026-11-06, Task 10; B3-28, B3-31, D12).

A person's grants (`oauth_refresh_families`) are global O rows under `users`,
client NULL, which only an every-client scope reads or writes. This module
holds that scope (`oauth_session`) for the console's AI-connections routes,
which open none themselves (`tests/test_client_catalog.py::
OAUTH_SESSION_USERS`).

- `registrations`: each named app's registration here (`oauth_clients`, G),
  whose `id` revoking names: a metadata-document client's `client_id` is a
  URL, which is no path segment (D12);
- `held_apps`: the apps holding a live grant in the organization -- the
  person's own, and everyone's for an `orgs.manage` holder -- so an app
  approved a moment ago, which has called no tool, can be revoked (B3-28;
  `CLAUDE.md` 1c(d): "every registered AI client can be revoked, with effect
  on its next call"); each says whether one of those grants is the
  person's, so a row only a colleague holds can show "revoked for you"
  (re-review f3-3, N4);
- `revoke_app_access`: the app's grants end -- the actor's for "me",
  everyone's in the organization for "everyone" -- together with the
  `mcp_revocation` row and the `mcp.app_revoked` event, in ONE transaction
  (D12). A grant that ended stops every token it issued at its next call
  (`deps.admit` reads it, D6) and its refresh token mints nothing.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.audit import audit_service
from app.core.auth.oauth import grants
from app.core.auth.oauth.redirects import host_of
from app.core.auth.oauth.store import oauth_session
from app.core.i18n import tr
from app.core.rbac import has_permission
from app.models.mcp import McpRevocation
from app.models.oauth import OAuthClient, OAuthRefreshFamily

#: The columns a `Registration` is read from.
_COLUMNS = (
    OAuthClient.id,
    OAuthClient.client_id,
    OAuthClient.client_name,
    OAuthClient.kind,
    OAuthClient.redirect_uris,
)


@dataclass(frozen=True)
class Registration:
    id: UUID
    client_id: str
    name: str | None
    kind: str
    #: What the row is titled by (plan B3-10): the host the app cannot fake --
    #: its metadata document's, or the hosts its registered redirects go to.
    host: str


@dataclass(frozen=True)
class HeldApp:
    registration: Registration
    #: Whether one of the live grants is the caller's own.
    mine: bool


def _registration(row: Any) -> Registration:
    app_id, client_id, name, kind, redirect_uris = row
    if kind == "metadata_document":
        host = urlsplit(client_id).hostname or client_id
    else:
        host = ", ".join(sorted({host_of(uri) for uri in redirect_uris} - {""}))
    return Registration(app_id, client_id, name, kind, host)


async def registrations(
    engine: AsyncEngine, *, org_id: UUID, client_ids: Sequence[str]
) -> dict[str, Registration]:
    """The registration of each app in `client_ids` this organization has."""
    if not client_ids:
        return {}
    async with oauth_session(org_id, engine) as session:
        rows = (
            await session.execute(
                select(*_COLUMNS).where(
                    OAuthClient.org_id == org_id, OAuthClient.client_id.in_(list(client_ids))
                )
            )
        ).all()
    found = [_registration(row) for row in rows]
    return {registration.client_id: registration for registration in found}


async def held_apps(
    engine: AsyncEngine, *, org_id: UUID, user_id: UUID, now: datetime, limit: int
) -> list[HeldApp]:
    """The apps holding a live grant here: `user_id`'s own, and anyone's when
    they hold `orgs.manage` across the organization; in `client_id` order,
    each with whether `user_id` holds one of them."""
    live = (
        OAuthRefreshFamily.org_id == org_id,
        OAuthRefreshFamily.revoked_at.is_(None),
        OAuthRefreshFamily.expires_at > now,
    )
    async with oauth_session(org_id, engine) as session:
        everyone = await has_permission(session, user_id, "orgs.manage")
        query = (
            select(*_COLUMNS)
            .join(OAuthRefreshFamily, OAuthRefreshFamily.oauth_client_id == OAuthClient.id)
            .where(OAuthClient.org_id == org_id, *live)
            .distinct()
            .order_by(OAuthClient.client_id)
            .limit(limit)
        )
        if not everyone:
            query = query.where(OAuthRefreshFamily.user_id == user_id)
        rows = (await session.execute(query)).all()
        mine = set(
            (
                await session.execute(
                    select(OAuthRefreshFamily.oauth_client_id).where(
                        OAuthRefreshFamily.user_id == user_id, *live
                    )
                )
            ).scalars()
        )
    found = [_registration(row) for row in rows]
    return [HeldApp(registration, registration.id in mine) for registration in found]


async def revoke_app_access(
    engine: AsyncEngine,
    *,
    org_id: UUID,
    actor_id: UUID,
    app_id: UUID,
    scope: Literal["me", "everyone"],
    now: datetime,
) -> int:
    """How many grants ended. 403 without `orgs.manage` for "everyone", 404
    when this organization has no such app: either way nothing is written."""
    async with oauth_session(org_id, engine) as session:
        # NO CLIENT, BY DESIGN (plan 2026-10-26 Task 14 keeps it): revoking
        # everyone writes the organisation's revocation list, which is no one
        # client's, so `orgs.manage` is asked on every client.
        if scope == "everyone" and not await has_permission(session, actor_id, "orgs.manage"):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, tr("requires {code}", code="orgs.manage")
            )
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
            McpRevocation(
                org_id=org_id, user_id=whose, client_id=app.client_id, revoked_by=actor_id
            )
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

En `backend/tests/test_client_catalog.py`, `OAUTH_SESSION_USERS` gana:

```python
    "app/core/auth/oauth/connections.py": (
        "Settings -> AI connections: the apps holding a person's live grants, and revoking one "
        "(its grants, the revocation row and the audit event in one transaction)"
    ),
```

En `backend/app/mcp/api.py`, el docstring del módulo pasa a:

```python
"""The console's side of the MCP server: Settings → AI connections
(MCP server design §3.4, §7, §8; own authentication phase 3, plan 2026-11-06
Task 10).

    GET  /api/v1/mcp/connections                   `mcp.use`
    POST /api/v1/mcp/connections/{app_id}/revoke   `mcp.use`; "everyone"
                                                   also needs `orgs.manage`

Neither route is a tool (`app/mcp/exclusions.py::NOT_FOR_MCP`): the person
manages their AI connections in the console, not through one of them.

THE LIST IS DERIVED FROM USE, AND FROM GRANTS (plan B3-28). An app is listed
when it has acted here -- the `app` (the token's `client_id`) of this org's
`mcp.tool_called` audit events, read under `org_session`, so RLS keeps another
tenant's use out as well as the `WHERE` clause does -- or when it holds a live
grant here and has not acted yet: the person's own, and everyone's for an
`orgs.manage` holder. An app approved a moment ago has called no tool, and must
be revocable (1c(d)). Each one carries the `id` and `name` of its registration
here (`oauth_clients`, G) when it has one; revoking names that `id`, because a
metadata-document client's `client_id` is a URL, which is no path segment
(plan D12).

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

REVOKE IS ONE TRANSACTION (plan D12), in `app.core.auth.oauth.connections`,
which holds the every-client scope over a person's grants; this module opens
none (plan B3-31). The app's grants end -- the person's for "me", everyone's in
the organization for "everyone" -- together with the `mcp_revocation` row and
the `mcp.app_revoked` event, or none of them does:

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

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.deps import AuthContext, require_all_clients, require_permission
from app.core.auth.oauth import connections
from app.core.mcp_channel import mcp_resource
from app.core.tenancy import org_session
from app.models import AuditEvent, Organization
from app.models.mcp import McpRevocation
```

(se van `asyncio`, `logging`, `httpx`, `HTTPException`, `Path`, `status`, `audit_service`, `Settings`, `tr`,
`has_permission`, `User`, el bloque de `kc_client` y `logger`). Se borran `CLIENT_ID_PATTERN`, `FIRST_PARTY_CLIENTS`,
`KEYCLOAK_CONCURRENCY`, `KEYCLOAK_BUDGET_SECONDS`, `record_revocation`, `remove_consents` y `_record_incomplete`.

En `McpAppOut`, `client_id` cambia su descripción y gana dos campos después; `people` y `last_used_at` dicen qué valen
para una app sin uso:

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
        description=(
            "The name the app gave itself, as it gave it (anyone can call an app anything); "
            "null when it gave none."
        )
    )
    host: str | None = Field(
        description=(
            "What the app is named by, which it cannot fake: the host of its client metadata "
            "document, or the host(s) its registered redirects go to; null when it has no "
            "registration here."
        )
    )
    kind: Literal["registered", "metadata_document"] | None = Field(
        description=(
            "`registered` when it registered itself here (any app can, under any name), "
            "`metadata_document` when a document at its client id describes it; null when "
            "it has no registration here."
        )
    )
    people: int = Field(
        description=(
            "How many people of this organization used the app in the last 90 days; 0 for an "
            "app that holds a grant and has not called a tool yet."
        )
    )
    last_used_at: datetime | None = Field(
        description=(
            "When anyone in this organization last called a tool through the app; null when "
            "nobody has yet."
        )
    )
```

En `McpConnectionsOut`, `apps`:

```python
    apps: list[McpAppOut] = Field(
        description=(
            f"The apps that called tools here, most recently used first, then the apps holding "
            f"a live grant that have not yet; at most {MAX_APPS}."
        )
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

`list_connections` pasa a (con `_kind` delante, para que mypy vea el `Literal`):

```python
def _kind(kind: str) -> Literal["registered", "metadata_document"]:
    return "metadata_document" if kind == "metadata_document" else "registered"


async def list_connections(
    session: AsyncSession, *, engine: AsyncEngine, org_id: UUID, user_id: UUID, now: datetime
) -> list[McpAppOut]:
    """The apps that called tools in this org inside `USAGE_WINDOW`, most
    recently used first, then those holding a live grant here that have not
    (plan B3-28); at most `MAX_APPS` in all."""
    usage = (
        await session.execute(
            usage_query(org_id=org_id, user_id=user_id, since=now - USAGE_WINDOW)
        )
    ).all()
    used = [row[0] for row in usage]
    known = await connections.registrations(engine, org_id=org_id, client_ids=used)
    held = await connections.held_apps(
        engine, org_id=org_id, user_id=user_id, now=now, limit=MAX_APPS
    )

    # The held rows read the revocations too (re-review f3-3, N4).
    asked = used + [held_app.registration.client_id for held_app in held]
    for_everyone: dict[str, datetime] = {}
    for_me: dict[str, datetime] = {}
    if asked:
        revocations = (
            await session.execute(
                select(
                    McpRevocation.client_id,
                    McpRevocation.user_id,
                    func.max(McpRevocation.revoked_at),
                )
                .where(
                    McpRevocation.org_id == org_id,
                    McpRevocation.client_id.in_(asked),
                    or_(McpRevocation.user_id.is_(None), McpRevocation.user_id == user_id),
                )
                .group_by(McpRevocation.client_id, McpRevocation.user_id)
            )
        ).all()
        for_everyone = {client: at for client, who, at in revocations if who is None}
        for_me = {client: at for client, who, at in revocations if who is not None}

    apps: list[McpAppOut] = []
    for client_id, people, last, mine in usage:
        registration = known.get(client_id)
        everyone_at = for_everyone.get(client_id)
        me_at = for_me.get(client_id)
        apps.append(
            McpAppOut(
                client_id=client_id,
                id=registration.id if registration else None,
                name=registration.name if registration else None,
                host=registration.host if registration else None,
                kind=_kind(registration.kind) if registration else None,
                people=people,
                last_used_at=last,
                revoked_for_everyone=everyone_at is not None and everyone_at > last,
                revoked_for_me=me_at is not None and (mine is None or me_at > mine),
            )
        )
    listed = set(used)
    for held_app in held:
        registration = held_app.registration
        if len(apps) >= MAX_APPS:
            break
        if registration.client_id in listed:
            continue
        me_at = for_me.get(registration.client_id)
        apps.append(
            McpAppOut(
                client_id=registration.client_id,
                id=registration.id,
                name=registration.name,
                host=registration.host,
                kind=_kind(registration.kind),
                people=0,
                last_used_at=None,
                # A live grant is newer than any revocation for everyone,
                # which ends every grant in its own transaction (D12).
                revoked_for_everyone=False,
                # Revoked for me, and no grant of mine since.
                revoked_for_me=me_at is not None and not held_app.mine,
            )
        )
    return apps
```

En `get_connections`, la llamada pasa a `apps = await list_connections(session, engine=request.app.state.app_engine,
org_id=ctx.org_id, user_id=ctx.user_id, now=datetime.now(UTC))`, y su docstring termina «and the apps that called
tools here in the last 90 days or hold a live grant».

La ruta de revocar:

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
    # `orgs.manage` (checked in `connections.revoke_app_access`) and -- because
    # it writes the organization's revocation list, which is no one client's --
    # a scope of every client (MSP clients spec §2.2). Refused before anything
    # is written.
    if body.scope == "everyone":
        require_all_clients(ctx)
    ended = await connections.revoke_app_access(
        request.app.state.app_engine,
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
`from app.core.auth.oauth import grants as ai_grants` **en el orden de isort dentro del bloque `app.core.auth`** (por
el nombre del módulo: detrás de cada `from app.core.auth.<m> import …` con `<m>` anterior a `oauth`, como `factors`,
`keys`, `lookup` o `methods`, y delante del primero posterior; decisión 3). La regla, no un vecino (re-revisión f3-3,
X9): con la fase 2 fusionada, `flows.py` ya no importa `find_staff` sino `find_person, org_of_slug`, y gana imports de
`app.core.auth.methods`. `ruff check --select I` (Step 6) lo comprueba. Después de
cada `await revoke_all_for_user(session, user.id)`, con el mismo sangrado, la línea de la tabla; el evento de
auditoría del acto, que en los siete sitios se escribe **después** de esa línea, gana `"ai_grants_revoked":
ai_ended` en sus metadatos (decisión 4):

| Archivo y función | Línea nueva | El evento que gana el recuento |
|---|---|---|
| `user_admin/service.py::set_enabled` (dentro de `if not enabled:`) | `ai_ended = await ai_grants.revoke_all_for_user(session, user.id, "disabled")` | `user.disabled` (ver abajo) |
| `user_admin/service.py::reset_factors` | `ai_ended = await ai_grants.revoke_all_for_user(session, user.id, "factors_reset")` | `user.factors_reset`: `{"email": …, "via": "administrator", "ai_grants_revoked": ai_ended}` |
| `user_admin/service.py::delete_user` | `ai_ended = await ai_grants.revoke_all_for_user(session, user.id, "deleted")` | `user.deleted`: `{"email": …, "roles_removed": len(grants), "ai_grants_revoked": ai_ended}` |
| `core/auth/account.py::change_password` | `ai_ended = await ai_grants.revoke_all_for_user(session, user.id, "password_changed")` | `auth.password_changed`: `{"via": "self", "ai_grants_revoked": ai_ended}` |
| `core/auth/flows.py::logout_everywhere` | `ai_ended = await ai_grants.revoke_all_for_user(session, user.id, "signed_out_everywhere")` | `auth.sessions_ended`: `{"by": "self", "scope": "everywhere", "ai_grants_revoked": ai_ended}` |
| `core/auth/flows.py::reset_password` | `ai_ended = await ai_grants.revoke_all_for_user(session, user.id, "password_changed")` | `auth.password_changed`: `{"via": row.purpose, "ai_grants_revoked": ai_ended}` |
| `core/auth/flows.py::accept_recovery` | `ai_ended = await ai_grants.revoke_all_for_user(session, user.id, "factors_reset")` | `user.recovery_link_used`: `{"email": user.email, "ai_grants_revoked": ai_ended}` |

En `set_enabled` el recuento solo existe al desactivar, y el evento de activar no cambia: `ai_ended = 0` antes de `if
not enabled:`, y su `metadata` pasa a

```python
        metadata={
            "email": user.email,
            "status": user.status,
            **({} if enabled else {"ai_grants_revoked": ai_ended}),
        },
```

En `accept_recovery`, el `auth.password_changed` que se escribe antes (cuando la persona elige contraseña) no cambia:
el recuento va en `user.recovery_link_used`, que sigue a la revocación. En `account.py`, la firma del envoltorio
`_record` (`:111`) pasa a la de abajo: el recuento es un entero (el `_audit` de `flows.py` ya toma `dict[str, Any]`), y
en una línea no cabe en 100 columnas:

```python
async def _record(
    session: AsyncSession, user: User, action: str, meta: dict[str, str | int]
) -> None:
```

Son los siete sitios que `grep -rn "await revoke_all_for_user(session, user.id)" backend/app` lista hoy
(`user_admin/service.py:1004`, `:1079`, `:1165`; `account.py:311`; `flows.py:819`, `:946`, `:1149`); si la fase 2
añade otro (un contacto: no tiene concesiones de IA, no pasa la puerta de la consola), no necesita la línea.

- [ ] **Step 4: Las pruebas de Ajustes, re-apuntadas**

En `backend/tests/test_mcp_admin_api.py`:

- El docstring del módulo: las viñetas primera a tercera pasan a «`GET /api/v1/mcp/connections` lists the apps that
  called tools in THIS org, from `mcp.tool_called`, with how many people used each and when, and whether a revocation
  newer than that use stands; then the apps holding a live grant that called nothing yet (plan B3-28)», «`POST
  /api/v1/mcp/connections/{app_id}/revoke` ends the app's grants -- the person's, or the organization's -- and writes
  the revocation row and its audit event in ONE transaction; no identity provider is called (own authentication phase
  3, plan 2026-11-06 Task 10)»; el párrafo de Keycloak pasa a «Keycloak is still `httpx.MockTransport`: for
  `kc_client.revoke_consent`'s own tests, which stay until phase 4, and to prove that revoking sends it nothing.»
- Imports: se quita `import anyio` (su único uso era la prueba de concurrencia de Keycloak, que se reescribe abajo;
  ruff F401, A3-57); se añaden `from datetime import UTC, datetime`, `from app.models.oauth import OAuthRefreshFamily`,
  `from app.core.auth.oauth import connections, grants`, `from app.core.auth.oauth.store import oauth_session`, `from
  app.core.mcp_channel import mcp_resource`, `from tests.oauth_support import connect, token_request`, y `mcp_app` a
  los de `tests.mcp_support`.
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


async def _approved(env: Any, org_id: UUID, slug: str, sub: str) -> dict[str, Any]:
    """The person approves "claude" on the consent screen and its code is
    exchanged (Task 8): a live grant and its refresh token, and no tool called.
    The consent is redeemed in the deployment's organization (plan B3-09)."""
    env.api.settings.default_org_slug = slug
    await mcp_app(env.api, org_id)
    return await connect(env.api, org_id, sub, client_id="claude")
```

- `test_two_people_using_one_app_list_as_one_app_with_two_people`: después de `assert app["client_id"] == "claude"`,
  `assert app["id"] == str(await mcp_app(env.api, org_id))`, `assert app["name"] == "claude"` y `assert (app["host"],
  app["kind"]) == ("claude.ai", "registered")` (`mcp_app` lo registra con el redirect de Claude).
- Nueva, junto a la anterior (A3-55, B3-28):

```python
async def test_an_app_approved_and_not_yet_used_is_listed_and_can_be_revoked(env: Any) -> None:
    """An app approved a moment ago has called no tool: it is listed all the
    same, with the id revoking names, and revoking it ends its grant."""
    org_id, slug = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    await _approved(env, org_id, slug, me)

    (app,) = (await _connections(env, org_id, me)).json()["apps"]
    assert app == {
        "client_id": "claude",
        "id": str(await mcp_app(env.api, org_id)),
        "name": "claude",
        "host": "claude.ai",
        "kind": "registered",
        "people": 0,
        "last_used_at": None,
        "revoked_for_me": False,
        "revoked_for_everyone": False,
    }
    resp = await _revoke(env, org_id, me, "me", app=app["id"])

    assert resp.json() == {"revoked": True, "grants_revoked": 1}
    assert await _live(env, org_id, me) == 0
    assert (await _connections(env, org_id, me)).json()["apps"] == []
```

- `test_revoke_for_me_cuts_off_my_old_token_and_not_my_colleagues`: la respuesta es `{"revoked": True,
  "grants_revoked": 1}`; las líneas de `env.kc.deleted` pasan a `assert env.kc.requests == []`; el evento, `{"client_id":
  "claude", "scope": "me", "grants_revoked": 1}`; se quita la aserción de `mcp.app_revoke_incomplete` (ningún código lo
  escribe ya: sería vacía); y antes de los `_initialize`, `assert await _live(env, org_id, me) == 0` y `assert await
  _live(env, org_id, colleague) == 1`, con `assert {f.revoked_reason for f in await _families(env, org_id) if
  f.revoked_at} == {"revoked_in_settings"}`.
- Nueva (A3-58): revocar en Ajustes también deja sin valor el refresh token, que es lo que el consentimiento de
  Keycloak garantizaba al borrarse:

```python
async def test_a_settings_revoke_also_ends_the_refresh_token(env: Any) -> None:
    org_id, slug = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    tokens = await _approved(env, org_id, slug, me)

    assert (await _revoke(env, org_id, me, "me")).status_code == 200

    resp = await token_request(
        env.api,
        grant_type="refresh_token",
        refresh_token=tokens["refresh_token"],
        client_id="claude",
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"
```

- `test_revoke_for_everyone_without_orgs_manage_is_403_and_writes_nothing` pasa a la de abajo: la persona abre antes
  su concesión, para que la aserción nueva no sea vacía (A3-57), y **después** de `_grant` (re-revisión f3-3, N7):
  sin `mcp.use` el consentimiento responde `back`/`denied`, sin código, y el `code_of` de `connect` levanta
  `KeyError`. El control de no vacuidad del final (revocar «me» da 200) se queda:

```python
async def test_revoke_for_everyone_without_orgs_manage_is_403_and_writes_nothing(
    env: Any,
) -> None:
    org_id, slug = await _org(env)
    tech = await env.api.create_user(org_id, "technician")
    await _grant(env, org_id, tech, "mcp.use")
    # After `_grant`: without `mcp.use` the consent gives back no code.
    await _approved(env, org_id, slug, tech)

    resp = await _revoke(env, org_id, tech, "everyone")

    assert resp.status_code == 403
    assert resp.json()["detail"] == "requires orgs.manage"
    assert await _rows(env, org_id) == []
    assert await _events(env, org_id, "mcp.app_revoked") == []
    assert env.kc.requests == []
    assert await _live(env, org_id, tech) == 1
    # Non-vacuity: the same person may revoke for themselves.
    assert (await _revoke(env, org_id, tech, "me")).status_code == 200
```

- Nueva, junto a la anterior (re-revisión f3-3, N4): una fila que solo sostiene un compañero lee también las
  revocaciones:

```python
async def test_revoking_for_me_an_app_only_a_colleague_holds_shows_it_revoked_for_me(
    env: Any,
) -> None:
    """An `orgs.manage` holder lists the apps everyone holds. Revoking for
    themselves one that only a colleague holds ends nothing, and the row says
    "revoked for you"; the colleague's grant and row stand."""
    org_id, slug = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    colleague = await env.api.create_user(org_id, "org-admin")
    await _approved(env, org_id, slug, colleague)
    app_id = str(await mcp_app(env.api, org_id))

    resp = await _revoke(env, org_id, me, "me", app=app_id)

    assert resp.json() == {"revoked": True, "grants_revoked": 0}
    (app,) = (await _connections(env, org_id, me)).json()["apps"]
    assert (app["id"], app["revoked_for_me"], app["revoked_for_everyone"]) == (app_id, True, False)
    (theirs,) = (await _connections(env, org_id, colleague)).json()["apps"]
    assert (theirs["revoked_for_me"], theirs["revoked_for_everyone"]) == (False, False)
    assert await _live(env, org_id, colleague) == 1
```
- `test_revoke_for_everyone_cuts_off_the_whole_org_and_no_other`: dentro de `running_mcp`, antes de revocar, cada uno
  abre su concesión (`await _call_a_tool(env, slug, admin)`, `… colleague`, `… other_slug, stranger`); la respuesta es
  `{"revoked": True, "grants_revoked": 2}`; las cuatro líneas de Keycloak (`deleted`, `token_requests`, `clients`)
  pasan a `assert env.kc.requests == []`; el evento, `{"client_id": "claude", "scope": "everyone", "grants_revoked":
  2}`; y **antes** de los `_initialize` (cada `_old_token` abre una concesión nueva), `assert await _live(env, org_id,
  admin) == 0`, `… colleague) == 0` y `assert await _live(env, other_id, stranger) == 1`; el comentario «Registered
  apps are realm-wide» pasa a «A metadata-document client has the same `client_id` in every org: the other org's grant
  and token are untouched.»
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

    org_id, slug = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    async with running_mcp(env.api):
        await _call_a_tool(env, slug, me)

    async def broken(*args: Any, **kw: Any) -> Any:
        raise OSError("database went away")

    if fails == "grants":
        monkeypatch.setattr(connections.grants, "revoke_app", broken)
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

- `test_consent_removal_is_bounded_in_concurrency_and_time` →
  `test_revoking_for_a_large_organization_ends_every_grant_in_the_request` (el nombre dice lo que mide: el tiempo y el
  recuento, no las sentencias; A3-57): fijaba que una organización grande no cuesta una llamada por persona:

```python
async def test_revoking_for_a_large_organization_ends_every_grant_in_the_request(
    env: Any,
) -> None:
    """Forty people with a grant each: one revocation ends all forty, inside
    the request, with no call per person to anyone."""
    org_id, _ = await _org(env)
    admin = await env.api.create_user(org_id, "org-admin")
    app_id = await mcp_app(env.api, org_id)
    resource = mcp_resource(env.api.settings)
    assert resource is not None
    subs = [await env.api.create_user(org_id, "technician") for _ in range(40)]
    people = [await _user_id(env, sub) for sub in subs]
    async with oauth_session(org_id, env.api.app_engine) as session:
        for user_id in people:
            await grants.open_family(
                session,
                org_id=org_id,
                user_id=user_id,
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

Cinco pruebas existentes comparan los metadatos enteros de un acto que ahora lleva el recuento (decisión 4); ganan
`"ai_grants_revoked": 0` (la persona no tenía ninguna concesión), y nada más cambia:

`tests/test_auth_signin.py:1024` (en `3a1ba1c`; era `:808` en `0182206`, re-revisión f3-3, N5):

```python
    assert [e.meta for e in ended] == [
        {"by": "self", "scope": "everywhere", "ai_grants_revoked": 0}
    ]
```

`tests/test_auth_password_reset.py:116`:

```python
    assert [e.meta for e in changed] == [{"via": "reset", "ai_grants_revoked": 0}]
```

`tests/test_auth_recover_command.py:367` y `:485` (las dos iguales):

```python
    assert [e.meta for e in changed] == [
        {"via": "reset", "ai_grants_revoked": 0},
        {"via": "recover"},
    ]
```

`tests/test_auth_recover_command.py:370`:

```python
    assert [e.meta for e in used] == [{"email": who.email, "ai_grants_revoked": 0}]
```

(El `{"via": "recover"}` es el `auth.password_changed` que `accept_recovery` escribe **antes** de revocar, y no cambia;
`tests/test_auth_recover_command.py:399` tampoco.)

- [ ] **Step 5: El recuento de logs**

**La base, la que tenga la worktree** (re-revisión f3-2, M-8). Sea N el número de `assert total == N` en
`backend/tests/test_logging_discipline.py` al empezar la tarea: 110 con las Tasks 3 y 7, y otro si la rama ya trae la fase 2 (su Task 5
añade un sitio) o el orden de fusión fue otro. El párrafo nuevo va **encima del párrafo «NOW N» cuyo N es el del `assert`** (en `3a1ba1c`, «NOW 108, own authentication
phase 1, fix 3 review Minor 2», `:441` de la prueba y `:178` del diseño; no el «NOW 65» de `:334` ni el «NOW 70» de `:135`, que
son los de más arriba) del docstring de la prueba del recuento, que pasa a «BEFORE THAT, …» (sin cambiar nada más de él); el titular del nuevo y
el `assert` (y el «expected N» de su mensaje) dicen **N−3** (abajo, 107, con N = 110). Lo mismo en el §2.2 de
`docs/superpowers/specs/2026-08-14-logging-kernel-design.md`: el párrafo de abajo va encima del «**NOW N**» cuyo N es el
del `assert` (el mismo párrafo nombrado arriba), que pasa a «**BEFORE THAT, …**», con el mismo número en su titular. El delta de esta tarea es **−3**.

En el docstring de la prueba del recuento:

```text
    NOW 107, own authentication phase 3 (plan 2026-11-06, Task 10): three
    sites fewer, all in `app/mcp/api.py` -- the two WARNINGs of
    `remove_consents` and the one of `_record_incomplete`. Revoking an AI app
    no longer calls the identity provider: its grants end in the same
    transaction as the revocation row and its audit event, so there is no
    best-effort half left to report.

```

En el §2.2 del diseño del kernel de logs:

```markdown
**NOW 107, own authentication phase 3 (plan 2026-11-06, Task 10).** Three
sites fewer, all in `app/mcp/api.py`: the two WARNINGs of `remove_consents`
and the one of `_record_incomplete`. Revoking an AI app no longer calls the
identity provider; its grants end in the same transaction as the
`mcp_revocation` row and the `mcp.app_revoked` event, so there is no
best-effort half left to report.

```

- [ ] **Step 6: Las pruebas pasan**

Run: `cd backend && .venv/bin/pytest -q -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning tests/test_mcp_grant_lifecycle.py tests/test_mcp_admin_api.py tests/test_global_writes.py tests/test_logging_discipline.py tests/test_i18n_catalog.py tests/test_client_catalog.py tests/test_mcp_channel_api.py tests/test_mcp_catalog.py tests/test_mcp_descriptions.py tests/test_ai_tool_surface.py tests/test_user_lifecycle.py tests/test_auth_my_security.py tests/test_auth_password_reset.py tests/test_auth_notice_order.py tests/test_auth_signin.py tests/test_auth_recover_command.py tests/test_route_gating.py`
Expected: PASS (los dos `-W error` hacen fallo de un `await` olvidado; Task 9, Step 7).

Run: `cd backend && grep -rn "revoke_all_for_user(session, user.id, " app | grep -vc "ai_grants.revoke_all_for_user"`
Expected: `0` (las siete llamadas nuevas van por el alias, ninguna por un nombre `grants` que una variable local pueda
tapar).

Run: `cd frontend && npx vitest run src/modules/audit src/i18n`
Expected: PASS (ninguna acción nueva; `mcp.app_revoked` ya está en el vocabulario).

Run: `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `Success: no issues found` y `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/core/auth/oauth/connections.py backend/app/mcp/api.py backend/app/mcp/exclusions.py \
  backend/app/core/i18n/es.py backend/app/modules/user_admin/service.py backend/app/core/auth/account.py \
  backend/app/core/auth/flows.py backend/tests/test_mcp_admin_api.py backend/tests/test_global_writes.py \
  backend/tests/test_mcp_grant_lifecycle.py backend/tests/test_client_catalog.py \
  backend/tests/test_auth_signin.py backend/tests/test_auth_password_reset.py backend/tests/test_auth_recover_command.py \
  backend/tests/test_logging_discipline.py docs/superpowers/specs/2026-08-14-logging-kernel-design.md
git commit -m "feat(mcp): revoking an AI app ends its grants in one transaction; account acts end them too"
```

### Task 11: Consola — Ajustes → Conexiones de IA: revocar por id, el nombre del cliente

Diseño §5.3; D12; B3-10, B3-28. Oleada 1, en paralelo con las Tasks 1–4: se escribe contra el contrato del Apéndice
A.2 y la Task 10 lo implementa en el servidor. Solo consola.

Hoy la pestaña revoca por `client_id` en la ruta (`/mcp/connections/{client_id}/revoke`) y avisa cuando Keycloak no
pudo retirar el consentimiento de alguien (`keycloak_failures`). Con el servidor propio, el `client_id` de un cliente CIMD
es una URL que no cabe en un segmento de ruta, y la revocación es una sola transacción sin mitad que pueda fallar. Así
que: la fila trae `id` (el de la fila del cliente; `null` si ya no tiene registro aquí), `name` (el nombre que el
cliente se dio), `host` y `kind`; se titula por el host, que la app no puede disfrazar, con su nombre debajo como
afirmación suya y la marca de «se registró sola» para una app de DCR (B3-10, A3-62; como la pantalla de
consentimiento, Task 4); se revoca por `id`, y el resultado es `{revoked, grants_revoked}`. Una app con una concesión
viva que aún no llamó herramientas también sale (Task 10, B3-28), sin personas ni último uso.

**Files:**
- Modify: `frontend/src/lib/api.ts` (`McpAppOut`, `McpRevokeOut`, `revokeMcpConnection` y sus comentarios; el
  comentario de `RequestRoute.global`)
- Modify: `frontend/src/modules/settings/api.ts` (`useRevokeMcpConnection`)
- Modify: `frontend/src/modules/settings/AiConnectionsTab.tsx`
- Modify: `frontend/src/modules/settings/fixtures.ts` (el comentario de `MCP_APPS`, `MCP_APPS` y `revokeMcpConnection`)
- Modify: `frontend/src/modules/settings/__tests__/harness.tsx` (`MCP_APP_FIXTURE` y el `mockResolvedValue` de
  `calls.revokeMcpConnection`)
- Modify: `frontend/src/i18n/en/settings.ts`, `frontend/src/i18n/es/settings.ts` (`aiConnections`)
- Modify: `frontend/src/modules/settings/SettingsPage.tsx` (la fila «AI connections» de la tabla de rutas; A3-60)
- Test: `frontend/src/modules/settings/__tests__/aiConnections.test.tsx`, `frontend/src/modules/settings/settings.es.test.tsx`
  («words the warning when sign-in could not be withdrawn, for one person and for you» y la que fija el texto de la
  lista vacía), `frontend/src/lib/api.test.ts` («puts an MCP client id in the revoke path as one encoded segment…»),
  `frontend/src/lib/api.clientScope.test.ts` (las llamadas con `"some-ai-app"`)

**Cada edición se ancla por el texto que cita, nunca por un número de línea** (re-revisión f3-3, X8). La fase 2 edita
los mismos cuatro archivos de la consola —`harness.tsx`, `fixtures.ts`, la tabla de rutas de `SettingsPage.tsx` e
`i18n/{en,es}/settings.ts`— (Puntos de encuentro con la fase 2), y con su rama fusionada las líneas se mueven. Esta
tarea corre en la oleada 1 y puede verlas sin moverse; las anclas de texto valen en los dos casos.

**Interfaces:**
- Consumes: el contrato del Apéndice A.2 (`GET /api/v1/mcp/connections` con `id`, `name`, `host` y `kind` por app;
  `POST /api/v1/mcp/connections/{app_id}/revoke` → `{"revoked": true, "grants_revoked": int}`).
- Produces: `McpAppOut.id: string | null`, `McpAppOut.name: string | null`, `McpAppOut.host: string | null`,
  `McpAppOut.kind: "registered" | "metadata_document" | null`, `McpRevokeOut = { revoked: boolean; grants_revoked:
  number }`, `ApiClient.revokeMcpConnection(appId: string, scope: McpRevokeScope)`, `useRevokeMcpConnection()` con
  variables `{ appId: string; label: string; scope: McpRevokeScope }`.

**Pruebas que cambian a propósito (re-apuntadas, no borradas).** Las tres de `aiConnections.test.tsx` sobre
`keycloak_failures` («warns when sign-in for some people could not be withdrawn…», «words a one-person failure in the
singular», «words a failed revoke for me as being about you…») y la de `settings.es.test.tsx` («words the warning when
sign-in could not be withdrawn…») probaban la mitad de Keycloak, que desaparece. Pasan a probar lo que la ocupa: que
una revocación nunca anuncia un resultado parcial (siempre la frase de revocada, cuente `grants_revoked` lo que cuente),
que una app sin registro no ofrece revocar, y que se nombra por su host, con el nombre que se dio como afirmación suya y
su `client_id` debajo, en los dos idiomas. Las de `api.test.ts` y `api.clientScope.test.ts` cambian la ruta de ejemplo a
un UUID, y la de `api.test.ts` conserva lo que fijaba (A3-61): que el id viaja como **un** segmento codificado.

**El nombre de la fila en las pruebas que no cambian de propósito.** Con `host: "claude.ai"` en `MCP_APP_FIXTURE`, la
fila y las frases nombran `claude.ai`: en `aiConnections.test.tsx` y `settings.es.test.tsx`, cada `findByText("claude")`
/ `getByText("claude")` que busca la fila o el diálogo pasa a `"claude.ai"`, y cada frase de resultado («claude is
revoked…», «Se revocó claude…») nombra `claude.ai`; las expresiones `/Revoke for me: claude/` y `/Revocar para mí:
claude/` ya casan con `…: claude.ai` y no cambian; `toHaveBeenCalledWith("claude", …)` pasa a `(MCP_APP_ID, …)`; y
cada `expectNoEnglish(…, [...RECORDED, "claude"])` gana `"claude.ai"` (un host es un dato, como el `client_id`).

- [ ] **Step 1: Las pruebas que fallan**

En `frontend/src/modules/settings/__tests__/harness.tsx`, `MCP_APP_FIXTURE` pasa a:

```ts
/** The `oauth_clients` row of `MCP_APP_FIXTURE` (`id`): what a revoke names. */
export const MCP_APP_ID = "7a1c2e9b-4f3d-4c55-9a0e-2b6d8f1e3c70";

export const MCP_APP_FIXTURE = {
  id: MCP_APP_ID,
  client_id: "claude",
  name: null,
  host: "claude.ai",
  kind: "registered" as const,
  people: 3,
  last_used_at: "2026-10-01T09:30:00Z",
  revoked_for_me: false,
  revoked_for_everyone: false,
};
```

(el comentario de arriba gana: «`name` null: an app that gave itself no name; the row is titled by its host»), y
la línea `calls.revokeMcpConnection.mockResolvedValue({ revoked: true, keycloak_failures: 0 });` pasa a
`calls.revokeMcpConnection.mockResolvedValue({ revoked: true, grants_revoked: 1 });`.

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

  it("names an app by its host, with the name it gave itself as its own claim", async () => {
    // B3-10: anyone can register any name; the host is what the app cannot fake.
    calls.getMcpConnections.mockResolvedValue({
      ...MCP_CONNECTIONS_FIXTURE,
      apps: [{ ...MCP_APP_FIXTURE, name: "Claude", client_id: "itms_Zm9vYmFy" }],
    });
    const user = userEvent.setup();
    openTab();
    const row = (await screen.findByText("claude.ai")).closest("tr") as HTMLElement;
    expect(within(row).getByText("It calls itself “Claude”.")).toBeInTheDocument();
    expect(within(row).getByText("It registered itself here: any app can, under any name.")).toBeInTheDocument();
    expect(within(row).getByText("itms_Zm9vYmFy")).toBeInTheDocument();
    await user.click(within(row).getByRole("button", { name: "Revoke for me: claude.ai" }));
    await waitFor(() => expect(calls.revokeMcpConnection).toHaveBeenCalledWith(MCP_APP_ID, "me"));
    expect(await screen.findByText("claude.ai is revoked for you.")).toBeInTheDocument();
  });

  it("names a metadata-document app by its document's host, with no self-registered mark", async () => {
    calls.getMcpConnections.mockResolvedValue({
      ...MCP_CONNECTIONS_FIXTURE,
      apps: [
        {
          ...MCP_APP_FIXTURE,
          client_id: "https://claude.example/oauth/client.json",
          name: "Claude Code",
          host: "claude.example",
          kind: "metadata_document",
        },
      ],
    });
    openTab();
    const row = (await screen.findByText("claude.example")).closest("tr") as HTMLElement;
    expect(within(row).getByText("It calls itself “Claude Code”.")).toBeInTheDocument();
    expect(within(row).queryByText(/registered itself/)).toBeNull();
  });

  it("shows an app approved a moment ago, which has called nothing yet", async () => {
    calls.getMcpConnections.mockResolvedValue({
      ...MCP_CONNECTIONS_FIXTURE,
      apps: [{ ...MCP_APP_FIXTURE, people: 0, last_used_at: null }],
    });
    openTab();
    const row = (await screen.findByText("claude.ai")).closest("tr") as HTMLElement;
    expect(within(row).getByRole("button", { name: /Revoke for me: claude\.ai/ })).toBeInTheDocument();
  });

  it("offers no revoke for an app that no longer has a registration here", async () => {
    calls.getMcpConnections.mockResolvedValue({
      ...MCP_CONNECTIONS_FIXTURE,
      apps: [{ ...MCP_APP_FIXTURE, id: null, host: null, kind: null }],
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
    await user.click(await screen.findByRole("button", { name: "Revocar para todos: claude.ai" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Revocar para todos" }));
    expect(
      await screen.findByText("Se revocó claude.ai para todas las personas de esta organización."),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Revocar para mí: claude.ai" }));
    expect(await screen.findByText("Se revocó claude.ai para ti.")).toBeInTheDocument();
    expect(screen.getByText("Se registró sola aquí: cualquier aplicación puede, con cualquier nombre.")).toBeInTheDocument();
    expectNoEnglish(container, [...RECORDED, "claude", "claude.ai"]);
    unmount();

    calls.getMcpConnections.mockResolvedValue({
      ...MCP_CONNECTIONS_FIXTURE,
      apps: [{ ...MCP_APP_FIXTURE, id: null, host: null, kind: null, name: "Claude" }],
    });
    const again = await openIn("ai-connections");
    expect(await screen.findByText("Ya no está registrada aquí, así que no tiene acceso.")).toBeInTheDocument();
    expect(screen.getByText("Dice llamarse «Claude».")).toBeInTheDocument();
    expectNoEnglish(again.container, [...RECORDED, "claude", "Claude"]);
  });
```

(`openIn` devuelve lo que devuelve `renderSettings`, que extiende el resultado de `render`, así que trae `unmount`; el
import del archivo gana `MCP_APP_FIXTURE` junto a `MCP_CONNECTIONS_FIXTURE`.)

En `frontend/src/lib/api.test.ts`, la prueba «puts an MCP client id in the revoke path as one encoded segment, with
the scope in the body», con el comentario que la precede, pasa a:

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
    // Whatever it is handed, the id travels as ONE encoded segment (A3-61): a
    // slash or a query in it never reaches another route.
    await client.revokeMcpConnection("a/b?c", "me");
    const [odd] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(odd).toBe("/api/v1/mcp/connections/a%2Fb%3Fc/revoke");
  });
```

En `frontend/src/lib/api.clientScope.test.ts`, en el bloque cuyo comentario empieza «A route that is global BY ITS
BODY», `"some-ai-app"` pasa a `"7a1c2e9b-4f3d-4c55-9a0e-2b6d8f1e3c70"` en las dos llamadas y en las dos rutas, y el
comentario dice `/mcp/connections/{app_id}/revoke`. Lo que prueban no cambia.

**La lista ya no es solo de apps usadas** (re-revisión f3-3, N3): con B3-28 trae también las que tienen una concesión
viva y no llamaron nada. En `aiConnections.test.tsx`, la prueba «says so when no AI app has been used here» pasa a
llamarse «says so when no AI app has been used here or holds access», y su `findByText` a
`"No AI app has been used in this organization in the last 90 days, and none holds access."`; en
`settings.es.test.tsx`, el `getByText("Ninguna aplicación de IA se ha usado en esta organización en los últimos 90
días.")` pasa a `getByText("Ninguna aplicación de IA se ha usado en esta organización en los últimos 90 días, y
ninguna tiene acceso.")`.

Run: `cd frontend && npx vitest run src/modules/settings src/lib/api.test.ts src/lib/api.clientScope.test.ts`
Expected: FAIL (el tipo `McpAppOut` no tiene `id`; la pestaña revoca por `client_id`; no existe
`apps.unregistered`; el texto de la lista vacía es el de antes).

- [ ] **Step 2: Los tipos y el método**

En `frontend/src/lib/api.ts`, `McpAppOut` pasa a:

```ts
/**
 * One AI app that called tools in this organization in the last 90 days, or
 * that holds a live grant here and has not yet (`backend/app/mcp/api.py::
 * McpAppOut`; B3-28).
 */
export interface McpAppOut {
  /** Its registration here (`oauth_clients`), what a revoke names; null when it has none any more. */
  id: string | null;
  /** The app's OAuth client id: what it registered, or its metadata document's URL. */
  client_id: string;
  /** The name the app gave itself, or null. Anyone can call an app anything. */
  name: string | null;
  /**
   * What the row is titled by, which the app cannot fake: its metadata
   * document's host, or the host(s) its registered redirects go to; null
   * without a registration (B3-10).
   */
  host: string | null;
  /** `registered`: it registered itself here (any app can, under any name). */
  kind: "registered" | "metadata_document" | null;
  /** How many people of this organization used it in the last 90 days; 0 for one approved and not used yet. */
  people: number;
  /** When anyone here last called a tool through it; null when nobody has yet. */
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
       if (app.id !== null) revoke.mutate({ appId: app.id, label: app.host ?? app.client_id, scope });
     }
   ```

   `confirming` pasa a guardar la app (`useState<McpAppOut | null>(null)`), la `ConfirmDialog` de «para todos» usa
   `subject={confirming ? confirming.host ?? confirming.client_id : ""}` y `revokeFor(target, "everyone")`, y la fila
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
4. `AppRow`: la etiqueta es `const label = app.host ?? app.client_id;` (B3-10: el host, que la app no puede
   disfrazar; sin registro, el `client_id`); la celda del nombre:

   ```tsx
         <Td lead={<Icon name="sparkles" size={18} />}>
           {/* The host and the client id are recorded, not translated; the name is the app's own claim. */}
           <div className="cell-title">{label}</div>
           {app.name !== null ? (
             <div className="cell-sub">{t("settings.aiConnections.apps.calledItself", { name: app.name })}</div>
           ) : null}
           {app.kind === "registered" ? (
             <div className="cell-sub">{t("settings.aiConnections.apps.selfRegistered")}</div>
           ) : null}
           {app.host !== null ? <div className="cell-sub mono">{app.client_id}</div> : null}
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
      calledItself: "It calls itself “{name}”.",
      selfRegistered: "It registered itself here: any app can, under any name.",
      unregistered: "No longer registered here, so it holds no access.",
```

y se quitan `partialMe` y `partial` de `aiConnections`. En el mismo `aiConnections.apps`, `subtitle` y `empty`
pasan a decir que la lista trae también las apps con acceso sin usar (N3):

```ts
      subtitle: "Apps people here used in the last 90 days, most recent first, then apps holding access that have not been used yet",
      empty: "No AI app has been used in this organization in the last 90 days, and none holds access.",
```

En `frontend/src/i18n/es/settings.ts`, lo mismo:

```ts
      calledItself: "Dice llamarse «{name}».",
      selfRegistered: "Se registró sola aquí: cualquier aplicación puede, con cualquier nombre.",
      unregistered: "Ya no está registrada aquí, así que no tiene acceso.",
```

se quitan `partialMe` y `partial`, y `subtitle` y `empty` pasan a:

```ts
      subtitle: "Aplicaciones que las personas de esta organización han usado en los últimos 90 días, las más recientes primero, y después las que tienen acceso y aún no se han usado",
      empty: "Ninguna aplicación de IA se ha usado en esta organización en los últimos 90 días, y ninguna tiene acceso.",
```

En `frontend/src/modules/settings/SettingsPage.tsx`, en la fila `AI connections` del comentario de la tabla de rutas,
`POST /mcp/connections/{client_id}/revoke` pasa a `POST /mcp/connections/{app_id}/revoke` (A3-60).

En `frontend/src/modules/settings/fixtures.ts`, el comentario de encima de `MCP_APPS` termina, en lugar de «The apps
are ones used before it was turned off: the list is derived from audited use, not from the switch.», con «The apps
are ones used, or approved, before it was turned off: the list is derived from audited use and live grants, not from
the switch.» (N3); y:

```ts
const MCP_APPS: McpAppOut[] = [
  {
    id: "0b6f3c1e-8d2a-4e7b-9c5f-1a2b3c4d5e6f",
    client_id: "itms_Q2xhdWRlRGVza3RvcEZpeHR1cmU",
    name: "Claude",
    host: "claude.ai",
    kind: "registered",
    people: 4,
    last_used_at: ago(12),
    revoked_for_me: false,
    revoked_for_everyone: false,
  },
  {
    id: "5d4c3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a",
    client_id: "https://helpdesk.example/oauth/client.json",
    name: "Helpdesk assistant",
    host: "helpdesk.example",
    kind: "metadata_document",
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
  frontend/src/lib/api.clientScope.test.ts frontend/src/modules/settings/SettingsPage.tsx
git commit -m "feat(console): AI connections revoke an app by its registration, with no identity-provider half"
```

### Task 12: Cierre: el falsificador 6 de §10 de punta a punta, documentos, recuentos y el informe

Diseño §9, §10 (falsificador 6); §8 fase 3; decisiones del operador C1 y C2 (2026-11-06). Oleada 6, sola, sobre el
árbol con todas las demás fusionadas (la 13 incluida). Backend y documentos.

Lo que ninguna tarea anterior fija sola: el falsificador 6 de §10 («Reutilizar un refresh token revoca su familia; un
token MCP con otra audiencia se rechaza; el token interno no vale en rutas públicas») sobre las rutas reales, de punta
a punta; la conexión entera tal como la hace Claude —con un técnico que la plataforma invitó (el D5 de la fase 1:
hasta esta fase no podía conectar una app), y con una app conocida por su documento CIMD, sin la opción del registro
(C2)—; el runbook y los documentos que dicen «hasta la fase 3», y las enmiendas del diseño que piden C1 y C2; los
recuentos que mueven varias tareas, comprobados en el árbol fusionado; y el informe para el operador.

**Files:**
- Create: `backend/tests/test_oauth_falsifiers.py`
- Modify: `deploy/README.md` (la sección «AI connections (MCP)»; tres frases de otras secciones; una fila de la tabla
  del cliente de aprovisionamiento)
- Modify: `deploy/.env.example` (los comentarios de `KC_HOSTNAME` y de `ITS_MCP_REGISTRATION`)
- Modify: `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` (§2.5, §2.7 y §5.4, enmiendas fechadas;
  C1, C2)
- Modify: `docs/superpowers/specs/2026-10-09-mcp-server-design.md` (§3, enmienda fechada)
- Modify: `docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` (§7.2 y §7.4, enmiendas fechadas)
- Modify: `docs/03-security-model.md` (línea 161), `docs/modules/console-auth/spec.md` (líneas 98–99)
- Modify (comentarios): `backend/app/modules/user_admin/service.py`, `backend/app/modules/user_admin/__init__.py`,
  `backend/app/api/v1/me.py`, `backend/app/core/auth/__init__.py`, `backend/tests/test_user_admin_api.py`,
  `backend/tests/conftest.py`

**Interfaces:**
- Consumes: Task 3 (`cimd.ClientDocument`, `cimd.MIN_CACHE`), Task 6 (`tests.oauth_support.register`), Task 7
  (`token_request`), Task 8 (`connect`), Task 9 (`tests.mcp_support.mcp_claims`, `signed_mcp_token`), Task 5
  (`oauth_org`, `oauth_http`, `BASE`, `RESOURCE`, `CLAUDE_CALLBACK`). Existentes: `ApiEnv.loopback_bearer(org_id,
  sub)`, `ApiEnv.create_user(..., sub=)`, `tests.mcp_support.mcp_session`, `marked_client`, `mcp_client`,
  `running_mcp`, `app.modules.user_admin.service.LOCAL_SUBJECT_PREFIX`,
  `tests/test_loopback.py::test_the_token_from_the_network_is_refused`.
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
  token mints a token; drop the `family_is_live` block from `deps.admit`, and
  the family's newest access token still works;
- drop the exact-audience check from `access.verify_access_token`, and a token
  of ours for two audiences is admitted;
- let `/mcp`, `/oauth/token` or the MCP door read the assistant's internal
  token, and it is admitted.

And the whole way, as Claude does it -- register (or be known by a client
metadata document, with no registration opt-in: operator decision C2), consent
in the console, exchange the code, call a tool -- by a technician the platform
invited, whose subject no identity provider issued (phase 1's D5, closed by
this phase).
"""

from typing import Any
from uuid import uuid4

import httpx
import pytest
from mcp.types import LATEST_PROTOCOL_VERSION
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.oauth import cimd
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
    CLAUDE_CALLBACK,
    RESOURCE,
    connect,
    oauth_http,
    oauth_org,
    register,
    token_request,
)
from tests.session_support import user_id_of

DOCUMENT_URL = "https://claude.example/oauth/client-metadata.json"
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


async def _tool_called(env: Any, org_id: Any) -> AuditEvent:
    async with AsyncSession(env.admin_engine) as session:
        (event,) = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id, AuditEvent.action == "mcp.tool_called"
                    )
                )
            )
            .scalars()
            .all()
        )
    return event


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
    `/mcp`, on the OAuth server and on an MCP call it is nothing. The same
    token sent from the network WITH that header is
    `tests/test_loopback.py::test_the_token_from_the_network_is_refused`'s
    (A3-66); this one holds the routes it never belongs on."""
    org_id, _ = await oauth_org(api_env)
    sub = await api_env.create_user(org_id, "org-admin")
    app = await register(api_env)
    bearer = await api_env.loopback_bearer(org_id, sub)

    async with running_mcp(api_env):
        mcp = await _initialize(api_env, bearer)
    async with oauth_http(api_env) as client:
        token = await client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": "x",
                "client_id": app["client_id"],
            },
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
    event = await _tool_called(api_env, org_id)
    assert event.actor_id == await user_id_of(api_env, sub)
    assert event.meta["app"] == issued["client_id"]
    assert issued["client_id"].startswith("itms_")


async def test_an_app_known_by_its_metadata_document_needs_no_registration_opt_in(
    api_env, monkeypatch
) -> None:
    """Operator decision C2 (2026-11-06): with MCP on, an app that publishes a
    client metadata document connects with `ITS_MCP_REGISTRATION` unset --
    and the same deployment refuses a registration."""

    async def document(client_id: str) -> cimd.ClientDocument:
        assert client_id == DOCUMENT_URL
        return cimd.ClientDocument(
            client_id=DOCUMENT_URL,
            client_name="Claude",
            redirect_uris=(CLAUDE_CALLBACK,),
            cache_for=cimd.MIN_CACHE,
        )

    monkeypatch.setattr(cimd, "fetch_document", document)
    org_id, _ = await oauth_org(api_env)
    api_env.settings.mcp_registration = ""
    sub = await api_env.create_user(org_id, "org-admin")
    async with oauth_http(api_env) as client:
        registration = await client.post(
            "/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]}
        )
    assert (registration.status_code, registration.json()["error"]) == (403, "access_denied")

    issued = await connect(api_env, org_id, sub, client_id=DOCUMENT_URL)
    async with running_mcp(api_env), mcp_session(api_env, issued["access_token"]) as session:
        result = await session.call_tool("me_get", {})

    assert result.is_error is False, result
    assert (await _tool_called(api_env, org_id)).meta["app"] == DOCUMENT_URL
```

Run: `cd backend && .venv/bin/pytest -q -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning tests/test_oauth_falsifiers.py`
Expected: PASS sobre el árbol fusionado (las Tasks 1–11 y 13 ya construyeron lo que prueba). Para ver cada
falsificador en rojo una vez, antes de dar la tarea por buena: quitar de `deps.admit` el bloque entero `if not await
grants.family_is_live(...): raise HTTPException(...)` (las dos líneas del `if` y la del `raise` con sus argumentos; solo
la del `if` deja un `raise` sin condición, que lo rechaza todo: A3-68), y la primera prueba falla en `== 401`;
devolverlo. Cambiar `claims["aud"] != aud` por `aud not in claims["aud"]` en `access.verify_access_token`, y falla
`two-audiences`; devolverlo. El informe de la tarea dice las dos salidas rojas.

- [ ] **Step 2: El runbook**

En `deploy/README.md`:

Cada edición se ancla por el texto que cita; los números de línea son los de `3a1ba1c` (re-revisión f3-3, N5) y solo
orientan.

1. En «Status of this runbook», **solo la línea** `Keycloak still signs MCP clients in until phase 3. The sections of
   this` (la 32) pasa a estas tres, sin tocar ninguna otra línea del párrafo (re-revisión f3-3, X11: la fase 2 edita
   la frase «Three things phase 1 changed…», que en `3a1ba1c` empieza en la línea 36, cuatro más abajo; si ninguno de
   los dos refluye el párrafo, git fusiona los dos cambios):

   ```markdown
   Since own authentication phase 3 AI apps connect over MCP through the
   platform's own OAuth server too; Keycloak signs nobody in, and phase 4
   removes it. The sections of this
   ```

2. Líneas 1240–1241, «What follows is Keycloak's login page, which only MCP clients meet until own authentication phase
   3.» pasa a «What follows is Keycloak's login page, which nobody meets since own authentication phase 3 (AI apps
   approve on the console's own consent page); it goes in phase 4.»
3. Líneas 1475–1476, «so the claim is now only in the access tokens MCP clients obtain, and the devtools one-liner…»
   pasa a «and since own authentication phase 3 no token Keycloak issues is accepted anywhere, so no token carries the
   claim to check; the devtools one-liner…» (el resto de la frase igual).
4. En la tabla de mitigaciones del cliente de aprovisionamiento (línea 2010), «remove one AI app's consent for a user
   (revoking an app under Settings → AI connections; needs nothing beyond `manage-users`)» pasa a «remove one AI app's
   consent for a user (no caller since own authentication phase 3, when revoking an app stopped involving Keycloak; it
   goes with Keycloak in phase 4)». «The code calls exactly six operations» no cambia: lo fija
   `tests/test_console_auth_realm.py`, y `kc_client.py` sigue definiendo las seis.
5. En `### What is never reachable over MCP`, la lista de lo «Refused twice» gana, al final (A3-64: una app nunca
   aprueba a otra):

   ```markdown
   - approving an AI app on the console's consent page, whose one-time code
     becomes the person's MCP tokens: an app can never approve itself or
     another.
   ```

6. Tres sustituciones, cada una de un encabezado hasta **justo antes** del siguiente que se nombra (re-revisión
   f3-3, N6; en `3a1ba1c` el orden es `## AI connections (MCP)`, `### Turning it on`, `### What keycloak-init
   converges`, `### What Traefik routes`, `### Revoking`, `### What is never reachable over MCP`, `### Residual risks,
   stated plainly` y `## Publishing the agent to the console`):
   - desde `## AI connections (MCP)` hasta justo antes de `### Revoking` (es decir, hasta el final de `### What
     Traefik routes`): el primer bloque de abajo;
   - desde `### Revoking` hasta justo antes de `### What is never reachable over MCP`, el siguiente `###`: el
     segundo;
   - desde `### Residual risks, stated plainly` hasta justo antes de `## Publishing the agent to the console`, el
     siguiente `##`: el tercero.

   `### What is never reachable over MCP` queda en su sitio, entre la segunda y la tercera, con la línea del punto 5.

El primer bloque:

````markdown
## AI connections (MCP)

An AI app — Claude, or any client that speaks the Model Context Protocol —
can use ITMS **as the person who connected it**: it sees what that person
sees and can do what their role allows, every call is in the audit log
(`mcp.tool_called`), and the app's own settings decide which tools need the
person's approval. Approving an app is audited too (`mcp.app_authorized`); a
call over MCP is not a sign-in, so the person's last sign-in stays the last
time they signed in to the console. The server is `/mcp` on the `api`
container, and the platform is its own OAuth 2.1 authorization server; there
is no new service. Design: `docs/superpowers/specs/2026-10-09-mcp-server-design.md`,
and for the OAuth server `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §5.

**It is off by default, three times over.** No AI app connects until all
three hold:

1. the deployment has a public https address **with no path**
   (`ITS_PUBLIC_CONSOLE_URL`) — without one, `/mcp` answers 503; with a path
   after the host (`https://example.com/itms`), too, because an OAuth issuer is
   an https address with no path, so the platform has none and says so;
2. an org-admin has turned on **Settings → AI connections → Allow AI apps to
   connect over MCP** for the organization;
3. the person holds the `mcp.use` permission.

The second and third are checked on every call, not only at connection time,
and the consent page checks them before it reads anything about the app.

**How an app introduces itself.** Claude and Claude Code publish a *client
metadata document* at an https address of their own; the platform reads it
when a signed-in person opens the consent page, and that needs no further
setting (operator decision 2026-11-06). An app that can only *register
itself* (OAuth dynamic client registration) needs the operator's opt-in,
`ITS_MCP_REGISTRATION=open`. Without it the platform refuses every
registration and does not advertise the endpoint; with it, registration is
still refused while MCP is off for the organization, and the platform holds
at most 200 live registrations. Closing registration again stops new apps
registering; an app that registered while it was open keeps its registration
until it goes unused for 30 days, when the daily sweep removes it.

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
2. **Deploy** as in "Upgrading an existing deployment". The `api` container
   reads the setting when it starts.
3. **Check it.** The platform's OAuth metadata names the console as its
   issuer:

   ```sh
   curl -s "$(sed -n 's/^ITS_PUBLIC_CONSOLE_URL=//p' .env)/.well-known/oauth-authorization-server"
   ```

   The answer is JSON whose `issuer` is your console address and which carries
   `"client_id_metadata_document_supported": true`. It carries
   `registration_endpoint` only when you opted in to registration (step 7)
   and MCP is on. A 503 with `temporarily_unavailable` means
   `ITS_PUBLIC_CONSOLE_URL` is not an https address without a path (step 1).
4. **Settings → AI connections:** an org-admin turns on **Allow AI apps to
   connect over MCP**. The tab shows the connection address.
5. **Settings → Roles:** grant `mcp.use` to the roles that may connect.
   Org-admins hold `mcp.use` by default; nobody else does until you grant it.
6. **Connect an AI app.** In the app (in Claude: add a custom connector),
   give it the connection address. The app opens the console's consent page;
   the person signs in to the console first if they are not (password and
   second factor, as always). The page is titled by the host the app cannot
   disguise — its metadata document's, or the one its answer goes back to —
   and shows the name the app gave itself as the app's own claim; for an app
   that only answers on the person's own computer, it says so. **Approving
   needs the person's second factor confirmed in the last ten minutes**, on a
   remembered browser too, and the person then receives a mail, "You
   connected <host>" (operator decision 2026-11-06). Approving lets the app
   act as the person. Then, in the app's settings for that connector, set
   each tool to *Always allow* or *Needs approval*; tools that change
   something are safest on *Needs approval*.
7. **Only for an app that cannot publish a metadata document:** opt in to
   registration. Read "Residual risks, stated plainly" below first: risks 3,
   7 and 8 exist only while it is set. From the directory holding
   `docker-compose.yml` and `.env`, this removes any earlier
   `ITS_MCP_REGISTRATION` line and adds the one that opens registration:

   ```sh
   sed -i '/^ITS_MCP_REGISTRATION=/d' .env && echo 'ITS_MCP_REGISTRATION=open' >> .env
   ```

   Only the exact word `open` opens it; any other value, or no line at all,
   keeps it closed. Deploy again for the `api` container to read it.

The consent page, like every console page, is never shown inside another
site's frame: nginx answers every console request with
`Content-Security-Policy: frame-ancestors 'none'` and `X-Frame-Options: DENY`
(`deploy/nginx.conf`, held by `backend/tests/test_deploy_frame_headers.py`), so
a page elsewhere cannot overlay it and trick a click on **Approve**.

### What keycloak-init still converges (until phase 4)

Since own authentication phase 3 none of this is in MCP's path: `/mcp`
accepts only the platform's own access tokens, and refuses every token
Keycloak issues, whatever its audience. keycloak-init keeps converging it on
every run until phase 4 removes Keycloak, **and while
`ITS_MCP_REGISTRATION=open` it still opens Keycloak's own anonymous
registration** (residual risk 7 below). Nothing is deployed before phase 4
removes Keycloak (operator decision 2026-10-31), so no host runs in this
state. In this order, so that a run that dies part-way leaves registration
closed:

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
| `/.well-known/jwks.json` | `api` — the public halves of the platform's signing keys (own authentication phase 1); AI apps do not need it, the platform verifies its own tokens |
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
````

El segundo:

````markdown
### Revoking

- **One app:** Settings → AI connections lists every app that called a tool
  here in the last 90 days **and every app that holds a live grant**, so one
  approved a moment ago is there before it has done anything. **Revoke for
  me**, or, for an org-admin, **Revoke for everyone**, which needs both
  `mcp.use` and `orgs.manage` (an org-admin holds both). The app's grants end
  — yours, or everyone's in this organization — in the same transaction as
  the record of the revocation: every token the app holds stops at its next
  call, and its refresh tokens mint nothing. No identity provider is
  involved, so there is no second half that can fail. It never affects
  another organization. An app listed without a **Revoke** button last acted
  here before it registered with the platform, or its registration expired:
  it holds no grant here.
- **Revoking is not a block-list.** It ends the grants the app holds now; it
  does not stop the app coming back. A person can approve the same app again,
  and an app can publish another metadata document, or (with the opt-in)
  register again, under any name it likes. To keep AI apps out, use one of
  the stops below.
- **One person:** take `mcp.use` away from their role. Their next call is
  refused. Disabling or deleting the person, resetting their factors, a
  password change and "Sign out everywhere" end all their AI grants too, and
  the act's own audit event says how many (`ai_grants_revoked`).
- **The whole organization — the emergency stop:** Settings → AI
  connections → turn **Allow AI apps to connect over MCP** off. Every app is
  refused from its next call, whatever it is. **Turning it back on ends no
  grant and restores every one that is still live:** the apps connected
  before work again at once. To have people approve them again, revoke the
  apps for everyone before turning the switch back on.
- **New apps that register themselves, deployment-wide:** delete the
  `ITS_MCP_REGISTRATION=` line from `.env` and redeploy. The platform refuses
  every new registration; apps already registered, and apps known by a
  metadata document, are unaffected, so pair it with one of the stops above
  or below.
- **The whole deployment — the deployment-wide stop:** empty
  `ITS_PUBLIC_CONSOLE_URL` and redeploy. `/mcp` answers 503 and the platform
  has no OAuth issuer. This also turns off everything else that uses the
  address — the console's own sign-in, Email intake's Microsoft sign-in, the
  install link — so it is a last resort, not a switch.
````

Y el tercero, la sección de riesgos residuales:

````markdown
### Residual risks, stated plainly

**Risks 3, 7 and 8 exist only while self-registration is open**
(`ITS_MCP_REGISTRATION=open`); closing it ends them for new apps. **Risks 1,
2, 4 and 10 exist whenever MCP is on**, because an app known by its client
metadata document needs no opt-in. Risks 5, 6 and 9 hold always.

1. **Consent phishing.** Anyone can publish a client metadata document — or,
   with the opt-in, register an app — under any name, and send a person a
   link to the consent page; that page is the only check between the link
   and the person's full authority. It is titled by the host the app cannot
   disguise (its document's, or the one its answer goes back to; ASCII only,
   so no look-alike letters), shows the app's chosen name only as its own
   claim, says what approving hands over — "If you approve, it can act as you
   in ITMS: everything your role allows, including running actions on managed
   computers" — needs the person's second factor confirmed in the last ten
   minutes, and mails them "You connected <host>" afterwards. Org-admins
   should approve only apps they connected themselves, and anyone who gets
   that mail for an app they do not recognise should revoke it.
2. **An app known by its client metadata document makes the platform fetch
   that address**, only inside a signed-in person's consent, with MCP on and
   `mcp.use` held. The fetch goes through the platform's outbound guard (only
   public addresses — no private, loopback, link-local or other special-use
   ones (RFC 6890) — port 443 only, no redirects), is cut at 5 KiB and 5
   seconds, and its answer is cached as its own cache headers say, bounded to
   between 5 minutes and 24 hours. A document that is refused is never
   stored; one that cannot be read just now keeps the copy already stored.
   A signed-in person can still make the `api` container send one bounded
   request to a public address of their choosing each time it needs the
   document. Refusals are logged at most once a minute.
3. **Registrations can be made by strangers** (only with the opt-in).
   Registering needs no account. One network may register 20 apps an hour
   (an IPv6 address is counted by its /64), registration is refused while MCP
   is off, and the platform holds at most 200 live registrations, after which
   it answers 503 until unused ones expire; a registration nobody uses for 30
   days goes with the daily sweep. Nothing is exposed by it — registering
   grants nothing without a person's consent. The hosted Claude apps do not
   register: they use their metadata documents. Their requests come from
   Anthropic's published egress ranges (`160.79.104.0/21` and
   `153.61.208.0/20`), not from your office. Every Claude user shares those
   addresses, and Anthropic does not say how many of them it uses, so one
   address can carry many people's requests; that is why the token and
   revocation endpoints allow 600 requests a minute per address (an IPv6
   address is counted by its /64).
4. **http redirects are allowed only to the person's own computer**
   (`127.0.0.1`, `[::1]`, `localhost`, any port), which desktop AI apps use
   (Claude Code's metadata document among them). Whatever answers on that
   port of the person's computer receives the authorization code; it is bound
   to the app's PKCE verifier and cannot be redeemed without it. The consent
   page says when an app only answers there.
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
   Keycloak's network, can fill its 200-client cap, and **the clients it
   registers never expire**: nothing sweeps them until phase 4 removes the
   realm.
8. **Realm names must be lowercase** for Keycloak's registration to be routed
   (see the table above), until phase 4.
9. **A refresh token used twice ends the app's grant, with no grace.** That is
   what a copied token looks like (design §5.3). It is also what an honest
   race looks like: a refresh whose answer the app never read (Claude stops
   waiting after 30 seconds), or two refreshes with the same token at once.
   Claude refreshes on two triggers, ahead of time (up to five minutes before
   the token expires) and when a call is refused with 401, so two can meet.
   Either way the app's next call is refused, and the person removes the
   connector and adds it again.
10. **One click can approve, within ten minutes of a second factor.** The
    consent page refuses to be framed (`frame-ancestors 'none'`,
    `X-Frame-Options: DENY`), but a page on another site can open it as a
    window of its own under the second click of a double click, which then
    lands on **Approve**. OWASP calls this DoubleClickjacking, and framing
    rules do not stop it. Approving needs the person's second factor
    confirmed in the last ten minutes, so outside that window the trick only
    reaches the second-factor prompt; inside it, one click is enough. It is
    bounded, not removed: the page is titled by the app's host, the person
    is mailed "You connected <host>", and Settings → AI connections revokes
    the app.

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

y el bloque de `ITS_MCP_REGISTRATION` (C2: abre el registro dinámico y nada más):

```text
# --- AI apps over MCP: self-registration (off unless you opt in) ---
# Uncommented, this lets AI apps that cannot publish a client metadata
# document register themselves with the platform (OAuth dynamic client
# registration): any app may then, under any name, though a person must
# still sign in to the console and approve it. Claude and Claude Code do not
# need it: the platform reads their client metadata documents whenever MCP is
# on. Read deploy/README.md "AI connections (MCP)" first. The api container
# reads it when it starts, and it needs ITS_PUBLIC_CONSOLE_URL too. Until
# phase 4 removes Keycloak, keycloak-init also reads it and opens Keycloak's
# own registration, whose tokens nothing accepts any more.
# ITS_MCP_REGISTRATION=open
```

(`tests/test_keycloak_init_job.py::test_the_env_example_documents_the_opt_in_and_leaves_it_off` exige la línea
`# ITS_MCP_REGISTRATION=open` y el texto «AI connections (MCP)»: los dos siguen.)

- [ ] **Step 4: Los documentos de diseño y los comentarios**

`docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` (decisiones del operador del 2026-11-06):

- Al final de `### 2.5 Confirmación reciente del segundo factor`, justo antes de `### 2.6`: detrás de la línea en
  blanco que sigue al párrafo «Exigida en los últimos 10 minutos para: …», y con una línea en blanco propia detrás. La
  Task 14 de la fase 2 reescribe ese párrafo sin tocar esa línea en blanco; si ya está en la rama, la enmienda va
  detrás de todo lo que escribe (la lista ampliada, «Por qué…» y «Lo que esto deliberadamente no cubre…») y no se toca
  nada suyo: aprobar una aplicación de IA no está en su lista ni en lo que deja fuera, y rechazarla, como revocarla, no
  pide nada en las dos (Puntos de encuentro):

  ```markdown
  > **Enmienda 2026-11-06 (decisión del operador C1; plan de la fase 3, Task 8).** También **la pide** aprobar una aplicación de
  > IA en la pantalla de consentimiento, en un equipo recordado también: aprobarla le entrega toda la autoridad de la
  > persona (`CLAUDE.md` 1c) mientras se use. Rechazarla no la pide.
  ```

- Al final de `### 2.7 Avisos por correo`:

  ```markdown
  > **Enmienda 2026-11-06 (decisión del operador C1; plan de la fase 3, Task 8).** También al aprobar una aplicación
  > de IA: «Has conectado <host de la app>», con el host que encabeza la pantalla de consentimiento y cómo revocarla.
  ```

- Al final de `### 5.4 Registro abierto, con límites`:

  ```markdown
  > **Enmienda 2026-11-06 (decisión del operador C2; plan de la fase 3, Tasks 6 y 12).** El registro dinámico se
  > abre solo con `ITS_MCP_REGISTRATION=open`; un cliente CIMD (§5.5) se acepta siempre que MCP esté encendido, sin
  > esa opción. Los metadatos anuncian CIMD siempre y `registration_endpoint` solo con el registro abierto. Límites:
  > 20 por hora y red (IPv6 por /64), 200 registros vivos y el registro cerrado mientras MCP esté apagado (plan B3-02).
  ```

`docs/superpowers/specs/2026-10-09-mcp-server-design.md`, justo después de `## 3. OAuth, and only OAuth`:

```markdown
> **Amended 2026-11-06 (autenticación propia, phase 3; plan
> `docs/superpowers/plans/2026-11-06-autenticacion-propia-fase3.md`).** The
> authorization server is the platform itself, not the Keycloak realm
> (`docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §5).
> The protected-resource metadata names the console's address in
> `authorization_servers`; an app is known by its client metadata document
> whenever MCP is on, or registers at the platform's `POST /oauth/register`
> only with `ITS_MCP_REGISTRATION=open` (operator decision 2026-11-06); the
> person signs in to the console and approves on its consent page
> (`/oauth/authorize`), with their second factor confirmed in the last ten
> minutes; the access token is the platform's ES256 `at+jwt`, for exactly the
> MCP resource, ten minutes, naming its grant, which `/mcp` reads on every
> call. §3.1 steps 2-4, §3.2 and §3.3 below describe the Keycloak arrangement
> this replaced, kept as the record until phase 4 removes Keycloak. In §3.4,
> revoking now ends the app's grants in the platform's own tables, in one
> transaction with the `mcp_revocation` row and its audit event; nothing is
> removed in Keycloak, and the list also shows apps holding a live grant that
> called no tool yet. §8's "app name (from its registration)" is now the
> host the app cannot fake, with its own name beneath as its claim.
```

`docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md`, justo después de `### 7.2 The staleness bug this
uncovered, which is not cosmetic`:

```markdown
> **Amended 2026-11-06 (autenticación propia, phase 3).** There is no claim
> to refresh from any more: the console signs in by session since phase 1,
> and since phase 3 the MCP door admits only the platform's own token, which
> carries no address. The `users` row is the one source of the address, for
> the domain rule and for every screen alike -- what this section asked for.
```

y justo después de `### 7.4 Falsifiers` (A3-52: los falsificadores están aquí, no en §7.2):

```markdown
> **Amended 2026-11-06 (autenticación propia, phase 3).** 13 and 14 now read
> against the row, since no sign-in carries an address or a name to refresh
> from: 13 is "the address the MCP door decides on is not the one the row
> holds, or an MCP call writes an identity change", and 14 is "an MCP call
> changes a `display_name` set in ITMS". `backend/tests/test_me_profile.py`
> holds both, the decision's side of 13 included
> (`test_the_door_decides_on_the_address_the_row_holds`).
```

`docs/03-security-model.md:161`, «MCP clients still authenticate through Keycloak until phase 3.» pasa a «MCP clients
authenticate on the platform's own OAuth 2.1 server since phase 3: PKCE S256, consent in the console with a recently
confirmed second factor, a ten-minute ES256 access token for exactly the MCP resource, and a refresh token that rotates
and whose reuse ends the grant.» Si la Task 13 de la fase 2 ya está en la rama, la frase de MCP es la suya, «MCP
clients still authenticate through Keycloak; phase 3 replaces that, and phase 4 removes Keycloak.» (partida en dos
líneas, entre «Not built: SAML and on-premises Active Directory federation (out of scope).» y sus viñetas de contactos
y de Microsoft): pasa a la misma frase, y lo demás de la fase 2 se queda, también la fila que su Task 14 añade a la
tabla «Threat model» (Puntos de encuentro).

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

Run: `cd "$(git rev-parse --show-toplevel)" && grep -rnI --exclude-dir=__pycache__ "until phase 3\|until own authentication phase 3" deploy backend/app backend/tests docs/03-security-model.md docs/modules/console-auth | grep -v "backend/tests/client_scope_support.py"`
Expected: sin salida (A3-63: `-I` y `__pycache__` dejan fuera los `.pyc`; `client_scope_support.py:172` habla de la fase
3 del proyecto de clientes MSP, no de esta).

En la misma viñeta «Retention:» que la Task 13 de la fase 2 añade a `docs/03-security-model.md` («sessions are deleted 90
days after they ended or expired; remembered devices and one-time links 30 days after; WebAuthn challenges after a day;
each daily sweep is audited (`auth.retention_applied`).»), esta fase añade entre «after a day» y «; each daily sweep»:
«; since phase 3, spent OAuth authorization codes 10 minutes after they expire, ended AI-app grants after a day, and AI
apps unused for 30 days» (los periodos son los de la Task 13: `OAUTH_CODE_RETENTION = 10 minutos`,
`OAUTH_GRANT_RETENTION = 1 día`, `CLIENT_IDLE = 30 días`). La viñeta ya existe en la rama porque la Task 12 corre en el
árbol fusionado con la fase 2.

Run: `cd "$(git rev-parse --show-toplevel)" && grep -c "since phase 3, spent OAuth authorization codes 10 minutes after they expire, ended AI-app grants after a day, and AI apps unused for 30 days" docs/03-security-model.md`
Expected: `1`.

Run: `cd "$(git rev-parse --show-toplevel)" && grep -n "authenticate through Keycloak" docs/03-security-model.md`
Expected: sin salida: no queda ni la frase de `3a1ba1c` ni la de la Task 13 de la fase 2, que la búsqueda de arriba no
ve (no dice «until phase 3»).

- [ ] **Step 5: Los recuentos y la suite, en el árbol fusionado**

Run: `cd backend && .venv/bin/pytest -q tests/test_logging_discipline.py tests/test_mcp_channel_api.py tests/test_route_gating.py tests/test_client_catalog.py tests/test_route_session_discipline.py tests/test_i18n_catalog.py tests/test_keycloak_init_job.py tests/test_console_auth_realm.py tests/test_deploy_env_documentation.py tests/test_security_headers.py tests/test_deploy_frame_headers.py`
Expected: PASS, con el recuento de logs en **107 + el delta de la fase 2**, `NEVER_EXPOSED` en **37 + lo de la fase 2**,
`UNGATED_BY_DESIGN` con las dos rutas del consentimiento y `SECRET_ONCE` con la decisión. Si un número no cuadra, se
corrige el número del árbol fusionado y su párrafo «NOW», no el código.

Run: `cd backend && .venv/bin/pytest -q -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning`
Expected: PASS, la suite entera.

Run: `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `Success: no issues found` y `All checks passed!`.

Run: `cd frontend && npx vitest run && npx tsc -b && npm run build`
Expected: PASS, sin errores de tipos, y el build termina (`✓ built in …`).

Run: `cd backend && PYTHONPATH=$PWD .venv/bin/python -m tests.export_console_global_routes && git diff --exit-code ../frontend/src/lib/globalRoutes.json`
Expected: sin diferencias (A3-67: con `PYTHONPATH`, como toda orden de Python en una worktree; esta fase no cambia
`GLOBAL_WRITES` ni `GLOBAL_READS`; si la fase 2 los cambió, el coordinador ya lo regeneró al fusionar).

Run: `cd "$(git rev-parse --show-toplevel)" && git log --format=%s claude/compassionate-bardeen-vgtwus..HEAD -- .github/workflows/ci.yml deploy/keycloak/`
Expected: ningún commit de las Tasks 1–13 (si aparece alguno, es de la fase 2; el paso MCP de la CI y `ci_mcp_flow.py`
son de la fase 4).

- [ ] **Step 6: Commit**

```bash
git add backend/tests/test_oauth_falsifiers.py deploy/README.md deploy/.env.example \
  docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md \
  docs/superpowers/specs/2026-10-09-mcp-server-design.md \
  docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md docs/03-security-model.md \
  docs/modules/console-auth/spec.md backend/app/modules/user_admin/service.py \
  backend/app/modules/user_admin/__init__.py backend/app/api/v1/me.py backend/app/core/auth/__init__.py \
  backend/tests/test_user_admin_api.py backend/tests/conftest.py
git commit -m "test(oauth): falsifier 6 end to end; the runbook and the docs say the platform is MCP's OAuth server"
```

- [ ] **Step 7: El informe**

El informe de la fase al coordinador (A3-65; `CLAUDE.md`, «Reporting to the operator») dice, en este orden:

1. **El camino hasta `main`.** La rama de la fase 3 se fusiona después de la de la fase 2 (0247 sigue a 0246), y las
   dos, con las fases 1 y 4, llegan a `main` cuando el proyecto entero termina: el operador despliega siempre desde
   `main` (2026-09-11), y decidió el 2026-10-31 que nada se despliega hasta que terminen los cuatro proyectos. Esta
   fase no se despliega sola.
2. **Lo que esta fase añade a la secuencia de despliegue del proyecto 4**, que es la que el operador correrá entera:
   - `migrate` aplica **0247** en el siguiente `docker compose up -d` (corre como dependencia de `api` siempre; aquí
     encuentra una migración que hacer);
   - el contenedor `api` se **recrea**: la etiqueta de Traefik de su router cambia (Task 6);
   - la imagen `web` cambia: `deploy/nginx.conf` gana `frame-ancestors 'none'` y `X-Frame-Options: DENY` (Task 4), así
     que se reconstruye con las demás imágenes;
   - `ITS_MCP_REGISTRATION` la lee también la API, y ahora solo abre el registro dinámico (C2); **ninguna clave `.env`
     nueva**; ninguna ruta nueva de OpenBao (la clave `auth-signing` es de la fase 1).
3. **Que la secuencia empieza por** `docker compose exec openbao bao status`: OpenBao está sellado tras cada reinicio
   y con él sellado `up -d` se queda esperando (`deploy/GO-LIVE.md` §4). No es esta fase la que lo sella, y por eso
   el paso nunca se da por innecesario.
4. Qué se verificó y qué no: las salidas rojas del Step 1, los recuentos del Step 5 en el árbol fusionado, y que la
   CI de Keycloak (`deploy`) sigue probando solo a Keycloak hasta la fase 4.

La secuencia completa, copiable y sin marcadores, la da el informe del proyecto 4, que es el que llega al host.

### Task 13: Las filas del servidor OAuth en la retención diaria de la fase 2

Diseño §5.2–§5.4; D10; B3-25. Oleada 5, en paralelo con la Task 10. Backend.

Los códigos gastados o caducados, las concesiones terminadas y las apps sin uso no sirven ya a nadie y no deben
quedarse para siempre. La fase 2 construye el barrido diario de la autenticación propia
(`backend/app/core/auth/retention.py`, su Task 3; las sesiones a los 90 días, los equipos y los enlaces a los 30, los
desafíos al día): un registro de pasos (`STEPS`), cada uno con su predicado `org_id`,
auditado como `auth.retention_applied` con un recuento por paso, y dice que la fase 3 añade aquí sus filas. Esta tarea
añade tres pasos al final, y nada más: ni otra tarea de beat ni otra acción de auditoría.

**Files:**
- Modify: `backend/app/core/auth/retention.py` (de la fase 2: dos ventanas, tres pasos, tres entradas de `STEPS`; en
  el docstring, tres viñetas y el párrafo del alcance)
- Modify: `backend/app/core/client_scope.py` (de la fase 2: la razón de `retention.py::sweep_org` en
  `ALL_SCOPE_ALLOWLIST`)
- Modify: `backend/tests/test_auth_retention.py` (de la fase 2: los metadatos exactos del evento ganan los tres pasos)
- Create: `backend/tests/test_oauth_retention.py`

**Interfaces:**
- Consumes: de la fase 2 (Task 3, en la misma rama antes de empezar): `retention.STEPS`, `Step`, `_deleted`, `sweep(session,
  org_id, *, now) -> Swept`, `sweep_org(org_id, engine, *, now=None) -> int` y la acción `auth.retention_applied`; Task 1
  (`policy.CLIENT_IDLE`); Task 5 (`OAuthClient`, `OAuthCode`, `OAuthRefreshFamily`, `OAuthRefreshToken`, las claves
  foráneas `ON DELETE CASCADE` de 0247, `ix_oauth_clients_org_last_used`, `tests/oauth_support.py`); Task 7
  (`grants.open_family`, `grants.issue_code`; en `tests/oauth_support.py`, `seed_code`, `pkce_pair`). En las pruebas,
  `bypassrls_session` (`backend/tests/conftest.py:185`).
- Produces: `retention.OAUTH_CODE_RETENTION = timedelta(minutes=10)`, `retention.OAUTH_GRANT_RETENTION =
  timedelta(days=1)`; los pasos `oauth_codes`, `oauth_grants`, `oauth_apps`, al final de `STEPS`.

**Decisión de la tarea (1): qué se va, y cuándo** (B3-25, D10):
- un **código** diez minutos después de caducar, usado o no. Mientras dura, un canje repetido lo encuentra y termina la
  concesión que abrió (RFC 6749 §4.1.2, Task 7); pasados sus 60 segundos y diez minutos más, ya no sirve a nadie;
- una **concesión** (familia de refresh) un día después de revocarse o de caducar, con sus tokens de refresh (`ON
  DELETE CASCADE`); los códigos que la nombraban se quedan sin ella (`SET NULL`) hasta su propio paso. El día es margen
  para leerla en un incidente: su motivo (`revoked_reason`) queda además en el evento del acto que la terminó
  (`mcp.*`, Task 7; `ai_grants_revoked`, Task 10);
- una **app** sin uso en `CLIENT_IDLE` (30 días; diseño §5.4: «Un registro sin uso en 30 días caduca»), registrada o
  conocida por su documento, con sus concesiones y sus códigos (`ON DELETE CASCADE`). `find_client` ya no la
  encontraba; `clients.purge_idle` (Task 6) sigue borrando las registradas antes de contar el techo de 200, y este paso
  borra las que ningún registro nuevo llega a barrer (las de documento, o todas cuando el registro está cerrado).

Las filas de `mcp_revocation` no se tocan: son el registro de lo que se revocó.

**Decisión de la tarea (2): sin índice nuevo** (la nota A2-14 del plan de la fase 2 deja a cada paso decidirlo). El de
las apps lo sirve `ix_oauth_clients_org_last_used` `(org_id, last_used_at)`, de 0247. Los códigos viven 60 segundos y
se barren a diario: la tabla guarda, como mucho, las aprobaciones de un día. Las familias son una por aprobación y se
van al día de terminar: un escaneo secuencial por organización y día, como los de la fase 2.

**Decisión de la tarea (3): el orden.** Códigos, concesiones, apps: cada paso cuenta lo que borra su sentencia; lo que
se va en cascada con una app no se cuenta dos veces ni se cuenta como suyo.

**Decisión de la tarea (4): la lista revisada dice para qué se usa el alcance** (re-revisión f3-3, N8 y X3).
`CLAUDE.md` 5a: «Solo los procesos internos usan el alcance "todos", y cada uso está en una lista revisada». Con esta
tarea, el `ClientScope.all()` de `sweep_org` borra también filas de OAuth de técnicos, que no tienen cliente; la razón
de su entrada en `ALL_SCOPE_ALLOWLIST` y la frase «over these tables only» del docstring lo dicen. Ninguna prueba las
lee, así que nada se pone rojo si se olvidan: por eso son un paso de esta tarea y no un comentario.

- [ ] **Step 1: Las pruebas que fallan**

`backend/tests/test_oauth_retention.py`:

```python
"""The OAuth server's rows in the daily retention sweep (autenticación
propia plan 2026-11-06, Task 13; phase 2's `app/core/auth/retention.py`).

- a code goes ten minutes after it expired, used or not; a live one stays;
- a grant goes a day after it was revoked or lapsed, its refresh tokens with
  it; a live one, and one revoked an hour ago, stay;
- an app unused for 30 days goes, its grants and codes with it; a used one
  stays;
- the sweep's one audit row counts each step (`auth.retention_applied`);
- every step names its organization: on a session RLS does not filter,
  another organization's rows stay.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import retention
from app.core.auth.oauth import grants
from app.core.auth.oauth.store import oauth_session
from app.models import AuditEvent
from app.models.oauth import OAuthClient, OAuthCode, OAuthRefreshFamily, OAuthRefreshToken
from tests.oauth_support import RESOURCE, oauth_org, pkce_pair, seed_client, seed_code
from tests.session_support import user_id_of

NOW = datetime.now(UTC)


async def _family(env: Any, org_id: UUID, sub: str, app: OAuthClient, **ended: Any) -> UUID:
    """A grant opened 40 days ago, used today unless `ended` says otherwise."""
    opened = NOW - timedelta(days=40)
    async with oauth_session(org_id, env.app_engine) as session:
        family, _ = await grants.open_family(
            session,
            org_id=org_id,
            user_id=await user_id_of(env, sub),
            oauth_client_id=app.id,
            resource=RESOURCE,
            now=opened,
        )
        values: dict[str, Any] = {"expires_at": NOW + timedelta(days=30), **ended}
        await session.execute(
            update(OAuthRefreshFamily).where(OAuthRefreshFamily.id == family.id).values(**values)
        )
        family_id: UUID = family.id
    return family_id


async def _ids(env: Any, model: Any, org_id: UUID) -> set[UUID]:
    async with AsyncSession(env.admin_engine) as session:
        return set(
            (await session.execute(select(model.id).where(model.org_id == org_id))).scalars()
        )


async def _world(env: Any, prefix: str) -> dict[str, Any]:
    org_id, _ = await oauth_org(env, prefix)
    sub = await env.create_user(org_id, "org-admin")
    app = await seed_client(env, org_id)
    idle = await seed_client(env, org_id, last_used_at=NOW - timedelta(days=31))
    _, challenge = pkce_pair()
    await seed_code(env, org_id, sub, app=app, challenge=challenge, now=NOW - timedelta(minutes=12))
    await seed_code(env, org_id, sub, app=app, challenge=challenge, now=NOW)
    await seed_code(env, org_id, sub, app=idle, challenge=challenge, now=NOW)
    revoked = {"revoked_reason": "revoked_in_settings"}
    return {
        "org_id": org_id,
        "app": app.id,
        "idle": idle.id,
        "live": await _family(env, org_id, sub, app),
        "revoked_old": await _family(
            env, org_id, sub, app, revoked_at=NOW - timedelta(days=2), **revoked
        ),
        "revoked_recent": await _family(
            env, org_id, sub, app, revoked_at=NOW - timedelta(hours=1), **revoked
        ),
        "lapsed_old": await _family(env, org_id, sub, app, expires_at=NOW - timedelta(days=2)),
        "of_the_idle_app": await _family(env, org_id, sub, idle),
    }


async def test_the_sweep_removes_what_can_no_longer_serve_anyone(api_env) -> None:
    w = await _world(api_env, "oauth-ret")
    org_id = w["org_id"]

    swept = await retention.sweep_org(org_id, api_env.app_engine, now=NOW)

    # One expired code, two ended grants, one idle app (whose grant and code
    # went with it, uncounted).
    assert swept == 4
    assert await _ids(api_env, OAuthClient, org_id) == {w["app"]}
    assert await _ids(api_env, OAuthRefreshFamily, org_id) == {w["live"], w["revoked_recent"]}
    async with AsyncSession(api_env.admin_engine) as session:
        codes = (
            (await session.execute(select(OAuthCode.expires_at).where(OAuthCode.org_id == org_id)))
            .scalars()
            .all()
        )
        tokens = set(
            (
                await session.execute(
                    select(OAuthRefreshToken.family_id).where(OAuthRefreshToken.org_id == org_id)
                )
            ).scalars()
        )
        (event,) = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id, AuditEvent.action == "auth.retention_applied"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(codes) == 1 and codes[0] > NOW  # the live one; the idle app's went with it
    assert tokens == {w["live"], w["revoked_recent"]}  # the ended grants' tokens cascaded
    assert (event.actor_type, event.actor_id) == ("system", None)
    assert (event.meta["oauth_codes"], event.meta["oauth_grants"], event.meta["oauth_apps"]) == (
        1,
        2,
        1,
    )


async def test_every_oauth_step_names_its_organization(api_env, bypassrls_session) -> None:
    """Phase 2's decision 3: `sweep` takes any session, and on one RLS does
    not filter, the predicate is what keeps one organization's sweep off
    another's rows."""
    mine = await _world(api_env, "oauth-ret-a")
    theirs = await _world(api_env, "oauth-ret-b")
    before = {
        model: await _ids(api_env, model, theirs["org_id"])
        for model in (OAuthClient, OAuthCode, OAuthRefreshFamily)
    }

    await retention.sweep(bypassrls_session, mine["org_id"], now=NOW)
    await bypassrls_session.commit()

    for model, ids in before.items():
        assert await _ids(api_env, model, theirs["org_id"]) == ids, model
    assert mine["idle"] not in await _ids(api_env, OAuthClient, mine["org_id"])


def test_the_oauth_steps_close_the_registry() -> None:
    assert [name for name, _ in retention.STEPS][-3:] == [
        "oauth_codes",
        "oauth_grants",
        "oauth_apps",
    ]
    assert retention.OAUTH_CODE_RETENTION == timedelta(minutes=10)
    assert retention.OAUTH_GRANT_RETENTION == timedelta(days=1)
```

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_retention.py`
Expected: FAIL (`swept` es 0: `STEPS` no tiene pasos de OAuth, y `retention.OAUTH_CODE_RETENTION` no existe).

- [ ] **Step 2: Los pasos**

En `backend/app/core/auth/retention.py`, en el docstring del módulo, la lista «WHAT GOES, AND AFTER HOW LONG» gana al
final:

```text
- an OAuth authorization code `OAUTH_CODE_RETENTION` (10 minutes) after it
  expired, used or not: while it lasts, a second exchange finds it and ends
  the grant it opened (own authentication phase 3, plan 2026-11-06, Task 13);
- an AI app's grant (`oauth_refresh_families`) `OAUTH_GRANT_RETENTION` (1 day)
  after it was revoked or lapsed, its refresh tokens with it (ON DELETE
  CASCADE); its reason is also in the audit event of the act that ended it;
- an AI app unused for `CLIENT_IDLE` (30 days), with its grants and codes.
```

El párrafo «THE REGISTRY (`STEPS`) …» **no cambia** (N-M7 de la segunda re-revisión del plan de la fase 2). Una
versión anterior de este paso sustituía en él «Phase 3 adds its expired authorization codes and refresh tokens here.»,
una frase que la fase 2 ya no escribe: con su X4, el párrafo termina «Phase 3 adds its authorization codes, its grants
(refresh-token families, whose tokens cascade) and its idle apps (`oauth_clients`) here.», que nombra lo que esta tarea
añade, tabla por tabla. Lo único que la frase de esta fase decía además, el orden de los tres pasos, lo dicen el
comentario de sus tres entradas en `STEPS` (abajo) y el orden de la lista de arriba. Si la revisión de la fase 2 vuelve
a cambiar esa frase, tampoco se toca.

Un párrafo sí pasa al de abajo, entero y con el mismo ancho que el resto del docstring: el del alcance (decisión 4),
que en el plan de la fase 2 dice «over these tables only.» (si su revisión lo cambió, se conserva lo suyo y se cambia
solo esa frase):

```text
ONE ORGANIZATION, EVERY CLIENT. A technician's rows have no client and only
an every-client scope deletes them (migration 0231), so `sweep_org` holds
`ClientScope.all()` (`ALL_SCOPE_ALLOWLIST`), over these tables only: phase
2's four and, since phase 3, `oauth_codes`, `oauth_refresh_families` and
`oauth_clients`, with what their foreign keys cascade to. The worker runs it
daily (`app/worker.py`, `auth-retention`); no person is behind it. No index
helps these scans (`auth_sessions` has none on `ended_at` or `expires_at`):
one sequential scan per organization per day is fine at this size, and a
step on a large table decides for itself.
```

Los imports ganan, en su sitio de isort:

```python
from app.core.auth.oauth.policy import CLIENT_IDLE
from app.models.oauth import OAuthClient, OAuthCode, OAuthRefreshFamily
```

Después de `CHALLENGE_RETENTION`:

```python
#: How long an OAuth code is kept after it expires, and an ended grant after
#: it ended (plan 2026-11-06, Task 13; B3-25).
OAUTH_CODE_RETENTION = timedelta(minutes=10)
OAUTH_GRANT_RETENTION = timedelta(days=1)
```

Después de `_challenges`:

```python
async def _oauth_codes(session: AsyncSession, org_id: UUID, now: datetime) -> int:
    return await _deleted(
        session,
        delete(OAuthCode)
        .where(OAuthCode.org_id == org_id, OAuthCode.expires_at < now - OAUTH_CODE_RETENTION)
        .returning(OAuthCode.id),
    )


async def _oauth_grants(session: AsyncSession, org_id: UUID, now: datetime) -> int:
    cutoff = now - OAUTH_GRANT_RETENTION
    return await _deleted(
        session,
        delete(OAuthRefreshFamily)
        .where(
            OAuthRefreshFamily.org_id == org_id,
            or_(OAuthRefreshFamily.revoked_at < cutoff, OAuthRefreshFamily.expires_at < cutoff),
        )
        .returning(OAuthRefreshFamily.id),
    )


async def _oauth_apps(session: AsyncSession, org_id: UUID, now: datetime) -> int:
    """`<=`, as `clients.find_client` and `clients.purge_idle` read idle."""
    return await _deleted(
        session,
        delete(OAuthClient)
        .where(OAuthClient.org_id == org_id, OAuthClient.last_used_at <= now - CLIENT_IDLE)
        .returning(OAuthClient.id),
    )
```

y `STEPS` gana, al final:

```python
    # Own authentication phase 3 (plan 2026-11-06, Task 13): codes, grants,
    # then the idle apps, whose removal takes their remaining grants along.
    ("oauth_codes", _oauth_codes),
    ("oauth_grants", _oauth_grants),
    ("oauth_apps", _oauth_apps),
```

En `backend/app/core/client_scope.py`, la entrada `"app/core/auth/retention.py::sweep_org"` de `ALL_SCOPE_ALLOWLIST`
pasa a (decisión 4; si la fase 2 cambió su texto en su revisión, se conserva lo suyo y se añade lo de esta fase):

```python
    "app/core/auth/retention.py::sweep_org": (
        "worker: own authentication's retention (final review A-M6, operator decision C7; "
        "phase 3 plan 2026-11-06, Task 13) -- one organization's old sessions, forgotten "
        "devices, spent links and old challenges, and its AI apps' spent codes, ended grants "
        "and idle registrations, whose technician rows only an every-client scope may delete; "
        "every step carries its own org_id predicate, and the sweep is audited"
    ),
```

En `backend/tests/test_auth_retention.py` (fase 2),
`test_a_sweep_that_deleted_something_is_audited_and_one_that_did_not_is_not` compara los metadatos enteros; ganan los
tres pasos, a cero (su mundo no tiene filas de OAuth):

```python
    assert event.meta == {
        "at": NOW.isoformat(),
        "sessions": 2,
        "devices": 1,
        "links": 4,
        "challenges": 1,
        "oauth_codes": 0,
        "oauth_grants": 0,
        "oauth_apps": 0,
    }
```

Si al empezar la tarea esa prueba o el registro dicen otra cosa (la fase 2 los cambió en su revisión), se conserva lo
suyo y se añaden las tres claves.

- [ ] **Step 3: Las pruebas pasan**

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_retention.py tests/test_auth_retention.py tests/test_worker_sweep_containment.py tests/test_client_catalog.py`
Expected: PASS.

Run: `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `Success: no issues found` y `All checks passed!`.

- [ ] **Step 4: Commit**

```bash
git add backend/app/core/auth/retention.py backend/app/core/client_scope.py backend/tests/test_auth_retention.py \
  backend/tests/test_oauth_retention.py
git commit -m "feat(oauth): spent codes, ended grants and idle AI apps go with the daily retention sweep"
```
---

## Autorrevisión

Hecha sobre el plan entero, contra el diseño, el dossier de decisiones y el código de **`3a1ba1c`**, el 2026-11-06,
después de aplicar la revisión y las tres re-revisiones de `4e39978`. `3a1ba1c` es la base sobre la que se ejecuta el
plan (la rama `claude/compassionate-bardeen-vgtwus`, que cada worktree fusiona antes de empezar), no `0182206`, contra
la que se comprobó la versión anterior (re-revisión f3-3, N9): entre las dos cambiaron `api/v1/auth.py`, `config.py`,
`.env.example`, `deploy/README.md`, `test_auth_signin.py`, `test_deploy_env_documentation.py`, una línea de
`test_auth_falsifiers.py` y el §2.3 del diseño. Los números de línea que el plan cita son los de `3a1ba1c`, y cada
edición se ancla además por su texto.

**Cobertura del diseño.**

| Diseño | Dónde |
|---|---|
| §5.1 los cuatro endpoints; metadatos RFC 8414 con `S256`, `none`, CIMD siempre y `registration_endpoint` con la opción (C2); RFC 9728 apuntando a la plataforma | Tasks 6 (metadatos, registro, RFC 9728, Traefik), 7 (token, revocación), 4 y 8 (autorizar) |
| §5.2 sesión de la consola; el host antes que el nombre; aviso de solo loopback; código de un uso y 60 s; redirects exactos salvo el puerto de loopback; `iss`; sin enmarcar; solo la aprobación navega sola | Tasks 4 y 8 (pantalla y rutas), 1 (`redirects`, `policy.CODE_TTL_S`), 7 (código de un uso) |
| §5.3 ES256, 10 min, audiencia el recurso MCP, otro token rechazado; `iss`/`aud`/`kid`/alg fijados; refresh opaco con hash que rota; reutilizar revoca la familia, sin gracia; 30 días sin uso; `invalid_grant`; revocar en Ajustes corta en la siguiente llamada | Tasks 2 (token), 7 (refresh y familias), 9 (puerta: otro token, familia en cada llamada), 10 y 11 (Ajustes) |
| §5.4 registrar no da acceso; límite por red; 30 días sin uso; MCP apagado hasta el org-admin; con C2, el registro solo con la opción | Task 6 (límites, techo, cerrado con MCP apagado), Task 8 (D11: quién puede conectar, antes que nada), Task 13 (las apps sin uso), la puerta de siempre |
| §5.5 CIMD por `egress.py`, `client_id` igual a la URL, caché por cabeceras | Tasks 3 y 6 (y la -03 donde es más estricta, B3-14) |
| §5.6 el token interno | fase 1; Task 12 comprueba que no vale en las rutas públicas |
| §2.5 y §2.7 con C1: aprobar pide el segundo factor reciente y avisa | Tasks 4 y 8; la Task 12 enmienda el diseño |
| §6.1 clientes OAuth, códigos, familias, con `org_id` y RLS y en el catálogo | Task 5 (con su ida y vuelta) |
| §6.2 la lectura antes de conocer a la persona, por `SECURITY DEFINER` | Task 5 (`lookup.org_of_slug`, de la fase 2) |
| §6.3 ES256 con la clave de OpenBao y JWKS con `kid` | fase 1 (`keys.py`, `/.well-known/jwks.json`); Task 2 lo usa, Task 6 lo publica en `jwks_uri` |
| §6.5 «MCP valida su JWT (`iss`, `aud`, `kid`, revocación)» | Task 9 |
| §7 consentimientos y revocaciones de clientes de IA auditados | Tasks 8 (`mcp.app_authorized`), 7 (`mcp.code_reused`, `mcp.refresh_reused`, `mcp.grant_revoked`, `mcp.grant_ended`), 10 (`mcp.app_revoked`, `ai_grants_revoked` en los actos de la cuenta), 13 (el barrido, `auth.retention_applied`) |
| §8 fase 3 | todo el plan; lo que queda para la fase 4, en «Lo que hace esta fase, y lo que deja a la fase 4» |
| §9 documentos | Task 12 (`docs/03-security-model.md`, el diseño propio §2.5/§2.7/§5.4, el de MCP §3, el de RBAC §7.2 y §7.4, `docs/modules/console-auth/spec.md`, el runbook, `.env.example`) |
| §10 falsificador 6 | Task 7 (unidad) y Task 12 (de punta a punta, CIMD sin la opción incluido); el 1 para estas tablas, Task 5; el 7 para la decisión, Task 8; el 10 (ninguna pantalla dice «Keycloak»), la guarda existente sobre los textos de la Task 4 |

**Los defectos de la revisión (A3-01…A3-73), uno a uno.** Los 73 están corregidos; la tarea que lo hace:

| Tarea | Defectos |
|---|---|
| Task 1 | A3-01, A3-02, A3-03, A3-04, A3-05, A3-06 |
| Task 2 | A3-07, A3-17 (su `git add`) |
| Task 3 | A3-08, A3-09, A3-10, A3-11, A3-12, A3-13, A3-14, A3-15, A3-17 (su `git add`) |
| Task 4 | A3-18, A3-19 (la página), A3-20, A3-21, A3-23, A3-24 |
| Task 1 (`main.py`) | A3-22 |
| Task 5 | A3-25, A3-26 (la guarda), A3-27, A3-28, A3-37 (`touch` como mucho por hora) |
| Task 6 | A3-26 (`register_new`), A3-29, A3-30 (con C2 cerrar el registro no toca a un CIMD: la frase que exageraba ya no es cierta ni hace falta), A3-38 (la cascada, sembrada) |
| Task 7 | A3-32, A3-33, A3-34, A3-35, A3-36, A3-37 (después de firmar), A3-39, A3-41 |
| Task 8 | A3-16 (§2.1), A3-19 (el servidor), A3-31, A3-42, A3-43, A3-44, A3-45, A3-46 |
| Task 9 | A3-47, A3-48, A3-49, A3-50, A3-51, A3-52 (la prueba), A3-53, A3-69 |
| Task 10 | A3-26 (`connections.py`), A3-54, A3-55, A3-56, A3-57, A3-58, A3-59 |
| Task 11 | A3-60, A3-61, A3-62 |
| Task 12 | A3-40, A3-52 (§7.4), A3-63, A3-64, A3-65, A3-66, A3-67, A3-68, A3-70 |
| Apéndice B | A3-71 (B3-02), A3-72 (B3-10), A3-73 (B3-12) |

Dos correcciones van más allá de lo que el dossier pedía, porque lo que pedía no bastaba: A3-47 pide `-W
error::RuntimeWarning`, y con él solo un coroutine sin esperar **no** hace fallar la prueba (pytest lo convierte en
`PytestUnraisableExceptionWarning`; comprobado), así que el plan lleva también `-W
error::pytest.PytestUnraisableExceptionWarning`. Y A3-64 dice que los riesgos 4 y 8 del runbook existen solo con el
registro abierto; eso valía para la primera versión, en la que la opción abría también CIMD. Con C2, el riesgo 4 (los
redirects http a este equipo) existe siempre que MCP esté encendido, porque el documento de Claude Code los usa: el
runbook lo agrupa así (3, 7 y 8 con la opción; 1, 2, 4 y 10 con MCP encendido).

**Las tres re-revisiones de `4e39978`, hallazgo a hallazgo.** Todos corregidos; donde el revisor daba dos caminos, se
dice cuál se tomó.

| Re-revisión | Hallazgo | Dónde, y cómo |
|---|---|---|
| f3-1 | A3-24 (PARCIAL) | Task 4: un fallo de `details` que pasa (5xx, 429, sin red) muestra `unavailableTitle` con «Intentarlo de nuevo», y una vitest para un 503 y para un `TypeError`; un 422 sigue siendo definitivo |
| f3-1 | Minor 1 | Task 3: `dns_failure` es `unavailable` (transitorio), con su caso en la prueba de la guarda |
| f3-1 | Minor 2 | Task 3: `64:ff9b::c000:201` en la lista de direcciones de uso especial |
| f3-1 | Minor 3 | Task 3: `on_origin` compara el host sin el punto final y el puerto con 443 por defecto, con los dos casos |
| f3-1 | Minor 4 | Task 4: un `temporarily_unavailable` de la decisión ofrece «Intentarlo de nuevo», que vuelve a pedir los detalles |
| f3-1 | Minor 5 | Notas de integración: los métodos van después de `acceptRecovery` y los tipos en `types.ts`; el recuento **baja** de 110 a 107 |
| f3-1 | Minor 6 | La segunda opción: documentarlo. Task 4, decisión 6, y el riesgo 10 del runbook (Task 12); el botón con espera no se construye |
| f3-1 | Minor 7 | Task 1: `stays_on_this_computer` acepta `localhost.` y todo `127.0.0.0/8` y `::1` |
| f3-1 | Minor 8 | La primera opción: la decisión 6 de la Task 3 dice que la intersección decide si el documento vale, no qué se concede; el refresh se emite siempre (D7) |
| f3-1 | Minor 9 | Task 2, Step 0: el paquete y, si falta, `policy.py` |
| f3-1 | Minor 10 | Tasks 3, 7 y 10, Step 5: el párrafo del diseño de logs y el del docstring, enteros |
| f3-2 | M-1 | La segunda opción: la decisión 4 de la Task 6 dice que un rechazo no borra la copia guardada (y la prueba lo fija), y D17 declina el SHOULD de CIMD-03 §6.1 con sus razones |
| f3-2 | M-2 | Task 6: el límite por red antes de leer la organización, y una prueba que lo fija; los metadatos sin `registration_endpoint` si la base de datos no responde, con su prueba; el docstring nombra la caché de 300 s |
| f3-2 | M-3 | Tasks 6 y 8: `record_use` en `resolve_client` y `validate`; quien no puede conectar no escribe `last_used_at`, con su prueba |
| f3-2 | M-4 | Task 7 (decisiones 3 y 8, el docstring), Task 12 (riesgos 3 y 9) y el Apéndice C: la cadencia de 5 a 10 minutos, las dos páginas de Anthropic citadas con su URL, sin «pocas» |
| f3-2 | M-5 | Task 8: el docstring de `oauth_consent.py` atribuye la exclusión del token interno a la regla 4 de `verify_loopback` (solo GET o HEAD). La prueba opcional no se añade: la regla ya la prueba `tests/test_loopback.py` |
| f3-2 | M-6 | Task 8: una app de documento se aprueba y se canjea con `mcp_registration = ""` |
| f3-2 | M-7 | Puntos de encuentro (fila de `tests/test_auth_phase2_falsifiers.py`) y Task 8, Step 8; la fila de `notices.py` en la tabla de la fase 2 es de su plan, y ya está |
| f3-2 | M-8 | Tasks 3, 7 y 10, Step 5: la base es la N que tenga la worktree, y el párrafo va encima del «NOW» de más arriba |
| f3-3 | N1 | Task 9, Step 7: la búsqueda por llamada; en `3a1ba1c` imprime 50 |
| f3-3 | N2 | Task 12, riesgo 3: un cubo por dirección, direcciones que comparten todos los usuarios de Claude; sin «pocas» (M-4) |
| f3-3 | N3 | Task 11: `subtitle`, `empty` y el comentario de `fixtures.ts`, con las dos pruebas que fijan el texto vacío |
| f3-3 | N4 | Corregido, no documentado: `held_apps` dice si la concesión es de quien pregunta, y las filas sin uso leen las revocaciones (Task 10, con su prueba) |
| f3-3 | N5 | Tasks 10 y 12: los números de `3a1ba1c` (`test_auth_signin.py:1024`; README 1240–1241, 1475–1476, 2010) |
| f3-3 | N6 | Task 12, punto 6: tres sustituciones, cada una hasta el siguiente encabezado que se nombra, en tres bloques |
| f3-3 | N7 | Task 10, Step 4: la prueba entera, con `_approved` después de `_grant` |
| f3-3 | N8 | Task 13, decisión 4: la razón de `ALL_SCOPE_ALLOWLIST` y «over these tables only» |
| f3-3 | N9 | Este párrafo |

**Lo que cruza con la fase 2 (X1–X11), por el lado de esta fase.** X1: ninguna frase de este plan dice que la fase 3
retire Keycloak, y la búsqueda de la Task 12, Step 4, sigue. X2: la fila de `security_headers.py` nombra las dos rutas
de la fase 2 y la de la Task 8. X3: es N8. X5, X6 y X7: las filas de `config.py`, del diseño y de `notices.py`,
corregidas. X8: cuatro filas nuevas, y la Task 11 ancla por texto. X9: la Task 10, Step 3, da la regla de isort. X10:
la Task 9, decisión 2, dice por qué la puerta de MCP no se iguala a la de la consola para un contacto (de los dos
caminos, explicar que no se alcanza). X11: la Task 12 cambia solo la línea 32 del README, sin refluir. X4 es del plan
de la fase 2.

**Ronda 3 (2026-11-09): la Task 14 de la fase 2, su cuarto contrato y N-M7.** La fase 2 añadió su Task 14 (la
confirmación reciente para los ajustes de inicio de sesión, el acceso de los técnicos y la definición de los roles,
decisiones del operador S1–S4 del 2026-11-08 y S5 del 2026-11-09) y, en sus «Puntos de encuentro con la fase 3», un
cuarto contrato. Leída en `3a214fd`. Por el lado de esta fase:

- **N-M7** (de la segunda re-revisión de la fase 2): la Task 13 ya no reescribe el párrafo de `STEPS` de
  `retention.py`. La frase que la fase 2 escribe con su X4 nombra lo que esta fase añade, y el orden de los pasos lo
  dicen el comentario de sus entradas y la lista del docstring. El párrafo del alcance sí cambia (decisión 4).
- **El contrato 4** (el canal MCP nunca cuenta como confirmación reciente): la rama MCP de `admit()` ya lo cumplía,
  porque devuelve su `Admission` sin `principal`. La Task 9 lo dice (decisión 6, y un comentario en el `return`) y lo
  fija con `test_an_mcp_call_never_counts_as_a_recent_confirmation`.
- **Las pruebas**: ninguna que esta fase crea o reescribe llama con una sesión a una ruta de la Task 14 (S1, S3 ni
  S5), y ninguna prueba MCP de esta fase espera que una de ellas sea herramienta. Lo único que cambia es de la fase 2:
  `test_step_up_widened.py`, re-apuntada en la Task 9, Step 7.
- **Puntos de encuentro**: filas nuevas o reescritas para `deps.py`, el contrato 4, `exclusions.py` y
  `test_mcp_channel_api.py` (por deltas, sin un número fusionado), `part_d.py`, `test_mcp_catalog.py`,
  `test_step_up_widened.py`, `test_mcp_tools.py`, las pruebas que llamarían a esas rutas, el diseño (§2.5),
  `deploy/README.md`, `docs/03-security-model.md`, `docs/modules/console-auth/spec.md`, `AiConnectionsTab.tsx` y
  `api.ts` de Ajustes, y `stepUp.tsx`. Donde las filas de las dos fases no decían lo mismo que el código, dicen lo que
  es: `docs/03-security-model.md:161` lo cambian las dos (no es una unión), y la frase del README que edita la fase 2
  está en la línea 36, no en la 35.
- **El diseño y el runbook (Task 12)**: la enmienda C1 va al final de §2.5, detrás de la línea en blanco que la Task 14
  deja intacta, y no contradice su lista; la frase de `docs/03-security-model.md` se ancla también en la que escribe la
  Task 13 de la fase 2, con un `grep` que lo comprueba; las ediciones del runbook dejan el párrafo de la Task 14 entre
  ellas, sin tocarlo.

**Marcadores.** Buscado en el plan: `TBD`, `TODO`, «rellenar», «similar a la Task», «añadir manejo de errores»; ninguno.
Cada paso que cambia código trae el código; los cambios mecánicos (`await mcp_token(`, los metadatos de los actos, las
filas de las pruebas de Ajustes) dicen cada archivo y su texto (y la línea, en `3a1ba1c`) y traen la orden que
comprueba que no queda ninguno.
Ningún comando lleva un valor que haya que sustituir.

**Coherencia de nombres y tipos**, comprobada entre tareas:

- `McpGrant(user_id, org_id, client_id, family_id, iat, exp, email="")` con `azp` (Task 2) es lo que la Task 9 pone en
  `AuthContext.claims` y en los dos `McpPrincipal` de `test_mcp_tools.py`; `_gate` lee `claims.iat` y `claims.azp`,
  `invoke.py` y el audit leen `claims.azp`: los tres existen.
- `grants.family_is_live(session, *, family_id, user_id, client_id, now)`, `grants.revoke_app(session, *,
  oauth_client_id, user_id, reason, now)` y `grants.revoke_all_for_user(session, user_id, reason)` (Task 7) son las
  firmas que llaman las Tasks 9 y 10; los motivos que usa la 10 están en `REVOKE_REASONS` (Task 5). En los tres
  archivos de la Task 10 el módulo se importa como `ai_grants`: `user_admin/service.py` tiene variables locales
  `grants` en `delete_user` y `cancel_invite`, que taparían al módulo (A3-54), y `devices.revoke_all_for_user` ya
  ocupa el nombre corto.
- `clients.resolve_client(..., fetch: Fetch | None = None, record_use: bool = True)` (Task 6) resuelve
  `cimd.fetch_document` al llamar; `authorize.validate` lleva los dos y los pasa; el consentimiento le pasa `_not_read`
  y `record_use=False` mientras la persona no puede conectar (Task 8); el Apéndice A.1 lo dice igual.
- `connections.held_apps(...) -> list[HeldApp]` (Task 10), con `HeldApp(registration, mine)`: lo lee
  `list_connections` y nadie más; el Apéndice A.1 lo dice igual.
- `authorize.NotRedirectable(detail, *, error)` y `Redirectable(redirect_uri, state, problem)` (Task 8): `consent.py`
  construye las cuatro respuestas del Apéndice A.2, que son las de `ConsentDetails` y `ConsentAnswer` de la Task 4.
- `tests/mcp_support.mcp_token` es asíncrono desde la Task 9; la Task 10 usa `mcp_app` y la 12 `mcp_claims` y
  `signed_mcp_token`, los tres de la 9. `tests/oauth_support` crece al final en las Tasks 5, 6, 7 y 8; la 10, la 12 y
  la 13 solo leen (`connect`, `token_request`, `seed_code`).
- La respuesta de revocar es `{"revoked", "grants_revoked"}` en la Task 10, el Apéndice A.2 y la Task 11; la lista gana
  `id`, `name`, `host` y `kind` en las tres.
- Recuentos, sobre `3a1ba1c` sin la fase 2: logs 108 → 109 (Task 3) → 110 (Task 7) → 107 (Task 10);
  `NEVER_EXPOSED` 36 → 37 (Task 8); `UNGATED_BY_DESIGN` +2 (Task 8); `SECRET_ONCE` +1 (Task 8). Con la fase 2 en la
  rama se suman sus deltas (logs +1, así que 109 → 110 → 111 → 108; `NEVER_EXPOSED` +19, así que 55 → 56), como dicen
  las filas de «Puntos de encuentro con la fase 2»; la Task 12 los comprueba en el árbol fusionado.

**Comprobado ejecutándolo, en una copia del árbol de `3a1ba1c`** (con un 0246 de sustitución que solo crea
`its_auth_org`, el `lookup.org_of_slug` que la fase 2 publica, y el `retention.py` de su plan): los bloques de código
de las Tasks 1–8, 10 (todo lo que no necesita llamar a `/mcp`, más sus cambios en prosa a cinco aserciones de
metadatos, al recuento de logs y a `test_global_writes.py`) y 13, con sus pruebas y los `-W error` de la Task 9, `mypy
app` y `ruff check` (E, F, I, UP, B a 100 columnas); y, desde `frontend/`, las Tasks 4, 7 y 8 (vitest de
`src/modules/auth`, `src/modules/audit` y `src/i18n`, y `tsc -b`). Tras las re-revisiones: 356 pruebas de las tareas
de OAuth, y 284 de las guardas y de los archivos que la Task 10 cambia, en verde; las pruebas nuevas de `record_use`
y de las filas sin uso fallan con el código de antes. Ninguna línea de un bloque Python pasa de 100 columnas. Lo que se
encontró así y se corrigió en el plan: dos E501 y un `NotRedirectable` cuyo primer argumento posicional era un código y
la guarda de i18n leía como texto; la prueba del límite de inactividad, que violaba `expires_at > created_at`; un
`_record` tipado `dict[str, str]` que no admite el recuento; cinco aserciones de metadatos exactos que el recuento
rompía; la posición de isort del import nuevo en tres archivos; y, en esta vuelta, buscando en todos los bloques
Python del plan líneas de más de 100 columnas, una de 103 en la parametrización de `test_token_origin.py` de la Task 9
(que no se ejecuta), partida. **No se ejecutaron**: de la Task 9, sus Steps 6 y 7 (re-apuntan pruebas en prosa,
archivo por archivo; sus Steps 1–4, hasta la ronda 3 tampoco); las pruebas de la Task 10 que llaman a `/mcp`
(`_call_a_tool`, `_initialize`), la Task 11 (consola) y la Task 12 (necesita el árbol fusionado).

**En la ronda 3**, en una copia nueva de ese árbol (Redis en la base 11):
- la Task 13 otra vez, sobre el `retention.py` y el `test_auth_retention.py` del plan de la fase 2 (iguales en
  `5d8fd85` y en `3a214fd`, con su X4), y con el párrafo de `STEPS` sin tocar: las pruebas de su Step 3
  (`test_oauth_retention.py`, `test_auth_retention.py`, `test_client_catalog.py` y
  `test_worker_sweep_containment.py`), 51 en verde. Se deseleccionó `test_auth_retention.py::test_the_sweep_runs_daily`,
  que pide la entrada de beat que la fase 2 añade a `app/worker.py` y la copia no tiene. Y `ruff check` y `mypy app`,
  limpios;
- por primera vez, los Steps 1–4 de la Task 9 (los ayudantes, la puerta, `/mcp` y la entrada de `es.py`) y su
  `test_mcp_own_tokens.py` entero, con los dos `-W error`: 12 en verde. La prueba nueva se pone roja con una puerta
  que da a la `Admission` de MCP un `principal` con `step_up_at` reciente (contesta 404, lo que contesta la consola),
  y vuelve a verde al quitarlo. `ruff check` de los archivos tocados y `mypy app`, limpios;
- el `test_step_up_widened.py` del plan de la fase 2 (en `5d8fd85` y en `3a214fd`) con las dos líneas re-apuntadas
  pasa `ruff check`. Así se vio que la línea de `bearer` con el `await` tendría 102 columnas. No se ejecuta: necesita
  las Tasks 5, 8 y 14 de la fase 2;
- `git merge-file` sobre `3a1ba1c`, con un lado editado como dice la fase 2 en `3a214fd` y el otro como dice esta. El
  §2.5 del diseño, «What is never reachable over MCP» del runbook, `AiConnectionsTab.tsx` y `test_mcp_tools.py` se
  unen solos.
  `docs/03-security-model.md` choca en la línea 161, que es la fila nueva. Con la fase 2 primero, la frase de la
  Task 12 no deja ningún «authenticate through Keycloak»;
- ninguna línea de un bloque Python del plan pasa de 100 columnas.

---

## Preguntas para el operador

Ninguna queda abierta. Las catorce de la primera versión se resolvieron el 2026-11-06: doce por una regla citada
(Apéndice B.2, cada una con su fuente) y dos por el operador, C1 (confirmar el segundo factor al aprobar y avisar por
correo) y C2 (CIMD siempre que MCP esté encendido; el registro dinámico solo con la opción). La única que podría
reabrirse es B3-27, la ventana de gracia de la reutilización del refresh: el plan sigue el diseño («reutilizar uno ya
usado revoca toda la familia») y escribe el coste en el runbook (riesgo 9); si el operador prefiere la gracia de Okta,
el Apéndice C tiene las fuentes, y cambiarlo es la decisión 7 de la Task 7 y nada más.
