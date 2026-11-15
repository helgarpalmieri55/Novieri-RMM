# Autenticación propia — Fase 4: retirar Keycloak. Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ninguna pieza de Keycloak queda en el repositorio que se despliega —ni servicio, ni imagen, ni base de datos que se cree, ni ruta de Traefik, ni ajuste, ni cliente, ni prueba que lo necesite— y la prueba de despliegue de la CI demuestra en vivo, a través de Traefik, el servidor OAuth propio contra el que conecta un cliente de IA; antes, las dos decisiones del operador del 2026-11-15: el «Cerrar sesión en todas partes» de un administrador también termina las conexiones de IA de la persona y la consola gana ese botón para la propia persona (E1), y los refresh tokens usados de una concesión viva se borran a los 30 días de su uso (E2).

**Architecture:** Tres oleadas de código y dos de documentos. Primero E1 y E2, independientes del retiro: una línea en `user_admin.service.end_sessions` con su auditoría (el patrón D2(c) de los siete actos), un botón en «Sesiones» de la página de seguridad propia y del portal, y un paso más en el barrido diario de `app/core/auth/retention.py`. Después el backend: el validador de Keycloak (`app/core/security.py`), sus ajustes y su JWKS se van, y las pruebas que acuñaban un token de Keycloak lo siguen acuñando con una clave que la plataforma no conoce (`tests/foreign_tokens.py`); los clientes de administración de Keycloak, el reenvío de sus eventos, sus dos sondas y la tabla `keycloak_event_ingest` (migración **0248**) se van con sus pruebas. Después el despliegue: los servicios `keycloak` y `keycloak-init`, `deploy/keycloak/`, sus rutas de Traefik, la base de datos que creaba la inicialización de Postgres, y los guiones de respaldo, restauración, simulacro y despliegue: la base `keycloak` de un host que ya la tenía va solo en la copia que `deploy.sh` toma antes de actualizar, y `restore.sh` la sigue restaurando cuando una copia la trae (decisión del operador K1); `deploy.sh` quita por su nombre los dos contenedores de Keycloak y termina una actualización que se quedó a medias. La CI levanta por primera vez la API con OpenBao real y prueba de punta a punta registro, consentimiento, token, refresco, revocación y `/mcp` (`deploy/ci/mcp_flow.py`). Al final, documentos y el cierre con los falsificadores 10 y 11.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, PostgreSQL 16 (RLS), Redis, PyJWT 2.15.1, pytest. React + TypeScript estricto, Vite, TanStack Query, vitest. Docker Compose v2 (`docker compose config`, que funciona sin demonio), Traefik v3.6, OpenBao 2.6.1, nginx. Python de la biblioteca estándar en el guion de la CI. Ninguna dependencia nueva.

**Diseño:** `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` (aprobado el 2026-11-04), §8 fase 4 («servicios, base de datos, `deploy/keycloak/`, rutas de Traefik, respaldo y restauración, preflight, secretos `keycloak-*`, claves `.env` `KEYCLOAK_*`, `ITS_OIDC_*`, `ENTRA_*`; la prueba de despliegue de la CI y `ci_mcp_flow.py`, reescrita contra el servidor propio»), §1.3 (lo que se va), §6.1 («Se borra `keycloak_event_ingest`»), §9 y §10. Decisiones del operador del 2026-11-15: E1, E2 y E3 («plan first from design §8 phase 4»), en el registro de ejecución; K1, K2 y R1, decididas sobre la revisión previa de este plan (Apéndice B.1); y las del coordinador del mismo día (Apéndice B.2, C1–C7). Las fuentes externas de este plan, en el Apéndice E, leídas el 2026-11-15.

## Global Constraints

- **Decisiones del operador del 2026-11-15 (vinculantes).**
  - **E1 — El «Cerrar sesión en todas partes» de un administrador también termina las concesiones de IA de la persona, y la consola gana el botón para las propias sesiones.** `user_admin.service.end_sessions` (`DELETE /api/v1/users/{user_id}/sessions`) llama `ai_grants.revoke_all_for_user` en la misma transacción, después de la fila de la persona y de las sesiones y antes de la cadena de auditoría; los códigos sin canjear terminan por su `expires_at`; el evento `auth.sessions_ended` gana `ai_grants_revoked` (Task 1). La página «Tu seguridad» gana «Cerrar sesión en todas partes», que llama `POST /api/v1/auth/logout-everywhere` (`api.logoutEverywhere`, que ya existe) y después lleva al inicio de sesión (Task 2). Invierte la decisión 3 de la Task 10 del plan de la fase 3, que se enmienda con fecha (Task 1).
  - **E2 — Los refresh tokens usados de una concesión viva se borran 30 días después de usarse.** Un paso nuevo del barrido diario, `oauth_used_refresh_tokens` (Task 3). Consecuencia que se documenta: presentar un token usado de más de 30 días es un `invalid_grant` simple, no una reutilización que termina la concesión (enmienda del diseño §5.3 y riesgo residual del runbook, Task 3).
- **K1, K2 y R1 — decididas por el operador el 2026-11-15 (vinculantes; Apéndice B.1).** Las copias nocturnas y externas dejan de volcar la base `keycloak`; la copia previa a la actualización de `deploy.sh` la guarda (`ITS_BACKUP_OLD_IDENTITY_DB=1`), y `restore.sh` la restaura cuando una copia la trae (Task 6). La base, los tres secretos `keycloak-*` del volumen `its-secrets` y las claves `.env` del proveedor viejo se retiran en un solo paso del runbook, «Retiring the old identity provider's data», cuando el operador lo decida; hasta entonces nada dice que se borren (Tasks 6 y 9). El informe de la fase da solo el delta de despliegue (Task 11).
- **Seguridad.** Invariantes 1, 1b, 1c, 2, 2a, 3, 4, 5, 5a y 6 de `CLAUDE.md` intactas. En particular:
  - **1c(a):** en `/mcp` solo vale un token del servidor OAuth propio; esta fase quita lo último que podía validar otro (`app/core/security.py`), y las pruebas que mandan un token con la forma de Keycloak a cada puerta siguen esperando su rechazo (Task 4, Task 11).
  - **1c(b) y 4:** el acto de E1 sigue auditado como `auth.sessions_ended`, ahora con el recuento de concesiones; el barrido de E2 sigue siendo un `auth.retention_applied` con su recuento por paso.
  - **5 y 5a:** el paso nuevo del barrido lleva su predicado `org_id` y bloquea solo lo que borra (FOR UPDATE SKIP LOCKED); la tabla que se borra (`keycloak_event_ingest`, G) sale del catálogo de alcance y de `ALL_SCOPE_ALLOWLIST` en la misma tarea.
  - **3:** ningún secreto en logs; el guion de la CI no imprime token, código, contraseña ni secreto, y el `.env` de la CI es de usar y tirar.
- **Migración.** Una sola, **0248** (`backend/migrations/versions/0248_keycloak_event_ledger_dropped.py`, revision `"0248"`, down_revision **`"0247"`**), hacia adelante; `downgrade()` recrea la tabla exactamente como la creó 0064 (CI corre `upgrade head → downgrade base → upgrade head`, y la Task 5 tiene su prueba de ida y vuelta en una base propia). Ninguna migración publicada se edita, ni sus docstrings que nombran a Keycloak (0064, 0245): son historia.
- **Pruebas.** Ninguna se salta ni se debilita. **Una prueba de una función que se borra se va con ella**, y el Apéndice C las nombra una por una con la pieza que probaba; ninguna protección desaparece en silencio: lo que protegía cada grupo, si sigue siendo cierto de algo que queda, lo prueba otra prueba nombrada en la misma fila. Las pruebas que demuestran que un token de Keycloak se rechaza en una puerta **se conservan** y acuñan el token con `tests/foreign_tokens.py::ForeignIssuer` (una clave RS256 que nada en la plataforma conoce). Las pruebas que pasan a decir lo contrario por una decisión del operador (E1) se invierten a la vista, con su nombre nuevo y el viejo en el docstring. Donde una prueba pasa a ser asíncrona o toca carreras, pytest corre con `-W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning`.
- **Recuentos que mueven varias tareas.** Llamadas de log en `tests/test_logging_discipline.py`: **109** en `main` (`dd062f8`, comprobado con el mismo recorrido AST el 2026-11-15) y **109** al final: ningún archivo que se borra tiene llamadas de log (`kc_client.py`, `realm_config.py`, `app/modules/keycloak_events/`, `app/core/security.py`: cero cada uno) y ninguna tarea añade una. `NEVER_EXPOSED` en `tests/test_mcp_channel_api.py`: **56** → **56** (ninguna ruta nueva ni quitada). `UNGATED_BY_DESIGN`, `SECRET_ONCE`, `NO_STORE_ROUTES`: sin cambios. La Task 11 los comprueba en el árbol fusionado.
- **Archivos generados.** `frontend/src/lib/globalRoutes.json` no se edita a mano; ninguna tarea cambia `GLOBAL_WRITES` ni `GLOBAL_READS`. La Task 11 lo regenera (`cd backend && PYTHONPATH=$PWD .venv/bin/python -m tests.export_console_global_routes`) y comprueba con `git diff --exit-code frontend/src/lib/globalRoutes.json` que no cambia.
- **Textos.** Backend: lo que ve una persona por `tr()` y `app/core/i18n/es.py`; `tests/test_i18n_catalog.py::test_no_stale_catalogue_entries` obliga a borrar las entradas que dejan de usarse, y cada tarea dice cuáles. Consola: `t()` con claves en `frontend/src/i18n/en/` y `es/`. La guarda «ninguna pantalla dice Keycloak» (`frontend/src/lib/noVendorNames.test.ts`, `backend/tests/test_no_vendor_names_backend.py`) se mantiene sin cambios de regla.
- **Documentos.** En el idioma del documento (los del despliegue y `docs/0x-*.md`, en inglés; el diseño y los planes, en español). Cada documento que nombra a Keycloak o cambia o queda como registro fechado, y el Apéndice D lo dice uno por uno. `CLAUDE.md` **no se toca** (Apéndice B.2, P4-14).
- **Despliegue.** Lo que solo se ve con contenedores se verifica en la CI y se dice así (diseño de rollback, D6): esta sesión no tiene demonio de Docker, pero `docker compose config` funciona sin él y las tareas lo usan. Nada se despliega antes de terminar los cuatro proyectos (operador, 2026-10-31); la secuencia de despliegue completa es del informe del proyecto 4, y este plan le entrega los hechos (§«Lo que el despliegue tiene que saber»).
- **Verificación, desde `backend/`:** `.venv/bin/pytest -q` con los archivos que nombra el paso «Las pruebas pasan» de cada tarea; `.venv/bin/mypy app`; `.venv/bin/ruff check app tests`. **Desde `frontend/`:** `npx vitest run` con los archivos que nombra la tarea, `npx tsc -b`, y en la Task 11 `npx vitest run` completo y `npm run build`. La suite completa del backend la corre el coordinador en el árbol fusionado.
- **Pistas en paralelo (worktrees).**
  - La worktree nace de `main` (`dd062f8`); lo primero, `git log --oneline -1`. Las tareas de la oleada 2 en adelante fusionan antes la rama del coordinador con las oleadas anteriores (el coordinador da su nombre; no es un valor que este plan pueda fijar) y lo comprueban: la Task 4 y la Task 5, `grep -c oauth_used_refresh_tokens backend/app/core/auth/retention.py` imprime un número mayor que 0; las de la oleada 3, `ls backend/app/core/security.py` responde «No such file»; las de la oleada 4, `ls deploy/keycloak` responde «No such file».
  - `ln -s /home/user/Novieri-RMM/backend/.venv backend/.venv` y `ln -s /home/user/Novieri-RMM/frontend/node_modules frontend/node_modules`.
  - `cd backend && export PYTHONPATH=$PWD`, y comprobar que `.venv/bin/python -c "import app; print(app.__file__)"` imprime una ruta de la worktree.
  - `export ITS_TEST_REDIS_URL=redis://localhost:6379/9`, o la base de Redis (de 0 a 15) que el coordinador asigne a la worktree si corren varias a la vez.
  - `pg_ctlcluster 16 main start 2>/dev/null; redis-cli ping >/dev/null 2>&1 || redis-server --daemonize yes`.
  - Se hace commit en la rama de la worktree; no se hace push ni se toca el árbol principal.

## Lo que hace esta fase, y lo que no

**Hace.** E1 y E2 (Tasks 1–3). El retiro entero de §8 fase 4 y de §1.3: el validador y sus ajustes (Task 4); los dos clientes de administración, el reenvío de eventos, sus sondas, sus ajustes y su tabla (Task 5); los servicios, la imagen, `deploy/keycloak/`, las rutas, la base de datos de un arranque nuevo, los guiones y `.env.example` (Task 6); la prueba de despliegue de la CI, reescrita contra el servidor propio (Task 7); lo que la consola aún nombraba (Task 8); el runbook y los documentos (Tasks 9 y 10); el falsificador 10 que ya existe, uno nuevo (11: «ningún token, imagen ni servicio de Keycloak es alcanzable ni aceptado en ninguna parte») y los recuentos (Task 11).

**No hace.**
1. **No borra datos de un despliegue existente.** La base `keycloak` de un host que ya la tiene, los secretos `keycloak-*` del volumen y las claves `.env` del proveedor viejo se quedan hasta que el operador los retire con el paso del runbook, cuando decida (K1, K2). Lo único que la actualización quita son los dos contenedores, que no guardan nada (C1).
2. **No migra `users.external_subject`.** Ya no identifica a nadie (diseño §6.1) y guarda marcadores (`itms-local:…`, el del contacto pendiente) o, en una base de antes de la fase 1, el `sub` de Keycloak de cada fila; nadie lo lee como identidad y su clave única sigue sirviendo de clave sustituta. Cambiarlo tocaría los disparadores de 0231 y 0232 y no compraría nada (P4-09).
3. **No escribe la secuencia de despliegue.** Es del informe del proyecto 4; esta fase le entrega los hechos y el delta (Task 11, Step 6).
4. **No renombra lo que ya no es de Keycloak aunque lo parezca:** `app/api/v1/realm_settings.py` (Ajustes → Correo; la ruta es `/settings/email`) y `tests/test_realm_settings_api.py` conservan el nombre (P4-15).

## Oleadas y paralelismo

| Oleada | Tareas | En paralelo | Depende de |
|---|---|---|---|
| 1 | 1, 2, 3 | sí | — |
| 2 | 4, 5, 8 | sí | oleada 1 fusionada (la 4 y la 5 tocan archivos que la 1 y la 3 cambian: `test_setup_api.py` no, pero `retention.py` y `client_scope.py` sí, en la 5) |
| 3 | 6, 7 | sí | 4 y 5 (la 6 borra pruebas que importan `kc_client`; la 7 necesita la API sin Keycloak) |
| 4 | 9, 10 | sí | 6 y 7 (el runbook describe lo que la 6 dejó y la 7 prueba) |
| 5 | 11 | — | todas |

**Las Tasks 6 y 7 se fusionan juntas.** La 6 quita los servicios `keycloak` y `keycloak-init` de `deploy/docker-compose.yml`; la 7 quita de `.github/workflows/ci.yml` los dos pasos que los arrancan. Por separado, el job `deploy` de la CI estaría rojo en cualquiera de las dos ramas; juntas, verde. El coordinador las fusiona una detrás de otra antes de empujar.

## Tareas

| Task | Qué | Diseño |
|---|---|---|
| 1 | E1 en el servidor: `end_sessions` termina las concesiones de IA; descripción MCP, mapa de bloqueos, runbook, enmiendas | §2.4, §5.3; E1 |
| 2 | E1 en la consola: «Cerrar sesión en todas partes» en «Tu seguridad» y en el portal | §2.4; E1 |
| 3 | E2: los refresh tokens usados de una concesión viva, a los 30 días; mapa de bloqueos; enmienda de §5.3 | §5.3; E2 |
| 4 | El validador de Keycloak se va: `security.py`, `deps.jwks`, `app.state.jwks`, `oidc_*`, `multi_tenant`; `ForeignIssuer` en las pruebas | §1.3, §6.5 |
| 5 | Los clientes de administración, el reenvío de eventos, las sondas `jwks` y `keycloak_events`, los ajustes `keycloak_*` y la migración 0248 | §1.3, §6.1 |
| 6 | El despliegue sin Keycloak: compose, Traefik, Postgres, guiones, simulacro, `.env.example`, `deploy/keycloak/` | §8 fase 4 |
| 7 | La prueba de despliegue de la CI contra el servidor propio (`deploy/ci/mcp_flow.py`) | §8 fase 4, §10.6 |
| 8 | Consola: la página de estado sin las sondas de Keycloak | §10.10 |
| 9 | Runbook: `deploy/README.md`, `deploy/GO-LIVE.md`, `docs/07-backup-restore.md`, y su guarda | §9 |
| 10 | Los demás documentos: arquitectura, pila, modelo de datos, hoja de ruta, observabilidad, producto, núcleo, diseños | §9 |
| 11 | Cierre: falsificadores 10 y 11, recuentos, `globalRoutes.json`, el informe y el delta de despliegue | §10 |

## Notas de integración del coordinador

- **Oleada 1, choques esperados** (orden de fusión: 1, 3, 2):
  - `backend/app/core/auth/guards.py` (el docstring de `lock_user_row`): la 1 añade `end_sessions` a la viñeta «THE ACTS THAT END A PERSON'S AI GRANTS»; la 3 cambia la viñeta de la retención, unas 60 líneas más abajo: unión.
  - `deploy/README.md`: la 1 cambia la viñeta «One person» de `### Revoking` y el riesgo 6; la 3 cambia la frase de cabecera de `### Residual risks, stated plainly` y añade el riesgo 12 detrás del 11. Las ediciones de la 3 quedan a más de tres líneas de las de la 1 salvo la frase de cabecera, que está a 50 líneas del riesgo 6: unión.
  - `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §5.3: **choque**. La 1 añade su enmienda detrás de la viñeta «Revocar un cliente de IA en Ajustes…» (`:340`); la 3, la suya detrás de la viñeta «**Refresco:**…» (`:338-339`), es decir, delante de la de «Revocar». Los dos trozos están pegados. Se conservan los dos: en el árbol fusionado §5.3 queda «Acceso», «Refresco», la enmienda E2, «Revocar», la enmienda E1, y `### 5.4`.
  - La 2 no toca ningún archivo de la 1 ni de la 3: `frontend/` solo. Pero el texto de la 1 en el runbook (la viñeta «One person» de `### Revoking`) nombra «their security page», cuyo botón añade la 2: el orden de fusión 1, 3, 2 deja el árbol fusionado en lo cierto al cerrar la oleada.
- **Oleada 2, choques esperados** (orden de fusión: 5, 4, 8):
  - `backend/app/core/config.py`: la 4 quita el bloque `oidc_*`…`multi_tenant` (`:32-56`) y deja `default_org_slug` con su comentario nuevo; la 5 quita el bloque `KEYCLOAK EVENT FORWARDING` y el de aprovisionamiento (`:82-113`) y una frase del comentario de `mcp_registration` (`:166-168`). Entre `:56` y `:82` quedan 25 líneas sin tocar: unión.
  - `backend/tests/test_setup_api.py`: la 4 quita el import de `tests.fake_idp` (`:156`), las dos líneas `oidc_*` (`:171-172`) y el `jwks_http_client` (`:181`); la 5 cambia `FakeKeycloak` por `OutboundRecorder` (`:57`, `:106`, `:113`, `:198-201`) y las aserciones `kc.seen` (`:339`, `:446`, `:522`) y la de `keycloak_admin_url` (`:515`). Las dos de la 4 más cercanas a la 5 (`:181` y `:198`) tienen 16 líneas entre ellas: unión.
  - `backend/app/core/i18n/es.py`: la 4 borra siete entradas (`:159-168`, `:5233-5238`); la 5 borra cinco en otros sitios (las dos etiquetas de sonda y las tres de `realm_config.py`): unión.
  - `backend/tests/test_platform_health_sampler.py`: la 4 le quita sus dos líneas `oidc_*` (`:44-45`, en su barrido), y la 5 sus pruebas de `jwks` y sus imports (`:31-33` y más abajo), a 10 líneas de distancia: unión. Así la 5 sola sigue verde aunque `oidc_issuer` y `oidc_jwks_url` sean aún obligatorios en su rama (revisión 2, I1). `test_platform_health_probes.py` es solo de la 5. `tests/test_config.py` es solo de la 4: la 5 no lo toca (Task 5, decisiones).
  - **La 5 se fusiona antes que la 4, a propósito:** la 4 quita `app.state.jwks`, que la sonda `jwks` lee, y la 5 quita la sonda; en el orden 5, 4 el árbol fusionado nunca tiene una sonda que lea un atributo que no existe (P4-32).
  - La 8 solo toca `frontend/`.
- **Oleada 3**: la 6 y la 7 no comparten ningún archivo. La 7 crea `deploy/ci/mcp_flow.py` y borra `deploy/keycloak/ci_mcp_flow.py`; la 6 borra el resto de `deploy/keycloak/`. Se fusionan juntas (arriba): el paso de K1 que la 7 añade a la CI corre el `backup.sh` de la 6, y con el de antes fallaría a propósito (el viejo vuelca `keycloak` siempre).
- **Oleada 4**: la 9 es dueña de `deploy/README.md`, `deploy/GO-LIVE.md` y `docs/07-backup-restore.md`; la 10, del resto de `docs/` y de `DESIGN.md`, `HANDOFF.md`. Ninguna toca un archivo de la otra.
- **Firmas que una tarea fija y otras consumen:** el Apéndice A. Donde el bloque «Interfaces» de una tarea dice otra cosa, manda la tarea, que se escribió contra el código real.
- **Para el informe de despliegue**, el apartado siguiente.

## Lo que el despliegue tiene que saber

No son comandos de despliegue: la secuencia completa es del informe del proyecto 4, que empieza por `docker compose exec openbao bao status` (`CLAUDE.md`, «Reporting to the operator»). Son los hechos que esa secuencia necesita, comprobados contra el código de `dd062f8` y contra las fuentes del Apéndice E.

**El caso de verdad es un arranque nuevo.** El proyecto 4 despliega en un VPS nuevo y «se empieza desde cero» (diseño del proyecto 1, P7). En un arranque nuevo no hay nada de Keycloak que retirar: `init-app-user.sh` ya no crea la base `keycloak` (Task 6), compose no define el servicio, `.env.example` no trae sus claves. Todo lo que sigue es para el host existente (`itms.toeshee.com`), si el operador decide actualizarlo en vez de reemplazarlo.

1. **Los contenedores.** Al quitar los servicios del archivo, `docker compose up -d` **no** borra los contenedores `keycloak` y `keycloak-init` que ya existen: los deja como huérfanos y avisa (Docker: «`--remove-orphans` Remove containers for services not defined in the Compose file»; con `COMPOSE_REMOVE_ORPHANS` sin poner, «Compose displays a warning about orphaned containers instead» [D1, D2]). **Y Traefik sigue enrutando a uno huérfano**: su proveedor de Docker lee las etiquetas de cada contenedor del motor («By default, Traefik watches for container level labels on a standalone Docker Engine»; con `exposedByDefault=false` solo ignora los que no llevan `traefik.enable=true` [D3]), y el `keycloak` huérfano conserva las suyas —`/realms`, `/resources`, `/js` y el registro anónimo—. Por eso el `deploy.sh` nuevo **quita esos dos contenedores por su nombre** cuando la capa de aplicación nueva ya está arriba, y nada más del proyecto: no `--remove-orphans`, que quitaría también lo que el operador haya añadido (decisión del coordinador C1; Task 6); el runbook da el mismo paso para la secuencia a mano (Task 9). *Que Traefik enrute a un contenedor que compose ya no define se deduce de esas frases; no se ha visto en vivo (Apéndice E, SIN VERIFICAR).*
2. **Su volumen.** Keycloak no tiene volumen propio en este stack: guarda todo en la base `keycloak` del Postgres compartido («Configure a relational database for Keycloak to store user, client, and realm data» [K1]), y la imagen 26.2.0 no declara ningún `VOLUME` (su `Dockerfile`, comprobado por la revisión previa). El directorio `/opt/keycloak/data` del contenedor solo recibía el realm por `--import-realm` desde un bind mount [K2]. Borrar el contenedor no pierde nada que no esté en la base.
3. **Su base de datos (decisión del operador K1, 2026-11-15).** La base `keycloak` guarda «hashed user passwords, client credentials and realm keys that are used to sign tokens» [K1]: no sirve de nada al producto desde esta fase y es material sensible. **Desde la actualización, las copias nocturnas y externas no la vuelcan**; la copia que `deploy.sh` toma antes de actualizar sí (`ITS_BACKUP_OLD_IDENTITY_DB=1`), mientras la base exista (la de la actualización que cruza la fase 4 es la vuelta atrás, punto 7). Se queda en el clúster hasta el paso «Retiring the old identity provider's data» del runbook, que el operador corre cuando elige (Task 9). Una migración **podría** borrarla —Alembic se conecta a `itsuite` y `autocommit_block()` corre una sentencia fuera de la transacción [P1, A1]—, pero sería un borrado irreversible y desatendido: por eso es un acto del operador y no de `up -d`.
4. **Sus secretos (decisión del operador K2).** `keycloak-init` escribió en el volumen `its-secrets` (`/var/lib/its/secrets/`) `keycloak-admin.secret`, `keycloak-events.secret` y `keycloak-bootstrap-admin.secret` (`deploy/keycloak/init-clients.py:106`, `:111`, `:119`). No hay `deploy/secrets/` en el repositorio. Ninguna pieza de esta fase los lee; los leían `keycloak-init` (el de administración y el de la cuenta bootstrap) y el barrido de eventos del worker viejo (el de eventos; `kc_client` no tiene ningún lector desde la fase 3). Se borran en el mismo paso del runbook que la base.
5. **Lo que el respaldo deja de incluir.** Desde la actualización, `backup.sh` no produce `keycloak.dump` salvo en la copia previa de `deploy.sh`, y mientras la base exista imprime una nota que lo recuerda; `restore-drill.sh` deja de exigirlo (Task 6). **Una copia vieja que contiene `keycloak.dump` se sigue restaurando**: `restore.sh` ya restauraba ese archivo solo si estaba (`deploy/scripts/restore.sh:227-235`), crea la base si falta (`prepare_db`, `:193-197`) y, tras el `pg_restore` y antes de volver a estrechar `app_user`, dice qué es y que se conserve si se está volviendo atrás (Task 6). Lo prueban dos pruebas con un `ITS_PG_EXEC` falso (una copia con el volcado se restaura y la siguiente copia nocturna no lo lleva) y un paso de la CI (Task 7).
6. **Las claves `.env` que quedan sin lector** (las que el host tiene y `.env.example` ya no trae): `KEYCLOAK_ADMIN_USER`, `KEYCLOAK_ADMIN_PASSWORD`, `ITS_OIDC_ISSUER`, `ITS_OIDC_JWKS_URL`, `ITS_OIDC_AUDIENCE`, `ITS_OIDC_ORG_CLAIM`, `ITS_OIDC_CONSOLE_CLIENT_ID`, `ITS_MULTI_TENANT`, `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`, `ITS_KEYCLOAK_ADMIN_URL`, `ITS_KEYCLOAK_REALM`, `ITS_KEYCLOAK_EVENTS_CLIENT_ID`, `ITS_KEYCLOAK_EVENTS_SECRET_PATH`, `ITS_KEYCLOAK_ADMIN_CLIENT_ID`, `ITS_KEYCLOAK_ADMIN_SECRET_PATH`. **Esta versión no las lee**: compose ya no las interpola, y los contenedores reciben `.env` como variables de entorno, que pydantic-settings lee solo para un campo que existe (y `extra="ignore"` descarta las de un archivo dotenv); lo fijan `tests/test_config.py` en sus dos formas (Task 4) y el falsificador 11 (Task 11). **Pero cuatro las necesita la versión anterior, y con ella la vuelta atrás**: su compose no se interpreta sin `KEYCLOAK_ADMIN_USER` y `KEYCLOAK_ADMIN_PASSWORD` (`dd062f8`, `deploy/docker-compose.yml:592-593`, `:747`, `:752`), y su API, su gateway, su worker y su `migrate` no arrancan sin `ITS_OIDC_ISSUER` y `ITS_OIDC_JWKS_URL` (`backend/app/core/config.py:32-34`, sin valor por omisión). Así que `deploy.sh` las nombra y **dice que no se borren todavía**; se borran en el paso «Retiring the old identity provider's data» (decisión del operador K1). `ENTRA_CLIENT_SECRET`, si existe, es un secreto vivo de la aplicación de Entra que usaba el broker de Keycloak: ese paso manda revocarlo en Entra. `preflight.sh` era la única pieza del repositorio que aún leía una (`ITS_OIDC_ISSUER`, para comprobar el certificado); pasa a `ITS_PUBLIC_CONSOLE_URL` (P4-38). **`ITS_DEFAULT_ORG_SLUG` no es obsoleta**: vivía en el bloque «Keycloak / OIDC» de `.env.example` y se queda, en su bloque propio (Task 6).
7. **Migración y vuelta atrás.** El `migrate` aplica 0248 en el siguiente `up -d` (borra `keycloak_event_ingest`). Como el esquema se mueve, **volver atrás es restaurar la copia** (diseño de rollback, D1), no `alembic downgrade`: `deploy.sh` ya lo imprime así cuando hay revisión nueva. **La copia que sirve se reconoce por su esquema, no por su `keycloak.dump`: es la copia previa a la actualización que cruzó la fase 4: la `pre-upgrade-*` más nueva cuyo `alembic_version` es menor que `0248`** (lo dicen el runbook, Task 9, y `deploy.sh`, Task 6). Una copia posterior no cruza 0248 hacia atrás, y llevar `keycloak.dump` no la hace la buena: `deploy.sh` vuelca la base en cada copia previa mientras exista, también en las de actualizaciones posteriores, ya a 0248. En el caso normal es la que tomó el `deploy.sh` de la actualización que cruzó la fase 4, con `keycloak.dump`, y la restauración forzada devuelve las dos bases. Volver además necesita el compose, `deploy/keycloak/` y los guiones de antes: el `rollback_help` de `deploy.sh` pasa a imprimir `git checkout <base> -- deploy` entero cuando el compose cambió (Task 6; antes nombraba `deploy/docker-compose.yml` y `deploy/keycloak` sueltos), y avisa si `.env` ya no tiene las cuatro claves del punto 6. Con `IT_SUITE_VERSION` en la etiqueta anterior y `docker compose up -d`, el host vuelve a como estaba. *Si en ese host `build-images.sh` nunca dejó una etiqueta anterior («Recent tags: (none yet)»), no hay imagen a la que volver y el único camino es restaurar la copia y reconstruir la versión vieja; el informe del proyecto 4 lo comprueba (`CLAUDE.md`).* Un detalle del tránsito: entre el `migrate` (que borra `keycloak_event_ingest`) y la recreación del worker, el beat **viejo** puede lanzar su `keycloak_event_sweep` horario contra la tabla ya borrada y dejar una línea de error; no hace nada más (revisión 3, M13).
8. **La primera actualización a esta versión con `deploy.sh` tiene que empezar con `git pull`.** `deploy.sh` hace su propio `git pull` a mitad de la ejecución, pero bash sigue ejecutando el texto del guion que abrió: comprobado aquí con git 2.43 y bash 5.2 (un guion que hace checkout de otra versión de sí mismo sigue imprimiendo su versión vieja; `scratchpad/p2f4plan/bashswap/probe.sh`; la revisión previa lo repitió con `git pull --ff-only`). El `deploy.sh` de antes de esta fase graba `IT_SUITE_VERSION` nuevo y migra, y después reinicia `keycloak` y corre `keycloak-init`, servicios que el compose nuevo ya no tiene: se para con `no such service` **después** de migrar, con la capa de aplicación en las imágenes viejas. Con `git pull` antes, el `deploy.sh` que corre es el nuevo (lo dicen el runbook, Task 9, y la cabecera de `deploy.sh`, Task 6). **Y si ya pasó**, volver a correr el `deploy.sh` nuevo termina la actualización: mientras quede un contenedor `keycloak` o `keycloak-init` no responde «Already deployed» y hace un despliegue completo (decisión del coordinador C2; Task 6). La copia que tomó aquel `deploy.sh` viejo la hizo el `backup.sh` nuevo sin la opción, sin `keycloak.dump`, a 0247; la base sigue en el clúster y `restore.sh` solo toca las bases que trae la copia, así que esa copia es la vuelta atrás, y la del `deploy.sh` que termina la actualización, ya a 0248, no lo es. Ese `deploy.sh` la busca por su esquema (la `pre-upgrade-*` más nueva por debajo de 0248), lee de su `image_version` la etiqueta vieja, y las dice al empezar y en su ayuda de vuelta atrás del final, con la restauración de esa copia, esa etiqueta y el `git checkout` de esa versión (re-revisión, N2; `test_the_way_back_from_a_half_done_upgrade_is_the_backup_from_before_0248`). Y al verificar comprueba, por las etiquetas de compose de los contenedores, que ninguno de los dos queda (re-revisión, m11).
9. **OpenBao y `.env` nuevos:** ninguno. Ninguna ruta nueva de OpenBao, ninguna clave `.env` nueva. Las imágenes `itsuite/backend` y `itsuite/web` cambian (backend y consola); la imagen `quay.io/keycloak/keycloak:26.2` deja de usarse y el paso del runbook la borra (`docker image rm quay.io/keycloak/keycloak:26.2`), cosa que tampoco borra datos.

## Apéndice A — Contratos

- `backend/app/core/auth/retention.py` (Task 3): `OAUTH_USED_TOKEN_RETENTION = timedelta(days=30)`; `async def _oauth_used_refresh_tokens(session, org_id, now) -> int`; `STEPS` gana `("oauth_used_refresh_tokens", _oauth_used_refresh_tokens)` entre `("oauth_refresh_tokens", …)` y `("oauth_grants", …)`; el evento `auth.retention_applied` gana la clave `oauth_used_refresh_tokens`.
- `backend/app/modules/user_admin/service.py::end_sessions` (Task 1): misma firma; el evento `auth.sessions_ended` lleva `{"by": "administrator", "scope": "everywhere", "ai_grants_revoked": int}`; las familias terminan con `revoked_reason = "signed_out_everywhere"`.
- `frontend/src/lib/session.ts` (Task 2): `export async function signOutEverywhere(api: Pick<ApiClient, "logoutEverywhere">, to: string = SIGN_IN_PATH): Promise<void>`. `SecurityBody` y `SessionsPanel` ganan la prop `signIn: string`.
- `backend/tests/foreign_tokens.py` (Task 4): `ISSUER`, `CONSOLE_CLIENT_ID`, `class ForeignIssuer` con `kid`, `private_pem`, `public_jwk() -> dict[str, Any]` (para el falsificador 11, Task 11) y `token(sub, org_slug="toeshee", *, aud="it-suite", iss=ISSUER, exp_in=300, email=…, name=…, azp=CONSOLE_CLIENT_ID, typ="Bearer", sid="fake-session", extra=None) -> str` (la firma de `FakeIdp.token`, sin JWKS). `ApiEnv.foreign` reemplaza a `ApiEnv.idp`. `create_app(settings: Settings | None = None) -> FastAPI` (sin `jwks_http_client`).
- `backend/tests/traefik_routes.py` (Task 6): `COMPOSE`, `def compose(path: Path = COMPOSE) -> dict[str, Any]`, `def routers(path: Path = COMPOSE) -> list[tuple[int, str, str]]` (con `functools.cache`), `def matches(rule: str, path: str, method: str) -> bool`, `def winner(path: str, compose_file: Path = COMPOSE) -> str`, `METHODS`. Lo que era `_routers`, `_matches` y `_winner` de `test_deploy_keycloak_registration_edge.py`, que se borra.
- `backend/tests/test_user_admin_api.py::OutboundRecorder` (Task 5): `seen: list[httpx.Request]`, `handler(request) -> httpx.Response` (responde 599 y anota); `install_outbound(monkeypatch) -> OutboundRecorder` (todo `httpx.AsyncClient` envía al registrador, el construido sin `transport` y el que trae un transporte de red propio, como los de `app/core/egress.py`; solo uno en proceso, `httpx.ASGITransport` o `httpx.MockTransport`, conserva el suyo). `users_env.outbound` reemplaza a `users_env.kc`; `setup_env.outbound`, a `setup_env.kc`; en `test_mcp_admin_api.py` y `test_me_locale.py`, `env.outbound`.
- `deploy/ci/mcp_flow.py` (Task 7): `python3 ci/mcp_flow.py --base https://localhost --cafile certs/fullchain.pem --phase closed` y `… --phase open --setup-code-file "$RUNNER_TEMP/setup-code"`; sale 0 si todo se cumple, 1 con una línea `FAIL` en la primera aserción que no se cumple (cada paso necesita los anteriores), 2 si no llega. Funciones puras que prueba `backend/tests/test_ci_mcp_flow.py` (y `FORMER_KEYCLOAK_PATHS`, que no puede quedar vacía): `s256`, `pkce_pair`, `jwt_parts`, `audiences`, `exact_audience`, `totp`, `totp_secret`, `challenge_params`, `authorize_query`, `callback_params`, `tampered`, `foreign_token`, `describe_claims`, `Cookies`.
- `deploy/scripts/backup.sh` (Task 6): `ITS_BACKUP_OLD_IDENTITY_DB` (`0` por omisión o `1`; otro valor se niega antes de volcar). `deploy/scripts/deploy.sh` (Task 6): `OBSOLETE_ENV_KEYS`, `obsolete_env_keys()`, `old_identity_containers()`, `remove_old_identity_containers()`, `way_back_backup()`, `stop_if_already_deployed()` (con `ROLLBACK_BACKUP_DIR` y `ROLLBACK_BASE`, que lee `rollback_help`), `old_identity_containers_left()`, `verify_old_identity_gone()`.
- Migración `0248` (Task 5): `upgrade()` borra `keycloak_event_ingest`, su política y su índice; `downgrade()` la recrea como 0064.


## Apéndice B — Decisiones

### B.1 Decisiones del operador (decididas el 2026-11-15; vinculantes)

**K1 y K2 — los datos del proveedor de identidad viejo, con un solo paso del runbook.** Decididas por el operador el 2026-11-15, sobre los hechos de `deploy/scripts/deploy.sh:312-359`, `backup.sh:92-93` y `restore.sh:222-235`: `deploy.sh` toma una copia antes de actualizar, esa copia trae `keycloak.dump`, `restore.sh` recrea la base desde ella, y es la vuelta atrás pasada la fase 4 (una copia nocturna tomada después de actualizar no puede cruzar la migración 0248 hacia atrás).

- **Desde la actualización a la fase 4, las copias nocturnas y externas (`backup.sh`) no vuelcan `keycloak`**, aunque la base siga existiendo. `backup.sh` lo dice en una nota mientras la base exista: el recordatorio de que sigue ahí (Task 6).
- **`restore.sh` sigue restaurando un `keycloak.dump` cuando una copia lo trae**, así que una copia de antes de la fase 4 se restaura entera. Lo prueban `test_a_backup_with_the_old_database_restores_it_and_the_next_backup_has_none` y `test_a_backup_without_it_restores_without_touching_it` (Task 6, con un `ITS_PG_EXEC` falso) y, en vivo, el paso nuevo del job `deploy` de la CI (Task 7).
- **La copia previa a la actualización que toma `deploy.sh` guarda `keycloak`**, porque la de la actualización que cruza la fase 4 es la vuelta atrás. La copia de la vuelta atrás se reconoce por su esquema, no por su volcado: es la copia previa a la actualización que cruzó la fase 4: la `pre-upgrade-*` más nueva cuyo `alembic_version` es menor que `0248` (`deploy.sh` vuelca la base en cada copia previa mientras exista, también en las posteriores, ya a 0248). **El mecanismo, elegido por este plan:** `backup.sh` lee `ITS_BACKUP_OLD_IDENTITY_DB` (`0` por omisión, `1` o se niega antes de volcar nada); con `1`, y solo si la base existe, vuelca `keycloak` y exige el volcado no vacío. `deploy.sh` es el único que pone `1` (`ITS_BACKUP_OLD_IDENTITY_DB=1 ./scripts/backup.sh "$BACKUP_DIR"`), y `scheduled-backup.sh` pone `0` explícitamente, para que una variable heredada no cambie la copia nocturna. Una opción en vez de un paso aparte, porque así la copia previa sigue siendo un solo directorio con un solo conjunto de comprobaciones (artefactos no vacíos, `globals.sql` con los tres roles) y `restore.sh` no cambia. Lo prueban cuatro pruebas de `test_platform_backup_scripts.py` (Task 6) y la CI (Task 7). *Un caso que lo rodea:* si el `deploy.sh` de antes de la fase 4 hace él mismo el `git pull` (punto 8), su copia la toma el `backup.sh` nuevo sin la opción, sin `keycloak.dump`; la base sigue intacta en el clúster y `restore.sh` solo toca las bases que trae la copia, así que la vuelta atrás con esa copia la deja como está. El `deploy.sh` que termina esa actualización busca esa copia por su esquema y la nombra, con la etiqueta vieja de su `image_version`, al empezar y en su ayuda de vuelta atrás del final (Task 6). La secuencia a mano del runbook pasa la opción también en su copia del paso 1 (Task 9).
- **Un solo paso del runbook, «Retiring the old identity provider's data»**, que el operador corre cuando ha comprobado la actualización, en el momento que elija (sin «una versión después»): (1) una copia final opcional, con los comandos exactos y su destino (`~/backups/keycloak-final.dump`); (2) `dropdb keycloak`; (3) borra los tres archivos `keycloak-*.secret` del volumen `its-secrets`; (4) borra las cuatro claves `.env` que necesita una vuelta atrás (`KEYCLOAK_ADMIN_USER`, `KEYCLOAK_ADMIN_PASSWORD`, `ITS_OIDC_ISSUER`, `ITS_OIDC_JWKS_URL`) con un comando que las borra, sin marcadores; (5) borra las demás claves obsoletas; (6) dice claramente que **cierra la vuelta atrás** a las imágenes de antes de la fase 4; (7) manda revocar en Entra el `ENTRA_CLIENT_SECRET` viejo, si alguna vez se creó para el broker de Keycloak (Task 9).
- **Hasta ese paso, el runbook y `deploy.sh` dicen que NO se borren** esas claves, los secretos ni la base: ningún «harmless; remove them by hand» queda (Tasks 6 y 9; revisión 1 I4, revisión 3 I2).
- **Las premisas de K1, corregidas** donde aparecían: la opción D (una migración) **es posible** con `autocommit_block()` de Alembic (`alembic/runtime/migration.py:274-283`, instalado: «to support the occasional database DDL or system operation that specifically has to be run outside of any kind of transaction block»), pero desaconsejable: un borrado irreversible y desatendido en cada `up -d`; `kc_client` no tiene ningún lector desde la fase 3 (`app/modules/user_admin/__init__.py:7-11`); quien leía los secretos es `keycloak-init` (el de la cuenta bootstrap y el de administración) y el barrido de eventos del worker viejo (`keycloak_events/client.py:49-58`, el de eventos); y lo que de verdad cuesta la opción A de antes es que **los hashes de contraseña y las claves del realm** («hashed user passwords, client credentials and realm keys that are used to sign tokens» [K1]) iban en cada copia nocturna y externa.

**R1 — el informe de la fase da solo el delta.** El informe de esta fase da el delta de despliegue; la secuencia completa y copiable va en el informe del proyecto 4, como en la fase 3 (Task 11, Step 6, lo dice).

E1 y E2 se decidieron antes (2026-11-15; Global Constraints). Ninguna otra pregunta queda abierta: lo demás lo decide una regla, el código o el coordinador (B.2).

### B.2 Decisiones del plan

| # | Decisión | Razón y fuente | Dónde |
|---|---|---|---|
| P4-01 | E1 termina las familias con el motivo `signed_out_everywhere`, el mismo del «Cerrar sesión en todas partes» de la persona. **Decisión del coordinador (2026-11-15): se queda**, porque es el nombre del propio botón. | `REVOKE_REASONS` es una lista cerrada en un CHECK de 0247 (`backend/app/models/oauth.py:37-48`, `ck_oauth_refresh_families_reason`): un motivo nuevo pediría una migración. El evento de auditoría distingue el acto del administrador del de la persona por la acción y el actor: `auth.sessions_ended` con `by: administrator` y el administrador como actor en `end_sessions`, `by: self` y la persona como actor en `flows.logout_everywhere`; lo dicen el docstring del módulo y el de `end_sessions` (Task 1). La fila de la familia se barre al día de terminar (fase 3, Task 13). | Task 1 |
| P4-02 | **El portal también tiene el botón** (decisión del coordinador, 2026-11-15: se queda; el operador dijo «each person»). Un contacto no tiene concesiones de IA, pero «Cerrar sesión en todas partes» le cierra las sesiones y le olvida los equipos recordados, que es lo que hace quien cree que le han usado la cuenta (`flows.logout_everywhere`, docstring). La ruta ya lo admite: `tests/test_auth_contact_security.py:451` lo prueba con `ai_grants_revoked: 0`. El texto del contacto no menciona IA. | Diseño §3: los contactos tienen «Mismos límites de intentos y mensajes genéricos que los técnicos»; §2.4 da la sesión propia a ambos. | Task 2 |
| P4-03 | **Después, la página va al inicio de sesión:** `/sign-in` en la consola, `/portal/sign-in` en el portal (`SIGN_IN_PATH`, `PORTAL_SIGN_IN_PATH`, `frontend/src/lib/session.ts:71`, `:74`), como `signOut`. **Un 401 cuenta como sesión cerrada**, como en `signOut` (decisión del coordinador, 2026-11-15): la sesión de esta pestaña ya no estaba, así que la página va al inicio de sesión (`/sign-in`, o `/portal/sign-in` para un contacto); cualquier otro error se queda en la página con su motivo. Lo prueban `session.navigation.test.ts` (el 401 y un 503) y `SecurityPage.test.tsx` (el 401 va a `/sign-in` sin alerta; un 503 se queda). | La sesión de esta pestaña muere con las demás (`flows.logout_everywhere` cierra «Every session of the person, this one included»); `auth.py:623-629` borra la cookie. | Task 2 |
| P4-04 | El botón pide una confirmación en un diálogo y **no** pide el segundo factor. | Diseño §2.5, decisión del operador S4: «No la piden cancelar una invitación, desactivar a una persona, cerrar sus sesiones, apagar MCP ni revocar una aplicación de IA: solo quitan acceso». La ruta no llama `require_step_up`. | Task 2 |
| P4-05 | E2: un paso propio, `oauth_used_refresh_tokens`, con su clave propia en `auth.retention_applied`, entre `oauth_refresh_tokens` y `oauth_grants`. | El brief admitía plegar el recuento en `oauth_refresh_tokens`; una clave propia separa «los tokens de concesiones terminadas» de «los usados de concesiones vivas», que es lo que alguien lee en un incidente. Hojas antes que concesiones, como THE OAUTH STEPS: un token no arrastra nada en cascada y su concesión se queda. | Task 3 |
| P4-06 | E2 toma solo concesiones **vivas** (sin revocar y sin pasar su límite de inactividad, con el reloj del barrido), y los tokens usados de una concesión terminada siguen siendo del paso `oauth_refresh_tokens`, al día de terminar. | Las palabras del operador («of a LIVE grant»), y que los dos pasos no compitan por la misma fila: el de concesiones terminadas excluye además las que un código aún nombra. | Task 3 |
| P4-07 | Las pruebas que mandan un token de Keycloak a una puerta lo siguen mandando, firmado por `ForeignIssuer`, una clave RS256 que nada en la plataforma conoce, **sin JWKS**; solo el falsificador 11 ofrece su JWKS a toda petición saliente (`ForeignIssuer.public_jwk()`), para que un validador que quedara no pueda pasar rechazando una clave desconocida (Task 11). | El brief: «Tests that prove a Keycloak token is refused everywhere must keep proving it, now with a token signed by an unknown key». Hoy el JWKS de las pruebas lo servía `jwks_client` a un validador que ninguna puerta llama desde la fase 3; quitarlo no cambia lo que la puerta ve. | Tasks 4, 11 |
| P4-08 | `NEVER_ORG_SETTINGS` (`backend/app/api/v1/orgs.py:255-277`) cambia `oidc_issuer`, `oidc_jwks_url` y `oidc_audience` por las anclas del inicio de sesión propio: `public_console_url` (emisor OAuth y origen de la CSRF), `auth_keys_backend`, `auth_keys_dir`, `default_org_slug`; y una prueba nueva exige que cada nombre de la lista sea un campo real de `Settings`. | La lista existe para que un ajuste de organización nunca pueda «redefine what the control plane trusts» (`orgs.py:83-86`); los tres nombres viejos ya no son ajustes, y los que ahora sostienen la identidad no estaban. | Task 4 |
| P4-09 | **Ninguna migración para `users.external_subject`.** | Diseño §6.1: «deja de identificar a nadie». Sigue siendo `NOT NULL` y único por organización, con marcadores; renombrarlo o vaciarlo tocaría `its_user_identity_guard` (0231, 0232) y la clave `users_org_id_external_subject_key`, sin ganar nada. Su motivo en `ORG_WIDE_KEYS` se reescribe (Task 5). | — |
| P4-10 | `keycloak_event_ingest` se borra con la migración 0248, cuyo `downgrade()` la recrea como 0064. | Diseño §6.1 y §1.3; la decisión D4 de la fase 1 lo dejó para esta fase. La tabla nunca fue evidencia (0064, docstring: «The evidence is in `audit_events`»). | Task 5 |
| P4-11 | El vocabulario de auditoría de la consola **conserva** las cuatro acciones que escribía el reenvío (`auth.identity_linked`, `auth.identity_unlinked`, `auth.passkey_registered`, `auth.passkey_removed`); la prueba de cobertura deja de buscarlas en `mapping.py`. | Las filas viejas siguen en la cadena de un host existente, y la página las tiene que decir en palabras. La prueba solo exige palabras para lo que el backend escribe; no exige lo contrario. | Task 5 |
| P4-12 | `backup.sh` vuelca `keycloak` **solo con `ITS_BACKUP_OLD_IDENTITY_DB=1`** (que pone `deploy.sh` en su copia previa) **y mientras exista** la base; sin la opción, la deja fuera y lo dice; `restore.sh` restaura `keycloak.dump` cuando la copia lo trae y lo dice. | Decisión del operador K1 (B.1). En un arranque nuevo no hay base y no hay volcado. `restore.sh` ya era condicional (`:227-235`). | Task 6 |
| P4-13 | `deploy.sh`: sin pasos de Keycloak; quita por su nombre los contenedores `keycloak` y `keycloak-init` (C1, abajo); no dice «Already deployed» mientras quede alguno (C2); su ayuda de vuelta atrás hace `git checkout <base> -- deploy` entero cuando el compose cambió y avisa si `.env` ya no tiene las cuatro claves que necesita una versión de antes de la fase 4; nombra las claves `.env` obsoletas que encuentre y dice que no se borren todavía, sin tocarlas. | El compose viejo monta `deploy/keycloak/`, que la versión nueva ya no tiene, así que devolver el compose solo no basta; el compose viejo no se interpreta sin `KEYCLOAK_ADMIN_*` (`dd062f8`, `deploy/docker-compose.yml:592-593`, `:747`, `:752`) y la API vieja no arranca sin `ITS_OIDC_ISSUER` y `ITS_OIDC_JWKS_URL` (`config.py:32-34`); y el guion «never edits `.env` beyond IT_SUITE_VERSION» (`deploy.sh:30`). | Task 6 |
| P4-14 | **`CLAUDE.md` no se toca.** Su línea 396 nombra `keycloak-init` como el ejemplo de la petición del operador del 2026-08-20 («not "same as last time, plus `keycloak-init`"»): es un registro fechado y sigue siendo verdad como tal. 1c(a) no nombra proveedor (diseño §9). | Las reglas de la sesión: `CLAUDE.md` es del operador. Si quiere otra frase de ejemplo, la cambia él. | — |
| P4-15 | `app/api/v1/realm_settings.py` y `tests/test_realm_settings_api.py` conservan el nombre. | Son Ajustes → Correo (`/api/v1/settings/email`); «realm» es historia del nombre de archivo, no una pieza de Keycloak, y renombrar un módulo de rutas mueve imports y pruebas sin cambiar nada que se vea. | — |
| P4-16 | `frontend/src/lib/oidcLeftovers.ts` (lo que la consola de Keycloak dejó en el almacenamiento del navegador) **se queda**. | `localStorage` sobrevive a la pestaña, y un navegador que usó la consola de antes de la fase 1 aún puede tener `oidc.*`; el código borra solo esas claves y cuesta nada. No hay un punto posterior en el que esto deje de poder pasar. | — |
| P4-17 | La prueba de despliegue de la CI levanta la API con **OpenBao real** (una sola parte de la clave, su token raíz en el `.env` de la CI) y registra la app de IA por **DCR** (`ITS_MCP_REGISTRATION=open`), no por CIMD. | Las claves de inicio de sesión viven en OpenBao (`auth-signing`, `auth-pepper`), y el runbook decía de ese camino «read, not run» (`deploy/README.md:533-538`): esta es su primera ejecución. CIMD exige un documento en una dirección pública por el 443 (`egress.get_bounded`), que una CI hermética no tiene; DCR prueba lo mismo del token en adelante. | Task 7 |
| P4-18 | El guion de la CI sigue siendo de la biblioteca estándar; su token ajeno es un HS256 con una clave aleatoria, con los claims de Keycloak. | Corre con el `python3` del runner, sin venv (lo fija su prueba); la biblioteca estándar no firma RS256. La puerta lo rechaza igual: ni algoritmo, ni clave, ni emisor son los suyos. | Task 7 |
| P4-19 | `/.well-known/openid-configuration` sigue enrutado a la API, que responde 404 a propósito; se reescribe su comentario sin el realm. | MCP: los clientes «MUST support both discovery mechanisms» (RFC 8414 y OpenID Connect Discovery) [M1]; si cae en la consola, su 200 en HTML termina el inicio de sesión del cliente (`test_deploy_mcp_route.py`). | Task 6 |
| P4-20 | Las rutas que eran de Keycloak (`/realms`, `/resources`, `/js`, `/admin`) caen en la consola (`web`), que responde su propia página. | Ningún router las nombra ya; el modelo de routers lo fija (Task 6) y la CI lo ve en vivo (Task 7: ningún documento OIDC detrás). | Tasks 6, 7 |
| P4-21 | Se quedan `ReasonCode.EMPTY_CACHE` (backend) y las claves de detalle `keys`, `ever_run`, `age_s` y `written` de la consola. | Son vocabulario del cable: las muestras de las sondas quitadas viven 48 h (`app/worker.py:148`, `RETENTION_HOURS`) y la página las sigue diciendo en palabras; borrarlas no compra nada. | Tasks 5, 8 |
| P4-22 | Las filas de `platform_health_sample` y `platform_probe_notice` de las sondas quitadas no se borran. | Las muestras caducan a las 48 h; un aviso guarda el último estado de una sonda que ya no corre y no vuelve a publicar nada. | Task 5 |
| P4-23 | `--skip-keycloak` de `restore-drill.sh` se sigue aceptando, sin efecto, con una línea que lo dice. | Un temporizador o un cron que lo pase no debe fallar por una opción que ya no tiene nada que saltar; `install-backup-timer.sh` no la usa. | Task 6 |
| P4-24 | `multi_tenant` se va con los `oidc_*`. | Solo lo leía `security.validate_token`; el despliegue es de una organización desde la fase 1 (`default_org_slug`). | Task 4 |
| P4-25 | Una prueba de una pieza que se borra se va con ella, nombrada en el Apéndice C; lo que protegía y sigue siendo cierto de algo que queda, lo prueba otra prueba nombrada en la misma fila. | Global Constraints, «Pruebas». | Apéndice C |
| P4-26 | Las siete pruebas de la sección «The email-domain allowlist» del runbook que vivían en `test_console_auth_realm.py` se mudan, sin cambiar, a `tests/test_email_domain_runbook.py`. | La sección y `ITS_CONSOLE_EMAIL_DOMAIN` siguen; el archivo que las alojaba se va con el realm. | Task 6 |
| P4-27 | El beat del reenvío se quita del calendario y nada más. | Celery 5.6.3, instalado: el despliegue corre `PersistentScheduler` (`--schedule=/var/lib/its/beat/celerybeat-schedule`, `deploy/docker-compose.yml:267`), cuyo `setup_schedule` (`celery/beat.py:531`) llama `merge_inplace` (`:558`): «Remove items from disk not in the schedule anymore» [C1]. Un mensaje `app.worker.keycloak_event_sweep` que quedara en la cola al actualizar: «Received unregistered task of type %s. The message has been ignored and discarded.» (`celery/worker/consumer/consumer.py:71-73`); `on_unknown_task` (`:596-628`) lo rechaza con `reject_log_error`, es decir `reject(requeue=False)` de kombu, sin volver a entregarlo, y marca su resultado como fallo `NotRegistered`. Queda una línea ERROR y un resultado fallido que caduca con `result_expires`. Queda también la clave de Redis `platform:keycloak_events:last_sweep` (`keycloak_events/sweep.py:288`, `:310`; sin TTL), con recuentos y nada más. | Task 5 |
| P4-28 | `deploy/GO-LIVE.md` §2 se reescribe entero. | Describía el primer arranque de Keycloak (correo de alta, cuenta bootstrap, consola de administración) aun antes de esta fase; el ledger lo apuntó en la fase 2 («banners only; project 4 rewrite»), y sin Keycloak no queda ninguna frase verdadera. | Task 9 |
| P4-29 | Los riesgos residuales 7 y 8 del runbook (el registro anónimo de Keycloak y los nombres de realm en minúsculas) se borran y los siguientes se renumeran. | Describen a Keycloak; la Task 9 da la frase de cabecera nueva entera. | Task 9 |
| P4-30 | La página de estado de la consola deja de tener el nombre de sonda `jwks` y los glifos `keycloak`, `keycloak_events` y `jwks`; `identity` se queda. | `identity` no nombra a Keycloak y es un nombre que el backend pudo registrar; los otros tres son de piezas que se van. | Task 8 |
| P4-31 | El aviso de `deploy.sh` por `ITS_PUBLIC_CONSOLE_URL` vacío deja de decir que los correos irán a `http://keycloak:8080`. | Desde la fase 1 la plataforma envía su propio correo y, sin esa dirección, no envía el enlace (README, «Status of this runbook»). | Task 6 |
| P4-32 | La sonda `jwks` la quita la Task 5, no la 4, y la 5 se fusiona primero. | La 4 quita `app.state.jwks`; `probe_jwks` lee `state.jwks` sin respaldo (`app/core/health/probes.py:116-122`), así que sola leería `down`. Las dos tocan los mismos archivos de sondas si la sonda fuera de la 4. | Tasks 4, 5 |
| P4-33 | La Task 5 no toca `tests/test_config.py`. | La 4 lo reescribe en la misma oleada; que no quede ningún ajuste `keycloak_*` lo fija el falsificador 11 y, para el ciclo de vida, `test_the_lifecycle_needs_no_identity_provider_configuration`. | Tasks 5, 11 |
| P4-34 | Las ediciones mecánicas van en guiones que el plan da enteros, y cada edición comprueba que su ancla aparece una sola vez. | 71 archivos de pruebas, 24 renombres, y los guiones del despliegue: a mano, una ancla mal copiada deja un archivo a medias. Los guiones se ensayaron tal como están sobre `dd062f8` (Apéndice E, «Comprobado»). Viven fuera del repositorio. | Tasks 4–8 |
| P4-35 | El runbook pierde lo que es de Keycloak, no se reescribe entero. | La reescritura para el modelo de sesión propia es del proyecto 4 (el propio runbook, «Status of this runbook», y el diseño §9). La sección de la lista de dominios conserva su explicación de «claim» y «stored row», que fijan siete pruebas. | Task 9 |
| P4-36 | La Task 7 trae una comprobación en vivo, fuera del repositorio, contra uvicorn con TLS. | La CI es la prueba de verdad, pero un guion de 800 líneas que solo corre en la CI se depura empujando; con el driver se depura en la worktree. Solo simula las dos páginas que sirve `web`. | Task 7 |
| P4-37 | `deploy.sh` nombra las claves `.env` obsoletas y dice que **no** se borren hasta el paso «Retiring the old identity provider's data»; `.env.example` no trae ninguna, ningún archivo de `deploy/` las nombra fuera de `deploy.sh` y del runbook, y unas pruebas lo fijan (`test_obsolete_env_keys_are_named_and_never_edited`, `test_no_other_deploy_file_names_an_obsolete_key`). | Punto 6 del despliegue; decisión del operador K1; el guion nunca edita `.env` (su cabecera). | Task 6 |
| P4-38 | `preflight.sh` comprueba el certificado contra el host de `ITS_PUBLIC_CONSOLE_URL`, no de `ITS_OIDC_ISSUER`, y un valor vacío da un host vacío (`sed -n …p`). | Un `.env` nuevo ya no trae `ITS_OIDC_ISSUER`, así que la comprobación negaba toda instalación nueva (revisión 3, I1); `ITS_PUBLIC_CONSOLE_URL` es la dirección del certificado y el README la fija en su paso 1. | Task 6 |

**Decisiones del coordinador** (2026-11-15, vinculantes; resuelven las decisiones disfrazadas de la revisión previa):

| # | Decisión | Dónde |
|---|---|---|
| C1 | **Nada de `--remove-orphans`.** `deploy.sh` quita exactamente los contenedores `keycloak` y `keycloak-init`, por su nombre de servicio y de forma idempotente, en la actualización, para que Traefik deje de enrutarles (sus etiquetas viven en el contenedor) y se cierre el registro anónimo de Keycloak. No guardan datos: los de Keycloak están en Postgres y su imagen no declara volumen. Nada más que el operador haya añadido al proyecto se toca. Lo prueban `test_the_old_identity_providers_containers_go_by_name_and_nothing_else`, `test_removing_them_twice_is_a_no_op`, `test_they_go_after_the_new_app_tier_is_up` y `test_no_up_removes_orphans`. | Tasks 6, 9 |
| C2 | **La primera actualización, y el host a medias.** El apartado «Upgrading» del runbook dice `git pull` primero y después `./scripts/deploy.sh`, con el porqué (bash conserva en memoria el texto viejo del guion). El `deploy.sh` nuevo no responde «Already deployed» con salida 0 mientras exista un contenedor `keycloak` o `keycloak-init`: termina la actualización como un despliegue completo (`stop_if_already_deployed`; `test_a_half_done_upgrade_is_finished_not_called_already_deployed`), y nombra como vuelta atrás la copia que tomó la ejecución parada, la más nueva por debajo de 0248, con la etiqueta de su `image_version` (re-revisión, N2). | Tasks 6, 9 |
| C3 | **E1, el botón del portal (P4-02):** se queda. | Task 2 |
| C4 | **E1, un 401 de `logout-everywhere` (P4-03):** cuenta como sesión cerrada y va al inicio de sesión; cualquier otro error se queda con su motivo; probados los dos. | Task 2 |
| C5 | **E1, el motivo de la concesión (P4-01):** se queda `signed_out_everywhere`; el evento de auditoría distingue los dos actos por acción y actor, y se dice donde se documenta el motivo. | Task 1 |
| C6 | **Falsificador 11:** se queda, como enmienda fechada del diseño §10, y su prueba discrimina: con la JWKS de la clave ofrecida a toda petición saliente, un validador que rechace una clave desconocida con un 401 no la pasa (revisión 1, M13). | Task 11 |
| C7 | **El simulacro:** documenta que una vuelta atrás pasada la fase 4 es solo con la copia previa a la actualización; el simulacro restaura las copias de ahora. | Tasks 6, 9 |

## Apéndice C — Las pruebas, una a una

**Se borran porque la pieza que prueban se borra.** Cada fila: el archivo, sus pruebas, la pieza y qué sigue protegiendo lo que protegían.

| Archivo (pruebas) | Pieza que se va | Lo que protegían, hoy |
|---|---|---|
| `tests/test_security.py` (17): `test_valid_token_yields_claims`, `test_expired_token_rejected`, `test_wrong_audience_rejected`, `test_wrong_issuer_rejected`, `test_garbage_token_rejected`, `test_signature_from_unknown_key_rejected`, `test_kid_rotation_refetches_jwks`, `test_unknown_kid_flood_is_rate_limited`, `test_failed_refetch_keeps_existing_keys`, `test_missing_org_claim_uses_default_slug`, `test_missing_org_claim_without_default_rejected`, `test_multi_tenant_rejects_missing_org_claim_even_with_default`, `test_multi_tenant_accepts_token_with_org_claim`, `test_an_mcp_audience_token_is_refused_on_the_default_audience`, `test_an_mcp_audience_token_passes_with_the_audience_override`, `test_a_list_audience_is_kept_as_a_tuple`, `test_a_console_token_is_refused_on_the_mcp_audience` | `security.validate_token`, `JwksCache`, `TokenClaims` | Lo que verifica el token propio: `tests/test_oauth_access_token.py` (firma, `iss`, `aud`, `kid`, caducidad, `typ`, un `kid` desconocido sin leer OpenBao en cada petición, D14 de la fase 3). Lo que ninguna puerta acepta otro token: las pruebas de puerta con `ForeignIssuer` (abajo) y el falsificador 11. |
| `tests/test_token_origin.py`, 10 de 26: `test_the_console_client_id_defaults_to_the_spa_client`, `test_the_console_client_id_is_read_from_the_environment`, `test_a_token_that_is_not_an_access_token_is_refused`, `test_a_token_with_no_typ_is_refused`, `test_an_id_token_carrying_the_console_audience_is_refused`, `test_a_bearer_token_is_admitted_and_carries_its_origin`, `test_require_person_mcp_token`, `test_the_legacy_client_id_claim_marks_a_service_account`, `test_the_sweep_catches_a_validation_with_no_origin_rule`, `test_every_validate_token_call_in_app_is_followed_by_an_origin_rule` (y los ayudantes `_settings`, `_claims`) | `validate_token`, `require_person_mcp_token`, `oidc_console_client_id` | **Las otras 16 se quedan** (cada puerta rechaza los tokens de Keycloak de cada forma: consola, ID token, otro cliente, sin persona, socket, `/mcp`; las del túnel mandan cookies, porque el túnel no lee bearer desde la fase 1), con `ForeignIssuer`. El barrido AST de `validate_token` se reemplaza por `test_no_door_can_validate_a_keycloak_token_any_more` y su prueba de no vacuidad (Task 4). |
| `tests/test_user_admin_client.py` (21), `tests/test_realm_config.py` (34) | `user_admin/kc_client.py`, `user_admin/realm_config.py` | Que el ciclo de vida no llama a ningún proveedor: `OutboundRecorder` en `test_user_admin_api.py` y `test_setup_api.py` (`.outbound.seen == []`). Ajustes → Correo: `tests/test_realm_settings_api.py` y `test_mail*.py`, sin cambios. |
| `tests/test_me_locale.py`, 9 de 17: `test_the_attribute_update_is_a_get_put_get_with_only_the_profile_fields`, `test_a_concurrent_disable_is_not_undone`, `test_a_null_locale_removes_the_attribute_and_keeps_the_rest`, `test_the_target_is_the_subject_given`, `test_a_write_keycloak_accepted_but_did_not_keep_raises`, `test_clearing_is_verified_too`, `test_a_user_with_no_attributes_map_gets_one`, `test_a_keycloak_failure_raises_and_carries_no_secret`, `test_set_locale_attribute_raises_on_a_non_object_user`; y `test_a_broken_provisioning_setting_cannot_touch_the_save` | `kc_client.set_locale_attribute`; `KeycloakAdminConfig.from_settings`, que la última rompía a propósito | Las siete de la ruta se quedan (`PATCH /me` guarda el idioma y no llama a nada: `env.outbound.seen == []`). La última probaba que un ajuste de aprovisionamiento roto no afectaba al guardado; ya no hay ajuste. |
| `tests/test_mcp_admin_api.py`, 6 de 38: `test_revoke_consent_deletes_the_consent_and_quotes_the_client_id`, `test_revoke_consent_treats_404_as_done`, `test_revoke_consent_raises_on_any_other_refusal`, `test_revoke_consent_refuses_an_id_that_is_not_one_segment_before_sending`, `test_every_user_operation_refuses_a_user_id_that_is_not_one_segment`, `test_the_sent_path_must_end_with_exactly_the_quoted_segments` | `kc_client.revoke_consent` y los demás de `kc_client` | Las 32 de revocar se quedan, con el registrador de salidas (`env.outbound.seen == []`): revocar no llama a nadie. |
| `tests/test_keycloak_event_client.py` (7), `tests/test_keycloak_event_mapping.py` (11), `tests/test_keycloak_event_sweep.py` (15) | `app/modules/keycloak_events/` | Nada que proteger queda: la plataforma registra ella misma los cambios de factores y los enlaces de Microsoft en la cadena desde las fases 1 y 2 (`auth.factor_added`, `auth.factor_removed`, `auth.microsoft_*`; `tests/test_auth_*`). |
| `tests/test_i18n_modules_rest.py::test_keycloak_events_nothing_it_forwards_or_reports_is_translated` | `keycloak_events.client`, `.mapping` | — (el módulo no existe) |
| `tests/test_platform_health_probes.py`, 6 de 20: `test_the_jwks_probe_performs_no_network_io`, `test_an_empty_jwks_cache_is_degraded_not_down`, `test_an_unconfigured_deployment_is_ok_and_says_so`, `test_a_configured_deployment_that_has_never_swept_is_degraded`, `test_a_recent_sweep_is_ok_and_a_stale_one_is_degraded`, `test_a_configured_secret_that_has_gone_missing_is_down_not_unconfigured` | `probe_jwks`, `probe_keycloak_events` | «Configurado: no» visible y no alarmante: las sondas de respaldo, que heredaron ese precedente (`tests/test_platform_backup_status.py`). `test_the_status_route_prints_a_label_and_carries_the_key_separately` se queda, re-apuntada a `backup.nightly`. |
| `tests/test_platform_health_sampler.py`, 2 de 4: `test_the_sampler_writes_no_jwks_row`, `test_jwks_is_excluded_because_a_worker_reading_would_be_a_false_down` | la exclusión de `jwks` de `WORKER_SAMPLED_PROBES` | `test_one_row_per_probe_is_written` se queda: 10 filas (eran 11, las doce sondas menos `jwks`; ahora diez, todas) y gana la igualdad de conjuntos que tenía `test_the_sampler_writes_no_jwks_row`. |
| `tests/test_keycloak_init_job.py` (115), `tests/test_keycloak_init_transport.py` (18) | `deploy/keycloak/init-clients.py` y el servicio `keycloak-init` | — (el guion no existe). Lo que de ellos tocaba `.env.example` (rutas de secretos que algo escribe) lo sustituye la guarda de claves obsoletas (Task 6). |
| `tests/test_keycloak_image_pin.py` (2) | la imagen `quay.io/keycloak/keycloak:26.2` | Que ninguna imagen de Keycloak vuelva: el falsificador 11. |
| `tests/test_console_auth_realm.py`, 100 de 107 (todas menos las siete que se mudan): 4 en la Task 5, las que leían `kc_client.py` (`test_the_env_example_ships_a_secret_path_something_actually_writes`, `test_every_document_that_counts_the_admin_operations_counts_the_same`, `test_the_action_sets_are_read_from_both_sides_and_are_not_empty`, `test_every_credential_step_itms_asks_for_exists_in_the_realm`), y 96 con el archivo en la Task 6 | el realm, el tema, `kc_client`, las secciones del runbook de Keycloak | Las **siete** de la lista de dominios se mudan a `tests/test_email_domain_runbook.py`. La mitad de `test_the_console_no_longer_asks_the_realm_to_sign_it_out` que leía la consola (ninguna fuente pide un cierre de sesión al realm) pasa al falsificador 11, ampliada a cualquier ruta de OIDC. |
| `tests/test_deploy_keycloak_registration_edge.py` (8) | los routers `kc`, `kc-registration-oidc`, `kc-registration-closed` | Su modelo de routers se muda a `tests/traefik_routes.py`; que ninguna ruta llega a un proveedor de identidad: `test_public_surface.py`, reescrito (Task 6), y el falsificador 11. |
| `tests/test_public_surface.py`, 4 de 5: `test_sign_in_is_still_routed`, `test_the_admin_console_is_not_publicly_routed`, `test_the_realm_file_is_where_this_test_thinks_it_is`, `test_the_account_console_is_closed_because_routing_cannot_close_it` | el router `kc`, el realm | Reescrito: ningún router lleva a Keycloak, las rutas que eran suyas caen en la consola, y no queda ni servicio ni imagen ni etiqueta suya (Task 6). |
| `tests/test_deploy_mcp_route.py::test_the_realm_keeps_its_own_discovery_documents` | el router `kc` | Re-apuntada: `test_the_former_realm_paths_reach_no_identity_provider` (Task 6). |
| `tests/test_deploy_script_steps.py`, 5 de 21: `test_a_theme_change_is_detected_at_all`, `test_a_realm_or_theme_change_restarts_keycloak_not_only_keycloak_init`, `test_keycloak_init_runs_on_every_deploy_not_only_on_a_realm_change`, `test_a_keycloak_image_change_does_need_the_restore` y `test_a_compose_or_theme_change_is_undone_from_git_not_by_restoring_the_database` | los pasos de Keycloak de `deploy.sh` y su ayuda de vuelta atrás | Nuevas (Task 6): `test_no_step_starts_an_identity_provider`, `test_no_up_removes_orphans`, `test_the_old_identity_providers_containers_go_by_name_and_nothing_else`, `test_removing_them_twice_is_a_no_op`, `test_they_go_after_the_new_app_tier_is_up`, `test_a_half_done_upgrade_is_finished_not_called_already_deployed`, `test_the_way_back_from_a_half_done_upgrade_is_the_backup_from_before_0248`, `test_without_a_backup_from_before_0248_it_says_so`, `test_the_verify_step_fails_while_a_container_of_it_is_left`, `test_the_check_runs_before_the_diff_is_taken`, `test_obsolete_env_keys_are_named_and_never_edited`, `test_no_other_deploy_file_names_an_obsolete_key`, `test_the_preflight_checks_the_certificate_against_the_console_address`, `test_a_compose_change_checks_the_whole_deploy_directory_out` y `test_a_rollback_past_phase_4_names_the_env_keys_it_needs`. |
| `tests/test_platform_backup_scripts.py::test_the_drill_runs_the_images_the_live_stack_pins` | su línea de `keycloak` | Se queda, sin esa línea, más una aserción de que el simulacro no define `keycloak` (Task 6). El mismo archivo gana siete pruebas de K1, que corren `backup.sh` y `restore.sh` con un `ITS_PG_EXEC` falso (Task 6; revisión 3, I3). |
| `tests/test_ci_mcp_flow.py` (13) | `deploy/keycloak/ci_mcp_flow.py` | Reescrita para `deploy/ci/mcp_flow.py` (Task 7, 19 pruebas): solo biblioteca estándar, PKCE, audiencia exacta, nada secreto impreso, TOTP contra los vectores de RFC 6238 y RFC 4226, la lista de rutas viejas no vacía, y el job `deploy` de la CI leído como YAML: sus dos fases, ningún secreto del almacén en un argv, y el paso de K1. |
| `tests/test_mcp_grant_lifecycle.py::test_an_administrator_ending_sessions_leaves_the_ai_connections` | ninguna: **se invierte** por E1 | `test_an_administrators_sign_out_everywhere_ends_the_ai_connections` (Task 1). |

**Se quedan y se re-apuntan** (Task 4): `test_auth_session_door.py` (2), `test_auth_falsifiers.py` (1), `test_ui_ws.py` (1), `test_auth_my_security.py` (1), `test_mcp_channel_api.py` (1), `test_mcp_server.py` (2), `test_auth_csrf.py` (1), `test_mcp_own_tokens.py` (2), y 16 de `test_token_origin.py` (consola, ID token, otro cliente, sin persona, el socket y `/mcp`; el túnel no lee ningún bearer desde la fase 1 y sus pruebas mandan cookies): `api_env.idp` pasa a `api_env.foreign` y nada más. `test_mcp_server.py::test_a_token_without_an_org_claim_is_401` pierde su bucle sobre `multi_tenant`. `test_config.py` se reescribe (Task 4). Los 71 archivos que construyen `Settings(...)` con `oidc_issuer=` y `oidc_jwks_url=` pierden esas dos líneas (Task 4, con un guion; 156 líneas en `dd062f8`, las de `test_platform_health_sampler.py` incluidas).

## Apéndice D — Inventario de Keycloak, y lo que le pasa a cada pieza

Punto de partida: `grep -rIl -i keycloak --exclude-dir=node_modules --exclude-dir=.venv --exclude-dir=.claude .` en `dd062f8`: 175 archivos; más `grep -rn -E 'oidc_|jwks|fake_idp|KC_|ENTRA_|multi_tenant'`. «Se va» = se borra; «Reescrito» = sigue con otro contenido; «Hasta K1» = se queda hasta que el operador borre la base; «Historia» = registro fechado que no se edita.

| Pieza (archivo:línea en `dd062f8`) | Qué le pasa | Tarea |
|---|---|---|
| `deploy/docker-compose.yml:575-716` servicio `keycloak` (imagen, `KC_*`, montajes, routers `kc`, `kc-registration-oidc`, `kc-registration-closed`, middleware `kc-registration-deny`) | Se va | 6 |
| `deploy/docker-compose.yml:718-795` servicio `keycloak-init` | Se va | 6 |
| `deploy/docker-compose.yml:168-169` `api.depends_on.keycloak` | Se va | 6 |
| `deploy/docker-compose.yml:125-145` comentario de la regla del router `api` (realm, «`kc`») | Reescrito | 6 |
| `deploy/docker-compose.yml:454-458` comentario de Postgres («OIDC config») | Reescrito | 6 |
| `deploy/keycloak/init-clients.py`, `realm-it-suite.json`, `themes/itms/**` | Se van | 6 |
| `deploy/keycloak/ci_mcp_flow.py` | Se va; lo reemplaza `deploy/ci/mcp_flow.py` | 7 |
| `deploy/postgres/init-app-user.sh:2`, `:67` (`CREATE DATABASE keycloak`) | Se va (solo afecta a un directorio de datos nuevo) | 6 |
| `deploy/scripts/backup.sh:8`, `:92-93`, `:130` | Reescrito: vuelca `keycloak` solo con `ITS_BACKUP_OLD_IDENTITY_DB=1` (la copia previa de `deploy.sh`) y mientras exista; sin ella, una nota | 6 |
| `deploy/scripts/scheduled-backup.sh:222-223` | Pone `ITS_BACKUP_OLD_IDENTITY_DB=0` (K1) | 6 |
| `deploy/scripts/restore.sh:179-184`, `:222-235` | Comentarios reescritos; la restauración condicional se queda y lo dice | 6 |
| `deploy/scripts/restore-drill.sh:8`, `:13-14`, `:29-35`, `:104`, `:162-181` | Reescrito: `keycloak.dump` opcional, sin arrancar Keycloak; comprueba la base restaurada si la hay | 6 |
| `deploy/drill/docker-compose.drill.yml:29-40` | Se va el servicio | 6 |
| `deploy/scripts/deploy.sh:1-40` (cabecera), `:167-169`, `:209-214`, `:229-292`, `:322`, `:357-377`, `:423-444`, `:463` | Reescrito (P4-13, P4-31, C1, C2) | 6 |
| `deploy/scripts/preflight.sh:149-155` (lee `ITS_OIDC_ISSUER`), `:183` | Reescrito: `ITS_PUBLIC_CONSOLE_URL` (P4-38); «§2 (the first administrator)» | 6 |
| `deploy/scripts/host-check.sh:120`, `:171` («the console is OIDC») | Reescrito | 6 |
| `deploy/host-network.md:34` (`ITS_OIDC_ISSUER`), `:302` («OIDC token expiry») | Reescrito | 6 |
| `deploy/scripts/build-images.sh`, `install-backup-timer.sh` | Sin cambios (no nombran a Keycloak) | — |
| `deploy/.env.example:41-49` (`ITS_MULTI_TENANT`), `:82-213` (Keycloak/OIDC, Entra, cliente de eventos y de aprovisionamiento), `:350` («the same one in ITS_OIDC_ISSUER»), `:410-414` (`KC_HOSTNAME`), `:466-468` (`keycloak-init` y `ITS_MCP_REGISTRATION`) | Se van, o `:350` reescrito; `ITS_DEFAULT_ORG_SLUG` se queda en un bloque propio | 6 |
| Secretos `keycloak-*.secret` en `its-secrets` (escritos por `init-clients.py:106`, `:111`, `:119`), las cuatro claves `.env` de la vuelta atrás y las demás obsoletas | Hasta el paso «Retiring the old identity provider's data» (K1, K2) | 9 |
| La base `keycloak` de un Postgres existente | Hasta ese mismo paso (K1); solo la copia previa de `deploy.sh` la guarda | 6, 9 |
| `deploy/README.md` (155 líneas): «Status» `:29-37`, `:66-68`; Step 1 `:236-237`; Step 2 `:324`; 3.6 `:533-538`; Step 7 `:742`; Steps 8–9 `:803-926`; «Break-glass: the identity admin console» `:1126-1261`; «Branding and extending sign-in» `:1262-1512`; Microsoft `:1517`; Idioma `:1630-1636`; «Forwarding Keycloak's credential events» `:1657-1786`; «Keycloak SMTP» `:1787-1961`; «The provisioning client» `:2077-2193`; «What keycloak-init still converges» `:2442-2476`; Traefik `:2488-2497`; riesgos 7–8 `:2658-2667`; Upgrading `:3220-3223`, `:3274-3280`; respaldos `:3336-3338`, `:3416-3423`; «What was broken» `:3582-3588` | Reescrito o se va, sección por sección (Task 9); la tabla «What was broken» queda como historia con una nota | 9 |
| `deploy/GO-LIVE.md:30`, `§2` (`:59-208`), `:435`, `:499-500`, `:513`, `:519` | Reescrito | 9 |
| `.github/workflows/ci.yml:568-738` (los dos pasos de Keycloak del job `deploy`) | Reescrito contra el servidor propio | 7 |
| `backend/app/core/security.py` (entero) | Se va | 4 |
| `backend/app/api/deps.py:52`, `:129-131` (`jwks`) | Se va; los comentarios `:274`, `:363` son historia | 4 |
| `backend/app/main.py:12` (`httpx`), `:23`, `:159-163`, `:207` | Se va; `create_app(settings)` | 4 |
| `backend/app/core/config.py:32-56` (`oidc_*`, `multi_tenant`) | Se va; `default_org_slug` con comentario nuevo | 4 |
| `backend/app/core/config.py:82-113` (`keycloak_*`), `:166-168` | Se va | 5 |
| `backend/app/api/v1/orgs.py:74`, `:81`, `:272-274` | Reescrito (P4-08) | 4 |
| `backend/app/core/i18n/es.py:159-168`, `:5233-5238` | Se van (7 entradas) | 4 |
| `backend/app/core/i18n/es.py:3756`, `:3961` y las tres de `realm_config.py` | Se van (5 entradas) | 5 |
| `backend/app/core/auth/oauth/access.py:30-33` | Reescrito (nombra un archivo que se va) | 4 |
| `backend/app/modules/user_admin/kc_client.py`, `realm_config.py` | Se van | 5 |
| `backend/app/modules/user_admin/__init__.py:8-11` | Reescrito | 5 |
| `backend/app/modules/keycloak_events/` (4 archivos) | Se va | 5 |
| `backend/app/worker.py:92-96`, `:2490-2530`, `:2700-2731` (docstring de `sample_health_once`), `:3399-3408`, `:3509-3511` | Se va / reescrito | 5 |
| `backend/app/api/probes.py:24-34`, `:49-79`, `:81-115` | Reescrito sin `jwks` ni `keycloak_events` | 5 |
| `backend/app/core/health/probes.py:116-122`, `:140-198` | Se van | 5 |
| `backend/app/core/health/backup_status.py:12` | Reescrito | 5 |
| `backend/app/api/v1/platform.py:49` | Historia (explica por qué hay `name` y `key`) | — |
| `backend/app/models/core.py:74`, `:870-890`; `backend/app/models/__init__.py:42`, `:335` | Se va el modelo; `:74` reescrito | 5 |
| `backend/app/core/client_scope_catalog.py:44`; `backend/app/core/client_scope.py:305-307` | Se van | 5 |
| `backend/app/core/mail.py:291` | Reescrito (nombraba un archivo que se va); `:259` historia | 5 |
| `backend/migrations/versions/0064_*.py`, `0245_auth_core.py:57-58` | Historia (no se editan) | — |
| Nueva `backend/migrations/versions/0248_keycloak_event_ledger_dropped.py` | Se crea | 5 |
| `backend/app/core/auth/__init__.py:4`, `loopback.py:6`, `oauth/policy.py:12`, `identity/__init__.py:117`, `identity/policy.py:152`, `identity/own_profile.py:221`, `api/v1/ws.py:35`, `api/v1/me.py:38`, `modules/remote/tunnel.py:8`, `modules/user_admin/service.py:29` | Historia (frases en pasado) | — |
| `backend/tests/fake_idp.py`; `conftest.py:244-247`, `:326-338`, `:371`, `:378` | Reescrito como `tests/foreign_tokens.py`; `ApiEnv.foreign` | 4 |
| Pruebas: Apéndice C | Según el Apéndice C | 4, 5, 6, 7 |
| `frontend/src/modules/platform-status/PlatformStatusPage.tsx:41-49`, `:150-152`, `:555`; `types.ts:32-34`, `:45`; `i18n/{en,es}/enums.ts` (`jwks`, el comentario de `keycloak_events`); sus dos pruebas | Reescrito | 8 |
| `frontend/src/modules/audit/auditVocabulary.coverage.test.ts:244-248`, `:502` | Se va la entrada de `keycloak_events` (P4-11) | 5 |
| `frontend/src/lib/oidcLeftovers.ts` | Se queda (P4-16) | — |
| `frontend/src/lib/noVendorNames.test.ts`, `backend/tests/test_no_vendor_names_backend.py` (las guardas) | Se quedan; el docstring de una prueba de sondas se corrige | 5 |
| Comentarios de la consola en pasado (`main.tsx:7`, `shell/AccountMenu.tsx:12`, `lib/ws.ts`, `modules/remote/tunnel.ts:9`, `i18n/test-utils.tsx:18`, y los de pruebas) | Historia | — |
| `frontend/src/preview/PreviewPage.tsx:4` («Every screen in this console is behind Keycloak», en presente) | Reescrito | 8 |
| `docs/01-architecture.md:17`, `:56`, `:169`, `:226`; `02-tech-stack.md:68`, `:118`; `04-data-model.md:62-65`; `06-roadmap.md:30`; `08-observability.md:153`, `:206-208`; `product.md:180`, `:199`, `:252`; `kernel/spec.md:23`, `:84`, `:178-186`, `:314`, `:327`; `modules/ai-admin.md:106`; `DESIGN.md:248`; `HANDOFF.md:401`, `:444` | Reescrito con fecha, o nota fechada (Task 10 dice cuál) | 10 |
| `docs/03-security-model.md:203-209` | Reescrito (E2) | 3 |
| `docs/07-backup-restore.md:21`, `:33`, `:39`, `:65`, `:156`, `:162`, `:387-388` | Reescrito | 9 |
| `docs/modules/console-auth/spec.md:18`, `:110`; `docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` (cabecera); `2026-10-09-mcp-server-design.md:74`; el diseño de esta fase §8, §9 | Nota fechada | 10 |
| `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §5.3 | Enmiendas E1 y E2 | 1, 3 |
| `docs/superpowers/plans/2026-11-06-autenticacion-propia-fase3.md` Task 10, decisión 3 | Enmienda E1 | 1 |
| El resto de `docs/superpowers/plans/`, `docs/superpowers/audits/`, `docs/reviews/`, y los diseños fechados que citan a Keycloak (`2026-08-14-logging-kernel`, `2026-08-15-deploy-rollback`, `2026-08-19-rbac-and-user-lifecycle`, `2026-08-20-ux-remediation-program`, `2026-09-10-*`, `2026-09-11-mail-oauth`, `2026-10-09-spanish-language`, `2026-10-23-msp-clientes`, `2026-11-04-autenticacion-propia-fuentes`), `docs/modules/mdm/spec.md:720`, `docs/modules/people/spec.md:297`, `docs/design/settings-language.md`, `docs/design/itms-prototype.html` | Historia: registros fechados de lo que se decidió o se construyó entonces; se dice en la Task 10 y no se editan | — |
| `CLAUDE.md:396` | Se queda (P4-14) | — |

## Apéndice E — Fuentes leídas por este plan (2026-11-15)

Cada cita está copiada de la página descargada ese día con `curl` y convertida a texto, o del código fuente instalado o publicado que se nombra.

- **[D1]** Docker, `docker compose up`. https://docs.docker.com/reference/cli/docker/compose/up/ — «`--remove-orphans` Remove containers for services not defined in the Compose file». También `docker compose down`, https://docs.docker.com/reference/cli/docker/compose/down/ — la misma opción, y «Networks and volumes defined as external are never removed».
- **[D2]** Docker, variables de entorno de Compose. https://docs.docker.com/compose/how-tos/environment-variables/envvars/ — `COMPOSE_REMOVE_ORPHANS`: «When enabled, Compose automatically removes orphaned containers when updating a service or stack. Orphaned containers are those that were created by a previous configuration but are no longer defined in the current compose.yaml file. … false or 0, to disable automatic removal. Compose displays a warning about orphaned containers instead. Defaults to: 0».
- **[D3]** Traefik, proveedor Docker (la página de v3, a la que redirige la antigua). https://doc.traefik.io/traefik/reference/install-configuration/providers/docker/ — `providers.docker.exposedByDefault`: «If set to false, containers that do not have a traefik.enable=true label are ignored from the resulting routing configuration»; «By default, Traefik watches for container level labels on a standalone Docker Engine»; y `allowEmptyServices` (por omisión `false`): «Instructs the provider to create any servers load balancer defined for Docker containers regardless of the healthiness of the corresponding containers» (por eso el paso de la CI espera a `web` además de a la API). Que eso alcance a un contenedor huérfano de compose es una deducción de esas frases, *SIN VERIFICAR en vivo*; por eso `deploy.sh` quita los dos contenedores por su nombre (C1).
- **[K1]** Keycloak, «Configuring the database» (la guía actual, 26.6.4; el stack fija 26.2). https://www.keycloak.org/server/db — «Configure a relational database for Keycloak to store user, client, and realm data»; «Keycloak stores sensitive information in the database. This includes hashed user passwords, client credentials and realm keys that are used to sign tokens».
- **[K2]** Keycloak, «Running Keycloak in a container». https://www.keycloak.org/server/containers — «The Keycloak containers have a directory /opt/keycloak/data/import. If you put one or more import files in that directory via a volume mount or other means and add the startup argument --import-realm, the Keycloak container will import that data on startup!».
- **[P1]** PostgreSQL 16, `DROP DATABASE`. https://www.postgresql.org/docs/16/sql-dropdatabase.html — «It cannot be executed while you are connected to the target database»; «DROP DATABASE cannot be executed inside a transaction block». Ninguna de las dos impide una migración: Alembic se conecta a `itsuite`, no a `keycloak`, y `MigrationContext.autocommit_block()` (Alembic 1.20.1, instalado, `alembic/runtime/migration.py:274-283`) existe «to support the occasional database DDL or system operation that specifically has to be run outside of any kind of transaction block». La opción D era posible y desaconsejable (B.1).
- **[A1]** Alembic, `autocommit_block()`. https://alembic.sqlalchemy.org/en/latest/api/runtime.html — «Enter an "autocommit" block, for databases that support AUTOCOMMIT isolation levels».
- **[B1]** OpenBao, `bao operator init`. https://openbao.org/docs/commands/operator/init/ — `-format`: «Valid formats are "table", "json", or "yaml"»; `-key-shares`, `-key-threshold`. Los nombres de los campos JSON, del código de la versión fijada: https://raw.githubusercontent.com/openbao/openbao/v2.6.1/command/operator_init.go, `:360` `json:"unseal_keys_b64"` y `:368` `json:"root_token"`.
- **[B3]** OpenBao 2.6.1, `bao status`: https://raw.githubusercontent.com/openbao/openbao/v2.6.1/command/status.go — «The exit code reflects the seal status: - 0 - unsealed - 1 - error - 2 - sealed». Y `bao write … key=-`: el valor `-` se lee entero de la entrada estándar, sin quitarle nada (`go-secure-stdlib/kv-builder/builder.go`, la rama `value == "-"`: `io.Copy` a un `bytes.Buffer`, `buf.String()`); `sys/unseal` lo decodifica tal cual (`http/sys_seal.go:117-122`, sin `TrimSpace`), por eso el paso de la CI lo manda con `printf '%s'`, sin salto de línea.
- **[B2]** OpenBao, `bao token create`. https://openbao.org/docs/commands/token/create/ — `-field`: «Print only the field with the given name»; `-orphan`, `-policy`.
- **[M1]** MCP 2026-07-28, Authorization. https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization.md — «MCP servers **MUST** implement OAuth 2.0 Protected Resource Metadata ([RFC9728])»; «MCP clients **MUST** support both discovery mechanisms»; «MCP client **MUST** use the Authorization request header field … `Authorization: Bearer <access-token>`»; «MCP servers **MUST** validate that access tokens were issued specifically for them as the intended audience»; «Invalid or expired tokens **MUST** receive a HTTP 401 response»; «MCP servers **MUST NOT** accept or transit any other tokens.»; y «Dynamic Client Registration is deprecated and retained for backwards compatibility» (P4-17: aquí se usa porque CIMD necesita una dirección pública).
- **[R1]** RFC 9700, §4.14.2. https://www.rfc-editor.org/rfc/rfc9700.txt — «The previous refresh token is invalidated, but information about the relationship is retained by the authorization server. If a refresh token is compromised and subsequently used by both the attacker and the legitimate client, one of them will present an invalidated refresh token, which will inform the authorization server of the breach.» E2 pone un límite de 30 días a esa retención; la Task 3 dice lo que cuesta.
- **[R2]** RFC 6238, §4. https://www.rfc-editor.org/rfc/rfc6238.txt — «TOTP = HOTP(K, T)»; «T = (Current Unix time - T0) / X, where the default floor function is used»; el vector del Apéndice B (SHA-1, T = 59 s, 8 cifras: `94287082`).
- **[R3]** RFC 4226, §5.3. https://www.rfc-editor.org/rfc/rfc4226.txt — «Let OffsetBits be the low-order 4 bits of String[19] … Return the Last 31 bits of P»; «Implementations MUST extract a 6-digit code at a minimum».
- **[C1]** Celery 5.6.3, instalado en `backend/.venv`: `celery/beat.py:458-472`, `merge_inplace` («Remove items from disk not in the schedule anymore»), llamado desde `PersistentScheduler.setup_schedule` (`:531`, la llamada en `:558`); `celery/worker/consumer/consumer.py:71-73` y `on_unknown_task` (`:596-628`). Y https://docs.celeryq.dev/en/stable/faq.html — «The worker rejects unknown tasks, messages with encoding errors and messages that don't contain the proper fields».
- **[L1]** Ensayo local del guion de la CI (`deploy/ci/mcp_flow.py`), el 2026-11-15, repetido en la ronda de correcciones: contra `uvicorn app.main:create_app --factory` con TLS, una base nueva del clúster de pruebas y los backends locales, con las dos páginas de `web` simuladas (Task 7, Step 6): las dos fases, 33 aserciones (7 y 26), salida 0. Lo que no cubre: Traefik, nginx y OpenBao.
- **[S1]** `shellcheck` 0.10.0 (la revisión previa lo instaló fuera del repositorio): sobre `backup.sh`, `restore.sh`, `restore-drill.sh`, `deploy.sh`, `preflight.sh`, `host-check.sh` y `scheduled-backup.sh` después de la Task 6, ningún aviso nuevo frente al árbol de antes (uno, `SC2088`, se corrigió en la ronda de correcciones).
- **[G1]** Experimento local (git 2.43.0, GNU bash 5.2.21): un guion que hace `git checkout` de otra versión de sí mismo sigue ejecutando su texto viejo. Guion: `/tmp/claude-0/-home-user-Novieri-RMM/21768af2-984e-5cf8-87b2-4dc8a41a4864/scratchpad/p2f4plan/bashswap/probe.sh`; salida: `version: OLD, before the pull` / `version: OLD, after the pull`.


**SIN VERIFICAR** (y por qué):
- Que Traefik siga enrutando a un contenedor huérfano de compose: se deduce de [D3]; no hay demonio de Docker en la sesión que escribió el plan. Que `deploy.sh` quite los dos contenedores lo hace irrelevante, y eso está probado con `docker` simulado, no contra un motor real: que `docker compose ps -a` liste un contenedor cuyo servicio el archivo ya no define viene de la ayuda de Compose v5.6.0 (`--orphans`, verdadero por omisión).
- Los dos pasos nuevos del job `deploy` de la CI enteros (OpenBao real, Traefik, `web`, la API en `production`; y la copia y restauración de una base `keycloak`): solo se comprobó que el YAML carga, que sus bloques pasan `bash -n` y que su guion pasa en vivo contra uvicorn ([L1]). Lo verifica el run del PR que fusiona las Tasks 6 y 7. Tampoco se midió cuánto tarda: `up -d --build api web traefik` construye MinIO desde su fuente en este job, como dependencia de `api`, sin la caché de gha que usa el job de copias (`ci.yml:306-313`), varios minutos de compilación de Go y red.
- `printf '%s' "$unseal" | … bao write sys/unseal key=-` y el token raíz por la entrada estándar de un `sh` dentro del contenedor: leídos en el código ([B3]), no ejecutados contra un OpenBao vivo.
- `bao operator init -format=json` y `bao status` contra un OpenBao 2.6.1 vivo: los nombres de campo salen de su código [B1]; que `bao status` responda 2 a un servidor sin inicializar (el bucle de espera del paso) se deduce de que uno sin inicializar está sellado.
- Que un mensaje `app.worker.keycloak_event_sweep` que quedara en la cola al actualizar se rechace una vez y nada más (P4-27).
- Un `./scripts/deploy.sh` real sobre un host con Keycloak (la retirada de sus contenedores, el final de una actualización a medias con la copia y la etiqueta de su vuelta atrás, el aviso de las claves, y la comprobación final por las etiquetas `com.docker.compose.project` y `com.docker.compose.service`, que Compose pone a cada contenedor y aquí solo se simularon), y una vuelta atrás de verdad a una versión de antes de la fase 4.
- Las páginas de Keycloak citadas ([K1], [K2]) son de su versión 26.6.4; el stack fijaba 26.2. Lo citado (dónde guarda los datos, la importación del realm) no cambia entre ellas, pero no se leyó la página de 26.2.

---

## Las tareas, una a una


### Task 1: E1 en el servidor — el «Cerrar sesión en todas partes» de un administrador termina las concesiones de IA

**Files:**
- Modify: `backend/app/modules/user_admin/service.py:1173-1199` (`end_sessions`) y el docstring del módulo (`:34-44`)
- Modify: `backend/app/mcp/descriptions/part_d.py:546-548`
- Modify: `backend/app/core/auth/guards.py:156-161` (docstring de `lock_user_row`, viñeta «THE ACTS THAT END A PERSON'S AI GRANTS»)
- Test: `backend/tests/test_mcp_grant_lifecycle.py` (docstring `:1-9`, import `:34`, la prueba de `:313-329`, una prueba nueva delante del bloque «# One clock» )
- Test: `backend/tests/test_user_lifecycle.py:644-646`
- Modify: `deploy/README.md` (`### Revoking`, viñeta «One person», `:2521-2531`; riesgo 6, `:2652-2656`)
- Modify: `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §5.3 (detrás de `:340`)
- Modify: `docs/superpowers/plans/2026-11-06-autenticacion-propia-fase3.md` (detrás del párrafo de la decisión 3 de la Task 10, `:11318-11327`)

**Interfaces:**
- Depende de: `ai_grants.revoke_all_for_user(session, user_id, reason) -> int` (`app/core/auth/oauth/grants.py`, fase 3, Task 10), ya importado en `service.py:59` como `ai_grants`; `end_all_for_user` (`app/core/auth/sessions.py`).
- Produce: el evento `auth.sessions_ended` del administrador con `{"by": "administrator", "scope": "everywhere", "ai_grants_revoked": int}`.

**Por qué este orden dentro del acto.** El patrón de los siete actos de la fase 3 (Task 10, decisión 4; guards, «THE ACTS THAT END A PERSON'S AI GRANTS»): la fila de la persona primero (ya la toma `_target(lock=True)`), después las sesiones, después `revoke_all_for_user` (códigos sin canjear por su `expires_at`, familias bloqueadas en orden de id), y la cadena de auditoría la última. Un canje de código de la persona que llegue a la vez espera en su fila y después lee su código caducado (la prueba de carrera de abajo).

- [ ] **Step 1: Preparar la worktree**

```bash
git log --oneline -1
ln -s /home/user/Novieri-RMM/backend/.venv backend/.venv
ln -s /home/user/Novieri-RMM/frontend/node_modules frontend/node_modules
cd backend && export PYTHONPATH=$PWD
.venv/bin/python -c "import app; print(app.__file__)"
pg_ctlcluster 16 main start 2>/dev/null; redis-cli ping >/dev/null 2>&1 || redis-server --daemonize yes
```
Expected: `dd062f8 Merge pull request #12 …`, y una ruta que empieza por la de la worktree. Exporta `ITS_TEST_REDIS_URL` con la base que te dé el coordinador.

- [ ] **Step 2: Las pruebas que fallan**

En `backend/tests/test_mcp_grant_lifecycle.py`, el docstring del módulo (`:5-9`) cambia:

```python
# antes
link), a password change (their own, or by the mail link) and "sign out
everywhere". An administrator ending the person's sessions forgets no browser,
and ends no grant (B3-07).
# después
link), a password change (their own, or by the mail link) and "sign out
everywhere" -- the person's own, and since operator decision E1 (2026-11-15;
phase 4 plan, Task 1) an administrator's too, which still forgets no
browser.
```

Detrás de `from app.models.oauth import OAuthCode, OAuthRefreshFamily` (`:34`):

```python
from app.modules.user_admin import service as user_admin_service
```

La prueba `test_an_administrator_ending_sessions_leaves_the_ai_connections` (`:313-329`) se reemplaza entera por estas dos:

```python
async def test_an_administrators_sign_out_everywhere_ends_the_ai_connections(
    lifecycle_env: Any,
) -> None:
    """Operator decision E1 (2026-11-15; phase 4 plan, Task 1). This test was
    `test_an_administrator_ending_sessions_leaves_the_ai_connections`, which
    pinned the opposite (phase 3 plan, Task 10, decision 3; B3-07): inverted
    on purpose, not weakened. The grant ends with the person's own act's
    reason, the unspent code by its expiry, and the act counts the grant."""
    env = lifecycle_env
    org = await env.create_org("grant-sessions")
    admin = await signed_in(env, org, "org-admin")
    member = await signed_in(env, org, "technician")
    family = await _connected(env, org, member.user_id)
    pending = await _pending(env, org, member.user_id)

    resp = await env.client.delete(
        f"/api/v1/users/{member.user_id}/sessions", headers=admin.headers()
    )

    assert resp.status_code == 204, resp.text
    assert await _reason(env, family) == "signed_out_everywhere"
    assert await _counted(env, org, "auth.sessions_ended") == 1
    assert await _ended(env, pending)


async def test_an_administrators_sign_out_everywhere_leaves_their_own_grants(
    lifecycle_env: Any,
) -> None:
    """Only the person named in the path loses their grants: the
    administrator who acted keeps theirs, and so does a colleague."""
    env = lifecycle_env
    org = await env.create_org("grant-sessions-own")
    admin = await signed_in(env, org, "org-admin")
    member = await signed_in(env, org, "technician")
    colleague = await signed_in(env, org, "technician")
    mine = await _connected(env, org, admin.user_id)
    theirs = await _connected(env, org, colleague.user_id)
    await _connected(env, org, member.user_id)

    resp = await env.client.delete(
        f"/api/v1/users/{member.user_id}/sessions", headers=admin.headers()
    )

    assert resp.status_code == 204, resp.text
    assert await _reason(env, mine) is None
    assert await _reason(env, theirs) is None
```

Y delante de la línea `# One clock (Task 10 review, Important 1)` (con su regla de guiones de encima), la carrera:

```python
async def test_an_exchange_waiting_on_an_administrators_sign_out_opens_no_grant(
    lifecycle_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator decision E1, the race the previous test's twin closes for the
    person's own act. A technician approved "claude" a moment ago; its code
    is not exchanged yet. An administrator signs them out everywhere, and the
    act is held where it ends their sessions -- the technician's `users` row
    held (`user_admin.service._target(lock=True)`). The app exchanges the
    code meanwhile and waits on that row; the act ends the unspent code and
    the grants and commits; the exchange then reads the code expired: a
    plain `invalid_grant`, and no live grant is left."""
    env = lifecycle_env
    org = await env.create_org("grant-admin-race")
    admin = await signed_in(env, org, "org-admin")
    member = await signed_in(env, org, "technician")
    verifier, challenge = pkce_pair()
    code, code_id = await _issue(env, org, member.user_id, challenge)
    paused, go, seen = pause_at(monkeypatch, user_admin_service, "end_all_for_user")
    signing_out = asyncio.create_task(
        env.client.delete(f"/api/v1/users/{member.user_id}/sessions", headers=admin.headers())
    )
    await asyncio.wait_for(paused.wait(), 30)
    exchanging = asyncio.create_task(
        token_request(
            env,
            grant_type="authorization_code",
            code=code,
            client_id="claude",
            code_verifier=verifier,
        )
    )
    watching = asyncio.create_task(until_blocked_by(env.app_engine, seen["pid"]))
    try:
        await asyncio.wait({watching, exchanging}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
        seen_waiting = watching.done() and watching.exception() is None
        waiting = watching.result() if seen_waiting else []
        held = seen_waiting and not exchanging.done()
    finally:
        if not watching.done():
            watching.cancel()
        go.set()
    signed_out, exchanged = await asyncio.wait_for(
        asyncio.gather(signing_out, exchanging, return_exceptions=True), timeout=60
    )

    assert not isinstance(signed_out, BaseException), signed_out
    assert signed_out.status_code == 204, signed_out.text
    assert not isinstance(exchanged, BaseException), exchanged
    assert held, exchanged.text
    assert waiting and all(
        "FROM USERS " in normalised(q) and "FOR NO KEY UPDATE" in normalised(q) for q in waiting
    ), waiting
    assert exchanged.status_code == 400, exchanged.text
    assert exchanged.json() == {
        "error": "invalid_grant",
        "error_description": "the authorization code is not valid",
    }
    assert await _ended(env, code_id)
    async with oauth_session(org, env.app_engine) as session:
        live = (
            await session.execute(
                select(OAuthRefreshFamily.id).where(
                    OAuthRefreshFamily.user_id == member.user_id,
                    OAuthRefreshFamily.revoked_at.is_(None),
                )
            )
        ).all()
    assert live == []
```

`_connected`, `_pending`, `_reason`, `_counted`, `_ended`, `_issue`, `pkce_pair`, `token_request`, `pause_at`, `until_blocked_by`, `normalised` y `oauth_session` ya están en el archivo o en sus imports (la carrera gemela del acto de la persona los usa).

En `backend/tests/test_user_lifecycle.py:644-646`, la aserción exacta del evento:

```python
    assert [(e.actor_id, e.target_id, e.meta) for e in events] == [
        (
            admin.user_id,
            str(member.user_id),
            {"by": "administrator", "scope": "everywhere", "ai_grants_revoked": 0},
        )
    ]
```

- [ ] **Step 3: Las pruebas fallan**

Run: `.venv/bin/pytest -q -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning tests/test_mcp_grant_lifecycle.py tests/test_user_lifecycle.py -k "administrator or sessions"`
Expected: FAIL en `test_an_administrators_sign_out_everywhere_ends_the_ai_connections` (`None == 'signed_out_everywhere'`), en `test_an_exchange_waiting_on_an_administrators_sign_out_opens_no_grant` (el canje sí espera en la fila, que `_target(lock=True)` ya tomaba, y después canjea su código: `assert 200 == 400` en `exchanged.status_code`, con una concesión viva) y en la de `test_user_lifecycle.py` (falta `ai_grants_revoked`). `test_an_administrators_sign_out_everywhere_leaves_their_own_grants` pasa ya: es la guarda de que el cambio no se pase de largo.

- [ ] **Step 4: El acto**

En `backend/app/modules/user_admin/service.py`, `end_sessions` (`:1173-1199`) queda así (firma igual):

```python
async def end_sessions(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, user_id: UUID
) -> None:
    """Every console session of the person ends, and so do their AI grants
    and the codes an AI app has not exchanged yet; their remembered devices
    stay (a reset or a disable forgets those). A deleted account is refused
    (`_require_live`, final review C-M5): deleting ended its sessions, and a
    record of ending them again would be about nobody. No `super-admin` rail
    (operator decision 2, 2026-11-05).

    THE AI GRANTS END TOO (operator decision E1, 2026-11-15; own
    authentication phase 4, Task 1), reversing phase 3's Task 10, decision 3:
    an administrator who signs a person out everywhere is stopping that
    person, and an AI app acting as them is the person too (CLAUDE.md 1c).
    `ai_grants.revoke_all_for_user` with the reason the person's own "sign
    out everywhere" gives (`signed_out_everywhere`: `REVOKE_REASONS` is a
    closed list in migration 0247; the audit event tells the two acts apart
    by `by` and the actor), after the sessions and before the chain, counted
    in the event as `ai_grants_revoked`, as every act that ends grants does.

    THE ROW FIRST (`_target(lock=True)`, own authentication phase 2, Task 9b
    fix round 1): a Microsoft link's return checks its session is live under
    the person's row and then links, so ending the sessions waits for a link
    in flight and is waited for by one that arrives -- a link never lands in
    a session ended before it committed. An AI app's code exchange takes the
    same row first (`grants._exchange`), so one in flight waits for this act
    and then finds its code ended (`tests/test_mcp_grant_lifecycle.py`)."""
    user = await _target(session, org_id, user_id, lock=True)
    _require_live(user)
    await end_all_for_user(session, user.id, "ended_by_admin")
    ai_ended = await ai_grants.revoke_all_for_user(session, user.id, "signed_out_everywhere")
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="auth.sessions_ended",
        target_type="user",
        target_id=str(user.id),
        metadata={"by": "administrator", "scope": "everywhere", "ai_grants_revoked": ai_ended},
    )
```

En `backend/app/mcp/descriptions/part_d.py:546-548`, la descripción de la herramienta MCP:

```python
    "DELETE /api/v1/users/{user_id}/sessions": (
        "Ends every console session of a user at once and disconnects the AI apps they "
        "connected (this connection too, if the user is you); their remembered browsers stay. "
        "Audited."
    ),
```

Y el docstring del módulo, detrás del párrafo «DISABLE, DELETE AND A FACTOR RESET END THE PERSON'S SESSIONS …» (`:34-44`, que termina «… event as `ai_grants_revoked`.»), antes de las comillas que lo cierran:

```python
AN ADMINISTRATOR'S SIGN-OUT EVERYWHERE (`end_sessions`) ends the sessions and,
since operator decision E1 (2026-11-15; own authentication phase 4), the AI
grants and the unspent codes too, in the same order and counted the same way;
it forgets no device. The grants end with the reason the person's own
sign-out gives (`signed_out_everywhere`, the button's own name); the audit
event tells the two acts apart: `auth.sessions_ended` with `by:
administrator` and the administrator as actor here, `by: self` and the
person as actor in `flows.logout_everywhere`.
```

En `backend/app/core/auth/guards.py`, la viñeta (`:156-161`):

```python
# antes
    - THE ACTS THAT END A PERSON'S AI GRANTS (own authentication phase 3,
      Task 10; operator decision D2, part c): a disable, a deletion, a
      factor reset (`user_admin.service`), a password change (`account`,
      or a mailed link in `flows.reset_password`), signing out everywhere
      and the recovery link (`flows`). Each holds the row above first, then
      forgets the devices, then `oauth.grants.revoke_all_for_user`: the
# después
    - THE ACTS THAT END A PERSON'S AI GRANTS (own authentication phase 3,
      Task 10; operator decision D2, part c): a disable, a deletion, a
      factor reset and, since operator decision E1 (2026-11-15), an
      administrator's sign-out everywhere (`user_admin.service`), a password
      change (`account`, or a mailed link in `flows.reset_password`), the
      person's own sign-out everywhere and the recovery link (`flows`).
      Each holds the row above first, then (all but an administrator's
      sign-out everywhere, which leaves them as they are) forgets the
      devices, then `oauth.grants.revoke_all_for_user`: the
```

(El resto de la viñeta sigue igual, desde «person's unspent codes end by their expiry …»: el orden fila → … → `revoke_all_for_user` → cadena es el mismo en el acto del administrador.)

- [ ] **Step 5: Las pruebas pasan**

Run: `.venv/bin/pytest -q -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning tests/test_mcp_grant_lifecycle.py tests/test_user_lifecycle.py tests/test_user_admin_api.py tests/test_user_admin_races.py tests/test_mcp_descriptions.py tests/test_mcp_catalog.py tests/test_logging_discipline.py`
Expected: PASS. (En la prueba de esta tarea, 44 pasaron en `test_mcp_grant_lifecycle.py` + `test_user_lifecycle.py`.)

- [ ] **Step 6: El runbook**

En `deploy/README.md`, `### Revoking`, la viñeta «One person» (`:2522-2531`) entera:

```markdown
- **One person:** take `mcp.use` away from their role. Their next call is
  refused. Disabling or deleting the person, resetting their factors (by an
  administrator, or with a recovery link), a password change (their own, or
  through a reset mail), the person's own **Sign out everywhere** (their
  security page, `POST /api/v1/auth/logout-everywhere`) and, since
  2026-11-15, an administrator's **Sign out everywhere** (Settings → Users)
  end all their AI grants too, and the codes an app has not exchanged yet;
  the act's own audit event says how many grants ended
  (`ai_grants_revoked`). An administrator's sign-out still leaves the
  person's remembered browsers as they are.
```

Y el riesgo 6 (`:2652-2656`):

```markdown
6. **A stolen access token works for at most ten minutes, or until its grant
   ends.** It names its grant, which the API reads on every call: revoking the
   app, the person's account acts above (an administrator's "Sign out
   everywhere" among them), or the app's refresh token being used twice (a
   sign it was copied) end it at once.
```

- [ ] **Step 7: Las enmiendas fechadas**

En `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §5.3, detrás de la viñeta «Revocar un cliente de IA en Ajustes corta su acceso en la siguiente llamada (1c(d)).» (`:340`):

```markdown
- *Enmienda del 2026-11-15 (decisión del operador E1; plan de la fase 4, Task 1).* «Cerrar sesión en todas partes»,
  el de la persona y el de un administrador (`DELETE /api/v1/users/{id}/sessions`), termina también sus concesiones
  de IA y los códigos que una app aún no canjeó, en la misma transacción y con el recuento en el evento
  (`ai_grants_revoked`). El del administrador sigue sin olvidar los equipos recordados. La consola ofrece el botón a
  la persona en su página de seguridad, y al contacto en la del portal.
```

En `docs/superpowers/plans/2026-11-06-autenticacion-propia-fase3.md`, detrás del párrafo «**Decisión de la tarea (3): los actos de la cuenta son los que ya olvidan los equipos recordados** (B3-07). …» (termina en «… el mismo alias en los tres archivos.», `:11327`), un párrafo nuevo:

```markdown
*Enmienda del 2026-11-15 (decisión del operador E1; plan de la fase 4,
`docs/superpowers/plans/2026-11-15-autenticacion-propia-fase4.md`, Task 1).* La frase «Que un administrador cierre
las sesiones de alguien no los olvida (`user_admin.service.end_sessions`, «their remembered devices stay»), y tampoco
termina sus concesiones» deja de valer en su segunda mitad: `end_sessions` termina las concesiones de la persona con
el motivo `signed_out_everywhere`, después de sus sesiones y antes de la cadena, y su evento lleva
`ai_grants_revoked`. Sigue sin olvidar sus equipos recordados. Son ocho actos, no siete.
```

- [ ] **Step 8: Tipos y estilo**

Run: `.venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `Success: no issues found` y `All checks passed!`

- [ ] **Step 9: Commit**

```bash
git add backend/app/modules/user_admin/service.py backend/app/mcp/descriptions/part_d.py \
  backend/app/core/auth/guards.py backend/tests/test_mcp_grant_lifecycle.py \
  backend/tests/test_user_lifecycle.py deploy/README.md \
  docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md \
  docs/superpowers/plans/2026-11-06-autenticacion-propia-fase3.md
git commit -m "feat(auth): an administrator's sign-out everywhere ends the person's AI grants (E1)"
```
(con las dos líneas de atribución que dé el coordinador al final del mensaje).

---

### Task 2: E1 en la consola — «Cerrar sesión en todas partes» en «Tu seguridad» y en el portal

**Files:**
- Modify: `frontend/src/lib/session.ts` (función nueva delante del comentario «Whether a person is signed in right now»)
- Modify: `frontend/src/modules/profile/SecurityPage.tsx` (cabecera de rutas, comentario, imports, `SecurityPage`, `PortalSecurityPage`, `SecurityBody`, `SessionsPanel`)
- Modify: `frontend/src/i18n/en/profile.ts`, `frontend/src/i18n/es/profile.ts` (`securityPage.sessions`, `securityPage.confirm`)
- Modify: `frontend/src/i18n/en/settings.ts:322`, `frontend/src/i18n/es/settings.ts:311` (la confirmación del administrador)
- Test: `frontend/src/lib/session.navigation.test.ts`, `frontend/src/modules/profile/SecurityPage.test.tsx`, `frontend/src/modules/profile/SecurityPage.contact.test.tsx`, `frontend/src/modules/settings/__tests__/users.test.tsx`, `frontend/src/modules/settings/__tests__/harness.tsx`

**Interfaces:**
- Depende de: `ApiClient.logoutEverywhere()` (ya existe, `POST /api/v1/auth/logout-everywhere`), `forgetStoredClient`, `sessionSignedOut`, `navigation`, `SIGN_IN_PATH`, `PORTAL_SIGN_IN_PATH` (`frontend/src/lib/session.ts:71`, `:74`), `ConfirmDialog`, `Btn`, `Failure`, `describeSubmitError` (ya usados en `SecurityPage.tsx`).
- Produce: `signOutEverywhere(api, to = SIGN_IN_PATH): Promise<void>`; la prop `signIn: string` de `SecurityBody` y `SessionsPanel`.

**Decisiones de la tarea** (Apéndice B.2): P4-02 (el portal también), P4-03 (adónde va la página; un 401 cuenta como sesión cerrada y va al inicio de sesión: decisión del coordinador, 2026-11-15), P4-04 (confirmación sin segundo factor). El texto de la persona técnica nombra las apps de IA; el del contacto no (no tiene concesiones). La confirmación del administrador en Ajustes → Usuarios pasa a decir que las apps de IA de la persona pierden el acceso (Task 1).

- [ ] **Step 1: Preparar la worktree** (Task 1, Step 1, sin el `cd backend`; `cd frontend`).

- [ ] **Step 2: Las pruebas que fallan**

`frontend/src/lib/session.navigation.test.ts`: el import (`enterConsole, goToSignIn, navigation, signOut` desde `./session`) gana `signOutEverywhere`:

```ts
import { enterConsole, goToSignIn, navigation, signOut, signOutEverywhere } from "./session";
```

y al final del archivo:

```ts
describe("signOutEverywhere (operator decision E1, 2026-11-15)", () => {
  it("asks the server to end every session, then leaves for the page it is given", async () => {
    const logoutEverywhere = vi.fn().mockResolvedValue(undefined);
    await signOutEverywhere({ logoutEverywhere });
    expect(logoutEverywhere).toHaveBeenCalledOnce();
    expect(assign).toHaveBeenCalledWith("/sign-in");
    await signOutEverywhere({ logoutEverywhere }, "/portal/sign-in");
    expect(assign).toHaveBeenLastCalledWith("/portal/sign-in");
  });

  it("counts a 401 as signed out: this session had already ended, so it leaves for the sign-in page", async () => {
    await signOutEverywhere({ logoutEverywhere: vi.fn().mockRejectedValue(new ApiError(401, "")) }, "/portal/sign-in");
    expect(assign).toHaveBeenCalledExactlyOnceWith("/portal/sign-in");
  });

  it("rejects, and stays, on any other refusal", async () => {
    await expect(
      signOutEverywhere({ logoutEverywhere: vi.fn().mockRejectedValue(new ApiError(503, "")) }),
    ).rejects.toBeInstanceOf(ApiError);
    expect(assign).not.toHaveBeenCalled();
  });

  it("stays, and rejects, when the server never heard", async () => {
    await expect(
      signOutEverywhere({ logoutEverywhere: vi.fn().mockRejectedValue(new TypeError("Failed to fetch")) }),
    ).rejects.toThrow("Failed to fetch");
    expect(assign).not.toHaveBeenCalled();
  });
});
```

(`assign` y `ApiError` ya están en el archivo: su `beforeEach` espía `navigation.assign`.)

`frontend/src/modules/profile/SecurityPage.test.tsx`: detrás de `import { ApiError } from "../../lib/api";`:

```ts
import { navigation } from "../../lib/session";
```

y delante de `it("changes nothing when the recent confirmation is closed, and says so where the change was asked", async () => {`:

```ts
  it("signs out everywhere after a confirmation that names the AI apps, then leaves for the sign-in page", async () => {
    const assign = vi.spyOn(navigation, "assign").mockImplementation(() => {});
    assign.mockClear();
    const user = userEvent.setup();
    renderSecurityPage();
    await user.click(await screen.findByRole("button", { name: "Sign out everywhere" }));
    const dialog = await screen.findByRole("dialog", { name: "Sign out everywhere" });
    expect(dialog).toHaveTextContent("every AI app you connected loses its access");
    await user.click(within(dialog).getByRole("button", { name: "Sign out everywhere" }));
    await waitFor(() => expect(assign).toHaveBeenCalledExactlyOnceWith("/sign-in"));
    expect(calls.logoutEverywhere).toHaveBeenCalledOnce();
    expect(calls.endMySession).not.toHaveBeenCalled();
    assign.mockRestore();
  });

  it("leaves for the sign-in page when this session had already ended (a 401)", async () => {
    const assign = vi.spyOn(navigation, "assign").mockImplementation(() => {});
    assign.mockClear();
    calls.logoutEverywhere.mockRejectedValue(new ApiError(401, ""));
    const user = userEvent.setup();
    renderSecurityPage();
    await user.click(await screen.findByRole("button", { name: "Sign out everywhere" }));
    const dialog = await screen.findByRole("dialog", { name: "Sign out everywhere" });
    await user.click(within(dialog).getByRole("button", { name: "Sign out everywhere" }));
    await waitFor(() => expect(assign).toHaveBeenCalledExactlyOnceWith("/sign-in"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    assign.mockRestore();
  });

  it("stays on the page and says why when signing out everywhere fails", async () => {
    const assign = vi.spyOn(navigation, "assign").mockImplementation(() => {});
    assign.mockClear();
    calls.logoutEverywhere.mockRejectedValue(new ApiError(503, ""));
    const user = userEvent.setup();
    renderSecurityPage();
    await user.click(await screen.findByRole("button", { name: "Sign out everywhere" }));
    const dialog = await screen.findByRole("dialog", { name: "Sign out everywhere" });
    await user.click(within(dialog).getByRole("button", { name: "Sign out everywhere" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(assign).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Sign out everywhere" })).toBeEnabled();
    assign.mockRestore();
  });

```

`frontend/src/modules/profile/SecurityPage.contact.test.tsx`: detrás de `import { ApiError } from "../../lib/api";`:

```ts
import { navigation } from "../../lib/session";
```

y como primera prueba dentro de `describe("a contact's security", () => {`:

```ts
  it("signs out everywhere with no word of AI apps, then leaves for the portal's sign-in", async () => {
    // Operator decision E1 (2026-11-15; phase 4 plan, Task 2, decision 2): a
    // contact holds no AI grant, but ending every session and forgetting every
    // remembered device is theirs to ask for as much as a technician's.
    const assign = vi.spyOn(navigation, "assign").mockImplementation(() => {});
    assign.mockClear();
    const user = userEvent.setup();
    renderPortalSecurity();
    const box = await panel("Sessions");
    expect(box).not.toHaveTextContent("AI");
    await user.click(within(box).getByRole("button", { name: "Sign out everywhere" }));
    const dialog = await screen.findByRole("dialog", { name: "Sign out everywhere" });
    expect(dialog).not.toHaveTextContent("AI");
    await user.click(within(dialog).getByRole("button", { name: "Sign out everywhere" }));
    await waitFor(() => expect(assign).toHaveBeenCalledExactlyOnceWith("/portal/sign-in"));
    expect(calls.logoutEverywhere).toHaveBeenCalledOnce();
    assign.mockRestore();
  });

```

(`assign.mockClear()` y `mockRestore()`: el espía conserva sus llamadas entre pruebas del mismo archivo; la prueba de ensayo lo vio.)

`frontend/src/modules/settings/__tests__/harness.tsx`: detrás de `  endMySession: vi.fn(),`:

```ts
  logoutEverywhere: vi.fn(),
```

y detrás de `  calls.endMySession.mockResolvedValue(undefined);`:

```ts
  calls.logoutEverywhere.mockResolvedValue(undefined);
```

`frontend/src/modules/settings/__tests__/users.test.tsx`, delante de `it("revoking a role confirms, and names both the role and the person", async () => {`:

```ts
  it("says that signing someone out everywhere ends the AI apps they connected too", async () => {
    // Operator decision E1 (2026-11-15; own-auth phase 4, Task 1): the
    // server ends their AI grants with their sessions, so the confirmation
    // says so before the administrator acts.
    calls.listUsers.mockResolvedValue({ users: [OPERATOR_USER, CASEY_USER, DANA_USER] });
    const user = await openUsers();
    const row = await screen.findByRole("row", { name: /operator@example\.com/ });
    await openMoreActions(user, row);
    await user.click(within(row).getByRole("button", { name: "Sign out operator@example.com everywhere" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("every AI app they connected loses its access");
  });

```

- [ ] **Step 3: Las pruebas fallan**

Run (desde `frontend/`): `npx vitest run src/lib/session.navigation.test.ts src/modules/profile src/modules/settings/__tests__/users.test.tsx`
Expected: FAIL — `TypeError: signOutEverywhere is not a function` en `session.navigation.test.ts` (vitest importa el nombre que falta como `undefined` y falla al llamarlo), no hay botón «Sign out everywhere» en «Sessions», y la confirmación del administrador no dice «every AI app».

- [ ] **Step 4: `signOutEverywhere`**

En `frontend/src/lib/session.ts`, delante del bloque de comentario que empieza `/**\n * Whether a person is signed in right now, as far as this tab knows:`:

```ts
/**
 * Sign out everywhere (operator decision E1, 2026-11-15): the server ends
 * every session of this person -- this one too -- and forgets every browser
 * they remembered; a technician's AI apps lose their access too. Then the
 * whole document goes to the sign-in page, as `signOut` does: the session
 * this tab was using is gone with the rest.
 *
 * A 401 COUNTS AS SIGNED OUT, as in `signOut` (coordinator's decision on
 * P4-03, 2026-11-15): this tab's session had already ended, so there is
 * nothing of it left here, and the page goes to the sign-in. The server
 * ended nothing else on that request; a person who still has another
 * session open signs in again and presses the button there. Every other
 * failure rejects, and the page stays and says why.
 *
 * `to`: the console's sign-in page, or the portal's for a contact.
 */
export async function signOutEverywhere(
  api: Pick<ApiClient, "logoutEverywhere">,
  to: string = SIGN_IN_PATH,
): Promise<void> {
  try {
    await api.logoutEverywhere();
  } catch (error: unknown) {
    if (!(error instanceof ApiError && error.status === 401)) throw error;
  }
  forgetStoredClient();
  sessionSignedOut();
  navigation.assign(to);
}

```

- [ ] **Step 5: La página**

En `frontend/src/modules/profile/SecurityPage.tsx`:

1. En la lista de rutas de la cabecera, detrás de `//   DELETE /me/sessions/{id}                                   sign out another session`:

```ts
//   POST   /auth/logout-everywhere                             sign out everywhere, this session too
```

2. Detrás del párrafo `// A PASSWORD CHANGE KEEPS THIS SESSION (own-auth Task 7): …` (sus tres líneas):

```ts
//
// "SIGN OUT EVERYWHERE" IS (operator decision E1, 2026-11-15; own-auth phase 4,
// Task 2). It ends every session of the person, this one included, forgets
// every remembered device and, for a technician, ends the AI apps they
// connected; so a 204 leaves for the sign-in page -- the console's, or the
// portal's for a contact -- and so does a 401, this session having already
// ended; anything else stays here and says why.
```

3. Imports: detrás de `import { useT } from "../../i18n/context";`:

```ts
import { useApi } from "../../lib/ApiContext";
```

y delante de `import { useOrgZone } from "../../lib/zone";`:

```ts
import { PORTAL_SIGN_IN_PATH, SIGN_IN_PATH, signOutEverywhere } from "../../lib/session";
```

4. `SecurityPage` devuelve `<SecurityBody zone={zone} back="/profile/security" signIn={SIGN_IN_PATH} />;` y `PortalSecurityPage` devuelve `<SecurityBody zone={me.data?.timezone ?? null} back="/portal/security" signIn={PORTAL_SIGN_IN_PATH} />;`.

5. `SecurityBody`:

```tsx
/**
 * `back`: this page's own address, where linking a Microsoft account comes
 * back to. `signIn`: where "Sign out everywhere" leaves for.
 */
function SecurityBody({ zone, back, signIn }: { zone: string | null; back: string; signIn: string }) {
```

y dentro, `<SessionsPanel security={data} run={stepUp.run} zone={zone} signIn={signIn} />`.

6. `SessionsPanel`: la firma y las primeras líneas quedan

```tsx
function SessionsPanel({
  security,
  run,
  zone,
  signIn,
}: {
  security: MySecurity;
  run: StepUpRun;
  zone: string | null;
  signIn: string;
}) {
  const t = useT();
  const api = useApi();
  const end = useEndMySessionMutation(run);
  const [ending, setEnding] = useState<MySession | null>(null);
  // Sign out everywhere: asked, then sent; `leaving` holds the button while the
  // request is out, and a failure is shown here because the page stays.
  const [askingEverywhere, setAskingEverywhere] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const [everywhereFailed, setEverywhereFailed] = useState<unknown>(null);
  const contact = security.kind === "contact";
```

y entre `{end.error ? <Failure message={describeSubmitError(end.error)} /> : null}` y el `<ConfirmDialog open={ending !== null}` que ya hay:

```tsx
      <div style={ROW}>
        <p className="note" style={ROW_TEXT}>
          {t(contact ? "profile.securityPage.sessions.everywhereNoteContact" : "profile.securityPage.sessions.everywhereNote")}
        </p>
        <Btn
          small
          variant="danger"
          icon="logout"
          label={t("profile.securityPage.sessions.everywhere")}
          disabled={leaving}
          onClick={() => setAskingEverywhere(true)}
        />
      </div>
      {everywhereFailed ? <Failure message={describeSubmitError(everywhereFailed)} /> : null}

      <ConfirmDialog
        open={askingEverywhere}
        title={t("profile.securityPage.confirm.everywhere.title")}
        body={t(contact ? "profile.securityPage.confirm.everywhere.bodyContact" : "profile.securityPage.confirm.everywhere.body")}
        subject={t("profile.securityPage.confirm.everywhere.subject")}
        confirmLabel={t("profile.securityPage.confirm.everywhere.action")}
        destructive
        onConfirm={() => {
          setAskingEverywhere(false);
          setEverywhereFailed(null);
          setLeaving(true);
          signOutEverywhere(api, signIn).catch((error: unknown) => {
            setLeaving(false);
            setEverywhereFailed(error);
          });
        }}
        onCancel={() => setAskingEverywhere(false)}
      />

```

(`ROW`, `ROW_TEXT`, `Btn`, `Failure`, `ConfirmDialog` y `describeSubmitError` ya los usa el archivo; `security.kind` es `"staff" | "contact"` en `MySecurity`.)

- [ ] **Step 6: Los textos**

`frontend/src/i18n/en/profile.ts`: en `securityPage.sessions`, detrás de `      endAria: "Sign out the session from {where}",`:

```ts
      everywhere: "Sign out everywhere",
      everywhereNote:
        "Signs out every session above, this one too, forgets your remembered devices and disconnects the AI apps you connected.",
      everywhereNoteContact: "Signs out every session above, this one too, and forgets your remembered devices.",
```

y en `securityPage.confirm`, detrás del bloque `session: { title: "Sign out a session", … action: "Sign out", },`:

```ts
      everywhere: {
        title: "Sign out everywhere",
        body: "Every session ends, this one too, and you sign in again here. Remembered devices ask for your second factor again, and every AI app you connected loses its access until you approve it again.",
        bodyContact: "Every session ends, this one too, and you sign in again here. Remembered devices ask for your second factor again.",
        subject: "All your sessions, this one included",
        action: "Sign out everywhere",
      },
```

`frontend/src/i18n/es/profile.ts`, en los mismos sitios (detrás de `      endAria: "Cerrar la sesión desde {where}",` y del bloque `session: { title: "Cerrar una sesión", … action: "Cerrar sesión", },`):

```ts
      everywhere: "Cerrar sesión en todas partes",
      everywhereNote:
        "Cierra todas las sesiones de arriba, también esta, olvida tus equipos recordados y desconecta las aplicaciones de IA que conectaste.",
      everywhereNoteContact: "Cierra todas las sesiones de arriba, también esta, y olvida tus equipos recordados.",
```

```ts
      everywhere: {
        title: "Cerrar sesión en todas partes",
        body: "Se cierran todas las sesiones, también esta, y vuelves a iniciar sesión aquí. Los equipos recordados vuelven a pedir tu segundo factor, y cada aplicación de IA que conectaste pierde su acceso hasta que la apruebes de nuevo.",
        bodyContact: "Se cierran todas las sesiones, también esta, y vuelves a iniciar sesión aquí. Los equipos recordados vuelven a pedir tu segundo factor.",
        subject: "Todas tus sesiones, también esta",
        action: "Cerrar sesión en todas partes",
      },
```

La confirmación del administrador, `frontend/src/i18n/en/settings.ts:322`:

```ts
        body: "Every session they have open ends now, and every AI app they connected loses its access until they approve it again. Their account stays active, and they can sign in again.",
```

y `frontend/src/i18n/es/settings.ts:311`:

```ts
        body: "Todas las sesiones que tiene abiertas terminan ahora, y cada aplicación de IA que conectó pierde su acceso hasta que la apruebe de nuevo. Su cuenta sigue activa y puede volver a iniciar sesión.",
```

- [ ] **Step 7: Las pruebas pasan**

Run: `npx vitest run src/lib src/modules/profile src/modules/settings src/modules/portal src/i18n src/App.portal.test.tsx && npx tsc -b`
Expected: PASS, y `tsc` sin salida. (En el ensayo de la ronda de correcciones, sobre un árbol nuevo de `dd062f8` con esta tarea: 1.122 pruebas pasan en 75 archivos. La única roja del ensayo, `src/lib/api.session.test.ts` «is named, and valued, exactly as the server reads it», lee un archivo de `backend/`, que el árbol de ensayo no tenía; en la worktree está.)

- [ ] **Step 8: Commit**

```bash
git add frontend/src/lib/session.ts frontend/src/lib/session.navigation.test.ts \
  frontend/src/modules/profile/SecurityPage.tsx frontend/src/modules/profile/SecurityPage.test.tsx \
  frontend/src/modules/profile/SecurityPage.contact.test.tsx frontend/src/i18n/en/profile.ts \
  frontend/src/i18n/es/profile.ts frontend/src/i18n/en/settings.ts frontend/src/i18n/es/settings.ts \
  frontend/src/modules/settings/__tests__/harness.tsx frontend/src/modules/settings/__tests__/users.test.tsx
git commit -m "feat(console): sign out everywhere from your own security page (E1)"
```

---

### Task 3: E2 — los refresh tokens usados de una concesión viva, a los 30 días

**Files:**
- Modify: `backend/app/core/auth/retention.py` (docstring `:27-29`, «THE OAUTH STEPS» `:64-72`; constante detrás de `OAUTH_GRANT_RETENTION` `:145`; función nueva delante de `_oauth_grants` `:301`; `STEPS` `:352-354`)
- Modify: `backend/app/core/auth/guards.py:219-224` (la viñeta de la retención en el mapa de bloqueos)
- Modify: `backend/app/core/auth/oauth/store.py:90-98` («THE RETENTION SWEEP TAKES NEITHER ROW…»)
- Modify: `backend/app/core/client_scope.py:277-285` (el motivo de `retention.py::sweep_org`)
- Test: `backend/tests/test_oauth_retention.py` (`OAUTH_KEYS`, cuatro aserciones exactas, un import, seis pruebas al final), `backend/tests/test_auth_retention.py:276-286`
- Modify: `docs/03-security-model.md:164`, `:206-209`; `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §5.3 (detrás de `:338-339`); `deploy/README.md` (`### Residual risks, stated plainly`: la frase de cabecera `:2597-2599` y un riesgo 12 detrás del 11)

**Interfaces:**
- Depende de: `_deleted`, `aliased`, `OAuthRefreshFamily`, `OAuthRefreshToken` (ya en `retention.py`); `grants.new_secret`, `grants.hash_secret`, `oauth_session`, `lock_user_row` y los ayudantes `_person`, `_open`, `_ids`, `_retention_rows`, `_sweeping`, `NOW` (ya en `test_oauth_retention.py`).
- Produce: `OAUTH_USED_TOKEN_RETENTION`, `_oauth_used_refresh_tokens`, la clave `oauth_used_refresh_tokens` del evento (Apéndice A).

**Por qué solo concesiones vivas, y con su propia clave:** P4-05, P4-06. **Por qué no espera a nadie:** como los otros pasos OAuth (fase 3, Task 13), bloquea lo que borra con FOR UPDATE SKIP LOCKED; un refresco que presenta el token usado lo tiene bloqueado (`grants._refresh`, paso 3) y el barrido lo deja para el día siguiente. La concesión se lee y no se bloquea: el token es una hoja y su `DELETE` no arrastra nada.

**La consecuencia que acepta E2, dicha entera.** RFC 9700 §4.14.2: «information about the relationship is retained by the authorization server … one of them will present an invalidated refresh token, which will inform the authorization server of the breach» [R1]. Pasados 30 días, el servidor ya no guarda esa relación: un token usado presentado después es un `invalid_grant` simple y la concesión sigue. El caso que cuesta: alguien copia el refresh token y refresca **antes** que la app; la app vuelve más de 30 días después con su copia, recibe `invalid_grant` y la persona la conecta de nuevo; la concesión del ladrón sigue viva hasta que alguien la revoca o pasa 30 días sin uso. Lo dicen la enmienda del diseño y el riesgo residual «A used refresh token is remembered for 30 days» del runbook (Steps 7 y 8; la Task 9 le cambia el número, 12 → 10, y por eso la enmienda lo cita por su título).

- [ ] **Step 1: Preparar la worktree** (Task 1, Step 1).

- [ ] **Step 2: Las pruebas que fallan**

En `backend/tests/test_oauth_retention.py`:

```python
# antes
OAUTH_KEYS = ("oauth_codes", "oauth_refresh_tokens", "oauth_grants", "oauth_apps")
# después
OAUTH_KEYS = (
    "oauth_codes",
    "oauth_refresh_tokens",
    "oauth_used_refresh_tokens",
    "oauth_grants",
    "oauth_apps",
)
```

Los recuentos exactos que ya fija el archivo ganan la clave nueva, a 0:
- el diccionario `{"oauth_codes": 1, "oauth_refresh_tokens": 2, "oauth_grants": 2, "oauth_apps": 1}` gana `"oauth_used_refresh_tokens": 0,` detrás de `"oauth_refresh_tokens": 2,`;
- `assert [swept.counts[key] for key in OAUTH_KEYS[:-1]] == [1, 2, 2]` pasa a `== [1, 2, 0, 2]`;
- `assert [name for name, _ in retention.STEPS][-3:] == list(OAUTH_KEYS[:-1])` pasa a `[-4:]`;
- detrás de `    assert retention.OAUTH_GRANT_RETENTION == timedelta(days=1)`: `    assert retention.OAUTH_USED_TOKEN_RETENTION == timedelta(days=30)`;
- `        assert [swept.counts[key] for key in OAUTH_KEYS[:-1]] == [0, 0, 0]` pasa a `== [0, 0, 0, 0]`;
- el diccionario `{"oauth_codes": 0, "oauth_refresh_tokens": 1, "oauth_grants": 0, "oauth_apps": 0}` gana `"oauth_used_refresh_tokens": 0,` detrás de `"oauth_refresh_tokens": 1,`;
- el principio del docstring de `test_the_oauth_steps_close_the_registries`:

```python
# antes
    """Codes, the ended grants' refresh tokens and the grants close `STEPS`,
    leaves first, in the sweep's transaction; the idle apps are
# después
    """Codes, the ended grants' refresh tokens, the refresh tokens live
    grants rotated past (operator decision E2) and the ended grants close
    `STEPS`, leaves first, in the sweep's transaction; the idle apps are
```

El import de `tests.oauth_support` (`from tests.oauth_support import RESOURCE, oauth_org, pkce_pair, seed_client, seed_code`) pasa a:

```python
from tests.oauth_support import (
    RESOURCE,
    oauth_org,
    pkce_pair,
    seed_client,
    seed_code,
    token_request,
)
```

Y al final del archivo:

```python
# --------------------------------------------------------------------------- #
# A live grant's used refresh tokens (operator decision E2, 2026-11-15; phase
# 4 plan, Task 3)
# --------------------------------------------------------------------------- #

#: Past and short of `OAUTH_USED_TOKEN_RETENTION` (30 days).
LONG_AGO = NOW - timedelta(days=31)
NOT_YET = NOW - timedelta(days=29)


async def _rotated(
    env: Any, org_id: UUID, user_id: UUID, family_id: UUID, *, used: datetime
) -> tuple[UUID, str]:
    """A refresh token of the grant `family_id` that a refresh rotated past at
    `used`: its id and its value."""
    secret = grants.new_secret()
    async with oauth_session(org_id, env.app_engine) as session:
        row = OAuthRefreshToken(
            org_id=org_id,
            user_id=user_id,
            family_id=family_id,
            token_hash=grants.hash_secret(secret),
            created_at=used - timedelta(minutes=10),
            used_at=used,
        )
        session.add(row)
        await session.flush()
        token_id: UUID = row.id
    return token_id, secret


async def _reused_audits(env: Any, org_id: UUID) -> list[Any]:
    async with AsyncSession(env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent.id).where(
                        AuditEvent.org_id == org_id, AuditEvent.action == "mcp.refresh_reused"
                    )
                )
            ).scalars()
        )


async def test_a_live_grants_used_tokens_go_thirty_days_after_their_use(api_env) -> None:
    """The one rotated past 31 days ago goes; the one rotated past 29 days
    ago, the grant's current token and the grant stay; the day's audit row
    counts it under its own key."""
    org_id, _sub, user_id, app = await _person(api_env, "oauth-ret-used")
    family_id, _current = await _open(api_env, org_id, user_id, app)
    old, _ = await _rotated(api_env, org_id, user_id, family_id, used=LONG_AGO)
    recent, _ = await _rotated(api_env, org_id, user_id, family_id, used=NOT_YET)

    assert await retention.sweep_org(org_id, api_env.app_engine, now=NOW) == 1

    left = await _ids(api_env, OAuthRefreshToken, org_id)
    assert old not in left and recent in left and len(left) == 2
    assert await _ids(api_env, OAuthRefreshFamily, org_id) == {family_id}
    (row,) = await _retention_rows(api_env, org_id)
    assert {key: row[key] for key in OAUTH_KEYS} == {
        "oauth_codes": 0,
        "oauth_refresh_tokens": 0,
        "oauth_used_refresh_tokens": 1,
        "oauth_grants": 0,
        "oauth_apps": 0,
    }


async def test_an_ended_grants_used_tokens_are_not_this_steps(api_env) -> None:
    """A grant revoked an hour ago keeps every token until a day after it
    ended, when `oauth_refresh_tokens` takes them all: this step takes a LIVE
    grant's only. Lapsed counts as ended too."""
    org_id, _sub, user_id, app = await _person(api_env, "oauth-ret-used-ended")
    revoked, _ = await _open(
        api_env,
        org_id,
        user_id,
        app,
        revoked_at=NOW - timedelta(hours=1),
        revoked_reason="revoked_in_settings",
    )
    lapsed, _ = await _open(api_env, org_id, user_id, app, expires_at=NOW - timedelta(hours=1))
    of_revoked, _ = await _rotated(api_env, org_id, user_id, revoked, used=LONG_AGO)
    of_lapsed, _ = await _rotated(api_env, org_id, user_id, lapsed, used=LONG_AGO)

    assert await retention.sweep_org(org_id, api_env.app_engine, now=NOW) == 0

    assert {of_revoked, of_lapsed} <= await _ids(api_env, OAuthRefreshToken, org_id)


async def test_reusing_a_token_the_sweep_took_is_a_plain_invalid_grant(api_env) -> None:
    """The consequence the operator accepted with E2 (design §5.3, amended
    2026-11-15): once the sweep took a used token, presenting it is a token
    this server does not know -- `invalid_grant`, no `mcp.refresh_reused`, and
    the grant, with the token the app holds now, lives on."""
    org_id, _sub, user_id, app = await _person(api_env, "oauth-ret-used-gone")
    family_id, _current = await _open(api_env, org_id, user_id, app)
    _, old = await _rotated(api_env, org_id, user_id, family_id, used=LONG_AGO)
    assert await retention.sweep_org(org_id, api_env.app_engine, now=NOW) == 1

    reused = await token_request(
        api_env, grant_type="refresh_token", refresh_token=old, client_id=app.client_id
    )

    assert reused.status_code == 400, reused.text
    assert reused.json() == {
        "error": "invalid_grant",
        "error_description": "the refresh token is not valid",
    }
    async with AsyncSession(api_env.admin_engine) as session:
        family = await session.get(OAuthRefreshFamily, family_id)
    assert family is not None and family.revoked_at is None
    assert await _reused_audits(api_env, org_id) == []


async def test_a_used_token_the_sweep_kept_still_ends_its_grant(api_env) -> None:
    """The control: rotated past 29 days ago, the sweep keeps it, and
    presenting it again is still a reuse (design §5.3): the grant ends and
    `mcp.refresh_reused` says so."""
    org_id, _sub, user_id, app = await _person(api_env, "oauth-ret-used-kept")
    family_id, _current = await _open(api_env, org_id, user_id, app)
    _, recent = await _rotated(api_env, org_id, user_id, family_id, used=NOT_YET)
    assert await retention.sweep_org(org_id, api_env.app_engine, now=NOW) == 0

    reused = await token_request(
        api_env, grant_type="refresh_token", refresh_token=recent, client_id=app.client_id
    )

    assert reused.status_code == 400, reused.text
    async with AsyncSession(api_env.admin_engine) as session:
        family = await session.get(OAuthRefreshFamily, family_id)
    assert family is not None and family.revoked_reason == "refresh_reused"
    assert len(await _reused_audits(api_env, org_id)) == 1


async def test_the_used_tokens_step_waits_on_no_token_a_refresh_holds(api_env) -> None:
    """A reuse being presented locks its token (`grants._refresh`, step 3).
    The step skips that one, FOR UPDATE SKIP LOCKED, and takes the other; the
    sweep's transaction runs under a lock timeout and never waits."""
    org_id, _sub, user_id, app = await _person(api_env, "oauth-ret-used-held")
    family_id, _current = await _open(api_env, org_id, user_id, app)
    held, held_secret = await _rotated(api_env, org_id, user_id, family_id, used=LONG_AGO)
    other, _ = await _rotated(
        api_env, org_id, user_id, family_id, used=LONG_AGO - timedelta(days=1)
    )

    async with oauth_session(org_id, api_env.app_engine) as refresh:
        await lock_user_row(refresh, user_id)
        await refresh.execute(
            select(OAuthRefreshToken.id)
            .where(OAuthRefreshToken.token_hash == grants.hash_secret(held_secret))
            .with_for_update()
        )
        async with _sweeping(api_env, org_id) as sweeping:
            swept = await retention.sweep(sweeping, org_id, now=NOW)
        assert swept.counts["oauth_used_refresh_tokens"] == 1

    left = await _ids(api_env, OAuthRefreshToken, org_id)
    assert held in left and other not in left


async def test_the_used_tokens_step_names_its_organization(api_env, bypassrls_session) -> None:
    """Phase 2's decision 3, for this step: on a session RLS does not filter,
    its `org_id` predicate is what keeps it off another organization's
    tokens."""
    mine_org, _s, mine_user, mine_app = await _person(api_env, "oauth-ret-used-a")
    theirs_org, _t, theirs_user, theirs_app = await _person(api_env, "oauth-ret-used-b")
    mine_family, _ = await _open(api_env, mine_org, mine_user, mine_app)
    theirs_family, _ = await _open(api_env, theirs_org, theirs_user, theirs_app)
    mine, _ = await _rotated(api_env, mine_org, mine_user, mine_family, used=LONG_AGO)
    theirs, _ = await _rotated(api_env, theirs_org, theirs_user, theirs_family, used=LONG_AGO)

    swept = await retention.sweep(bypassrls_session, mine_org, now=NOW)
    await bypassrls_session.commit()

    assert swept.counts["oauth_used_refresh_tokens"] == 1
    assert mine not in await _ids(api_env, OAuthRefreshToken, mine_org)
    assert theirs in await _ids(api_env, OAuthRefreshToken, theirs_org)
```

En `backend/tests/test_auth_retention.py`, el diccionario exacto del evento (`:276-286`) gana `"oauth_used_refresh_tokens": 0,` detrás de `"oauth_refresh_tokens": 0,`.

- [ ] **Step 3: Las pruebas fallan**

Run: `.venv/bin/pytest -q -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning tests/test_oauth_retention.py tests/test_auth_retention.py`
Expected: FAIL — `AttributeError: … OAUTH_USED_TOKEN_RETENTION`, `KeyError: 'oauth_used_refresh_tokens'` y los recuentos exactos.

- [ ] **Step 4: El paso**

En `backend/app/core/auth/retention.py`:

1. En la lista del docstring, detrás de la viñeta «an AI app's grant (`oauth_refresh_families`) `OAUTH_GRANT_RETENTION` (1 day) … the act that ended it;»:

```python
- a refresh token a LIVE grant rotated past, `OAUTH_USED_TOKEN_RETENTION` (30
  days) after it was used (operator decision E2, 2026-11-15; own
  authentication phase 4, Task 3). Until then, presented again, it ends its
  grant (design §5.3: "reutilizar uno ya usado revoca toda la familia");
  after it, it is a token the server does not know, a plain `invalid_grant`,
  and the grant lives on (design §5.3, amended 2026-11-15);
```

2. En «THE OAUTH STEPS», la primera viñeta (`:64-72`) queda:

```python
- `oauth_codes`, `oauth_refresh_tokens`, `oauth_used_refresh_tokens` and
  `oauth_grants` hold no person's row and no app row, and WAIT ON NO ROW:
  each statement locks what it deletes FOR UPDATE SKIP LOCKED, with the
  ended (for a used token, the rotated) condition inside it, and
  leaves a row another transaction holds to the next day's sweep. Its
  `org_id` predicate is where it chooses and locks those rows, so on a
  session RLS does not filter it does not even lock another organization's.
  They go leaves first -- the codes; the ended grants' refresh tokens; the
  refresh tokens live grants rotated past more than 30 days ago (since
  operator decision E2, 2026-11-15), whose grants stay; then the ended grants
  no token or code still names -- so no cascade of theirs reaches a row they
  did not lock. Why not plain DELETEs: a revocation's one
```

(la línea siguiente, «UPDATE (`revoked_at IS NULL`) also matches a grant that lapsed unrevoked,», sigue como está).

3. Detrás de `OAUTH_GRANT_RETENTION = timedelta(days=1)`:

```python
#: How long a LIVE grant keeps a refresh token it rotated past, counted from
#: the rotation (operator decision E2, 2026-11-15; own authentication phase
#: 4, Task 3): within it a second presentation is a reuse and ends the grant,
#: after it the token is gone and the presentation a plain `invalid_grant`.
OAUTH_USED_TOKEN_RETENTION = timedelta(days=30)
```

4. Delante de `async def _oauth_grants(session: AsyncSession, org_id: UUID, now: datetime) -> int:`:

```python
async def _oauth_used_refresh_tokens(session: AsyncSession, org_id: UUID, now: datetime) -> int:
    """The refresh tokens a LIVE grant rotated past more than
    `OAUTH_USED_TOKEN_RETENTION` ago (operator decision E2, 2026-11-15); how
    many. Live: neither revoked nor past its idle limit, by the sweep's
    clock -- an ended grant's tokens are `_oauth_refresh_tokens`', the step
    before, a day after it ended, and a grant that ended since its token was
    used is that step's too. Each locked FOR UPDATE SKIP LOCKED (THE OAUTH
    STEPS): one a refresh holds -- a reuse being presented -- is left to the
    next sweep, and the grant row is read, never locked. A token is a leaf
    and its grant stays, so the DELETE cascades nowhere."""
    family = aliased(OAuthRefreshFamily)
    token = aliased(OAuthRefreshToken)
    rotated_long_ago = (
        select(token.id)
        .join(family, family.id == token.family_id)
        .where(
            token.org_id == org_id,
            token.used_at < now - OAUTH_USED_TOKEN_RETENTION,
            family.revoked_at.is_(None),
            family.expires_at > now,
        )
        .with_for_update(of=token, skip_locked=True)
    )
    return await _deleted(
        session,
        delete(OAuthRefreshToken)
        .where(OAuthRefreshToken.id.in_(rotated_long_ago))
        .returning(OAuthRefreshToken.id),
    )


```

5. En `STEPS`, entre `("oauth_refresh_tokens", _oauth_refresh_tokens),` y `("oauth_grants", _oauth_grants),`:

```python
    # Operator decision E2 (2026-11-15; phase 4 plan, Task 3): the refresh
    # tokens live grants rotated past more than 30 days ago. Leaves too, and
    # their grants stay, so before or after the grants step is all one.
    ("oauth_used_refresh_tokens", _oauth_used_refresh_tokens),
```

- [ ] **Step 5: Las pruebas pasan, y cada condición se ve fallar**

Run: `.venv/bin/pytest -q -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning tests/test_oauth_retention.py tests/test_auth_retention.py tests/test_oauth_token.py tests/test_client_isolation.py tests/test_client_catalog.py`
Expected: PASS (32 en los dos primeros archivos en el ensayo).

Mutaciones (una a una, deshaciendo cada una con `git checkout backend/app/core/auth/retention.py` antes de la siguiente; ninguna se commitea). Cada una debe volver roja al menos una de las seis pruebas nuevas, y en el ensayo lo hicieron:
- el paso no borra nada (`("oauth_used_refresh_tokens", lambda s, o, n: _zero())` con `async def _zero() -> int: return 0` junto a él) → `test_a_live_grants_used_tokens_go_thirty_days_after_their_use`;
- sin `family.revoked_at.is_(None),` o sin `family.expires_at > now,` → `test_an_ended_grants_used_tokens_are_not_this_steps`;
- sin `token.org_id == org_id,` → `test_the_used_tokens_step_names_its_organization`;
- `.with_for_update(of=token, skip_locked=True)` sin `skip_locked=True` → `test_the_used_tokens_step_waits_on_no_token_a_refresh_holds` (el barrido se para en el `lock_timeout`);
- `timedelta(days=30)` cambiado a `timedelta(days=28)` → `test_a_used_token_the_sweep_kept_still_ends_its_grant`.

- [ ] **Step 6: El mapa de bloqueos y la lista de alcance**

`backend/app/core/auth/guards.py:219-224`:

```python
# antes
      locks what it deletes FOR UPDATE SKIP LOCKED, the ended condition
      inside it, leaves first: `oauth_codes` expired more than ten minutes
      ago; then (`oauth_refresh_tokens`) the refresh tokens of the grants
      ended more than a day ago that no code still names; then those grants
      (`oauth_grants`, `oauth_refresh_families`) that no token and no code
      still names, so their cascade reaches no row they did not lock. A row
# después
      locks what it deletes FOR UPDATE SKIP LOCKED, the ended (for a used
      token, the rotated) condition inside it, leaves first: `oauth_codes`
      expired more than ten minutes ago; then (`oauth_refresh_tokens`) the
      refresh tokens of the grants ended more than a day ago that no code
      still names; then (`oauth_used_refresh_tokens`, operator decision E2,
      2026-11-15) the refresh tokens a live grant rotated past more than 30
      days ago, whose grant is read, never locked, and stays; then those
      ended grants (`oauth_grants`, `oauth_refresh_families`) that no token
      and no code still names, so their cascade reaches no row they did not
      lock. A row
```

`backend/app/core/auth/oauth/store.py:90-98`:

```python
# antes
THE RETENTION SWEEP TAKES NEITHER ROW, AND WAITS ON NO GRANT ROW (Task 13;
`retention.py`'s THE OAUTH STEPS). Its codes, refresh-token and grants steps
run in one transaction of their own, under `ClientScope.all()`, and skip
steps 1, 2, 3 and 5: they take no person's row, no advisory lock of theirs
and no app row, and write no use. Each statement locks what it deletes FOR
UPDATE SKIP LOCKED, leaves first -- codes, then the ended grants' refresh
tokens, then the ended grants no token or code still names -- so it never
# después
THE RETENTION SWEEP TAKES NEITHER ROW, AND WAITS ON NO GRANT ROW (Task 13;
`retention.py`'s THE OAUTH STEPS). Its codes, refresh-token, used-token and
grants steps run in one transaction of their own, under `ClientScope.all()`,
and skip steps 1, 2, 3 and 5: they take no person's row, no advisory lock of
theirs and no app row, and write no use. Each statement locks what it
deletes FOR UPDATE SKIP LOCKED, leaves first -- codes, then the ended grants'
refresh tokens, then the refresh tokens live grants rotated past more than
30 days ago (operator decision E2, 2026-11-15), then the ended grants no
token or code still names -- so it never
```

`backend/app/core/client_scope.py:277-285`, el motivo de `"app/core/auth/retention.py::sweep_org"`:

```python
    "app/core/auth/retention.py::sweep_org": (
        "worker: own authentication's retention (final review A-M6, operator decision C7; "
        "phase 3 plan 2026-11-06, Task 13) -- one organization's old sessions, forgotten "
        "devices, spent links and old challenges, and its AI apps' spent codes, ended grants "
        "with their refresh tokens and the refresh tokens live grants rotated past 30 days "
        "ago (operator decision E2, 2026-11-15), whose technician rows only an every-client "
        "scope may delete (its idle AI apps go by clients.purge_idle, under oauth_session, and "
        "when the sweep then fails a second transaction writes their audit row alone); every "
        "step carries its own org_id predicate, and the sweep is audited"
    ),
```

- [ ] **Step 7: Las enmiendas y el modelo de seguridad**

`docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §5.3, detrás de la viñeta «**Refresco:** opaco, guardado como hash, …» (`:338-339`):

```markdown
- *Enmienda del 2026-11-15 (decisión del operador E2; plan de la fase 4, Task 3).* Un refresh token ya usado de una
  concesión viva se guarda 30 días desde su uso, y después lo borra el barrido diario (`oauth_used_refresh_tokens`).
  Presentarlo pasados esos 30 días es un `invalid_grant` simple, como un token que el servidor no conoce: ya no
  revoca la familia. RFC 9700 §4.14.2 basa la detección en que el servidor guarda la relación del token invalidado;
  E2 le pone un límite. Lo que cuesta: si alguien copió el token y refrescó antes que la app, y la app vuelve con su
  copia pasados 30 días, la app recibe `invalid_grant` y la concesión copiada sigue hasta que se revoca o pasa 30
  días sin uso. Lo dice el riesgo residual del runbook «A used refresh token is remembered for 30 days».
```

`docs/03-security-model.md:164`:

```markdown
  refresh token that rotates and whose reuse, within 30 days of its use, ends
  the grant (operator decision E2, 2026-11-15).
```

(sustituye a «refresh token that rotates and whose reuse ends the grant.»), y `:206-209`:

```markdown
  WebAuthn challenges after a day; since phase 3, OAuth authorization codes
  10 minutes after they expire, used or not, ended AI-app grants after a day
  (their refresh tokens with them), and AI apps unused for 30 days; since
  2026-11-15 (operator decision E2), the refresh tokens a live grant rotated
  past, 30 days after their use -- presenting one after that is a plain
  `invalid_grant`, not a reuse that ends the grant; each
  daily sweep that deletes anything is audited (`auth.retention_applied`).
```

- [ ] **Step 8: El riesgo residual**

En `deploy/README.md`, `### Residual risks, stated plainly`, la frase de cabecera (`:2597-2599`) queda:

```markdown
**Risks 3, 7 and 8 exist only while self-registration is open**
(`ITS_MCP_REGISTRATION=open`); closing it ends them for new apps. **Risks 1,
2, 4, 10 and 11 exist whenever MCP is on**, because an app known by its
client metadata document needs no opt-in. Risks 5, 6, 9 and 12 hold always.
```

y detrás del riesgo 11 (que termina «… it is the same person, in their own session.»):

```markdown
12. **A used refresh token is remembered for 30 days** (operator decision
    E2, 2026-11-15). Within them, presenting it again ends the app's grant
    (risk 9). After them the platform has forgotten it: presenting it is a
    plain `invalid_grant`, and the grant lives on. So a token copied and used
    by someone else first is caught only if the real app tries its own copy
    within 30 days of the theft; an app that comes back later is simply asked
    to connect again, while the copy goes on working until its grant is
    revoked or sits unused for 30 days. Settings → AI connections lists every
    app holding a live grant. If an app asks you to connect again when you
    did not expect it, use **Revoke for me** on it there before reconnecting:
    that ends every grant of yours to that app, the copy included.
```

(La Task 9 quita los riesgos 7 y 8 y renumera; su texto de cabecera ya cuenta con el 12.)

- [ ] **Step 9: Tipos, estilo y commit**

Run: `.venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: limpio.

```bash
git add backend/app/core/auth/retention.py backend/app/core/auth/guards.py \
  backend/app/core/auth/oauth/store.py backend/app/core/client_scope.py \
  backend/tests/test_oauth_retention.py backend/tests/test_auth_retention.py \
  docs/03-security-model.md docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md deploy/README.md
git commit -m "feat(auth): the daily sweep deletes a live grant's used refresh tokens after 30 days (E2)"
```

---


### Task 4: El validador de Keycloak se va

**Files:**
- Delete: `backend/app/core/security.py`
- Modify: `backend/app/core/config.py:32-56` (`oidc_*`, `oidc_console_client_id`, `multi_tenant`; `default_org_slug` se queda con su comentario nuevo)
- Modify: `backend/app/api/deps.py:52`, `:129-131` (`jwks`)
- Modify: `backend/app/main.py:12`, `:23`, `:159-163`, `:207`
- Modify: `backend/app/api/v1/orgs.py:74`, `:81`, `:272-274` (`NEVER_ORG_SETTINGS`)
- Modify: `backend/app/core/i18n/es.py` (siete entradas: `:159-168`, `:5233-5238`)
- Modify: `backend/app/core/auth/oauth/access.py:29-33` (comentario)
- Delete: `backend/tests/test_security.py`, `backend/tests/fake_idp.py`
- Create: `backend/tests/foreign_tokens.py`
- Modify (guion, Step 4): `backend/tests/conftest.py`, `test_setup_api.py`, `test_harness_connection_hygiene.py`, `test_auth_session_door.py`, `test_backup_e2e_minio.py`, `test_auth_signin.py`, `test_token_origin.py`, `test_mcp_server.py`, `test_logging_kernel.py:591`, `test_error_capture_wiring.py:91`, y los 72 archivos con `oidc_issuer=`/`oidc_jwks_url=` en un `Settings(...)` (`test_platform_health_sampler.py` incluido)
- Modify (guion, Step 4): `backend/app/core/identity/email_domain.py:38`, `backend/app/api/v1/orgs.py:24` (frases en presente que nombraban `security.py` o el claim `org` de OIDC)
- Rewrite: `backend/tests/test_config.py`
- Test: `backend/tests/test_org_settings.py` (una prueba al final)

**Interfaces:**
- Depende de: nada nuevo. Nada en `app/` llama `validate_token`, `require_person_mcp_token` ni `deps.jwks` desde la fase 3 (`config.py:41-44` lo dice).
- Produce: `tests/foreign_tokens.py` (`ForeignIssuer`, `ISSUER`, `CONSOLE_CLIENT_ID`); `ApiEnv.foreign` en lugar de `ApiEnv.idp`; `create_app(settings: Settings | None = None) -> FastAPI` (Apéndice A).

**La sonda `jwks`.** `probe_jwks` lee `app.state.jwks`, que esta tarea quita: en esta rama sola la sonda leería `down`. La Task 5 la borra, y el coordinador fusiona la 5 antes que la 4 (notas de integración): el árbol fusionado nunca tiene una sonda que lea un atributo que no existe (P4-32).

- [ ] **Step 1: Preparar la worktree**

Task 1, Step 1, y además fusionar la rama del coordinador con la oleada 1 y comprobar `grep -c oauth_used_refresh_tokens backend/app/core/auth/retention.py` (un número mayor que 0).

- [ ] **Step 2: Las pruebas que fallan**

Crear `backend/tests/foreign_tokens.py`:

```python
"""Tokens from an issuer the platform does not trust (own authentication phase 4).

Until phase 4 this was `tests/fake_idp.py`: a fake Keycloak whose JWKS the
test app was given, so `app.core.security.validate_token` could verify what
it minted. Keycloak and that validator are gone, and so is the JWKS: nothing
in the platform fetches a key for this issuer, so a token minted here is what
a token from Keycloak -- or from anyone else -- now is to every door, signed
by a key the platform has never seen.

THE TESTS THAT MINT ONE ARE THE REFUSALS. Every door that once read a
Keycloak token (`/api/v1`, the console socket, `/mcp`) still has a test
sending one, with every claim Keycloak put in it, and expecting the door's
refusal: those tests keep pinning that refusal now that nothing on the
platform's side can accept the token at all. (The remote tunnel reads no
bearer since own authentication phase 1; its tests send cookies.) Design §10,
falsifier 11 (`tests/test_auth_phase4_falsifiers.py`) walks every route with one.
"""

import time
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

#: Where Keycloak's realm lived; the `iss` such a token carries.
ISSUER = "https://idp.test/realms/it-suite"
#: The client the console's SPA signed in with at Keycloak until own
#: authentication phase 1: the default `azp` below.
CONSOLE_CLIENT_ID = "it-suite-web"


class ForeignIssuer:
    """An RS256 key of its own, which no part of the platform knows."""

    def __init__(self, kid: str = "test-key-1") -> None:
        self.kid = kid
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def public_jwk(self) -> dict[str, Any]:
        """This key's public half as a JWK, for a test that plays a JWKS
        server (falsifier 11's positive control)."""
        jwk: dict[str, Any] = RSAAlgorithm.to_jwk(self._key.public_key(), as_dict=True)
        return {**jwk, "kid": self.kid, "alg": "RS256", "use": "sig"}

    @property
    def private_pem(self) -> bytes:
        return self._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    def token(
        self,
        sub: str,
        org_slug: str | None = "toeshee",
        *,
        aud: str = "it-suite",
        iss: str = ISSUER,
        exp_in: int = 300,
        email: str = "user@example.com",
        name: str = "Test User",
        azp: str | None = CONSOLE_CLIENT_ID,
        typ: str | None = "Bearer",
        sid: str | None = "fake-session",
        extra: dict[str, Any] | None = None,
    ) -> str:
        """An access token as Keycloak issued one: `typ` "Bearer", `azp` the
        client that obtained it (the console's, unless told otherwise), `sid`
        the person's sign-in session, `org` the organization's slug.
        `azp=None` / `typ=None` / `sid=None` omit the claim."""
        claims: dict[str, Any] = {
            "sub": sub,
            "aud": aud,
            "iss": iss,
            "iat": int(time.time()),
            "exp": int(time.time()) + exp_in,
            "email": email,
            "name": name,
        }
        if org_slug is not None:
            claims["org"] = org_slug
        if azp is not None:
            claims["azp"] = azp
        if typ is not None:
            claims["typ"] = typ
        if sid is not None:
            claims["sid"] = sid
        if extra:
            claims.update(extra)
        return jwt.encode(claims, self.private_pem, algorithm="RS256", headers={"kid": self.kid})
```

Reescribir `backend/tests/test_config.py` entero:

```python
from pathlib import Path

import pytest

from app.core.config import Settings, get_settings, override_settings

#: Every `.env` key own authentication phase 4 made obsolete, with a value a
#: deployment upgraded from the Keycloak era could hold (deploy/.env.example
#: before 2026-11-15). The api must start on such a `.env` as it is.
KEYCLOAK_ERA_ENV = {
    "ITS_OIDC_ISSUER": "https://itms.example.com/realms/it-suite",
    "ITS_OIDC_JWKS_URL": "http://keycloak:8080/realms/it-suite/protocol/openid-connect/certs",
    "ITS_OIDC_AUDIENCE": "it-suite",
    "ITS_OIDC_ORG_CLAIM": "org",
    "ITS_OIDC_CONSOLE_CLIENT_ID": "it-suite-web",
    "ITS_MULTI_TENANT": "true",
    "ITS_KEYCLOAK_ADMIN_URL": "http://keycloak:8080",
    "ITS_KEYCLOAK_REALM": "it-suite",
    "ITS_KEYCLOAK_EVENTS_CLIENT_ID": "it-suite-events",
    "ITS_KEYCLOAK_EVENTS_SECRET_PATH": "/var/lib/its/secrets/keycloak-events.secret",
    "ITS_KEYCLOAK_ADMIN_CLIENT_ID": "it-suite-provisioning",
    "ITS_KEYCLOAK_ADMIN_SECRET_PATH": "/var/lib/its/secrets/keycloak-admin.secret",
    "KEYCLOAK_ADMIN_USER": "admin",
    "KEYCLOAK_ADMIN_PASSWORD": "not-a-real-password",
    "ENTRA_TENANT_ID": "",
    "ENTRA_CLIENT_ID": "",
    "ENTRA_CLIENT_SECRET": "",
}


def _required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ITS_DATABASE_URL", "postgresql+asyncpg://app_user:app@localhost/x")
    monkeypatch.setenv("ITS_DATABASE_ADMIN_URL", "postgresql+asyncpg://admin:a@localhost/x")
    monkeypatch.setenv("ITS_CA_DIR", "/tmp/ca")


def test_settings_load_from_env(monkeypatch) -> None:
    _required(monkeypatch)
    s = Settings()
    assert s.database_url.endswith("/x")
    assert s.signing_backend == "local"
    assert s.ca_dir == Path("/tmp/ca")
    assert s.job_max_attempts == 3
    assert s.gateway_relay_url == "wss://localhost:8443/relay"  # default
    assert s.default_org_slug is None  # default
    # guacd is retired: nothing reads its settings, and an old .env naming
    # them is ignored rather than fatal.
    assert not hasattr(s, "remote_guacd_host")


def test_a_keycloak_era_env_is_read_and_ignored(monkeypatch) -> None:
    """Own authentication phase 4 removed every Keycloak and OIDC setting. A
    `.env` that still holds them -- every deployment upgraded from before --
    must start exactly as one without them. Compose hands `.env` to the
    containers as environment variables, and pydantic-settings reads one only
    for a field that exists; what this pins is that no such field is left."""
    _required(monkeypatch)
    for key, value in KEYCLOAK_ERA_ENV.items():
        monkeypatch.setenv(key, value)
    s = Settings()
    fields = set(Settings.model_fields)
    assert {f for f in fields if f.startswith("oidc_")} == set()
    assert "multi_tenant" not in fields
    assert not hasattr(s, "oidc_issuer")
    assert s.default_org_slug is None


def test_a_keycloak_era_dotenv_file_is_read_and_ignored(monkeypatch, tmp_path) -> None:
    """The same keys from a dotenv file, which is where `extra` matters: with
    `extra="forbid"` this construction raises, and `extra="ignore"` is what
    keeps an upgraded `.env` read this way from stopping the process."""
    _required(monkeypatch)
    dotenv = tmp_path / "keycloak-era.env"
    dotenv.write_text("".join(f"{key}={value}\n" for key, value in KEYCLOAK_ERA_ENV.items()))
    s = Settings(_env_file=dotenv)
    assert s.default_org_slug is None
    assert not hasattr(s, "oidc_issuer")


def test_override_settings_swaps_cached_instance(monkeypatch) -> None:
    _required(monkeypatch)
    base = Settings()
    with override_settings(base.model_copy(update={"default_org_slug": "other"})):
        assert get_settings().default_org_slug == "other"
    assert get_settings().default_org_slug is None
```

Al final de `backend/tests/test_org_settings.py`:

```python


def test_every_name_kept_out_of_org_settings_is_a_real_setting() -> None:
    """A name in `NEVER_ORG_SETTINGS` that no `Settings` field carries guards
    nothing. Own authentication phase 4 removed three (`oidc_issuer`,
    `oidc_jwks_url`, `oidc_audience`) with Keycloak's settings, and named the
    sign-in's own anchors in their place: the console's address (the OAuth
    issuer and the CSRF origin), the sign-in keys, and the organization the
    deployment signs people in to (plan 2026-11-15, P4-08)."""
    from app.core.config import Settings

    fields = set(Settings.model_fields)
    assert NEVER_ORG_SETTINGS <= fields, sorted(NEVER_ORG_SETTINGS - fields)
    assert {
        "public_console_url",
        "auth_keys_backend",
        "auth_keys_dir",
        "default_org_slug",
    } <= NEVER_ORG_SETTINGS
```

Run: `.venv/bin/pytest -q tests/test_config.py tests/test_org_settings.py`
Expected: 5 FAIL y 19 PASS (en el ensayo de la ronda de correcciones): `test_a_keycloak_era_env_is_read_and_ignored` y `test_a_keycloak_era_dotenv_file_is_read_and_ignored` (los campos `oidc_*` y `multi_tenant` existen), `test_settings_load_from_env` y `test_override_settings_swaps_cached_instance` (`ValidationError`: `oidc_issuer` y `oidc_jwks_url` aún son obligatorios y la prueba nueva ya no los pasa), y `test_every_name_kept_out_of_org_settings_is_a_real_setting` (las cuatro anclas no están en la lista).

- [ ] **Step 3: El validador y sus ajustes**

`git rm backend/app/core/security.py`

`backend/app/core/config.py`: el bloque desde `    oidc_issuer: str` hasta `    multi_tenant: bool = False` (`:32-56`, ambos incluidos) se sustituye por:

```python
    # The organization this deployment signs people in to (`flows.login`, the
    # OAuth server's `store.deployment_org`, the setup claim's slug and the
    # recovery command). One organization per deployment. The Keycloak-era
    # `ITS_OIDC_*` and `ITS_MULTI_TENANT` settings went with Keycloak in own
    # authentication phase 4; a `.env` that still sets them starts as before:
    # the containers get them as environment variables, which are read only
    # for a field that exists, and a dotenv file's extra keys are dropped by
    # `extra="ignore"` (`tests/test_config.py`, both ways).
    default_org_slug: str | None = None
```

(La línea en blanco que sigue en el archivo, `:57`, se queda: el bloque no trae la suya.)

`backend/app/api/deps.py`: se borra la línea `from app.core.security import JwksCache` (`:52`) y la función entera

```python
def jwks(request: Request) -> JwksCache:
    cache: JwksCache = request.app.state.jwks
    return cache
```

(`:129-131`, con las dos líneas en blanco que la siguen).

`backend/app/main.py`: se borran `import httpx` (`:12`), `from app.core.security import JwksCache` (`:23`) y `    app.state.jwks = JwksCache(settings.oidc_jwks_url, client=jwks_http_client)` (`:207`); la firma (`:159-163`) queda `def create_app(settings: Settings | None = None) -> FastAPI:`.

`backend/app/api/v1/orgs.py`: en el docstring, `  * OIDC issuer/JWKS/audience -- the identity this route's own auth rests on` (`:74`) pasa a

```python
  * the sign-in's own anchors (`ITS_PUBLIC_CONSOLE_URL`, which is the OAuth
    issuer and the origin every console write is checked against;
    `ITS_AUTH_KEYS_BACKEND`, `ITS_AUTH_KEYS_DIR`; `ITS_DEFAULT_ORG_SLUG`) --
    the identity this route's own auth rests on
```

y `signing key or the OIDC issuer is a FLEET-level action.` (`:81`) pasa a `signing key or the sign-in's issuer is a FLEET-level action.`. En `NEVER_ORG_SETTINGS`, las tres líneas `"oidc_issuer",`, `"oidc_jwks_url",`, `"oidc_audience",` (`:272-274`) se sustituyen por:

```python
        "public_console_url",
        "auth_keys_backend",
        "auth_keys_dir",
        "default_org_slug",
```

`backend/app/core/i18n/es.py`: se borran las siete entradas que solo usaba `security.py` — `"no JWKS key for kid {kid}"`, `"token has no kid header"`, `"this token is not an access token"`, `"token missing required {oidc_org_claim} claim in multi-tenant mode"` (con su paréntesis de dos líneas), `"token missing {oidc_org_claim} claim and no default org configured"` (con el suyo, de tres), `"this token was issued to the console and is not accepted over MCP"` y `"MCP access must be granted by a person signing in"` (cada una con su paréntesis). `tests/test_i18n_catalog.py::test_no_stale_catalogue_entries` las exige fuera.

`backend/app/core/auth/oauth/access.py:30-33`:

```python
# antes
process -- `JwksCache`'s defence for Keycloak's keys (`app/core/security.py`)
-- so a token signed just after a rotation is still admitted at once, unless
a stranger's token with an invented `kid` armed the interval first: then for
up to `REFRESH_EVERY_S` seconds. Acceptable, and said so (plan D14).
# después
process -- the defence Keycloak's key cache had (`JwksCache`, removed in own
authentication phase 4) -- so a token signed just after a rotation is still
admitted at once, unless a stranger's token with an invented `kid` armed the
interval first: then for up to `REFRESH_EVERY_S` seconds. Acceptable, and
said so (plan D14).
```

- [ ] **Step 4: Las pruebas, con un guion**

Las ediciones son mecánicas y muchas (24 `.idp` → `.foreign`; las dos líneas `oidc_*` de cada `Settings(...)` en 72 archivos; las frases que aún nombraban `app/core/security.py` en presente, y los dos títulos de sección que las pruebas borradas dejan vacíos en `test_token_origin.py`), así que van en un guion que comprueba cada ancla. Al final borra `tests/test_security.py` y `tests/fake_idp.py` (Apéndice C, primera fila: lo que protegían lo prueban `tests/test_oauth_access_token.py` y el falsificador 11). Guárdalo fuera del repositorio, en `"${TMPDIR:-/tmp}/p4_task4_tests.py"`, y córrelo desde la raíz de la worktree con `python3 "${TMPDIR:-/tmp}/p4_task4_tests.py"`:

```python
"""Own authentication phase 4, Task 4: the tests stop minting Keycloak tokens a
JWKS could verify, and stop configuring the Keycloak-era settings.

Run from the worktree root: python3 p4_task4_tests.py
Every edit asserts its anchor is found exactly once, so a tree that moved
since dd062f8 stops here instead of being half-edited.
"""

import ast
import re
from pathlib import Path

TESTS = Path("backend/tests")


def swap(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert text.count(old) == 1, (path, old[:70], text.count(old))
    path.write_text(text.replace(old, new))


# --- the shared environment -------------------------------------------------- #
conftest = TESTS / "conftest.py"
swap(
    conftest,
    "    def __init__(self, app, client, idp, settings, admin_engine, app_engine):\n",
    "    def __init__(self, app, client, foreign, settings, admin_engine, app_engine):\n",
)
swap(conftest, "        self.idp = idp\n", "        self.foreign = foreign\n")
swap(
    conftest,
    """    from tests.fake_idp import ISSUER, JWKS_URL, FakeIdp, jwks_client
    from tests.session_support import CONSOLE

    # `idp` and `jwks_client(idp)` stay until phase 4: the tests of
    # `security.validate_token`, and those that prove a Keycloak token is
    # refused everywhere, still mint one.
    idp = FakeIdp()
""",
    """    from tests.foreign_tokens import ForeignIssuer
    from tests.session_support import CONSOLE

    # Keycloak-shaped tokens no part of the platform can verify: the tests
    # that prove such a token is refused at every door mint them from here
    # (`tests/foreign_tokens.py`, own authentication phase 4).
    foreign = ForeignIssuer()
""",
)
swap(
    conftest,
    """        oidc_issuer=ISSUER,
        oidc_jwks_url=JWKS_URL,
        ca_dir=tmp_path / "ca",""",
    """        ca_dir=tmp_path / "ca",""",
)
swap(
    conftest,
    "    app = create_app(settings, jwks_http_client=jwks_client(idp))\n",
    "    app = create_app(settings)\n",
)
swap(
    conftest,
    "            yield ApiEnv(app, client, idp, settings, admin_engine, app_engine)\n",
    "            yield ApiEnv(app, client, foreign, settings, admin_engine, app_engine)\n",
)

setup = TESTS / "test_setup_api.py"
swap(setup, "    from tests.fake_idp import ISSUER, JWKS_URL, FakeIdp, jwks_client\n", "")
swap(setup, "        oidc_issuer=ISSUER,\n        oidc_jwks_url=JWKS_URL,\n", "")
swap(
    setup,
    "    app = create_app(settings, jwks_http_client=jwks_client(FakeIdp()))\n",
    "    app = create_app(settings)\n",
)
swap(TESTS / "test_harness_connection_hygiene.py", "    from tests.fake_idp import ISSUER, JWKS_URL\n", "")
swap(
    TESTS / "test_auth_session_door.py",
    '    bob_token = api_env.idp.token(bob, await api_env.org_slug(org_id), email=f"{bob}@test.local")\n',
    "    slug = await api_env.org_slug(org_id)\n"
    '    bob_token = api_env.foreign.token(bob, slug, email=f"{bob}@test.local")\n',
)

# `.idp` -> `.foreign`, in every test that mints a Keycloak-shaped token.
RENAME = re.compile(r"\b(api_env|mcp_env|auth_env)\.idp\b")
renamed = 0
for path in sorted(TESTS.glob("*.py")):
    text = path.read_text()
    new, n = RENAME.subn(r"\1.foreign", text)
    if n:
        path.write_text(new)
        renamed += n
print("renamed .idp -> .foreign:", renamed)

# The two `oidc_*` keyword lines every `Settings(...)` carried. One file is
# left to Task 5, which deletes it, and test_token_origin.py to the step after
# this one.
KWARG = re.compile(r'^[ \t]+oidc_(issuer|jwks_url)=("[^"\n]*"|ISSUER|JWKS_URL),[ \t]*\n', re.M)
LEFT = {"test_keycloak_event_sweep.py", "test_token_origin.py"}
stripped = 0
for path in sorted(TESTS.glob("*.py")):
    if path.name in LEFT:
        continue
    text = path.read_text()
    new, n = KWARG.subn("", text)
    if n:
        path.write_text(new)
        stripped += n
print("oidc_* keyword lines removed:", stripped)
swap(
    TESTS / "test_backup_e2e_minio.py",
    '        oidc_issuer="https://i", oidc_jwks_url="https://i/certs", ca_dir=tmp_path,\n',
    "        ca_dir=tmp_path,\n",
)
swap(
    TESTS / "test_auth_signin.py",
    """    "oidc_issuer": "https://kc.example/realms/it-suite",
    "oidc_jwks_url": "https://kc.example/realms/it-suite/certs",
""",
    "",
)

# --- test_token_origin.py ------------------------------------------------------ #
origin = TESTS / "test_token_origin.py"
text = origin.read_text()
GONE = {
    "_settings",
    "test_the_console_client_id_defaults_to_the_spa_client",
    "test_the_console_client_id_is_read_from_the_environment",
    "test_a_token_that_is_not_an_access_token_is_refused",
    "test_a_token_with_no_typ_is_refused",
    "test_an_id_token_carrying_the_console_audience_is_refused",
    "test_a_bearer_token_is_admitted_and_carries_its_origin",
    "_claims",
    "test_require_person_mcp_token",
    "test_the_legacy_client_id_claim_marks_a_service_account",
    "test_the_sweep_catches_a_validation_with_no_origin_rule",
    "test_every_validate_token_call_in_app_is_followed_by_an_origin_rule",
}
lines = text.splitlines(keepends=True)
spans = []
for node in ast.parse(text).body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in GONE:
        start = (node.decorator_list[0].lineno if node.decorator_list else node.lineno) - 1
        spans.append((start, node.end_lineno))
assert len(spans) == len(GONE), sorted(GONE)
for start, end in sorted(spans, reverse=True):
    del lines[start:end]
text = "".join(lines)
RULE = "# " + "-" * 75 + " #\n"
for banner in ("# The settings default\n", "# validate_token: access tokens only\n"):
    block = RULE + banner + RULE
    assert text.count(block) == 1, banner
    text = text.replace(block, "")
origin.write_text(re.sub(r"\n{4,}", "\n\n\n", text))

swap(
    origin,
    """- `validate_token` admits only access tokens: `typ` exactly "Bearer", as
  Keycloak issues every access token. ID, refresh, offline and logout tokens,
  and a token with no `typ`, are refused on every door;
""",
    """- until own authentication phase 4, `security.validate_token` admitted only
  access tokens (`typ` exactly "Bearer", as Keycloak issued every access
  token). The validator went with Keycloak; the tokens below are refused at
  every door now because nothing on the platform can verify them at all,
  and each is still sent (`tests/foreign_tokens.py`);
""",
)
swap(
    origin,
    """from app.core.security import (
    JwksCache,
    TokenClaims,
    TokenValidationError,
    require_person_mcp_token,
    validate_token,
)
""",
    "",
)
swap(origin, "from app.core.config import Settings\n", "")
swap(
    origin,
    "from tests.fake_idp import CONSOLE_CLIENT_ID, ISSUER, JWKS_URL, FakeIdp, jwks_client\n",
    "from tests.foreign_tokens import ForeignIssuer\n",
)
swap(
    origin,
    '''def _id_token(idp: FakeIdp, sub: str, slug: str | None = "toeshee", **kw: Any) -> str:
    """What Keycloak issues as the ID token of a client registered as
    `it-suite`: `typ` "ID", `aud` and `azp` both that client id."""
    return idp.token(sub, slug, aud="it-suite", azp="it-suite", typ="ID", **kw)
''',
    '''def _id_token(
    issuer: ForeignIssuer, sub: str, slug: str | None = "toeshee", **kw: Any
) -> str:
    """What Keycloak issued as the ID token of a client registered as
    `it-suite`: `typ` "ID", `aud` and `azp` both that client id."""
    return issuer.token(sub, slug, aud="it-suite", azp="it-suite", typ="ID", **kw)
''',
)
swap(origin, "    api_env.settings.oidc_console_client_id = azp\n", "")
swap(
    origin,
    '''APP = Path(__file__).resolve().parents[1] / "app"
ORIGIN_RULES = frozenset({"require_person_mcp_token"})
''',
    '''APP = Path(__file__).resolve().parents[1] / "app"

#: The Keycloak validator's names (`app/core/security.py`, removed in phase 4).
VALIDATOR_NAMES = frozenset(
    {"validate_token", "JwksCache", "TokenClaims", "require_person_mcp_token"}
)


def _validator_uses(source: str) -> list[int]:
    """Lines of `source` whose CODE names the validator: an import of
    `app.core.security`, or one of its names. Comments and docstrings are not
    code, and the history they tell is allowed."""
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module == "app.core.security":
            found.append(node.lineno)
        elif isinstance(node, ast.Import) and any(
            alias.name == "app.core.security" for alias in node.names
        ):
            found.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id in VALIDATOR_NAMES:
            found.append(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr in VALIDATOR_NAMES:
            found.append(node.lineno)
    return found


def test_the_validator_sweep_catches_a_use() -> None:
    """Non-vacuity: an import and a call are found; a comment is not."""
    bad = "from app.core.security import validate_token\\nvalidate_token(t)\\n"
    assert _validator_uses(bad) == [1, 2]
    assert _validator_uses("# validate_token( was Keycloak's\\nx = 1\\n") == []


def test_no_door_can_validate_a_keycloak_token_any_more() -> None:
    """Was `test_every_validate_token_call_in_app_is_followed_by_an_origin_rule`
    (and its non-vacuity twin), which held every `security.validate_token`
    site to an origin rule and, since phase 3, counted none. Own
    authentication phase 4 removed the validator, its JWKS cache and its
    origin rules; what the sweep protected -- that no door admits a token the
    platform's own servers did not issue -- is now that nothing in `app/`
    could: the module is gone and no code names it."""
    assert not (APP / "core" / "security.py").exists()
    left = [
        f"{path.relative_to(APP.parent)}:{line}"
        for path in sorted(APP.rglob("*.py"))
        for line in _validator_uses(path.read_text())
    ]
    assert left == [], left
''',
)
swap(
    origin,
    '''def _unruled_validations(
    source: str, call: str = "validate_token", rules: frozenset[str] = ORIGIN_RULES
) -> list[int]:''',
    """def _unruled_validations(source: str, call: str, rules: frozenset[str]) -> list[int]:""",
)
for name in (
    'NOT_ACCESS_EN = "this token is not an access token"\n',
    'NOT_ACCESS_ES = "este token no es un token de acceso"\n',
    'CONSOLE_OVER_MCP_EN = "this token was issued to the console and is not accepted over MCP"\n',
    'NOT_A_PERSON_EN = "MCP access must be granted by a person signing in"\n',
    'NOT_A_PERSON_ES = "el acceso por MCP debe concederlo una persona al iniciar sesión"\n',
):
    swap(origin, name, "")
swap(
    origin,
    """        # Proven live (deploy/keycloak/ci_mcp_flow.py): a person's token always
        # carries `sid`, a `client_credentials` token never does.
""",
    """        # Proven live against Keycloak 26.2 by the CI script of the time
        # (`deploy/keycloak/ci_mcp_flow.py`, removed in own authentication
        # phase 4): a person's token always carried `sid`, a
        # `client_credentials` token never did.
""",
)
remaining = KWARG.subn("", origin.read_text())
origin.write_text(remaining[0])
print("test_token_origin.py: oidc_* lines removed:", remaining[1])

# --- test_mcp_server.py: the multi-tenant switch went with Keycloak --------- #
swap(
    TESTS / "test_mcp_server.py",
    '''    """The organization is the token's `org`, which the platform signed. Without
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
''',
    '''    """The organization is the token's `org`, which the platform signed. Without
    it the token does not verify, whatever `ITS_DEFAULT_ORG_SLUG` says. (Until
    own authentication phase 4 this also ran with the multi-tenant switch on
    and off; the switch was Keycloak's, and went with it.)"""
    org_id, slug = await _org(mcp_env, mcp_enabled=True)
    sub = await mcp_env.create_user(org_id, "org-admin")
    mcp_env.settings.default_org_slug = slug
    claims = mcp_claims(await mcp_token(mcp_env, slug, sub))
    del claims["org"]
    async with mcp_client(mcp_env) as client:
        resp = await client.post(
            "/mcp", json=INITIALIZE, headers=_bearer(await signed_mcp_token(claims))
        )
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == REFUSED_CHALLENGE
''',
)
# --- the sentences that still name the removed validator in the present ------- #
# (review 2, M10). Each says now what it said then, dated.
swap(
    Path("backend/app/core/identity/email_domain.py"),
    """    a second parser for them buys nothing here. An absent ``email`` claim
    arrives as ``""`` (``app/core/security.py``), which has no ``@`` and so
    fails closed.
""",
    """    a second parser for them buys nothing here. An empty address -- once an
    absent ``email`` claim of a Keycloak token (``app/core/security.py``,
    removed in own authentication phase 4) -- has no ``@`` and so fails
    closed.
""",
)
swap(
    Path("backend/app/api/v1/orgs.py"),
    """`slug` and `status` are READ-ONLY here. The slug is the tenant's identity in
the OIDC `org` claim (`app.api.deps.auth_context` resolves the org BY SLUG,
before any tenant context exists) -- renaming it through the console would log
""",
    """`slug` and `status` are READ-ONLY here. The slug is the tenant's identity at
sign-in: `ITS_DEFAULT_ORG_SLUG` names the organization people sign in to and
AI apps connect to, by its slug (until own authentication phase 4, also the
`org` claim of a Keycloak token) -- renaming it through the console would log
""",
)
swap(
    TESTS / "test_setup_api.py",
    """    \"\"\"`ITS_DEFAULT_ORG_SLUG` is what `app/core/security.py` resolves a token to
    when the org claim is absent, which is every token on a single-org install.
""",
    """    \"\"\"`ITS_DEFAULT_ORG_SLUG` is the organization `POST /auth/login` signs
    people in to (until own authentication phase 4 also what a Keycloak token
    with no org claim resolved to), which is everyone on a single-org install.
""",
)
swap(
    TESTS / "test_logging_kernel.py",
    "# bearer-token shape (`app/core/security.py`). Module-level so every test\n",
    "# bearer-token shape (`app/core/auth/oauth/access.py`). Module-level so every test\n",
)
swap(
    TESTS / "test_error_capture_wiring.py",
    """    binding line at all -- `validate_token` raises first, which FastAPI's
""",
    """    binding line at all -- the door refuses first (`validate_token`, until own
    authentication phase 4), which FastAPI's
""",
)
for gone in ("test_security.py", "fake_idp.py"):
    (TESTS / gone).unlink()
print("ok")
```

Expected: `renamed .idp -> .foreign: 24`, `oidc_* keyword lines removed: 156`, `test_token_origin.py: oidc_* lines removed: 0`, `ok`. (El archivo que deja con líneas `oidc_*`, `test_keycloak_event_sweep.py`, es de la Task 5, que lo borra; con `extra="ignore"` sigue pasando mientras tanto. `test_platform_health_sampler.py` sí es de esta tarea: así la Task 5 sola no depende de que `oidc_issuer` deje de ser obligatorio — revisión 2, I1.)

Run: `grep -rlnI --include='*.py' "oidc_\|fake_idp\|\.idp\b\|jwks_http_client\|multi_tenant" backend/app backend/tests`
Expected (en el ensayo, en este orden con `sort`): `ai_admin_fakes.py` y `test_job_cancel.py` (la palabra `oidc_sub` en un docstring), `foreign_tokens.py` (docstrings), `test_config.py`, `test_deploy_keycloak_registration_edge.py` (un nombre de prueba con `oidc`), `test_keycloak_event_sweep.py` (de la Task 5), `test_org_settings.py` y `test_token_origin.py` (un docstring que nombra `oidc_console_client_id` como historia); ninguno de `backend/app`. `-I --include='*.py'`: los `.pyc` que dejó el Step 2 también contienen `oidc_` (revisión 2, M3).

- [ ] **Step 5: Las pruebas pasan**

Run: `.venv/bin/pytest -q -W error::RuntimeWarning tests/test_token_origin.py tests/test_mcp_server.py tests/test_config.py tests/test_org_settings.py tests/test_setup_api.py tests/test_auth_session_door.py tests/test_auth_signin.py tests/test_harness_connection_hygiene.py tests/test_auth_falsifiers.py tests/test_ui_ws.py tests/test_auth_my_security.py tests/test_mcp_channel_api.py tests/test_auth_csrf.py tests/test_mcp_own_tokens.py tests/test_i18n_catalog.py tests/test_api_lifespan.py tests/test_auth_keys.py tests/test_portal_session.py tests/test_logging_discipline.py`
Expected: PASS (412 en el ensayo de la ronda de correcciones, sobre la oleada 1). Después, los archivos de prueba que la tarea cambió, desde `backend/` (las rutas de `git diff` son relativas a la carpeta con `--relative`, y `--diff-filter=M` deja fuera `tests/test_security.py`, que ya no existe): `.venv/bin/pytest -q $(git diff --name-only --diff-filter=M --relative -- tests/ | grep '^tests/test_')` (83 archivos; 1.325 pasan y 1 se salta en el ensayo).

- [ ] **Step 6: Tipos, estilo y commit**

Run: `.venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: limpio (556 archivos en el ensayo).

```bash
git add -A backend/app/core/security.py backend/app/core/config.py backend/app/api/deps.py \
  backend/app/main.py backend/app/api/v1/orgs.py backend/app/core/i18n/es.py \
  backend/app/core/auth/oauth/access.py backend/app/core/identity/email_domain.py backend/tests
git commit -m "refactor(auth): the Keycloak token validator and its settings go (phase 4)"
```

---

### Task 5: Los clientes de administración, el reenvío de eventos, sus sondas, sus ajustes y la migración 0248

**Files:**
- Delete: `backend/app/modules/user_admin/kc_client.py`, `backend/app/modules/user_admin/realm_config.py`, `backend/app/modules/keycloak_events/` (cuatro archivos)
- Modify (guion, Step 4): `backend/app/modules/user_admin/__init__.py`, `backend/app/worker.py` (`:92-96`, `:2490-2530`, el docstring de `sample_health_once`, `:3399-3408`, `:3509-3511`), `backend/app/api/probes.py`, `backend/app/core/health/probes.py`, `backend/app/core/health/backup_status.py:12`, `backend/app/models/core.py` (`:73-74`, `:870-893`), `backend/app/models/__init__.py:42`, `:335`, `backend/app/core/client_scope_catalog.py:44`, `backend/app/core/client_scope.py:305-307`, `backend/app/core/mail.py:289-291`, `backend/app/core/config.py` (`:82-113`, `:166-168` en una rama sin la Task 4; `:64-96`, `:148-149` después de ella), `backend/app/core/i18n/es.py` (cinco entradas)
- Create: `backend/migrations/versions/0248_keycloak_event_ledger_dropped.py`, `backend/tests/test_keycloak_ledger_migration.py`
- Delete: `backend/tests/test_keycloak_event_client.py`, `test_keycloak_event_mapping.py`, `test_keycloak_event_sweep.py`, `test_realm_config.py`, `test_user_admin_client.py`
- Modify (guion, Step 5): `test_user_admin_api.py`, `test_setup_api.py`, `test_mcp_admin_api.py`, `test_me_locale.py`, `test_platform_health_probes.py`, `test_platform_health_sampler.py`, `test_i18n_modules_rest.py`, `test_roadmap_status.py`, `test_no_vendor_names_backend.py`, `test_client_catalog.py`, `test_console_auth_realm.py` (cuatro pruebas), `frontend/src/modules/audit/auditVocabulary.coverage.test.ts`

**Interfaces:**
- Depende de: nada. Ningún módulo de `app/` llama a `kc_client` ni a `realm_config` (`app/modules/user_admin/__init__.py:8-11`), y el reenvío solo lo programa el beat.
- Produce: `OutboundRecorder` e `install_outbound(monkeypatch)` en `tests/test_user_admin_api.py` (Apéndice A); `WORKER_SAMPLED_PROBES = frozenset(_ALL_PROBES)`; la migración 0248.

**Decisiones de la tarea:** P4-10, P4-11, P4-21, P4-22, P4-27, P4-32 (Apéndice B.2). Y una más: **esta tarea no toca `tests/test_config.py`**, que la Task 4 reescribe en la misma oleada; que no quede ningún ajuste `keycloak_*` lo fija el falsificador 11 (Task 11) y, para el ciclo de vida, `test_the_lifecycle_needs_no_identity_provider_configuration` (`not hasattr(…, "keycloak_admin_url")`).

- [ ] **Step 1: Preparar la worktree** (como la Task 4, Step 1).

- [ ] **Step 2: Las pruebas que fallan**

Crear `backend/tests/test_keycloak_ledger_migration.py`:

```python
"""Migration 0248 on a database of its own, stopped at 0247 (the pattern of
`tests/test_oauth_server_migration.py`; own authentication phase 4, Task 5).

upgrade drops `keycloak_event_ingest` with its policy, its index and its
unique key; downgrade puts back exactly what 0064 made -- the columns, the
FORCE RLS and its `tenant_isolation`, the key, the index and app_user's three
grants -- so the migrations job's round trip through 0064 still works.
"""

import secrets
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.conftest import PgCluster, _psql
from tests.test_remote_supervise_migration import _alembic

TABLE = "keycloak_event_ingest"


@pytest.fixture()
def db_at_0247(pg_cluster: PgCluster) -> Iterator[str]:
    dbname = f"its_kc_ledger_{secrets.token_hex(4)}"
    _psql(f'CREATE DATABASE "{dbname}" OWNER its_admin')
    url = pg_cluster.admin_url.rsplit("/", 1)[0] + f"/{dbname}"
    try:
        _alembic(url, "upgrade", "0247")
        yield url
    finally:
        _psql(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


async def _shape(engine: AsyncEngine) -> dict[str, object]:
    async with engine.connect() as conn:
        present = (await conn.execute(text("SELECT to_regclass(:t)"), {"t": TABLE})).scalar_one()
        if present is None:
            return {"table": False}
        columns = (
            await conn.execute(
                text(
                    "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_name = :t ORDER BY column_name"
                ),
                {"t": TABLE},
            )
        ).all()
        rls = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"
                ),
                {"t": TABLE},
            )
        ).one()
        policies = set(
            (
                await conn.execute(
                    text("SELECT policyname FROM pg_policies WHERE tablename = :t"), {"t": TABLE}
                )
            ).scalars()
        )
        indexes = set(
            (
                await conn.execute(
                    text("SELECT indexname FROM pg_indexes WHERE tablename = :t"), {"t": TABLE}
                )
            ).scalars()
        )
        grants = set(
            (
                await conn.execute(
                    text(
                        "SELECT privilege_type FROM information_schema.role_table_grants "
                        "WHERE table_name = :t AND grantee = 'app_user'"
                    ),
                    {"t": TABLE},
                )
            ).scalars()
        )
    return {
        "table": True,
        "columns": [tuple(row) for row in columns],
        "rls": tuple(rls),
        "policies": policies,
        "indexes": indexes,
        "grants": grants,
    }


async def test_0248_drops_the_ledger_and_round_trips(db_at_0247: str) -> None:
    engine = create_async_engine(db_at_0247)
    try:
        before = await _shape(engine)
        assert before["table"] is True
        assert before["policies"] == {"tenant_isolation"}
        assert before["rls"] == (True, True)
        assert {"ix_keycloak_event_age", "uq_keycloak_event_fingerprint"} <= set(
            before["indexes"]  # type: ignore[call-overload]
        )

        _alembic(db_at_0247, "upgrade", "0248")
        assert await _shape(engine) == {"table": False}

        _alembic(db_at_0247, "downgrade", "0247")
        assert await _shape(engine) == before

        _alembic(db_at_0247, "upgrade", "0248")
        assert await _shape(engine) == {"table": False}
    finally:
        await engine.dispose()
```

Run: `.venv/bin/pytest -q tests/test_keycloak_ledger_migration.py`
Expected: FAIL — `subprocess.CalledProcessError: Command '[… 'upgrade', '0248']' returned non-zero exit status 1` (`_alembic` corre con `capture_output=True`, así que el «Can't locate revision identified by '0248'» de Alembic no se ve en la salida de pytest).

- [ ] **Step 3: La migración**

Crear `backend/migrations/versions/0248_keycloak_event_ledger_dropped.py`:

```python
"""own authentication phase 4: Keycloak's event ledger goes

`keycloak_event_ingest` (migration 0064) was the dedupe ledger of the worker
sweep that copied four of Keycloak's credential events into the audit chain
(docs/modules/console-auth/spec.md §7.1). Keycloak is gone, and with it the
sweep and the module that wrote this table (own authentication design
2026-11-04, §1.3 and §6.1: "Se borra `keycloak_event_ingest`"; phase 1 plan,
decision D4, deferred it to this phase).

NOTHING A PERSON READS GOES WITH IT. The ledger was never evidence: each event
it let through was written to `audit_events`, which keeps it, and only the
sweep ever read the table. The ledger's own rows -- fingerprints and the time
each was seen -- describe a sweep that no longer runs.

FORWARD-ONLY (CLAUDE.md). `downgrade()` recreates the table exactly as 0064
made it -- no later migration altered it: 0231 leaves a G table's
`tenant_isolation` as it is -- so CI's `upgrade head -> downgrade base ->
upgrade head` still runs 0064's own downgrade, which drops it again. The rows
are not restored: rollback is restore-from-backup (deploy-rollback design
D1).

Revision ID: 0248
Revises: 0247
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0248"
down_revision: str | None = "0247"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "keycloak_event_ingest"
_ORG_CTX = "current_setting('app.current_org', true)::uuid"


def upgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}")
    op.drop_index("ix_keycloak_event_age", table_name=_TABLE)
    op.drop_table(_TABLE)


def downgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("fingerprint", name="uq_keycloak_event_fingerprint"),
    )
    op.create_index("ix_keycloak_event_age", _TABLE, ["ingested_at"])
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {_TABLE} "
        f"USING (org_id = {_ORG_CTX}) WITH CHECK (org_id = {_ORG_CTX})"
    )
    op.execute(f"GRANT SELECT, INSERT, DELETE ON {_TABLE} TO app_user")
```

Run: `.venv/bin/pytest -q tests/test_keycloak_ledger_migration.py`
Expected: PASS (`downgrade()` reproduce columnas, FORCE RLS, `tenant_isolation`, la clave única, el índice y los tres permisos de `app_user` tal como los dejó 0064).

- [ ] **Step 4: El código que se va, con un guion**

El guion borra también `kc_client.py`, `realm_config.py` y `app/modules/keycloak_events/` (el `git add -A` del commit recoge los borrados). Guárdalo en `"${TMPDIR:-/tmp}/p4_task5_app.py"` y córrelo desde la raíz con `python3 "${TMPDIR:-/tmp}/p4_task5_app.py"`:

```python
"""Own authentication phase 4, Task 5: the Keycloak admin clients, the event
forwarding, its two probes, its settings and its ledger go from the app.

Run from the worktree root: python3 p4_task5_app.py. Every edit asserts its
anchor is found exactly once."""

import pathlib
import shutil

root = pathlib.Path("backend")


def swap(path: pathlib.Path, old: str, new: str) -> None:
    text = path.read_text()
    assert text.count(old) == 1, (path, old[:70])
    path.write_text(text.replace(old, new))


def cut(path: pathlib.Path, start: str, end: str, new: str = "") -> None:
    """Replace from the line starting `start` up to (not including) `end`."""
    text = path.read_text()
    assert text.count(start) == 1, (path, start[:70])
    i = text.index(start)
    j = text.index(end, i)
    path.write_text(text[:i] + new + text[j:])


app = root / "app"

# --- the two admin clients and the event forwarding ------------------------ #
(app / "modules/user_admin/kc_client.py").unlink()
(app / "modules/user_admin/realm_config.py").unlink()
shutil.rmtree(app / "modules/keycloak_events")

swap(
    app / "modules/user_admin/__init__.py",
    """§7: the platform holds the accounts itself. `service.py` holds every decision
worth testing, and `api.py` is a thin translation of it to HTTP. The two
Keycloak clients stay only until phase 4 and are not part of the lifecycle:
neither has a caller since phase 3 (revoking an AI app no longer asks
Keycloak). `realm_config.py` imports `kc_client.py`'s types, and nothing
imports `realm_config.py`.
""",
    """§7: the platform holds the accounts itself. `service.py` holds every decision
worth testing, and `api.py` is a thin translation of it to HTTP. No identity
provider is called: the two Keycloak admin clients that once lived here went
in own authentication phase 4, having had no caller since phase 3.
""",
)

# --- worker ---------------------------------------------------------------- #
worker = app / "worker.py"
swap(
    worker,
    """
# Imported as a module for the same reason as `health` and `ipam`: the beat
# entry below reads `keycloak_sweep_module.SWEEP_INTERVAL_S` rather than a
# literal 3600.0, so the schedule and the staleness threshold the
# `keycloak_events` probe measures against cannot drift apart.
from app.modules.keycloak_events import sweep as keycloak_sweep_module
""",
    "",
)
cut(worker, "async def keycloak_event_sweep_once() -> dict[str, int]:\n", "def register_worker_job_types() -> None:\n")
swap(
    worker,
    """        # console-auth §7.1. Hourly, matching the module's own constant rather
        # than a literal: this is the LAG the sweep costs versus a push
        # listener, and it is stated in the spec as the price of not shipping a
        # Java plugin into a Keycloak image this project pins and does not
        # build. A faster beat would buy nothing -- Keycloak's own event
        # filters are day-granular.
        "keycloak-event-sweep": {
            "task": "app.worker.keycloak_event_sweep",
            "schedule": keycloak_sweep_module.SWEEP_INTERVAL_S,
        },
""",
    "",
)
swap(
    worker,
    """    @celery_app.task(name="app.worker.keycloak_event_sweep")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def keycloak_event_sweep() -> dict[str, int]:
        return asyncio.run(keycloak_event_sweep_once())

""",
    "",
)
cut(
    worker,
    "    ONLY `WORKER_SAMPLED_PROBES` ARE SAMPLED -- every probe but `jwks`. This\n",
    '    """\n    from app.api.probes import WORKER_SAMPLED_PROBES, build_registry\n',
    """    EVERY PROBE IS SAMPLED (`WORKER_SAMPLED_PROBES`). Until own authentication
    phase 4 one was not: `jwks` read the API process's cache of Keycloak's
    keys, which a worker never had, and sampling it here would have written a
    fabricated `down` every 60 seconds. The probe went with Keycloak; the name
    `WORKER_SAMPLED_PROBES` stays so that a probe only the API can measure
    has somewhere to be left out again.
""",
)

# --- probes ---------------------------------------------------------------- #
probes = app / "api/probes.py"
swap(probes, "    probe_jwks,\n    probe_keycloak_events,\n", "")
swap(probes, '    "jwks": probe_jwks,\n', "")
swap(
    probes,
    """    # SOFT, and `ok` when unconfigured. Forwarding Keycloak's credential events
    # is optional; a deployment that has not enabled it is not unhealthy. What
    # this probe exists to prevent is the OTHER silence -- configured, not
    # running, and nobody told.
    "keycloak_events": probe_keycloak_events,
""",
    "",
)
cut(
    probes,
    "# The probes a Celery worker can meaningfully SAMPLE into history -- every\n",
    "# WHAT THE STATUS PAGE PRINTS",
    """# The probes a Celery worker can meaningfully SAMPLE into history: every one
# since own authentication phase 4 removed `jwks`, which read a cache only the
# API process held (`app.worker.sample_health_once` says why that mattered).
# Derived from `_ALL_PROBES`, never typed out, so the two cannot drift.
WORKER_SAMPLED_PROBES = frozenset(_ALL_PROBES)

""",
)
swap(
    probes,
    """# But `/api/v1/platform/status` sends the name straight to
# `PlatformStatusPage`, which prints it in a table -- so `keycloak_events` put
# the identity vendor's name on an operator's screen, from a Python dict key.""",
    """# But `/api/v1/platform/status` sends the name straight to
# `PlatformStatusPage`, which prints it in a table -- so a probe once keyed
# `keycloak_events` (removed in own authentication phase 4) put the identity
# vendor's name on an operator's screen, from a Python dict key.""",
)
swap(probes, '    "jwks": N_("Sign-in key cache"),\n', "")
swap(probes, '    "keycloak_events": N_("Sign-in event forwarding"),\n', "")

health = app / "core/health/probes.py"
cut(health, "async def probe_jwks(state: Any) -> ProbeResult:\n", "async def probe_signing(state: Any) -> ProbeResult:\n")
text = health.read_text()
i = text.index("\n\nasync def probe_keycloak_events(state: Any) -> ProbeResult:\n")
health.write_text(text[:i] + "\n")

swap(
    app / "core/health/backup_status.py",
    "`configured: false` (the `keycloak_events` precedent): visible, not alarming.",
    "`configured: false`: visible, not alarming.",
)

# --- the ledger ------------------------------------------------------------ #
core = app / "models/core.py"
swap(
    core,
    """    # Migration 0066. The three fields ITMS owns about a person, as against the
    # ones Keycloak owns (spec §7.1: password, MFA, passkeys, email). All""",
    """    # Migration 0066. The three fields a person's profile holds, as against
    # the sign-in's own (password, second factors, passkeys, email). All""",
)
cut(core, "class KeycloakEventIngest(Base):\n", "class DomainEvent(Base):\n")
init = app / "models/__init__.py"
swap(init, "    KeycloakEventIngest,\n", "")
swap(init, '    "KeycloakEventIngest",\n', "")
swap(
    app / "core/client_scope_catalog.py",
    '"audit_events": G, "keycloak_event_ingest": G,',
    '"audit_events": G,',
)
swap(
    app / "core/client_scope.py",
    """    "app/modules/keycloak_events/sweep.py::_forward": (
        "worker: forwards Keycloak events into the audit chain"
    ),
""",
    "",
)

# --- mail: the reasons that lived in realm_config.py ----------------------- #
swap(
    app / "core/mail.py",
    """    \"\"\"The sending address, the caller's own mailbox (a `+tag` is fine), or an
    address in the organization's console sign-in domain. Why the list is this
    short: `realm_config.py`'s module docstring, "THE TEST SEND".\"\"\"""",
    """    \"\"\"The sending address, the caller's own mailbox (a `+tag` is fine), or an
    address in the organization's console sign-in domain.

    WHY THE LIST IS THIS SHORT (moved from `user_admin/realm_config.py`, which
    went with Keycloak in own authentication phase 4):

    - A mail-sending primitive that took ANY address would send from the
      organization's own domain, through its own relay, to anyone. The content
      is the platform's fixed test text, so the worst case is small, but it is
      a primitive this platform does not need to hold.
    - "The caller's DOMAIN" was the first version, and review showed why not:
      a caller signed in as `admin@gmail.com` would have opened all of
      gmail.com. The caller's own address is proof of one mailbox, not of a
      domain. The console sign-in domain is the only domain here that already
      means "this organization's people": it decides who may sign in at all.
    - The sending address's DOMAIN is not on the list for the same reason: it
      is one form field away from any value.\"\"\"""",
)

# --- settings -------------------------------------------------------------- #
config = app / "core/config.py"
cut(config, "    # KEYCLOAK EVENT FORWARDING (docs/modules/console-auth/spec.md §7.1).\n", "    # The console email-domain allowlist.")
swap(
    config,
    """    # (`app/core/auth/oauth/policy.py::registration_open`); keycloak-init reads
    # the same `.env` line until phase 4 retires it.
""",
    """    # (`app/core/auth/oauth/policy.py::registration_open`).
""",
)

es = app / "core/i18n/es.py"
for entry in (
    '    "Sign-in event forwarding": "Reenvío de eventos de inicio de sesión",\n',
    '    "Sign-in key cache": "Claves de inicio de sesión (JWKS)",\n',
):
    swap(es, entry, "")
text = es.read_text()
for head in (
    "    \"the identity provider did not return this platform's service account, so a test message \"",
    '    "no sending address is saved, so there is nowhere to send a test message": (',
    '    "{address} is the address of a sign-in account, and the identity provider will not send a "',
):
    assert text.count(head) == 1, head
    i = text.index(head)
    # the entry ends at the first line that closes it: '    ),\n' after the key
    j = text.index("\n    ),\n", i) + len("\n    ),\n")
    print("removing es entry:\n" + text[i:j])
    text = text[:i] + text[j:]
es.write_text(text)
print("ok")
```

Expected: tres bloques `removing es entry:` y `ok`. Lo que hace, en orden: el docstring de `user_admin`; el import, la función, la entrada del beat y la tarea de Celery del reenvío en `worker.py`, y el párrafo del docstring de `sample_health_once` sobre `jwks`; las dos sondas y sus etiquetas (`WORKER_SAMPLED_PROBES` pasa a ser todas); el precedente nombrado en `backup_status.py`; el modelo `KeycloakEventIngest`, su export, su línea del catálogo de alcance y su entrada en `ALL_SCOPE_ALLOWLIST`; las razones de la lista corta de destinatarios de la prueba de correo, que vivían en el docstring de `realm_config.py` y pasan al de `mail.recipient_permitted`; los dos bloques de ajustes `keycloak_*` y la frase de `keycloak-init` del comentario de `mcp_registration`; y las cinco entradas del catálogo español que solo usaban las sondas y `realm_config.py`.

Run: `grep -rn -i "keycloak" backend/app --include=*.py | grep -v "i18n/es.py"`
Expected: solo frases en pasado (Apéndice D, «Historia»): `user_admin/service.py:29`, `user_admin/__init__.py`, `remote/tunnel.py:8`, `worker.py` (el párrafo nuevo de `sample_health_once`), `api/v1/me.py:38`, `api/v1/platform.py:49`, `api/v1/ws.py:35`, `api/probes.py` (el comentario de las etiquetas), `api/deps.py`, `core/identity/*`, `core/auth/*`, `core/config.py` (el comentario de `default_org_slug`, si la Task 4 ya está), `core/mail.py`.

- [ ] **Step 5: Las pruebas, con un guion**

El guion borra primero las cinco pruebas de lo que se fue (`test_keycloak_event_client.py`, `test_keycloak_event_mapping.py`, `test_keycloak_event_sweep.py`, `test_realm_config.py`, `test_user_admin_client.py`; Apéndice C) y después edita el resto. Guardado en `"${TMPDIR:-/tmp}/p4_task5_tests.py"` y corrido desde la raíz:

```python
"""Own authentication phase 4, Task 5: the tests of what went with the app
edits go with them, and the tests that only watched for a call to Keycloak
watch for any call at all (`OutboundRecorder`).

Run from the worktree root: python3 p4_task5_tests.py."""

import ast
import pathlib
import re

root = pathlib.Path(".")
tests = root / "backend/tests"


def swap(path: pathlib.Path, old: str, new: str, count: int = 1) -> None:
    text = path.read_text()
    assert text.count(old) == count, (path, old[:70], text.count(old))
    path.write_text(text.replace(old, new))


def cut(path: pathlib.Path, start: str, end: str, new: str = "") -> None:
    """Replace from `start` up to (not including) `end`."""
    text = path.read_text()
    assert text.count(start) == 1, (path, start[:70])
    i = text.index(start)
    j = text.index(end, i)
    path.write_text(text[:i] + new + text[j:])


def drop_functions(path: pathlib.Path, names: set[str]) -> None:
    text = path.read_text()
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    spans = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            start = (node.decorator_list[0].lineno if node.decorator_list else node.lineno) - 1
            spans.append((start, node.end_lineno))
    assert len(spans) == len(names), (path, names, spans)
    for start, end in sorted(spans, reverse=True):
        # take the two blank lines that follow the function too
        while end < len(lines) and lines[end].strip() == "":
            end += 1
        del lines[start:end]
    path.write_text("".join(lines))


for gone in (
    "test_keycloak_event_client.py",
    "test_keycloak_event_mapping.py",
    "test_keycloak_event_sweep.py",
    "test_realm_config.py",
    "test_user_admin_client.py",
):
    (tests / gone).unlink()

# --- test_user_admin_api.py ------------------------------------------------ #
ua = tests / "test_user_admin_api.py"
swap(
    ua,
    """3. **No identity provider is called** (design §1.3, D5). A fake Keycloak stays
   installed on `httpx` so that any call would be SEEN: every `kc.seen == []`
   below holds the rule. An invited technician's `external_subject` is
   `itms-local:<uuid>`.
""",
    """3. **No identity provider is called** (design §1.3, D5). Every
   `httpx.AsyncClient` built during a test sends to an `OutboundRecorder`, so
   that any call would be SEEN: every `outbound.seen == []` below holds the
   rule. An invited technician's `external_subject` is `itms-local:<uuid>`.
""",
)
swap(ua, "from app.modules.user_admin.kc_client import KeycloakAdminConfig\n", "")
cut(
    ua,
    "#: Captured at import, before any fixture swaps the name -- see\n",
    "@pytest.fixture()\ndef users_env(",
    '''#: Captured at import, before any fixture swaps the name: a factory that
#: resolved `httpx.AsyncClient` by name would recurse into itself.
_REAL_CLIENT = httpx.AsyncClient


class OutboundRecorder:
    """Every request an `httpx.AsyncClient` built during a test sends, kept so
    that a call to anything outside the platform would be SEEN.

    Rule 3 above, and the same rule in `test_setup_api.py`, `test_me_locale.py`
    and `test_mcp_admin_api.py`: `.seen == []` holds it. Until own
    authentication phase 4 this was a fake Keycloak that answered its admin
    API. Nothing is answered now: a 599, which no real server sends, so a
    caller that broke the rule would fail visibly as well as be seen.
    """

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return httpx.Response(599)


def install_outbound(monkeypatch: pytest.MonkeyPatch) -> OutboundRecorder:
    """From now until the test ends, every `httpx.AsyncClient` sends to a new
    `OutboundRecorder` -- one built with no transport, and one built with a
    network transport of its own (`app/core/egress.py` pins its addresses
    that way). Only an in-process transport keeps itself: an ASGI client (the
    tests', the assistant's, the MCP server's own call) or a mock."""
    recorder = OutboundRecorder()

    def factory(*args: Any, **kw: Any) -> httpx.AsyncClient:
        if isinstance(kw.get("transport"), (httpx.ASGITransport, httpx.MockTransport)):
            return _REAL_CLIENT(*args, **kw)
        kw["transport"] = httpx.MockTransport(recorder.handler)
        return _REAL_CLIENT(*args, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return recorder


''',
)
swap(
    ua,
    """    # Rule 3: nothing on this surface calls an identity provider. The fake
    # stays on `httpx` so a call would be SEEN, and `kc.seen == []` holds it.
    kc = FakeKeycloak()

    def factory(**kw: Any) -> httpx.AsyncClient:
        return _REAL_CLIENT(transport=httpx.MockTransport(kc.handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    console_ready(api_env, monkeypatch)
    api_env.kc = kc
""",
    """    # Rule 3: nothing on this surface calls an identity provider, or anything
    # else outside the platform. The recorder stays on `httpx` so a call would
    # be SEEN, and `outbound.seen == []` holds it.
    api_env.outbound = install_outbound(monkeypatch)
    console_ready(api_env, monkeypatch)
""",
)
swap(
    ua,
    '''    """Rule 3, from the deployment's side: with no `ITS_KEYCLOAK_ADMIN_*` at all
    (the test settings set none) every lifecycle write works, where it used to
    be a 503."""
    assert users_env.settings.keycloak_admin_url is None
''',
    '''    """Rule 3, from the deployment's side: no identity-provider setting exists
    (the `ITS_KEYCLOAK_ADMIN_*` settings went with Keycloak in own
    authentication phase 4) and every lifecycle write works, where it used to
    be a 503."""
    assert not hasattr(users_env.settings, "keycloak_admin_url")
''',
)
swap(ua, "users_env.kc.seen", "users_env.outbound.seen", count=14)

# --- test_setup_api.py ----------------------------------------------------- #
su = tests / "test_setup_api.py"
swap(
    su,
    """NO IDENTITY PROVIDER IS CALLED. The fake from `test_user_admin_api` stays on
`httpx` so that a call would be seen; `kc.seen == []` holds it.
""",
    """NO IDENTITY PROVIDER IS CALLED. `test_user_admin_api.install_outbound` stays
on `httpx` so that a call would be seen; `outbound.seen == []` holds it.
""",
)
swap(
    su,
    "from tests.test_user_admin_api import _REAL_CLIENT, FakeKeycloak\n",
    "from tests.test_user_admin_api import _REAL_CLIENT, OutboundRecorder, install_outbound\n",
)
swap(su, "        kc: FakeKeycloak,\n", "        outbound: OutboundRecorder,\n")
swap(su, "        self.kc = kc\n", "        self.outbound = outbound\n")
swap(
    su,
    """    kc = FakeKeycloak()

    def factory(**kw: Any) -> httpx.AsyncClient:
        return _REAL_CLIENT(transport=httpx.MockTransport(kc.handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
""",
    "    outbound = install_outbound(monkeypatch)\n",
)
swap(
    su,
    "        yield SetupEnv(app, client, settings, admin_engine, kc, code_path)\n",
    "        yield SetupEnv(app, client, settings, admin_engine, outbound, code_path)\n",
)
swap(
    su,
    '''    """P2 phase 1: there is no provisioning credential to wait for. With no
    `ITS_KEYCLOAK_ADMIN_*` at all the claim makes the owner."""
    assert setup_env.app.state.settings.keycloak_admin_url is None
''',
    '''    """P2 phase 1: there is no provisioning credential to wait for, and since
    own authentication phase 4 no such setting at all: the claim makes the
    owner."""
    assert not hasattr(setup_env.app.state.settings, "keycloak_admin_url")
''',
)
swap(su, "setup_env.kc.seen", "setup_env.outbound.seen", count=3)

# --- test_mcp_admin_api.py ------------------------------------------------- #
ma = tests / "test_mcp_admin_api.py"
swap(
    ma,
    """Keycloak is still `httpx.MockTransport`: for `kc_client.revoke_consent`'s own
tests, which stay until phase 4, and to prove that revoking sends it nothing.
""",
    """Every `httpx.AsyncClient` built without a transport during a test sends to
`test_user_admin_api.OutboundRecorder`, to prove that revoking sends nothing
anywhere. Until own authentication phase 4 it was a fake Keycloak, which also
served `kc_client.revoke_consent`'s own tests; they went with that client.
""",
)
swap(
    ma,
    """from app.modules.user_admin import kc_client
from app.modules.user_admin.kc_client import (
    KeycloakAdminConfig,
    KeycloakAdminError,
    delete_user,
    execute_actions_email,
    revoke_consent,
    set_enabled,
    set_locale_attribute,
)
""",
    "",
)
swap(
    ma,
    "from tests.race_helpers import normalised, pause_at, until_blocked_by\n",
    "from tests.race_helpers import normalised, pause_at, until_blocked_by\n"
    "from tests.test_user_admin_api import install_outbound\n",
)
swap(
    ma,
    '''BASE = "https://itms.example.com"
_REAL_CLIENT = httpx.AsyncClient

CONFIG = KeycloakAdminConfig(
    base_url="https://keycloak.test",
    realm="it-suite",
    client_id="it-suite-provisioning",
    client_secret="not-a-real-secret",
)
TOKEN = "tok-never-log-me"
''',
    '''BASE = "https://itms.example.com"
''',
)
cut(
    ma,
    "# --------------------------------------------------------------------------- #\n"
    "# Fake Keycloak: the token endpoint and the consents endpoint, nothing else\n",
    "@pytest.fixture()\nasync def env(",
)
swap(
    ma,
    """    api_env.settings.public_console_url = BASE
    kc = FakeKeycloak()
    _install(monkeypatch, kc)
    monkeypatch.setattr(
        KeycloakAdminConfig, "from_settings", classmethod(lambda cls, settings: CONFIG)
    )
    return SimpleNamespace(api=api_env, kc=kc)
""",
    """    api_env.settings.public_console_url = BASE
    return SimpleNamespace(api=api_env, outbound=install_outbound(monkeypatch))
""",
)
swap(ma, "    env.kc.raise_connect = True\n", "")
swap(
    ma,
    """async def test_revoking_needs_no_provisioning_client(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        KeycloakAdminConfig, "from_settings", classmethod(lambda cls, settings: None)
    )
    org_id, slug""",
    '''async def test_revoking_needs_no_provisioning_client(env: Any) -> None:
    """There is none since own authentication phase 4 (until then this test
    made the provisioning client's settings read as absent first): a grant in
    use ends, and nothing is sent anywhere."""
    org_id, slug''',
)
cut(
    ma,
    "# --------------------------------------------------------------------------- #\n"
    "# The Keycloak call itself\n",
    "# --------------------------------------------------------------------------- #\n"
    "# 6. The switch is audited when it moves\n",
)
text = ma.read_text()
n = text.count("env.kc.requests == []")
print("mcp_admin kc.requests sites:", n)
ma.write_text(text.replace("env.kc.requests == []", "env.outbound.seen == []"))

# --- test_me_locale.py ----------------------------------------------------- #
ml = tests / "test_me_locale.py"
cut(
    ml,
    '"""`PATCH /me` locale',
    "async def _row(engine: AsyncEngine, sub: str) -> User:\n",
    '''"""`PATCH /me` locale (spec 2026-10-09 §3.5, §4), and the identity-provider
write it no longer makes (P2 phase 1 Task 8; design §1.3 retires "el espejo del
idioma en Keycloak (`me.py`)").

The format check (unchanged), and the language staying in the platform.
`PATCH /me` calls no identity provider whatever is configured, and its answer
carries no `identity_locale_synced`. The sign-in pages and the account mails
are the platform's own and read `users.locale`. Every `httpx.AsyncClient` the
route could build sends to `test_user_admin_api.OutboundRecorder`, so a call
would be seen; `env.outbound.seen == []` holds the rule. (Until own
authentication phase 4 this file also drove `kc_client.set_locale_attribute`
on its own; that function went with Keycloak.)
"""

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import User
from tests.test_user_admin_api import install_outbound


@pytest.fixture()
async def env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    org_id = await api_env.create_org("loc")
    sub = await api_env.create_user(org_id, "technician")
    outbound = install_outbound(monkeypatch)
    headers = await api_env.headers(org_id, sub)
    return SimpleNamespace(
        api=api_env, org_id=org_id, sub=sub, outbound=outbound, headers=headers
    )


''',
)
swap(
    ml,
    '''async def _write(env: Any, locale: str | None) -> None:
    """The client function on its own, against the fake, for this user."""
    await set_locale_attribute(CONFIG, user_id=env.sub, locale=locale)


''',
    "",
)
drop_functions(ml, {"test_a_broken_provisioning_setting_cannot_touch_the_save"})
text = ml.read_text()
i = text.index("# --------------------------------------------------------------------------- #\n"
               "# kc_client.set_locale_attribute on its own")
ml.write_text(text[:i].rstrip("\n") + "\n")
swap(ml, "env.kc.seen", "env.outbound.seen", count=6)

# --- test_platform_health_probes.py --------------------------------------- #
hp = tests / "test_platform_health_probes.py"
swap(hp, "    probe_jwks,\n", "")
drop_functions(
    hp, {"test_the_jwks_probe_performs_no_network_io", "test_an_empty_jwks_cache_is_degraded_not_down"}
)
cut(
    hp,
    "# --------------------------------------------------------------------------- #\n"
    "# keycloak_events: the probe whose whole job is making a silence visible\n",
    "# --------------------------------------------------------------------------- #\n"
    "# what the status PAGE prints",
)
swap(
    hp,
    """    `PlatformStatusPage` renders the probe name the API sends, and one probe is
    registered as `keycloak_events` -- so a buyer read the identity vendor's
    name in a table heading position, from a dict key that no scan of user-
    visible string literals could have caught.

    The key is NOT renamed: `platform_health_sample.probe` rows carry it,
    `/readyz`'s `failing` list reports it, and
    `test_platform_health_sampler.py` pins the sampled set -- renaming would
    orphan every stored sample. So the route sends both: `key`, the stable
    identifier, and `name`, the human one the page prints.
    \"\"\"""",
    """    `PlatformStatusPage` renders the probe name the API sends, and one probe was
    registered as `keycloak_events` (until own authentication phase 4 removed
    it) -- so a buyer read the identity vendor's name in a table heading
    position, from a dict key that no scan of user-visible string literals
    could have caught.

    Keys are NOT renamed: `platform_health_sample.probe` rows carry them,
    `/readyz`'s `failing` list reports them, and
    `test_platform_health_sampler.py` pins the sampled set -- renaming would
    orphan every stored sample. So the route sends both: `key`, the stable
    identifier, and `name`, the human one the page prints.
    \"\"\"""",
)
swap(
    hp,
    '    assert by_key["keycloak_events"]["name"] == probe_label("keycloak_events")\n',
    '    assert by_key["backup.nightly"]["name"] == probe_label("backup.nightly")\n'
    '    assert by_key["backup.nightly"]["name"] != "backup.nightly"\n',
)

# --- test_platform_health_sampler.py -------------------------------------- #
hs = tests / "test_platform_health_sampler.py"
swap(
    hs,
    """`sample_health_once` also runs `WORKER_SAMPLED_PROBES` -- seven of the eight
probes, all but `jwks` (see `app.api.probes`) -- against real `Settings`""",
    """`sample_health_once` also runs `WORKER_SAMPLED_PROBES` -- every probe since
own authentication phase 4 removed `jwks`, the one only the API process could
measure (see `app.api.probes`) -- against real `Settings`""",
)
swap(hs, "from types import SimpleNamespace\n", "")
swap(hs, "from app.core.health.probes import probe_jwks\n", "")
swap(hs, "from app.api.probes import WORKER_SAMPLED_PROBES\n", "from app.api.probes import _ALL_PROBES, WORKER_SAMPLED_PROBES\n")
swap(
    hs,
    """    written = await sample_health_once(db_session)
    # One short of the registry's size: the sampler runs
    # `WORKER_SAMPLED_PROBES`, every probe but `jwks` -- see
    # `test_the_sampler_writes_no_jwks_row` below for why. Re-measured when
    # `keycloak_events` was added, and again for the three backup probes.
    assert written == 11, "one row per worker-sampled probe (every probe minus jwks)"
""",
    """    \"\"\"Every probe, one row each, and exactly the registry's set -- so a probe
    silently swapped in for another still fails this. Ten since own
    authentication phase 4 removed `jwks` (never sampled: a worker holds no
    key cache of an identity provider) and `keycloak_events`.\"\"\"
    written = await sample_health_once(db_session)
    assert written == 10, "one row per probe"
    names = set((await db_session.execute(select(PlatformHealthSample.probe))).scalars().all())
    assert names == WORKER_SAMPLED_PROBES == set(_ALL_PROBES)
""",
)
drop_functions(
    hs,
    {
        "test_the_sampler_writes_no_jwks_row",
        "test_jwks_is_excluded_because_a_worker_reading_would_be_a_false_down",
    },
)

# --- test_i18n_modules_rest.py -------------------------------------------- #
im = tests / "test_i18n_modules_rest.py"
swap(
    im,
    "ai_portal, patch, inventory, reporting, security_logs and keycloak_events.",
    "ai_portal, patch, inventory, reporting and security_logs.",
)
swap(
    im,
    "# security_logs and keycloak_events: events ingested from outside are stored as\n",
    "# security_logs: events ingested from outside are stored as\n",
)
drop_functions(im, {"test_keycloak_events_nothing_it_forwards_or_reports_is_translated"})

# --- test_roadmap_status.py ----------------------------------------------- #
rs = tests / "test_roadmap_status.py"
swap(
    rs,
    """#
# `keycloak_events` (added 2026-08-18) is unphased for a different and narrower
# reason: it is NOT A BUSINESS MODULE. It adds no routes, no job types and no
# frontend folder -- it is a worker sweep implementing one clause of
# `docs/modules/console-auth/spec.md` §4, which is sign-in and deployment work
# rather than a numbered phase. It lives under `app/modules/` because that is
# where a self-contained package with its own migration belongs, not because it
# is a module in the roadmap's sense. A roadmap row for it would claim a
# deliverable that does not exist.
""",
    "",
)
swap(rs, '        "keycloak_events",\n', "")

# --- test_no_vendor_names_backend.py -------------------------------------- #
nv = tests / "test_no_vendor_names_backend.py"
swap(
    nv,
    """            # lives there: `__tablename__ = "keycloak_event_ingest"` in
            # `app/models/core.py`, which is a POSTGRES TABLE NAME. Collecting""",
    """            # lives there: `__tablename__ = "keycloak_event_ingest"` in
            # `app/models/core.py` until own authentication phase 4, a
            # POSTGRES TABLE NAME. Collecting""",
)
swap(
    nv,
    """    `PlatformStatusPage` prints the probe name the status API sends, and the
    probe was called `keycloak_events` -- a dict KEY, in the position of a
    heading, on an operator's screen. The key stays (history rows in
    `platform_health_sample.probe` carry it, `/readyz` reports it, and
    `test_platform_health_sampler.py` pins the sampled set); the label is the
    separate, user-facing string, and this is what keeps it clean.""",
    """    `PlatformStatusPage` prints the probe name the status API sends, and a
    probe was called `keycloak_events` (until own authentication phase 4) -- a
    dict KEY, in the position of a heading, on an operator's screen. Keys stay
    as they are (history rows in `platform_health_sample.probe` carry them,
    `/readyz` reports them, and `test_platform_health_sampler.py` pins the
    sampled set); the label is the separate, user-facing string, and this is
    what keeps it clean.""",
)

# --- test_client_catalog.py ----------------------------------------------- #
swap(
    tests / "test_client_catalog.py",
    """        "an identity, not a name: one subject of the identity provider was one person in "
        "the organisation. No door reads it since own authentication phase 3 (the MCP token "
        "names the person by id); the Keycloak event ingest still does until phase 4 removes "
        "both. Per client, one login could hold a row in each client. Written only under "
        "scope all (`POST /users`, a contact's placeholder), and the value is an opaque id"
""",
    """        "an identity, not a name: one subject of the identity provider was one person in "
        "the organisation. No door reads it since own authentication phase 3 (the MCP token "
        "names the person by id), and nothing at all since phase 4 removed the Keycloak event "
        "ingest; it stays a surrogate key (phase 4 plan, P4-09). Per client, one login could "
        "hold a row in each client. Written only under scope all (`POST /users`, a contact's "
        "placeholder), and the value is an opaque id"
""",
)

# --- test_console_auth_realm.py: the four tests that read kc_client.py --- #
cr = tests / "test_console_auth_realm.py"
drop_functions(
    cr,
    {
        "test_the_env_example_ships_a_secret_path_something_actually_writes",
        "test_every_document_that_counts_the_admin_operations_counts_the_same",
        "test_the_action_sets_are_read_from_both_sides_and_are_not_empty",
        "test_every_credential_step_itms_asks_for_exists_in_the_realm",
        "_actions_the_backend_mails",
    },
)
text = cr.read_text()
old = '''KC_CLIENT = REPO / "backend/app/modules/user_admin/kc_client.py"
DESIGN = REPO / "docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md"

#: Small enough that spelling them out beats a dependency.
_SPELLED = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven"}


'''
assert text.count(old) == 1
cr.write_text(text.replace(old, ""))
text = cr.read_text()
assert text.count("import ast\n") == 1
cr.write_text(text.replace("import ast\n", "", 1))

# --- the console's audit-vocabulary guard stops reading keycloak_events -- #
path = root / "frontend/src/modules/audit/auditVocabulary.coverage.test.ts"
text = path.read_text()
for old, new in (
    (
        '''  {
    file: "modules/keycloak_events/mapping.py",
    how: "map_event(): _DIRECT table and the passkey branch",
    needles: ["_DIRECT"],
    codes: ["auth.identity_linked", "auth.identity_unlinked", "auth.passkey_registered", "auth.passkey_removed"],
  },
''',
        "",
    ),
    ('  "modules/keycloak_events/sweep.py": 3,\n', ""),
):
    assert text.count(old) == 1, old[:60]
    text = text.replace(old, new)
path.write_text(text)
print("ok")
```

Expected: `mcp_admin kc.requests sites: 11` y `ok`. Lo que hace: `OutboundRecorder` e `install_outbound` sustituyen al Keycloak falso (todo cliente httpx que no sea en proceso, también uno con un transporte de red propio como los de `app/core/egress.py`, envía al registrador: revisión 2, M5) de `test_user_admin_api.py` (14 aserciones `.seen == []`), de `test_setup_api.py` (3), de `test_mcp_admin_api.py` (11) y de `test_me_locale.py` (6), que pierde la mitad de `set_locale_attribute` y `test_a_broken_provisioning_setting_cannot_touch_the_save`; en `test_mcp_admin_api.py` se van las seis pruebas de `kc_client` y `test_revoking_needs_no_provisioning_client` deja de parchear un ajuste que no existe; las sondas `jwks` y `keycloak_events` salen de sus pruebas, la de la etiqueta se re-apunta a `backup.nightly` y el muestreador pasa a 10 filas con la igualdad de conjuntos; `keycloak_events` sale de `test_i18n_modules_rest.py` y de `EXPECTED_UNPHASED`; dos docstrings de `test_no_vendor_names_backend.py` y el motivo de `users_org_id_external_subject_key` (P4-09) dejan de hablar en presente; las cuatro pruebas de `test_console_auth_realm.py` que leían `kc_client.py` se van con él; y la guarda del vocabulario de auditoría de la consola deja de buscar `modules/keycloak_events/` (P4-11).

- [ ] **Step 6: Las pruebas pasan**

Run: `.venv/bin/pytest -q -W error::RuntimeWarning tests/test_user_admin_api.py tests/test_setup_api.py tests/test_mcp_admin_api.py tests/test_me_locale.py tests/test_platform_health_probes.py tests/test_platform_health_sampler.py tests/test_i18n_modules_rest.py tests/test_roadmap_status.py tests/test_no_vendor_names_backend.py tests/test_client_catalog.py tests/test_config.py tests/test_keycloak_ledger_migration.py tests/test_i18n_catalog.py tests/test_client_isolation.py tests/test_phase2_foundations.py tests/test_worker_beat_expiry.py tests/test_review_beats.py tests/test_worker_beat_schedule.py tests/test_logging_discipline.py tests/test_platform_health_endpoints.py tests/test_console_auth_realm.py tests/test_realm_settings_api.py`
Expected: PASS (691 en el ensayo de la ronda de correcciones, con esta tarea sola sobre la oleada 1: ya no depende de la Task 4).

Desde `frontend/`: `npx vitest run src/modules/audit`
Expected: PASS (113).

- [ ] **Step 7: Tipos, estilo y commit**

Run: `.venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: limpio (551 archivos en esta rama sola; 550 después de fusionar la Task 4, que borra `security.py`).

```bash
git add -A backend/app backend/migrations/versions/0248_keycloak_event_ledger_dropped.py backend/tests \
  frontend/src/modules/audit/auditVocabulary.coverage.test.ts
git commit -m "refactor: Keycloak's admin clients, event forwarding, probes, settings and ledger go (phase 4)"
```

---


### Task 6: El despliegue sin Keycloak

**Files:**
- Modify (guion, Step 3): `deploy/docker-compose.yml` (comentario del router `api` `:126-135`; `api.depends_on.keycloak` `:168-169`; comentario de Postgres `:456-457`; los servicios `keycloak` y `keycloak-init`, `:575-796`)
- Delete: `deploy/keycloak/init-clients.py`, `deploy/keycloak/realm-it-suite.json`, `deploy/keycloak/themes/` (`deploy/keycloak/ci_mcp_flow.py` es de la Task 7)
- Modify (guion): `deploy/postgres/init-app-user.sh:2`, `:67`; `deploy/scripts/backup.sh:7-9`, `:37-38`, `:61-65`, `:92-93`, `:130`; `deploy/scripts/scheduled-backup.sh:222-223`; `deploy/scripts/restore.sh:179-184`, `:233-235`; `deploy/scripts/restore-drill.sh:8`, `:10-15`, `:29-30`, `:35-36`, `:104`, `:162-181`; `deploy/drill/docker-compose.drill.yml:29-40`; `deploy/scripts/deploy.sh` (la cabecera `:35-40`, la ayuda `:55`, `:165-170`, `:209-214`, `:229`, `:237`, `:240-254`, `:270`, `:291-292`, `:322`, `rollback_help` `:357-377`, `:423-444`, `:463`); `deploy/scripts/preflight.sh:149-155`, `:183`; `deploy/scripts/host-check.sh:120`, `:171`; `deploy/host-network.md:34`, `:302`
- Modify (guion, Step 3): `deploy/.env.example` (`:41-49`, `:82-213`, `:350`, `:410-414`, `:466-468`)
- Create: `backend/tests/traefik_routes.py`, `backend/tests/test_email_domain_runbook.py` (siete pruebas que se mudan)
- Rewrite: `backend/tests/test_public_surface.py`
- Delete: `backend/tests/test_console_auth_realm.py`, `test_keycloak_init_job.py`, `test_keycloak_init_transport.py`, `test_keycloak_image_pin.py`, `test_deploy_keycloak_registration_edge.py`
- Modify (guion, Step 2): `backend/tests/test_deploy_mcp_route.py`, `test_auth_jwks.py`, `test_platform_backup_scripts.py`, `test_deploy_script_steps.py`

**Interfaces:**
- Depende de: la Task 5 (el código que leía `ITS_KEYCLOAK_*` ya no está) y la Task 4.
- Produce: `tests/traefik_routes.py` (Apéndice A); el nombre de sección `Retiring the old identity provider's data` que `restore.sh`, `backup.sh` y `deploy.sh` citan y la Task 9 crea; `backup.sh` con `ITS_BACKUP_OLD_IDENTITY_DB`; `deploy.sh` con `OBSOLETE_ENV_KEYS`, `obsolete_env_keys()`, `old_identity_containers()`, `remove_old_identity_containers()`, `way_back_backup()`, `stop_if_already_deployed()`, `old_identity_containers_left()` y `verify_old_identity_gone()` (Apéndice A).

**Decisiones de la tarea:** P4-12, P4-13, P4-19, P4-20, P4-23, P4-26, P4-31, P4-37, P4-38; las del operador K1 y K2 (B.1: las copias nocturnas sin `keycloak`, la previa con él, y nada se borra hasta el paso del runbook) y las del coordinador C1, C2 y C7. **Se fusiona junto con la Task 7** (notas de integración).

- [ ] **Step 1: Preparar la worktree**

Task 1, Step 1; fusionar la rama del coordinador con las oleadas 1 y 2, y comprobar que `ls backend/app/core/security.py` responde «No such file».

- [ ] **Step 2: Las pruebas, que fallan**

Crear `backend/tests/traefik_routes.py`:

```python
"""Traefik's router selection, read from the compose labels.

Moved here from `tests/test_deploy_keycloak_registration_edge.py` in own
authentication phase 4, when that file went with the routers it held to
Keycloak's registration edge. The model is the same: highest priority first,
first match wins, a router's default priority being its rule's length, with
Python's `re` standing in for Go's RE2 (the patterns used are in their
common subset). It proves what the RULES say for the paths given; it cannot
prove what Traefik's own normalisation does with them -- the `deploy` job in
CI sends real requests through the real `traefik` service for that.

Used by `test_public_surface.py`, `test_deploy_mcp_route.py` and
`test_auth_jwks.py`.
"""

import functools
import re
from pathlib import Path
from typing import Any

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "deploy/docker-compose.yml"

#: Every method a request to the public entrypoint is checked with.
METHODS = ("GET", "HEAD", "POST", "PUT", "DELETE")

_MATCHER = re.compile(r"(PathRegexp|PathPrefix|Path|Method)\(`([^`]*)`\)")


def compose(path: Path = COMPOSE) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text())
    return loaded


@functools.cache
def routers(path: Path = COMPOSE) -> list[tuple[int, str, str]]:
    """`(priority, name, rule)` for every websecure router in the stack,
    highest priority first."""
    found: dict[str, dict[str, str]] = {}
    for service in compose(path)["services"].values():
        labels = service.get("labels") or {}
        if isinstance(labels, list):
            labels = dict(item.split("=", 1) for item in labels)
        for key, value in labels.items():
            match = re.fullmatch(r"traefik\.http\.routers\.([^.]+)\.(\w+)", key)
            if match:
                # Compose substitutes `${VAR:-default}` before Traefik sees it.
                text = re.sub(r"\$\{[A-Z0-9_]+:-([^}]*)\}", r"\1", str(value))
                found.setdefault(match.group(1), {})[match.group(2)] = text
    return sorted(
        (
            (int(r.get("priority") or len(r["rule"])), name, r["rule"])
            for name, r in found.items()
            if r.get("entrypoints") == "websecure"
        ),
        reverse=True,
    )


def matches(rule: str, path: str, method: str) -> bool:
    """Evaluate a Traefik rule: path and method matchers, `&&`, `||`, `!`,
    parentheses. Any other matcher fails the assertion below, so a router
    using one cannot be silently misread."""

    def one(match: re.Match[str]) -> str:
        kind, arg = match.groups()
        if kind == "PathRegexp":
            hit = re.search(arg, path) is not None
        elif kind == "PathPrefix":
            hit = path.startswith(arg)
        elif kind == "Path":
            hit = path == arg
        else:
            hit = method == arg
        return f" {hit} "

    expression = _MATCHER.sub(one, rule)
    expression = expression.replace("&&", " and ").replace("||", " or ").replace("!", " not ")
    assert re.fullmatch(r"[\s()]*((True|False|and|or|not)[\s()]*)*", expression), expression
    return bool(eval(expression))  # noqa: S307 -- only True/False/and/or/not remain


def winner(path: str, compose_file: Path = COMPOSE) -> str:
    """The router Traefik picks, which must be the same for every method."""
    winners = set()
    for method in METHODS:
        for _, name, rule in routers(compose_file):
            if matches(rule, path, method):
                winners.add(name)
                break
        else:
            winners.add("<none>")
    assert len(winners) == 1, (path, winners)
    return winners.pop()
```

Reescribir `backend/tests/test_public_surface.py` entero:

```python
"""What the public entrypoint routes, pinned.

UNTIL OWN AUTHENTICATION PHASE 4 this file guarded the identity provider's
router: Keycloak's admin console had to stay off the internet while its
sign-in pages (`/realms`, `/resources`, `/js`) were routed, and its account
console had to be closed at the realm because routing could not close it.
Keycloak is gone, and so is every router that led to it (plan 2026-11-15,
Task 6). What this file holds now is the other side of that: NOTHING on :443
reaches an identity provider -- no router, no service, no image, no label --
and the paths that used to be Keycloak's fall to the console, whose nginx
answers its own page.

Nothing here starts a proxy: it reads the committed compose file and models
Traefik's router selection (`tests/traefik_routes.py`). The `deploy` job in
CI sends the same paths through the real `traefik` (deploy/ci/mcp_flow.py,
`--phase closed`).
"""

import re

from tests.traefik_routes import COMPOSE, compose, routers, winner

#: What Keycloak's `kc` router took, with what its registration edge took.
FORMER_IDENTITY_PATHS = (
    "/realms/it-suite/.well-known/openid-configuration",
    "/realms/it-suite/protocol/openid-connect/auth",
    "/realms/it-suite/protocol/openid-connect/token",
    "/realms/it-suite/clients-registrations/openid-connect",
    "/realms/it-suite/clients-registrations/default",
    "/realms/it-suite/account",
    "/resources/abc/login/itms/css/itms.css",
    "/js/keycloak.js",
    "/admin/master/console/",
)


def test_the_compose_file_is_where_this_test_thinks_it_is() -> None:
    """Non-vacuity, first order: a moved file makes every assertion below a
    no-op on an empty list."""
    assert COMPOSE.is_file(), f"{COMPOSE} does not exist; this test measures nothing"
    assert len(routers()) >= 3, "the router model found almost nothing; it is broken"


def test_no_service_image_or_label_of_the_identity_provider_is_left() -> None:
    stack = compose()
    for name, service in stack["services"].items():
        assert "keycloak" not in name.lower(), name
        assert "keycloak" not in str(service.get("image", "")).lower(), (name, service["image"])
        labels = service.get("labels") or {}
        keys = labels if isinstance(labels, dict) else [item.split("=", 1)[0] for item in labels]
        assert not [k for k in keys if re.search(r"routers\.kc[.-]|middlewares\.kc-", k)], name
    assert "keycloak" not in COMPOSE.read_text().lower(), (
        "the compose file still names Keycloak, in a comment or anywhere else"
    )


def test_no_router_leads_to_an_identity_provider() -> None:
    """The :443 routers are the platform's own three, and no other: the api,
    the console, and object storage's presigned URLs. (The gateway's :8443 is
    a TCP router of its own entrypoint, never websecure.)"""
    names = {name for _, name, _ in routers()}
    assert names == {"api", "minio", "web"}, names


def test_the_former_identity_paths_fall_to_the_console() -> None:
    """No router names them, so the console's catch-all takes them and its
    nginx answers its own page: nothing behind :443 is asked about a realm."""
    for path in FORMER_IDENTITY_PATHS:
        assert winner(path) == "web", (path, winner(path))
```

Y el guion de las demás pruebas, guardado en `"${TMPDIR:-/tmp}/p4_task6_tests.py"` y corrido desde la raíz de la worktree con `python3 "${TMPDIR:-/tmp}/p4_task6_tests.py"`:

```python
"""Own authentication phase 4, Task 6: the deployment's tests, written before
the deployment changes. The seven email-domain runbook tests move out of
test_console_auth_realm.py, which goes with the realm; the routing tests read
tests/traefik_routes.py; and deploy.sh, backup.sh, restore.sh and the
preflight gain the tests of what this task makes them do.

Run from the worktree root, after writing tests/traefik_routes.py and the
new tests/test_public_surface.py (Step 2)."""

import ast
import pathlib

root = pathlib.Path(".")
tests = root / "backend/tests"


def swap(path: pathlib.Path, old: str, new: str, count: int = 1) -> None:
    text = path.read_text()
    assert text.count(old) == count, (path, old[:80], text.count(old))
    path.write_text(text.replace(old, new))


# --- the seven email-domain runbook tests move ------------------------------ #
MOVING = (
    "test_the_domain_section_documents_the_preflight_as_a_required_step",
    "test_the_domain_section_documents_the_break_glass",
    "test_the_domain_section_names_all_three_doors",
    "test_the_runbook_does_not_promise_an_email_refresh_the_code_does_not_do",
    "test_the_domain_section_states_the_stored_row_live_claim_divergence",
    "test_the_break_glass_explains_why_a_blank_value_is_actually_safe",
    "test_the_unset_default_is_documented_as_load_bearing",
)
realm = tests / "test_console_auth_realm.py"
source = realm.read_text()
lines = source.splitlines(keepends=True)
bodies = {}
for node in ast.parse(source).body:
    if isinstance(node, ast.FunctionDef) and node.name in MOVING:
        bodies[node.name] = "".join(lines[node.lineno - 1 : node.end_lineno])
assert set(bodies) == set(MOVING), set(MOVING) - set(bodies)
header = '''"""The runbook's email-domain allowlist section, `ITS_CONSOLE_EMAIL_DOMAIN`
(spec `docs/modules/console-auth/spec.md` §3.3; the 2026-08-19 RBAC and
user-lifecycle design §3.3).

Moved, unchanged, from `tests/test_console_auth_realm.py` in own
authentication phase 4 (plan 2026-11-15, Task 6, P4-26): that file held the
Keycloak realm, its theme and its runbook sections, and went with them; the
allowlist and its section stay. Each guard reads ONE isolated section,
because `preflight` appearing SOMEWHERE in a 3,000-line file would satisfy a
whole-file grep while the section an operator actually reads said something
else entirely.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
README = REPO / "deploy/README.md"
ENV_EXAMPLE = REPO / "deploy/.env.example"
DEPS = REPO / "backend/app/api/deps.py"
DOMAIN_HEADING = "## The email-domain allowlist"


def _readme_section(heading: str) -> str:
    """One `## ` section of the runbook, from its heading to the next one."""
    text = README.read_text()
    start = text.index(heading)
    try:
        end = text.index("\\n## ", start + 1)
    except ValueError:  # last section in the file
        end = len(text)
    return text[start:end]


'''
(tests / "test_email_domain_runbook.py").write_text(
    header + "\n\n".join(bodies[name] for name in MOVING)
)

for gone in (
    "test_console_auth_realm.py",
    "test_keycloak_init_job.py",
    "test_keycloak_init_transport.py",
    "test_keycloak_image_pin.py",
    "test_deploy_keycloak_registration_edge.py",
):
    (tests / gone).unlink()


# --- test_deploy_mcp_route.py ----------------------------------------------- #
mr = tests / "test_deploy_mcp_route.py"
swap(
    mr,
    """the sign-in. Routed to the api, they answer FastAPI's JSON 404
(`test_mcp_discovery_fallbacks.py` holds the answers). Keycloak keeps its own
documents under `/realms/<realm>/` until phase 4 removes it.
""",
    """the sign-in. Routed to the api, they answer FastAPI's JSON 404
(`test_mcp_discovery_fallbacks.py` holds the answers). Since own
authentication phase 4 nothing serves `/realms/<realm>/`: the paths Keycloak
answered fall to the console (`test_public_surface.py`).
""",
)
swap(
    mr,
    """The router model (priority, then first match) is the one
`test_deploy_keycloak_registration_edge.py` already holds to the compose file.
""",
    """The router model (priority, then first match) is `tests/traefik_routes.py`.
""",
)
swap(
    mr,
    "from tests.test_deploy_keycloak_registration_edge import _winner\n",
    "from tests.traefik_routes import winner as _winner\n",
)
swap(
    mr,
    '''def test_the_realm_keeps_its_own_discovery_documents() -> None:
    """The realm's documents live under `/realms/<realm>/.well-known/`, which
    begins `/realms` -- no new prefix can take them."""
    for path in (
        "/realms/it-suite/.well-known/openid-configuration",
        "/realms/it-suite/.well-known/oauth-authorization-server",
        "/realms/it-suite/protocol/openid-connect/auth",
        "/realms/it-suite/clients-registrations/openid-connect",
    ):
        assert _winner(path) in {"kc", "kc-registration-oidc"}, (path, _winner(path))
''',
    '''def test_the_former_realm_paths_reach_no_identity_provider() -> None:
    """Was `test_the_realm_keeps_its_own_discovery_documents`: until own
    authentication phase 4 these were Keycloak's (`kc`,
    `kc-registration-oidc`). Now no router names them, and none of the MCP
    prefixes above takes them either: the console's catch-all does, and an
    MCP client reading one gets a page, not a second authorization server."""
    for path in (
        "/realms/it-suite/.well-known/openid-configuration",
        "/realms/it-suite/.well-known/oauth-authorization-server",
        "/realms/it-suite/protocol/openid-connect/auth",
        "/realms/it-suite/clients-registrations/openid-connect",
    ):
        assert _winner(path) == "web", (path, _winner(path))
''',
)
swap(
    tests / "test_auth_jwks.py",
    "from tests.test_deploy_keycloak_registration_edge import _winner\n",
    "from tests.traefik_routes import winner as _winner\n",
)

# --- test_platform_backup_scripts.py ---------------------------------------- #
swap(
    tests / "test_platform_backup_scripts.py",
    """    assert drill["postgres"]["image"] == live["postgres"]["image"]
    assert drill["keycloak"]["image"] == live["keycloak"]["image"]
""",
    """    assert drill["postgres"]["image"] == live["postgres"]["image"]
    # Own authentication phase 4: the drill no longer starts the old identity
    # provider; a `keycloak.dump` is checked in the restored database instead.
    assert set(drill) == {"postgres"}, set(drill)
""",
)
swap(
    tests / "test_platform_backup_scripts.py",
    "# --- the timers ---------------------------------------------------------------\n",
    r'''# --- the old identity provider's database (operator decision K1, 2026-11-15) ---
#
# Own authentication phase 4 removed Keycloak. A host upgraded from before keeps
# its `keycloak` database until the operator retires it, and that database holds
# Keycloak's password hashes and the realm keys that signed its tokens: only the
# backup deploy.sh takes before an upgrade (the way back) keeps it, and
# restore.sh still restores a backup that has it. These run the real scripts
# against fake client tools that answer from FAKE_DBS.

RESTORE_SH = DEPLOY / "scripts/restore.sh"
_FAKE_PG = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
cmd="$1"; shift
case "$cmd" in
  pg_dumpall) printf 'CREATE ROLE its_admin;\nCREATE ROLE app_user;\nCREATE ROLE portal_user;\n' ;;
  pg_dump)
    db="${*: -1}"
    [ "$db" = keycloak ] && [ "${FAKE_EMPTY_KEYCLOAK:-0}" = 1 ] && exit 0
    echo "dump of $db" ;;
  pg_restore) cat > /dev/null ;;
  psql)
    case "$*" in
      *"datname = 'keycloak'"*|*"datname='keycloak'"*)
        case " $FAKE_DBS " in *" keycloak "*) echo 1 ;; *) echo 0 ;; esac ;;
      *"datname='itsuite'"*) echo 1 ;;
      *information_schema.tables*) echo 0 ;;
      *version_num*) echo 0248 ;;
      *pg_policies*) echo 133 ;;
      *relforcerowsecurity*) echo 122 ;;
      *role_table_grants*) echo 0 ;;
      *string_agg*) echo ;;
      *" -f -"*) cat > /dev/null ;;
      *) echo 1 ;;
    esac ;;
esac
"""


def _pg(tmp_path: Path, dbs: str, **env: str) -> dict[str, str]:
    fake = tmp_path / "fakepg.sh"
    fake.write_text(_FAKE_PG)
    log = str(tmp_path / "calls")
    return {"ITS_PG_EXEC": f"bash {fake}", "FAKE_LOG": log, "FAKE_DBS": dbs, **env}


def test_a_nightly_backup_leaves_the_old_identity_providers_database_out(tmp_path: Path) -> None:
    """While the database is still there, and says so: the reminder that it is."""
    run = _bash(f'"{BACKUP_SH}" "{tmp_path}/nightly"', **_pg(tmp_path, "itsuite keycloak"))
    assert run.returncode == 0, run.stderr
    assert (tmp_path / "nightly/itsuite.dump").read_text() == "dump of itsuite\n"
    assert not (tmp_path / "nightly/keycloak.dump").exists()
    assert "pg_dump -Fc -U its_admin keycloak" not in (tmp_path / "calls").read_text()
    assert "Retiring the old identity provider's data" in run.stdout, run.stdout


def test_the_pre_upgrade_backup_keeps_it_while_it_exists(tmp_path: Path) -> None:
    flag = {"ITS_BACKUP_OLD_IDENTITY_DB": "1"}
    kept = _bash(f'"{BACKUP_SH}" "{tmp_path}/kept"', **_pg(tmp_path, "itsuite keycloak", **flag))
    assert kept.returncode == 0, kept.stderr
    assert (tmp_path / "kept/keycloak.dump").read_text() == "dump of keycloak\n"
    gone = _bash(f'"{BACKUP_SH}" "{tmp_path}/gone"', **_pg(tmp_path, "itsuite", **flag))
    assert gone.returncode == 0, gone.stderr
    assert not (tmp_path / "gone/keycloak.dump").exists()
    assert "--> keycloak" not in gone.stdout, gone.stdout


def test_an_empty_dump_of_it_fails_the_backup(tmp_path: Path) -> None:
    env = _pg(tmp_path, "itsuite keycloak", ITS_BACKUP_OLD_IDENTITY_DB="1", FAKE_EMPTY_KEYCLOAK="1")
    run = _bash(f'"{BACKUP_SH}" "{tmp_path}/bk"', **env)
    assert run.returncode != 0
    assert "keycloak.dump is missing or empty" in run.stderr, run.stderr


def test_the_flag_is_zero_or_one_and_checked_before_any_dump(tmp_path: Path) -> None:
    env = _pg(tmp_path, "itsuite", ITS_BACKUP_OLD_IDENTITY_DB="yes")
    run = _bash(f'"{BACKUP_SH}" "{tmp_path}/bk"', **env)
    assert run.returncode != 0 and "ITS_BACKUP_OLD_IDENTITY_DB" in run.stderr
    assert not (tmp_path / "calls").exists(), "a database was dumped before the flag was checked"


def test_only_deploy_sh_asks_for_it_and_the_nightly_run_says_no() -> None:
    deploy_sh = (DEPLOY / "scripts/deploy.sh").read_text()
    assert 'ITS_BACKUP_OLD_IDENTITY_DB=1 ./scripts/backup.sh "$BACKUP_DIR"' in deploy_sh
    nightly = SCHEDULED.read_text()
    assert 'ITS_BACKUP_OLD_IDENTITY_DB=0 "$DEPLOY_DIR/scripts/backup.sh" "$dest"' in nightly


def test_a_backup_with_the_old_database_restores_it_and_the_next_backup_has_none(
    tmp_path: Path,
) -> None:
    """A pre-upgrade backup, restored on a host that had already dropped the
    database: restore.sh creates it, restores it and says what it is. The
    nightly backup after that leaves it out again."""
    flag = {"ITS_BACKUP_OLD_IDENTITY_DB": "1"}
    pre = _bash(f'"{BACKUP_SH}" "{tmp_path}/pre"', **_pg(tmp_path, "itsuite keycloak", **flag))
    assert pre.returncode == 0, pre.stderr
    (tmp_path / "calls").unlink()
    restored = _bash(f'"{RESTORE_SH}" "{tmp_path}/pre"', **_pg(tmp_path, "itsuite"))
    assert restored.returncode == 0, restored.stdout + restored.stderr
    calls = (tmp_path / "calls").read_text()
    assert "CREATE DATABASE keycloak OWNER its_admin" in calls, calls
    assert "pg_restore -U its_admin -d keycloak" in calls, calls
    assert "keep it if you are rolling back" in restored.stdout, restored.stdout
    assert restored.stdout.rstrip().endswith("==> OK"), restored.stdout
    after = _bash(f'"{BACKUP_SH}" "{tmp_path}/after"', **_pg(tmp_path, "itsuite keycloak"))
    assert after.returncode == 0, after.stderr
    assert not (tmp_path / "after/keycloak.dump").exists()


def test_a_backup_without_it_restores_without_touching_it(tmp_path: Path) -> None:
    assert _bash(f'"{BACKUP_SH}" "{tmp_path}/bk"', **_pg(tmp_path, "itsuite")).returncode == 0
    (tmp_path / "calls").unlink()
    restored = _bash(f'"{RESTORE_SH}" "{tmp_path}/bk"', **_pg(tmp_path, "itsuite keycloak"))
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert "keycloak" not in (tmp_path / "calls").read_text()
    assert "keycloak.dump restored" not in restored.stdout


# --- the timers ---------------------------------------------------------------
''',
)

# --- test_deploy_script_steps.py -------------------------------------------- #
ds = tests / "test_deploy_script_steps.py"
text = ds.read_text()
start = text.index("def _keycloak_branch() -> str:\n")
end = text.index("def test_every_one_shot_checks_its_exit_code() -> None:\n")
text = (
    text[:start]
    + '''def test_no_step_starts_an_identity_provider() -> None:
    """Own authentication phase 4. deploy.sh restarted `keycloak` on a realm
    or theme change and ran `keycloak-init` on every deploy; both services
    left the compose file, and a step naming either would stop the deploy
    with `no such service` -- after the migration had already run. No compose
    command and no one-shot names either now: the two names are left only
    where the script finds and removes the old containers (below). (Replaces
    `test_a_theme_change_is_detected_at_all`,
    `test_a_realm_or_theme_change_restarts_keycloak_not_only_keycloak_init`
    and `test_keycloak_init_runs_on_every_deploy_not_only_on_a_realm_change`,
    which pinned those steps.)"""
    steps = _SH_COMMENT.sub("", _script())
    assert not re.search(r"NEED_KEYCLOAK|KC_IMAGE_CHANGED", steps)
    commands = [
        line.strip()
        for line in steps.splitlines()
        if '"${COMPOSE[@]}"' in line or "run_oneshot " in line
    ]
    assert [c for c in commands if "keycloak" in c.lower()] == [], commands


def test_no_up_removes_orphans() -> None:
    """The coordinator's decision (2026-11-15): `up --remove-orphans` would
    also remove any container the operator added to this project. The old
    identity provider's two containers go by name instead (below)."""
    assert "--remove-orphans" not in _SH_COMMENT.sub("", _script())


def test_obsolete_env_keys_are_named_and_never_edited() -> None:
    """The keys an upgraded `.env` still holds are named once, and the file is
    left byte for byte as it was (the script never edits `.env` beyond
    IT_SUITE_VERSION). `.env.example` ships none of them."""
    listed = re.search(r"OBSOLETE_ENV_KEYS=\\((.*?)\\)", _script(), re.DOTALL)
    assert listed is not None
    keys = listed.group(1).split()
    assert len(keys) == 17 and len(set(keys)) == 17, keys
    example = (REPO / "deploy/.env.example").read_text()
    shipped = [k for k in keys if re.search(rf"^#?\\s*{k}=", example, re.MULTILINE)]
    assert shipped == [], shipped
    script = f"""
set -euo pipefail
cd "$(mktemp -d)"
printf '%s\\\\n' 'ITS_OIDC_ISSUER=https://x/realms/it-suite' 'KEYCLOAK_ADMIN_PASSWORD=p' \\\\
    'ITS_DEFAULT_ORG_SLUG=toeshee' > .env
cp .env before
note() {{ printf '%s\\\\n' "$*"; }}
OBSOLETE_ENV_KEYS=({" ".join(keys)})
{_fn("obsolete_env_keys")}
obsolete_env_keys
cmp -s .env before && echo UNCHANGED
"""
    run = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    assert "2 key(s)" in run.stdout, run.stdout
    assert "KEYCLOAK_ADMIN_PASSWORD ITS_OIDC_ISSUER" in run.stdout, run.stdout
    assert "ITS_DEFAULT_ORG_SLUG" not in run.stdout.split("\\n", 1)[1], run.stdout
    assert "UNCHANGED" in run.stdout, run.stdout
    # Not "harmless; remove them": the release before needs four of them
    # (operator decision K1, 2026-11-15).
    assert "Do NOT remove them yet" in run.stdout, run.stdout
    assert "Retiring the old identity provider's data" in run.stdout, run.stdout
    assert "Harmless" not in _script()


'''
    + text[end:]
)


def once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, (old[:80], text.count(old))
    return text.replace(old, new)


text = once(
    text,
    'for svc in ("migrate", "postgres-grants", "openbao-init", "keycloak-init"):',
    'for svc in ("migrate", "postgres-grants", "openbao-init"):',
)
# The rollback harness gains the old compose file `git show` prints and the
# .env the rollback is printed against.
text = once(
    text,
    r'''    migrate: int = 0,
    keycloak: int = 0,
    compose: int = 0,
    kc_image: int = 0,
) -> str:
    return f"""
set -euo pipefail
''',
    r'''    migrate: int = 0,
    compose: int = 0,
    old_compose: str = "",
    env: str = "",
) -> str:
    # OLD_COMPOSE is what `git show <base>:deploy/docker-compose.yml` prints;
    # ENV is the .env the rollback is printed against.
    return f"""
set -euo pipefail
cd "$(mktemp -d)"
printf '%b' {env!r} > .env
git() {{ case "$*" in *" show "*) printf '%b' {old_compose!r} ;; *) return 1 ;; esac; }}
''',
)
text = once(
    text,
    """NEED_MIGRATE={migrate}; NEED_KEYCLOAK={keycloak}; NEED_COMPOSE={compose}
KC_IMAGE_CHANGED={kc_image}
""",
    """NEED_MIGRATE={migrate}; NEED_COMPOSE={compose}
""",
)
text = once(
    text,
    """    assert "audit" in out and "DROPS BOTH DATABASES" in out, out""",
    """    assert "audit" in out and "DROPS THE DATABASES IN THE BACKUP" in out, out""",
)
start = text.index("def test_a_compose_or_theme_change_is_undone_from_git_not_by_restoring_the_database() -> None:\n")
end = text.index("def test_a_run_that_dies_part_way_still_prints_how_to_go_back() -> None:\n")
text = (
    text[:start]
    + r'''def test_a_compose_change_checks_the_whole_deploy_directory_out() -> None:
    """Restoring Postgres undoes no compose change -- the running compose file
    and what it bind-mounts come from the checkout -- and costs every write
    and audited action since the backup. And the compose file alone is not
    enough: an earlier release's compose bind-mounts files of that release
    (the old identity provider's realm and theme, until own authentication
    phase 4) which the new checkout no longer has. So `deploy`, whole. (Was
    `test_a_compose_or_theme_change_is_undone_from_git_not_by_restoring_the_database`
    and, with it, `test_a_keycloak_image_change_does_need_the_restore`.)"""
    out = _rollback(compose=1)
    assert f"checkout {BASE[:12]} -- deploy\n" in out, out
    assert "restore.sh" not in out, f"a compose change sent to a database restore:\n{out}"
    assert "restart" not in out, out


def test_a_rollback_past_phase_4_names_the_env_keys_it_needs() -> None:
    """The release before own authentication phase 4 needs four keys this one
    does not read: its compose file does not parse without the two
    KEYCLOAK_ADMIN_*, and its api, gateway, worker and migrate do not start
    without the two ITS_OIDC_*. If .env lost any, the rollback says so
    before the step that would fail."""
    old = "      KC_BOOTSTRAP_ADMIN_USERNAME: ${KEYCLOAK_ADMIN_USER:?set in .env}\n"
    out = _rollback(
        migrate=1,
        compose=1,
        old_compose=old,
        env="KEYCLOAK_ADMIN_USER=admin\nITS_OIDC_ISSUER=https://x/realms/it-suite\n",
    )
    warning = _step(out, "no longer has")
    assert "KEYCLOAK_ADMIN_PASSWORD ITS_OIDC_JWKS_URL" in out.splitlines()[warning], out
    edit = _step(out, "IT_SUITE_VERSION=53b02f81")
    assert warning < edit < _step(out, "docker compose up -d"), out
    four = ("KEYCLOAK_ADMIN_USER", "KEYCLOAK_ADMIN_PASSWORD", "ITS_OIDC_ISSUER")
    four += ("ITS_OIDC_JWKS_URL",)
    kept = "".join(f"{key}=x\n" for key in four)
    assert "no longer has" not in _rollback(migrate=1, compose=1, old_compose=old, env=kept)
    assert "no longer has" not in _rollback(migrate=1, compose=1, old_compose="services: {}\n")


'''
    + text[end:]
)

# The old identity provider's containers, the half-done upgrade, the obsolete
# keys outside deploy.sh and the preflight's host: after the obsolete-keys test.
text = once(
    text,
    "def test_every_one_shot_checks_its_exit_code() -> None:\n",
    r'''def _containers(listing: str) -> str:
    """deploy.sh's two container functions, with `docker compose ps -a`
    answering LISTING (one `name service` per line) and every other docker
    call written to ./calls."""
    return f"""
set -euo pipefail
cd "$(mktemp -d)"
say()  {{ printf '%s\\n' "$*"; }}
note() {{ printf '%s\\n' "$*"; }}
warn() {{ printf 'WARNING %s\\n' "$*"; }}
docker() {{
  case "$1 $2" in
    "compose ps") printf '%b' {listing!r} ;;
    *) echo "docker $*" >> calls ;;
  esac
}}
COMPOSE=(docker compose)
{_fn("old_identity_containers")}
{_fn("remove_old_identity_containers")}
"""


def test_the_old_identity_providers_containers_go_by_name_and_nothing_else() -> None:
    """Compose leaves a container whose service left the file RUNNING, and
    Traefik routes to it by the labels on the container: Keycloak, its
    anonymous client registration included, would go on answering on :443.
    Exactly `keycloak` and `keycloak-init`, by service name -- not the
    operator's own `guacd`, not a `keycloak-proxy` of theirs (the
    coordinator's decision, 2026-11-15)."""
    listing = (
        "it-suite-api-1 api\n"
        "it-suite-keycloak-1 keycloak\n"
        "it-suite-keycloak-init-1 keycloak-init\n"
        "it-suite-guacd-1 guacd\n"
        "it-suite-keycloak-proxy-1 keycloak-proxy\n"
    )
    run = _run_bash(_containers(listing) + "remove_old_identity_containers\ncat calls\n")
    assert run.returncode == 0, run.stdout + run.stderr
    calls = [line for line in run.stdout.splitlines() if line.startswith("docker ")]
    assert calls == [
        "docker stop it-suite-keycloak-1",
        "docker rm it-suite-keycloak-1",
        "docker stop it-suite-keycloak-init-1",
        "docker rm it-suite-keycloak-init-1",
    ], run.stdout


def test_removing_them_twice_is_a_no_op() -> None:
    script = _containers("it-suite-api-1 api\n")
    run = _run_bash(script + "remove_old_identity_containers\ntest ! -e calls && echo NO_CALLS\n")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "none left" in run.stdout and "NO_CALLS" in run.stdout, run.stdout


def test_they_go_after_the_new_app_tier_is_up() -> None:
    """Until then, a deploy that stops leaves the release before able to run."""
    text = _SH_COMMENT.sub("", _script())
    up = text.index('\n"${COMPOSE[@]}" up -d\n')
    removal = text.index("\nremove_old_identity_containers\n")
    assert up < removal < text.index('say "Verifying"')


def _already(listing: str, home: str = "") -> str:
    """`stop_if_already_deployed` after a pull that brought nothing, with
    HOME (where `backups/pre-upgrade-*` live) a directory of its own."""
    return _containers(listing) + f"""
HOME={home!r}; [ -n "$HOME" ] || HOME="$PWD"
git() {{ echo 2f20afbe00000000000000000000000000000000; }}
BASELINE={BASE!r}; AFTER=2f20afbe00000000000000000000000000000000
SHORT_AFTER=2f20afbe; REPO_ROOT=/opt/it-suite; RUNNING_TAG=2f20afbe
{_fn("way_back_backup")}
{_fn("stop_if_already_deployed")}
ROLLBACK_BACKUP_DIR=""; ROLLBACK_BASE=""
stop_if_already_deployed
echo "CONTINUED baseline=[$BASELINE] running=[$RUNNING_TAG]"
echo "back=[$ROLLBACK_BACKUP_DIR] base=[$ROLLBACK_BASE]"
"""


def _backups(home: Path) -> dict[str, Path]:
    """What a host holds after the deploy.sh of the release before phase 4
    stopped part-way: an older pre-upgrade backup, the one it took (0247,
    images 53b02f81), and one taken past 0248 by an earlier finishing try."""
    made = {}
    for name, rev, tag in (
        ("pre-upgrade-2026-11-01T0900Z", "0246", "1a2b3c4d"),
        ("pre-upgrade-2026-11-20T1000Z", "0247", "53b02f81"),
        ("pre-upgrade-2026-11-20T1015Z", "0248", "2f20afbe"),
    ):
        made[rev] = home / "backups" / name
        made[rev].mkdir(parents=True)
        (made[rev] / "alembic_version").write_text(f"{rev}\n")
        (made[rev] / "image_version").write_text(f"IT_SUITE_VERSION={tag}\n")
    return made


def test_a_half_done_upgrade_is_finished_not_called_already_deployed() -> None:
    """The deploy.sh of a release before own authentication phase 4, run as
    usual, pulls this release, records its tag and migrates, then stops at
    `restart keycloak`. Re-run, this script read the new tag as running and
    answered "Already deployed" with exit 0, the app tier still on the old
    images. While either old container is there it runs a full deploy."""
    for old in ("it-suite-keycloak-1 keycloak\n", "it-suite-keycloak-init-1 keycloak-init\n"):
        run = _run_bash(_already("it-suite-api-1 api\n" + old))
        assert run.returncode == 0, run.stdout + run.stderr
        assert "Already deployed" not in run.stdout, run.stdout
        assert "CONTINUED baseline=[]" in run.stdout and "full deploy" in run.stdout, run.stdout
    done = _run_bash(_already("it-suite-api-1 api\n"))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Already deployed" in done.stdout and "CONTINUED" not in done.stdout, done.stdout


def test_the_way_back_from_a_half_done_upgrade_is_the_backup_from_before_0248(
    tmp_path: Path,
) -> None:
    """Not this run's backup, which is past 0248, and not the newest one that
    holds `keycloak.dump`: the newest `pre-upgrade-*` whose schema is below
    0248, with the images its `image_version` names. The closing rollback
    help prints that backup, that tag and that commit."""
    made = _backups(tmp_path)
    finishing = tmp_path / "backups" / "pre-upgrade-2026-11-20T1030Z"
    script = _already("it-suite-keycloak-1 keycloak\n", home=str(tmp_path)) + f"""
{_fn("rollback_help")}
BACKUP_DIR={str(finishing)!r}; NEED_MIGRATE=1; NEED_COMPOSE=1
rollback_help
"""
    run = _run_bash(script)
    assert run.returncode == 0, run.stdout + run.stderr
    assert f"back=[{made['0247']}]" in run.stdout, run.stdout
    assert "running=[53b02f81]" in run.stdout and "base=[53b02f81]" in run.stdout, run.stdout
    help_ = run.stdout[run.stdout.index("If you need to go back") :]
    assert f"ITS_RESTORE_FORCE=1 ./scripts/restore.sh {made['0247']}\n" in help_, help_
    assert "IT_SUITE_VERSION=53b02f81" in help_ and "checkout 53b02f81 -- deploy" in help_, help_
    assert "There is no image to swap back to" not in help_, help_
    restore = [line for line in help_.splitlines() if "restore.sh" in line]
    assert str(finishing) not in "\n".join(restore), help_


def test_without_a_backup_from_before_0248_it_says_so(tmp_path: Path) -> None:
    (tmp_path / "backups" / "pre-upgrade-2026-11-20T1015Z").mkdir(parents=True)
    (tmp_path / "backups" / "pre-upgrade-2026-11-20T1015Z" / "alembic_version").write_text("0248\n")
    run = _run_bash(_already("it-suite-keycloak-1 keycloak\n", home=str(tmp_path)))
    assert run.returncode == 0, run.stdout + run.stderr
    assert "back=[] base=[]" in run.stdout and "from before migration 0248" in run.stdout


def _verify(left: str) -> str:
    return f"""
set -euo pipefail
cd "$(mktemp -d)"
note() {{ printf '%s\\n' "$*"; }}
warn() {{ printf 'WARNING %s\\n' "$*"; }}
docker() {{
  echo "docker $*" >> calls
  case "$*" in
    "compose config") printf 'name: it-suite\\nservices:\\n  api: {{}}\\n' ;;
    *"service=keycloak") printf '%b' {left!r} ;;
  esac
}}
COMPOSE=(docker compose)
{_fn("old_identity_containers_left")}
{_fn("verify_old_identity_gone")}
"""


def test_the_verify_step_fails_while_a_container_of_it_is_left() -> None:
    """By the containers' own compose labels, not by `compose ps`: if that
    did not list a container whose service left the file, the removal would
    say "none left" and Traefik would go on routing to Keycloak."""
    check = "verify_old_identity_gone && echo PASSED || echo FAILED\n"
    left = _run_bash(_verify("3f1c9a\n") + check + "cat calls\n")
    assert "FAILED" in left.stdout and "docker rm -f 3f1c9a" in left.stdout, left.stdout
    assert "label=com.docker.compose.project=it-suite" in left.stdout, left.stdout
    gone = _run_bash(_verify("") + check)
    assert "PASSED" in gone.stdout and "no container of the old identity provider" in gone.stdout
    text = _SH_COMMENT.sub("", _script())
    assert text.index('say "Verifying"') < text.index("\nverify_old_identity_gone || FAIL=1\n")


def test_the_check_runs_before_the_diff_is_taken() -> None:
    text = _SH_COMMENT.sub("", _script())
    call = text.index("\nstop_if_already_deployed\n")
    assert text.index("\nold_identity_containers() {") < call < text.index('CHANGED="$(git')


OBSOLETE = re.compile(
    r"KEYCLOAK_ADMIN_(USER|PASSWORD)|ITS_OIDC_[A-Z_]+|ITS_MULTI_TENANT"
    r"|ENTRA_(TENANT_ID|CLIENT_ID|CLIENT_SECRET)|ITS_KEYCLOAK_[A-Z_]+"
)


def test_no_other_deploy_file_names_an_obsolete_key() -> None:
    """A script that still READS one is the failure: `preflight.sh` read
    `ITS_OIDC_ISSUER`, which a fresh `.env` no longer has, and refused every
    new install. deploy.sh names them on purpose (its list and the rollback
    check). The runbook is Task 9's guard's (`deploy/README.md`,
    `deploy/GO-LIVE.md`), and `deploy/keycloak/` holds only Task 7's
    `ci_mcp_flow.py`, which that task removes."""
    exempt = {DEPLOY_SH, REPO / "deploy/README.md", REPO / "deploy/GO-LIVE.md"}
    found = [
        f"{path.relative_to(REPO)}:{number}"
        for path in sorted((REPO / "deploy").rglob("*"))
        if path.is_file()
        and path not in exempt
        and "keycloak" not in path.relative_to(REPO).parts
        for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1)
        if OBSOLETE.search(line)
    ]
    assert found == [], found


def test_the_preflight_checks_the_certificate_against_the_console_address(tmp_path: Path) -> None:
    """Its host comes from `ITS_PUBLIC_CONSOLE_URL` (until own authentication
    phase 4, `ITS_OIDC_ISSUER`, which a fresh `.env` no longer has), and a
    blank value gives no host -- not the whole line, which `sed` without
    `-n ...p` printed."""
    text = (REPO / "deploy/scripts/preflight.sh").read_text()
    found = re.search(
        r"^  host=\"\$\(grep -E '\^ITS_PUBLIC_CONSOLE_URL=' \.env.*?\|\| true\)\"$",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert found is not None, "preflight.sh does not take the host from ITS_PUBLIC_CONSOLE_URL"
    for value, host in (
        ("https://itms.example.test", "itms.example.test"),
        ("https://itms.example.test:8443/", "itms.example.test"),
        ("", ""),
        ("https://your-host", "your-host"),
    ):
        env = f"ITS_DEFAULT_ORG_SLUG=toeshee\nITS_PUBLIC_CONSOLE_URL={value}\n"
        (tmp_path / ".env").write_text(env)
        run = subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + found.group(0) + '\nprintf "[%s]" "$host"'],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            check=True,
        )
        assert run.stdout == f"[{host}]", (value, run.stdout)


def test_every_one_shot_checks_its_exit_code() -> None:
''',
)
ds.write_text(text)
print("ok")
```

Expected: `ok`. Lo que hace: las siete pruebas de la sección del runbook «The email-domain allowlist» se copian, sin cambiar, a `tests/test_email_domain_runbook.py` con los ayudantes que usan (P4-26), y `test_console_auth_realm.py` se borra con las otras 96 (Apéndice C); se borran las pruebas de `init-clients.py`, del pin de la imagen y del borde de registro de Keycloak; `test_deploy_mcp_route.py` y `test_auth_jwks.py` leen `tests/traefik_routes.py`, y la prueba de los documentos del realm pasa a decir que esas rutas caen en la consola; el simulacro solo tiene `postgres`; `test_platform_backup_scripts.py` gana siete pruebas de K1, que corren `backup.sh` y `restore.sh` de verdad con un `ITS_PG_EXEC` falso (la copia nocturna deja fuera la base vieja y lo dice; la previa la guarda mientras exista; un volcado vacío falla; la opción se comprueba antes de volcar; solo `deploy.sh` la pide y `scheduled-backup.sh` la niega; una copia con `keycloak.dump` lo restaura en un host que ya no tiene la base, y la copia nocturna siguiente no lo lleva; una copia sin él no toca `keycloak`); y `test_deploy_script_steps.py` cambia sus pruebas de Keycloak por las del Apéndice C: ningún paso arranca un proveedor de identidad, ningún `up` quita huérfanos, los dos contenedores se van por su nombre y nada más (con un `docker` simulado que lista también un `guacd` y un `keycloak-proxy` del operador), dos veces es lo mismo que una, después de la capa nueva, una actualización a medias se termina en vez de responder «Already deployed», y su vuelta atrás es la copia de antes de 0248 con su etiqueta, no la de esta ejecución (y lo dice si no hay ninguna), la verificación falla mientras quede un contenedor de los dos, las claves obsoletas se nombran sin tocar `.env` y con el aviso de no borrarlas, ningún otro archivo de `deploy/` las nombra, el preflight saca el host de `ITS_PUBLIC_CONSOLE_URL`, la vuelta atrás saca `deploy` entero y avisa si faltan las cuatro claves.

Run: `.venv/bin/pytest -q tests/test_public_surface.py tests/test_deploy_script_steps.py tests/test_platform_backup_scripts.py tests/test_deploy_mcp_route.py`
Expected: 28 FAIL y 48 PASS (en el ensayo de la segunda ronda de correcciones, sobre las oleadas 1 y 2 de los guiones corregidos). Fallan las tres de `test_public_surface.py` (el servicio `keycloak`, los routers `kc*`, las rutas antiguas llegan a `kc`), `test_the_former_realm_paths_reach_no_identity_provider`, la del simulacro, las siete de K1 menos `test_an_empty_dump_of_it_fails_the_backup` (que el `backup.sh` de hoy ya cumple: vuelca `keycloak` siempre y exige el volcado no vacío), y en `test_deploy_script_steps.py` las nuevas menos `test_no_up_removes_orphans` (el guion de hoy no quita huérfanos, y la prueba fija que siga sin hacerlo), más las cinco de la vuelta atrás que llegan a leer `NEED_KEYCLOAK` en el `rollback_help` de hoy (su arnés ya no lo define, y la función lo lee con `set -u`).

- [ ] **Step 3: El despliegue, con dos guiones**

Guardados en `"${TMPDIR:-/tmp}/"` y corridos desde la raíz de la worktree, uno detrás de otro:

```python
"""Own authentication phase 4, Task 6: the deployment without Keycloak --
compose, deploy/keycloak (but ci_mcp_flow.py, which is Task 7's), the
Postgres first boot, the backup, restore, drill and deploy scripts, the
preflight, host-check.sh and host-network.md.

Run from the worktree root: python3 p4_task6_deploy.py. Every edit asserts
its anchor is found exactly once."""

import pathlib
import shutil

root = pathlib.Path(".")
deploy = root / "deploy"


def swap(path: pathlib.Path, old: str, new: str) -> None:
    text = path.read_text()
    assert text.count(old) == 1, (path, old[:80], text.count(old))
    path.write_text(text.replace(old, new))


def cut(path: pathlib.Path, start: str, end: str, new: str = "") -> None:
    text = path.read_text()
    assert text.count(start) == 1, (path, start[:80])
    i = text.index(start)
    j = text.index(end, i)
    path.write_text(text[:i] + new + text[j:])


# --- compose --------------------------------------------------------------- #
compose = deploy / "docker-compose.yml"
swap(
    compose,
    """      # The MCP server (spec 2026-10-09 §2): `/mcp` itself and its RFC 9728
      # metadata, `/.well-known/oauth-protected-resource[/mcp]`. The other two
      # `/.well-known/` prefixes are discovery FALLBACKS and are here so they
      # answer 404: an MCP client looks for the realm's metadata at
      # `/.well-known/oauth-authorization-server/realms/<realm>` and
      # `/.well-known/openid-configuration/realms/<realm>` before trying
      # `/realms/<realm>/.well-known/...`, moves on after a 4xx -- and fails
      # the whole sign-in on the SPA's 200 `index.html`, which is what `web`
      # would answer. The realm's own documents begin `/realms` and stay on
      # `kc`. backend/tests/test_deploy_mcp_route.py holds all of this.
""",
    """      # The MCP server (spec 2026-10-09 §2): `/mcp` itself and its RFC 9728
      # metadata, `/.well-known/oauth-protected-resource[/mcp]`.
      # `/.well-known/openid-configuration` is here so that it answers the
      # api's JSON 404: an MCP client "MUST support both discovery
      # mechanisms" (RFC 8414 and OpenID Connect Discovery), moves on after a
      # 4xx -- and fails the whole sign-in on the SPA's 200 `index.html`,
      # which is what `web` would answer. backend/tests/test_deploy_mcp_route.py
      # holds all of this. Since own authentication phase 4 no router names
      # `/realms`, `/resources`, `/js` or `/admin`: they fall to `web`
      # (backend/tests/test_public_surface.py).
""",
)
swap(
    compose,
    """      keycloak:
        condition: service_started
""",
    "",
)
swap(
    compose,
    """      # rather than via `env_file: .env` so the database container never sees
      # the application's OpenBao token, S3 keys or OIDC config. Without""",
    """      # rather than via `env_file: .env` so the database container never sees
      # the application's OpenBao token or S3 keys. Without""",
)
cut(compose, "  keycloak:\n    # PINNED AT 26.2 AS A FEATURE FLOOR", "  openbao:\n")

# --- deploy/keycloak (ci_mcp_flow.py is Task 7's) --------------------------- #
for name in ("init-clients.py", "realm-it-suite.json"):
    (deploy / "keycloak" / name).unlink()
shutil.rmtree(deploy / "keycloak" / "themes")

# --- postgres first boot ---------------------------------------------------- #
init = deploy / "postgres/init-app-user.sh"
swap(
    init,
    "# First-boot init: RLS-constrained application role + Keycloak database.\n",
    "# First-boot init: the RLS-constrained application role, and portal_user's\n"
    "# password. (Until own authentication phase 4 it also created the identity\n"
    "# provider's database; a fresh install has none.)\n",
)
swap(
    init,
    """    -- their own tickets.
    CREATE DATABASE keycloak OWNER its_admin;
EOSQL""",
    """    -- their own tickets.
EOSQL""",
)

# --- backup.sh -------------------------------------------------------------- #
backup = deploy / "scripts/backup.sh"
swap(
    backup,
    """#   keycloak.dump   the realm, users and federation config -- losing this
#                   locks every operator out even with an intact itsuite
""",
    """#   keycloak.dump   ONLY in the backup deploy.sh takes before an upgrade
#                   (ITS_BACKUP_OLD_IDENTITY_DB=1), and only while a
#                   `keycloak` database exists: own authentication phase 4
#                   removed Keycloak, and a host upgraded from before keeps
#                   that database until the operator retires it. A nightly
#                   or off-site backup never holds it; a fresh install has
#                   none.
""",
)
swap(
    backup,
    """echo "--> keycloak"
run pg_dump -Fc -U "$ITS_PG_ADMIN_USER" keycloak > "$OUT_DIR/keycloak.dump"
""",
    """# THE OLD IDENTITY PROVIDER'S DATABASE (operator decision K1, 2026-11-15). A
# host upgraded from before own authentication phase 4 keeps `keycloak` until
# the operator retires it (deploy/README.md "Retiring the old identity
# provider's data"). It holds Keycloak's password hashes and the realm keys
# that signed its tokens, so it goes ONLY into the backup deploy.sh takes
# before an upgrade (the one before the phase-4 upgrade, with its schema below
# 0248, is the way back to a release from before it), and never into a
# nightly or off-site one. The note below is the reminder that
# it is still there. A -tAc scalar into a string compare, not
# `psql | grep -q`: under pipefail a grep that exits early SIGPIPEs psql
# (restore.sh's prepare_db says the same).
has_keycloak=$(run psql -X -A -t -U "$ITS_PG_ADMIN_USER" -d itsuite \\
    -c "SELECT count(*) FROM pg_database WHERE datname = 'keycloak'")
with_keycloak=0
if [ "${has_keycloak//[[:space:]]/}" = "1" ]; then
    if [ "$ITS_BACKUP_OLD_IDENTITY_DB" = 1 ]; then
        echo "--> keycloak (the old identity provider's database: the way back past phase 4)"
        run pg_dump -Fc -U "$ITS_PG_ADMIN_USER" keycloak > "$OUT_DIR/keycloak.dump"
        with_keycloak=1
    else
        echo "    NOTE: the old identity provider's database ('keycloak') is still on this"
        echo "    cluster. This backup leaves it out; only deploy.sh's pre-upgrade backup"
        echo "    keeps it. deploy/README.md \\"Retiring the old identity provider's data\\""
        echo "    removes it once you no longer need the way back."
    fi
fi
""",
)
swap(
    backup,
    """for f in globals.sql itsuite.dump keycloak.dump alembic_version policy_count image_version; do""",
    """artefacts="globals.sql itsuite.dump alembic_version policy_count image_version"
[ "$with_keycloak" = 1 ] && artefacts="$artefacts keycloak.dump"
for f in $artefacts; do""",
)
swap(
    backup,
    """case "$ITS_BACKUP_OBJECTS" in
    none|all|no-fleet-backups) ;;
    *) echo "ERROR: ITS_BACKUP_OBJECTS must be none, all or no-fleet-backups" >&2; exit 1 ;;
esac

# Resolve""",
    """case "$ITS_BACKUP_OBJECTS" in
    none|all|no-fleet-backups) ;;
    *) echo "ERROR: ITS_BACKUP_OBJECTS must be none, all or no-fleet-backups" >&2; exit 1 ;;
esac
# 1 only from deploy.sh's pre-upgrade backup (the keycloak.dump note above).
: "${ITS_BACKUP_OLD_IDENTITY_DB:=0}"
case "$ITS_BACKUP_OLD_IDENTITY_DB" in
    0|1) ;;
    *) echo "ERROR: ITS_BACKUP_OLD_IDENTITY_DB must be 0 or 1" >&2; exit 1 ;;
esac

# Resolve""",
)
swap(
    backup,
    "#   ITS_PG_EXEC=\"\" PGHOST=127.0.0.1 PGUSER=its_admin ./backup.sh /tmp/bk\n",
    "#   ITS_PG_EXEC=\"\" PGHOST=127.0.0.1 PGUSER=its_admin ./backup.sh /tmp/bk\n"
    "#\n"
    "# ITS_BACKUP_OLD_IDENTITY_DB=1 also dumps the old identity provider's\n"
    "# database while it exists. Only deploy.sh sets it, for its pre-upgrade backup.\n",
)

# The nightly run says so itself rather than trusting the environment it was
# started from: it never holds the old identity provider's database (K1).
swap(
    deploy / "scripts/scheduled-backup.sh",
    """    ITS_BACKUP_OBJECTS="$OBJECTS" ITS_BACKUP_OBJECTS_DIR="$BACKUP_ROOT/objects" \\
        "$DEPLOY_DIR/scripts/backup.sh" "$dest"
""",
    """    ITS_BACKUP_OBJECTS="$OBJECTS" ITS_BACKUP_OBJECTS_DIR="$BACKUP_ROOT/objects" \\
        ITS_BACKUP_OLD_IDENTITY_DB=0 "$DEPLOY_DIR/scripts/backup.sh" "$dest"
""",
)

# --- restore.sh -------------------------------------------------------------- #
restore = deploy / "scripts/restore.sh"
swap(
    restore,
    """# BOTH DATABASES GO THROUGH THIS, and they are checked BEFORE either is
# restored. Guarding only `itsuite` would abort on `keycloak` AFTER `itsuite`
# had already been replaced -- the half-done state this guard exists to
# prevent. And note a real rollback ALWAYS has a populated target, so the
# rollback runbook uses ITS_RESTORE_FORCE=1 by design; the default refusal is
# for the mistyped restore, not for that.""",
    """# EVERY DATABASE IN THE BACKUP GOES THROUGH THIS, and they are checked BEFORE
# any is restored. A backup taken before own authentication phase 4, or the
# one deploy.sh takes before an upgrade on a host that still keeps the old
# identity provider's database, also carries `keycloak.dump`; guarding only
# `itsuite` would abort on `keycloak` AFTER `itsuite` had already been
# replaced -- the half-done state this guard exists to prevent. And note a
# real rollback ALWAYS has a populated target, so the rollback runbook uses
# ITS_RESTORE_FORCE=1 by design; the default refusal is for the mistyped
# restore, not for that.""",
)
swap(
    restore,
    """if [ -s "$BACKUP_DIR/keycloak.dump" ]; then
    run pg_restore -U "$ITS_PG_ADMIN_USER" -d keycloak < "$BACKUP_DIR/keycloak.dump"
fi
""",
    """if [ -s "$BACKUP_DIR/keycloak.dump" ]; then
    run pg_restore -U "$ITS_PG_ADMIN_USER" -d keycloak < "$BACKUP_DIR/keycloak.dump"
    echo "    keycloak.dump restored: the old identity provider's database. Releases"
    echo "    from own authentication phase 4 on do not use it; the one before does --"
    echo "    keep it if you are rolling back. It goes in deploy/README.md"
    echo "    \\"Retiring the old identity provider's data\\", once no way back needs it."
fi
""",
)

# --- restore-drill.sh ------------------------------------------------------- #
drill = deploy / "scripts/restore-drill.sh"
swap(
    drill,
    "#   ./scripts/restore-drill.sh --skip-keycloak # databases only (less memory)\n",
    "#   ./scripts/restore-drill.sh --skip-keycloak # accepted, and does nothing since\n"
    "#                                              # own authentication phase 4\n",
)
swap(
    drill,
    """# WHAT IT PROVES: both dumps restore; restore.sh's own end-state checks pass
# (RLS policy count equals the backup's, app_user narrowed); the schema is at
# the backup's alembic revision; app_user cannot bypass RLS; the audit chain's
# linkage is intact (docs/07 §4 pre-flight); Keycloak boots on the restored
# database and serves the realm. WHAT IT DOES NOT: key material (OpenBao, the
# CA, the audit HMAC key) and the API are not in this backup and not started.""",
    """# WHAT IT PROVES: the dumps restore; restore.sh's own end-state checks pass
# (RLS policy count equals the backup's, app_user narrowed); the schema is at
# the backup's alembic revision; app_user cannot bypass RLS; the audit chain's
# linkage is intact (docs/07 §4 pre-flight); and a `keycloak.dump`, when the
# backup has one (taken before own authentication phase 4, or by deploy.sh
# before an upgrade), restores into a database that holds tables. WHAT IT
# DOES NOT: key material (OpenBao, the CA, the audit HMAC key) and the API
# are not in this backup and not started. Nor does it prove the way back to
# a release from before own authentication phase 4: nothing here starts the
# old identity provider (this release has no image of it), and the nightly
# backups it drills hold no `keycloak.dump` since then. That way back is
# only the backup taken before that upgrade, whose schema is below 0248
# (deploy/README.md "Rolling back a deployment").""",
)
swap(drill, "        -h|--help) sed -n '2,20p' \"$0\"; exit 0 ;;\n",
     "        -h|--help) sed -n '2,28p' \"$0\"; exit 0 ;;\n")
swap(drill, 'KC_WAIT_S="${ITS_DRILL_KEYCLOAK_WAIT_S:-240}"\nKEEP=0; SKIP_KC=0; BACKUP_DIR=""\n',
     'KEEP=0; BACKUP_DIR=""\n')
swap(
    drill,
    "        --skip-keycloak) SKIP_KC=1 ;;\n",
    "        # Kept so a timer or a cron line that passes it does not fail; there\n"
    "        # is nothing left to skip (own authentication phase 4).\n"
    '        --skip-keycloak) echo "--skip-keycloak: nothing to skip since own authentication phase 4" ;;\n',
)
swap(
    drill,
    "for f in itsuite.dump keycloak.dump globals.sql alembic_version policy_count; do\n",
    "for f in itsuite.dump globals.sql alembic_version policy_count; do\n",
)
cut(
    drill,
    'if [ "$SKIP_KC" = 0 ]; then\n    say "Starting Keycloak on the restored database"\n',
    'objects="$(dirname "$BACKUP_DIR")/../objects"\n',
    """if [ -s "$BACKUP_DIR/keycloak.dump" ]; then
    kc_tables="$(psql_drill -d keycloak -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" 2>/dev/null | tr -d '[:space:]' || true)"
    case "$kc_tables" in
        ''|0) check "the restored keycloak database holds tables" "${kc_tables:-query failed}" ;;
        *) check "the restored keycloak database holds tables" ok ;;
    esac
fi

""",
)
dc = deploy / "drill/docker-compose.drill.yml"
cut(dc, "  keycloak:\n    image: quay.io/keycloak/keycloak:26.2\n", "volumes:\n")

# --- deploy.sh --------------------------------------------------------------- #
ds = deploy / "scripts/deploy.sh"
swap(
    ds,
    """# A WARNING, NOT A STOP: a blank console address breaks email links and
# Microsoft sign-in, not the deploy -- and CI's throwaway .env leaves it blank.
if ! grep -qE '^ITS_PUBLIC_CONSOLE_URL=https?://[^[:space:]]+' .env; then
    warn "ITS_PUBLIC_CONSOLE_URL is not set in .env. Invitation and password-reset"
    warn "emails will link to the internal address http://keycloak:8080, which"
    warn "nobody can open. Set it to the address people open the console at."
fi
""",
    """# A WARNING, NOT A STOP: a blank console address breaks sign-in, email links
# and the OAuth server for AI apps, not the deploy -- and CI's throwaway .env
# starts blank.
if ! grep -qE '^ITS_PUBLIC_CONSOLE_URL=https?://[^[:space:]]+' .env; then
    warn "ITS_PUBLIC_CONSOLE_URL is not set in .env. Nobody can sign in to the console,"
    warn "invitations and password resets carry no link, and AI apps cannot connect."
    warn "Set it to the address people open the console at."
fi

# KEYS THIS RELEASE NO LONGER READS (own authentication phase 4 removed the old
# identity provider). No compose service of this release interpolates them,
# and its containers ignore them: pydantic-settings reads an environment
# variable only for a field it declares. BUT THE RELEASE BEFORE NEEDS FOUR:
# its compose file does not parse without KEYCLOAK_ADMIN_USER and
# KEYCLOAK_ADMIN_PASSWORD, and its api, gateway, worker and migrate do not
# start without ITS_OIDC_ISSUER and ITS_OIDC_JWKS_URL. So they stay until the
# operator closes the way back, in deploy/README.md "Retiring the old
# identity provider's data" (operator decision K1, 2026-11-15). Named here,
# and never removed here: this script never edits .env beyond IT_SUITE_VERSION.
OBSOLETE_ENV_KEYS=(
    KEYCLOAK_ADMIN_USER KEYCLOAK_ADMIN_PASSWORD
    ITS_OIDC_ISSUER ITS_OIDC_JWKS_URL ITS_OIDC_AUDIENCE ITS_OIDC_ORG_CLAIM
    ITS_OIDC_CONSOLE_CLIENT_ID ITS_MULTI_TENANT
    ENTRA_TENANT_ID ENTRA_CLIENT_ID ENTRA_CLIENT_SECRET
    ITS_KEYCLOAK_ADMIN_URL ITS_KEYCLOAK_REALM
    ITS_KEYCLOAK_EVENTS_CLIENT_ID ITS_KEYCLOAK_EVENTS_SECRET_PATH
    ITS_KEYCLOAK_ADMIN_CLIENT_ID ITS_KEYCLOAK_ADMIN_SECRET_PATH
)
obsolete_env_keys() {
    local key found=()
    for key in "${OBSOLETE_ENV_KEYS[@]}"; do
        if grep -qE "^${key}=" .env; then found+=("$key"); fi
    done
    if [ ${#found[@]} -gt 0 ]; then
        note ".env holds ${#found[@]} key(s) this release no longer reads:"
        note "  ${found[*]}"
        note "Do NOT remove them yet: a rollback past own authentication phase 4 needs four of them."
        note "deploy/README.md \\"Retiring the old identity provider's data\\" removes them, when you choose."
    fi
    return 0
}
obsolete_env_keys

# THE OLD IDENTITY PROVIDER'S CONTAINERS (own authentication phase 4; the
# coordinator's decision, 2026-11-15). Compose leaves a container whose
# service left the file RUNNING, with only a warning, and Traefik keeps
# routing to it by the labels on the container itself: Keycloak would go on
# answering /realms, its anonymous client registration included, on :443.
# These two, BY NAME, and nothing else -- not `up --remove-orphans`, which
# would also remove any container the operator added to this project.
# Neither holds data: Keycloak's is in Postgres, and its image declares no
# volume. The listing is this project's own (`compose ps -a` includes a
# container whose service the file no longer defines).
old_identity_containers() {
    "${COMPOSE[@]}" ps -a --format '{{.Name}} {{.Service}}' 2>/dev/null </dev/null \\
        | awk '$2 == "keycloak" || $2 == "keycloak-init" { print $1 }' || true
}
remove_old_identity_containers() {
    local name found
    found="$(old_identity_containers)"
    if [ -z "$found" ]; then
        note "none left"
        return 0
    fi
    while read -r name; do
        [ -n "$name" ] || continue
        docker stop "$name" >/dev/null
        docker rm "$name" >/dev/null
        note "removed $name"
    done <<<"$found"
}
""",
)
swap(
    ds,
    """if [ -n "$BASELINE" ] && [ "$(git -C "$REPO_ROOT" rev-parse "$BASELINE")" = "$AFTER" ]; then
    say "Already deployed"
    note "the running image was built from HEAD ($SHORT_AFTER) and nothing was pulled."
    note "Nothing to do. Use --full to redeploy anyway."
    exit 0
fi
""",
    """# NOT "Already deployed" WHILE THE OLD IDENTITY PROVIDER'S CONTAINERS ARE HERE.
# The deploy.sh of a release before own authentication phase 4, run as usual,
# pulls this release, records its tag in .env and migrates -- and then stops
# at `restart keycloak`, a service this compose file no longer has, with the
# app tier still on the old images. Re-run, this script would read the new
# tag as running and stop here with exit 0. Those two containers are the mark
# of that half-done upgrade, so it finishes it as a full deploy instead.
#
# AND THE WAY BACK IS NOT THIS RUN'S BACKUP. This run backs up a database the
# stopped run already migrated past 0248, which no release before phase 4 can
# read. The way back is the backup the stopped run took first: the newest
# `pre-upgrade-*` whose schema is below 0248 (its `alembic_version`), with the
# images its `image_version` names. That backup has no `keycloak.dump` (the
# new backup.sh took it without the flag), and needs none: the old identity
# provider's database is still on the cluster, and restore.sh touches only the
# databases a backup holds. rollback_help prints that backup and that tag.
ROLLBACK_BACKUP_DIR=""; ROLLBACK_BASE=""
way_back_backup() {
    local dir rev found=""
    for dir in "$HOME"/backups/pre-upgrade-*/; do
        dir="${dir%/}"
        [ -r "$dir/alembic_version" ] || continue
        rev="$(tr -d '[:space:]' < "$dir/alembic_version")"
        case "$rev" in [0-9][0-9][0-9][0-9]) ;; *) continue ;; esac
        [ "$((10#$rev))" -lt 248 ] && found="$dir"
    done
    printf '%s' "$found"
}
stop_if_already_deployed() {
    local old_tag
    [ -n "$BASELINE" ] || return 0
    [ "$(git -C "$REPO_ROOT" rev-parse "$BASELINE")" = "$AFTER" ] || return 0
    if [ -n "$(old_identity_containers)" ]; then
        warn "the old identity provider's containers are still here: an upgrade past own"
        warn "authentication phase 4 stopped part-way. Finishing it as a full deploy."
        BASELINE=""
        ROLLBACK_BACKUP_DIR="$(way_back_backup)"
        if [ -z "$ROLLBACK_BACKUP_DIR" ]; then
            warn "No backup under $HOME/backups/pre-upgrade-* is from before migration 0248, so"
            warn "this run cannot name the way back past phase 4. This run's own backup is not it."
            return 0
        fi
        old_tag="$(sed -n 's/^IT_SUITE_VERSION=//p' "$ROLLBACK_BACKUP_DIR/image_version" 2>/dev/null | tail -1 || true)"
        case "$old_tag" in *[!A-Za-z0-9._-]*) old_tag="" ;; esac
        warn "The way back is the backup the stopped run took first, not the one this run takes:"
        warn "  $ROLLBACK_BACKUP_DIR (schema $(tr -d '[:space:]' < "$ROLLBACK_BACKUP_DIR/alembic_version"), images ${old_tag:-unrecorded})"
        if [ -n "$old_tag" ]; then
            RUNNING_TAG="$old_tag"
            if git -C "$REPO_ROOT" rev-parse --verify --quiet "${old_tag}^{commit}" >/dev/null 2>&1; then
                ROLLBACK_BASE="$old_tag"
            fi
        fi
        return 0
    fi
    say "Already deployed"
    note "the running image was built from HEAD ($SHORT_AFTER) and nothing was pulled."
    note "Nothing to do. Use --full to redeploy anyway."
    exit 0
}
stop_if_already_deployed
""",
)
swap(
    ds,
    "NEED_MIGRATE=0; NEED_GRANTS=0; NEED_KEYCLOAK=0; NEED_OPENBAO=0; NEED_COMPOSE=0\n",
    "NEED_MIGRATE=0; NEED_GRANTS=0; NEED_OPENBAO=0; NEED_COMPOSE=0\n",
)
swap(ds, "touched '^deploy/(keycloak|realm)'         && NEED_KEYCLOAK=1\n", "")
cut(
    ds,
    "# A compose change that moves KEYCLOAK'S IMAGE is not undone by checking the old\n",
    "# A NEW migration file is the signal",
)
swap(ds, "    NEED_KEYCLOAK=1; NEED_OPENBAO=1\n", "    NEED_OPENBAO=1\n")
swap(
    ds,
    """    local prev="$RUNNING_TAG" base="${BASELINE:-}" n=1
    say "If you need to go back"
    note "deploy/README.md \\"Rolling back a deployment\\" -- here with this run's values."
""",
    """    local prev="$RUNNING_TAG" base="${ROLLBACK_BASE:-${BASELINE:-}}" n=1
    # Past an upgrade that stopped part-way, the backup taken before it
    # (stop_if_already_deployed), not this run's, which is past 0248.
    local way_back="${ROLLBACK_BACKUP_DIR:-$BACKUP_DIR}"
    say "If you need to go back"
    note "deploy/README.md \\"Rolling back a deployment\\" -- here with this run's values."
    if [ -n "${ROLLBACK_BACKUP_DIR:-}" ]; then
        note "An upgrade stopped part-way before this run: the way back is the backup it took first."
        note "This run's own backup ($BACKUP_DIR) is past migration 0248 and cannot go back past it."
    fi
""",
)
swap(
    ds,
    """        note "There is no image to swap back to. The backup this run took is at:"
        note "  $BACKUP_DIR"
""",
    """        note "There is no image to swap back to. The backup to restore is at:"
        note "  $way_back"
""",
)
swap(
    ds,
    """[ "$NEED_KEYCLOAK" = 1 ] && note "RUN    restart keycloak — realm or theme changed" || note "skip   keycloak restart — no realm or theme change"
note "RUN    keycloak-init — converges the realm allowlist (every deploy)"
""",
    """note "RUN    remove the old identity provider's containers (keycloak, keycloak-init), if any are left"
""",
)
swap(
    ds,
    """    if [ "$NEED_MIGRATE" = 1 ] || [ "$KC_IMAGE_CHANGED" = 1 ]; then
        warn "This deploy moved a database schema (a migration, or Keycloak's image), so the old images cannot run against it."
        note "  $n. ITS_RESTORE_FORCE=1 ./scripts/restore.sh $BACKUP_DIR"; n=$((n + 1))
        warn "     That DROPS BOTH DATABASES and rewinds the audit log to the backup: every write and every"
        warn "     audited action since then is gone. Export anything you need first (see the runbook)."
    fi
    if [ "$NEED_COMPOSE" = 1 ] || [ "$NEED_KEYCLOAK" = 1 ]; then
        local paths=""
        [ "$NEED_COMPOSE" = 1 ] && paths="deploy/docker-compose.yml"
        [ "$NEED_KEYCLOAK" = 1 ] && paths="${paths:+$paths }deploy/keycloak"
        if [ -n "$base" ]; then
            note "  $n. git -C $REPO_ROOT checkout ${base:0:12} -- $paths"; n=$((n + 1))
            note "     (this leaves the checkout modified, so the next deploy.sh will refuse until you reset it -- deliberately)"
        else
            warn "This deploy changed $paths, and this run could not tell which commit was running before."
            warn "Check those paths out at the previous release by hand; the images alone do not carry them."
        fi
    fi
    note "  $n. grep -q '^IT_SUITE_VERSION=' .env && sed -i 's/^IT_SUITE_VERSION=.*/IT_SUITE_VERSION=$prev/' .env || echo 'IT_SUITE_VERSION=$prev' >> .env"; n=$((n + 1))
    note "  $n. docker compose up -d"; n=$((n + 1))
    [ "$NEED_KEYCLOAK" = 1 ] && note "  $n. docker compose restart keycloak    # it caches the theme and realm in memory"
    return 0""",
    """    if [ "$NEED_MIGRATE" = 1 ]; then
        warn "This deploy moved the database schema (a migration), so the old images cannot run against it."
        note "  $n. ITS_RESTORE_FORCE=1 ./scripts/restore.sh $way_back"; n=$((n + 1))
        warn "     That DROPS THE DATABASES IN THE BACKUP and rewinds the audit log to it: every write and"
        warn "     every audited action since then is gone. Export anything you need first (see the runbook)."
    fi
    # THE WHOLE deploy/ DIRECTORY, not the compose file alone: an earlier
    # release's compose file bind-mounts files of that release (the old
    # identity provider's realm and theme, until own authentication phase 4),
    # which a newer checkout may no longer have.
    if [ "$NEED_COMPOSE" = 1 ]; then
        if [ -n "$base" ]; then
            note "  $n. git -C $REPO_ROOT checkout ${base:0:12} -- deploy"; n=$((n + 1))
            note "     (this leaves the checkout modified, so the next deploy.sh will refuse until you reset it -- deliberately)"
        else
            warn "This deploy changed deploy/docker-compose.yml, and this run could not tell which commit was running before."
            warn "Check deploy/ out at the previous release by hand; the images alone do not carry it."
        fi
    fi
    # A RELEASE FROM BEFORE OWN AUTHENTICATION PHASE 4 needs four .env keys this
    # one does not read (OBSOLETE_ENV_KEYS above). Say so if .env lost any.
    local old_compose key missing=()
    if [ -n "$base" ]; then
        old_compose="$(git -C "$REPO_ROOT" show "$base:deploy/docker-compose.yml" 2>/dev/null || true)"
        if grep -q 'KEYCLOAK_ADMIN_USER' <<<"$old_compose"; then
            for key in KEYCLOAK_ADMIN_USER KEYCLOAK_ADMIN_PASSWORD ITS_OIDC_ISSUER ITS_OIDC_JWKS_URL; do
                grep -qE "^${key}=." .env 2>/dev/null || missing+=("$key")
            done
        fi
    fi
    if [ ${#missing[@]} -gt 0 ]; then
        warn "The release you would go back to predates own authentication phase 4. Its compose file"
        warn "or its api stops without these .env keys, which .env no longer has: ${missing[*]}"
        warn "Put them back, with the values they had, before the next step."
    fi
    note "  $n. grep -q '^IT_SUITE_VERSION=' .env && sed -i 's/^IT_SUITE_VERSION=.*/IT_SUITE_VERSION=$prev/' .env || echo 'IT_SUITE_VERSION=$prev' >> .env"; n=$((n + 1))
    note "  $n. docker compose up -d"; n=$((n + 1))
    return 0""",
)
cut(
    ds,
    'if [ "$NEED_KEYCLOAK" = 1 ]; then\n    # RESTART IT, AND RESTART IT FIRST.',
    "# ------------------------------------------------------------------- restart\n",
)
swap(
    ds,
    """BACKUP_DIR="$HOME/backups/pre-upgrade-$(date -u +%FT%H%MZ)"
./scripts/backup.sh "$BACKUP_DIR"
""",
    """BACKUP_DIR="$HOME/backups/pre-upgrade-$(date -u +%FT%H%MZ)"
# With the old identity provider's database while one exists: the backup taken
# before the phase-4 upgrade is the way back past it, and the nightly ones
# leave that database out (backup.sh; operator decision K1, 2026-11-15).
ITS_BACKUP_OLD_IDENTITY_DB=1 ./scripts/backup.sh "$BACKUP_DIR"
""",
)
swap(
    ds,
    """if [ ${#RECREATE[@]} -gt 0 ]; then
    "${COMPOSE[@]}" up -d --force-recreate "${RECREATE[@]}"
fi
"${COMPOSE[@]}" up -d
""",
    """if [ ${#RECREATE[@]} -gt 0 ]; then
    "${COMPOSE[@]}" up -d --force-recreate "${RECREATE[@]}"
fi
"${COMPOSE[@]}" up -d
# After the new app tier is up, not before: until then a deploy that stops
# leaves the release before it able to run.
say "Removing the old identity provider's containers"
remove_old_identity_containers
""",
)
swap(
    ds,
    """say "Verifying"
FAIL=0
""",
    """say "Verifying"
FAIL=0

# THE OLD IDENTITY PROVIDER'S CONTAINERS ARE GONE, checked by the labels on the
# containers themselves (the project and the service), so this does not rest
# on `compose ps` listing a container whose service left the file. One left is
# still routed by Traefik, so it fails the deploy and says how to remove it.
old_identity_containers_left() {
    local cfg project service
    cfg="$("${COMPOSE[@]}" config 2>/dev/null </dev/null || true)"
    project="$(awk '/^name: / { print $2; exit }' <<<"$cfg")"
    for service in keycloak keycloak-init; do
        docker ps -aq --filter "label=com.docker.compose.project=${project:-it-suite}" \\
            --filter "label=com.docker.compose.service=$service" 2>/dev/null </dev/null || true
    done
}
verify_old_identity_gone() {
    local left
    left="$(old_identity_containers_left | tr '\\n' ' ')"
    if [ -z "${left// /}" ]; then
        note "no container of the old identity provider is left"
        return 0
    fi
    warn "a container of the old identity provider is still here, and Traefik routes to it:"
    warn "  docker rm -f ${left% }"
    return 1
}
verify_old_identity_gone || FAIL=1
""",
)

swap(
    ds,
    """# Usage:
#   ./deploy.sh            # pull, show the plan, ask, then run it
""",
    """# THE FIRST UPGRADE PAST OWN AUTHENTICATION PHASE 4 starts with the pull:
#   git -C .. pull --ff-only && ./scripts/deploy.sh
# The release before runs its own deploy.sh, which pulls part-way through --
# and bash goes on running the text it opened, which restarts a Keycloak this
# compose file no longer has. If that already happened, run ./scripts/deploy.sh
# again: it finishes the upgrade (stop_if_already_deployed).
#
# Usage:
#   ./deploy.sh            # pull, show the plan, ask, then run it
""",
)
swap(ds, "        -h|--help) sed -n '2,30p' \"$0\"; exit 0 ;;\n",
     "        -h|--help) sed -n '2,47p' \"$0\"; exit 0 ;;\n")
swap(
    deploy / "scripts/preflight.sh",
    'echo "work through GO-LIVE.md §2 (Keycloak), §4 (OpenBao seal), §5 (gateway cert)"',
    'echo "work through GO-LIVE.md §2 (the first administrator), §4 (OpenBao seal), §5 (gateway cert)"',
)
swap(
    deploy / "scripts/preflight.sh",
    """  host="$(grep -E '^ITS_OIDC_ISSUER=' .env \\
          | sed -E 's|^[^=]+=https?://([^/:]+).*|\\1|' || true)"
  if [ -z "$host" ] || [ "$host" = "your-host" ]; then
    refuse "ITS_OIDC_ISSUER does not name a real host, so the certificate cannot be checked against it and no OIDC login will work. Set it to your operator hostname (README step 1)."
""",
    """  #    The console's own address since own authentication phase 4 (until
  #    then the old identity provider's issuer, on the same host). `sed -n
  #    ...p` so a blank value yields nothing rather than the whole line.
  host="$(grep -E '^ITS_PUBLIC_CONSOLE_URL=' .env | tail -1 \\
          | sed -nE 's|^[^=]+=https?://([^/:]+).*|\\1|p' || true)"
  if [ -z "$host" ] || [ "$host" = "your-host" ]; then
    refuse "ITS_PUBLIC_CONSOLE_URL does not name a real host, so the certificate cannot be checked against it and nobody can sign in. Set it to the address people open the console at (README step 1)."
""",
)
swap(
    deploy / "scripts/preflight.sh",
    "(the host in ITS_OIDC_ISSUER)",
    "(the host in ITS_PUBLIC_CONSOLE_URL)",
)
swap(
    deploy / "scripts/host-check.sh",
    "# ITMS brings up postgres, redis, minio, keycloak, openbao, traefik, the\n",
    "# ITMS brings up postgres, redis, minio, openbao, traefik, the console, the\n",
)
swap(
    deploy / "scripts/host-check.sh",
    """# The agent gateway is mTLS and the console is OIDC. Both fail in confusing
# ways on a host whose clock is wrong -- certificate validity and token
# expiry are absolute times.
""",
    """# The agent gateway is mTLS, and the console's one-time codes and the tokens
# it issues AI apps are time-based. Both fail in confusing ways on a host whose
# clock is wrong -- certificate validity and token expiry are absolute times.
""",
)
swap(
    deploy / "host-network.md",
    "one your `:443` certificate covers. `ITS_OIDC_ISSUER`, `ITS_GATEWAY_URL`,\n",
    "one your `:443` certificate covers. `ITS_PUBLIC_CONSOLE_URL`, `ITS_GATEWAY_URL`,\n",
)
swap(
    deploy / "host-network.md",
    "boot, not after. Certificate validity windows and OIDC token expiry are\n"
    "absolute times:",
    "boot, not after. Certificate validity windows, one-time sign-in codes and\n"
    "token expiry are absolute times:",
)
print("ok")
```

```python
"""Own authentication phase 4, Task 6: `deploy/.env.example` without the old
identity provider's settings. Run from the worktree root."""

import pathlib

env = pathlib.Path("deploy/.env.example")


def swap(old: str, new: str) -> None:
    text = env.read_text()
    assert text.count(old) == 1, (old[:80], text.count(old))
    env.write_text(text.replace(old, new))


swap(
    """# "production" fails closed on missing key material (signing key, CA root,
# audit HMAC key) instead of silently generating it, and (with multi-tenant)
# requires the org claim on every token. Use "dev" only for local development.
ITS_ENVIRONMENT=production
# Reject operator tokens that lack the org claim instead of using the default
# org. Leave false for single-org on-prem; set true for multi-tenant.
ITS_MULTI_TENANT=false
""",
    """# "production" fails closed on missing key material (signing key, CA root,
# audit HMAC key, the sign-in keys) instead of silently generating it. Use
# "dev" only for local development.
ITS_ENVIRONMENT=production
""",
)
text = env.read_text()
start = text.index("# --- Keycloak / OIDC ---\n")
end = text.index("# The console email-domain allowlist (deploy/README.md")
env.write_text(
    text[:start]
    + """# --- The organization this deployment signs people in to ---
# The console's sign-in, the OAuth server AI apps connect to, the setup claim
# and the recovery command all name this one organization by its slug: one
# organization per deployment. Own authentication phase 4 removed the old
# identity provider and every setting that sat here for it. An existing .env
# that still holds them starts this release exactly as before: nothing in it
# reads them. KEEP THEM until you retire the old identity provider's data
# (deploy/README.md): the release before needs four of them to start, so a
# rollback does. `scripts/deploy.sh` names them on every run.
ITS_DEFAULT_ORG_SLUG=toeshee

"""
    + text[end:]
)
swap(
    "# WITH THE BUNDLED MINIO this is your own :443 hostname -- the same one in\n"
    "# ITS_OIDC_ISSUER -- because docker-compose.yml routes `/<bucket>/` on :443\n",
    "# WITH THE BUNDLED MINIO this is your own :443 hostname -- the same one in\n"
    "# ITS_PUBLIC_CONSOLE_URL -- because docker-compose.yml routes `/<bucket>/` on :443\n",
)
swap(
    """#   * Keycloak, as KC_HOSTNAME: the address of its own pages, which no
#     sign-in uses since own authentication phase 3 (Keycloak goes in phase
#     4). It no longer emails anyone: since own authentication phase 1 the
#     platform sends invitations and password resets itself, through
#     Settings -> Email.
""",
    "",
)
swap(
    """# reads it when it starts, and it needs ITS_PUBLIC_CONSOLE_URL too. Until
# phase 4 removes Keycloak, keycloak-init also reads it and opens Keycloak's
# own registration, whose tokens nothing accepts any more.
""",
    """# reads it when it starts, and it needs ITS_PUBLIC_CONSOLE_URL too.
""",
)
left = [n for n, line in enumerate(env.read_text().splitlines(), 1) if "keycloak" in line.lower()]
print("lines still naming keycloak:", left)
print("ok")
```

Expected: `ok`, y del segundo `lines still naming keycloak: []` y `ok`. El primero deja `deploy/keycloak/` con un solo archivo, `ci_mcp_flow.py`, que borra la Task 7.

Lo que hacen, por archivo:
- **compose:** fuera los dos servicios y la dependencia de `api`; el comentario del router `api` dice por qué `/.well-known/openid-configuration` sigue en él (P4-19) y que nada nombra ya `/realms`, `/resources`, `/js` ni `/admin` (P4-20).
- **`init-app-user.sh`:** deja de crear la base `keycloak`. Solo actúa en un directorio de datos nuevo (es un guion de `docker-entrypoint-initdb.d`).
- **`backup.sh`:** `ITS_BACKUP_OLD_IDENTITY_DB` (`0` por omisión; `0` o `1`, comprobado antes de volcar nada); mira si la base `keycloak` existe (un escalar `-tAc`, no `psql | grep -q`, por lo de `pipefail` que `restore.sh` ya explica); con `1` y la base, la vuelca y exige su volcado no vacío; sin `1` y con la base, una nota que la recuerda y nombra el paso del runbook (K1, P4-12). `scheduled-backup.sh` pasa `ITS_BACKUP_OLD_IDENTITY_DB=0`.
- **`restore.sh`:** sigue restaurando `keycloak.dump` cuando la copia lo trae, y lo dice: que las versiones desde la fase 4 no lo usan, que la anterior sí y se conserve si se está volviendo atrás, y el nombre de la sección del runbook que lo retira (revisión 3, M2). El comentario de `prepare_db` se reescribe entero, hasta «… not for that.» (revisión 3, M1).
- **`restore-drill.sh` y su compose:** no arranca Keycloak; si la copia trae `keycloak.dump`, comprueba que la base restaurada tiene tablas; `--skip-keycloak` se acepta y avisa de que no hace nada (P4-23); su cabecera dice que no prueba la vuelta atrás a una versión de antes de la fase 4, que es solo con la copia previa de `deploy.sh` (C7), y su `--help` imprime la cabecera entera (`2,28p`).
- **`deploy.sh`:** la cabecera dice que la primera actualización pasada la fase 4 empieza con el `git pull` y que, si el guion viejo ya se paró, basta volver a correr este (C2; su `--help` imprime ahora la cabecera hasta «Usage», `2,47p`); el aviso de `ITS_PUBLIC_CONSOLE_URL` sin `http://keycloak:8080` (P4-31); `OBSOLETE_ENV_KEYS` y `obsolete_env_keys()`, que nombran las 17 claves sin tocar `.env` y dicen que no se borren todavía (K1); `old_identity_containers()` y `remove_old_identity_containers()`, que quitan por su nombre `keycloak` y `keycloak-init` después del último `up -d` y nada más (C1); `stop_if_already_deployed()`, que no responde «Already deployed» mientras quede alguno de los dos (C2) y entonces busca la copia de la vuelta atrás por su esquema (`way_back_backup()`: la `pre-upgrade-*` más nueva por debajo de 0248), lee su etiqueta vieja de `image_version` y se las da a `rollback_help` (`ROLLBACK_BACKUP_DIR`, `ROLLBACK_BASE`), que las imprime en vez de la copia de esta ejecución, ya a 0248 (re-revisión, N2); `verify_old_identity_gone()`, al verificar, que falla el despliegue si queda un contenedor de los dos, buscado por sus etiquetas de compose y no por `compose ps` (re-revisión, m11); la copia previa con `ITS_BACKUP_OLD_IDENTITY_DB=1`; fuera `NEED_KEYCLOAK`, `KC_IMAGE_CHANGED`, el reinicio de `keycloak` y `keycloak-init`; la ayuda de vuelta atrás hace `checkout <base> -- deploy` entero, dice «DROPS THE DATABASES IN THE BACKUP» y, si la versión de destino es de antes de la fase 4 (su compose nombra `KEYCLOAK_ADMIN_USER`), avisa de cuáles de las cuatro claves faltan en `.env` (P4-13).
- **`preflight.sh`:** el host del certificado sale de `ITS_PUBLIC_CONSOLE_URL`, con `sed -n …p` para que un valor vacío dé un host vacío (P4-38), y su frase de GO-LIVE §2. **`host-check.sh`:** la lista de servicios y la nota del reloj. **`host-network.md`:** `ITS_PUBLIC_CONSOLE_URL` en la lista de claves que llevan el nombre del host, y la frase del reloj.
- **`.env.example`:** fuera `ITS_MULTI_TENANT`, el bloque «Keycloak / OIDC», `ENTRA_*`, `ITS_KEYCLOAK_*`, la viñeta de `KC_HOSTNAME` y la frase de `keycloak-init` sobre `ITS_MCP_REGISTRATION`; `ITS_DEFAULT_ORG_SLUG=toeshee` se queda en un bloque propio (`test_setup_api.py::test_the_shipped_defaults_name_the_organization_the_console_signs_in_to` lo exige), con un comentario que dice que las claves viejas de un `.env` existente se conservan hasta el paso del runbook; y `:350` nombra `ITS_PUBLIC_CONSOLE_URL`.

Comprobación sin demonio (compose lee `deploy/.env` para `env_file:`; se crea y se borra):

```bash
test ! -e deploy/.env && sed -E "s/^([A-Z_]+)=change-me$/\1=ci-x/" deploy/.env.example > deploy/.env
(cd deploy && docker compose config --services | sort | tr '\n' ' '; echo; docker compose config --images | grep -ci keycloak)
rm deploy/.env
for f in deploy/scripts/*.sh deploy/postgres/init-app-user.sh; do bash -n "$f" || echo "SYNTAX $f"; done
```
Expected: `api backup-replicator gateway migrate minio openbao openbao-init postgres postgres-grants redis traefik web worker`, `0`, y ninguna línea `SYNTAX`. Si `shellcheck` está a mano, `shellcheck -x` de `backup.sh`, `restore.sh`, `restore-drill.sh`, `deploy.sh`, `preflight.sh`, `host-check.sh` y `scheduled-backup.sh` no da ningún aviso que no diera antes de la tarea (en el ensayo de la ronda de correcciones, con `shellcheck` 0.10.0: ninguno [S1]).

- [ ] **Step 4: Las pruebas pasan**

Run: `.venv/bin/pytest -q tests/test_email_domain_runbook.py tests/test_public_surface.py tests/test_deploy_mcp_route.py tests/test_auth_jwks.py tests/test_deploy_script_steps.py tests/test_platform_backup_scripts.py tests/test_deploy_env_documentation.py tests/test_deploy_scripts.py tests/test_mcp_discovery_fallbacks.py tests/test_deploy_install_link.py tests/test_deploy_object_storage_route.py tests/test_openbao_seal_script.py tests/test_deploy_runbook_secrets.py tests/test_setup_api.py tests/test_ci_mcp_flow.py`
Expected: PASS (en el ensayo de la segunda ronda de correcciones, sobre las oleadas 1 y 2 de los guiones corregidos: 197 en esta rama sola, con el `tests/test_ci_mcp_flow.py` viejo, que lee `deploy/keycloak/ci_mcp_flow.py`, que aquí sigue; 203 con la Task 7). Y `.venv/bin/ruff check tests`: limpio.

- [ ] **Step 5: Commit**

```bash
git add -A deploy backend/tests
git commit -m "chore(deploy): Keycloak leaves the stack, the scripts and .env.example (phase 4)"
```

---

### Task 7: La prueba de despliegue de la CI contra el servidor propio

**Files:**
- Create: `deploy/ci/mcp_flow.py`
- Delete: `deploy/keycloak/ci_mcp_flow.py`
- Rewrite: `backend/tests/test_ci_mcp_flow.py`
- Modify: `.github/workflows/ci.yml` (el nombre del job `deploy`, `:367`; sus pasos «keycloak 26.2 — …» y «MCP — live registration, PKCE and audience, through Traefik», `:568-738`, se sustituyen por uno; y un paso nuevo, el de K1, detrás de «backup.sh -> restore.sh round trip», `:449-522`)

**Interfaces:**
- Depende de: la Task 6 en el mismo merge (el compose sin `keycloak`; sin ella, `docker compose config --services` nombra `keycloak` y el paso nuevo falla a propósito).
- Produce: `deploy/ci/mcp_flow.py` (Apéndice A).

**Decisiones de la tarea:** P4-17 (OpenBao real con una sola parte; DCR en vez de CIMD), P4-18 (biblioteca estándar; token ajeno HS256), K1 (el paso de la copia vieja). **Lo que el paso hace, en orden:** (1) comprueba que compose no define nada de Keycloak; (2) inicializa OpenBao con una parte, lo abre, carga la política `its-app` y crea su token (README 3.1-3.4), y corre `openbao-init` (3.6) — la primera vez que ese camino se ejecuta y no solo se lee (`deploy/README.md:533-538`); la clave de apertura y el token raíz llegan a `bao` por la entrada estándar, nunca en un argv que un listado de procesos de un runner compartido vería, y una `trap` borra los dos archivos que tuvieron un secreto acabe como acabe el paso (revisión 3, M6); (3) la clave HMAC de auditoría (paso 5) y un certificado para el :443 (paso 7); (4) `up -d --build api web traefik` y espera, a través de Traefik y con `--noproxy '*'`, al documento RFC 8414 **y** a la página de la consola en `/` (Traefik solo enruta a un contenedor sano [D3]; revisión 3, M7); (5) `--phase closed`; (6) `ITS_MCP_REGISTRATION=open` y recrea `api`; (7) pide `setup/status` (que escribe el código de instalación), lo lee del contenedor y corre `--phase open`, que además de `/mcp` manda el token con la forma del de Keycloak a la puerta de la consola (`GET /api/v1/me`: 401; revisión 3, M8); (8) comprueba que ningún contenedor de **este proyecto** era de Keycloak (`docker compose ps -a`, no `docker ps -a`, que vería los de otros trabajos de un runner compartido; revisión 3, M5).

**El paso de K1** (revisión 3, I3; decisión del operador K1), detrás de la ida y vuelta de `backup.sh` → `restore.sh`: crea una base `keycloak` con una tabla, comprueba que una copia nocturna no la lleva y que una con `ITS_BACKUP_OLD_IDENTITY_DB=1` sí, la borra, restaura forzada esa copia, comprueba que `restore.sh` lo dice y que la tabla volvió, y la borra otra vez. Es la copia de antes de la fase 4 sobre un host de después, en vivo.

**Por qué esto basta para el MCP:** lo que la fase 3 dejó probado solo en el backend (RFC 9728, RFC 8414, el 401 con `resource_metadata`, PKCE S256, el token ES256 `at+jwt` con la audiencia exacta, la rotación, la reutilización, RFC 7009, `/mcp`) pasa ahora por el Traefik y el nginx reales, con las claves en OpenBao. La especificación MCP lo pide así: el servidor «MUST implement OAuth 2.0 Protected Resource Metadata», «MUST validate that access tokens were issued specifically for them as the intended audience», y los clientes «MUST support both discovery mechanisms» [M1].

- [ ] **Step 1: Preparar la worktree** (como la Task 6, Step 1).

- [ ] **Step 2: Las pruebas que fallan**

Reescribir `backend/tests/test_ci_mcp_flow.py` entero:

```python
"""The pure parts of `deploy/ci/mcp_flow.py`, the live CI check of the
platform's own OAuth server and its MCP door (own authentication phase 4,
plan 2026-11-15, Task 7). The live run is CI's (`deploy` job); these hold the
helpers that decide what it asserts and what it is allowed to print.

Until phase 4 this file tested `deploy/keycloak/ci_mcp_flow.py`, which drove
Keycloak's registration and sign-in; that script went with Keycloak.
"""

import ast
import base64
import hashlib
import hmac
import importlib.util
import json
import re
import sys
from email.message import Message
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy/ci/mcp_flow.py"
CI = REPO / ".github/workflows/ci.yml"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_ci_mcp_flow", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


flow = _load()


def _part(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _jwt(claims: dict, header: dict | None = None) -> str:
    return f"{_part(header or {'alg': 'ES256', 'typ': 'at+jwt'})}.{_part(claims)}.c2ln"


def test_it_imports_nothing_outside_the_standard_library() -> None:
    """CI runs it with the runner's bare `python3`: no venv, no pip."""
    tree = ast.parse(SCRIPT.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    assert names <= set(sys.stdlib_module_names) | {"__future__"}, names


def test_the_pkce_pair_is_s256_of_a_valid_verifier() -> None:
    verifier, challenge = flow.pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert set(verifier) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    )
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert challenge == expected.rstrip(b"=").decode()
    assert "=" not in challenge
    assert flow.pkce_pair()[0] != verifier


def test_a_token_is_read_whatever_the_padding_and_refused_when_it_is_not_a_jwt() -> None:
    for org in ("a", "ab", "abc"):  # three payload lengths mod 4
        header, claims = flow.jwt_parts(_jwt({"org": org}))
        assert claims["org"] == org and header["alg"] == "ES256"
    for bad in ("not-a-jwt", "a.b", "e30.bm90LWpzb24.c2ln"):
        with pytest.raises(flow.Failed):
            flow.jwt_parts(bad)


def test_the_audience_must_be_exactly_the_resource_as_a_string() -> None:
    """Design §5.3 and the api's verifier: a string, never a list holding it."""
    resource = "https://localhost/mcp"
    assert flow.exact_audience({"aud": resource}, resource)
    for wrong in ([resource], [resource, "it-suite"], "it-suite", None, "https://other/mcp"):
        assert not flow.exact_audience({"aud": wrong}, resource), wrong
    assert flow.audiences({"aud": ["x", "y"]}) == ["x", "y"]
    assert flow.audiences({}) == []


def test_totp_matches_rfc_6238s_own_vectors() -> None:
    """RFC 6238 Appendix B, SHA-1, eight digits: the ASCII secret
    "12345678901234567890", base32-encoded as an otpauth URI carries it."""
    secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    assert flow.totp(secret, 59, digits=8) == "94287082"
    assert flow.totp(secret, 1111111109, digits=8) == "07081804"
    assert flow.totp(secret, 1234567890, digits=8) == "89005924"
    # RFC 4226 Appendix D: the same key at counter 1, six digits.
    assert flow.totp(secret, 30) == "287082"


def test_the_totp_secret_comes_out_of_the_otpauth_uri() -> None:
    uri = "otpauth://totp/ITMS:owner%40ci.invalid?secret=JBSWY3DPEHPK3PXP&issuer=ITMS"
    assert flow.totp_secret(uri) == "JBSWY3DPEHPK3PXP"
    for bad in ("https://totp/x?secret=A", "otpauth://hotp/x?secret=A", "otpauth://totp/x"):
        with pytest.raises(flow.Failed):
            flow.totp_secret(bad)


def test_the_challenge_is_read_into_its_parameters() -> None:
    header = 'Bearer resource_metadata="https://h/.well-known/x", error="invalid_token"'
    assert flow.challenge_params(header) == {
        "resource_metadata": "https://h/.well-known/x",
        "error": "invalid_token",
    }
    with pytest.raises(flow.Failed):
        flow.challenge_params('Basic realm="x"')


def test_the_authorization_request_is_an_mcp_clients() -> None:
    query = dict(
        pair.split("=", 1)
        for pair in flow.authorize_query("itms_x", "c" * 43, "st", "https://h/mcp").split("&")
    )
    assert query["response_type"] == "code"
    assert query["code_challenge_method"] == "S256"
    assert query["resource"] == "https%3A%2F%2Fh%2Fmcp"
    assert query["redirect_uri"] == "http%3A%2F%2F127.0.0.1%3A33418%2Fcallback"


def test_a_callback_is_matched_exactly_not_by_prefix() -> None:
    good = flow.REDIRECT_URI + "?code=c&state=s&iss=https%3A%2F%2Fh"
    assert flow.callback_params(good) == {"code": "c", "state": "s", "iss": "https://h"}
    for bad in (
        "http://127.0.0.1:33418/callback.evil?code=c",
        "http://127.0.0.1:33419/callback?code=c",
        "https://127.0.0.1:33418/callback?code=c",
        "http://localhost:33418/callback?code=c",
    ):
        with pytest.raises(flow.Failed):
            flow.callback_params(bad)


def test_a_tampered_token_changes_only_its_signature() -> None:
    token = _jwt({"sub": "x"})
    changed = flow.tampered(token)
    assert changed != token
    assert changed.split(".")[:2] == token.split(".")[:2]


def test_the_foreign_token_is_shaped_like_keycloaks_and_signed_by_nobody_known() -> None:
    token = flow.foreign_token("https://localhost", "someone")
    header, claims = flow.jwt_parts(token)
    assert header["alg"] == "HS256"
    assert claims["iss"] == "https://localhost/realms/it-suite"
    assert (claims["aud"], claims["azp"], claims["typ"]) == ("it-suite", "it-suite-web", "Bearer")
    assert not flow.exact_audience(claims, "https://localhost/mcp")
    # A fresh random key each time: two tokens never share a signature.
    assert token.split(".")[2] != flow.foreign_token("https://localhost", "someone").split(".")[2]
    signing_input = ".".join(token.split(".")[:2]).encode()
    guessed = hmac.new(b"", signing_input, hashlib.sha256).digest()
    assert flow._b64url(guessed) != token.split(".")[2]


def test_only_non_secret_claim_values_are_printed() -> None:
    claims = {"aud": "https://h/mcp", "sub": "u-1", "jti": "j", "fam": "f", "scope": "mcp"}
    shown = flow.describe_claims(claims)
    assert 'aud="https://h/mcp"' in shown and 'scope="mcp"' in shown
    for secret_value in ("u-1", '"j"', '"f"'):
        assert secret_value not in shown


def test_no_secret_reaches_a_printed_line() -> None:
    """Nothing the script prints -- `print`, `ok`, or a `check` message that
    becomes the FAIL line -- names a token, a code, a password or a secret."""
    secret_names = {
        "password", "secret", "setup_code", "code", "verifier", "access", "refresh", "token",
    }
    tree = ast.parse(SCRIPT.read_text())
    leaks = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in {"print", "ok", "check"}:
            continue
        printed = node.args[1:] if node.func.id == "check" else node.args
        for arg in printed:
            for inner in ast.walk(arg):
                name = (
                    inner.id
                    if isinstance(inner, ast.Name)
                    else inner.attr
                    if isinstance(inner, ast.Attribute)
                    else None
                )
                if name in secret_names:
                    leaks.append(f"line {node.lineno}: {name}")
    assert leaks == [], leaks


def test_the_cookie_store_keeps_the_session_and_forgets_it_when_cleared() -> None:
    set_it = Message()
    set_it["Set-Cookie"] = "__Host-itms_session=abc; Path=/; Secure; HttpOnly; SameSite=Strict"
    clear = Message()
    clear["Set-Cookie"] = '__Host-itms_session=""; Max-Age=0; Path=/; Secure; HttpOnly'
    jar = flow.Cookies()
    jar.take(set_it)
    assert jar.header() == "__Host-itms_session=abc"
    jar.take(clear)
    assert jar.header() is None


def test_it_speaks_the_mcp_revision_the_backend_tests_speak() -> None:
    from mcp.types import LATEST_PROTOCOL_VERSION

    assert flow.PROTOCOL_VERSION == LATEST_PROTOCOL_VERSION


def test_the_former_keycloak_paths_are_all_checked() -> None:
    """The closed phase checks each path Keycloak's router answered; an
    emptied list would make that check pass without checking anything."""
    paths = flow.FORMER_KEYCLOAK_PATHS
    assert len(paths) >= 6 and len(set(paths)) == len(paths), paths
    for prefix in ("/realms/", "/resources/", "/js/", "/admin/"):
        assert any(path.startswith(prefix) for path in paths), prefix


def _deploy_step(name: str) -> str:
    """The `run` of the deploy job's step whose name starts with NAME, read
    as YAML and without its comment lines: a commented-out command is not one
    the job runs."""
    steps = yaml.safe_load(CI.read_text())["jobs"]["deploy"]["steps"]
    for step in steps:
        if str(step.get("name", "")).startswith(name):
            lines = step["run"].splitlines()
            return "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    raise AssertionError(f"the deploy job has no step named {name!r}...")


OAUTH_STEP = "the platform's own OAuth server and MCP door"


def test_ci_runs_both_phases_through_traefik_on_a_real_openbao() -> None:
    """The deploy job runs the closed phase before the claim and the open
    phase after the api restarted with the opt-in, against the stack's own
    OpenBao; and it names Keycloak only to assert that nothing of it is
    defined or ran (design §10, falsifier 11)."""
    run = _deploy_step(OAUTH_STEP)
    assert "python3 ci/mcp_flow.py" in run
    closed = run.index("flow --phase closed")
    opened = run.index("flow --phase open")
    restart = run.index('echo "ITS_MCP_REGISTRATION=open" >> .env')
    assert closed < restart < opened, "closed, then the opt-in and a restart, then open"
    assert "bao operator init -key-shares=1 -key-threshold=1 -format=json" in run
    assert "docker compose run --rm openbao-init" in run
    named = [line.strip() for line in run.splitlines() if re.search("keycloak", line, re.I)]
    assert len(named) == 2, named
    assert all(line.startswith("if grep -qi keycloak <<<") for line in named), named


#: Lines that hold a vault secret without an argv: an assignment, and bash's
#: own printf and echo, which run in the shell and start no process.
SHELL_BUILTINS = ("unseal=", "root=", "printf '%s' ", 'echo "OPENBAO_TOKEN=')


def test_no_secret_of_the_vault_travels_in_an_argv() -> None:
    """The unseal key and the root token reach `bao` on stdin, never as an
    argument a process listing on a shared runner would show; the files that
    held them go however the step ends."""
    run = _deploy_step(OAUTH_STEP)
    for secret in ('"$unseal"', '"$root"', "$root"):
        argv = [
            line.strip()
            for line in run.splitlines()
            if secret in line
            and "<<<" not in line
            and "::add-mask::" not in line
            and not line.strip().startswith(SHELL_BUILTINS)
        ]
        assert argv == [], argv
    assert """trap 'rm -f "$init_json" "$RUNNER_TEMP/setup-code"' EXIT""" in run


def test_ci_restores_the_old_identity_providers_database_from_a_pre_upgrade_backup() -> None:
    """Operator decision K1 (2026-11-15), live: a nightly backup leaves the
    `keycloak` database out, the pre-upgrade one keeps it, and a forced
    restore of that one brings it back."""
    run = _deploy_step("the old identity provider's database")
    nightly = run.index("./scripts/backup.sh /tmp/ci-nightly")
    assert "test ! -e /tmp/ci-nightly/keycloak.dump" in run
    pre = run.index("ITS_BACKUP_OLD_IDENTITY_DB=1 ./scripts/backup.sh /tmp/ci-pre-upgrade")
    restore = run.index("ITS_RESTORE_FORCE=1 ./scripts/restore.sh /tmp/ci-pre-upgrade")
    assert nightly < pre < restore
    assert "test -s /tmp/ci-pre-upgrade/keycloak.dump" in run
    assert run.index("DROP DATABASE keycloak WITH (FORCE)") < restore
    assert "realm_probe" in run[restore:]
```

Run: `.venv/bin/pytest -q tests/test_ci_mcp_flow.py`
Expected: FAIL al importar — `deploy/ci/mcp_flow.py` no existe.

- [ ] **Step 3: El guion**

`git rm deploy/keycloak/ci_mcp_flow.py`, y crear `deploy/ci/mcp_flow.py`:

```python
#!/usr/bin/env python3
"""Live proof of the platform's own OAuth server and its MCP door, through
Traefik, as an AI app sees them (own authentication phase 4, plan 2026-11-15,
Task 7; design §5, §10.6).

Until phase 4 this was `deploy/keycloak/ci_mcp_flow.py`, which proved what
Keycloak 26.2 and Traefik 3.6 did with the realm's registration and audience
rules. Keycloak is gone; the authorization server is the platform's own
(phase 3), and the backend suite proves what it DECIDES. This script is the
half no unit test reaches: the real compose `traefik`, `web` and `api`
services, the api on a real OpenBao (its ES256 key never leaves it), reached
from outside on :443 with a certificate the script verifies.

TWO PHASES, because the registration switch is read when the api starts.
  --phase closed  before anyone has claimed the installation, with
                  ITS_MCP_REGISTRATION unset: the discovery documents, the
                  401 challenge, the 404 where an OpenID document would be,
                  the signing keys, registration refused, and the paths
                  Keycloak used to answer now answered by the console.
  --phase open    after the api restarted with ITS_MCP_REGISTRATION=open:
                  claim the installation with the install code, sign in,
                  enrol a TOTP, turn MCP on, then as an AI app register,
                  be approved, exchange the code with PKCE S256, call
                  `/mcp`, refresh, and see a reused refresh token, a reused
                  code, an RFC 7009 revocation and "sign out everywhere" each
                  end a grant at the next call. A token signed by anybody
                  else -- a tampered one, and one shaped like Keycloak's --
                  is refused at `/mcp`.

WHAT IT NEVER PRINTS. Tokens, codes, passwords, the install code, the TOTP
secret and recovery codes. Claims are printed by NAME, with values only for
the non-secret claims in `SHOWN_CLAIMS`.

Standard library only (the runner's `python3`, no venv). Exits 0 when every
assertion holds, 1 with one `FAIL` line at the first that does not (each
step needs the ones before it), 2 when the base cannot be reached at all.

    python3 ci/mcp_flow.py --base https://localhost --cafile certs/fullchain.pem \\
        --phase closed
    python3 ci/mcp_flow.py --base https://localhost --cafile certs/fullchain.pem \\
        --phase open --setup-code-file "$RUNNER_TEMP/setup-code"
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import Message
from http.cookies import SimpleCookie
from typing import Any

#: A loopback callback, the form desktop MCP clients register; PKCE binds
#: the code to the verifier, so whatever listens there cannot redeem it.
REDIRECT_URI = "http://127.0.0.1:33418/callback"
#: The MCP revision the backend's own tests speak (`mcp.types.
#: LATEST_PROTOCOL_VERSION`; `backend/tests/test_ci_mcp_flow.py` holds the two
#: equal).
PROTOCOL_VERSION = "2026-07-28"
SESSION_COOKIE = "__Host-itms_session"
#: Claims whose VALUES are safe to print. Everything else is printed by name.
SHOWN_CLAIMS = frozenset({"aud", "iss", "scope", "client_id", "typ"})
#: Paths Keycloak's router (`kc`) answered until phase 4. Nothing routes them
#: to an identity provider any more: they fall to the console, whose nginx
#: answers its own page.
FORMER_KEYCLOAK_PATHS = (
    "/realms/it-suite/.well-known/openid-configuration",
    "/realms/it-suite/protocol/openid-connect/auth",
    "/realms/it-suite/clients-registrations/openid-connect",
    "/resources/x/login/itms/css/itms.css",
    "/js/keycloak.js",
    "/admin/master/console/",
)
#: The access token's lifetime (design §5.3: ten minutes).
ACCESS_TOKEN_S = 600
TIMEOUT_S = 30


class Failed(Exception):
    """One assertion that did not hold. The message says what was expected."""


class Unreachable(Exception):
    """The base did not answer at all."""


def check(condition: object, message: str) -> None:
    if not condition:
        raise Failed(message)


# --------------------------------------------------------------------------- #
# Pure helpers (backend/tests/test_ci_mcp_flow.py)
# --------------------------------------------------------------------------- #


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def s256(verifier: str) -> str:
    """RFC 7636 §4.2: BASE64URL(SHA256(ASCII(code_verifier))), unpadded."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def pkce_pair() -> tuple[str, str]:
    """`(verifier, S256 challenge)`. 48 random bytes make a 64-character
    verifier, inside RFC 7636 §4.1's 43-128."""
    verifier = _b64url(secrets.token_bytes(48))
    return verifier, s256(verifier)


def jwt_parts(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """`(header, claims)` of a JWT, NOT verified: CI asserts on what the
    server put in it; verifying is the api's, and the backend suite's."""
    parts = token.split(".")
    if len(parts) != 3:
        raise Failed("the token is not a three-part JWT")
    try:
        header, claims = json.loads(_unb64url(parts[0])), json.loads(_unb64url(parts[1]))
    except ValueError as exc:
        raise Failed("the token's header or payload is not base64url JSON") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise Failed("the token's header or payload is not a JSON object")
    return header, claims


def audiences(claims: Mapping[str, Any]) -> list[str]:
    aud = claims.get("aud")
    if aud is None:
        return []
    return [aud] if isinstance(aud, str) else [str(a) for a in aud]


def exact_audience(claims: Mapping[str, Any], resource: str) -> bool:
    """The audience is the MCP resource, as a string, and nothing else
    (design §5.3; 1c(a): "carrying the MCP audience and no other platform
    audience"). A list holding it is refused by the api too."""
    return claims.get("aud") == resource


def totp(secret: str, at: float | None = None, *, step: int = 30, digits: int = 6) -> str:
    """RFC 6238 over RFC 4226, HMAC-SHA1: what an authenticator app shows."""
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    counter = int((time.time() if at is None else at) // step)
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{value % 10**digits:0{digits}d}"


def totp_secret(otpauth_uri: str) -> str:
    """The `secret` of an `otpauth://totp/...` URI."""
    parts = urllib.parse.urlsplit(otpauth_uri)
    check(parts.scheme == "otpauth" and parts.netloc == "totp", "not an otpauth://totp URI")
    secret = urllib.parse.parse_qs(parts.query).get("secret", [""])[0]
    check(secret, "the otpauth URI carries no secret")
    return secret


def challenge_params(header: str) -> dict[str, str]:
    """The parameters of a `WWW-Authenticate: Bearer k="v", ...` challenge."""
    scheme, _, rest = header.partition(" ")
    check(scheme == "Bearer", f"the challenge's scheme is {scheme!r}, not Bearer")
    params: dict[str, str] = {}
    for item in rest.split(","):
        key, sep, value = item.strip().partition("=")
        if sep:
            params[key.strip()] = value.strip().strip('"')
    return params


def authorize_query(client_id: str, challenge: str, state: str, resource: str) -> str:
    """The query of `/oauth/authorize` as an MCP client sends it."""
    return urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "resource": resource,
            "scope": "mcp",
        }
    )


def callback_params(location: str, redirect_uri: str = REDIRECT_URI) -> dict[str, str]:
    """The query of a redirect to `redirect_uri` -- same scheme, host, port
    and path, not a string prefix, which `.../callback.evil` would pass."""
    got, want = urllib.parse.urlsplit(location), urllib.parse.urlsplit(redirect_uri)
    if (got.scheme, got.netloc, got.path) != (want.scheme, want.netloc, want.path):
        raise Failed(f"redirected to {location.split('?')[0]!r}, not the registered callback")
    return {k: v[0] for k, v in urllib.parse.parse_qs(got.query).items()}


def tampered(token: str) -> str:
    """The same token with one byte of its signature changed."""
    head, payload, signature = token.split(".")
    raw = bytearray(_unb64url(signature))
    raw[0] ^= 0x01
    return f"{head}.{payload}.{_b64url(bytes(raw))}"


def foreign_token(base: str, sub: str) -> str:
    """An access token shaped exactly like Keycloak's for the console --
    `iss` the realm that used to live at `base`, `aud` `it-suite`, `azp` the
    console's SPA, `typ` Bearer, `org` -- signed with a key nobody has. The
    platform trusts no key but its own, so `/mcp` refuses it."""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT", "kid": "keycloak-ci"}
    claims = {
        "iss": base.rstrip("/") + "/realms/it-suite",
        "aud": "it-suite",
        "azp": "it-suite-web",
        "typ": "Bearer",
        "sub": sub,
        "org": "toeshee",
        "iat": now,
        "exp": now + 300,
    }
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}"
    signature = hmac.new(secrets.token_bytes(32), signing_input.encode(), hashlib.sha256)
    return f"{signing_input}.{_b64url(signature.digest())}"


def describe_claims(claims: Mapping[str, Any]) -> str:
    """Claim names, with values only for `SHOWN_CLAIMS`. Safe to print."""
    shown = []
    for name in sorted(claims):
        if name in SHOWN_CLAIMS:
            shown.append(f"{name}={json.dumps(claims[name])}")
        else:
            shown.append(name)
    return " ".join(shown)


class Cookies:
    """The cookies one host set, by name. Kept by hand rather than by
    `http.cookiejar`, whose policy for a dotless host (`localhost`) and for
    `__Host-` names is not this script's to second-guess: the api sets one
    host's cookies and this sends them back to that host."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def take(self, headers: Message) -> None:
        """Every `Set-Cookie` of a response; `Max-Age=0` or an empty value
        clears the name."""
        for line in headers.get_all("Set-Cookie") or []:
            jar: SimpleCookie = SimpleCookie()
            jar.load(line)
            for name, morsel in jar.items():
                if morsel.value == "" or morsel["max-age"] == "0":
                    self.values.pop(name, None)
                else:
                    self.values[name] = morsel.value

    def header(self) -> str | None:
        if not self.values:
            return None
        return "; ".join(f"{name}={value}" for name, value in sorted(self.values.items()))


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


@dataclass
class Response:
    path: str
    status: int
    headers: Message
    body: bytes

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except ValueError as exc:
            raise Failed(
                f"{self.path} answered {self.status} with a body that is not JSON"
            ) from exc

    def media(self) -> str:
        return (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is a result here, not something to follow."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class Client:
    """One host, its cookies, no redirects followed, no proxy, TLS verified."""

    def __init__(self, base: str, cafile: str | None) -> None:
        self.base = base.rstrip("/")
        self.cookies = Cookies()
        context = ssl.create_default_context(cafile=cafile)
        self._opener = urllib.request.build_opener(
            # Everything this talks to is the runner itself; an inherited
            # HTTPS_PROXY would send the requests somewhere else entirely.
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
            _NoRedirect(),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        form: Mapping[str, str] | None = None,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        console: bool = False,
        bearer: str | None = None,
        cookies: bool = True,
    ) -> Response:
        """`console`: the two headers the console's own client adds to every
        write (`X-ITMS-Request: 1`, and its `Origin`), without which the
        api's CSRF rule refuses it. `cookies`: send this host's cookies."""
        data = None
        sent = {"Accept": "application/json"}
        sent.update(headers or {})
        if console:
            sent.setdefault("X-ITMS-Request", "1")
            sent.setdefault("Origin", self.base)
        if bearer is not None:
            sent["Authorization"] = f"Bearer {bearer}"
        jar = self.cookies.header() if cookies else None
        if jar:
            sent["Cookie"] = jar
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            sent["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            sent["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, headers=sent, method=method)
        try:
            with self._opener.open(request, timeout=TIMEOUT_S) as reply:
                answer = Response(path.split("?")[0], reply.status, reply.headers, reply.read())
        except urllib.error.HTTPError as exc:
            answer = Response(path.split("?")[0], exc.code, exc.headers, exc.read())
        except (urllib.error.URLError, OSError) as exc:
            raise Unreachable(f"{self.base}{path.split('?')[0]}: {exc}") from exc
        if cookies:
            self.cookies.take(answer.headers)
        return answer


def ok(message: str) -> None:
    print(f"ok    {message}")


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #


@dataclass
class Grant:
    client_id: str
    access: str
    refresh: str


class Flow:
    def __init__(self, client: Client) -> None:
        self.http = client
        self.base = client.base
        self.resource = self.base + "/mcp"
        self.kids: set[str] = set()

    # -- the parts both phases share ----------------------------------------- #

    def mcp(self, token: str | None) -> Response:
        """`initialize`, the first call any MCP client makes."""
        return self.http.request(
            "POST",
            "/mcp",
            body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "itms-ci", "version": "1"},
                },
            },
            headers={"Accept": "application/json, text/event-stream"},
            bearer=token,
            cookies=False,
        )

    def refused_at_mcp(self, token: str, what: str) -> None:
        answer = self.mcp(token)
        check(answer.status == 401, f"/mcp answered {answer.status} to {what}, not 401")
        params = challenge_params(answer.headers.get("WWW-Authenticate", ""))
        check(params.get("error") == "invalid_token", f"{what}: the 401 does not say invalid_token")
        ok(f"/mcp refuses {what}: 401 invalid_token")

    def refused_at_the_console(self, token: str, what: str) -> None:
        """The console's API door, with no cookie: it reads a session cookie
        only, and since own authentication phase 4 nothing on the platform can
        validate a Keycloak token at all."""
        answer = self.http.request("GET", "/api/v1/me", bearer=token, cookies=False)
        check(answer.status == 401, f"/api/v1/me answered {answer.status} to {what}, not 401")
        ok(f"/api/v1/me refuses {what}: 401")

    def discovery(self, *, registration: bool) -> dict[str, Any]:
        answer = self.mcp(None)
        check(answer.status == 401, f"/mcp without a token answered {answer.status}, not 401")
        metadata_url = self.base + "/.well-known/oauth-protected-resource/mcp"
        challenge = answer.headers.get("WWW-Authenticate", "")
        check(
            challenge == f'Bearer resource_metadata="{metadata_url}"',
            "the 401's challenge is not exactly Bearer resource_metadata=<the PRM URL>",
        )
        ok("/mcp without a token: 401 naming the protected-resource metadata")

        prm = self.http.request("GET", "/.well-known/oauth-protected-resource/mcp", cookies=False)
        check(prm.status == 200, f"the protected-resource metadata answered {prm.status}")
        document = prm.json()
        check(document.get("resource") == self.resource, "PRM: resource is not <base>/mcp")
        check(document.get("authorization_servers") == [self.base], "PRM: the server is not <base>")
        ok("RFC 9728 metadata: this resource, this authorization server")

        meta = self.http.request("GET", "/.well-known/oauth-authorization-server", cookies=False)
        check(meta.status == 200, f"the RFC 8414 document answered {meta.status}")
        server = meta.json()
        expected = {
            "issuer": self.base,
            "authorization_endpoint": self.base + "/oauth/authorize",
            "token_endpoint": self.base + "/oauth/token",
            "revocation_endpoint": self.base + "/oauth/revoke",
            "jwks_uri": self.base + "/.well-known/jwks.json",
        }
        for key, value in expected.items():
            check(server.get(key) == value, f"RFC 8414: {key} is {server.get(key)!r}")
        check(server.get("code_challenge_methods_supported") == ["S256"], "RFC 8414: not S256 only")
        check(
            server.get("token_endpoint_auth_methods_supported") == ["none"],
            "RFC 8414: token endpoint auth is not `none` only",
        )
        check(server.get("client_id_metadata_document_supported") is True, "RFC 8414: no CIMD")
        check(
            server.get("authorization_response_iss_parameter_supported") is True,
            "RFC 8414: no RFC 9207 iss",
        )
        if not registration:
            check("registration_endpoint" not in server, "registration is offered while closed")
        ok("RFC 8414 document: issuer, endpoints, S256, public clients, CIMD, iss")

        oidc = self.http.request("GET", "/.well-known/openid-configuration", cookies=False)
        check(oidc.status == 404, f"/.well-known/openid-configuration answered {oidc.status}")
        check(oidc.media() == "application/json", "the OpenID path is not the api's JSON 404")
        ok("no OpenID document: the api's JSON 404, not the console's page")

        keys = self.http.request("GET", "/.well-known/jwks.json", cookies=False)
        check(keys.status == 200, f"the JWKS answered {keys.status}")
        listed = keys.json().get("keys", [])
        check(listed, "the JWKS lists no key")
        for key in listed:
            check(key.get("kty") == "EC" and key.get("crv") == "P-256", "a JWKS key is not P-256")
            check("d" not in key, "the JWKS carries a private key")
            self.kids.add(str(key.get("kid")))
        ok(f"JWKS: {len(listed)} public P-256 key(s)")
        return server

    # -- phase closed ---------------------------------------------------------- #

    def closed(self) -> None:
        self.discovery(registration=False)
        refused = self.http.request(
            "POST", "/oauth/register", body={"redirect_uris": [REDIRECT_URI]}, cookies=False
        )
        check(refused.status == 403, f"registration answered {refused.status} while closed")
        check(
            refused.json().get("error") == "access_denied", "registration closed: not access_denied"
        )
        ok("registration closed: 403 access_denied")
        for path in FORMER_KEYCLOAK_PATHS:
            page = self.http.request("GET", path, headers={"Accept": "text/html"}, cookies=False)
            check(page.status == 200, f"{path} answered {page.status}, not the console's 200")
            check(page.media() == "text/html", f"{path} is not the console's page")
            check("keycloak" not in page.text().lower(), f"{path}'s page names the old provider")
        ok(f"the {len(FORMER_KEYCLOAK_PATHS)} paths Keycloak answered: the console's page")

    # -- phase open ------------------------------------------------------------ #

    def sign_in(self, setup_code: str) -> None:
        status = self.http.request("GET", "/api/v1/setup/status")
        check(status.status == 200 and status.json() == {"needed": True}, "setup is not needed")
        email = "owner@ci.invalid"
        password = "ci-" + secrets.token_urlsafe(24)
        claim = self.http.request(
            "POST",
            "/api/v1/setup/claim",
            body={
                "code": setup_code,
                "email": email,
                "display_name": "CI Owner",
                "organization_name": "CI",
                "password": password,
            },
            console=True,
        )
        check(claim.status == 201, f"the setup claim answered {claim.status}")
        ok("the installation is claimed with the install code")

        forged = self.http.request(
            "POST",
            "/api/v1/auth/login",
            body={"email": email, "password": password},
            headers={"Origin": self.base},
        )
        check(forged.status == 403, f"a sign-in without X-ITMS-Request answered {forged.status}")
        check(forged.json().get("code") == "csrf", "the refusal is not the CSRF rule's")
        check(SESSION_COOKIE not in self.http.cookies.values, "a refused sign-in set a session")
        ok("a sign-in without the console's header: 403 csrf, no session")

        login = self.http.request(
            "POST", "/api/v1/auth/login", body={"email": email, "password": password}, console=True
        )
        check(login.status == 200, f"the sign-in answered {login.status}")
        check(
            login.json() == {"state": "enroll_required"}, "the first sign-in is not enroll_required"
        )
        check(SESSION_COOKIE in self.http.cookies.values, "the sign-in set no session cookie")
        start = self.http.request("POST", "/api/v1/auth/enroll/totp/start", console=True)
        check(start.status == 200, f"TOTP start answered {start.status}")
        secret = totp_secret(start.json()["otpauth_uri"])
        confirm = self.http.request(
            "POST", "/api/v1/auth/enroll/totp/confirm", body={"code": totp(secret)}, console=True
        )
        check(confirm.status == 204, f"TOTP confirm answered {confirm.status}")
        finish = self.http.request("POST", "/api/v1/auth/enroll/finish", console=True)
        check(finish.status == 200, f"enrolment finish answered {finish.status}")
        check(finish.json().get("recovery_codes"), "enrolment returned no recovery codes")
        ok("signed in: password, a TOTP enrolled, recovery codes issued")

        switch = self.http.request(
            "PATCH", "/api/v1/orgs/current", body={"settings": {"mcp_enabled": True}}, console=True
        )
        check(switch.status == 200, f"turning MCP on answered {switch.status}")
        ok("MCP is on for the organization")

    def register(self, wait_s: float) -> str:
        """The metadata offers registration once the switch is read; the api
        keeps that answer for up to 30 s (`clients.OFFERED_FOR_S`)."""
        self.discovery(registration=True)
        deadline = time.monotonic() + wait_s
        while True:
            meta = self.http.request(
                "GET", "/.well-known/oauth-authorization-server", cookies=False
            )
            check(meta.status == 200, f"the RFC 8414 document answered {meta.status}")
            if meta.json().get("registration_endpoint") == self.base + "/oauth/register":
                break
            check(time.monotonic() < deadline, "RFC 8414 never offered registration")
            time.sleep(5)
        ok("RFC 8414 offers registration: opted in, and MCP is on")
        answer = self.http.request(
            "POST",
            "/oauth/register",
            body={
                "client_name": "ITMS CI",
                "redirect_uris": [REDIRECT_URI],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
            cookies=False,
        )
        check(answer.status == 201, f"registration answered {answer.status}")
        registered = answer.json()
        check(registered.get("token_endpoint_auth_method") == "none", "not a public client")
        check("client_secret" not in registered, "a public client was given a secret")
        ok("registered by RFC 7591, as a public client")
        return str(registered["client_id"])

    def approve(self, client_id: str) -> tuple[str, str]:
        """The person approves: `(code, verifier)`. Approving needs a recent
        second factor (`require_step_up`, ten minutes: `STEP_UP_WINDOW` in
        `app/core/auth/sessions.py`); the enrolment at sign-in gave it, and
        the four approvals of `open` all fall inside that window."""
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(12)
        query = authorize_query(client_id, challenge, state, self.resource)
        page = self.http.request(
            "GET", "/oauth/authorize?" + query, headers={"Accept": "text/html"}
        )
        check(page.status == 200 and page.media() == "text/html", "the consent page is not served")
        details = self.http.request(
            "POST", "/api/v1/oauth/authorize/details", body={"query": query}, console=True
        )
        check(details.status == 200, f"the consent details answered {details.status}")
        asked = details.json()
        check(asked.get("outcome") == "consent", f"the consent outcome is {asked.get('outcome')!r}")
        check(asked.get("scope") == "mcp", "the consent does not ask for `mcp`")
        app = asked.get("client") or {}
        check(app.get("loopback_only") is True, "the app is not shown as loopback-only")
        check(
            app.get("redirect_host") == "127.0.0.1", "the consent does not name the redirect host"
        )
        decision = self.http.request(
            "POST",
            "/api/v1/oauth/authorize/decision",
            body={"query": query, "approve": True},
            console=True,
        )
        check(decision.status == 200, f"the approval answered {decision.status}")
        answered = decision.json()
        check(
            answered.get("outcome") == "redirect",
            f"the approval outcome is {answered.get('outcome')!r}",
        )
        params = callback_params(str(answered.get("redirect_to")))
        check(params.get("state") == state, "the callback's state is not the request's")
        check(params.get("iss") == self.base, "the callback carries no RFC 9207 iss")
        check(params.get("code"), "the callback carries no code")
        return params["code"], verifier

    def token(self, form: Mapping[str, str]) -> Response:
        return self.http.request(
            "POST", "/oauth/token", form={**form, "resource": self.resource}, cookies=False
        )

    def exchange(self, client_id: str, code: str, verifier: str) -> Grant:
        answer = self.token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
            }
        )
        return self.issued(answer, client_id, "the code exchange")

    def issued(self, answer: Response, client_id: str, what: str) -> Grant:
        check(answer.status == 200, f"{what} answered {answer.status}")
        check("no-store" in (answer.headers.get("Cache-Control") or ""), f"{what}: not no-store")
        body = answer.json()
        check(body.get("token_type") == "Bearer", f"{what}: token_type is not Bearer")
        check(
            body.get("expires_in") == ACCESS_TOKEN_S, f"{what}: expires_in is not {ACCESS_TOKEN_S}"
        )
        check(body.get("scope") == "mcp", f"{what}: scope is not mcp")
        header, claims = jwt_parts(str(body.get("access_token")))
        check(header.get("alg") == "ES256", f"{what}: the access token is not ES256")
        check(header.get("typ") == "at+jwt", f"{what}: the access token's typ is not at+jwt")
        check(str(header.get("kid")) in self.kids, f"{what}: the token's kid is not in the JWKS")
        check(exact_audience(claims, self.resource), f"{what}: the audience is not exactly /mcp")
        check(claims.get("iss") == self.base, f"{what}: the issuer is not <base>")
        ok(f"{what}: ES256 at+jwt from the JWKS, {describe_claims(claims)}")
        return Grant(client_id, str(body["access_token"]), str(body["refresh_token"]))

    def reaches_mcp(self, grant: Grant, what: str) -> None:
        answer = self.mcp(grant.access)
        check(answer.status == 200, f"/mcp answered {answer.status} to {what}")
        check(b"protocolVersion" in answer.body, f"/mcp's answer to {what} is not initialize's")
        ok(f"/mcp answers {what}")

    def open(self, setup_code: str, wait_s: float) -> None:
        self.sign_in(setup_code)
        client_id = self.register(wait_s)

        # One grant through every turn of its life.
        code, verifier = self.approve(client_id)
        first = self.exchange(client_id, code, verifier)
        self.reaches_mcp(first, "the access token")
        self.refused_at_mcp(tampered(first.access), "a token with a changed signature")
        keycloak_shaped = foreign_token(self.base, "ci-owner")
        self.refused_at_mcp(keycloak_shaped, "a token shaped like Keycloak's")
        self.refused_at_the_console(keycloak_shaped, "a token shaped like Keycloak's")
        rotated = self.issued(
            self.token(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": first.refresh,
                    "client_id": client_id,
                }
            ),
            client_id,
            "the refresh",
        )
        check(rotated.refresh != first.refresh, "the refresh token did not rotate")
        self.reaches_mcp(rotated, "the refreshed access token")
        reused = self.token(
            {"grant_type": "refresh_token", "refresh_token": first.refresh, "client_id": client_id}
        )
        check(reused.status == 400, f"a reused refresh token answered {reused.status}")
        check(
            reused.json()
            == {"error": "invalid_grant", "error_description": "the refresh token is not valid"},
            "a reused refresh token is not invalid_grant",
        )
        self.refused_at_mcp(rotated.access, "the access token of a grant whose refresh was reused")

        # A code presented twice ends the grant it opened.
        code, verifier = self.approve(client_id)
        second = self.exchange(client_id, code, verifier)
        again = self.token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
            }
        )
        check(again.status == 400, f"a reused code answered {again.status}")
        check(
            again.json()
            == {
                "error": "invalid_grant",
                "error_description": "the authorization code is not valid",
            },
            "a reused code is not invalid_grant",
        )
        self.refused_at_mcp(second.access, "the access token of a grant whose code was reused")

        # RFC 7009, by the app itself.
        code, verifier = self.approve(client_id)
        third = self.exchange(client_id, code, verifier)
        revoked = self.http.request(
            "POST",
            "/oauth/revoke",
            form={"token": third.refresh, "client_id": client_id},
            cookies=False,
        )
        check(revoked.status == 200, f"the RFC 7009 revocation answered {revoked.status}")
        self.refused_at_mcp(third.access, "the access token of a grant the app revoked")

        # The person signs out everywhere: their AI grants end with their sessions.
        code, verifier = self.approve(client_id)
        fourth = self.exchange(client_id, code, verifier)
        self.reaches_mcp(fourth, "a fresh grant's access token")
        out = self.http.request("POST", "/api/v1/auth/logout-everywhere", console=True)
        check(out.status == 204, f"sign out everywhere answered {out.status}")
        self.refused_at_mcp(fourth.access, "the access token of a person who signed out everywhere")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--base", required=True, help="the public entrypoint, e.g. https://localhost"
    )
    parser.add_argument("--cafile", help="the certificate the entrypoint presents")
    parser.add_argument("--phase", choices=("closed", "open"), required=True)
    parser.add_argument("--setup-code-file", help="the install code (phase open)")
    parser.add_argument("--wait-registration-s", type=float, default=45.0)
    args = parser.parse_args(argv)
    if args.phase == "open" and not args.setup_code_file:
        parser.error("--phase open needs --setup-code-file")
    flow = Flow(Client(args.base, args.cafile))
    run: Callable[[], None]
    if args.phase == "closed":
        run = flow.closed
    else:
        with open(args.setup_code_file, encoding="utf-8") as handle:
            code = handle.read().strip()
        run = lambda: flow.open(code, args.wait_registration_s)  # noqa: E731
    try:
        run()
    except Failed as exc:
        print(f"FAIL  {exc}")
        return 1
    except Unreachable as exc:
        print(f"UNREACHABLE  {exc}", file=sys.stderr)
        return 2
    print(f"mcp_flow: phase {args.phase}: every assertion held")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Run: `.venv/bin/pytest -q tests/test_ci_mcp_flow.py`
Expected: 16 PASS y 3 FAIL: `test_ci_runs_both_phases_through_traefik_on_a_real_openbao`, `test_no_secret_of_the_vault_travels_in_an_argv` y `test_ci_restores_the_old_identity_providers_database_from_a_pre_upgrade_backup` (el `ci.yml` aún es el viejo).

- [ ] **Step 4: El paso de la CI**

Guardar el paso nuevo en `"${TMPDIR:-/tmp}/p4_ci_step.yml"` (exactamente, con su sangría de seis espacios):

```yaml
      - name: the platform's own OAuth server and MCP door, live, through Traefik
        # THE HALF NO UNIT TEST CAN REACH (own authentication phase 4, plan
        # 2026-11-15, Task 7). The backend suite proves what the OAuth server
        # DECIDES, and test_deploy_mcp_route.py what the Traefik rules SAY.
        # This proves what the shipped images DO with them: the real `api` on
        # the stack's real OpenBao -- the token key and the pepper never leave
        # it, and this is the first run of deploy/README.md step 3.6 that is
        # not a reader's -- with `web` and `traefik`, reached on :443 with a
        # certificate the script verifies (no -k anywhere).
        # deploy/ci/mcp_flow.py lists every assertion; it prints claim NAMES,
        # never a token, a code, a password or the install code.
        #
        # OPENBAO WITH ONE SHARE, because CI has nobody to hand five to. Its
        # unseal key, root token and the its-app token live in this step's
        # shell and the throwaway .env, are masked in the log, and reach `bao`
        # on stdin, never in an argv a process listing on a shared runner
        # would show. The two files that held a secret are removed however the
        # step ends (the trap); `teardown` takes the vault itself with
        # `down -v`. On a self-hosted runner the throwaway .env stays in the
        # checkout until the next job's checkout cleans it. Production keeps
        # five shares and never runs on root.
        #
        # TWO PHASES: the opt-in (ITS_MCP_REGISTRATION) is read when the api
        # starts, so the closed phase runs first and the api is recreated
        # with it before the open one.
        working-directory: deploy
        run: |
          set -euo pipefail
          umask 077
          init_json="$RUNNER_TEMP/bao-init.json"
          trap 'rm -f "$init_json" "$RUNNER_TEMP/setup-code"' EXIT

          # 1. Nothing of the old identity provider is defined, at all.
          defined="$(docker compose config --services) $(docker compose config --images)"
          if grep -qi keycloak <<<"$defined"; then
            echo "::error::the compose stack still defines an identity provider service or image"; exit 1
          fi

          # 2. OpenBao: initialise, unseal, the its-app policy and its token
          #    (deploy/README.md 3.1-3.4), then provision (3.6).
          docker compose up -d openbao
          rc=1
          for _ in $(seq 1 30); do
            rc=0; docker compose exec -T openbao bao status >/dev/null 2>&1 || rc=$?
            [ "$rc" = 2 ] && break    # answering, sealed, not yet initialised
            sleep 2
          done
          [ "$rc" = 2 ] || { docker compose logs openbao; echo "::error::openbao never answered"; exit 1; }
          docker compose exec -T openbao \
            bao operator init -key-shares=1 -key-threshold=1 -format=json > "$init_json"
          unseal=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["unseal_keys_b64"][0])' "$init_json")
          root=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["root_token"])' "$init_json")
          rm -f "$init_json"
          echo "::add-mask::$unseal"
          echo "::add-mask::$root"
          # `key=-` reads the value from stdin, as it is: no trailing newline.
          printf '%s' "$unseal" | docker compose exec -T openbao bao write sys/unseal key=- >/dev/null
          app_token=$(docker compose exec -T openbao sh -c 'read -r BAO_TOKEN && export BAO_TOKEN &&
            bao policy write its-app /bao/its-app.hcl >/dev/null &&
            bao token create -orphan -policy=its-app -ttl=24h -field=token' <<<"$root")
          echo "::add-mask::$app_token"
          sed -i -e '/^ITS_OPENBAO_TOKEN=/d' -e '/^OPENBAO_TOKEN=/d' \
            -e '/^ITS_PUBLIC_CONSOLE_URL=/d' -e '/^ITS_MCP_REGISTRATION=/d' .env
          {
            echo "ITS_OPENBAO_TOKEN=$app_token"
            echo "OPENBAO_TOKEN=$root"
            echo "ITS_PUBLIC_CONSOLE_URL=https://localhost"
          } >> .env
          docker compose run --rm openbao-init

          # 3. The audit HMAC key (step 5) and a certificate for :443 (step 7).
          docker compose run --rm --no-deps --entrypoint sh api -c \
            'head -c 32 /dev/urandom > /var/lib/its/secrets/audit-hmac.key && chmod 600 /var/lib/its/secrets/audit-hmac.key'
          openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=localhost" \
            -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
            -keyout certs/privkey.pem -out certs/fullchain.pem 2>/dev/null

          # 4. The api, the console and the edge. Both behind Traefik, which
          #    routes only a healthy container: the api's metadata, and the
          #    console's page for `/`. `--noproxy '*'`: the runner's proxy, if
          #    it has one, is not the way to localhost (the flow sets no proxy
          #    either).
          get() { curl -sf --noproxy '*' --cacert certs/fullchain.pem "$@"; }
          wait_for_api() {
            local page
            for _ in $(seq 1 60); do
              if get -o /dev/null https://localhost/.well-known/oauth-authorization-server; then
                page="$(get -o /dev/null -w '%{content_type}' https://localhost/ || true)"
                case "$page" in text/html*) return 0 ;; esac
              fi
              sleep 3
            done
            docker compose logs api web traefik
            echo "::error::the api and the console never both answered through Traefik"
            return 1
          }
          docker compose up -d --build api web traefik
          wait_for_api
          flow() { python3 ci/mcp_flow.py --base https://localhost --cafile certs/fullchain.pem "$@"; }

          # 5. Unclaimed, registration closed.
          flow --phase closed

          # 6. The opt-in, read at start: recreate the api with it.
          echo "ITS_MCP_REGISTRATION=open" >> .env
          docker compose up -d --no-deps --force-recreate api
          wait_for_api

          # 7. Claimed with the install code the api writes on the first
          #    status request (deploy/README.md step 8), then the whole AI
          #    app's path.
          get -o /dev/null https://localhost/api/v1/setup/status
          docker compose exec -T api cat /var/lib/its/secrets/setup-code > "$RUNNER_TEMP/setup-code"
          flow --phase open --setup-code-file "$RUNNER_TEMP/setup-code"

          # 8. And nothing of the old identity provider ever ran in this
          #    project (not the host's: a shared runner may run anything).
          running="$(docker compose ps -a --format '{{.Image}} {{.Name}}')"
          if grep -qi keycloak <<<"$running"; then
            echo "::error::a container of the old identity provider ran in this job"; exit 1
          fi
```

el paso de K1 en `"${TMPDIR:-/tmp}/p4_ci_step_k1.yml"` (igual):

```yaml
      - name: the old identity provider's database -- kept by the pre-upgrade backup only, and restored
        # OPERATOR DECISION K1 (2026-11-15), live. A host upgraded from before
        # own authentication phase 4 keeps its `keycloak` database until the
        # operator retires it: the nightly backup leaves it out, the one
        # deploy.sh takes before an upgrade (ITS_BACKUP_OLD_IDENTITY_DB=1)
        # keeps it, because it is the way back, and restore.sh brings it back.
        # A fresh install has no such database, so this step makes one.
        working-directory: deploy
        run: |
          set -euo pipefail
          psql_admin() { docker compose exec -T postgres psql -X -v ON_ERROR_STOP=1 -U its_admin "$@"; }
          psql_admin -d postgres -c "CREATE DATABASE keycloak OWNER its_admin"
          psql_admin -d keycloak -c "CREATE TABLE realm_probe (x int)"
          ./scripts/backup.sh /tmp/ci-nightly
          test ! -e /tmp/ci-nightly/keycloak.dump
          ITS_BACKUP_OLD_IDENTITY_DB=1 ./scripts/backup.sh /tmp/ci-pre-upgrade
          test -s /tmp/ci-pre-upgrade/keycloak.dump
          # The operator retired it; then went back.
          psql_admin -d postgres -c "DROP DATABASE keycloak WITH (FORCE)"
          ITS_RESTORE_FORCE=1 ./scripts/restore.sh /tmp/ci-pre-upgrade | tee /tmp/kc-restore.log
          grep -q "keep it if you are rolling back" /tmp/kc-restore.log
          probe="$(psql_admin -A -t -d keycloak -c "SELECT count(*) FROM information_schema.tables WHERE table_name = 'realm_probe'")"
          [ "$(tr -d '[:space:]' <<<"$probe")" = 1 ] || { echo "::error::the restored keycloak database lost its table"; exit 1; }
          psql_admin -d postgres -c "DROP DATABASE keycloak WITH (FORCE)"
```

y el guion que lo pone en su sitio, en `"${TMPDIR:-/tmp}/p4_task7_ci.py"`:

```python
"""Own authentication phase 4, Task 7: the deploy job's two Keycloak steps
become one, against the platform's own OAuth server, and the backup round
trip gains the old identity provider's database (operator decision K1). Run
from the worktree root, with the two new steps saved as the files named in
argv[1] and argv[2]; it prints the job's steps and `bash -n` of both."""

import pathlib
import subprocess
import sys

import yaml

root = pathlib.Path(".")
step = pathlib.Path(sys.argv[1]).read_text()
k1_step = pathlib.Path(sys.argv[2]).read_text()
ci = root / ".github/workflows/ci.yml"
text = ci.read_text()
start = text.index("      - name: keycloak 26.2 — the realm still parses, and takes the token fields\n")
end = text.index("      - name: teardown\n        if: always()\n", start)
text = text[:start] + step + "\n" + text[end:]
after = "      - name: IT_SUITE_VERSION selects the image reference (interpolation only)\n"
assert text.count(after) == 1
text = text.replace(after, k1_step + "\n" + after)
old_name = "    name: deploy (compose first boot · app_user privileges)\n"
assert text.count(old_name) == 1
text = text.replace(
    old_name,
    "    name: deploy (compose first boot · app_user privileges · own OAuth server, live)\n",
)
ci.write_text(text)

doc = yaml.safe_load(text)
steps = doc["jobs"]["deploy"]["steps"]
names = [s.get("name") for s in steps]
print("\n".join(str(n) for n in names))
for prefix in ("the platform's own OAuth", "the old identity provider's database"):
    run = next(s["run"] for s in steps if str(s.get("name", "")).startswith(prefix))
    check = subprocess.run(["bash", "-n"], input=run, text=True, capture_output=True)
    print("bash -n:", check.returncode, check.stderr)
print("ok")
```

Run (desde la raíz; necesita `yaml`, que está en el venv del backend): `backend/.venv/bin/python "${TMPDIR:-/tmp}/p4_task7_ci.py" "${TMPDIR:-/tmp}/p4_ci_step.yml" "${TMPDIR:-/tmp}/p4_ci_step_k1.yml"`
Expected: la lista de pasos del job (`None`, `Throwaway .env`, …, `backup.sh -> restore.sh round trip, invoked AS DOCUMENTED`, `the old identity provider's database -- kept by the pre-upgrade backup only, and restored`, `IT_SUITE_VERSION selects the image reference (interpolation only)`, `the platform's own OAuth server and MCP door, live, through Traefik`, `teardown`), dos `bash -n: 0` y `ok`.

- [ ] **Step 5: Las pruebas pasan**

Run: `.venv/bin/pytest -q tests/test_ci_mcp_flow.py && .venv/bin/ruff check tests`
Expected: 19 PASS, limpio. (Con la Task 6 en el mismo árbol, el comando de su Step 4: 203.)

- [ ] **Step 6: El guion, en vivo contra la API (sin Traefik)**

La CI es la prueba de verdad (Global Constraints, «Despliegue»). Antes de empujar, la misma ejecución contra una API real en local, con TLS, base de datos nueva y los backends locales: las dos comprobaciones de páginas de la consola (`GET /oauth/authorize` y las rutas antiguas de Keycloak, que sirve `web`) se simulan, todo lo demás va en vivo. Guardar en `"${TMPDIR:-/tmp}/p4_live_check.py"`:

```python
"""Run deploy/ci/mcp_flow.py against a real uvicorn (TLS, local backends) on a
fresh database of the test cluster. Not Traefik and not `web`: the two
console-page checks are faked (the former Keycloak paths and GET
/oauth/authorize), everything else is live. A verification aid for own
authentication phase 4, Task 7; not part of the repository.

Run from backend/ with PYTHONPATH=$PWD, two arguments: the path of
mcp_flow.py and a work directory for the certificate, keys and logs.
"""

import importlib.util
import os
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, ".")
from tests.conftest import APP_ROLE, APP_PASSWORD, PgCluster, _psql, _run_migrations  # noqa: E402

script, work = Path(sys.argv[1]), Path(sys.argv[2])
work.mkdir(parents=True, exist_ok=True)
spec = importlib.util.spec_from_file_location("mcp_flow", script)
flow = importlib.util.module_from_spec(spec)
sys.modules["mcp_flow"] = flow
spec.loader.exec_module(flow)

dbname = f"its_flow_{secrets.token_hex(4)}"
_psql(f'CREATE DATABASE "{dbname}" OWNER its_admin')
_psql(f'GRANT CONNECT ON DATABASE "{dbname}" TO {APP_ROLE}', dbname)
admin = f"its_admin:its_admin_test@127.0.0.1:5432/{dbname}"
app_dsn = f"{APP_ROLE}:{APP_PASSWORD}@127.0.0.1:5432/{dbname}"
cluster = PgCluster(
    dbname=dbname,
    app_url=f"postgresql+asyncpg://{app_dsn}",
    admin_url=f"postgresql+asyncpg://{admin}",
    portal_url="",
    app_url_sync="",
    admin_url_sync="",
)
_run_migrations(cluster)

subprocess.run(
    [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        "-keyout", str(work / "key.pem"), "-out", str(work / "cert.pem"),
    ],
    check=True,
    capture_output=True,
)
BASE = "https://localhost:8743"
env = os.environ | {
    "ITS_ENVIRONMENT": "dev",
    "ITS_DATABASE_URL": cluster.app_url,
    "ITS_DATABASE_ADMIN_URL": cluster.admin_url,
    "ITS_REDIS_URL": "redis://localhost:6379/9",
    "ITS_CA_DIR": str(work / "ca"),
    "ITS_SIGNING_KEY_PATH": str(work / "job.key"),
    "ITS_VAULT_ROOT_KEY_PATH": str(work / "vault.key"),
    "ITS_AUTH_KEYS_DIR": str(work / "auth-keys"),
    "ITS_PUBLIC_CONSOLE_URL": BASE,
    "ITS_DEFAULT_ORG_SLUG": "toeshee",
    "ITS_SETUP_CODE_PATH": str(work / "setup-code"),
    "ITS_TRUSTED_PROXY_CIDRS": "10.255.255.0/24",
    "ITS_BACKUP_STATUS_DIR": str(work / "backup-status"),
    # Keycloak-era keys an upgraded .env still holds: must be ignored.
    "ITS_OIDC_ISSUER": "https://itms.example.com/realms/it-suite",
    "ITS_KEYCLOAK_ADMIN_URL": "http://keycloak:8080",
    "ITS_MULTI_TENANT": "true",
}
env.pop("ITS_MCP_REGISTRATION", None)
env.pop("HTTPS_PROXY", None)
env.pop("https_proxy", None)


def serve(extra: dict[str, str]) -> subprocess.Popen:
    log = open(work / f"uvicorn-{len(extra)}.log", "w")
    proc = subprocess.Popen(
        [
            ".venv/bin/uvicorn", "app.main:create_app", "--factory", "--host", "127.0.0.1",
            "--port", "8743", "--ssl-keyfile", str(work / "key.pem"),
            "--ssl-certfile", str(work / "cert.pem"), "--no-proxy-headers",
        ],
        env=env | extra,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    import ssl

    context = ssl.create_default_context(cafile=str(work / "cert.pem"))
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    )
    for _ in range(60):
        try:
            opener.open(BASE + "/livez", timeout=2)
            return proc
        except Exception:
            time.sleep(1)
    proc.kill()
    raise SystemExit("uvicorn never answered; see " + str(work))


# The console pages `web` would serve: faked here, nowhere else.
real_request = flow.Client.request


def request(self, method, path, **kw):
    if method == "GET" and path.startswith("/oauth/authorize?"):
        from email.message import Message

        headers = Message()
        headers["Content-Type"] = "text/html"
        return flow.Response("/oauth/authorize", 200, headers, b"<!doctype html><title>ITMS</title>")
    return real_request(self, method, path, **kw)


flow.Client.request = request
flow.FORMER_KEYCLOAK_PATHS = ()

server = serve({})
try:
    closed = flow.main(["--base", BASE, "--cafile", str(work / "cert.pem"), "--phase", "closed"])
finally:
    server.terminate()
    server.wait()
print("closed exit", closed)
server = serve({"ITS_MCP_REGISTRATION": "open"})
try:
    status = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(
            context=__import__("ssl").create_default_context(cafile=str(work / "cert.pem"))
        ),
    ).open(BASE + "/api/v1/setup/status")
    print("setup/status", status.status, status.read())
    opened = flow.main(
        [
            "--base", BASE, "--cafile", str(work / "cert.pem"), "--phase", "open",
            "--setup-code-file", str(work / "setup-code"),
        ]
    )
finally:
    server.terminate()
    server.wait()
    _psql(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
print("open exit", opened)
```

Run (desde `backend/`, con `PYTHONPATH=$PWD`): `.venv/bin/python "${TMPDIR:-/tmp}/p4_live_check.py" ../deploy/ci/mcp_flow.py "${TMPDIR:-/tmp}/p4-live"`
Expected: `closed exit 0`, `setup/status 200 b'{"needed":true}'`, una línea `ok` por cada aserción y `open exit 0`. (En el ensayo de la ronda de correcciones: 33 líneas `ok`, 7 de la fase cerrada y 26 de la abierta, entre ellas `/api/v1/me refuses a token shaped like Keycloak's: 401`. El guion pone `FORMER_KEYCLOAK_PATHS` a `()` porque aquí no hay `web`; en la CI la lista es la de verdad, y `test_the_former_keycloak_paths_are_all_checked` impide que quede vacía.)

- [ ] **Step 7: Commit**

```bash
git add -A deploy/ci deploy/keycloak backend/tests/test_ci_mcp_flow.py .github/workflows/ci.yml
git commit -m "ci(deploy): the deploy job proves the platform's own OAuth server live (phase 4)"
```

**Lo que la CI tiene que enseñar al fusionar 6 y 7** (lo comprueba el coordinador en el run del PR): el paso nuevo verde, con las líneas `ok` de las dos fases y `mcp_flow: phase open: every assertion held`. Si la API no arranca en producción por algo que este plan no previó (una clave o un ajuste que pida el modo `production`), el paso imprime `docker compose logs api traefik`: se corrige con esa evidencia, no se relaja el paso.

---

### Task 8: Consola — la página de estado sin las sondas de Keycloak

**Files:**
- Modify: `frontend/src/modules/platform-status/PlatformStatusPage.tsx` (`:41-49`, `:150-152`, `:552-556`), `frontend/src/modules/platform-status/types.ts` (`:32-34`, `:45`), `frontend/src/i18n/en/enums.ts` (`:147-149`, `:171`), `frontend/src/i18n/es/enums.ts` (`:141-143`, `:165`); `frontend/src/preview/PreviewPage.tsx:4` (el comentario de cabecera)
- Test: `frontend/src/modules/platform-status/PlatformStatusPage.test.tsx` (`:38-47`, `:194-206`), `frontend/src/modules/platform-status/platform-status.es.test.tsx` (`:30`, `:107`)

**Interfaces:** ninguna. Depende de que el backend ya no registre `jwks` ni `keycloak_events` (Task 5); la consola sigue mostrando como su clave derivada cualquier nombre de sonda que no conozca.

**Decisiones:** P4-21, P4-30.

- [ ] **Step 1: Preparar la worktree** (Task 1, Step 1, desde `frontend/`).

- [ ] **Step 2: El cambio y sus pruebas, con un guion**

Las pruebas se re-apuntan en el mismo guion: la que fija que la página muestra el `detail` de una sonda lo hace ahora con `backup.nightly` sin configurar (el mismo caso: `ok` y `configured: false`), y la española deja `jwks` por `ca`. Guardado en `"${TMPDIR:-/tmp}/p4_task8_console.py"` y corrido desde la raíz:

```python
"""Own authentication phase 4, Task 8: the status page without the old
identity provider's probes. Run from the worktree root."""

import pathlib

src = pathlib.Path("frontend/src")


def swap(path: pathlib.Path, old: str, new: str) -> None:
    text = path.read_text()
    assert text.count(old) == 1, (path, old[:70])
    path.write_text(text.replace(old, new))


page = src / "modules/platform-status/PlatformStatusPage.tsx"
swap(
    page,
    """//   3. Drop a probe's `detail`. Added 2026-08-18 with the `keycloak_events`
//      probe, and it is not cosmetic: that probe answers `ok` with
//      `configured: false` for a deployment where Keycloak credential events
//      are NOT being forwarded into the audit chain. A row reading
//      `keycloak_events / ok / —` would tell an operator forwarding works. The
//      status alone is deliberately unalarming -- nothing is broken -- so the
//      whole distinction lives in `detail`, and a page that discards it turns
//      a visible gap back into a silent one. `jwks` (`keys: n`) was invisible
//      for the same reason until now.
""",
    """//   3. Drop a probe's `detail`. Added 2026-08-18 with a probe that answered
//      `ok` with `configured: false` for a feature nobody had set up (the
//      forwarding of the old identity provider's events, removed in own
//      authentication phase 4), and the backup probes do the same today: a
//      host with no backup timer reads `backup.nightly / ok`, and only
//      `configured: false` says nothing is being backed up. The status alone
//      is deliberately unalarming -- nothing is broken -- so the whole
//      distinction lives in `detail`, and a page that discards it turns a
//      visible gap back into a silent one.
""",
)
swap(
    page,
    """  identity: "id-badge-2",
  keycloak: "id-badge-2",
  keycloak_events: "history",
  jwks: "key",
""",
    """  identity: "id-badge-2",
""",
)
swap(
    page,
    """        {/* THE SAME SHAPE AS THE REASON AND MODULE CELLS: the words first,
            the key under them. `object_store` and `keycloak_events` sat in
            snake case in a column of ordinary nouns, and the key is still on
            the row because it is what the health endpoint and the logs call
            this probe. */}""",
    """        {/* THE SAME SHAPE AS THE REASON AND MODULE CELLS: the words first,
            the key under them. `object_store` sat in snake case in a column
            of ordinary nouns, and the key is still on the row because it is
            what the health endpoint and the logs call this probe. */}""",
)

types = src / "modules/platform-status/types.ts"
swap(
    types,
    """// because `.` separates catalogue paths. `keycloak_events` is deliberately
// absent: its English label would name the identity vendor, which no catalogue
// string may do.
""",
    """// because `.` separates catalogue paths.
""",
)
swap(types, '  | "identity"\n  | "jwks"\n', '  | "identity"\n')

for lang, label in (("en", '    jwks: "Jwks",\n'), ("es", '    jwks: "Claves de inicio de sesión (JWKS)",\n')):
    enums = src / f"i18n/{lang}/enums.ts"
    swap(enums, label, "")
    text = enums.read_text()
    start = text.index("`keycloak_events` is left out on purpose:")
    end = text.index("*/", start)
    print(lang, repr(text[start - 40 : end + 2]))

swap(
    src / "i18n/en/enums.ts",
    """   * the same either way. `keycloak_events` is left out on purpose: its English
   * would name the identity vendor, which the no-vendor-names guard forbids in
   * any catalogue string.
   */""",
    """   * the same either way.
   */""",
)
swap(
    src / "i18n/es/enums.ts",
    """   * the same either way. `keycloak_events` is left out on purpose: its English
   * would name the identity vendor, which the no-vendor-names guard forbids in
   * any catalogue string.
   */""",
    """   * the same either way.
   */""",
)

test = src / "modules/platform-status/PlatformStatusPage.test.tsx"
swap(
    test,
    """    // `ok` AND NOT FORWARDING. The status is deliberately unalarming -- an
    // operator who has not enabled Keycloak event forwarding has not broken
    // anything -- so the entire distinction lives in `detail`.
    {
      name: "keycloak_events",
      status: "ok",
      duration_ms: 1,
      reason: null,
      detail: { configured: false },
    },
""",
    """    // `ok` AND NOT BACKING UP. The status is deliberately unalarming -- a
    // host with no backup timer has not broken anything -- so the entire
    // distinction lives in `detail`.
    {
      name: "backup.nightly",
      status: "ok",
      duration_ms: 1,
      reason: null,
      detail: { configured: false },
    },
""",
)
swap(
    test,
    """    // THE FAILURE THIS GUARDS is a page that reads `keycloak_events / ok / —`
    // on a deployment where Keycloak credential events are not reaching the
    // audit chain at all. The probe answers `ok` on purpose; `configured:
    // false` is the part that says forwarding is off, and a page that drops
    // `detail` turns a deliberately-visible gap back into a silent one.
    renderWithProviders(<PlatformStatusPage />);
    const row = (await screen.findByText("keycloak_events")).closest("tr");""",
    """    // THE FAILURE THIS GUARDS is a page that reads `backup.nightly / ok / —`
    // on a host where nothing is being backed up at all. The probe answers
    // `ok` on purpose; `configured: false` is the part that says the backup
    // is not set up, and a page that drops `detail` turns a
    // deliberately-visible gap back into a silent one. (Until own
    // authentication phase 4 this row was the identity provider's event
    // forwarding, which answered the same way.)
    renderWithProviders(<PlatformStatusPage />);
    const row = (await screen.findByText("backup.nightly")).closest("tr");""",
)

es_test = src / "modules/platform-status/platform-status.es.test.tsx"
swap(
    es_test,
    '    { name: "jwks", status: "ok", duration_ms: 2, reason: null, detail: { keys: 2 } },\n',
    '    { name: "ca", status: "ok", duration_ms: 2, reason: null, detail: null },\n',
)
swap(es_test, '    expect(within(dependencies).getByText("Claves: 2")).toBeInTheDocument();\n', "")
# The dev-only design harness says why it exists, in the present tense.
swap(
    src / "preview/PreviewPage.tsx",
    " * WHY THIS EXISTS. Every screen in this console is behind Keycloak, so until a\n"
    " * full stack is running nobody can see the ported design",
    " * WHY THIS EXISTS. Every screen in this console is behind the sign-in, so until\n"
    " * a full stack is running nobody can see the ported design",
)
print("ok")
```

Expected: dos líneas que muestran la frase de `keycloak_events` que se quita de cada `enums.ts`, y `ok`. El guion también corrige el comentario de cabecera de `frontend/src/preview/PreviewPage.tsx:4`, que decía en presente que toda pantalla de la consola estaba «behind Keycloak» (revisión 1, M15).

- [ ] **Step 3: Las pruebas pasan**

Run (desde `frontend/`): `npx vitest run src/modules/platform-status src/i18n src/lib/noVendorNames.test.ts && npx tsc -b`
Expected: PASS (81 en el ensayo) y `tsc` sin salida.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/modules/platform-status frontend/src/i18n/en/enums.ts frontend/src/i18n/es/enums.ts
git commit -m "chore(console): the status page forgets the identity provider's probes (phase 4)"
```

---


### Task 9: Runbook — `deploy/README.md`, `deploy/GO-LIVE.md`, `docs/07-backup-restore.md`, y su guarda

**Files:**
- Create: `backend/tests/test_runbook_without_keycloak.py`
- Modify: `deploy/README.md`, `deploy/GO-LIVE.md`, `docs/07-backup-restore.md`

**Interfaces:**
- Depende de: las Tasks 6 y 7 fusionadas (el runbook describe el despliegue que dejaron y la CI que lo prueba); `restore.sh`, `backup.sh` y `deploy.sh` ya citan «Retiring the old identity provider's data».
- Produce: la sección `## Retiring the old identity provider's data`: el paso único de las decisiones del operador K1 y K2 (2026-11-15, Apéndice B.1).

**El alcance, dicho entero.** Esta tarea quita del runbook lo que es de Keycloak y lo que ya no es verdad por eso; no lo reescribe entero. La reescritura del runbook para el modelo de sesión propia es del proyecto 4 (lo dice el propio runbook, «Status of this runbook»: «the runbook is rewritten with the deployment project (design §9)»). Por eso la sección de la lista de dominios conserva su explicación de «claim» y «stored row», que la fase 1 ya dejó desfasada y sus siete pruebas fijan (P4-26): aquí solo pierde las tres frases que nombran a Keycloak (P4-28 para `GO-LIVE.md` §2, que sí se reescribe porque entero era de Keycloak).

- [ ] **Step 1: Preparar la worktree**

Task 1, Step 1; fusionar la rama del coordinador con las oleadas 1-3 y comprobar que `ls deploy/keycloak` responde «No such file».

- [ ] **Step 2: La guarda, que falla**

Crear `backend/tests/test_runbook_without_keycloak.py`:

```python
"""The runbooks after own authentication phase 4 (plan 2026-11-15, Task 9).

Keycloak left the stack, so a runbook that still told an operator to open its
admin console, read its realm import or wait for `keycloak-init` would send
them after something that is not there. What may still name it: the section
that retires its data on a host upgraded from before
(`## Retiring the old identity provider's data`, which `restore.sh`,
`backup.sh` and `deploy.sh` point at; operator decisions K1 and K2,
2026-11-15), the dated table of what was broken on the first boots, and any
line that says it is history by naming phase 4.

And because whole sections went, every in-page link must still land on a
heading: a deleted section is otherwise a link that scrolls nowhere.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
README = REPO / "deploy" / "README.md"
GO_LIVE = REPO / "deploy" / "GO-LIVE.md"
BACKUP_DOC = REPO / "docs" / "07-backup-restore.md"
SCRIPTS = REPO / "deploy" / "scripts"
RESTORE_SH = SCRIPTS / "restore.sh"

# The names case-blind; the `.env` keys as keys, so the audit action
# `auth.entra_credential_used` (Microsoft sign-in, phase 2) is not one.
NAMES = re.compile(r"(?i:keycloak|kcadm|realm-it-suite)|KC_HOSTNAME|ITS_OIDC_|\bENTRA_[A-Z]")
RETIRING = "## Retiring the old identity provider's data"
ALLOWED_SECTIONS = (RETIRING, "## What was broken, and how you would recognise it")
GONE_HEADINGS = (
    "## Break-glass: the identity admin console",
    "## Branding and extending sign-in",
    "## Forwarding Keycloak's credential events",
    "## Keycloak SMTP",
    "## The provisioning client",
    "### What keycloak-init still converges",
)


def _sections(text: str) -> list[tuple[str, str]]:
    """`(heading, body)` for every `## ` section, outside code fences; the
    text before the first one is `("", ...)`."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            sections.append((line, []))
        sections[-1][1].append(line)
    return [(heading, "\n".join(lines)) for heading, lines in sections]


def _dated(lines: list[str], index: int) -> bool:
    """History says when it stopped being true: "phase 4" on the line, on one
    of the three before it (a command under its comment) or on the next one
    (a sentence wrapped across the two)."""
    return any("phase 4" in line for line in lines[max(0, index - 3) : index + 2])


def _offenders(path: Path, allowed: tuple[str, ...] = ()) -> list[str]:
    found = []
    for heading, body in _sections(path.read_text()):
        if allowed and heading.startswith(allowed):
            continue
        lines = body.splitlines()
        for index, line in enumerate(lines):
            if NAMES.search(line) and not _dated(lines, index):
                found.append(f"{path.name} [{heading or 'top'}]: {line.strip()[:100]}")
    return found


def test_the_guard_finds_a_stray_name() -> None:
    """Non-vacuity: a line naming the old provider outside the allowed
    sections, and not dated, is found; a dated one is not."""
    assert NAMES.search("run `docker compose up keycloak-init` again")
    assert NAMES.search("revoke ENTRA_CLIENT_SECRET") and NAMES.search("ITS_OIDC_ISSUER")
    assert not NAMES.search("the platform's own sign-in")
    assert not NAMES.search("(`auth.entra_credential_used`)")
    sections = _sections("intro\n## A\nkeycloak here\n```\n## not a heading\n```\n## B\nok\n")
    assert [h for h, _ in sections] == ["", "## A", "## B"]
    lines = ["# Only before own authentication", "# phase 4:", "x", "pg_restore -d keycloak"]
    assert _dated(lines, 3)
    assert _dated(["integrated with Keycloak until own", "authentication phase 4."], 0)
    assert not _dated(["# phase 4", "a", "b", "c", "pg_restore -d keycloak", "d"], 4)


def test_the_runbook_names_the_old_provider_only_where_it_retires_it() -> None:
    assert _offenders(README, ALLOWED_SECTIONS) == []


def test_go_live_and_the_backup_document_name_it_only_as_history() -> None:
    assert _offenders(GO_LIVE) + _offenders(BACKUP_DOC) == []


def test_the_sections_about_the_old_provider_are_gone() -> None:
    text = README.read_text()
    assert [h for h in GONE_HEADINGS if h in text] == []
    # What the SMTP section held that was not Keycloak's keeps a home.
    assert "## Notification email channels over Microsoft 365" in text


def test_the_retiring_section_exists_where_the_scripts_point() -> None:
    assert RETIRING in README.read_text()
    for script in ("restore.sh", "backup.sh", "deploy.sh"):
        assert "Retiring the old identity provider's data" in (SCRIPTS / script).read_text(), script


def _section(heading: str) -> str:
    return next(body for h, body in _sections(README.read_text()) if h.startswith(heading))


def test_the_retiring_step_is_the_operators_decision_entire() -> None:
    """Operator decisions K1 and K2 (2026-11-15): one step, run when the
    operator chooses, that drops the database, deletes the three secret
    files and the four keys a rollback needs, then the other keys, says it
    closes the way back, and sends them to revoke the old Entra secret --
    with commands that need nothing filled in (CLAUDE.md)."""
    body = _section(RETIRING)
    flat = " ".join(body.split())
    for needed in (
        "dropdb -U its_admin keycloak",
        "keycloak-admin.secret",
        "keycloak-events.secret",
        "keycloak-bootstrap-admin.secret",
        "KEYCLOAK_ADMIN_USER|KEYCLOAK_ADMIN_PASSWORD|ITS_OIDC_ISSUER|ITS_OIDC_JWKS_URL",
        "closes the way back",
        "ENTRA_CLIENT_SECRET",
        "Do not delete",
    ):
        assert needed in flat, needed
    # Said before anything is deleted, not only after (re-review, m8).
    assert flat.index("closes the way back") < flat.index("dropdb")
    assert "one release later" not in flat
    code = "\n".join(re.findall(r"```sh\n(.*?)```", body, re.DOTALL))
    assert code and not re.search(r"<[a-z][a-z -]*>", code), "a placeholder in a command"


def test_the_first_upgrade_past_phase_4_pulls_first() -> None:
    """The release before phase 4 runs its own deploy.sh, which pulls part-way
    through and goes on running the old text (coordinator's decision,
    2026-11-15; review 1, I5)."""
    body = _section("## Upgrading an existing deployment")
    assert "git -C .. pull --ff-only && ./scripts/deploy.sh" in body
    assert "--remove-orphans" not in body


def test_the_status_keeps_what_an_old_host_still_needs() -> None:
    """An upgrade from before phase 1 crosses all four phases at once, so
    the facts phases 1 and 3 changed stay (review 1, I3); and nothing tells
    the operator to delete the keys a rollback needs (I4)."""
    status = " ".join(_section("## Status of this runbook").split())
    assert "no path to one" in status
    assert "stops working (401)" in status
    assert "harmless" not in status.lower()
    text = README.read_text()
    assert "delete them by hand" not in text and "remove them by hand" not in text


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation but `-` and `_`
    dropped, spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def test_every_in_page_link_lands_on_a_heading() -> None:
    for path in (README, GO_LIVE):
        text = path.read_text()
        anchors: set[str] = set()
        seen: dict[str, int] = {}
        fenced = False
        for line in text.splitlines():
            if line.startswith("```"):
                fenced = not fenced
                continue
            match = None if fenced else re.match(r"^#{1,6} (.+)$", line)
            if match:
                base = _slug(match.group(1))
                count = seen.get(base, 0)
                seen[base] = count + 1
                anchors.add(base if count == 0 else f"{base}-{count}")
        links = set(re.findall(r"\]\(#([^)]+)\)", text))
        assert links - anchors == set(), (path.name, sorted(links - anchors))
```

Run: `.venv/bin/pytest -q tests/test_runbook_without_keycloak.py`
Expected: 6 FAIL y 3 PASS (en el ensayo de la segunda ronda de correcciones, sobre un árbol con las oleadas 1 a 3): las líneas que nombran al proveedor viejo (137 del README, 18 de `GO-LIVE.md` y 7 de `docs/07-backup-restore.md`), las secciones de Keycloak siguen, no hay sección «Retiring…» ni el paso único, ni el `git pull` primero en «Upgrading». Pasan la no vacuidad, los enlaces internos (hoy resuelven todos) y `test_the_status_keeps_what_an_old_host_still_needs`, que es una guarda de lo que el runbook ya dice y la Task 9 no debe perder.

- [ ] **Step 3: `deploy/README.md`, sección por sección**

Cada cambio, en el orden del archivo. Las líneas son las de `dd062f8`; las Tasks 1 y 3 ya movieron unas pocas en `### Revoking` y en los riesgos. **Las frases citadas entre «» se buscan sin atender a los saltos de línea**: en el archivo, muchas están partidas en dos o tres líneas (los items 3, 5, 11, 12, 14, 18 y 20; en `GO-LIVE.md`, §6 y §7; en `docs/07`, `:21`). Lo que sustituye se escribe con el ancho de línea del archivo.

1. **Cabecera (`:3-5`).** «every default that must be changed — secrets, TLS, the Keycloak admin account, the OpenBao seal, …» → «every default that must be changed — secrets, TLS, the OpenBao seal, the audit-chain key backup, database backups.».

2. **«Status of this runbook», el párrafo en negrita y sus viñetas (`:29-91`).** Se sustituye desde «**Since own authentication phase 1 (2026-11) the console signs in on the» hasta la última viñeta («… ten minutes.») por:

```markdown
**Since own authentication phase 4 (2026-11-15) there is no Keycloak.** The
console and the portal sign in on the platform's own screens -- a password
and a second factor, or Microsoft, and a session cookie -- since phase 1, and
AI apps connect over MCP through the platform's own OAuth server since phase
3 (`docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md`). Own
authentication phase 4 removed the `keycloak` and `keycloak-init` services,
their image, their routes and every setting they read. Sections written before
phase 1 still describe token claims where the platform now reads its own
session; the runbook is rewritten with the deployment project (design §9).
What the four phases changed that every deployment needs -- and a host
upgraded from a release before phase 1 crosses all four at once:

- **`ITS_PUBLIC_CONSOLE_URL` is required for any sign-in**, and it must be
  exactly the `https://host[:port]` origin the browser's address bar shows
  (step 1). Unset, or naming another host, port or scheme, every sign-in write
  is refused as `csrf` and nobody can sign in -- first boot completes, then
  the sign-in screen refuses. Passkeys, the reset and invitation links, the
  OAuth server and the recovery command are built from it too. The platform
  sends invitations and password resets itself, through **Settings → Email**.
- **`ITS_AUTH_KEYS_BACKEND=openbao` must be in `.env`** (step 3, the sign-in
  keys). A new `.env` copied from `.env.example` has it; one from before
  phase 1 does not, and without it the api will not start.
- **Phase 1 installs on a fresh database.** Design P7: "no hay datos que
  conservar" -- no account carries over from the old identity provider.
  Migration 0245 is safe on a populated database (it only creates tables),
  but on a host upgraded from before phase 1 an existing technician has no
  password row and **no path to one**: forgot-password needs an existing
  password, an invitation refuses an address that already has an account,
  and the recovery command keeps the password as it is. First boot (step 8)
  mints the one `org-admin`, who sets their password on the setup screen;
  every other technician is invited from **Settings → Users**.
- **A super-admin is never invited** (no organization can grant the role, so
  none can invite or re-invite its holder), and an account without a password
  has no way to get one: forgot-password needs an existing password and a
  second factor, and the recovery command leaves the password as it is. So a
  super-admin is made from a technician who has already signed in -- the
  first-boot `org-admin`, or an invited technician who finished enrolment --
  by the direct database grant in [Publishing the agent to the
  console](#publishing-the-agent-to-the-console) (`super-admin` is not
  grantable from any screen). The account keeps its own password and second
  factor; nothing is mailed.
- **Phase 2's migration 0246** runs with every `up -d` (`migrate`), and
  phase 2 adds **no `.env` key and no OpenBao path**. A host with
  `ITS_AUTH_KEYS_BACKEND=local` in production now refuses to start (it had no
  business running); `grep '^ITS_AUTH_KEYS_BACKEND=openbao' .env` must print
  the line.
- **Sign in with Microsoft is configured in the console**, not in `.env`:
  see [Sign in with Microsoft](#sign-in-with-microsoft-own-authentication-phase-2).
- **Phase 3 moves MCP onto the platform's own OAuth server**
  ([AI connections (MCP)](#ai-connections-mcp)). A `.env` that already holds
  `ITS_MCP_REGISTRATION=open` -- step 2 of the connection steps before phase
  3 -- now opens the platform's own anonymous registration (`POST
  /oauth/register`, advertised as `registration_endpoint`) whenever MCP is
  on. Claude and Claude Code no longer need it, because the platform reads
  their client metadata documents: delete the line unless another app needs
  it (step 7 of "Turning it on"), with `sed -i '/^ITS_MCP_REGISTRATION=/d'
  .env`, and deploy. And every connector made before phase 3 stops working
  (401): each person removes the connector, adds it again and approves on
  the console's consent page, with their second factor confirmed in the last
  ten minutes.
- **The first upgrade past phase 4 starts with `git pull`**, then
  `./scripts/deploy.sh` ([Upgrading an existing
  deployment](#upgrading-an-existing-deployment) says why).
- **An existing `.env` keeps the old identity provider's keys**, which
  `scripts/deploy.sh` names on every run. Nothing in this release reads them,
  but the release before phase 4 does not start without four of them, so
  **keep them** until you close that way back in [Retiring the old identity
  provider's data](#retiring-the-old-identity-providers-data).
  `ITS_DEFAULT_ORG_SLUG` is not one of them: keep it.
- **A host upgraded from before phase 4 still has the old identity
  provider's database and three secret files.** Nothing in this release uses
  them. The way back past phase 4 is the backup taken before that upgrade:
  the newest `~/backups/pre-upgrade-*` whose `alembic_version` is below
  `0248` ([Rolling back a deployment](#rolling-back-a-deployment)). The
  nightly and off-site backups leave the database out from phase 4 on. They
  all stay until you remove them, when you choose: [Retiring the old identity
  provider's data](#retiring-the-old-identity-providers-data).
```

3. **DNS (`:147-149`).** «including in `.env` (`ITS_OIDC_ISSUER`, `ITS_GATEWAY_URL`, `ITS_GATEWAY_RELAY_URL`)» → «including in `.env` (`ITS_PUBLIC_CONSOLE_URL`, `ITS_GATEWAY_URL`, `ITS_GATEWAY_RELAY_URL`)».

4. **Step 1, el bucle (`:237-238`).** «Five you generate now» → «Four you generate now», y el bucle:

```sh
for v in POSTGRES_ADMIN_PASSWORD APP_DB_PASSWORD PORTAL_DB_PASSWORD MINIO_ROOT_PASSWORD; do
  sed -i "s|^$v=change-me$|$v=$(openssl rand -base64 24 | tr -d '=+/')|" .env
done
sed -i 's/your-host/itms.toeshee.com/g' .env
```

5. **Step 2 (`:322-326`).** «(the migration/bootstrap role), creates the `keycloak` database, and installs» → «(the migration/bootstrap role), and installs»; y en la salida esperada (`:335-341`) se borra la **última** línea `postgres-1  | CREATE DATABASE`, la que sigue a los dos `ALTER DEFAULT PRIVILEGES`: es la de la base `keycloak` (`init-app-user.sh:67`). La primera, la de `itsuite`, la escribe el `docker_setup_db` del entrypoint de la imagen antes de cualquier guion de init, y se queda.

6. **3.6 (`:533-538`).** El párrafo «⚠ **UNVERIFIED — this paragraph has not been run against a live OpenBao.** …» se sustituye por:

```markdown
**Run in CI since own authentication phase 4.** The `deploy` job initialises
OpenBao (one share), loads `its-app`, runs `openbao-init` and starts the api
against it, then signs a person in and connects an AI app end to end through
Traefik (`deploy/ci/mcp_flow.py`): transit `hmac` with the `auth-pepper` key
and `sign` with `auth-signing` run live there. Five shares, the unseal by
hand and a host reboot have still only been run by the people who wrote
this section.
```

7. **Step 7 (`:742`, `:754`).** En la salida de `docker compose ps -a`, se borra la línea `keycloak          Up 26 seconds`; y «The four `Exited (0)` services are the one-shot jobs» → «The three `Exited (0)` services are the one-shot jobs» (la salida mostrada tiene tres: `migrate`, `openbao-init`, `postgres-grants`).

8. **Step 8 (`:810-841`).** Desde «First confirm the realm imported and the init job finished:» hasta el párrafo «**Configure email first if you can.** … removes that entirely.» (ambos incluidos, con el bloque «> **Superseded by own authentication (phase 1).**») se sustituye por:

```markdown
The setup screen sets the administrator's password itself and sends **no
mail**; the first sign-in enrols the second factor (step 9). Account mail
(invitations, password resets) is configured afterwards in ITMS at
**Settings → Email**.
```

y el párrafo «**The organization's slug is `ITS_DEFAULT_ORG_SLUG` from `.env`** … cannot sign in to.» (`:862-866`) queda:

```markdown
**The organization's slug is `ITS_DEFAULT_ORG_SLUG` from `.env`** — `toeshee`
in the shipped example — not something you type, and not derived from the name
you enter unless that setting is blank. That is deliberate: the sign-in, the
OAuth server for AI apps and the recovery command all look people up in that
one organization, and an organization created under any other slug is one its
own administrator cannot sign in to.
```

9. **Step 9 (`:875-926`).** El paso entero (desde `### Step 9 — Finish enrolment and sign in` hasta el «⚠ **UNVERIFIED**: no browser session, …» incluido) se sustituye por:

```markdown
### Step 9 — Sign in, and enrol your second factor

Setup does not sign you in. Open `https://itms.toeshee.com/sign-in` and sign
in with the address and the password you gave the setup screen. The first
sign-in asks for a second factor before anything else -- an authenticator
app (TOTP) or a passkey -- and then shows ten recovery codes, once. Keep them
where you keep the OpenBao shares: an administrator who loses every factor
needs one, or the [recovery
link](#recovery-link-an-administrator-who-lost-every-second-factor).

There is no identity admin console behind this product any more. Everything
an administrator does with accounts -- inviting, disabling, resetting second
factors, signing someone out everywhere -- is in **Settings → Users**, and
Microsoft sign-in is in **Settings → Microsoft sign-in**.
```

10. **Las secciones de Keycloak, enteras.** Se borran, desde su línea de título hasta la línea anterior al título `## ` siguiente:
    - `## Break-glass: the identity admin console (VENDOR-ONLY)` (`:1126-1261`, con sus `###`);
    - `## Branding and extending sign-in (VENDOR-ONLY)` (`:1262-1512`);
    - `## Forwarding Keycloak's credential events` (`:1657-1786`);
    - `## The provisioning client` (`:2077-2193`).

11. **`## Keycloak SMTP (before phase 1) — …` (`:1787-1980`).** Se borra desde su título hasta el final de `### 3. What this does NOT achieve` (la línea anterior a `### 4. Signing in to Microsoft 365 instead of with a password`). Lo que queda se re-aloja:
    - `### 4. Signing in to Microsoft 365 instead of with a password` pasa a `## Sending account mail through Microsoft 365 (Settings → Email)`; en su texto, se borra la frase «The realm is not involved.»; «this secret lives in the realm, and it should not also unlock reading mail.» → «this secret sends the platform's mail, and it should not also unlock reading mail.»; «If step 9 fails with an identity-provider error» → «If step 9 fails with a Microsoft sign-in error»; «The Microsoft application details are removed from the realm in the same save.» → «The Microsoft application details are removed from ITMS's vault in the same save.».
    - `### 5. Notification email channels over Microsoft 365` pasa a `## Notification email channels over Microsoft 365`, y «The Azure side is steps 1-6 above.» → «The Azure side is steps 1-6 of the section above.».

12. **`## Sign in with Microsoft (own authentication, phase 2)` (`:1515-1517`).** El párrafo «**This replaces "3. Sign in with Microsoft — by hand, for now" above for the console and the portal.** Since own authentication phase 3 that section concerns nobody: Keycloak signs nobody in, and phase 4 removes it.» se borra.

13. **Idioma (`:1630-1636`).** La viñeta «- the Keycloak sign-in pages and Keycloak's own emails (invitations, password resets). … were not updated.» se sustituye por «- the sign-in pages and the account emails (invitations, password resets), which the platform serves and sends itself in the person's own language;».

14. **La lista de dominios.** La celda entera «| should keep access | Fix the address in Keycloak, then have them sign in once. … it can only see rows. |» → «| should keep access | No screen changes a technician's address (none has since own authentication phase 1): delete the account and invite the correct address (**Settings → Users**), or disable it (next row). |» — `user_admin/api.py` no tiene `PATCH /users/{id}`, y `me.py:25-31` deja el correo fuera del perfil; «If it still exits `1` on an address you have already fixed in Keycloak,» → «If it still exits `1` on an address you have already fixed,»; «an address changed in Keycloak this morning by» → «an address changed this morning by». Las siete pruebas de `tests/test_email_domain_runbook.py` siguen pasando: ninguna lee esas tres frases.

15. **`### What keycloak-init still converges (until phase 4)` (`:2442-2476`).** Se borra entera.

16. **`### What Traefik routes` (`:2479-2497`).** Las tres filas de Keycloak de la tabla se sustituyen por una:

```markdown
| `/realms/…`, `/resources/…`, `/js/…`, `/admin/…` | the console's own page: no router names them since own authentication phase 4, so no identity provider is reachable on `:443` |
```

y el párrafo de debajo («The `deploy` CI job still proves Keycloak's half of this live …») por:

```markdown
The `deploy` CI job proves this live, through the real Traefik, `web` and
`api` containers and the api on a real OpenBao (`deploy/ci/mcp_flow.py`):
the 401 and its `resource_metadata`, both discovery documents, the 404 where
an OpenID document would be, registration closed and then open, the consent,
PKCE, the token's audience, refresh rotation and reuse, RFC 7009, `/mcp`
itself, and that the old identity provider's paths reach only the console.
The backend suite proves the server's decisions
(`backend/tests/test_oauth_*.py`, `test_mcp_own_tokens.py`).
```

17. **`### Residual risks, stated plainly`.** Se borran los riesgos 7 («**Until phase 4, Keycloak's own anonymous registration opens with the same setting.**» …) y 8 («**Realm names must be lowercase**» …), y los siguientes se renumeran: 9 → 7, 10 → 8, 11 → 9, 12 → 10 (P4-29). La frase de cabecera queda:

```markdown
**Risk 3 exists only while self-registration is open**
(`ITS_MCP_REGISTRATION=open`); closing it ends it for new apps. **Risks 1, 2,
4, 8 and 9 exist whenever MCP is on**, because an app known by its client
metadata document needs no opt-in. Risks 5, 6, 7 and 10 hold always.
```

y, dentro del riesgo 10 (el antiguo 12, de la Task 3), «(risk 9)» → «(risk 7)». Después, `grep -n "risk [0-9]\|Risk [0-9]" deploy/README.md` no debe nombrar ningún número viejo.

18. **«Upgrading an existing deployment».** Cuatro cambios:
    - Detrás del primer párrafo («**The short version: `./scripts/deploy.sh`.** … without acting on it.»):

````markdown
**The first upgrade past own authentication phase 4 (2026-11-15) starts with
the pull**, from `deploy/`:

```sh
git -C .. pull --ff-only && ./scripts/deploy.sh
```

The release before it runs its own `deploy.sh`, which pulls part-way through
-- and bash goes on running the text it already opened, which restarts and
provisions the old identity provider, services this compose file no longer
has: it stops after the migration, with the app tier still on the old images.
If that already happened, run `./scripts/deploy.sh` again: it sees the old
identity provider's containers still there and finishes the upgrade as a full
deploy, instead of answering "Already deployed".
````

    - En la lista «Since 2026-09-23 it also:», la viñeta «- **runs `keycloak-init` on every deploy**, which converges the realm allowlist in [Realm drift](#realm-drift-what-reaches-an-existing-host-and-what-never-does);» se borra, y «(`migrate`, `postgres-grants`, `openbao-init`, `keycloak-init`)» → «(`migrate`, `postgres-grants`, `openbao-init`)».
    - Detrás de esa lista (antes de «The manual sequence stays below, …»), con su fecha propia (revisión 1, M10):

```markdown
Since 2026-11-15 (own authentication phase 4) it also:

- **removes the old identity provider's two containers, `keycloak` and
  `keycloak-init`, by name**, once the new app tier is up: compose leaves a
  container whose service left the file running, and Traefik would go on
  routing to it. Nothing else in the project is touched;
- **keeps the old identity provider's database in the backup it takes before
  an upgrade, and only there** (`ITS_BACKUP_OLD_IDENTITY_DB=1`); the nightly
  backups leave it out;
- **names the old identity provider's `.env` keys and says to keep them**
  until [Retiring the old identity provider's
  data](#retiring-the-old-identity-providers-data).
```

    - En la secuencia a mano (`:3274-3280`), el paso `# 5b. Re-run the identity init job. …` y su `docker compose up keycloak-init` se borran; y detrás del `docker compose up -d` del paso 6:

```sh
# 6b. The old identity provider's two containers go, by name: compose leaves
#     them running and Traefik routes to them. Only on the upgrade past own
#     authentication phase 4, and a no-op after it; nothing else is touched.
docker compose ps -a --format '{{.Name}} {{.Service}}' \
  | awk '$2 == "keycloak" || $2 == "keycloak-init" { print $1 }' | xargs -r docker rm -f
```

19. **«What remains unverified» (`:3336-3338`).** La viñeta «- Restore of the `keycloak` database specifically. …» se sustituye por «- A rollback past own authentication phase 4 on a live host. CI's `deploy` job restores a pre-upgrade backup that holds `keycloak.dump` and checks the database came back; nothing has started the release before phase 4 on it since.».

20. **El simulacro (`:3412-3423`).** «It proves both dumps restore, the RLS policy count …, the audit-chain linkage, and that Keycloak serves the realm from the restored database.» → «It proves the dumps restore, the RLS policy count …, the audit-chain linkage, and, for a backup that holds `keycloak.dump` (one taken before own authentication phase 4, or by `deploy.sh` before an upgrade), that the restored `keycloak` database holds tables. It does not prove the way back past phase 4: the nightly backups it drills leave that database out, and that way back is only the backup taken before the phase-4 upgrade ([Rolling back a deployment](#rolling-back-a-deployment)).»; y «(`--keep` leaves it up to inspect; `--skip-keycloak` saves memory).» → «(`--keep` leaves it up to inspect).».

21. **`## Rolling back a deployment`** (revisión 1, M12; K1). Detrás del párrafo «**`IT_SUITE_VERSION` rolls back the images, not the configuration.** … check those out at the old SHA too.»:

```markdown
**Back past own authentication phase 4 (2026-11-15), the backup in step 3 is
the one taken before that upgrade: the newest `~/backups/pre-upgrade-*` whose
`alembic_version` is below `0248`** (`cat ~/backups/pre-upgrade-*/alembic_version`).
A backup taken after it cannot cross its migration backwards, and holding
a dump of the old identity provider's database does not make one the right
one: every pre-upgrade backup taken while that database exists holds one. If
that upgrade stopped part-way and a second `deploy.sh` finished it, the right
one is the first run's: it holds no such dump, and needs none, because the
database stayed on the cluster and `restore.sh` touches only the databases a
backup holds; the second run names it, and its images, at its start and in
its closing rollback help. Both halves of the paragraph above apply, too: check out the whole `deploy/` directory at the old release, not
only `docker-compose.yml` -- that release bind-mounts files this checkout no
longer has, and `deploy.sh` prints the exact `git checkout` at the end of the
upgrade -- and `.env` must still hold the four keys that release starts
with, which [Retiring the old identity provider's
data](#retiring-the-old-identity-providers-data) lists. Until you take that
step, they are there.
```

22. **La sección nueva**, detrás de `## Rolling back a deployment` (antes del `## ` siguiente), el paso único de las decisiones del operador K1 y K2 (2026-11-15):

````markdown
## Retiring the old identity provider's data

**Only on a host that ran a release from before own authentication phase 4
(2026-11-15).** A fresh install never had any of this.

Phase 4 removed Keycloak from the stack, and `deploy.sh` removed its two
containers on the upgrade. What a host that ran it still keeps, on purpose:

- the `keycloak` database in the stack's Postgres -- the old realm, with its
  users' password hashes, its client secrets and the keys that signed its
  tokens;
- three files on the `its-secrets` volume: `keycloak-admin.secret`,
  `keycloak-events.secret` and `keycloak-bootstrap-admin.secret`;
- four `.env` keys the release before phase 4 needs to start -- its compose
  file does not parse without `KEYCLOAK_ADMIN_USER` and
  `KEYCLOAK_ADMIN_PASSWORD`, and its api, gateway, worker and migrate do not
  start without `ITS_OIDC_ISSUER` and `ITS_OIDC_JWKS_URL` -- and the other
  keys of the old identity provider, which nothing reads at all;
- the image `quay.io/keycloak/keycloak:26.2`.

Nothing in this release reads any of them. **The way back past phase 4 is the
backup taken before that upgrade** -- the newest `~/backups/pre-upgrade-*`
whose `alembic_version` is below `0248` -- restored as [Rolling back a
deployment](#rolling-back-a-deployment) says, with the four keys still in
`.env`, the database and the secret files still here, and `deploy/` checked
out at the old release. **Do not delete the keys, the secret files or the
database before the step below.**

**The step, at a time you choose, once you have checked the upgrade. It
closes the way back past phase 4:** after it, a release from before phase 4
no longer starts on this host (part 6 says what going back would then take). A
migration could have done it (Alembic can run a statement outside its
transaction), but it is irreversible, so it is your act and not `up -d`'s.
In `deploy/`:

1. **A final copy of the database, if you want one.** It goes to
   `~/backups/keycloak-final.dump`, beside the pre-upgrade backups and apart
   from the nightly ones:

   ```sh
   umask 077 && mkdir -p ~/backups
   docker compose exec -T postgres pg_dump -Fc -U its_admin keycloak > ~/backups/keycloak-final.dump
   test -s ~/backups/keycloak-final.dump && echo "dump ok"
   ```

   It holds password hashes and the realm's signing keys: keep it encrypted,
   like `globals.sql`, or skip this part.
2. **The database, and the image.** No container of the old identity
   provider may still be connected (`deploy.sh` removed them; the first
   command removes any left):

   ```sh
   docker compose ps -a --format '{{.Name}} {{.Service}}' \
     | awk '$2 == "keycloak" || $2 == "keycloak-init" { print $1 }' | xargs -r docker rm -f
   docker compose exec -T postgres dropdb -U its_admin keycloak
   docker compose exec -T postgres psql -X -A -t -U its_admin -d postgres \
     -c "SELECT count(*) FROM pg_database WHERE datname = 'keycloak'"
   docker image rm quay.io/keycloak/keycloak:26.2
   ```

   The query prints `0`.
3. **The three secret files:**

   ```sh
   docker compose run --rm --no-deps --label traefik.enable=false --entrypoint rm api -f \
     /var/lib/its/secrets/keycloak-admin.secret \
     /var/lib/its/secrets/keycloak-events.secret \
     /var/lib/its/secrets/keycloak-bootstrap-admin.secret
   docker compose run --rm --no-deps --label traefik.enable=false --entrypoint ls api /var/lib/its/secrets
   ```

   The listing shows no `keycloak-` file.
4. **The four keys a rollback needed:**

   ```sh
   sed -i -E '/^(KEYCLOAK_ADMIN_USER|KEYCLOAK_ADMIN_PASSWORD|ITS_OIDC_ISSUER|ITS_OIDC_JWKS_URL)=/d' .env
   grep -cE '^(KEYCLOAK_ADMIN_USER|KEYCLOAK_ADMIN_PASSWORD|ITS_OIDC_ISSUER|ITS_OIDC_JWKS_URL)=' .env
   ```

   `grep -c` prints `0`.
5. **The other keys of the old identity provider:**

   ```sh
   sed -i -E '/^(ITS_OIDC_[A-Z_]+|ITS_MULTI_TENANT|ENTRA_TENANT_ID|ENTRA_CLIENT_ID|ENTRA_CLIENT_SECRET|ITS_KEYCLOAK_[A-Z_]+)=/d' .env
   grep -cE '^(KEYCLOAK_ADMIN_(USER|PASSWORD)|ITS_OIDC_[A-Z_]+|ITS_MULTI_TENANT|ENTRA_(TENANT_ID|CLIENT_ID|CLIENT_SECRET)|ITS_KEYCLOAK_[A-Z_]+)=' .env
   ```

   `grep -c` prints `0`, and the next `./scripts/deploy.sh` names no key.
   `ITS_DEFAULT_ORG_SLUG` stays. The containers that read `.env` see the
   change the next time they are recreated; nothing in them read those keys.
6. **This closed the way back.** From here, a release from before own
   authentication phase 4 does not start on this host: its compose file and
   its api refuse without the keys, and its identity provider has neither
   its database nor its secrets nor its image. Going back would mean
   restoring the backup from before the upgrade (`itsuite`; and `keycloak`
   only if that backup holds `keycloak.dump`, which the one a stopped
   upgrade took does not -- the final copy of part 1 does) and putting back
   by hand the four keys, the three secret files and the image.
7. **If a client secret was ever created in Entra for the old identity
   provider's Microsoft sign-in** (`ENTRA_CLIENT_SECRET`), revoke it there:
   in the Microsoft Entra admin center, **App registrations** → that
   application → **Certificates & secrets**. Deleting the line in part 5
   does not stop the secret working; nothing in this platform reads it any
   more.

Copies made before the step keep what it removed: the nightly and off-site
backups taken before the phase-4 upgrade hold `keycloak.dump`, and a
hand-made copy of `its-secrets` holds the three files, until they age out
under your retention -- an off-site bucket with Object Lock or a lifecycle
rule may not let you delete them sooner. The next `./scripts/backup.sh`
writes no `keycloak.dump` and prints no note about the database.
````

23. **`## Rolling back a deployment`, el paso 3** (re-revisión, m9). En el comentario, «#    ITS_RESTORE_FORCE=1 IS REQUIRED HERE AND IT DROPS BOTH DATABASES.» → «#    ITS_RESTORE_FORCE=1 IS REQUIRED HERE AND IT DROPS EVERY DATABASE THE BACKUP» y una línea más, «#    HOLDS: `itsuite`, and the old identity provider's if it is in the backup.» (lo que dice `deploy.sh` desde la Task 6).

24. **«Upgrading an existing deployment», la secuencia a mano, el paso 1** (re-revisión, m10). La línea «cd deploy && ./scripts/backup.sh ~/backups/pre-upgrade-$(date -u +%FT%H%MZ) && cd ..» pasa a «cd deploy && ITS_BACKUP_OLD_IDENTITY_DB=1 ./scripts/backup.sh ~/backups/pre-upgrade-$(date -u +%FT%H%MZ) && cd ..», con esta línea de comentario delante: «#    ITS_BACKUP_OLD_IDENTITY_DB=1: the backup before an upgrade keeps the old identity provider's database while one exists, as deploy.sh's does (a backup.sh from before own authentication phase 4 ignores it, and dumped it anyway).».

25. **«What was broken, and how you would recognise it».** Debajo del párrafo de introducción, antes de la tabla, una línea: «Rows 8, 9, 13 and 14 describe the identity provider this platform used until own authentication phase 4 (2026-11-15); they are kept as the record of what went wrong, and none of them can happen on a release after it.» La tabla no se toca.

- [ ] **Step 4: `deploy/GO-LIVE.md`**

1. **§1 (`:30`).** La fila `| `KEYCLOAK_ADMIN_PASSWORD` | … |` se borra.
2. **§2 (`:59-208`).** El título y toda la sección se sustituyen por (P4-28):

```markdown
## 2. The first administrator

There is no identity provider to secure any more (own authentication phase 4,
2026-11-15): the platform holds the accounts itself. What used to be this
section -- the bootstrap admin of a separate identity service, its admin
console, its realm -- is gone with it.

- **Claim the installation promptly** (`deploy/README.md` step 8). Until it is
  claimed, the setup screen answers anybody who has the install code, and the
  code is in the api's log and at `/var/lib/its/secrets/setup-code`. Once it
  is claimed, the screen is gone.
- **Enrol the second factor at the first sign-in, and keep the ten recovery
  codes** where you keep the OpenBao shares (step 9). The recovery link
  (`deploy/README.md`, "Recovery link") is the way back for an administrator
  who loses every factor; it is run on this host, never over HTTP.
- **Give at least two people `org-admin`**, each with their own account, so
  that one lost factor never locks the organization out.
- **`ITS_PUBLIC_CONSOLE_URL`** is the address people open the console at,
  exactly (`https://host[:port]`). Every sign-in write is refused without it.
- **On a host upgraded from before phase 4**, the old identity provider's
  database, three secret files and four `.env` keys are still there, and the
  way back past phase 4 needs them, until you retire them (`deploy/README.md`,
  "Retiring the old identity provider's data").
```

3. **§6 (`:432-436`).** «That writes **three** artefacts, and the third is the one a hand-rolled `pg_dump` misses:» → «That writes the artefacts below, and the last is the one a hand-rolled `pg_dump` misses:»; y la viñeta «- `keycloak.dump` — your realm, users and federation config. Losing it locks every operator out even with an intact application database.» se sustituye por «- `keycloak.dump` — only in the backup `deploy.sh` takes before an upgrade, on a host upgraded from before own authentication phase 4 and while the old identity provider's database exists. The nightly backups leave it out. What takes you back to a release from before phase 4 is the backup taken before that upgrade, whose `alembic_version` is below `0248`, with `keycloak.dump` or without it (`deploy/README.md`, "Rolling back a deployment").».
4. **§6 (`:499-500`).** «**The `keycloak` restore has not been verified beyond running** — no realm, user or federation check has been done after one.» → «A `keycloak.dump` (a backup from before own authentication phase 4) is restored and checked for tables by the monthly drill, and nothing past that.».
5. **§7 (`:513-519`).** La viñeta de `ITS_MULTI_TENANT` se borra; en «**Pin every image.** `openbao`, `minio`, `traefik`, `postgres`, `keycloak` and `redis`» se quita `` `keycloak` ``; y en «**Firewall.** … Postgres, Redis, MinIO, OpenBao and Keycloak's HTTP port have no published ports» → «Postgres, Redis, MinIO and OpenBao have no published ports».

`tests/test_deploy_runbook_secrets.py` cuenta una línea con `[removed]` en `GO-LIVE.md` (`:353`, §5): no se toca.

- [ ] **Step 5: `docs/07-backup-restore.md`**

1. `:21` — «> OpenBao and Keycloak sections were **not** executed» → «> The OpenBao section was **not** executed» (y el resto de la frase igual).
2. `:33` — la fila 2 de la tabla queda `| 2 | The old identity provider's database (`keycloak`), only on a host upgraded from before own authentication phase 4 | same volume, same cluster | the realm that release used; only the backup `deploy.sh` takes before an upgrade holds it, and nothing reads it after phase 4 (`deploy/README.md`, "Retiring the old identity provider's data") |`.
3. `:39` — «DB passwords, OpenBao token, MinIO credentials, OIDC config» → «DB passwords, OpenBao token, MinIO credentials, the console address».
4. `:65` — la línea `pg_dump … -d keycloak …` queda, con un comentario encima: `# Only for the way back past own authentication phase 4, while the keycloak database exists (deploy.sh's pre-upgrade backup does this itself):`.
5. `:155-162` — los pasos 2 y 3 del bloque quedan:

```sh
# 2. Fresh Postgres. The entrypoint runs init-app-user.sh, which creates
#    app_user/portal_user with the .env passwords and installs the ALTER
#    DEFAULT PRIVILEGES baseline. (Since own authentication phase 4 it no
#    longer creates a keycloak database.)
docker compose up -d --wait postgres

# 3. Restore the databases as its_admin.
docker compose exec -T postgres pg_restore -U its_admin -d itsuite  --clean --if-exists < "$BK/itsuite.dump"
# Only when the backup carries keycloak.dump (taken before own authentication
# phase 4, or by deploy.sh before an upgrade on a host that still keeps it):
docker compose exec -T postgres createdb -U its_admin keycloak
docker compose exec -T postgres pg_restore -U its_admin -d keycloak --clean --if-exists < "$BK/keycloak.dump"
```
6. `:387-389` — «plus a Keycloak boot on the restored `keycloak` database — the realm check this document could not run before.» → «plus, for a backup that holds `keycloak.dump` (taken before own authentication phase 4, or by `deploy.sh` before an upgrade), a check that the restored `keycloak` database holds tables.»

- [ ] **Step 6: La guarda y las pruebas de los documentos pasan**

Run: `.venv/bin/pytest -q tests/test_runbook_without_keycloak.py tests/test_email_domain_runbook.py tests/test_deploy_runbook_secrets.py tests/test_deploy_env_documentation.py tests/test_deploy_object_storage_route.py tests/test_worker_beat_schedule.py tests/test_storage_public_endpoint.py tests/test_deploy_script_steps.py`
Expected: PASS (93 en el ensayo de la segunda ronda de correcciones). Si `test_the_runbook_names_the_old_provider_only_where_it_retires_it` lista una línea que este paso no previó, se reescribe la línea (o se fecha con «phase 4» si es historia); la guarda no se afloja.

- [ ] **Step 7: Commit**

```bash
git add deploy/README.md deploy/GO-LIVE.md docs/07-backup-restore.md backend/tests/test_runbook_without_keycloak.py
git commit -m "docs(deploy): the runbook without Keycloak, and how to retire its database (phase 4)"
```

---


### Task 10: Los demás documentos

**Files:**
- Modify: `docs/01-architecture.md:17`, `:56-57`, `:169`, `:226`; `docs/02-tech-stack.md:68`, `:118`; `docs/04-data-model.md:62-65`; `docs/06-roadmap.md:30-31`; `docs/08-observability.md:151-153`, `:206-208`; `docs/product.md:180`, `:199`, `:250-253`; `docs/modules/ai-admin.md:106`; `DESIGN.md:246-248`; `HANDOFF.md:401`, `:444`
- Modify (nota fechada): `docs/kernel/spec.md` (detrás de `:3`), `docs/modules/console-auth/spec.md:14-19`, `:110`; `docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md:4`; `docs/superpowers/specs/2026-10-09-mcp-server-design.md:72-74`; `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §8 (`:521-523`)
- Create: `backend/tests/test_docs_without_keycloak.py`

**Interfaces:** ninguna. Esta tarea no toca `deploy/README.md`, `deploy/GO-LIVE.md` ni `docs/07-backup-restore.md` (Task 9).

**Qué es historia y qué no.** Los documentos vivos (`docs/0x-*.md`, `product.md`, `DESIGN.md`, `HANDOFF.md`, `docs/modules/ai-admin.md`) describen el producto de hoy: se reescriben. Las especificaciones y los planes fechados describen lo que se decidió o se construyó entonces: no se editan, salvo una nota fechada donde aún dicen «until phase 4» o la usan como su presente (Apéndice D, última fila: los demás se quedan como están).

- [ ] **Step 1: Preparar la worktree** (como la Task 9, Step 1).

- [ ] **Step 2: La guarda, que falla**

Crear `backend/tests/test_docs_without_keycloak.py` (un archivo propio, no una prueba más en el de la Task 9: las dos tareas corren en paralelo):

```python
"""The documents that describe the product as it is name Keycloak only as
history (own authentication phase 4, plan 2026-11-15, Task 10).

Specs and plans are dated records and may describe what was decided then;
the runbooks are `test_runbook_without_keycloak.py`'s. A line here may name
the old identity provider only if it says when that stopped being true:
"phase 4" on it, on one of the three lines before, or on the next.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NAMES = re.compile(r"keycloak", re.IGNORECASE)
LIVING = (
    REPO / "docs" / "01-architecture.md",
    REPO / "docs" / "02-tech-stack.md",
    REPO / "docs" / "04-data-model.md",
    REPO / "docs" / "06-roadmap.md",
    REPO / "docs" / "08-observability.md",
    REPO / "docs" / "product.md",
    REPO / "docs" / "modules" / "ai-admin.md",
    REPO / "DESIGN.md",
    REPO / "HANDOFF.md",
)


def _undated(text: str, name: str = "doc") -> list[str]:
    lines = text.splitlines()
    return [
        f"{name}:{index + 1}: {line.strip()[:100]}"
        for index, line in enumerate(lines)
        if NAMES.search(line)
        and not any("phase 4" in near for near in lines[max(0, index - 3) : index + 2])
    ]


def test_the_rule_sees_an_undated_name() -> None:
    assert _undated("Auth -- Keycloak as identity broker.") != []
    assert _undated("Keycloak was removed in own\nauthentication phase 4.") == []


def test_the_living_documents_name_keycloak_only_as_history() -> None:
    assert [line for path in LIVING for line in _undated(path.read_text(), path.name)] == []
```

Run: `.venv/bin/pytest -q tests/test_docs_without_keycloak.py`
Expected: 1 FAIL (19 líneas de los nueve documentos en el ensayo, la primera `01-architecture.md:17: │ … ├─▶ Auth (Keycloak)     │`) y 1 PASS.

- [ ] **Step 3: Los documentos vivos**

Las frases citadas entre «» se buscan sin atender a los saltos de línea: en `06-roadmap.md`, las dos de `08-observability.md`, `product.md:250`, `DESIGN.md` y `HANDOFF.md:401` están partidas en dos o tres líneas (y en el Step 4, `console-auth/spec.md:18`). Lo que las sustituye se escribe con el ancho de línea del archivo.

1. `docs/01-architecture.md:17` (el diagrama; mismo ancho): `├─▶ Auth (Keycloak)     │` → `├─▶ Auth (own + OAuth)  │`.
2. `docs/01-architecture.md:56-57`:

```markdown
- **Auth** — the platform's own sign-in: a password and a second factor (TOTP
  or a passkey), Microsoft as an option, and a session cookie; and its own
  OAuth 2.1 server for AI apps over MCP
  (`docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md`).
  Keycloak, the identity broker of the first design, was removed in own
  authentication phase 4 (2026-11-15).
```

3. `docs/01-architecture.md:169`: `                         [ keycloak ] [ postgres ] [ redis ]` → `                         [ openbao ] [ postgres ] [ redis ]`.
4. `docs/01-architecture.md:226`: «object storage, Keycloak (or a managed OIDC IdP).» → «object storage; sign-in and the OAuth server are the platform's own (a managed OIDC IdP was the plan until own authentication phase 4, 2026-11-15).».
5. `docs/02-tech-stack.md:68`: la fila queda `| Identity | **The platform's own sign-in and OAuth 2.1 server** | Password + TOTP or passkey, Microsoft sign-in, sessions, and the OAuth server AI apps connect through (own authentication design 2026-11-04). Keycloak filled this row until own authentication phase 4 (2026-11-15). |`.
6. `docs/02-tech-stack.md:118`: `| AD / Entra ID | Sign in with Microsoft (multi-tenant application, the platform's own certificate); no SAML or on-premises AD federation (own authentication phase 2; Keycloak's federation went in phase 4) |`.
7. `docs/04-data-model.md:62-65`:

```markdown
An operator identity, held by the platform itself since own authentication
phase 1 (password verifier, second factors and sessions in their own tables).

- `id`, `org_id`, `external_subject` (until own authentication phase 1 the
  subject Keycloak issued -- Keycloak went in phase 4, and rows from before
  phase 1 may still hold one; now an opaque surrogate key that no door
  reads), `email`,
```

8. `docs/06-roadmap.md:30-31`: `- **Keycloak** integration: SSO (OIDC/SAML, AD/Entra federation), MFA, and RBAC with baseline roles.` → `- **Identity**: SSO, MFA and RBAC with baseline roles -- delivered on Keycloak, which own authentication replaced in phases 1 to 3 and removed in phase 4 (2026-11-15).` (una sola línea: la guarda pide «phase 4» en ella o junto a ella).
9. `docs/08-observability.md:151-153`: «(the internal CA, the audit-chain HMAC key, the Keycloak realm)» → «(the internal CA, the audit-chain HMAC key)»; y `:206-208`: «The Keycloak realm restore is now checked by the monthly drill (Keycloak boots on the restored database and serves the realm); a sign-in against a restored realm has not been.» → «A backup that holds `keycloak.dump` (one from before own authentication phase 4, or the one `deploy.sh` takes before an upgrade) is restored by the drill, which checks it holds tables; nothing reads it after.».
10. `docs/product.md:180`: «- The Keycloak realm restore beyond "the command runs".» se borra. `:199`: «Redis, MinIO, Keycloak for identity, OpenBao for secrets, Traefik in front.» → «Redis, MinIO, OpenBao for secrets and keys, Traefik in front; the platform signs people in itself.». `:250-253`: «imports, type names and comments are exempt, because the platform does integrate with Keycloak and source that pretended otherwise would be lying about its own dependency.» → «imports, type names and comments are exempt, because the platform did integrate with Keycloak until own authentication phase 4, and source that pretended otherwise would have been lying about its own history.».
11. `docs/modules/ai-admin.md:106`: `- **RBAC / Keycloak** — sessions bound to the admin's real permissions.` → `- **RBAC and the platform's own sign-in** — sessions bound to the admin's real permissions.`
12. `DESIGN.md:246-248`: «the sign-in page lives in the Keycloak theme, outside this bundle,» → «the sign-in pages were Keycloak's theme, outside this bundle, until own authentication phase 1 (Keycloak itself went in phase 4); they are the console's own now,».
13. `HANDOFF.md:401`: «the Keycloak console in a browser, AD/Entra federation,» → «Sign in with Microsoft against a live tenant,»; `:444`: `| **Live-host upgrade, rollback, Keycloak realm restore** |` → `| **Live-host upgrade, rollback, and restoring a backup from before own authentication phase 4** |`.

- [ ] **Step 4: Las notas fechadas**

1. `docs/kernel/spec.md`, detrás de la línea `Status: …` (`:3`):

```markdown
> **2026-11-15 (own authentication phase 4).** The passages on operator
> authentication through Keycloak -- the scope list, the out-of-scope list,
> the authentication section, the console's login and the compose services --
> describe Phase 0 as it was built. The platform signs people in itself since
> own authentication phase 1, and Keycloak, its realm import and
> `deploy/keycloak/` were removed in phase 4
> (`docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md`).
```

2. `docs/modules/console-auth/spec.md`: detrás de «… it signs nobody in, and phase 4 removes it with Keycloak.» (`:18-19`) y de «… the realm is removed in phase 4.» (`:110`), la frase «Removed on 2026-11-15 (own authentication phase 4).».
3. `docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md:4`: al final de la línea `**Status:**`, «; Keycloak removed from the stack on 2026-11-15 (own authentication phase 4)».
4. `docs/superpowers/specs/2026-10-09-mcp-server-design.md:72-74`: detrás de «kept as the record until phase 4 removes Keycloak.», «(It did, on 2026-11-15: plan `docs/superpowers/plans/2026-11-15-autenticacion-propia-fase4.md`.)».
5. `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §8, al final de la viñeta 4 («… reescrita contra el servidor propio.»): « *Plan: `docs/superpowers/plans/2026-11-15-autenticacion-propia-fase4.md` (2026-11-15). La base `keycloak`, los secretos `keycloak-*` y las claves `.env` del proveedor de identidad de antes los retira el operador en un paso del runbook, cuando decide cerrar la vuelta atrás (decisiones del operador K1 y K2 de ese plan, 2026-11-15); nada del repositorio los crea ni los lee, y de las copias de seguridad solo la previa a la actualización guarda la base.*»

- [ ] **Step 5: Las pruebas pasan y commit**

Run: `.venv/bin/pytest -q tests/test_docs_without_keycloak.py`
Expected: PASS.

```bash
git add docs DESIGN.md HANDOFF.md backend/tests/test_docs_without_keycloak.py
git commit -m "docs: the architecture, stack, data model and product docs without Keycloak (phase 4)"
```

---

### Task 11: Cierre — falsificadores 10 y 11, recuentos, `globalRoutes.json`, el informe

**Files:**
- Create: `backend/tests/test_auth_phase4_falsifiers.py`
- Modify: `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §10 (un falsificador 11, fechado)

**Interfaces:**
- Depende de: todas las tareas, en el árbol fusionado.
- Produce: el informe de la fase al coordinador.

- [ ] **Step 1: Preparar el árbol fusionado**

Worktree nueva desde la rama del coordinador con las Tasks 1–10 fusionadas (Task 1, Step 1). Comprobar: `ls backend/app/core/security.py deploy/keycloak` → «No such file» las dos; `grep -c oauth_used_refresh_tokens backend/app/core/auth/retention.py` > 0.

- [ ] **Step 2: El falsificador 11**

Crear `backend/tests/test_auth_phase4_falsifiers.py`:

```python
"""Own authentication phase 4: design §10's falsifier 11 (added 2026-11-15 by
the phase 4 plan, Task 11) -- no Keycloak token, image or service is reachable
or accepted anywhere.

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
flag it. `/mcp` must refuse the token as an invalid token. The console's
socket refuses it too, in `tests/test_token_origin.py`.

THE DEPLOYMENT HALF READS WHAT SHIPS. No setting, no compose service, image
or router, no `deploy/keycloak/`, no console code that asks an identity
provider to sign it out, and no token verification in `app/` but the
platform's own three (its access tokens, its loopback tokens, Microsoft's ID
tokens). The `deploy` CI job asserts the same of the running stack
(`deploy/ci/mcp_flow.py`, and its two `grep -qi keycloak` checks).

Falsifier 10 -- no screen says "Keycloak" -- is the two guards that already
hold it: `frontend/src/lib/noVendorNames.test.ts` and
`backend/tests/test_no_vendor_names_backend.py`.
"""

import ast
import io
import json
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
from tests.traefik_routes import COMPOSE, routers

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "backend" / "app"
CONSOLE_SRC = REPO / "frontend" / "src"
DRILL = REPO / "deploy" / "drill" / "docker-compose.drill.yml"

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
            return {"issuer": ISSUER, "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs"}
        return self.jwks

    def handler(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=self.document(str(request.url)))


def offer_the_key(monkeypatch: pytest.MonkeyPatch, issuer: ForeignIssuer) -> OfferedKey:
    """From now until the test ends, every httpx client but an in-process
    one, and every urllib opener, gets `issuer`'s JWKS for any URL."""
    offered = OfferedKey(issuer)
    in_process = (httpx.ASGITransport, httpx.MockTransport)

    def async_client(*args: Any, **kw: Any) -> httpx.AsyncClient:
        if not isinstance(kw.get("transport"), in_process):
            kw["transport"] = httpx.MockTransport(offered.handler)
        return _REAL_ASYNC_CLIENT(*args, **kw)

    def client(*args: Any, **kw: Any) -> httpx.Client:
        if not isinstance(kw.get("transport"), httpx.MockTransport):
            kw["transport"] = httpx.MockTransport(offered.handler)
        return _REAL_CLIENT(*args, **kw)

    def opener(self: urllib.request.OpenerDirector, fullurl: Any, *a: Any, **kw: Any) -> Any:
        url = fullurl if isinstance(fullurl, str) else fullurl.full_url
        if url.startswith(("http://", "https://")):
            return io.BytesIO(json.dumps(offered.document(url)).encode())
        return _REAL_OPEN(self, fullurl, *a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    monkeypatch.setattr(httpx, "Client", client)
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


def _a_door_that_still_trusts_the_realm() -> FastAPI:
    """The positive control: what `app/core/security.py` was, in two lines
    of each kind -- a JWKS fetched over httpx, and PyJWT's own client over
    urllib -- behind one route each."""
    door = FastAPI()

    def _bearer(request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        return header.removeprefix("Bearer ") if header.startswith("Bearer ") else None

    @door.get("/api/v1/control/httpx")
    async def over_httpx(request: Request) -> JSONResponse:
        token = _bearer(request)
        if token is None:
            return JSONResponse({"detail": "no token"}, status_code=401)
        async with httpx.AsyncClient() as fetch:
            keys = (await fetch.get(f"{ISSUER}/protocol/openid-connect/certs")).json()["keys"]
        try:
            claims = jwt.decode(
                token, jwt.PyJWK(keys[0]).key, algorithms=["RS256"], audience="it-suite"
            )
        except jwt.PyJWTError:
            return JSONResponse({"detail": "bad token"}, status_code=401)
        return JSONResponse({"sub": claims["sub"]})

    @door.get("/api/v1/control/urllib")
    async def over_urllib(request: Request) -> JSONResponse:
        token = _bearer(request)
        if token is None:
            return JSONResponse({"detail": "no token"}, status_code=401)
        key = jwt.PyJWKClient(f"{ISSUER}/protocol/openid-connect/certs").get_signing_key_from_jwt(
            token
        )
        try:
            claims = jwt.decode(token, key.key, algorithms=["RS256"], audience="it-suite")
        except jwt.PyJWTError:
            return JSONResponse({"detail": "bad token"}, status_code=401)
        return JSONResponse({"sub": claims["sub"]})

    return door


async def test_the_walk_sees_a_door_that_trusts_the_realm(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-vacuity of the walk below: with the key on offer, a door that still
    validates a Keycloak token answers it differently from no credential --
    and so does the comparison the walk uses. A door that refused an unknown
    key with a 401 would be seen here too: the key is offered."""
    offered = offer_the_key(monkeypatch, api_env.foreign)
    token = str(api_env.foreign.token("someone"))
    transport = httpx.ASGITransport(app=_a_door_that_still_trusts_the_realm())
    async with httpx.AsyncClient(transport=transport, base_url="http://door") as client:
        found = await _differs(
            client,
            [("GET", "/api/v1/control/httpx"), ("GET", "/api/v1/control/urllib")],
            {"Authorization": f"Bearer {token}"},
        )
    assert found == [
        ("GET", "/api/v1/control/httpx", 401, 200),
        ("GET", "/api/v1/control/urllib", 401, 200),
    ]
    assert len(offered.seen) == 2, offered.seen


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
    walked = [
        (m, p) for m, p, _r in _routes(api_env.app) if not p.startswith(EXCEPT_PREFIXES)
    ]
    assert len(walked) > 300, "the walk found too few routes to mean anything"
    differs = await _differs(api_env.client, walked, {"Authorization": f"Bearer {token}"})
    assert differs == []
    # And nothing even asked for the key.
    assert [url for url in offered.seen if "realms" in url or "certs" in url] == []


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


def test_falsifier_11_no_setting_of_the_old_provider_is_left() -> None:
    fields = set(Settings.model_fields)
    left = {f for f in fields if f.startswith(("oidc_", "keycloak_")) or f == "multi_tenant"}
    assert left == set(), sorted(left)


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


def test_falsifier_11_the_console_asks_no_identity_provider_anything() -> None:
    """No console code that is not a test builds an OpenID sign-out, a
    `/realms/` URL or a JWKS fetch. (`lib/oidcLeftovers.ts` only deletes
    what the Keycloak-era console left in the browser's storage, P4-16.)"""
    pattern = re.compile(r"post_logout_redirect_uri|signoutRedirect|end_session_endpoint|/realms/")
    offenders = [
        str(path.relative_to(REPO))
        for path in sorted(CONSOLE_SRC.rglob("*.ts*"))
        if ".test." not in path.name and pattern.search(path.read_text())
    ]
    assert offenders == [], offenders


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
```

Run: `.venv/bin/pytest -q -W error::RuntimeWarning tests/test_auth_phase4_falsifiers.py`
Expected: 8 PASS (en el ensayo de la ronda de correcciones, en el árbol con todas las tareas aplicadas). La prueba del recorrido compara cada ruta con y sin el token **mientras toda petición saliente recibe la JWKS de la clave que firmó el token** (por httpx y por urllib, que es lo que usa el cliente JWKS de PyJWT), así que un validador que rechace una clave desconocida con un 401 no la pasa rechazando: la clave no le es desconocida (revisión 1, M13; decisión del coordinador). Para no ser vacía, comprueba antes que una sesión real sí cambia `/api/v1/me` (401 frente a 200), y `test_the_walk_sees_a_door_that_trusts_the_realm` es el control positivo: una puerta de prueba que valida el token con la JWKS del realm, por httpx y por PyJWT, y la misma comparación la señala en sus dos rutas (401 frente a 200). El barrido de `jwt.decode` mira también `decode_complete`, los nombres importados de `jwt` y el cliente JWKS de PyJWT.

Una prueba que pasa al primer intento no prueba que vigile. Tres comprobaciones a mano, sin commit, deshaciendo cada una con `git checkout` antes de la siguiente:
- al final de `backend/app/api/v1/me.py`, una ruta con la forma del validador que se fue (una clave desconocida o inalcanzable es un 401):

```python
@router.get("/me/still-trusts-the-realm")
async def _mutant(request: Request) -> dict[str, str]:
    import httpx as _httpx
    import jwt as _jwt
    from fastapi import HTTPException as _HTTPException

    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    try:
        async with _httpx.AsyncClient() as fetch:
            url = "https://idp.test/realms/it-suite/protocol/openid-connect/certs"
            keys = (await fetch.get(url)).json()["keys"]
        _jwt.decode(token, _jwt.PyJWK(keys[0]).key, algorithms=["RS256"], audience="it-suite")
    except Exception as error:
        raise _HTTPException(status_code=401) from error
    return {"ok": "yes"}
```

  (con `Request` en el import de `fastapi` del archivo): `test_falsifier_11_a_keycloak_token_changes_nothing_at_any_route` falla con `('GET', '/api/v1/me/still-trusts-the-realm', 401, 200)`, y `test_falsifier_11_only_the_platforms_own_tokens_are_decoded` también. En el ensayo, la versión anterior de esta prueba, sin la clave ofrecida, pasaba ese mutante;
- en `backend/app/core/config.py`, añade `    oidc_issuer: str | None = None` detrás de `default_org_slug`: `test_falsifier_11_no_setting_of_the_old_provider_is_left` falla;
- en `deploy/drill/docker-compose.drill.yml`, añade un servicio `keycloak:` con `image: quay.io/keycloak/keycloak:26.2`: `test_falsifier_11_nothing_of_it_is_deployed` falla.

- [ ] **Step 3: El falsificador en el diseño**

En `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §10, detrás de «10. Ninguna pantalla dice «Keycloak» (la guarda que ya existe se mantiene).»:

```markdown
11. *(Enmienda del 2026-11-15, plan de la fase 4, Task 11.)* Ningún token, imagen ni servicio de Keycloak es
    alcanzable ni aceptado en ninguna parte: cada ruta de `/api/v1` responde igual con un token con la forma del de la
    consola de Keycloak que sin credencial, aunque toda petición saliente reciba la JWKS de la clave que lo firmó;
    `/mcp` lo rechaza; ni compose ni el simulacro definen su servicio ni su imagen; ningún router de Traefik lleva a
    él; ningún ajuste lo nombra; y en `app/` solo se verifican los tokens propios y los de Microsoft
    (`backend/tests/test_auth_phase4_falsifiers.py`, con un control positivo: una puerta que aún confía en el realm
    es señalada; en vivo, el job `deploy` de la CI).
```

Y el falsificador 10 sigue en sus dos guardas, sin cambios de regla: `frontend/src/lib/noVendorNames.test.ts` y `backend/tests/test_no_vendor_names_backend.py` (Step 4 las corre).

- [ ] **Step 4: Los recuentos y la suite entera**

Run: `cd backend && .venv/bin/pytest -q tests/test_logging_discipline.py tests/test_mcp_channel_api.py tests/test_route_gating.py tests/test_client_catalog.py tests/test_route_session_discipline.py tests/test_i18n_catalog.py tests/test_no_vendor_names_backend.py tests/test_deploy_env_documentation.py tests/test_security_headers.py`
Expected: PASS, con el recuento de llamadas de log en **109** (ninguna tarea añade ni quita una: los archivos borrados no tenían ninguna) y `NEVER_EXPOSED` en **56**; `UNGATED_BY_DESIGN`, `SECRET_ONCE` y `NO_STORE_ROUTES` sin cambios. Si un número no cuadra, se corrige el número del árbol fusionado y su párrafo, no el código, y el informe lo dice.

Run: `cd backend && .venv/bin/pytest -q -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning`
Expected: PASS, la suite entera.

Run: `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: limpio.

Run: `cd frontend && npx vitest run && npx tsc -b && npm run build`
Expected: PASS, sin errores de tipos, y el build termina.

Run: `cd backend && PYTHONPATH=$PWD .venv/bin/python -m tests.export_console_global_routes && git diff --exit-code ../frontend/src/lib/globalRoutes.json`
Expected: sin diferencias (ninguna tarea cambia `GLOBAL_WRITES` ni `GLOBAL_READS`).

Run: `grep -rIl -i keycloak --exclude-dir=node_modules --exclude-dir=.venv --exclude-dir=.claude backend/app frontend/src deploy .github`
Expected (en el ensayo de la segunda ronda de correcciones, sobre un árbol con las oleadas 1 y 2 de los guiones corregidos y las Tasks 6–9: 48 archivos): `.github/workflows/ci.yml`; en `backend/app`, las frases en pasado de la columna «Historia» del Apéndice D y los comentarios que las Tasks 4 y 5 dejaron como historia (`api/deps.py`, `api/probes.py`, `api/v1/me.py`, `api/v1/orgs.py` y `core/identity/email_domain.py` (fechados por la Task 4), `api/v1/platform.py`, `api/v1/ws.py`, `core/auth/__init__.py`, `core/auth/loopback.py`, `core/auth/oauth/access.py`, `core/auth/oauth/policy.py`, `core/config.py`, `core/identity/__init__.py`, `core/identity/own_profile.py`, `core/identity/policy.py`, `core/mail.py`, `modules/remote/tunnel.py`, `modules/user_admin/__init__.py`, `modules/user_admin/service.py`, `worker.py`); en `deploy/`, `README.md` y `GO-LIVE.md` (el paso de retirada y las líneas fechadas), `ci/mcp_flow.py` (`FORMER_KEYCLOAK_PATHS`) y `scripts/{backup,restore,restore-drill,deploy}.sh` (las líneas que conservan, restauran o quitan lo viejo, a propósito); en `frontend/src`, `lib/oidcLeftovers.ts` (P4-16, se queda), los comentarios en pasado de `main.tsx`, `lib/ws.ts`, `modules/remote/tunnel.ts`, `shell/AccountMenu.tsx` e `i18n/test-utils.tsx`, y archivos de prueba (`App.auth`, `lib/noVendorNames`, `lib/oidcLeftovers`, `lib/session`, `lib/session.navigation`, `lib/ws`, `modules/audit/auditVocabulary.coverage`, `modules/profile/ProfilePage`, `modules/profile/profile.es`, `modules/settings/__tests__/email`, `modules/settings/__tests__/users`, `modules/setup/__tests__/setup`, `shell/AppShell`, `shell/authSurface`). Ninguno fuera de esa lista (revisión 1, M14; re-revisión, m5).

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_auth_phase4_falsifiers.py docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md
git commit -m "test(auth): falsifier 11 -- no Keycloak token, image or service anywhere (phase 4)"
```

- [ ] **Step 6: El informe**

El informe de la fase al coordinador (`CLAUDE.md`, «Reporting to the operator») dice, en este orden:

1. **El camino hasta `main`.** La rama de la fase 4 se fusiona después de las de las fases 1–3 (0248 sigue a 0247), y todas llegan a `main` cuando el proyecto entero termina: el operador despliega siempre desde `main` (2026-09-11) y decidió el 2026-10-31 que nada se despliega hasta que terminen los cuatro proyectos. Esta fase no se despliega sola.
2. **Solo el delta: lo que esta fase añade a la secuencia de despliegue del proyecto 4** (decisión del operador R1, 2026-11-15; §«Lo que el despliegue tiene que saber», puntos 1–9). La secuencia completa no va en este informe:
   - `migrate` aplica **0248** en el siguiente `up -d` (borra `keycloak_event_ingest`); volver atrás es restaurar la copia previa a la actualización, la única que guarda la base `keycloak`;
   - **en un host existente, `git pull --ff-only` antes de `./scripts/deploy.sh`** (punto 8); si el `deploy.sh` viejo ya se paró a medias, se vuelve a correr el nuevo, que termina la actualización y nombra la copia de la vuelta atrás (la de antes de 0248, no la suya);
   - el `deploy.sh` nuevo quita por su nombre los contenedores `keycloak` y `keycloak-init` cuando la capa de aplicación ya está arriba (a mano, el paso 6b del runbook), y nada más del proyecto;
   - las imágenes `itsuite/backend` y `itsuite/web` cambian; `quay.io/keycloak/keycloak:26.2` deja de usarse;
   - **ninguna clave `.env` nueva ni ninguna ruta nueva de OpenBao**; 17 claves quedan sin lector (punto 6), cuatro de ellas las necesita la vuelta atrás, y `deploy.sh` las nombra y dice que no se borren todavía;
   - las copias nocturnas y externas dejan de llevar la base `keycloak`; la copia previa a la actualización la lleva (decisión del operador K1);
   - la base `keycloak`, los tres secretos `keycloak-*` y las cuatro claves se quedan hasta que el operador decida retirarlos, con el paso único del runbook «Retiring the old identity provider's data» (K1, K2), que cierra la vuelta atrás;
   - en un VPS nuevo nada de lo anterior aplica: `init-app-user.sh` ya no crea la base.
3. **Que la secuencia empieza por** `docker compose exec openbao bao status`: OpenBao está sellado tras cada reinicio y con él sellado `up -d` se queda esperando (`deploy/GO-LIVE.md` §4). No es esta fase la que lo sella.
4. **Qué se verificó y qué no:** el run del job `deploy` de la CI sobre el PR (las líneas `ok` de las dos fases de `deploy/ci/mcp_flow.py`), los recuentos del Step 4, las mutaciones del Step 2 y las de la Task 3, y la lista *SIN VERIFICAR* del Apéndice E, que esta fase no puede cerrar sin un host: un `deploy.sh` real sobre un host con Keycloak (la retirada de sus dos contenedores y el final de una actualización a medias, probados aquí con `docker` simulado), y una vuelta atrás de verdad a una versión de antes de la fase 4.

La secuencia completa, copiable y sin marcadores, la da el informe del proyecto 4, que es el que llega al host, como en la fase 3 (decisión del operador R1). Este informe da el delta y lo dice.

---

## Autorrevisión

**Cobertura del brief.**
- E1: servidor (Task 1: el acto, la auditoría `ai_grants_revoked`, la carrera con un canje, la herramienta MCP, el mapa de bloqueos y el docstring del módulo, el runbook «Revoking» y el riesgo 6, la enmienda del diseño §5.3 y la de la decisión 3 de la Task 10 de la fase 3) y consola (Task 2: el botón en «Tu seguridad» y en el portal, adónde va la página, un 401 como sesión cerrada, el texto de la confirmación del administrador).
- E2: Task 3 (el paso con `org_id` explícito, FOR UPDATE SKIP LOCKED, hojas primero, clave propia, las aserciones exactas actualizadas, la consecuencia documentada en el diseño §5.3, en `docs/03-security-model.md` y en el riesgo residual «A used refresh token is remembered for 30 days»; el mapa de `guards.lock_user_row` y `oauth/store.py`).
- El retiro: el inventario con archivo:línea y destino (Apéndice D); las pruebas una a una (Apéndice C); las pruebas de rechazo conservadas con `ForeignIssuer` (Task 4); la prueba de despliegue de la CI reescrita contra el servidor propio, más el paso de la base vieja (Task 7).
- Las decisiones del operador K1, K2 y R1 (Apéndice B.1) y las del coordinador C1–C7 (B.2), cada una en la tarea que la cumple y con su prueba.
- Los hechos del despliegue (§«Lo que el despliegue tiene que saber»): contenedores, volumen, base (K1), secretos (K2), respaldo y restauración de copias viejas, claves `.env` sin lector y las cuatro de la vuelta atrás, migración, vuelta atrás, la primera actualización y el host a medias.
- Las fuentes, con sus URL y lo no verificado (Apéndice E).
- El cierre: falsificador 10 (las dos guardas), falsificador 11 (nuevo, con control positivo), recuentos (109 y 56), `globalRoutes.json`, el informe con solo el delta de despliegue (Task 11).

**Lo comprobado, en árboles de ensayo fuera del repositorio** (`scratchpad/p2f4plan/`). Al escribir el plan, el código de las Tasks 1–8 y 11 se aplicó tal como está aquí y sus pruebas pasaron. En la ronda de correcciones (2026-11-15) se volvió a correr todo bloque de código que cambió, sobre árboles nuevos, con los guiones y los comandos de este plan:
- **Task 1 y Task 3**, sobre `dd062f8`: 466 pruebas en sus archivos; `ruff` y `mypy` limpios (557 archivos).
- **Task 2**, sobre un árbol nuevo: 1.122 pruebas de vitest en los directorios del Step 7 (la única roja, `api.session.test.ts`, lee `backend/`, que ese árbol no tenía); `tsc` sin salida. El Step 3 da `TypeError: signOutEverywhere is not a function`.
- **Task 4**, sobre la oleada 1: el Step 2 rojo (5 FAIL, 19 PASS); el guion, `24` / `156` / `0` / `ok`; el `grep` del Step 4, la lista que da; el Step 5, 412; el barrido, 83 archivos, 1.325 pasan y 1 se salta; `mypy` limpio en 556 archivos; `ruff` limpio.
- **Task 5**, sola sobre la oleada 1 (ya no depende de la 4): el Step 6, 691; `mypy` limpio en 551 archivos; `ruff` limpio; vitest de `src/modules/audit`, 113. **La 4 sobre la 5**: `mypy` y `ruff` limpios, y el Step 6 de la 5 otra vez, 693.
- **Tasks 6, 7, 8, 9 y 11**, en la segunda ronda de correcciones, sobre un árbol con las oleadas 1 y 2 hechas con los guiones corregidos (las Tasks 1 y 3, después la 5 y después la 4: `grep -c oauth_used_refresh_tokens` da 3 y `security.py` no está) y la Task 8 encima, no sobre el árbol de antes de la primera ronda:
  - **Task 6**, en el orden nuevo: el Step 2 rojo (28 FAIL, 48 PASS); el Step 3 (`ok`, `lines still naming keycloak: []`, los 13 servicios, `0`, ningún `SYNTAX`, `--help` en `2,47p` y `2,28p`); `shellcheck` 0.10.0 sin avisos nuevos en los siete guiones [S1]; el Step 4, 197; `ruff` limpio.
  - **Task 7**, sobre la 6: el Step 2 (error al importar), el Step 3 (16 PASS, 3 FAIL), el Step 4 (la lista de pasos, dos `bash -n: 0`, `ok`), el Step 5 (19); el comando del Step 4 de la 6 con la 7, 203; y el guion en vivo contra uvicorn con TLS (Step 6), en la primera ronda y otra vez en la re-revisión: 33 líneas `ok` y salida 0 en las dos fases [L1].
  - **Task 8**: el guion (`ok`) y su comentario nuevo de `PreviewPage.tsx`; las 81 pruebas y `tsc` son del ensayo al escribir el plan (el cambio nuevo es un comentario).
  - **Task 9**: los cambios del runbook, de `GO-LIVE.md` y de `docs/07` se aplicaron con un simulador que lee cada bloque de este plan por su primera línea y comprueba que cada ancla aparece una vez (sin atender a los saltos de línea donde el plan lo dice): todas las anclas, una vez. La guarda, roja antes (6 FAIL, 3 PASS) y verde después, con las demás pruebas de documentos del Step 6 (93); `grep -n "risk [0-9]"` no nombra ningún número viejo.
- **Cada bloque «antes / después» del plan**, contra `dd062f8`: cada «antes» aparece una vez en su archivo, y los dos de `guards.py`, aplicados en orden, dejan la viñeta sin frases repetidas.
- **Task 10**: solo texto; su guarda se escribió y se vio fallar sobre los documentos de hoy al escribir el plan; la línea nueva del modelo de datos lleva «phase 4» en sí misma, que es lo que la guarda pide.
- **Task 11**: el falsificador 11, 8 PASS en ese árbol con las Tasks 6–9 encima; y la mutación que la revisión pedía: una ruta con la forma del validador viejo (una clave desconocida o inalcanzable es un 401, con `import jwt as _jwt`) la señalan las dos pruebas que el Step 2 nombra, el recorrido con `('GET', '/api/v1/me/still-trusts-the-realm', 401, 200)` y el barrido de `jwt`, que desde la segunda ronda sigue los alias de `import jwt as …`; la prueba de antes, sin la clave ofrecida, la dejaba pasar. El `grep` del Step 4: 48 archivos, los de la lista.

**Lo que la revisión pidió y aquí se hizo de otra manera, con su porqué.**
- *Revisión 3, I4* pedía dos condiciones más antes de «Already deployed»: que la etiqueta de la `api` en marcha sea la de HEAD y que `docker compose ps -a` no tenga ningún servicio fuera de `docker compose config --services`. La decisión del coordinador C2 la acota a los dos contenedores de Keycloak, y con razón contra el código: la segunda condición haría completo cada despliegue de un host con un contenedor propio del operador en el proyecto (lo que C1 protege), y el caso que la revisión describe (el `deploy.sh` viejo parado en «Identity provider») siempre deja esos dos contenedores, porque el viejo nunca llega a quitarlos.
- *Revisión 1, M11 (c)* pedía empezar el paso de retirada con `up -d --remove-orphans`. C1 lo descarta; el paso quita los dos contenedores por su nombre antes del `dropdb`, con el mismo comando que da la secuencia a mano.
- *Revisión 3, I1*, su última viñeta (que el paso 1 del README fije `ITS_PUBLIC_CONSOLE_URL`): ya lo hace, `deploy/README.md:277-292` en `dd062f8`, y la Task 9 lo conserva; el `preflight.sh` nuevo remite a ese paso.
- *Revisión 1, I4* sugería borrar las claves con un patrón (`ENTRA_(TENANT|CLIENT)_[A-Z_]+`); el paso las nombra una a una (las tres de `ENTRA_*` que existieron), para que el comando no pueda borrar una clave futura que empiece igual.

**Placeholders.** Ninguno: los únicos valores que el plan no fija son el nombre de la rama del coordinador y la base de Redis de cada worktree, que da el coordinador. Los comandos del paso de retirada y de la secuencia a mano no llevan nada que sustituir (`test_the_retiring_step_is_the_operators_decision_entire` lo comprueba en el primero).

**Coherencia de nombres.** `signOutEverywhere`, `OAUTH_USED_TOKEN_RETENTION`, `_oauth_used_refresh_tokens`, `oauth_used_refresh_tokens`, `ForeignIssuer`, `public_jwk`, `ApiEnv.foreign`, `OutboundRecorder`, `install_outbound`, `traefik_routes.winner`, `deploy/ci/mcp_flow.py`, `ITS_BACKUP_OLD_IDENTITY_DB`, `OBSOLETE_ENV_KEYS`, `obsolete_env_keys`, `old_identity_containers`, `remove_old_identity_containers`, `stop_if_already_deployed`, y la sección «Retiring the old identity provider's data»: los mismos en el Apéndice A, en las tareas que los crean, en las que los usan y en los guiones que los citan.

## Preguntas para el operador

Ninguna abierta. K1, K2 y R1 las decidió el operador el 2026-11-15 (Apéndice B.1), y el coordinador resolvió las demás el mismo día (Apéndice B.2, C1–C7).
