# Despliegue en un VPS nuevo y cambio de marca a Novieri RMM — plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que un VPS recién creado con Ubuntu 24.04 sirva `https://rmm.novieri.com` con un certificado de Let's Encrypt que Traefik pide y renueva solo (TLS-ALPN-01 en el 443), que todo lo que una persona o el equipo de un cliente ve diga «Novieri RMM» (§1.1 del diseño) sin tocar nada de §1.2, que el primer arranque se haga con comandos copiables y sin marcadores, y que la CI pruebe en cada subida el mismo camino: el certificado por ACME contra Pebble y el agente Go compilado inscribiéndose por el 8443 y ejecutando un trabajo firmado.

**Architecture:** Cuatro oleadas de código y una de cierre. La primera hace, en paralelo, los nombres visibles del backend, el certificado por ACME con su respuesta 503 cuando `api` no está, el `.env` generado y la comprobación previa, los pendientes de comportamiento, la espera por condición de la prueba Go y la CI contra Pebble (estas tres últimas piezas del despliegue se fusionan juntas). La segunda renombra el agente a la vez en sus dos lados —el binario Go, sus instaladores y el control plane que los publica— y arregla la infraestructura de pruebas. La tercera reescribe el runbook para un VPS nuevo y añade a la CI el agente compilado de extremo a extremo. La cuarta fija los nombres con una guarda y ensaya el primer arranque entero aquí, con Docker y Pebble. La quinta es el informe.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic (ninguna migración nueva), PostgreSQL 16, Redis, pytest 9.2.0 con pytest-asyncio 1.5.0. Go 1.24 (agente). Bash. Docker Engine 29.9.3 y Compose v5.7.1 en esta sesión; Traefik v3.6 (la imagen `traefik:v3.6` es la 3.6.25 el 2026-11-23), OpenBao 2.6.1, Pebble 2.10.1. GitHub Actions. Ninguna dependencia nueva de Python ni de Go; una cota nueva en `pyproject.toml` (`starlette>=0.48`, Task 2).

**Diseño:** `docs/superpowers/specs/2026-11-23-vps-y-marca-novieri-design.md` (aprobado «tal cual» el 2026-11-23): §1.1 y §1.2 son el contrato del cambio de nombre, §2 el HTTPS, §3 el VPS, §4 la verificación, §5 las pruebas, §6 los pendientes, §7 el informe. Decisiones del operador del 2026-11-23 B1–B4 y A3 (`.git/sdd/decisiones-p34.md`). Hechos del despliegue: `.git/sdd/dossier-deploy.md`, comprobados de nuevo contra el código de `1bc0e5e` donde este plan los usa.

## Global Constraints

- **Producto (A3):** «Novieri RMM» en títulos, correos y equipos; «Novieri» en la barra de la consola (proyecto 3). Host (B2): `rmm.novieri.com`. Organización por defecto: `ITS_DEFAULT_ORG_SLUG=novieri`.
- **Lo que cambia (diseño §1.1), valor viejo → nuevo, exactos:** `notices.PRODUCT` `"Novieri"` → `"Novieri RMM"`; asuntos `[Toeshee] …` → `[Novieri RMM] …` (con sus `msgid` de `app/core/i18n/es.py`); servidor MCP `"itms"` → `"novieri-rmm"`; CN de la CA interna `it-suite internal CA` → `Novieri RMM internal CA`; `its-agent` → `novieri-agent` (binario, servicio, `novieri-agent-enroll`, `novieri-agent-uninstall`, `novieri-agent-update.service`, artefactos `novieri-agent-<os>-<arch>`, paquete `novieri-agent_<versión>_<arch>.deb`); `/opt/toeshee` y `/var/opt/toeshee` → `/opt/novieri` y `/var/opt/novieri`; `%ProgramFiles%\ITSuite` y `%ProgramData%\ITSuite` → `%ProgramFiles%\NovieriRMM` y `%ProgramData%\NovieriRMM` (sin espacio, CWE-428); nombre visible en Windows «Novieri RMM Agent»; `com.toeshee.itsagent` → `com.novieri.rmm.agent`; producto MSI «Novieri RMM Agent», fabricante «Novieri», `UPGRADE_NAMESPACE` **`8814cc17-6469-4df7-93f9-0f75ecdbb6a9`** (antes `22ad0fe1-20ac-49a3-938c-25347fa54ecb`); prefijo del instalador `ITMS-Agent-Setup_` → `NovieriRMM-Agent-Setup_` en los dos lados; MSI `ITMS-Agent-<host>-<arch>.msi` → `NovieriRMM-Agent-<host>-<arch>.msi`; `itms.toeshee.com` → `rmm.novieri.com` en documentos, ejemplos y fixtures de despliegue; «ITMS» visible → «Novieri RMM». **Añadidos en la revisión previa (2026-11-24), por decisión del operador y resoluciones del coordinador (`.git/sdd/decisiones-p34.md`):** la cabecera de los *webhooks* `X-Toeshee-Event-Id` → `X-Novieri-Event-Id` (Task 1; no hay consumidores); y las minúsculas que ve un cliente o un tercero: el prefijo `itms:` de los mensajes del comando de instalación → `novieri-agent:`, las instantáneas ZFS `@itms-<tag>` → `@novieri-snap-<tag>` y las de btrfs `.itms-snap-<tag>` → `.novieri-snap-<tag>`, el temporal de una restauración `.itms-restore-*` → `.novieri-restore-*` (Task 8), el CN del certificado de «Sign in with Microsoft» `itms-sign-in-<id>` → `novieri-rmm-sign-in-<id>` y la etiqueta del correo de prueba `+itms-test` → `+novieri-rmm-test` (Task 1; los nombres de descarga `.cer` de la consola son del proyecto 3, Apéndice F).
- **Lo que no cambia (diseño §1.2), y una prueba lo fija (Task 11):** el prefijo `ITS_` del `.env` y `IT_SUITE_VERSION` (y, por la misma razón de §1.2, `ITMS_BIND_IP` e `ITMS_NETWORK_SUBNET`: Apéndice B, P4-B-08); las imágenes `itsuite/*`, la base `itsuite`, el proyecto y los volúmenes `it-suite`, las rutas del servidor `/opt/it-suite`, `/etc/it-suite`, `/var/lib/it-suite`, `/var/backups/it-suite` (P4-B-07); el módulo Go `github.com/toeshee/it-suite/agent`; `itms-rsp.1`, `ITMSRSP1`, `X-ITS-Agent-*`, `X-ITMS-*`, `ITS_AGENT_*`, las cookies `__Host-itms_*`, `urn:itms:loopback`; `its-agent-release/v1`, `itms recovery key wrap v1`, `itms/password/v1`, `it-suite/push-run/credential-version/v1`; `itms-local:`; y, en minúsculas, lo que guarda un dato o es herramienta del operador: los prefijos del *bucket* de la copia fuera del host (`.itms-probe/`, `OFFSITE_S3_PREFIX=itms-platform`) y `scripts/itms_recover.py` (P4-B-10, P4-B-12). La cabecera de webhook **sí** cambia (arriba; P4-B-09).
- **TLS (diseño §2):** Traefik v3.6, resolutor `le`, `tlsChallenge` (TLS-ALPN-01), almacén `/etc/traefik/acme/acme.json` en el volumen `traefik-acme`; el 80 sigue cerrado; claves `.env` nuevas `ITS_PUBLIC_HOST`, `ITS_ACME_EMAIL`, `ITS_ACME_CA_SERVER` (por defecto `https://acme-v02.api.letsencrypt.org/directory`; staging `https://acme-staging-v02.api.letsencrypt.org/directory`); el camino `deploy/certs/` desaparece; el 8443 sigue siendo paso TCP al gateway con la CA interna, sin cambio.
- **Pebble (solo CI y ensayo):** `ghcr.io/letsencrypt/pebble:2.10.1` y `ghcr.io/letsencrypt/pebble-challtestsrv:2.10.1` (publicadas el 2026-04-20; comprobado con `docker pull`), confianza por `LEGO_CA_CERTIFICATES` en el contenedor de Traefik.
- **Seguridad.** Invariantes 1, 1b, 1c, 2, 2a, 3, 4, 5, 5a y 6 de `CLAUDE.md` intactas, y en concreto:
  - **1:** ningún cambio toca la firma, los bytes canónicos ni la verificación (`agent/internal/jobs`, `agent/internal/canonical`, `backend/app/jobs/canonical.py`); el trabajo de la CI (Task 10) es un `hello` firmado que el agente compilado verifica antes de ejecutar.
  - **2:** el agente de la CI marca hacia fuera (443 y 8443); nada abre un puerto en él.
  - **2a(b):** la ruta de preparación del instalador remoto (`/opt/novieri`) está compilada en el agente; ningún campo firmado nuevo, ningún campo firmado cambia de forma.
  - **3:** `new-env.sh` no imprime ningún secreto (lo prueba la Task 4); la CI enmascara cada secreto que lee y borra los ficheros temporales en el `trap`.
  - **4:** el reuso de un código o token sobre una concesión ya terminada pasa a auditarse (Task 5).
- **Protocolo (`CLAUDE.md`, «The wire protocol is still a contract»):** ningún `docs/kernel/wire-protocol/*.schema.json` ni `testdata/` cambia; el `README.md` de esa carpeta solo cambia el nombre del binario en una frase (Task 8, que lo comprueba con `git diff --exit-code`).
- **Migraciones:** ninguna.
- **Pruebas.** Ninguna se salta ni se debilita. Cada prueba nueva se ve en rojo antes de en verde. Una prueba que fija un nombre viejo cambia al nuevo en la misma tarea que cambia el código.
- **Textos.** Backend: por `tr()` y `app/core/i18n/es.py`; `tests/test_i18n_catalog.py` obliga a cambiar cada `msgid` con su texto. Las líneas pasan `ruff` (100 columnas): cada tarea trae sus reajustes de línea escritos.
- **Documentos:** los del despliegue y del agente, en inglés; el plan, el diseño y el informe, en español. Los planes y diseños fechados de `docs/superpowers/` son historia y no se tocan. **`CLAUDE.md` no se toca** (es el contrato del operador; pregunta abierta Q1).
- **Frontend:** no se edita (proyecto 3). El Apéndice F da cada línea de `frontend/src` que este proyecto obliga a cambiar.
- **Verificación, desde `backend/`:** `.venv/bin/pytest -q <archivos de la tarea>`, `.venv/bin/mypy app`, `.venv/bin/ruff check app tests`. **Desde `agent/`:** `gofmt -l .` (no imprime nada), `go vet ./...`, `go test ./... -count=1`. **Desde `deploy/`:** `docker compose config -q`. La suite completa del backend la corre el coordinador en el árbol fusionado.
- **Pistas en paralelo (worktrees), igual que en la fase 4:**
  - **La worktree nace de la rama del proyecto, `claude/compassionate-bardeen-vgtwus`, tal como está en `origin`; nunca de la rama local `main`** (resolución del coordinador del 2026-11-24). La `main` local está en `dd062f8` (PR #12), 252 archivos por detrás de `origin/main` (`6a14dbc`, PR #13), y el código de la rama del proyecto es el de `1bc0e5e`, sobre el que se midió todo este plan (`git diff --stat 1bc0e5e origin/claude/compassionate-bardeen-vgtwus -- backend agent deploy .github scripts` no imprime nada). Una worktree de la `main` local tiene otro árbol: cada ancla `count == 1` de los guiones o muere o, peor para un paso a mano, encuentra otro texto. El coordinador la crea con `git fetch -q origin claude/compassionate-bardeen-vgtwus` y, para la Task 1, `git worktree add -b p4-t1 ../wt-p4-t1 origin/claude/compassionate-bardeen-vgtwus` (y así con el número de cada tarea). El Step 0 de cada tarea lo comprueba con estos comandos, desde la raíz de la worktree:

    ```bash
    git fetch -q origin claude/compassionate-bardeen-vgtwus
    git log --oneline -1
    git merge-base --is-ancestor HEAD origin/claude/compassionate-bardeen-vgtwus && echo ON-PROJECT-BRANCH
    git merge-base --is-ancestor 6a14dbc HEAD && echo HAS-PR-13
    ```

    Expected: la primera línea es la cabeza de `origin/claude/compassionate-bardeen-vgtwus` cuando el coordinador creó la worktree (el 2026-11-24, `21e72ec docs(plan): project 3 plan, pre-flight fixes in progress (snapshot)`, o un commit posterior de esa rama), y después `ON-PROJECT-BRANCH` y `HAS-PR-13`. Si la primera línea es `dd062f8 Merge pull request #12 …`, o falta una de las otras dos, la worktree salió de otra rama: se para y se avisa al coordinador, sin tocar nada. Las tareas de la oleada 1 comprueban además que el código es el medido (`CODE-AS-MEASURED`, en su Step 0); las de la oleada 2 en adelante fusionan antes la rama del coordinador (él da el nombre) y comprueban lo que dice su Step 0.
  - `ln -s /home/user/Novieri-RMM/backend/.venv backend/.venv`; `cd backend && export PYTHONPATH=$PWD`, y `.venv/bin/python -c "import app; print(app.__file__)"` imprime una ruta de la worktree.
  - `export ITS_TEST_REDIS_URL=redis://localhost:6379/3`, o la base (0–15) que el coordinador asigne a la worktree si corren varias a la vez (desde la Task 2, toda la suite la respeta).
  - `pg_ctlcluster 16 main start 2>/dev/null; redis-cli ping >/dev/null 2>&1 || redis-server --daemonize yes`.
  - Docker (Tasks 3, 4, 7, 10 y 12): `docker version >/dev/null 2>&1 || (nohup dockerd >/tmp/dockerd.log 2>&1 &)`, y esperar a que `docker version --format '{{.Server.Version}}'` imprima `29.9.3`. `ip` y `ss` no vienen en este contenedor: `apt-get install -y iproute2` (comprobado el 2026-11-23).
  - **Las imágenes base de la sesión** (Tasks 7, 10 y 12, que construyen `itsuite/backend`, `itsuite/web` e `itsuite/minio`). Desde un contenedor, el HTTPS de esta sesión pasa por un proxy que vuelve a firmar los certificados con su propia CA (`/root/.ccr/README.md`, «docker build / docker run»); `pip`, `npm` y `git` dentro de un `docker build` fallan con `CERTIFICATE_VERIFY_FAILED` (comprobado el 2026-11-23). Sin tocar ningún `Dockerfile` del repositorio, se tapan en el almacén local las tres imágenes base con una copia que confía en ese paquete de CA (BuildKit usa la imagen local para `FROM`; comprobado: `pip download fastapi==0.115.0` pasa dentro del `build`):

    ```bash
    S=/tmp/claude-0/-home-user-Novieri-RMM/21768af2-984e-5cf8-87b2-4dc8a41a4864/scratchpad/shadow
    mkdir -p "$S" && cp /root/.ccr/ca-bundle.crt "$S/session-ca.crt"
    for base in python:3.12-slim node:22-slim golang:1.24-bookworm; do
      docker pull -q "$base" >/dev/null
      printf 'FROM %s
COPY session-ca.crt /usr/local/share/session-ca.crt
ENV SSL_CERT_FILE=/usr/local/share/session-ca.crt PIP_CERT=/usr/local/share/session-ca.crt REQUESTS_CA_BUNDLE=/usr/local/share/session-ca.crt NODE_EXTRA_CA_CERTS=/usr/local/share/session-ca.crt npm_config_cafile=/usr/local/share/session-ca.crt GIT_SSL_CAINFO=/usr/local/share/session-ca.crt CURL_CA_BUNDLE=/usr/local/share/session-ca.crt
' "$base" > "$S/Dockerfile"
      docker build -q -t "$base" "$S"
    done
    ```

    Las imágenes construidas así llevan esa CA dentro: **son solo de esta sesión**, no se publican, y el informe lo dice. En un VPS o en GitHub no hace falta nada de esto.
  - **Docker Hub limita las descargas anónimas.** Un `build` que falla en `load metadata for docker.io/library/…` con `429 Too Many Requests` (visto el 2026-11-23 con `alpine:3.21` y `nginx:alpine`) no es un fallo del plan: se espera y se repite el paso, sin tocar nada. Con las imágenes ya en el almacén local, BuildKit solo pregunta por sus metadatos.
  - Se hace commit en la rama de la worktree; no se hace push ni se toca el árbol principal. Nada se despliega en un host real.

## Lo que hace este proyecto, y lo que no

**Hace.** El cambio de nombre de §1.1 en el backend (Task 1) y en el agente con su control plane (Task 8); el certificado por ACME, las reglas con `Host()`, el 503 de `/api` cuando `api` no está y el fin de `deploy/certs/` (Task 3); `new-env.sh`, la comprobación previa, la comprobación del certificado tras el arranque (Task 4); los pendientes de §6 (Tasks 2, 5 y 6); la CI contra Pebble (Task 7) y con el agente compilado (Task 10); el runbook para un VPS nuevo (Task 9); la guarda de nombres (Task 11); el ensayo del primer arranque entero en esta sesión (Task 12); el informe (Task 13).

**No hace.**
1. **No renombra lo de §1.2** ni lo interno que el diseño no nombra (Apéndice B, P4-B-07 a P4-B-10).
2. **No migra nada de `itms.toeshee.com`** (P7 del proyecto 1, B1): se empieza desde cero.
3. **No toca el frontend** (Apéndice F) **ni `CLAUDE.md`** (Q1).
4. **No resuelve el pendiente «una `app_session` puede quedar con el alcance vacío»**: el coordinador lo quitó el 2026-11-24 (`.git/sdd/decisiones-p34.md`). No hay nada que se llame así en el código (`grep -rn app_session backend/app` no encuentra nada), y la lectura más cercana, una `org_session` con un `ClientScope` vacío, es justo lo que exige la invariante 5a: «Una sesión sin alcance ve cero filas»; rechazarla cambiaría ese fallo seguro por un error. El informe (Task 13) lo dice. Los otros ocho pendientes de §6 sí (Tasks 2, 3, 5 y 6).
5. **No despliega.** La secuencia completa la da el informe (Task 13), y el operador la corre desde `main`.

## Oleadas y paralelismo

| Oleada | Tareas | En paralelo | Depende de | Se fusiona |
|---|---|---|---|---|
| 1 | 1, 3, 4, 5, 6, 7 | sí, archivos disjuntos | — | **3, 4 y 7 juntas**, en el orden 4, 3, 7 (abajo) |
| 2 | 2, 8 | sí | oleada 1 fusionada (la 8 edita `es.py` después de la 1; la 2 edita `test_auth_phase4_falsifiers.py` después de la 3) | 2 y 8 en cualquier orden |
| 3 | 9, 10 | sí | oleada 2 (el runbook nombra `novieri-agent`; la 10 compila `novieri-agent` y usa la CI de la 7) | en cualquier orden |
| 4 | 11, 12 | sí | oleada 3 (la guarda lee el runbook de la 9; el ensayo lo sigue) | 12 antes que 11 si la 12 corrige el runbook |
| 5 | 13 | — | todas | — |

## Tareas

| Task | Qué | Diseño |
|---|---|---|
| 1 | Los nombres visibles del backend: producto, asuntos y `msgid`, servidor MCP, título de la API, CN de la CA, ejemplos | §1.1 |
| 2 | La infraestructura de pruebas: `unique_version`, Redis por `ITS_TEST_REDIS_URL`, alcance del bucle de pytest-asyncio, `HTTP_422_UNPROCESSABLE_CONTENT` | §6 |
| 3 | HTTPS por ACME: `certResolver` con `tlsChallenge`, `Host()` en cada router, `traefik-acme`, `.env.example`, el 503 de la API caída, fuera `deploy/certs/` | §2, §6 |
| 4 | `new-env.sh`, `preflight.sh` (host, DNS, puertos, versiones), `check-public-tls.sh`, y `deploy.sh` que lo usa | §2, §3 |
| 5 | Los pendientes de comportamiento: reuso auditado sobre una concesión terminada; el reloj del enlace de correo empieza tras el envío | §6 |
| 6 | La prueba Go de la desconexión espera por condición, no 15 s fijos | §6 |
| 7 | La CI `deploy` obtiene su certificado de Pebble por ACME, con `new-env.sh` | §4, §5 |
| 8 | El agente en sus dos lados: binario, servicio, rutas, paquetes, MSI, prefijo del instalador, artefactos, CI del agente, documentos del agente | §1.1 |
| 9 | El runbook para un VPS nuevo: requisitos, primer arranque, después de un reinicio, actualización; GO-LIVE; los nombres | §3 |
| 10 | La CI: el agente de Linux compilado se inscribe por el 8443 y ejecuta un `hello` firmado; la cadena de auditoría verifica | §4, §5 |
| 11 | La guarda de nombres viejos (`test_no_old_brand_names.py`) y lo que quede | §1, §5 |
| 12 | El ensayo del primer arranque completo en esta sesión, con Docker y Pebble, y lo que imprimió cada paso | §4 |
| 13 | El informe del proyecto y del programa | §7 |

## Notas de integración del coordinador

- **Oleada 1 — las Tasks 3, 4 y 7 se fusionan juntas, en el orden 4, 3, 7, antes de empujar.** La 3 añade `Host()` a cada router de :443 y quita `deploy/certs/`; la CI de antes (`https://localhost` con un certificado autofirmado en `certs/`) dejaría de encontrar ningún router en el árbol de la 3 sola. La 7 escribe el `.env` de la CI con el `new-env.sh` de la 4 y comprueba el certificado con el `check-public-tls.sh` de la 4. Por separado, el job `deploy` está rojo en la 3 y en la 7; juntas, verde.
- **Oleada 1, choques esperados: ninguno.** La 1 y la 5 tocan archivos distintos de `backend/app` (la 1, `modules/user_admin/api.py`; la 5, `modules/user_admin/service.py`). La 1 es la única que toca `es.py` en esta oleada. **Un archivo, dos tareas, sin choque:** `.gitignore` lo tocan la 3 (el bloque de `deploy/certs/`, líneas 28-34) y la 4 (una línea nueva tras `.env`, línea 12: el temporal de `new-env.sh`); son trozos separados y `git merge` los une sin conflicto en el orden 4, 3 (comprobado en la revisión previa del 2026-11-24).
- **Oleada 2:** la 8 es la única que toca `es.py`, `storage.py` y `app/modules/agent_distribution/` salvo `api.py`, que es de la 2 (`HTTP_422`). La 2 edita 43 pruebas; ninguna es de la 8 (las listas están en sus tareas). **La regla del host de la 8 no entra en dos pruebas de la oleada 1** que nombran `itms.toeshee.com` a propósito, para negarlo (`test_deploy_acme.py` de la 3 y `test_deploy_script_steps.py` de la 4): la Task 8 las excluye y las corre en su Step 4.
- **Oleada 3:** la 9 es dueña de `deploy/README.md`, `deploy/GO-LIVE.md`, `deploy/host-network.md` y `docs/07-backup-restore.md`; la 10, del paso nuevo del job `deploy` de `ci.yml` y de `deploy/ci/agent_flow.py`.
- **Oleada 4:** la 12 es dueña de `deploy/README.md` (corrige lo que el ensayo desmienta y escribe el estado del runbook); la 11 no lo toca. Si la 12 cambia el runbook, se fusiona antes que la 11, y la 11 vuelve a pasar su prueba sobre el árbol fusionado.
- **Firmas que una tarea fija y otras consumen:** el Apéndice A.

## Lo que el despliegue tiene que saber

No son los comandos (los da la Task 13); son los hechos que esos comandos necesitan.

1. **Host nuevo, nada que migrar** (B1, P7). En un `.env` nuevo no hay claves viejas, ni base `keycloak`, ni agentes.
2. **El DNS va antes del primer `up -d`.** Traefik pide el certificado en cuanto ve el router; si `rmm.novieri.com` no resuelve al VPS, la validación falla, Let's Encrypt cuenta un fallo de autorización (5 por nombre, cuenta y hora [E3]) y Traefik sirve su propio certificado. `preflight.sh` lo comprueba antes (Task 4).
3. **Solo IPv4, y ningún registro `AAAA`** (resolución del coordinador del 2026-11-24). Este despliegue es de una dirección IPv4: el runbook, `host-network.md` y `preflight.sh` razonan sobre una IPv4 (`ITMS_BIND_IP`, el registro `A`), y nada comprueba ni documenta que el 443 y el 8443 contesten por IPv6 (que Docker publique `0.0.0.0` también en `[::]` no se puede medir en esta sesión, que no tiene IPv6). Un `AAAA` mandaría a los navegadores y a los agentes, que prueban IPv6 primero, a una dirección que nadie ha comprobado que sirva este stack, o, si es de otra máquina, a esa máquina. **No es por Let's Encrypt:** su validador de TLS-ALPN-01 prueba la primera IPv6 y, ante **cualquier** error, vuelve a la IPv4 (Boulder, `va/tlsalpn.go`, líneas 105-137 [E5]), así que un `AAAA` no hace fallar la emisión por sí mismo. `preflight.sh` se niega ante cualquier `AAAA`, preguntando al DNS y no a `/etc/hosts` (Task 4).
4. **Límites de Let's Encrypt:** 5 certificados por semana para el mismo conjunto de nombres [E3]. Cada primer arranque con un volumen `traefik-acme` vacío pide uno; un ensayo se hace contra staging (`--acme-ca-server https://acme-staging-v02.api.letsencrypt.org/directory`), cuyos límites son mucho más altos y cuyos certificados ningún navegador acepta [E4].
5. **El certificado se renueva solo**, 30 días antes de caducar [E1]. Let's Encrypt baja la vida por defecto a 64 días el 2027-02-10 y a 45 el 2028-02-16 [E6]; con 30 días de margen la renovación sigue cayendo dentro de la vida (a los 34 y a los 15 días). No hace falta cambiar `certificatesDuration`; el informe lo deja como cosa a revisar en 2027 (Task 13).
6. **El correo de ACME sigue siendo obligatorio** (resolución del coordinador del 2026-11-24): lo pide `new-env.sh`, y es **compose** quien no arranca Traefik sin él (`${ITS_ACME_EMAIL:?…}` en `docker-compose.yml`, Task 3), no Traefik, que arranca sin correo (`pkg/provider/acme/provider.go`, `initAccount`, v3.6; medido en la revisión previa con `traefik:v3.6` 3.6.25). Let's Encrypt dejó de mandar avisos de caducidad el 2025-06-04 y ya no guarda la dirección en la cuenta: la pasa a la lista de correo de ISRG [E7]. Nada de la renovación depende de ese buzón; quien vigila la renovación es `check-public-tls.sh` (se niega si quedan menos de 28 días, Task 4) y, según recomienda el informe, un monitor externo (Task 13).
7. **El volumen `traefik-acme` no es material de clave insustituible**: perderlo cuesta un certificado nuevo (dentro del límite de 5 por semana), no datos. No entra en la copia nocturna; GO-LIVE §6 lo dice (Task 9).
8. **OpenBao está sellado tras cada reinicio** y un `up -d` con OpenBao sellado *se bloquea* (`CLAUDE.md`); toda secuencia empieza por `docker compose exec openbao bao status`.
9. **`migrate` corre en cada `up -d`** como dependencia de `api`; este proyecto no añade ninguna migración.
10. **Vuelta atrás en el host nuevo:** `build-images.sh` imprime `Recent tags:` con una sola etiqueta, la del primer arranque. No hay etiqueta anterior: hasta la primera copia (Task 9, paso 12 del primer arranque) no hay vuelta atrás; después, la única es restaurar la copia (`CLAUDE.md`, «A rollback target has to already exist to be one»).
11. **`itms.toeshee.com` se apaga** cuando el nuevo funcione (B1): lo hace el operador; nada del repositorio lo toca. **Hasta entonces, ese host no toma esta `main`:** su `.env` no tiene `ITS_PUBLIC_HOST` ni `ITS_ACME_EMAIL`, compose se niega a interpolar (Task 3) y, con las claves puestas a mano, Traefik dejaría de servir el certificado copiado y pediría uno a Let's Encrypt. El informe y el runbook lo dicen (Tasks 9 y 13).
12. **La clave de los artefactos en el *bucket* cambia con el nombre** (`core/storage.py`, `<prefijo>/<versión>/novieri-agent-<os>-<arch>`): `agent_releases.storage_ref` la guarda, y `service.py` y `api.py` la vuelven a derivar del nombre. En una base con versiones subidas antes del cambio, esas versiones apuntarían a un objeto que no existe y habría que subirlas de nuevo. En el host nuevo no hay ninguna (B1, P7), así que no hace falta migración; por eso esta compilación no se apunta nunca a la base de `itms.toeshee.com` (Task 8).

## Las tareas, una a una

### Task 1: Los nombres visibles del backend

**Files:**
- Create: `backend/tests/test_brand_names_backend.py`
- Modify: `backend/app/modules/notification/delivery.py` (36 apariciones de `Toeshee` en asuntos, y la cabecera `X-Toeshee-Event-Id` de `:1194`, que pasa a `X-Novieri-Event-Id`)
- Modify: `backend/app/core/auth/entra.py:204` (el CN del certificado de «Sign in with Microsoft»), `docs/modules/notification/spec.md:190,479` (la cabecera)
- Modify: `backend/app/core/i18n/es.py` (68 `Toeshee` y 9 entradas con `ITMS` o `itms.example.com` de los archivos de esta tarea; las de `agent_distribution` son de la Task 8)
- Modify: `backend/app/core/auth/notices.py:33`, `backend/app/mcp/server.py:804`, `backend/app/main.py:162`, `backend/app/api/v1/realm_settings.py:178,222,230,548-551`, `backend/app/api/v1/orgs.py:709,762`, `backend/app/api/v1/setup.py:143`, `backend/app/modules/user_admin/api.py:192,227`, `backend/app/modules/people/checklists.py:131`, `backend/app/services/install_link.py:152`, `backend/app/modules/agent_push/dispatch.py:268`, `backend/app/core/ca.py:132`
- Modify: `deploy/openbao/init.sh:37`
- Modify (pruebas que fijan los nombres viejos): `backend/tests/test_i18n_modules_desk.py`, `test_notification_delivery.py` (los asuntos y la cabecera), `test_notification_email_language.py`, `test_platform_backup_status.py`, `test_mcp_server.py`, `test_realm_settings_api.py` (el asunto de prueba y la etiqueta `+itms-test`)

**Interfaces:**
- Consumes: nada de otras tareas.
- Produces: `notices.PRODUCT == "Novieri RMM"` (lo leen el emisor TOTP, `flows.py:893`, y el `rp_name` de WebAuthn, `flows.py:958,1671`); el servidor MCP se presenta como `"novieri-rmm"` en `serverInfo.name`; `FastAPI(title="Novieri RMM API")`; la CA interna se llama `Novieri RMM internal CA` (la Task 9 lo escribe en la salida esperada del paso 7 del primer arranque); cada *webhook* lleva `X-Novieri-Event-Id`; `entra.new_certificate` hace un certificado con CN `novieri-rmm-sign-in-<id de la app>`; `realm_settings.suggested_test_recipient` propone `nombre+novieri-rmm-test@dominio`.

**Decisiones de la tarea.**
- **`X-Toeshee-Event-Id` pasa a `X-Novieri-Event-Id`** (P4-B-09; decisión del operador del 2026-11-24, `.git/sdd/decisiones-p34.md`): es la cabecera con la que un consumidor de *webhooks* deduplica (`docs/modules/notification/spec.md:190,479`), un contrato con quien los consume, y por eso se cambia **ahora**, cuando no hay ninguno (B1, P7); después de su primer consumidor, cambiarla rompería la deduplicación. Se renombra, no se duplica: la prueba de `test_notification_delivery.py` exige la nueva y que la vieja no viaje. `docs/modules/notification/plan.md:1837` es un plan fechado y no se toca.
- **Las dos minúsculas que ve el operador en un tercero se renombran** (resolución del coordinador del 2026-11-24): el CN del certificado que la consola hace para la app de Entra (`entra.py:204`, `itms-sign-in-<id>` → `novieri-rmm-sign-in-<id>`; el portal de Azure lo enseña en la lista de certificados de la app) y la etiqueta del correo de prueba que propone la consola (`realm_settings.py`, `+itms-test` → `+novieri-rmm-test`; llega al buzón del operador). Nada los lee de vuelta: el certificado se identifica por su huella (`x5t#S256`), y la etiqueta es una sugerencia. Los nombres de los archivos `.cer` que descarga la consola (`itms-sign-in.cer`, `itms-sign-in-new.cer`) son de `frontend/src/modules/settings/MicrosoftTab.tsx:215,244`: los cambia el proyecto 3 (Apéndice F, F.1), y el runbook los nombra igual (Task 9).
- **Los ejemplos de host visibles pasan a `rmm.example.com`** donde un administrador los lee (`agent_push/dispatch.py:268`; los de `msi.py` y `link_page.py`, Task 8). Un `itms.example.com` no es la marca en mayúsculas que la guarda busca, pero dice el nombre viejo a quien lo lee.
- **El CN y nada más de la CA local** (`ca.py:132`); su `O = it-suite` (`:133`) es de la CA de desarrollo y queda (P4-B-07). La raíz de OpenBao (`init.sh:37`) solo lleva CN.

- [ ] **Step 0: La worktree**

Run, desde la raíz de la worktree, la comprobación de Global Constraints («Pistas en paralelo») y la de esta oleada:

```bash
git fetch -q origin claude/compassionate-bardeen-vgtwus
git log --oneline -1
git merge-base --is-ancestor HEAD origin/claude/compassionate-bardeen-vgtwus && echo ON-PROJECT-BRANCH
git merge-base --is-ancestor 6a14dbc HEAD && echo HAS-PR-13
git diff --quiet 1bc0e5e HEAD -- backend agent deploy .github scripts && echo CODE-AS-MEASURED
```

Expected: `21e72ec docs(plan): project 3 plan, pre-flight fixes in progress (snapshot)` (o el commit posterior de `origin/claude/compassionate-bardeen-vgtwus` del que el coordinador creó la worktree), `ON-PROJECT-BRANCH`, `HAS-PR-13`, `CODE-AS-MEASURED`. Si la primera línea es `dd062f8 Merge pull request #12 …` o falta alguna de las otras tres, se para y se avisa al coordinador. Después, la preparación de Global Constraints.

- [ ] **Step 1: Las pruebas dicen los nombres nuevos**

Crear `backend/tests/test_brand_names_backend.py`:

```python
"""The backend says «Novieri RMM» where a person reads the product's name
(design 2026-11-23-vps-y-marca-novieri §1.1; operator decisions A3 and B4).

Each assertion is a value a person meets: the name the authenticator app and
the passkey prompt show (`notices.PRODUCT`), the prefix of every notification
subject in both languages, the API's own title, the name an AI app sees for
the MCP server, the CN of the internal CA every agent certificate chains to,
and the CN of the certificate the Azure portal lists for the organization's
"Sign in with Microsoft" app. `tests/test_no_old_brand_names.py` (Task 11)
refuses the OLD names everywhere; this pins the NEW ones where they live.
"""

import ast
import datetime as dt
import re
import uuid
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.x509.oid import NameOID

from app.core.auth import entra, notices
from app.core.ca import LocalCA
from app.core.i18n import tr, use_locale

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
DELIVERY = BACKEND / "app/modules/notification/delivery.py"


def _subject_prefixes() -> list[str]:
    """`[...]` at the start of every string constant in delivery.py."""
    found = []
    for node in ast.walk(ast.parse(DELIVERY.read_text())):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            match = re.match(r"\[([^\]]+)\] ", node.value)
            if match:
                found.append(match.group(1))
    return found


def test_the_product_is_novieri_rmm_in_the_authenticator_and_the_passkey_prompt() -> None:
    assert notices.PRODUCT == "Novieri RMM"


def test_every_notification_subject_starts_with_the_product() -> None:
    prefixes = _subject_prefixes()
    assert len(prefixes) >= 25, "the subjects were not found: the scan is broken"
    assert set(prefixes) == {"Novieri RMM"}, sorted(set(prefixes))


def test_a_subject_keeps_the_prefix_in_spanish() -> None:
    with use_locale("es"):
        subject = tr("[Novieri RMM] Ticket {line} resolved", line="#T-1")
    assert subject == "[Novieri RMM] Ticket #T-1 resuelto"


def test_the_api_is_titled_with_the_product(api_env: Any) -> None:
    assert api_env.app.title == "Novieri RMM API"


def test_the_mcp_server_introduces_itself_as_novieri_rmm() -> None:
    source = (BACKEND / "app/mcp/server.py").read_text()
    assert re.search(r'Server\(\n\s+"novieri-rmm",', source), "the MCP server is not novieri-rmm"


def test_the_internal_ca_is_named_for_the_product(tmp_path: Path) -> None:
    """Both of its sources: the local CA of a development backend, and the
    OpenBao PKI root a deployment's first boot generates (`openbao/init.sh`).
    Set before any agent enrols: agents pin the CA at enrollment."""
    LocalCA(tmp_path)
    root = x509.load_pem_x509_certificate((tmp_path / "root.crt").read_bytes())
    (cn,) = root.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    assert cn.value == "Novieri RMM internal CA"
    init = (REPO / "deploy/openbao/init.sh").read_text()
    assert 'common_name="Novieri RMM internal CA"' in init


def test_the_sign_in_with_microsoft_certificate_is_named_for_the_product() -> None:
    """The CN the Azure portal lists under the app's certificates (decided
    2026-11-24). Nothing reads it back: Entra matches the thumbprint."""
    app = uuid.UUID(int=7)
    made = entra.new_certificate(app_client_id=app, now=dt.datetime.now(dt.UTC))
    certificate = x509.load_pem_x509_certificate(made.certificate_pem.encode("ascii"))
    (cn,) = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    assert cn.value == f"novieri-rmm-sign-in-{app}"
```

Y cambiar las seis pruebas que fijan los nombres viejos: los asuntos, el servidor MCP, el asunto del correo de prueba y su etiqueta (`+itms-test` → `+novieri-rmm-test`), y la cabecera de los *webhooks*, que pasa a `X-Novieri-Event-Id` y deja de viajar con el nombre viejo (decisión del operador del 2026-11-24). Desde la raíz de la worktree (el guion no se guarda en el repositorio; cada recuento que no cuadre lo para antes de escribir nada):

```bash
python3 - . <<'PY'
"""Task 1, Step 1: the tests say the new names first."""
import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1])
EDITS = {
    "backend/tests/test_i18n_modules_desk.py": [
        (r"\[Toeshee\]", "[Novieri RMM]", 8),
        (r"Sign in to Toeshee", "Sign in to Novieri RMM", 1),
        (r"Inicia sesión en Toeshee", "Inicia sesión en Novieri RMM", 1),
    ],
    "backend/tests/test_notification_delivery.py": [
        (r"\[Toeshee\]", "[Novieri RMM]", 6),
        (r'"x-toeshee-event-id"', '"x-novieri-event-id"', 2),
    ],
    "backend/tests/test_notification_email_language.py": [(r"\[Toeshee\]", "[Novieri RMM]", 2)],
    "backend/tests/test_platform_backup_status.py": [(r"\[Toeshee\]", "[Novieri RMM]", 1)],
    "backend/tests/test_mcp_server.py": [
        (r'\["serverInfo"\]\["name"\] == "itms"', '["serverInfo"]["name"] == "novieri-rmm"', 1)
    ],
    "backend/tests/test_realm_settings_api.py": [
        (r'== "ITMS test message"', '== "Novieri RMM test message"', 1),
        (r"\+itms-test@", "+novieri-rmm-test@", 4),
    ],
}
REWRAPS = [
    # Not a wrap: renamed, not duplicated -- the old header must not travel.
    (
        "backend/tests/test_notification_delivery.py",
        '''    assert request.headers["x-novieri-event-id"] == str(event.id)
''',
        '''    assert request.headers["x-novieri-event-id"] == str(event.id)
    assert "x-toeshee-event-id" not in request.headers, "renamed, not duplicated"
''',
    ),
    (
        "backend/tests/test_notification_delivery.py",
        '''    assert subject == "[Novieri RMM] New computer waiting for review: lab-pc-07Bcc: x@evil.test[31m-end"''',
        '''    assert subject == (
        "[Novieri RMM] New computer waiting for review: lab-pc-07Bcc: x@evil.test[31m-end"
    )''',
    ),
]
planned = {}
for rel, edits in EDITS.items():
    text = (ROOT / rel).read_text()
    for pattern, new, expected in edits:
        found = len(re.findall(pattern, text))
        if found != expected:
            sys.exit(f"{rel}: {pattern!r} matched {found} times, the plan says {expected}")
        text = re.sub(pattern, new, text)
    planned[rel] = text
for rel, old, new in REWRAPS:
    if planned[rel].count(old) != 1:
        sys.exit(f"{rel}: the line to wrap is not there exactly once: {old[:60]!r}")
    planned[rel] = planned[rel].replace(old, new)
for rel, text in planned.items():
    (ROOT / rel).write_text(text)
    print(f"rewrote {rel}")
PY
```

Expected: seis líneas `rewrote backend/tests/...`.

- [ ] **Step 2: Las pruebas fallan**

Run: `cd backend && .venv/bin/pytest -q tests/test_brand_names_backend.py tests/test_i18n_modules_desk.py tests/test_notification_delivery.py tests/test_notification_email_language.py tests/test_platform_backup_status.py tests/test_mcp_server.py tests/test_realm_settings_api.py`
Expected: `28 failed, 217 passed` (medido el 2026-11-24 sobre una copia de `1bc0e5e`): las siete de `test_brand_names_backend.py` (`assert 'Novieri' == 'Novieri RMM'`, `{'Toeshee'}`, `'it-suite internal CA'`, `'itms-sign-in-…'`…), las aserciones de asunto (`'[Toeshee] ...' == '[Novieri RMM] ...'`), las dos de la cabecera (`KeyError: 'x-novieri-event-id'`), `serverInfo` (`'itms' == 'novieri-rmm'`), el asunto de prueba de correo y las tres de la etiqueta `+novieri-rmm-test`.

- [ ] **Step 3: Los nombres nuevos**

Desde la raíz de la worktree:

```bash
python3 - . <<'PY'
"""Task 1: the backend's visible names (plan 2026-11-23, Task 1, Step 3)."""
import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1])
# (file, [(pattern, replacement, expected number of matches)]); a count that
# differs stops the script before anything is written.
EDITS = {
    "backend/app/modules/notification/delivery.py": [
        (r"Toeshee(?!-Event-Id)", "Novieri RMM", 36),
        # The webhook dedupe header (operator, 2026-11-24): no consumer yet.
        (r"X-Toeshee-Event-Id", "X-Novieri-Event-Id", 1),
    ],
    "backend/app/core/i18n/es.py": [
        (r"Toeshee", "Novieri RMM", 68),
        (r"ITMS test message", "Novieri RMM test message", 1),
        (r"test message from ITMS", "test message from Novieri RMM", 1),
        (r"Nothing installed through ITMS yet", "Nothing installed through Novieri RMM yet", 1),
        (r"the network ITMS's own proxy", "the network Novieri RMM's own proxy", 1),
        (r"https://itms\.example\.com, with no path\)", "https://rmm.example.com, with no path)", 1),
        (r"ejemplo https://itms\.example\.com, sin ruta\)", "ejemplo https://rmm.example.com, sin ruta)", 1),
        (r"el proxy propio de ITMS ", "el proxy propio de Novieri RMM ", 1),
        (r'"Mensaje de prueba de ITMS"', '"Mensaje de prueba de Novieri RMM"', 1),
        (r"Este es un mensaje de prueba de ITMS\.", "Este es un mensaje de prueba de Novieri RMM.", 1),
        (r"Todavía no se ha instalado nada con ITMS", "Todavía no se ha instalado nada con Novieri RMM", 1),
    ],
    "backend/app/core/auth/notices.py": [(r'PRODUCT = "Novieri"', 'PRODUCT = "Novieri RMM"', 1)],
    "backend/app/mcp/server.py": [(r'Server\(\n        "itms",', 'Server(\n        "novieri-rmm",', 1)],
    "backend/app/main.py": [(r'title="ITMS API"', 'title="Novieri RMM API"', 1)],
    "backend/app/api/v1/realm_settings.py": [
        (r"ITMS test message", "Novieri RMM test message", 1),
        (r"test message from ITMS", "test message from Novieri RMM", 1),
        (r"\+itms-test", "+novieri-rmm-test", 3),
    ],
    "backend/app/core/auth/entra.py": [
        (r'f"itms-sign-in-\{app_client_id\}"', 'f"novieri-rmm-sign-in-{app_client_id}"', 1)
    ],
    "docs/modules/notification/spec.md": [(r"X-Toeshee-Event-Id", "X-Novieri-Event-Id", 2)],
    "backend/app/api/v1/orgs.py": [
        (r"ITMS record", "Novieri RMM record", 1),
        (r"e\.g\. toeshee\.com\.", "e.g. novieri.com.", 1),
    ],
    "backend/app/api/v1/setup.py": [(r"ITMS user id", "Novieri RMM user id", 1)],
    "backend/app/modules/user_admin/api.py": [(r"ITMS user id", "Novieri RMM user id", 2)],
    "backend/app/modules/people/checklists.py": [
        (r"Nothing installed through ITMS yet", "Nothing installed through Novieri RMM yet", 1)
    ],
    "backend/app/services/install_link.py": [
        (r"the network ITMS's own proxy", "the network Novieri RMM's own proxy", 1)
    ],
    "backend/app/modules/agent_push/dispatch.py": [
        (r"https://itms\.example\.com, with no path\)", "https://rmm.example.com, with no path)", 1)
    ],
    "backend/app/core/ca.py": [(r'"it-suite internal CA"', '"Novieri RMM internal CA"', 1)],
    "deploy/openbao/init.sh": [
        (r'common_name="it-suite internal CA"', 'common_name="Novieri RMM internal CA"', 1)
    ],
}
# Lines the longer name pushes past ruff's 100 columns, wrapped by hand:
# (file, exact text after EDITS, its replacement). Each must occur once.
REWRAPS = [
    (
        "backend/app/core/i18n/es.py",
        '''        "Este es un mensaje de prueba de Novieri RMM. Te ha llegado, así que el correo de cuentas de "
        "esta organización (invitaciones y restablecimientos de contraseña) funciona."''',
        '''        "Este es un mensaje de prueba de Novieri RMM. Te ha llegado, así que el correo de "
        "cuentas de esta organización (invitaciones y restablecimientos de contraseña) funciona."''',
    ),
    (
        "backend/app/api/v1/realm_settings.py",
        '''                    "This is a test message from Novieri RMM. It reached you, so this organization's "
                    "account email (invitations and password resets) is working."''',
        '''                    "This is a test message from Novieri RMM. It reached you, so this "
                    "organization's account email (invitations and password resets) is working."''',
    ),
    (
        "backend/app/core/i18n/es.py",
        '''    "{item} overlaps {proxy}, the network Novieri RMM's own proxy sits on (ITS_TRUSTED_PROXY_CIDRS). "
    "Requests can appear to come from there, so listing it would let any computer in": (''',
        '''    "{item} overlaps {proxy}, the network Novieri RMM's own proxy sits on "
    "(ITS_TRUSTED_PROXY_CIDRS). Requests can appear to come from there, so "
    "listing it would let any computer in": (''',
    ),
    (
        "backend/app/core/i18n/es.py",
        '''    "[Novieri RMM] Push install stopped: {prefix}": "[Novieri RMM] Instalación remota detenida: {prefix}",''',
        '''    "[Novieri RMM] Push install stopped: {prefix}": (
        "[Novieri RMM] Instalación remota detenida: {prefix}"
    ),''',
    ),
    (
        "backend/app/core/i18n/es.py",
        '''    "[Novieri RMM] Ticket {line}: approval approved": "[Novieri RMM] Ticket {line}: aprobación aprobada",''',
        '''    "[Novieri RMM] Ticket {line}: approval approved": (
        "[Novieri RMM] Ticket {line}: aprobación aprobada"
    ),''',
    ),
    (
        "backend/app/core/i18n/es.py",
        '''    "[Novieri RMM] Ticket {line}: approval rejected": "[Novieri RMM] Ticket {line}: aprobación rechazada",''',
        '''    "[Novieri RMM] Ticket {line}: approval rejected": (
        "[Novieri RMM] Ticket {line}: aprobación rechazada"
    ),''',
    ),
    (
        "backend/app/core/i18n/es.py",
        '''    "[Novieri RMM] Ticket {line}: approval decided": "[Novieri RMM] Ticket {line}: aprobación decidida",''',
        '''    "[Novieri RMM] Ticket {line}: approval decided": (
        "[Novieri RMM] Ticket {line}: aprobación decidida"
    ),''',
    ),
    (
        "backend/app/modules/notification/delivery.py",
        '''        tr("[Novieri RMM] Internal note added to ticket #{ticket_number}", ticket_number=ticket_number),''',
        '''        tr(
            "[Novieri RMM] Internal note added to ticket #{ticket_number}",
            ticket_number=ticket_number,
        ),''',
    ),
    (
        "backend/app/modules/notification/delivery.py",
        '''        subject = tr("[Novieri RMM] Ticket {line}: approval {decision}", line=line, decision=decision)''',
        '''        subject = tr(
            "[Novieri RMM] Ticket {line}: approval {decision}", line=line, decision=decision
        )''',
    ),
    (
        "backend/app/modules/notification/delivery.py",
        '''        tr("[Novieri RMM] Health alert ESCALATED ({severity}): {line}", severity=severity, line=line),''',
        '''        tr(
            "[Novieri RMM] Health alert ESCALATED ({severity}): {line}",
            severity=severity,
            line=line,
        ),''',
    ),
    # Not a wrap: the header's comment says when and why it was renamed.
    (
        "backend/app/modules/notification/delivery.py",
        '''    # retries of the same row.
    headers: dict[str, str] = {"X-Novieri-Event-Id": str(delivery.event_id)}''',
        '''    # retries of the same row. Renamed with the product on 2026-11-24, while
    # no consumer existed yet (the operator's decision; spec §7 names it).
    headers: dict[str, str] = {"X-Novieri-Event-Id": str(delivery.event_id)}''',
    ),
    (
        "backend/app/core/auth/entra.py",
        '''    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"novieri-rmm-sign-in-{app_client_id}")])''',
        '''    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, f"novieri-rmm-sign-in-{app_client_id}")]
    )''',
    ),
]
planned = {}
for rel, edits in EDITS.items():
    text = (ROOT / rel).read_text()
    for pattern, new, expected in edits:
        found = len(re.findall(pattern, text))
        if found != expected:
            sys.exit(f"{rel}: {pattern!r} matched {found} times, the plan says {expected}")
        text = re.sub(pattern, new, text)
    planned[rel] = text
for rel, old, new in REWRAPS:
    if planned[rel].count(old) != 1:
        sys.exit(f"{rel}: the line to wrap is not there exactly once: {old[:60]!r}")
    planned[rel] = planned[rel].replace(old, new)
for rel, text in planned.items():
    (ROOT / rel).write_text(text)
    print(f"rewrote {rel}")
PY
```

Expected: dieciséis líneas `rewrote ...` (las catorce de antes, más `backend/app/core/auth/entra.py` y `docs/modules/notification/spec.md`), la última `rewrote deploy/openbao/init.sh`. Comprobado en una copia de `1bc0e5e` el 2026-11-23, y de nuevo con la cabecera, el CN de Entra y la etiqueta del correo de prueba el 2026-11-24: los recuentos son exactos y, con los reajustes, `ruff` queda limpio.

- [ ] **Step 4: Las pruebas pasan**

Run (desde `backend/`): `.venv/bin/pytest -q tests/test_brand_names_backend.py tests/test_i18n_catalog.py tests/test_i18n_modules_desk.py tests/test_notification_delivery.py tests/test_notification_email_language.py tests/test_platform_backup_status.py tests/test_mcp_server.py tests/test_realm_settings_api.py tests/test_ca.py tests/test_auth_passkeys.py tests/test_auth_falsifiers.py tests/test_install_link_api.py tests/test_agent_push_dispatch.py tests/test_setup_api.py tests/test_no_vendor_names_backend.py tests/test_auth_entra.py tests/test_entra_settings_api.py && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `488 passed` (medido el 2026-11-24 en una copia de `1bc0e5e` con esta tarea: las quince primeras recogen 431 pruebas, la nueva incluida, y las dos de Entra 57; el «688 passed, 1 skipped» del borrador era otro conjunto), `Success: no issues found in 550 source files`, `All checks passed!`. Y desde `backend/`:

```bash
grep -c "Toeshee" app/modules/notification/delivery.py app/core/i18n/es.py
grep -rn 'X-Toeshee-Event-Id\|itms-sign-in\|+itms-test' app ../docs/modules/notification/spec.md || echo NONE-LEFT
```

Expected: `app/modules/notification/delivery.py:0`, `app/core/i18n/es.py:0` y `NONE-LEFT`.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_brand_names_backend.py backend/tests/test_i18n_modules_desk.py backend/tests/test_notification_delivery.py backend/tests/test_notification_email_language.py backend/tests/test_platform_backup_status.py backend/tests/test_mcp_server.py backend/tests/test_realm_settings_api.py backend/app/modules/notification/delivery.py backend/app/core/i18n/es.py backend/app/core/auth/notices.py backend/app/mcp/server.py backend/app/main.py backend/app/api/v1/realm_settings.py backend/app/api/v1/orgs.py backend/app/api/v1/setup.py backend/app/modules/user_admin/api.py backend/app/modules/people/checklists.py backend/app/services/install_link.py backend/app/modules/agent_push/dispatch.py backend/app/core/ca.py backend/app/core/auth/entra.py deploy/openbao/init.sh docs/modules/notification/spec.md
git commit -m "feat(brand): the backend says Novieri RMM: product, subjects, webhook header, MCP server, API title, internal CA, Entra certificate"
```

---

### Task 2: La infraestructura de pruebas (oleada 2)

**Files:**
- Create: `backend/tests/redis_support.py`, `backend/tests/test_suite_support.py`
- Modify: `backend/tests/agent_update_support.py:91`
- Modify: `backend/pyproject.toml` (`description`, `starlette>=0.48`, `pytest-asyncio>=0.24`, `asyncio_default_fixture_loop_scope`)
- Modify: `backend/app/modules/remote/api.py:225,246`, `backend/app/modules/agent_distribution/api.py:144,178,434,492`, `backend/app/api/v1/jobs.py:202,227,231`
- Modify (el literal de Redis, 43 archivos): `backend/tests/conftest.py`, `test_ai_act_sweep.py`, `test_api_lifespan.py`, `test_api_routes.py`, `test_auth_phase4_falsifiers.py`, `test_backup_api.py`, `test_backup_offsite_worker.py`, `test_backup_worker.py`, `test_cert_renewal.py`, `test_client_scope_push.py`, `test_collection_schedule.py`, `test_deploy_api.py`, `test_deploy_window.py`, `test_deploy_worker.py`, `test_device_group_reevaluate.py`, `test_dispatch.py`, `test_email_intake_poller.py`, `test_error_capture_worker_wiring.py`, `test_gateway_heartbeat.py`, `test_gateway_mtls.py`, `test_health_ingest.py` (2), `test_health_sweep.py`, `test_ipam_sweep_dispatch.py`, `test_logging_kernel.py`, `test_patch_api.py`, `test_patch_worker.py`, `test_platform_health_sampler.py`, `test_platform_heartbeat.py`, `test_provisioning_worker.py`, `test_push_run_tick.py`, `test_rate_limit.py`, `test_remote_relay_registry.py`, `test_review_sweeps.py`, `test_security_ingest.py`, `test_setup_api.py`, `test_sla_sweep.py`, `test_ui_ws.py`, `test_worker.py`, `test_worker_audit_chain.py`, `test_worker_beat_expiry.py`, `test_worker_ipam_sweep_containment.py`, `test_worker_sweep_containment.py`, `tool_surface.py`

**Interfaces:**
- Consumes: el árbol con la oleada 1 fusionada (la Task 3 cambió una línea de `test_auth_phase4_falsifiers.py`).
- Produces: `tests.redis_support.TEST_REDIS_URL: str` — `os.environ.get("ITS_TEST_REDIS_URL", "redis://localhost:6379/9")`, la única fuente de la URL de Redis de la suite; `unique_version(major: int = 2) -> str` devuelve `f"{major}.{uuid4().int % 10**12}.0"`.

**Decisiones de la tarea.**
- **`function` como alcance del bucle**, no `fixture-scope`: ningún *fixture* asíncrono de la suite pide un alcance más ancho que el de función (los de alcance `session` de `conftest.py` son síncronos), así que es el comportamiento de hoy y el valor que pytest-asyncio adoptará («Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to "function" scope», `pytest_asyncio/plugin.py:277-285`, 1.5.0).
- **`starlette>=0.48`**: `HTTP_422_UNPROCESSABLE_CONTENT` no existe en 0.47.3 y sí en 0.48.0 (comprobado el 2026-11-23 descargando las dos ruedas y leyendo `starlette/status.py`); `fastapi>=0.115` sola dejaría resolver una Starlette sin la constante.
- **La guarda de Redis mira el puerto 6379**, no cualquier URL: `test_ai_admin_api.py:926` apunta a propósito a `redis://127.0.0.1:6399/0`, un puerto muerto.
- **El guion de los literales no toca `redis_support.py` ni `test_suite_support.py`**: el segundo nombra las dos URLs que espera.
- **`test_e2e_kernel.py` ya leía `ITS_TEST_REDIS_URL`** por `api_env` (fase 4, C8) y no cambia.

- [ ] **Step 0: La worktree, con la oleada 1**

Run, desde la raíz de la worktree (creada de `origin/claude/compassionate-bardeen-vgtwus`, nunca de la `main` local: Global Constraints, «Pistas en paralelo»), después de fusionar la rama del coordinador con la oleada 1 (`git merge --no-edit` de la rama que él nombra):

```bash
git log --oneline -1
git merge-base --is-ancestor 6a14dbc HEAD && echo HAS-PR-13
grep -c '"api-unavailable"' backend/tests/test_auth_phase4_falsifiers.py
```

Expected: la primera línea es la fusión con la rama del coordinador (o su cabeza, si la fusión fue rápida), nunca `dd062f8 Merge pull request #12 …`; después `HAS-PR-13` y `1`. Si no, se para y se avisa al coordinador. Después, la preparación de Global Constraints.

- [ ] **Step 1: La prueba de la suite**

Crear `backend/tests/test_suite_support.py`:

```python
"""The test suite's own plumbing (design 2026-11-23-vps-y-marca-novieri §6).

Four things a run depended on without anything holding them:

  * `agent_update_support.unique_version` drew the minor from `% 100000`, so
    a run that seeds a few hundred releases -- they are global -- had a real
    chance of two tests minting the same version;
  * 43 files named Redis database 9 by a literal, and several `flushdb()` it:
    two suites at once (two worktrees, two jobs on one runner) emptied each
    other's. Every one now takes `tests.redis_support.TEST_REDIS_URL`, which
    is `ITS_TEST_REDIS_URL` when it is set;
  * pytest-asyncio warned on every run that its fixture loop scope was
    unset, and will change the default under the suite;
  * three routes named `HTTP_422_UNPROCESSABLE_ENTITY`, which Starlette
    deprecated for RFC 9110's `HTTP_422_UNPROCESSABLE_CONTENT` (0.48).
"""

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.modules.agent_update.jobtypes import _SEMVER
from tests.agent_update_support import unique_version

BACKEND = Path(__file__).resolve().parents[1]
TESTS = BACKEND / "tests"


def _docstring_ids(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def test_unique_versions_are_strict_semver_and_do_not_repeat_in_a_long_run() -> None:
    """20 000 draws: with `% 100000` about 2 000 of them collide (n^2/2N);
    with `% 10**12`, the chance of one collision is about 2 in 10 000."""
    drawn = [unique_version() for _ in range(20_000)]
    assert all(re.fullmatch(_SEMVER, version) for version in drawn)
    assert len(set(drawn)) == len(drawn)


def test_no_test_names_a_redis_database_of_its_own() -> None:
    offenders = []
    for path in sorted(TESTS.glob("*.py")):
        if path.name in ("redis_support.py", "test_suite_support.py"):
            continue
        tree = ast.parse(path.read_text())
        skip = _docstring_ids(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in skip
                # The local server's port: a test that points at a dead
                # port on purpose (6399, `test_ai_admin_api.py`) is not one.
                and re.search(r"redis://[^\s/]+:6379/\d+", node.value)
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], "take tests.redis_support.TEST_REDIS_URL instead: " + ", ".join(
        offenders
    )


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({"ITS_TEST_REDIS_URL": "redis://localhost:6379/7"}, "redis://localhost:6379/7"),
        ({}, "redis://localhost:6379/9"),
    ],
)
def test_the_suites_redis_is_the_environments(environment: dict[str, str], expected: str) -> None:
    env = {k: v for k, v in os.environ.items() if k != "ITS_TEST_REDIS_URL"} | environment
    printed = subprocess.run(
        [sys.executable, "-c", "from tests.redis_support import TEST_REDIS_URL as u; print(u)"],
        cwd=BACKEND,
        env=env | {"PYTHONPATH": str(BACKEND)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert printed == expected


def test_the_fixture_loop_scope_is_set(pytestconfig: pytest.Config) -> None:
    """`function`: what every async fixture of this suite already gets (none
    asks for a wider loop), written down before pytest-asyncio's default
    moves under it."""
    assert pytestconfig.getini("asyncio_default_fixture_loop_scope") == "function"


def test_no_route_names_the_deprecated_422() -> None:
    found = [
        f"{path.relative_to(BACKEND)}:{node.lineno}"
        for path in sorted((BACKEND / "app").rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Attribute) and node.attr == "HTTP_422_UNPROCESSABLE_ENTITY"
    ]
    assert found == [], found
```

- [ ] **Step 2: La prueba falla**

Run: `cd backend && .venv/bin/pytest -q tests/test_suite_support.py`
Expected: `6 failed` (comprobado sobre `1bc0e5e`): versiones repetidas, una lista de 43 archivos con su Redis, `ModuleNotFoundError: No module named 'tests.redis_support'` en las dos de entorno, `None == 'function'`, y nueve `HTTP_422_UNPROCESSABLE_ENTITY`.

- [ ] **Step 3: La implementación**

Crear `backend/tests/redis_support.py`:

```python
"""The suite's Redis: `ITS_TEST_REDIS_URL`, or database 9 of the local server.

Every test that needs a Redis takes it from here, never from a literal
(design 2026-11-23-vps-y-marca-novieri §6). Several of them empty the
database they are given (`flushdb()`), so two suites at once -- two
worktrees, two CI jobs on one runner -- must be handed different ones, and
only a value read in one place can be.
"""

import os

TEST_REDIS_URL = os.environ.get("ITS_TEST_REDIS_URL", "redis://localhost:6379/9")
```

Cambiar los literales, desde `backend/` (el guion no se guarda):

```bash
python3 - <<'PY'
"""Task 2, Step 3: every test takes its Redis from tests/redis_support.py.
Run from backend/."""
import ast
import re
from pathlib import Path

LITERAL = re.compile(
    r'os\.environ\.get\(\s*"ITS_TEST_REDIS_URL",\s*"redis://localhost:6379/9"\s*\)'
    r'|"redis://(?:localhost|127\.0\.0\.1):6379/9"'
)
IMPORT = "from tests.redis_support import TEST_REDIS_URL\n"
changed = []
for path in sorted(Path("tests").glob("*.py")):
    # The module itself, and the test that holds every other one to it.
    if path.name in ("redis_support.py", "test_suite_support.py"):
        continue
    text = path.read_text()
    new, count = LITERAL.subn("TEST_REDIS_URL", text)
    if not count:
        continue
    # After the last import of the module's FIRST block of imports (conftest.py
    # has a second one further down); ruff's isort (I001) then sorts it in.
    body = ast.parse(new).body
    start = 1 if body and isinstance(body[0], ast.Expr) else 0
    end = start
    while end < len(body) and isinstance(body[end], (ast.Import, ast.ImportFrom)):
        end += 1
    assert end > start, f"{path}: no import block"
    lines = new.splitlines(keepends=True)
    lines.insert(body[end - 1].end_lineno, IMPORT)
    path.write_text("".join(lines))
    changed.append(f"{path} ({count})")
print("\n".join(changed))
print(f"{len(changed)} files")
PY
.venv/bin/ruff check --fix --select I001,F401 tests
```

Expected: 43 líneas `tests/... (1)` (`tests/test_health_ingest.py (2)`), `43 files`, y `Found 13 errors (13 fixed, 0 remaining).` (el `import os` que queda sin uso en 13 archivos, y el orden de los imports; comprobado sobre `1bc0e5e`).

El resto, desde `backend/`:

```bash
sed -i 's/    return f"{major}.{uuid4().int % 100000}.0"/    return f"{major}.{uuid4().int % 10**12}.0"/' tests/agent_update_support.py
sed -i 's/status\.HTTP_422_UNPROCESSABLE_ENTITY/status.HTTP_422_UNPROCESSABLE_CONTENT/g' app/modules/remote/api.py app/modules/agent_distribution/api.py app/api/v1/jobs.py
python3 - <<'PY'
from pathlib import Path

p = Path("pyproject.toml")
s = p.read_text()
for old, new in (
    ('description = "Toeshee IT suite control plane"', 'description = "Novieri RMM control plane"'),
    (
        '    "fastapi>=0.115",\n',
        '    "fastapi>=0.115",\n'
        "    # `status.HTTP_422_UNPROCESSABLE_CONTENT` (RFC 9110) arrived in 0.48; the\n"
        "    # name before it is deprecated (design 2026-11-23 §6).\n"
        '    "starlette>=0.48",\n',
    ),
    (
        '    "pytest-asyncio>=0.23",',
        "    # 0.24 is the first that reads `asyncio_default_fixture_loop_scope`.\n"
        '    "pytest-asyncio>=0.24",',
    ),
    (
        'asyncio_mode = "auto"\n',
        'asyncio_mode = "auto"\n'
        "# What every async fixture here already gets (none asks for a wider loop),\n"
        "# set before pytest-asyncio's default changes under the suite.\n"
        'asyncio_default_fixture_loop_scope = "function"\n',
    ),
):
    assert s.count(old) == 1, old
    s = s.replace(old, new)
p.write_text(s)
PY
grep -c "HTTP_422_UNPROCESSABLE_CONTENT" app/modules/remote/api.py app/modules/agent_distribution/api.py app/api/v1/jobs.py
```

Expected: `app/modules/remote/api.py:2`, `app/modules/agent_distribution/api.py:4`, `app/api/v1/jobs.py:3`.

- [ ] **Step 4: Las pruebas pasan, y el aviso ya no sale**

Run: `.venv/bin/pytest -q -W error::pytest.PytestDeprecationWarning tests/test_suite_support.py tests/test_api_routes.py tests/test_rate_limit.py tests/test_dispatch.py tests/test_worker.py tests/test_agent_update_api.py tests/test_agent_update_reconcile.py tests/test_remote_relay_registry.py tests/test_gateway_mtls.py tests/test_setup_api.py tests/test_auth_phase4_falsifiers.py tests/test_e2e_kernel.py tests/test_jobs_paging.py tests/test_job_retry.py tests/test_remote_api.py tests/test_agent_release_api.py && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: PASS (en una copia de `1bc0e5e`: las seis nuevas, y `63 passed` para las seis primeras con `test_dispatch.py`, `test_worker.py`, `test_agent_update_api.py` y `test_remote_relay_registry.py`); con `-W error::pytest.PytestDeprecationWarning` no hay `INTERNALERROR` (antes, `pytest -W error::DeprecationWarning` moría en `pytest_configure` con el aviso del alcance; comprobado); `Success`, `All checks passed!`.

- [ ] **Step 5: Commit**

```bash
git add backend/tests backend/pyproject.toml backend/app/modules/remote/api.py backend/app/modules/agent_distribution/api.py backend/app/api/v1/jobs.py
git commit -m "test: one Redis for the suite, unique release versions, the asyncio loop scope set, RFC 9110's 422"
```

---

### Task 3: HTTPS por ACME, el host en cada router, el 503 de la API caída

**Files:**
- Create: `backend/tests/test_deploy_acme.py`
- Modify: `deploy/docker-compose.yml` (traefik `:7-46`, routers de `api` `:146-148`, `web` `:414-417`, `minio` `:510-513,556-562`, `volumes:` `:643-657`)
- Modify (reescrito entero): `deploy/traefik/dynamic.yml`
- Modify: `deploy/minio/Dockerfile:56`, `.gitignore:28-34`
- Delete: `deploy/certs/.gitkeep`
- Modify: `backend/tests/traefik_routes.py` (reescrito entero), `backend/tests/test_public_surface.py:62`, `backend/tests/test_auth_phase4_falsifiers.py` (la línea `assert {name for _p, name, _r in routers()} == {"api", "minio", "web"}`), `backend/tests/test_deploy_object_storage_route.py:47`

**Interfaces:**
- Consumes: nada de otras tareas. **Se fusiona junto con las Tasks 4 y 7** (notas de integración): las claves `ITS_PUBLIC_HOST`, `ITS_ACME_EMAIL` e `ITS_ACME_CA_SERVER` las añade a `.env.example` la Task 4.
- Produces:
  - el resolutor ACME `le` (los routers lo nombran en `traefik.http.routers.<r>.tls.certresolver: le`);
  - compose exige `ITS_PUBLIC_HOST` (`${ITS_PUBLIC_HOST:?set in .env}`) y `ITS_ACME_EMAIL`, y lee `ITS_ACME_CA_SERVER` con el valor de producción por defecto;
  - el volumen `traefik-acme` (montado en `/etc/traefik/acme`);
  - el router de fichero `api-unavailable` (prioridad 2, servicio sin servidores: 503);
  - `tests/traefik_routes.py`: `PUBLIC_HOST = "rmm.novieri.com"`, `DYNAMIC: Path`, `dynamic() -> dict[str, Any]`, `routers(path=COMPOSE) -> list[tuple[int, str, str]]` (con los routers de fichero), `matches(rule, path, method, host=PUBLIC_HOST) -> bool` (entiende `Host()`), `winner(path, compose_file=COMPOSE, *, host=PUBLIC_HOST, down=()) -> str`.

**Decisiones de la tarea.**
- **La configuración estática va en `command:` y la confianza en Pebble, en una variable de entorno** (`LEGO_CA_CERTIFICATES`, Task 7): con `command:`, un archivo de compose adicional tendría que repetir la lista entera para añadir `--certificatesresolvers.le.acme.cacertificates`, y una configuración estática de Traefik no se reparte entre CLI y entorno («mutually exclusive», doc de Traefik [E2]). `LEGO_CA_CERTIFICATES` es de lego, el cliente ACME que Traefik lleva dentro [E8]; comprobado el 2026-11-23 con `traefik:v3.6` (3.6.25) contra Pebble 2.10.1: con ella, «Server responded with a certificate»; sin ella, «x509: certificate signed by unknown authority».
- **`tls.certresolver` en cada router**, no un resolutor por defecto en el *entrypoint*: comprobado en vivo con etiquetas por router; la prueba exige que los tres de compose y el de fichero lo nombren, así un router nuevo que lo olvide no pasa.
- **El 503 cubre las once rutas de `api`, no solo `/api` y `/mcp`**: el diseño nombra esas dos, y las otras nueve (`/enroll/`, `/i/`, `/.well-known/*`, `/oauth/token|register|revoke`) caían igual a la consola. La prueba fija que las dos reglas sean iguales.
- **El servicio sin servidores** responde `503` con el cuerpo `no available server` (medido el 2026-11-23 con Traefik 3.6.25: `/api/v1/x` y `/mcp` `200` con el contenedor arriba, `503` parado, `/` siempre `200`).
- **`.gitignore` sigue ignorando `deploy/certs/`**, aunque nada lo lea: un host anterior puede tener allí una clave privada, y no debe poder entrar en un commit.
- **El certificado no entra en la copia**: perder `traefik-acme` cuesta un certificado nuevo (Task 9, GO-LIVE §6).

- [ ] **Step 0: La worktree**

Run, desde la raíz de la worktree, la comprobación de Global Constraints («Pistas en paralelo») y la de esta oleada:

```bash
git fetch -q origin claude/compassionate-bardeen-vgtwus
git log --oneline -1
git merge-base --is-ancestor HEAD origin/claude/compassionate-bardeen-vgtwus && echo ON-PROJECT-BRANCH
git merge-base --is-ancestor 6a14dbc HEAD && echo HAS-PR-13
git diff --quiet 1bc0e5e HEAD -- backend agent deploy .github scripts && echo CODE-AS-MEASURED
```

Expected: `21e72ec docs(plan): project 3 plan, pre-flight fixes in progress (snapshot)` (o el commit posterior de `origin/claude/compassionate-bardeen-vgtwus` del que el coordinador creó la worktree), `ON-PROJECT-BRANCH`, `HAS-PR-13`, `CODE-AS-MEASURED`. Si la primera línea es `dd062f8 Merge pull request #12 …` o falta alguna de las otras tres, se para y se avisa al coordinador. Después, la preparación de Global Constraints (con Docker).

- [ ] **Step 1: Las pruebas dicen el TLS nuevo**

Reemplazar `backend/tests/traefik_routes.py` entero por:

```python
"""Traefik's router selection, read from the compose labels and from
`deploy/traefik/dynamic.yml`.

Moved here from `tests/test_deploy_keycloak_registration_edge.py` in own
authentication phase 4, when that file went with the routers it held to
Keycloak's registration edge. The model is the same: highest priority first,
first match wins, a router's default priority being its rule's length, with
Python's `re` standing in for Go's RE2 (the patterns used are in their
common subset). It proves what the RULES say for the paths given; it cannot
prove what Traefik's own normalisation does with them -- the `deploy` job in
CI sends real requests through the real `traefik` service for that.

SINCE 2026-11-23 (design 2026-11-23-vps-y-marca-novieri §2) every router on
:443 names the host: Traefik asks Let's Encrypt for the certificate of the
name in a router's `Host()`, and a request for any other name is routed
nowhere. The model reads `Host()` too, against `PUBLIC_HOST` unless a test
asks for another, and the file provider's routers beside the labels' --
`api-unavailable`, the 503 a request for the api gets while no api
container is healthy, lives there.

Used by `test_public_surface.py`, `test_deploy_mcp_route.py`,
`test_auth_jwks.py`, `test_auth_phase4_falsifiers.py` and
`test_deploy_acme.py`.
"""

import functools
import re
from pathlib import Path
from typing import Any

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "deploy/docker-compose.yml"
DYNAMIC = COMPOSE.parent / "traefik" / "dynamic.yml"

#: The name the model's requests carry; what compose and the file provider
#: substitute for `ITS_PUBLIC_HOST`.
PUBLIC_HOST = "rmm.novieri.com"

#: Every method a request to the public entrypoint is checked with.
METHODS = ("GET", "HEAD", "POST", "PUT", "DELETE")

_MATCHER = re.compile(r"(PathRegexp|PathPrefix|Path|Method|Host)\(`([^`]*)`\)")
#: Compose substitutes `${VAR:-default}` and `${VAR:?message}` before
#: Traefik sees a label; the file provider's template, `{{ env "VAR" }}`.
_PUBLIC_HOST_VARIABLE = re.compile(
    r"\$\{ITS_PUBLIC_HOST:\?[^}]*\}|\{\{\s*env\s+\"ITS_PUBLIC_HOST\"\s*\}\}"
)


def _substitute(text: str) -> str:
    text = _PUBLIC_HOST_VARIABLE.sub(PUBLIC_HOST, text)
    return re.sub(r"\$\{[A-Z0-9_]+:-([^}]*)\}", r"\1", text)


def compose(path: Path = COMPOSE) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text())
    return loaded


def dynamic(path: Path = DYNAMIC) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text())
    return loaded


@functools.cache
def routers(path: Path = COMPOSE) -> list[tuple[int, str, str]]:
    """`(priority, name, rule)` for every websecure router in the stack --
    the compose labels' and, beside the committed compose file, the file
    provider's -- highest priority first."""
    found: dict[str, dict[str, str]] = {}
    for service in compose(path)["services"].values():
        labels = service.get("labels") or {}
        if isinstance(labels, list):
            labels = dict(item.split("=", 1) for item in labels)
        for key, value in labels.items():
            match = re.fullmatch(r"traefik\.http\.routers\.([^.]+)\.(\w+)", key)
            if match:
                found.setdefault(match.group(1), {})[match.group(2)] = _substitute(str(value))
    if path == COMPOSE:
        for name, router in (dynamic().get("http", {}).get("routers") or {}).items():
            found[name] = {
                "rule": _substitute(str(router["rule"])),
                "priority": str(router.get("priority") or ""),
                "entrypoints": ",".join(router.get("entryPoints") or []),
            }
    return sorted(
        (
            (int(r.get("priority") or len(r["rule"])), name, r["rule"])
            for name, r in found.items()
            if r.get("entrypoints") == "websecure"
        ),
        reverse=True,
    )


def matches(rule: str, path: str, method: str, host: str = PUBLIC_HOST) -> bool:
    """Evaluate a Traefik rule: host, path and method matchers, `&&`, `||`,
    `!`, parentheses. Any other matcher fails the assertion below, so a
    router using one cannot be silently misread."""

    def one(match: re.Match[str]) -> str:
        kind, arg = match.groups()
        if kind == "PathRegexp":
            hit = re.search(arg, path) is not None
        elif kind == "PathPrefix":
            hit = path.startswith(arg)
        elif kind == "Path":
            hit = path == arg
        elif kind == "Host":
            hit = host.lower() == arg.lower()
        else:
            hit = method == arg
        return f" {hit} "

    expression = _MATCHER.sub(one, rule)
    expression = expression.replace("&&", " and ").replace("||", " or ").replace("!", " not ")
    assert re.fullmatch(r"[\s()]*((True|False|and|or|not)[\s()]*)*", expression), expression
    return bool(eval(expression))  # noqa: S307 -- only True/False/and/or/not remain


def winner(
    path: str,
    compose_file: Path = COMPOSE,
    *,
    host: str = PUBLIC_HOST,
    down: tuple[str, ...] = (),
) -> str:
    """The router Traefik picks, which must be the same for every method.
    `down`: routers whose container is not healthy, which Traefik's Docker
    provider leaves out."""
    winners = set()
    for method in METHODS:
        for _, name, rule in routers(compose_file):
            if name in down:
                continue
            if matches(rule, path, method, host):
                winners.add(name)
                break
        else:
            winners.add("<none>")
    assert len(winners) == 1, (path, winners)
    return winners.pop()
```

Crear `backend/tests/test_deploy_acme.py`:

```python
"""HTTPS from Let's Encrypt, asked for and renewed by Traefik (design
2026-11-23-vps-y-marca-novieri §2; operator decision B3), and the 503 the api's
paths answer while no api container is healthy (§6).

Read from the committed compose file and `traefik/dynamic.yml`, with
`tests/traefik_routes.py`'s model of Traefik's router selection. That the real
Traefik obtains a certificate this way, and answers 503, is the `deploy` job's
to show (deploy/ci/acme-pebble.yml, Task 7) and the rehearsal's (Task 12).
"""

from pathlib import Path

from tests.traefik_routes import COMPOSE, DYNAMIC, compose, dynamic, routers, winner

HOST_RULE = "Host(`${ITS_PUBLIC_HOST:?set in .env}`)"
API_PATHS = (
    "/api/v1/devices",
    "/api/v1/setup/status",
    "/enroll/v1",
    "/i/abc",
    "/mcp",
    "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
    "/.well-known/jwks.json",
    "/oauth/token",
    "/oauth/register",
    "/oauth/revoke",
)


def _traefik() -> dict:
    service: dict = compose()["services"]["traefik"]
    return service


def _labels(name: str) -> dict[str, str]:
    labels: dict[str, str] = compose()["services"][name].get("labels") or {}
    return labels


def test_traefik_asks_lets_encrypt_over_tls_alpn_on_443_and_80_stays_shut() -> None:
    command = _traefik()["command"]
    assert "--certificatesresolvers.le.acme.tlschallenge=true" in command
    assert "--certificatesresolvers.le.acme.storage=/etc/traefik/acme/acme.json" in command
    assert (
        "--certificatesresolvers.le.acme.caserver="
        "${ITS_ACME_CA_SERVER:-https://acme-v02.api.letsencrypt.org/directory}"
    ) in command
    assert any(
        flag.startswith("--certificatesresolvers.le.acme.email=${ITS_ACME_EMAIL:?")
        for flag in command
    ), "compose must refuse to start Traefik without the ACME address"
    assert not [f for f in command if "httpchallenge" in f or "dnschallenge" in f]
    assert not [f for f in command if f.endswith(".address=:80")], ":80 is not opened"
    published = sorted(str(p).rsplit(":", 1)[-1] for p in _traefik()["ports"])
    assert published == ["443", "8443"]


def test_the_certificate_lives_in_its_own_volume_and_nothing_is_copied_in() -> None:
    volumes = _traefik()["volumes"]
    assert "traefik-acme:/etc/traefik/acme" in volumes
    assert "traefik-acme" in compose()["volumes"]
    assert not [v for v in volumes if "certs" in str(v)], "the deploy/certs/ bind mount is gone"
    assert not (COMPOSE.parent / "certs").exists()
    assert "tls" not in dynamic(), "no certificate file is configured any more"
    assert _traefik()["environment"]["ITS_PUBLIC_HOST"] == "${ITS_PUBLIC_HOST:?set in .env}"


def test_every_router_on_443_names_the_host_and_takes_the_resolver() -> None:
    found = 0
    for name in ("api", "web", "minio"):
        labels = _labels(name)
        assert labels[f"traefik.http.routers.{name}.rule"].startswith(HOST_RULE + " && "), name
        assert labels[f"traefik.http.routers.{name}.tls.certresolver"] == "le", name
        assert f"traefik.http.routers.{name}.tls" not in labels, f"{name}: tls is the resolver's"
        found += 1
    router = dynamic()["http"]["routers"]["api-unavailable"]
    assert router["rule"].startswith('Host(`{{ env "ITS_PUBLIC_HOST" }}`) && (')
    assert router["tls"] == {"certResolver": "le"}
    assert {name for _p, name, _r in routers()} == {"api", "api-unavailable", "minio", "web"}
    assert found == 3


def test_a_request_for_another_name_is_routed_nowhere() -> None:
    for path in ("/", "/sign-in", "/api/v1/devices", "/mcp", "/it-suite/agent-releases/x"):
        for host in ("203.0.113.7", "itms.toeshee.com", "evil.example"):
            assert winner(path, host=host) == "<none>", (path, host)


def test_while_the_api_is_down_its_paths_answer_503_not_the_console() -> None:
    for path in API_PATHS:
        assert winner(path) == "api", path
        assert winner(path, down=("api",)) == "api-unavailable", path
    for path in ("/", "/sign-in", "/devices", "/oauth/authorize"):
        assert winner(path, down=("api",)) == "web", path
    service = dynamic()["http"]["services"]["api-unavailable"]
    assert service == {"loadBalancer": {"servers": []}}, "no server: Traefik answers 503"


def test_the_503_router_claims_exactly_the_api_routers_paths() -> None:
    rules = {name: rule for _p, name, rule in routers()}
    assert rules["api-unavailable"] == rules["api"]
    priorities = {name: priority for priority, name, _r in routers()}
    assert priorities["web"] < priorities["api-unavailable"] < priorities["api"]


def test_the_gateway_is_still_tcp_passthrough_on_8443() -> None:
    labels = _labels("gateway")
    assert labels["traefik.tcp.routers.gateway.rule"] == "HostSNI(`*`)"
    assert labels["traefik.tcp.routers.gateway.entrypoints"] == "gateway"
    assert labels["traefik.tcp.routers.gateway.tls.passthrough"] == "true"
    assert not [k for k in labels if "certresolver" in k]


def test_dynamic_yml_is_where_the_model_reads_it() -> None:
    assert DYNAMIC == Path(COMPOSE.parent / "traefik" / "dynamic.yml") and DYNAMIC.is_file()
    assert "./traefik/dynamic.yml:/etc/traefik/dynamic.yml:ro" in _traefik()["volumes"]
```

Y las tres pruebas que fijaban el TLS de antes, desde la raíz de la worktree:

```bash
python3 - <<'PY'
from pathlib import Path

for rel, old, new in (
    (
        "backend/tests/test_public_surface.py",
        '    assert names == {"api", "minio", "web"}, names',
        '    assert names == {"api", "api-unavailable", "minio", "web"}, names',
    ),
    (
        "backend/tests/test_auth_phase4_falsifiers.py",
        '    assert {name for _p, name, _r in routers()} == {"api", "minio", "web"}',
        '    assert {name for _p, name, _r in routers()} == {"api", "api-unavailable", "minio", "web"}',
    ),
    (
        "backend/tests/test_deploy_object_storage_route.py",
        '    assert labels["traefik.http.routers.minio.tls"] == "true"',
        '    assert labels["traefik.http.routers.minio.tls.certresolver"] == "le"',
    ),
):
    path = Path(rel)
    text = path.read_text()
    assert text.count(old) == 1, rel
    path.write_text(text.replace(old, new))
    print("rewrote", rel)
PY
```

- [ ] **Step 2: Las pruebas fallan**

Run: `cd backend && .venv/bin/pytest -q tests/test_deploy_acme.py tests/test_public_surface.py tests/test_auth_phase4_falsifiers.py tests/test_deploy_object_storage_route.py`
Expected: FAIL — en `test_deploy_acme.py`, `--certificatesresolvers.le.acme.tlschallenge=true` no está, `traefik-acme` tampoco, `KeyError: 'traefik.http.routers.api.tls.certresolver'`, `KeyError: 'http'` en `dynamic()`, y `winner(path, host="evil.example")` devuelve `api` (las reglas de hoy no miran el host); en las otras tres, `api-unavailable` no existe y `minio.tls.certresolver` tampoco.

- [ ] **Step 3: La implementación**

Desde la raíz de la worktree:

```bash
python3 - <<'PY'
"""Task 3, Step 3: Let's Encrypt on :443, the host in every router, the 503
while the api is down. Run from the repository root."""
import sys
from pathlib import Path

API_PATHS = (
    "PathPrefix(`/api`) || PathPrefix(`/enroll/`) || PathPrefix(`/i/`) || PathPrefix(`/mcp`)"
    " || PathPrefix(`/.well-known/oauth-protected-resource`)"
    " || PathPrefix(`/.well-known/oauth-authorization-server`)"
    " || PathPrefix(`/.well-known/openid-configuration`) || Path(`/.well-known/jwks.json`)"
    " || Path(`/oauth/token`) || Path(`/oauth/register`) || Path(`/oauth/revoke`)"
)
HOST = "Host(`${ITS_PUBLIC_HOST:?set in .env}`)"

COMPOSE_EDITS = [
    (
        "      - --providers.file.filename=/etc/traefik/dynamic.yml\n",
        "      - --providers.file.filename=/etc/traefik/dynamic.yml\n"
        "      # LET'S ENCRYPT, ASKED FOR AND RENEWED BY TRAEFIK (design\n"
        "      # 2026-11-23-vps-y-marca-novieri §2, operator decision B3). One\n"
        "      # certificate, for the name every router on :443 names in its Host():\n"
        "      # ITS_PUBLIC_HOST. TLS-ALPN-01 is answered on :443 itself, so :80 stays\n"
        "      # shut (\"Traefik must be reachable by Let's Encrypt through port 443\",\n"
        "      # doc.traefik.io), and Traefik renews 30 days before expiry. The CA is\n"
        "      # production unless .env says otherwise: staging for a rehearsal --\n"
        "      # production allows 5 certificates a week for one name -- and Pebble\n"
        "      # in CI (ci/acme-pebble.yml). NOT THE GATEWAY'S: :8443 is TCP\n"
        "      # passthrough, and the gateway presents its own certificate from the\n"
        "      # internal CA, which agents pin (README step 6).\n"
        "      - --certificatesresolvers.le.acme.tlschallenge=true\n"
        "      - --certificatesresolvers.le.acme.email=${ITS_ACME_EMAIL:?set in .env (scripts/new-env.sh asks for it)}\n"
        "      - --certificatesresolvers.le.acme.storage=/etc/traefik/acme/acme.json\n"
        "      - --certificatesresolvers.le.acme.caserver=${ITS_ACME_CA_SERVER:-https://acme-v02.api.letsencrypt.org/directory}\n"
        "    environment:\n"
        "      # Read by traefik/dynamic.yml's `{{ env \"ITS_PUBLIC_HOST\" }}`.\n"
        "      ITS_PUBLIC_HOST: ${ITS_PUBLIC_HOST:?set in .env}\n",
    ),
    (
        "    # `docker compose up` dies with EADDRINUSE and giving ITMS its own IP does\n"
        "    # not help until this is set. ITMS_BIND_IP is that setting: the address\n"
        "    # ITMS answers on, from .env, default 0.0.0.0 = today's behaviour.\n",
        "    # `docker compose up` dies with EADDRINUSE and giving Novieri RMM its own\n"
        "    # IP does not help until this is set. ITMS_BIND_IP is that setting: the\n"
        "    # address Novieri RMM answers on, from .env, default 0.0.0.0 = today's\n"
        "    # behaviour.\n",
    ),
    (
        "      # The operator's real certificate for the :443 entrypoint, as a BIND\n"
        "      # MOUNT of a gitignored directory in the working tree -- see\n"
        "      # traefik/dynamic.yml for the filenames and why this is not a named\n"
        "      # volume. (It replaces a `traefik-certs` named volume that nothing ever\n"
        "      # wrote to: there is no ACME resolver configured, so it only ever held\n"
        "      # nothing, and a `git pull` deployment cannot put a file inside it.)\n"
        "      - ./certs:/etc/traefik/certs:ro\n",
        "      # The ACME account key and the certificates Traefik obtained\n"
        "      # (acme.json, which Traefik writes 0600). A named volume, since\n"
        "      # 2026-11-23 in place of the hand-copied deploy/certs/: nothing on the\n"
        "      # host writes it, and losing it costs one new certificate, not data\n"
        "      # (GO-LIVE.md §6).\n"
        "      - traefik-acme:/etc/traefik/acme\n",
    ),
    (
        f"      traefik.http.routers.api.rule: {API_PATHS.replace(chr(10), '')}\n",
        f"      traefik.http.routers.api.rule: {HOST} && ({API_PATHS})\n",
    ),
    ('      traefik.http.routers.api.tls: "true"\n', "      traefik.http.routers.api.tls.certresolver: le\n"),
    (
        "      traefik.http.routers.web.rule: PathPrefix(`/`)\n",
        f"      traefik.http.routers.web.rule: {HOST} && PathPrefix(`/`)\n",
    ),
    ('      traefik.http.routers.web.tls: "true"\n', "      traefik.http.routers.web.tls.certresolver: le\n"),
    (
        "      # and one certificate; `objects.itms.toeshee.com` would need a new DNS\n",
        "      # and one certificate; `objects.rmm.novieri.com` would need a new DNS\n",
    ),
    (
        "      # `https://itms.toeshee.com/it-suite/<key>` is already exactly S3\n",
        "      # `https://rmm.novieri.com/it-suite/<key>` is already exactly S3\n",
    ),
    (
        "      traefik.http.routers.minio.rule: >-\n"
        "        (PathPrefix(`/${ITS_S3_BUCKET:-it-suite}/`)\n"
        "        && !Path(`/${ITS_S3_BUCKET:-it-suite}/`)\n"
        "        && (Method(`GET`) || Method(`HEAD`)))\n"
        "        || (Path(`/${ITS_S3_BUCKET:-it-suite}`) && Method(`POST`))\n",
        "      traefik.http.routers.minio.rule: >-\n"
        f"        {HOST}\n"
        "        && ((PathPrefix(`/${ITS_S3_BUCKET:-it-suite}/`)\n"
        "        && !Path(`/${ITS_S3_BUCKET:-it-suite}/`)\n"
        "        && (Method(`GET`) || Method(`HEAD`)))\n"
        "        || (Path(`/${ITS_S3_BUCKET:-it-suite}`) && Method(`POST`)))\n",
    ),
    ('      traefik.http.routers.minio.tls: "true"\n', "      traefik.http.routers.minio.tls.certresolver: le\n"),
    (
        "  beat-schedule:\n",
        "  beat-schedule:\n"
        "  # Traefik's ACME account and certificates (design 2026-11-23 §2).\n"
        "  # Re-obtainable, so not in the backup set: GO-LIVE.md §6.\n"
        "  traefik-acme:\n",
    ),
]

DYNAMIC = """\
# The operator-facing entrypoint's (:443) configuration that no container's
# labels can carry. A Go template: Traefik's file provider fills
# `{{ env "ITS_PUBLIC_HOST" }}` from traefik's environment (docker-compose.yml).
#
# THE CERTIFICATE IS NOT CONFIGURED HERE ANY MORE (design
# 2026-11-23-vps-y-marca-novieri §2). Until 2026-11-23 this file pointed
# Traefik at deploy/certs/, copied in by hand, and a missing file made it serve
# its own self-signed default without a word. Traefik now asks Let's Encrypt
# for the certificate of ITS_PUBLIC_HOST itself (the `le` resolver) and keeps
# it in the traefik-acme volume; scripts/check-public-tls.sh says, after a
# start, whether a client would accept what :443 presents.
#
# NOT THE AGENT-FACING TLS EITHER. :8443 is TCP passthrough -- the gateway
# terminates mutual TLS itself, with a certificate from the INTERNAL CA
# (scripts/issue-gateway-cert.py), which agents pin at enrollment. Public
# certificate on :443, internal CA on :8443, and the two never swap.
#
# WHILE THE API IS DOWN, ITS PATHS ANSWER 503 (design §6). Traefik's Docker
# provider leaves out a container that is stopped or not yet healthy, and its
# router with it; until 2026-11-23 a request for /api or /mcp then fell to the
# console's catch-all (`web`, priority 1) and got the console's index.html
# with a 200 -- HTML where an AI app and the console's own client expect
# JSON. `api-unavailable` claims exactly the api router's paths
# (backend/tests/test_deploy_acme.py holds the two rules equal) at priority 2:
# above `web`, below `api`, whose default priority is its rule's length. Its
# service has no server, which Traefik answers "503 no available server"
# (measured on Traefik 3.6.25, 2026-11-23).
http:
  routers:
    api-unavailable:
      rule: Host(`{{ env "ITS_PUBLIC_HOST" }}`) && (""" + API_PATHS + """)
      entryPoints: [websecure]
      priority: 2
      service: api-unavailable
      tls:
        certResolver: le
  services:
    api-unavailable:
      loadBalancer:
        servers: []
"""

OTHER_EDITS = [
    (
        "deploy/minio/Dockerfile",
        'LABEL org.opencontainers.image.title="minio (built from source for ITMS)" \\\n',
        'LABEL org.opencontainers.image.title="minio (built from source for Novieri RMM)" \\\n',
    ),
    (
        ".gitignore",
        "# Operator TLS material for the Traefik :443 entrypoint (deploy/certs/).\n"
        "# The deployment is a `git pull` on the host, so this directory is a bind\n"
        "# mount the operator drops real certificates into -- they must never be\n"
        "# committed. deploy/certs/.gitkeep keeps the directory itself in the tree so\n"
        "# a fresh clone has somewhere to put them.\n"
        "deploy/certs/*\n"
        "!deploy/certs/.gitkeep\n",
        "# Operator TLS material that a host deployed before 2026-11-23 copied into\n"
        "# deploy/certs/ by hand. Nothing reads the directory since Traefik asks\n"
        "# Let's Encrypt itself (design 2026-11-23-vps-y-marca-novieri §2), but a\n"
        "# private key left there must still never be committed.\n"
        "deploy/certs/\n",
    ),
]

compose = Path("deploy/docker-compose.yml")
text = compose.read_text()
for old, new in COMPOSE_EDITS:
    if text.count(old) != 1:
        sys.exit(f"docker-compose.yml: not there exactly once: {old[:70]!r}")
    text = text.replace(old, new)
compose.write_text(text)
Path("deploy/traefik/dynamic.yml").write_text(DYNAMIC)
for rel, old, new in OTHER_EDITS:
    path = Path(rel)
    body = path.read_text()
    if body.count(old) != 1:
        sys.exit(f"{rel}: not there exactly once: {old[:70]!r}")
    path.write_text(body.replace(old, new))
print("rewrote deploy/docker-compose.yml, deploy/traefik/dynamic.yml, deploy/minio/Dockerfile, .gitignore")
PY
git rm -q deploy/certs/.gitkeep
```

Expected: `rewrote deploy/docker-compose.yml, deploy/traefik/dynamic.yml, deploy/minio/Dockerfile, .gitignore`, y `ls deploy/certs` responde `No such file or directory`.

- [ ] **Step 4: Las pruebas pasan, y compose acepta y rechaza lo que debe**

Run (desde `backend/`): `.venv/bin/pytest -q tests/test_deploy_acme.py tests/test_public_surface.py tests/test_deploy_mcp_route.py tests/test_auth_jwks.py tests/test_auth_phase4_falsifiers.py tests/test_deploy_object_storage_route.py tests/test_deploy_install_link.py tests/test_deploy_env_documentation.py && .venv/bin/ruff check tests`
Expected: `65 passed` (en una copia de `1bc0e5e`), `All checks passed!`.

Y compose, desde `deploy/`, con un `.env` de usar y tirar (compose lee `.env` como `env_file` de los servicios, así que `--env-file` solo no basta):

```bash
printf 'ITS_PUBLIC_HOST=rmm.novieri.com\nITS_ACME_EMAIL=ops@novieri.example\nPOSTGRES_ADMIN_PASSWORD=x\nAPP_DB_PASSWORD=x\nPORTAL_DB_PASSWORD=x\nMINIO_ROOT_USER=m\nMINIO_ROOT_PASSWORD=xxxxxxxx\nITS_OPENBAO_TOKEN=x\nOPENBAO_TOKEN=x\n' > .env
docker compose config -q && echo CONFIG-OK
sed -i '/^ITS_ACME_EMAIL=/d' .env && docker compose config -q; echo "exit $?"
sed -i '/^ITS_PUBLIC_HOST=/d' .env && echo 'ITS_ACME_EMAIL=ops@novieri.example' >> .env && docker compose config -q; echo "exit $?"
rm -f .env
```

Expected:

```
CONFIG-OK
error while interpolating services.traefik.command.[]: required variable ITS_ACME_EMAIL is missing a value: set in .env (scripts/new-env.sh asks for it)
exit 1
error while interpolating services.api.labels.traefik.http.routers.api.rule: required variable ITS_PUBLIC_HOST is missing a value: set in .env
exit 1
```

(Las dos primeras líneas, medidas el 2026-11-23 con Compose v5.7.1; la tercera puede nombrar otro de los servicios que interpolan `ITS_PUBLIC_HOST`.)

- [ ] **Step 5: Commit**

```bash
git add -A deploy/docker-compose.yml deploy/traefik/dynamic.yml deploy/minio/Dockerfile .gitignore deploy/certs backend/tests/traefik_routes.py backend/tests/test_deploy_acme.py backend/tests/test_public_surface.py backend/tests/test_auth_phase4_falsifiers.py backend/tests/test_deploy_object_storage_route.py
git commit -m "feat(deploy): Let's Encrypt over TLS-ALPN-01 on :443, the host in every router, 503 while the api is down"
```

---

### Task 4: `new-env.sh`, la comprobación previa y la del certificado

**Files:**
- Create: `deploy/scripts/new-env.sh`, `deploy/scripts/check-public-tls.sh` (ejecutables, `chmod +x`)
- Modify (reemplazado entero): `deploy/scripts/preflight.sh`
- Modify: `deploy/.env.example` (las tres claves nuevas, `ITS_PUBLIC_CONSOLE_URL=https://your-host`, `ITS_DEFAULT_ORG_SLUG=novieri`, cinco comentarios con «ITMS» o el host viejo)
- Modify: `deploy/scripts/deploy.sh:705,709` (el host de `.env`, el mensaje de la sonda, que ya no puede ser un 404, y la comprobación del certificado), `deploy/scripts/host-check.sh` (13 «ITMS» y «an ITMS»), `deploy/scripts/install-backup-timer.sh` (4 `Description=`)
- Modify: `.gitignore:12` (una línea nueva, `deploy/.env.new.*`, tras `.env`; la Task 3 toca otro trozo del mismo archivo, `:28-34`: notas de integración)
- Create: `backend/tests/test_new_env_script.py`, `backend/tests/test_preflight_script.py`, `backend/tests/test_check_public_tls.py`
- Modify: `backend/tests/test_deploy_script_steps.py` (`test_the_preflight_checks_the_certificate_against_the_console_address` → `test_the_preflight_takes_the_console_host_from_its_address`, y una prueba nueva al final), `backend/tests/test_deploy_install_link.py:56,62`

**Interfaces:**
- Consumes: nada. **Se fusiona junto con las Tasks 3 y 7** (la 3 hace que compose exija las claves que esta añade).
- Produces:
  - `./scripts/new-env.sh --host <nombre DNS> [--acme-email <dirección>] [--acme-ca-server <URL https>]` desde `deploy/`: escribe `deploy/.env` (0600) o sale 1 sin escribir nada; sin `--acme-email` y con terminal, la pregunta («Let's Encrypt account address (the certificate's contact): »); sin terminal, se niega.
  - `./scripts/check-public-tls.sh [--connect <IP>] [--cafile <PEM>] [--wait <s>] [--port <n>]`: sale 0 e imprime `ok      <ip>:<puerto> presents a trusted certificate for <host>` con emisor, SAN y `notAfter`, o sale 1 con una línea `REFUSE` en stderr; también cuando el certificado es bueno pero le quedan **menos de 28 días** (la renovación lleva días fallando). Lo usan `deploy.sh` (esta tarea), la CI (Tasks 7 y 10), el runbook (Task 9) y el ensayo (Task 12).
  - `preflight.sh`: líneas `ok`, `note` y `REFUSE` como hasta ahora; las nuevas están en la prueba.
  - `.env.example`: `ITS_PUBLIC_HOST=your-host`, `ITS_ACME_EMAIL=`, `ITS_ACME_CA_SERVER=https://acme-v02.api.letsencrypt.org/directory`.

**Decisiones de la tarea.**
- **Los dos tokens de OpenBao siguen en `change-me`** (P4-B-04). El diseño dice «un secreto aleatorio nuevo en cada clave `change-me`»; su propósito es que no quede nada que sustituir a mano, y estos dos no se sustituyen a mano: los escribe el paso 3.5 con `read -rsp`, sin marcadores. Un valor aleatorio ahí convertiría «te saltaste el 3.5» en una API que falla con 403 de OpenBao, y `preflight.sh` dejaría de avisarlo.
- **`openssl rand -hex 24`**: 48 caracteres hexadecimales, seguros dentro de las URLs de base de datos que los interpolan (`postgresql+asyncpg://app_user:${APP_DB_PASSWORD}@…`); el `base64 | tr -d '=+/'` del runbook de antes da longitudes variables.
- **El enlace duro** (`ln`) en lugar de `mv`: nunca reemplaza un `.env` que haya aparecido mientras corría.
- **La comprobación del DNS pregunta al DNS, como Let's Encrypt, y no a `/etc/hosts`.** El registro `A`, con `getent -s dns ahostsv4` (sin `-s dns`, glibc lee antes `/etc/hosts`, donde cloud-init escribe `127.0.1.1 <nombre del host>`: una negativa falsa si el VPS se llama `rmm.novieri.com`, y una línea puesta a mano tapa un registro `A` que falta o es otro). El `AAAA`, con `getaddrinfo(AF_INET6)` de Python **sin** `AI_ADDRCONFIG`: `getent ahosts` lo pone, y en un host sin IPv6 propia —el VPS que construye el runbook— no enseña ningún `AAAA` (medido en la revisión previa con glibc 2.39, la de Ubuntu 24.04). Python viene con Ubuntu Server; sin él, se anota. Y `|| true` en las dos: con `set -o pipefail`, el `exit 2` de `getent` ante un nombre que no resuelve hacía morir el guion en la asignación, sin imprimir la negativa (el *stub* de la prueba sale ahora con `2`, como `getent`). Se niega si el nombre resuelve a una dirección que no es de este host y este tiene alguna pública; si todas las suyas son privadas (un VPS tras NAT) lo anota y no se niega.
- **Ningún `AAAA`, por la razón verdadera** (resolución del coordinador del 2026-11-24): el despliegue es de una IPv4; nada comprueba que el 443 y el 8443 contesten por IPv6, y un `AAAA` manda a los navegadores y a los agentes, que prueban IPv6 primero, a una dirección que nadie ha comprobado, o a otra máquina. **No** porque Let's Encrypt no vuelva al IPv4: su validador de TLS-ALPN-01 vuelve ante cualquier error de IPv6 (Boulder, `va/tlsalpn.go:105-137` [E5]); el borrador decía lo contrario, citando una página de HTTP-01.
- **Los puertos ocupados por el Traefik de este proyecto** no son una negativa: `preflight.sh` sirve también para volver a mirar un host en marcha.
- **`check-public-tls.sh` confía en las raíces del sistema** salvo `--cafile`: un certificado de staging o de Pebble es, a propósito, no confiable, y lo dice.
- **Y se niega si al certificado le quedan menos de 28 días.** Traefik renueva cuando quedan menos de 30 y lo mira cada 24 h (`provider.go`, v3.6 [E16]): uno sano nunca baja de unos 29. Menos de 28 es una renovación que lleva días fallando, y nada más lo diría: Let's Encrypt ya no manda avisos de caducidad [E7], y con los certificados de 45 días de 2028 [E6] un fallo silencioso deja 15. El `preflight.sh` de antes avisaba a 7 días (`-checkend 604800`); este, a 28.
- **El temporal de `new-env.sh` no se puede subir a git**: `.env.new.XXXXXX` vive en `deploy/` (el enlace duro necesita el mismo sistema de archivos) y lleva los cuatro secretos nuevos; la trampa `EXIT` lo borra, pero un `SIGKILL` o un corte de luz lo dejan. `.gitignore` solo ignoraba `.env`; la línea nueva `deploy/.env.new.*` y una prueba (`git check-ignore`) lo cubren.
- **`preflight.sh` propone `openssl rand -hex 24`**, no `-base64 24`: el base64 trae `/` y `+`, que rompen las URLs de base de datos que interpolan esas claves (la razón de `new-env.sh`, arriba).

- [ ] **Step 0: La worktree**

Run, desde la raíz de la worktree, la comprobación de Global Constraints («Pistas en paralelo») y la de esta oleada:

```bash
git fetch -q origin claude/compassionate-bardeen-vgtwus
git log --oneline -1
git merge-base --is-ancestor HEAD origin/claude/compassionate-bardeen-vgtwus && echo ON-PROJECT-BRANCH
git merge-base --is-ancestor 6a14dbc HEAD && echo HAS-PR-13
git diff --quiet 1bc0e5e HEAD -- backend agent deploy .github scripts && echo CODE-AS-MEASURED
```

Expected: `21e72ec docs(plan): project 3 plan, pre-flight fixes in progress (snapshot)` (o el commit posterior de `origin/claude/compassionate-bardeen-vgtwus` del que el coordinador creó la worktree), `ON-PROJECT-BRANCH`, `HAS-PR-13`, `CODE-AS-MEASURED`. Si la primera línea es `dd062f8 Merge pull request #12 …` o falta alguna de las otras tres, se para y se avisa al coordinador. Después, la preparación de Global Constraints (`apt-get install -y iproute2` incluido).

- [ ] **Step 1: Las pruebas**

Crear `backend/tests/test_new_env_script.py`:

```python
"""`deploy/scripts/new-env.sh`: the `.env` of a new host, with nothing left to
substitute by hand (design 2026-11-23-vps-y-marca-novieri §3; `CLAUDE.md`, "A
copy-pasteable command contains no placeholders").

Run for real, against the repository's own `.env.example`, in a scratch copy
of `deploy/`. What it must do: a fresh random value in every `change-me` but
the two OpenBao tokens, the host in every URL, the ACME keys set, mode 0600 --
and print none of what it generated. What it must refuse: an existing `.env`,
a host that is not a DNS name, an address that is not one.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "scripts" / "new-env.sh"
EXAMPLE = REPO / "deploy" / ".env.example"
OPENBAO_TOKENS = {"ITS_OPENBAO_TOKEN", "OPENBAO_TOKEN"}


def _deploy(tmp_path: Path) -> Path:
    deploy = tmp_path / "deploy"
    (deploy / "scripts").mkdir(parents=True)
    (deploy / "scripts" / SCRIPT.name).write_bytes(SCRIPT.read_bytes())
    (deploy / ".env.example").write_bytes(EXAMPLE.read_bytes())
    return deploy


def _run(deploy: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(deploy / "scripts" / SCRIPT.name), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _settings(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )


def _placeholders() -> set[str]:
    return {key for key, value in _settings(EXAMPLE).items() if value == "change-me"}


ARGS = ("--host", "rmm.novieri.com", "--acme-email", "ops@novieri.example")


def test_the_example_still_has_what_the_script_fills() -> None:
    """Non-vacuity: the six `change-me` and the three ACME keys this task added."""
    example = _settings(EXAMPLE)
    assert _placeholders() == OPENBAO_TOKENS | {
        "POSTGRES_ADMIN_PASSWORD",
        "APP_DB_PASSWORD",
        "PORTAL_DB_PASSWORD",
        "MINIO_ROOT_PASSWORD",
    }
    assert example["ITS_PUBLIC_HOST"] == "your-host"
    assert example["ITS_ACME_EMAIL"] == ""
    assert example["ITS_ACME_CA_SERVER"] == "https://acme-v02.api.letsencrypt.org/directory"
    assert example["ITS_PUBLIC_CONSOLE_URL"] == "https://your-host"
    assert example["ITS_DEFAULT_ORG_SLUG"] == "novieri"
    assert "https://acme-staging-v02.api.letsencrypt.org/directory" in EXAMPLE.read_text()


def test_a_new_env_has_fresh_secrets_the_host_and_mode_600(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path)
    run = _run(deploy, *ARGS)
    assert run.returncode == 0, run.stderr
    env = deploy / ".env"
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    settings = _settings(env)
    for key in _placeholders() - OPENBAO_TOKENS:
        assert re.fullmatch(r"[0-9a-f]{48}", settings[key]), key
    for key in OPENBAO_TOKENS:
        assert settings[key] == "change-me", f"{key} is OpenBao's to issue (README step 3)"
    assert settings["ITS_PUBLIC_HOST"] == "rmm.novieri.com"
    assert settings["ITS_PUBLIC_CONSOLE_URL"] == "https://rmm.novieri.com"
    assert settings["ITS_GATEWAY_URL"] == "wss://rmm.novieri.com:8443/agent"
    assert settings["ITS_GATEWAY_RELAY_URL"] == "wss://rmm.novieri.com:8443/relay"
    assert settings["ITS_S3_PUBLIC_ENDPOINT"] == "https://rmm.novieri.com"
    assert settings["ITS_ACME_EMAIL"] == "ops@novieri.example"
    assert settings["ITS_ACME_CA_SERVER"] == "https://acme-v02.api.letsencrypt.org/directory"
    assert not [k for k, v in settings.items() if "your-host" in v]
    # Everything else is the example's, byte for byte, comments included.
    example_lines = EXAMPLE.read_text().splitlines()
    env_lines = env.read_text().splitlines()
    assert len(env_lines) == len(example_lines)
    assert [line for line in env_lines if line.startswith("#")] == [
        line for line in example_lines if line.startswith("#")
    ]


def test_nothing_it_generated_is_printed(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path)
    run = _run(deploy, *ARGS)
    settings = _settings(deploy / ".env")
    secrets = [settings[key] for key in _placeholders() - OPENBAO_TOKENS]
    assert len(secrets) == 4
    for secret in secrets:
        assert secret not in run.stdout and secret not in run.stderr
    for key in _placeholders() - OPENBAO_TOKENS:
        assert key in run.stdout, "it names the keys it generated"


def test_two_hosts_get_two_sets_of_secrets(tmp_path: Path) -> None:
    first, second = _deploy(tmp_path / "a"), _deploy(tmp_path / "b")
    _run(first, *ARGS)
    _run(second, *ARGS)
    a, b = _settings(first / ".env"), _settings(second / ".env")
    assert a["POSTGRES_ADMIN_PASSWORD"] != b["POSTGRES_ADMIN_PASSWORD"]


def test_an_existing_env_is_never_overwritten(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path)
    (deploy / ".env").write_text("POSTGRES_ADMIN_PASSWORD=the-live-one\n")
    run = _run(deploy, *ARGS)
    assert run.returncode == 1
    assert ".env already exists" in run.stderr
    assert (deploy / ".env").read_text() == "POSTGRES_ADMIN_PASSWORD=the-live-one\n"
    assert [p.name for p in deploy.iterdir() if p.name.startswith(".env.new")] == []


@pytest.mark.parametrize(
    "host",
    ["10.0.0.1", "RMM.novieri.com", "https://rmm.novieri.com", "rmm", "rmm.novieri.com:443", ""],
)
def test_a_host_that_is_not_a_dns_name_is_refused(tmp_path: Path, host: str) -> None:
    deploy = _deploy(tmp_path)
    run = _run(deploy, "--host", host, "--acme-email", "ops@novieri.example")
    assert run.returncode == 1
    assert not (deploy / ".env").exists()


@pytest.mark.parametrize("email", ["ops", "ops@novieri", "ops novieri@x.com", "@novieri.com"])
def test_an_address_that_is_not_one_is_refused(tmp_path: Path, email: str) -> None:
    deploy = _deploy(tmp_path)
    run = _run(deploy, "--host", "rmm.novieri.com", "--acme-email", email)
    assert run.returncode == 1
    assert "name@domain.tld" in run.stderr
    assert not (deploy / ".env").exists()


def test_without_a_terminal_the_address_must_be_given(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path)
    run = _run(deploy, "--host", "rmm.novieri.com")
    assert run.returncode == 1
    assert "--acme-email is required when stdin is not a terminal" in run.stderr


def test_a_rehearsal_names_its_ca(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path)
    staging = "https://acme-staging-v02.api.letsencrypt.org/directory"
    run = _run(deploy, *ARGS, "--acme-ca-server", staging)
    assert run.returncode == 0, run.stderr
    assert _settings(deploy / ".env")["ITS_ACME_CA_SERVER"] == staging
    refused = _run(_deploy(tmp_path / "x"), *ARGS, "--acme-ca-server", "http://pebble:14000/dir")
    assert refused.returncode == 1 and "must be an https URL" in refused.stderr


def test_it_runs_where_the_runbook_runs_it_and_nowhere_else(tmp_path: Path) -> None:
    """`cd "$(dirname "$0")/.."`: from any directory it writes deploy/.env."""
    deploy = _deploy(tmp_path)
    run = subprocess.run(
        ["bash", "deploy/scripts/new-env.sh", *ARGS],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env={**os.environ},
    )
    assert run.returncode == 0, run.stderr
    assert (deploy / ".env").is_file() and not (tmp_path / ".env").exists()


def test_the_temporary_file_a_crash_leaves_is_never_committable() -> None:
    """The four new secrets sit in `.env.new.XXXXXX`, in deploy/, until the
    hard link; the EXIT trap removes it, but a SIGKILL or a power cut does
    not run traps, and `git add -A` would take what is left."""
    for name in ("deploy/.env.new.Ab12Cd", "deploy/.env.new.zZ9yY8"):
        run = subprocess.run(
            ["git", "-C", str(REPO), "check-ignore", "-q", name], capture_output=True
        )
        assert run.returncode == 0, f"{name} is not gitignored"
```

Crear `backend/tests/test_preflight_script.py`:

```python
"""`deploy/scripts/preflight.sh`, run for real against a stubbed host
(design 2026-11-23-vps-y-marca-novieri §2, §3).

Since 2026-11-23 there is no certificate file to check: Traefik asks Let's
Encrypt for one at the first start. What would make that request fail -- or
succeed for the wrong machine -- is checked before it is made: the public
name is the console's, it resolves to this host and has no AAAA record, 443
and 8443 are free, and Docker is new enough. `getent`, `python3`, `ip`, `ss`
and `docker` are stubs on PATH, each answering what the test sets; nothing
here resolves a name or opens a socket.

The name is asked of DNS alone, as Let's Encrypt asks it: the `getent` stub
answers `-s dns` from FAKE_V4 and, without it, lets /etc/hosts
(FAKE_HOSTS_FILE) answer first, as `hosts: files dns` does; and, like glibc's
`getent ahosts` (AI_ADDRCONFIG), it shows no AAAA on a host with no IPv6
address of its own. The AAAA is asked through `python3` (getaddrinfo with no
AI_ADDRCONFIG), whose stub answers FAKE_V6 whatever the host holds.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PREFLIGHT = REPO / "deploy" / "scripts" / "preflight.sh"

GOOD_ENV = {
    "IT_SUITE_VERSION": "1bc0e5e",
    "ITMS_BIND_IP": "0.0.0.0",
    "ITS_PUBLIC_HOST": "rmm.novieri.com",
    "ITS_PUBLIC_CONSOLE_URL": "https://rmm.novieri.com",
    "ITS_ACME_EMAIL": "ops@novieri.example",
    "ITS_ACME_CA_SERVER": "https://acme-v02.api.letsencrypt.org/directory",
    "POSTGRES_ADMIN_PASSWORD": "x" * 48,
    "ITS_OPENBAO_TOKEN": "change-me",
    "OPENBAO_TOKEN": "change-me",
}
GOOD_HOST = {
    "FAKE_V4": "203.0.113.10",
    "FAKE_V6": "",
    "FAKE_MINE": "203.0.113.10/24 172.17.0.1/16",
    "FAKE_LISTEN": "",
    "FAKE_TRAEFIK": "",
    "FAKE_ENGINE": "29.9.3",
    "FAKE_COMPOSE": "5.7.1",
}

STUBS = {
    "getent": """#!/bin/sh
if [ "$1" = "-s" ]; then
  [ "$2" = "dns" ] || exit 1
  shift 2
  found="$FAKE_V4"
else
  found="${FAKE_HOSTS_FILE:-$FAKE_V4}"
fi
case "$1" in
  ahostsv4) ;;
  ahosts) case " $FAKE_MINE " in *:*) found="$found $FAKE_V6" ;; esac ;;
  *) exit 1 ;;
esac
# Like glibc's getent: nothing found is exit 2.
[ -n "$(echo $found)" ] || exit 2
for a in $found; do echo "$a       STREAM $2"; done
""",
    "python3": """#!/bin/sh
echo $FAKE_V6
""",
    "ip": """#!/bin/sh
case "$*" in
  "-brief address") for a in $FAKE_MINE; do echo "eth0 UP $a"; done ;;
  *) for a in $FAKE_MINE; do
       echo "2: eth0    inet $a brd 0 scope global eth0"
     done ;;
esac
""",
    "ss": """#!/bin/sh
for a in $FAKE_LISTEN; do echo "LISTEN 0 4096 $a 0.0.0.0:*"; done
""",
    "docker": """#!/bin/sh
case "$*" in
  "version --format {{.Server.Version}}") [ -n "$FAKE_ENGINE" ] && echo "$FAKE_ENGINE" || exit 1 ;;
  "compose version --short") [ -n "$FAKE_COMPOSE" ] && echo "$FAKE_COMPOSE" || exit 1 ;;
  "compose ps --status running -q traefik") [ -n "$FAKE_TRAEFIK" ] && echo "$FAKE_TRAEFIK" ;;
  *) exit 1 ;;
esac
""",
}


def _run(
    tmp_path: Path, env: dict[str, str], host: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    deploy = tmp_path / "repo" / "deploy"
    (deploy / "scripts").mkdir(parents=True, exist_ok=True)
    (deploy / "scripts" / "preflight.sh").write_bytes(PREFLIGHT.read_bytes())
    (tmp_path / "repo" / ".gitignore").write_text(".env\n")
    subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
    dotenv = deploy / ".env"
    dotenv.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    dotenv.chmod(0o600)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in STUBS.items():
        stub = bin_dir / name
        stub.write_text(body)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return subprocess.run(
        ["bash", str(deploy / "scripts" / "preflight.sh")],
        capture_output=True,
        text=True,
        env={**os.environ, **host, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )


def test_a_host_ready_for_its_first_start_passes(tmp_path: Path) -> None:
    run = _run(tmp_path, GOOD_ENV, GOOD_HOST)
    assert run.returncode == 0, run.stdout + run.stderr
    for line in (
        "ok      ITS_PUBLIC_HOST=rmm.novieri.com is the console's host",
        "ok      rmm.novieri.com resolves to 203.0.113.10, an address of this host",
        "ok      443 and 8443 are free",
        "ok      Docker Engine 29.9.3",
        "ok      Docker Compose 5.7.1",
        "ok      ACME CA: Let's Encrypt production",
    ):
        assert line in run.stdout, run.stdout
    assert "certs/" not in run.stdout, "the certificate files are gone (design §2)"


@pytest.mark.parametrize(
    ("env", "host", "refusal"),
    [
        pytest.param(
            {"ITS_PUBLIC_HOST": "your-host"},
            {},
            "ITS_PUBLIC_HOST is 'your-host'",
            id="placeholder-host",
        ),
        pytest.param(
            {"ITS_PUBLIC_CONSOLE_URL": "https://console.novieri.com"},
            {},
            "is not the host of ITS_PUBLIC_CONSOLE_URL ('console.novieri.com')",
            id="host-differs-from-console",
        ),
        pytest.param({"ITS_ACME_EMAIL": ""}, {}, "ITS_ACME_EMAIL is empty", id="no-acme-email"),
        pytest.param(
            {"ITS_ACME_CA_SERVER": "http://pebble:14000/dir"},
            {},
            "is not an https URL",
            id="http-ca",
        ),
        pytest.param({}, {"FAKE_V4": ""}, "does not resolve to an IPv4 address", id="no-a-record"),
        pytest.param(
            {},
            {"FAKE_V4": "198.51.100.7"},
            "which is not an address of this host",
            id="a-elsewhere",
        ),
        pytest.param(
            {}, {"FAKE_V6": "2001:db8::7"}, "has an AAAA record (2001:db8::7)", id="aaaa-record"
        ),
        pytest.param(
            {},
            {"FAKE_LISTEN": "0.0.0.0:443"},
            "something already listens on 0.0.0.0:443",
            id="443-held",
        ),
        pytest.param(
            {}, {"FAKE_ENGINE": "24.0.7"}, "Docker Engine 24.0.7 is older than 26", id="old-engine"
        ),
        pytest.param(
            {},
            {"FAKE_COMPOSE": "2.20.2"},
            "Docker Compose 2.20.2 is older than 2.24",
            id="old-compose",
        ),
        pytest.param({}, {"FAKE_ENGINE": ""}, "Docker Engine does not answer", id="no-docker"),
    ],
)
def test_each_state_that_would_fail_the_certificate_is_refused(
    tmp_path: Path, env: dict[str, str], host: dict[str, str], refusal: str
) -> None:
    run = _run(tmp_path, {**GOOD_ENV, **env}, {**GOOD_HOST, **host})
    assert run.returncode == 1, run.stdout
    assert "REFUSE" in run.stdout and refusal in run.stdout, run.stdout


def test_this_stacks_own_traefik_holding_the_ports_is_not_a_refusal(tmp_path: Path) -> None:
    run = _run(
        tmp_path, GOOD_ENV, {**GOOD_HOST, "FAKE_LISTEN": "0.0.0.0:443", "FAKE_TRAEFIK": "c0ffee"}
    )
    assert run.returncode == 0, run.stdout
    assert "ok      443 and 8443 are held by this stack's traefik" in run.stdout


def test_a_bound_address_is_the_one_the_name_must_resolve_to(tmp_path: Path) -> None:
    env = {**GOOD_ENV, "ITMS_BIND_IP": "203.0.113.20"}
    host = {**GOOD_HOST, "FAKE_MINE": "203.0.113.10/24 203.0.113.20/24"}
    run = _run(tmp_path, env, host)
    assert (
        run.returncode == 1 and "which is not an address of this host (203.0.113.20)" in run.stdout
    )
    run = _run(tmp_path, env, {**host, "FAKE_V4": "203.0.113.20"})
    assert run.returncode == 0, run.stdout


def test_behind_nat_the_address_cannot_be_compared_and_it_says_so(tmp_path: Path) -> None:
    run = _run(
        tmp_path, GOOD_ENV, {**GOOD_HOST, "FAKE_V4": "198.51.100.7", "FAKE_MINE": "10.0.0.4/24"}
    )
    assert run.returncode == 0, run.stdout
    assert "every address of this host is private (10.0.0.4)" in run.stdout


def test_a_rehearsal_ca_is_noted_not_refused(tmp_path: Path) -> None:
    staging = "https://acme-staging-v02.api.letsencrypt.org/directory"
    run = _run(tmp_path, {**GOOD_ENV, "ITS_ACME_CA_SERVER": staging}, GOOD_HOST)
    assert run.returncode == 0, run.stdout
    assert f"note    ACME CA is {staging}" in run.stdout


def test_an_etc_hosts_line_for_the_name_is_not_what_lets_encrypt_sees(tmp_path: Path) -> None:
    """cloud-init writes `127.0.1.1 <this host's name>` into /etc/hosts: no
    false refusal when the VPS is named rmm.novieri.com. And a line written
    by hand does not stand in for the public A record either."""
    run = _run(tmp_path, GOOD_ENV, {**GOOD_HOST, "FAKE_HOSTS_FILE": "127.0.1.1"})
    assert run.returncode == 0, run.stdout
    assert "ok      rmm.novieri.com resolves to 203.0.113.10, an address of this host" in run.stdout
    run = _run(tmp_path, GOOD_ENV, {**GOOD_HOST, "FAKE_HOSTS_FILE": "203.0.113.10", "FAKE_V4": ""})
    assert run.returncode == 1, run.stdout
    assert "does not resolve to an IPv4 address" in run.stdout, run.stdout


def test_an_aaaa_record_is_seen_on_a_host_with_no_ipv6_of_its_own(tmp_path: Path) -> None:
    """The VPS this runbook builds has one IPv4 address and no IPv6, the host
    on which glibc's getent hides every AAAA (AI_ADDRCONFIG)."""
    host = {**GOOD_HOST, "FAKE_V6": "2001:db8::7", "FAKE_MINE": "203.0.113.10/24"}
    run = _run(tmp_path, GOOD_ENV, host)
    assert run.returncode == 1, run.stdout
    assert "has an AAAA record (2001:db8::7)" in run.stdout, run.stdout
```

Crear `backend/tests/test_check_public_tls.py`:

```python
"""`deploy/scripts/check-public-tls.sh` against real TLS servers (design
2026-11-23-vps-y-marca-novieri §2: "if ACME fails, the check after start says
so").

Traefik serves its own self-signed certificate, without failing, until Let's
Encrypt has issued one -- and for good when it cannot. So the check refuses
anything a browser or an agent would refuse, and says which; and a trusted
certificate with fewer than 28 days left, which only a renewal that keeps
failing produces. Each case here is a local TLS server presenting one
certificate the test made: one a trusted CA issued for the host, one for
another name, Traefik's default, an expired one, and one with 20 days left.
`openssl s_client` does the checking, as on a host.
"""

import datetime as dt
import socket
import ssl
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "scripts" / "check-public-tls.sh"
HOST = "rmm.novieri.com"
NOW = dt.datetime.now(dt.UTC)


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _cert(
    subject: str,
    issuer_key: ec.EllipticCurvePrivateKey,
    issuer: str,
    key: ec.EllipticCurvePrivateKey,
    *,
    sans: list[str] | None = None,
    ca: bool = False,
    days: tuple[int, int] = (-1, 30),
) -> x509.Certificate:
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(_name(issuer))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW + dt.timedelta(days=days[0]))
        .not_valid_after(NOW + dt.timedelta(days=days[1]))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if sans:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in sans]), critical=False
        )
    return builder.sign(issuer_key, hashes.SHA256())


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("pki")
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _cert("Test Root", ca_key, "Test Root", ca_key, ca=True, days=(-1, 365))
    (out / "root.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cases = {
        "good": ("leaf", ca_key, "Test Root", [HOST], (-1, 30)),
        "other": ("leaf", ca_key, "Test Root", ["other.example"], (-1, 30)),
        "expired": ("leaf", ca_key, "Test Root", [HOST], (-60, -1)),
        "short": ("leaf", ca_key, "Test Root", [HOST], (-60, 20)),
        "default": ("TRAEFIK DEFAULT CERT", None, "TRAEFIK DEFAULT CERT", None, (-1, 30)),
    }
    for case, (cn, issuer_key, issuer, sans, days) in cases.items():
        key = ec.generate_private_key(ec.SECP256R1())
        cert = _cert(cn, issuer_key or key, issuer, key, sans=sans, days=days)
        (out / f"{case}.pem").write_bytes(
            cert.public_bytes(serialization.Encoding.PEM)
            + key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    return {p.stem: p for p in out.iterdir()}


def _serve(certfile: Path) -> Iterator[int]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile)
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    stop = threading.Event()

    def loop() -> None:
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            try:
                with context.wrap_socket(conn, server_side=True) as tls:
                    tls.recv(1)
            except (ssl.SSLError, OSError):
                pass

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    yield port
    stop.set()
    thread.join()
    listener.close()


def _check(
    tmp_path: Path, port: int, *args: str, host: str = HOST
) -> subprocess.CompletedProcess[str]:
    deploy = tmp_path / "deploy"
    (deploy / "scripts").mkdir(parents=True, exist_ok=True)
    (deploy / "scripts" / SCRIPT.name).write_bytes(SCRIPT.read_bytes())
    (deploy / ".env").write_text(f"ITS_PUBLIC_HOST={host}\nITMS_BIND_IP=0.0.0.0\n")
    return subprocess.run(
        ["bash", str(deploy / "scripts" / SCRIPT.name), "--port", str(port), "--wait", "0", *args],
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def server(request: pytest.FixtureRequest, pki: dict[str, Path]) -> Iterator[int]:
    yield from _serve(pki[request.param])


@pytest.mark.parametrize("server", ["good"], indirect=True)
def test_a_trusted_certificate_for_the_host_passes(
    tmp_path: Path, server: int, pki: dict[str, Path]
) -> None:
    run = _check(tmp_path, server, "--cafile", str(pki["root"]))
    assert run.returncode == 0, run.stdout + run.stderr
    assert f"ok      127.0.0.1:{server} presents a trusted certificate for {HOST}" in run.stdout
    assert f"DNS:{HOST}" in run.stdout


@pytest.mark.parametrize(
    ("server", "use_root", "says"),
    [
        ("good", False, "does not verify (openssl code 21"),
        ("other", True, "does not verify (openssl code 62"),
        ("expired", True, "does not verify (openssl code 10"),
        ("default", True, "Traefik is still serving its own default certificate"),
    ],
    indirect=["server"],
)
def test_what_a_client_would_refuse_is_refused(
    tmp_path: Path, server: int, pki: dict[str, Path], use_root: bool, says: str
) -> None:
    args = ("--cafile", str(pki["root"])) if use_root else ()
    run = _check(tmp_path, server, *args)
    assert run.returncode == 1, run.stdout
    assert "REFUSE" in run.stderr and says in run.stderr, run.stderr
    assert "docker compose logs traefik | grep -i acme" in run.stderr


@pytest.mark.parametrize("server", ["short"], indirect=True)
def test_a_certificate_whose_renewal_is_overdue_is_refused(
    tmp_path: Path, server: int, pki: dict[str, Path]
) -> None:
    """Traefik renews 30 days before expiry and looks once a day: a healthy
    certificate never has fewer than about 29 days left. Fewer than 28 is a
    renewal that has been failing, and nothing else says so: Let's Encrypt
    stopped mailing expiry warnings in 2025."""
    run = _check(tmp_path, server, "--cafile", str(pki["root"]))
    assert run.returncode == 1, run.stdout
    assert "REFUSE" in run.stderr and "fewer than 28 days left" in run.stderr, run.stderr
    assert "docker compose logs traefik | grep -i acme" in run.stderr


def test_nothing_listening_is_refused(tmp_path: Path) -> None:
    with socket.create_server(("127.0.0.1", 0)) as free:
        port = free.getsockname()[1]
    run = _check(tmp_path, port)
    assert run.returncode == 1
    assert f"nothing answered TLS on 127.0.0.1:{port}" in run.stderr


def test_without_a_public_host_it_refuses_before_connecting(tmp_path: Path) -> None:
    run = _check(tmp_path, 9, host="your-host")
    assert run.returncode == 1
    assert "ITS_PUBLIC_HOST is not set in .env" in run.stderr
```

Y las dos pruebas que cambian, desde la raíz de la worktree:

```bash
python3 - <<'PY'
from pathlib import Path

p = Path("backend/tests/test_deploy_install_link.py")
s = p.read_text()
for old, new in (
    (
        "    an operator with a further proxy in front of ITMS to trust it; with such a",
        "    an operator with a further proxy in front of Novieri RMM to trust it; with such a",
    ),
    (
        '    assert "not supported" in section and "in front of ITMS" in section',
        '    assert "not supported" in section and "in front of Novieri RMM" in section',
    ),
):
    assert s.count(old) == 1, old
    s = s.replace(old, new)
p.write_text(s)

p = Path("backend/tests/test_deploy_script_steps.py")
s = p.read_text()
old = '''def test_the_preflight_checks_the_certificate_against_the_console_address(tmp_path: Path) -> None:
    """Its host comes from `ITS_PUBLIC_CONSOLE_URL` (until own authentication
    phase 4, `ITS_OIDC_ISSUER`, which a fresh `.env` no longer has), and a
    blank value gives no host -- not the whole line, which `sed` without
    `-n ...p` printed."""
    text = (REPO / "deploy/scripts/preflight.sh").read_text()
    found = re.search(
        r"^  host=\\"\\$\\(grep -E '\\^ITS_PUBLIC_CONSOLE_URL=' \\.env.*?\\|\\| true\\)\\"$",'''
new = '''def test_the_preflight_takes_the_console_host_from_its_address(tmp_path: Path) -> None:
    """Its host comes from `ITS_PUBLIC_CONSOLE_URL` (until own authentication
    phase 4, `ITS_OIDC_ISSUER`, which a fresh `.env` no longer has), and a
    blank value gives no host -- not the whole line, which `sed` without
    `-n ...p` printed. Until 2026-11-23 it was checked against the certificate
    file; since then, against ITS_PUBLIC_HOST, the name Traefik asks Let's
    Encrypt for (`test_preflight_script.py`)."""
    text = (REPO / "deploy/scripts/preflight.sh").read_text()
    found = re.search(
        r"^host=\\"\\$\\(grep -E '\\^ITS_PUBLIC_CONSOLE_URL=' \\.env.*?\\|\\| true\\)\\"$",'''
assert s.count(old) == 1
s = s.replace(old, new)
s += '''

def test_the_verify_step_checks_the_public_host_and_its_certificate() -> None:
    """Design 2026-11-23 §2 and §3: the 401 probe asks the host this .env
    names (it named one deployment's host, for every deployment, until
    2026-11-23), and a certificate a browser would refuse fails the deploy
    rather than passing it: Traefik's self-signed fallback is silent."""
    steps = _SH_COMMENT.sub("", _script())
    assert "itms.toeshee.com" not in steps
    assert "PUBLIC_HOST=\\"$(sed -n 's/^ITS_PUBLIC_HOST=//p' .env | tail -1)\\"" in steps
    assert '"https://${PUBLIC_HOST:-localhost}/api/v1/devices"' in steps
    check = steps.index("./scripts/check-public-tls.sh --wait 60")
    assert steps.index("FAIL=1", check) < steps.index("DEPLOY_FINISHED=1", check)
'''
p.write_text(s)
print("rewrote the two tests")
PY
```

- [ ] **Step 2: Las pruebas fallan**

Run: `cd backend && .venv/bin/pytest -q tests/test_new_env_script.py tests/test_preflight_script.py tests/test_check_public_tls.py tests/test_deploy_script_steps.py tests/test_deploy_install_link.py`
Expected: `48 failed, 42 passed` (medido el 2026-11-24 sobre una copia de `1bc0e5e`): las 19 de `new-env.sh` (el guion no existe, y `deploy/.env.new.*` no está en `.gitignore`) y las 8 de `check-public-tls.sh` (`No such file or directory` en el `read_bytes` del guion); las 18 de `preflight.sh` (el `preflight.sh` de `1bc0e5e` busca `certs/fullchain.pem`); la del `host=` sin sangría; la de `deploy.sh` (`itms.toeshee.com` está); y la de `in front of Novieri RMM`.

- [ ] **Step 3: La implementación**

Crear `deploy/scripts/new-env.sh`:

```bash
#!/usr/bin/env bash
#
# Write deploy/.env for a NEW host from .env.example, with nothing left to
# substitute by hand (design 2026-11-23-vps-y-marca-novieri §3).
#
#   ./scripts/new-env.sh --host rmm.novieri.com
#   ./scripts/new-env.sh --host rmm.novieri.com --acme-email ops@novieri.com
#   ./scripts/new-env.sh --host rmm.novieri.com --acme-email ops@novieri.com \
#       --acme-ca-server https://acme-staging-v02.api.letsencrypt.org/directory
#
# WHAT IT WRITES. Every `change-me` gets a fresh random value (`openssl rand
# -hex 24`: 48 hex characters, safe inside the database URLs that interpolate
# them) -- except ITS_OPENBAO_TOKEN and OPENBAO_TOKEN, which stay `change-me`:
# their real values are tokens OpenBao issues at first boot (README steps 3.2
# and 3.4), and a random string there would only turn "you skipped step 3.5"
# into an api failing with 403 from the vault. `your-host` becomes the host in
# every URL; ITS_PUBLIC_HOST, ITS_PUBLIC_CONSOLE_URL, ITS_ACME_EMAIL and, when
# given, ITS_ACME_CA_SERVER are set.
#
# WHAT IT REFUSES. An existing .env (it never overwrites one: that file holds
# the deployment's passwords, and a second run would orphan the database's);
# a host that is not a lower-case DNS name with a dot (an IP address included:
# the public certificate, the gateway's and the Windows installer all need a
# name); an address that is not one.
#
# WHAT IT NEVER DOES. Print a secret. It prints the host, the URLs, the
# contact address, the CA and the NAMES of the keys it generated, and nothing
# it generated. The file is created 0600, before anything is written to it.
set -euo pipefail

cd "$(dirname "$0")/.."   # deploy/

die() { printf 'new-env.sh: %s\n' "$*" >&2; exit 1; }

host=""
email=""
ca=""
while [ $# -gt 0 ]; do
  case "$1" in
    --host) [ $# -ge 2 ] || die "--host needs a value"; host="$2"; shift 2 ;;
    --acme-email) [ $# -ge 2 ] || die "--acme-email needs a value"; email="$2"; shift 2 ;;
    --acme-ca-server) [ $# -ge 2 ] || die "--acme-ca-server needs a value"; ca="$2"; shift 2 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

[ -e .env ] && die ".env already exists; this never overwrites one. Move it aside first if you really mean to start over."
[ -f .env.example ] || die ".env.example is missing; run this from a checkout of the repository"
[ -n "$host" ] || die "--host is required, e.g. --host rmm.novieri.com"
# A DNS name: lower-case labels of letters, digits and inner hyphens, at least
# two of them. Four all-digit labels are an IPv4 address, and refused.
printf '%s' "$host" | grep -Eq '^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$' \
  || die "--host must be a lower-case DNS name such as rmm.novieri.com, not '$host'"

if [ -z "$email" ]; then
  [ -t 0 ] || die "--acme-email is required when stdin is not a terminal"
  read -r -p "Let's Encrypt account address (the certificate's contact): " email
fi
printf '%s' "$email" | grep -Eq '^[^@[:space:]]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}$' \
  || die "the ACME address must look like name@domain.tld, not '$email'"
if [ -n "$ca" ]; then
  printf '%s' "$ca" | grep -Eq '^https://[^[:space:]]+$' || die "--acme-ca-server must be an https URL"
fi
command -v openssl >/dev/null 2>&1 || die "openssl is required (apt-get install -y openssl)"

first_boot_tokens=" ITS_OPENBAO_TOKEN OPENBAO_TOKEN "
generated=()
umask 077
tmp="$(mktemp .env.new.XXXXXX)"
trap 'rm -f "$tmp"' EXIT
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    \#*|'') printf '%s\n' "$line"; continue ;;
  esac
  key="${line%%=*}"
  value="${line#*=}"
  if [ "$value" = "change-me" ] && [[ "$first_boot_tokens" != *" $key "* ]]; then
    value="$(openssl rand -hex 24)"
    generated+=("$key")
  fi
  case "$key" in
    ITS_PUBLIC_HOST) value="$host" ;;
    ITS_PUBLIC_CONSOLE_URL) value="https://$host" ;;
    ITS_ACME_EMAIL) value="$email" ;;
    ITS_ACME_CA_SERVER) [ -z "$ca" ] || value="$ca" ;;
  esac
  value="${value//your-host/$host}"
  printf '%s=%s\n' "$key" "$value"
done < .env.example > "$tmp"
for key in ITS_PUBLIC_HOST ITS_PUBLIC_CONSOLE_URL ITS_ACME_EMAIL ITS_ACME_CA_SERVER; do
  grep -q "^$key=." "$tmp" || die ".env.example has no $key line; it is older than this script"
done
! grep -v '^#' "$tmp" | grep -q 'your-host' || die "a setting still names your-host; nothing was written"
# A hard link never replaces an existing file, so a .env that appeared while
# this ran is not overwritten either. mktemp made the file 0600.
ln "$tmp" .env 2>/dev/null || die ".env appeared while this ran; nothing was written"
rm -f "$tmp"
trap - EXIT

printf 'wrote .env (mode %s)\n' "$(stat -c %a .env)"
printf '  host            %s\n' "$host"
grep -E '^(ITS_PUBLIC_CONSOLE_URL|ITS_GATEWAY_URL|ITS_GATEWAY_RELAY_URL|ITS_S3_PUBLIC_ENDPOINT|ITS_ACME_EMAIL|ITS_ACME_CA_SERVER)=' .env \
  | sed 's/^/  /'
printf '  new secrets     %s\n' "${generated[*]}"
printf '  still change-me ITS_OPENBAO_TOKEN OPENBAO_TOKEN (README step 3.5 fills them)\n'
```

Crear `deploy/scripts/check-public-tls.sh`:

```bash
#!/usr/bin/env bash
#
# After `docker compose up -d`: is :443 serving a certificate for
# ITS_PUBLIC_HOST that a browser and an agent will accept? (design
# 2026-11-23-vps-y-marca-novieri §2: "if ACME fails, the check after start
# says so, instead of leaving a certificate browsers refuse").
#
#   ./scripts/check-public-tls.sh                  # waits up to 180 s
#   ./scripts/check-public-tls.sh --wait 30
#   ./scripts/check-public-tls.sh --connect 127.0.0.1 --cafile "$RUNNER_TEMP/pebble-root.pem"
#
# Traefik asks Let's Encrypt for the certificate when it first sees a router
# for the host, and until one arrives it serves its own self-signed default
# ("TRAEFIK DEFAULT CERT") WITHOUT FAILING. So this waits, then refuses on
# anything a client would refuse: an untrusted chain, a name that is not the
# host, an expired certificate, the default one.
#
# AND A RENEWAL THAT KEEPS FAILING. Traefik renews 30 days before expiry and
# looks once a day, so a healthy certificate never has fewer than about 29
# days left; fewer than 28 means renewal has been failing for days. Nothing
# else would say so: Let's Encrypt stopped mailing expiry warnings in 2025.
#
# --connect is the address to dial (default: ITMS_BIND_IP when it names one
# address, else 127.0.0.1); the name sent as SNI and checked is always
# ITS_PUBLIC_HOST. --cafile trusts that file instead of the system's roots:
# for Pebble in CI, or Let's Encrypt staging's root on a rehearsal. --port is
# for the script's own tests (backend/tests/test_check_public_tls.py).
# Exit 0 = the certificate is good, with 28 days or more left. Exit 1 = it is
# not, and why.
set -euo pipefail

cd "$(dirname "$0")/.."   # deploy/

die() { printf 'check-public-tls.sh: %s\n' "$*" >&2; exit 1; }

connect=""
port=443
cafile=""
wait_s=180
while [ $# -gt 0 ]; do
  case "$1" in
    --connect) [ $# -ge 2 ] || die "--connect needs an address"; connect="$2"; shift 2 ;;
    --cafile) [ $# -ge 2 ] || die "--cafile needs a file"; cafile="$2"; shift 2 ;;
    --wait) [ $# -ge 2 ] || die "--wait needs seconds"; wait_s="$2"; shift 2 ;;
    --port) [ $# -ge 2 ] || die "--port needs a number"; port="$2"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[ -f .env ] || die ".env does not exist"
host="$(sed -n 's/^ITS_PUBLIC_HOST=//p' .env | tail -1)"
[ -n "$host" ] && [ "$host" != "your-host" ] || die "ITS_PUBLIC_HOST is not set in .env"
if [ -z "$connect" ]; then
  bind="$(sed -n 's/^ITMS_BIND_IP=//p' .env | tail -1)"
  case "$bind" in ''|0.0.0.0) connect=127.0.0.1 ;; *) connect="$bind" ;; esac
fi
ca_args=()
if [ -n "$cafile" ]; then
  [ -s "$cafile" ] || die "--cafile $cafile is missing or empty"
  ca_args=(-CAfile "$cafile")
fi

deadline=$(( $(date +%s) + wait_s ))
last=""
while :; do
  out="$(openssl s_client -connect "$connect:$port" -servername "$host" \
           -verify_hostname "$host" "${ca_args[@]}" </dev/null 2>&1 || true)"
  code="$(printf '%s\n' "$out" | sed -n 's/^ *Verify return code: \([0-9]*\).*/\1/p' | tail -1)"
  pem="$(printf '%s\n' "$out" | sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/p')"
  issuer="$(printf '%s\n' "$pem" | openssl x509 -noout -issuer 2>/dev/null || true)"
  if [ "$code" = "0" ] && [ -n "$pem" ]; then
    if ! printf '%s\n' "$pem" | openssl x509 -noout -checkend $((28 * 86400)) >/dev/null; then
      printf 'REFUSE  the certificate on %s:%s for %s is trusted but has fewer than 28 days left (%s): Traefik renews at 30, so its renewal has been failing\n' \
        "$connect" "$port" "$host" "$(printf '%s\n' "$pem" | openssl x509 -noout -enddate)" >&2
      printf '        What Traefik said about it: docker compose logs traefik | grep -i acme\n' >&2
      exit 1
    fi
    printf 'ok      %s:%s presents a trusted certificate for %s\n' "$connect" "$port" "$host"
    printf '%s\n' "$pem" | openssl x509 -noout -issuer -ext subjectAltName -enddate | sed 's/^/        /'
    exit 0
  fi
  case "$issuer" in
    *"TRAEFIK DEFAULT CERT"*) last="Traefik is still serving its own default certificate: Let's Encrypt has not issued one for $host" ;;
    "") last="nothing answered TLS on $connect:$port" ;;
    *) last="the certificate on $connect:$port for $host does not verify (openssl code ${code:-none}; $issuer)" ;;
  esac
  [ "$(date +%s)" -lt "$deadline" ] || break
  sleep 5
done
printf 'REFUSE  %s\n' "$last" >&2
printf '        after %s s. What Traefik said about it: docker compose logs traefik | grep -i acme\n' "$wait_s" >&2
printf '        A staging or Pebble CA is expected to be untrusted: pass --cafile with its root.\n' >&2
exit 1
```

Reemplazar `deploy/scripts/preflight.sh` entero por:

```bash
#!/usr/bin/env bash
#
# Pre-flight for a first deploy. Run from `deploy/`, before `docker compose up`.
#
# WHY THIS EXISTS. GO-LIVE.md is eight sections of things that must be changed
# from defaults, and the failure mode of a checklist is a human reading it at
# the end of a long day and being sure they did step 4. Every check below is
# something the checklist already requires; this just refuses instead of
# trusting. It reads `.env`, the filesystem, DNS, this host's addresses and
# listening sockets, and Docker's versions -- it starts nothing and changes
# nothing.
#
# Exit 0 = nothing obviously wrong. Exit 1 = at least one refusal. It cannot
# tell you the deployment is CORRECT; a checker never can. It tells you that a
# specific list of known-bad states is absent.
set -euo pipefail

cd "$(dirname "$0")/.."

fail=0
refuse() { printf '  REFUSE  %s\n' "$1"; fail=1; }
ok()     { printf '  ok      %s\n' "$1"; }

echo "Novieri RMM pre-flight"
echo

# --- .env exists and is not world-readable -------------------------------
if [ ! -f .env ]; then
  refuse ".env does not exist. cp .env.example .env, then work through GO-LIVE.md §1."
  echo; echo "Refused before any other check -- nothing else can be judged."; exit 1
fi
ok ".env exists"

perms="$(stat -c '%a' .env)"
if [ "$perms" != "600" ]; then
  refuse ".env is mode $perms, not 600. It holds every password in the deployment. chmod 600 .env"
else
  ok ".env is mode 600"
fi

# --- no placeholder secrets ----------------------------------------------
# All six ship as `change-me`. The two OpenBao tokens are legitimately still
# `change-me` until first boot has run, so they are reported separately rather
# than blocking a first `up`.
first_boot_only="ITS_OPENBAO_TOKEN OPENBAO_TOKEN"
while IFS='=' read -r key value; do
  case "$key" in ''|\#*) continue ;; esac
  [ "$value" = "change-me" ] || continue
  if printf '%s\n' $first_boot_only | grep -qx "$key"; then
    printf '  note    %s is still change-me -- expected before first boot; §3.2/§3.4 issue it\n' "$key"
  else
    refuse "$key is still change-me. openssl rand -hex 24 (or start over with scripts/new-env.sh)"
  fi
done < .env

# --- IT_SUITE_VERSION is a real tag --------------------------------------
version="$(grep -E '^IT_SUITE_VERSION=' .env | cut -d= -f2- || true)"
if [ -z "$version" ] || [ "$version" = "dev" ]; then
  refuse "IT_SUITE_VERSION is '${version:-unset}'. ':dev' names whatever was last built on this host, so it is not a deployable state and NOT A ROLLBACK TARGET -- a deployment left on it has nothing to roll back to. Run ./scripts/build-images.sh and set the tag it prints."
else
  ok "IT_SUITE_VERSION=$version"
fi

# --- ITMS_BIND_IP names an address this host actually has -----------------
# A typo here fails at `docker compose up` with "cannot assign requested
# address", which reads like a Docker problem rather than a one-character .env
# problem. 0.0.0.0 (and an unset value, which compose defaults to 0.0.0.0) is
# the dedicated-host case and needs no check.
bind_ip="$(grep -E '^ITMS_BIND_IP=' .env | cut -d= -f2- || true)"
if [ -z "$bind_ip" ] || [ "$bind_ip" = "0.0.0.0" ]; then
  ok "ITMS_BIND_IP=${bind_ip:-unset} (all interfaces)"
elif ! command -v ip >/dev/null 2>&1; then
  printf '  note    ITMS_BIND_IP=%s could not be checked -- iproute2 is not installed\n' "$bind_ip"
elif ip -brief address 2>/dev/null | grep -qwF "$bind_ip"; then
  ok "ITMS_BIND_IP=$bind_ip is assigned to this host"
else
  refuse "ITMS_BIND_IP=$bind_ip is not an address on this host, so traefik cannot bind it. \`ip -brief address\` lists what is available."
fi

# --- the public address, and the certificate Traefik will ask for ----------
# Since 2026-11-23 there is no certificate file to check (design
# 2026-11-23-vps-y-marca-novieri §2): Traefik asks Let's Encrypt for one for
# ITS_PUBLIC_HOST at the first start. What can stop that is checked instead,
# BEFORE it is asked, because every failed validation counts against Let's
# Encrypt's limit of 5 per name and hour and a failed start leaves Traefik
# serving its own self-signed default. scripts/check-public-tls.sh checks the
# result after the start.
public_host="$(grep -E '^ITS_PUBLIC_HOST=' .env | tail -1 | cut -d= -f2- || true)"
# `sed -n ...p` so a blank value yields nothing rather than the whole line.
host="$(grep -E '^ITS_PUBLIC_CONSOLE_URL=' .env | tail -1 \
        | sed -nE 's|^[^=]+=https?://([^/:]+).*|\1|p' || true)"
if [ -z "$public_host" ] || [ "$public_host" = "your-host" ]; then
  refuse "ITS_PUBLIC_HOST is '${public_host:-unset}': Traefik has no name to ask a certificate for. scripts/new-env.sh --host <the console's DNS name> writes it."
elif [ "$host" != "$public_host" ]; then
  refuse "ITS_PUBLIC_HOST=$public_host is not the host of ITS_PUBLIC_CONSOLE_URL ('${host:-unset}'). The certificate would be for one name and sign-in checked against another; they must be the same."
else
  ok "ITS_PUBLIC_HOST=$public_host is the console's host"
fi

acme_email="$(grep -E '^ITS_ACME_EMAIL=' .env | tail -1 | cut -d= -f2- || true)"
if [ -z "$acme_email" ]; then
  refuse "ITS_ACME_EMAIL is empty, and compose will not start Traefik without it. Put the address for Let's Encrypt's account notices there."
else
  ok "ITS_ACME_EMAIL is set"
fi
acme_ca="$(grep -E '^ITS_ACME_CA_SERVER=' .env | tail -1 | cut -d= -f2- || true)"
case "${acme_ca:-https://acme-v02.api.letsencrypt.org/directory}" in
  https://acme-v02.api.letsencrypt.org/directory) ok "ACME CA: Let's Encrypt production" ;;
  https://*) printf '  note    ACME CA is %s, not Let'"'"'s Encrypt production: its certificates are for a rehearsal, and no browser trusts them\n' "$acme_ca" ;;
  *) refuse "ITS_ACME_CA_SERVER=$acme_ca is not an https URL" ;;
esac

# The name must reach THIS machine, or Let's Encrypt's validation reaches
# another. ASKED OF DNS ALONE, as Let's Encrypt asks it: `getent -s dns` skips
# /etc/hosts, where cloud-init writes `127.0.1.1 <this host's name>` and where
# a line written by hand would hide a missing or wrong A record.
#
# IPv4 ONLY (decided 2026-11-24). This deployment is one IPv4 address: the
# runbook, host-network.md and this script reason about it, and nothing
# checks that :443 and :8443 answer on IPv6. So an AAAA record would send
# browsers and agents, which try IPv6 first, where nobody has checked this
# stack answers -- or, if it is another machine's, to that machine. (Not for
# Let's Encrypt's sake: its TLS-ALPN-01 validator falls back to IPv4 after any
# IPv6 error, Boulder va/tlsalpn.go.) The AAAA is asked through Python's
# getaddrinfo, not `getent ahosts`: glibc's getent sets AI_ADDRCONFIG, which
# hides every AAAA on a host with no IPv6 address of its own -- exactly the
# VPS this runbook builds. `|| true`: getent exits 2 for a name with no
# address, which pipefail would turn into this script dying silently here.
if [ -n "$public_host" ] && [ "$public_host" != "your-host" ]; then
  if ! command -v getent >/dev/null 2>&1; then
    printf '  note    %s could not be resolved -- getent is not installed\n' "$public_host"
  else
    v4="$(getent -s dns ahostsv4 "$public_host" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ' || true)"
    v6=""
    if command -v python3 >/dev/null 2>&1; then
      v6="$(python3 -I -c 'import socket, sys
try:
    found = socket.getaddrinfo(sys.argv[1], None, socket.AF_INET6, socket.SOCK_STREAM)
except OSError:
    found = []
print(" ".join(sorted({f[4][0] for f in found if not f[4][0].startswith("::ffff:")})))' "$public_host" 2>/dev/null || true)"
    else
      printf '  note    %s has no AAAA check -- python3 is not installed\n' "$public_host"
    fi
    if [ -z "$v4" ]; then
      refuse "$public_host does not resolve to an IPv4 address. Create its A record, pointing at this server, and wait for it to answer before the first start."
    else
      if [ -n "$bind_ip" ] && [ "$bind_ip" != "0.0.0.0" ]; then
        mine="$bind_ip"
      elif command -v ip >/dev/null 2>&1; then
        mine="$(ip -4 -o address show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | tr '\n' ' ')"
      else
        mine=""
      fi
      match=""
      for a in $v4; do
        case " $mine " in *" $a "*) match="$a" ;; esac
      done
      if [ -n "$match" ]; then
        ok "$public_host resolves to $match, an address of this host"
      elif [ -z "$mine" ]; then
        printf '  note    %s resolves to %s; this host'"'"'s addresses could not be read (iproute2 is not installed) -- compare them by hand\n' "$public_host" "${v4% }"
      elif printf '%s\n' $mine | grep -qvE '^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.)'; then
        refuse "$public_host resolves to ${v4% }, which is not an address of this host (${mine% }). Let's Encrypt would validate whatever answers there."
      else
        printf '  note    %s resolves to %s; every address of this host is private (%s): behind NAT the public address is not on an interface -- compare it by hand\n' "$public_host" "${v4% }" "${mine% }"
      fi
    fi
    if [ -n "$v6" ]; then
      refuse "$public_host has an AAAA record (${v6% }). This deployment is IPv4 only: browsers and agents try IPv6 first, and nothing has checked that this stack answers there (or the address is another machine's). Remove the AAAA record."
    fi
  fi
fi

# --- ports 443 and 8443 are free, unless this stack holds them -------------
if ! command -v ss >/dev/null 2>&1; then
  printf '  note    listeners on 443/8443 could not be checked -- iproute2 is not installed\n'
elif [ -n "$(docker compose ps --status running -q traefik 2>/dev/null || true)" ]; then
  ok "443 and 8443 are held by this stack's traefik"
else
  held="$(ss -Hltn '( sport = :443 or sport = :8443 )' 2>/dev/null | awk '{print $4}' | tr '\n' ' ')"
  if [ -n "$held" ]; then
    refuse "something already listens on ${held% } -- traefik needs 443 and 8443. scripts/host-check.sh section 2 names it."
  else
    ok "443 and 8443 are free"
  fi
fi

# --- Docker Engine 26+ and Compose 2.24+ (README, Prerequisites) -----------
at_least() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }
engine="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
composev="$(docker compose version --short 2>/dev/null | sed 's/^v//' || true)"
if [ -z "$engine" ]; then
  refuse "Docker Engine does not answer (docker version). Install Docker CE from Docker's repository (README, Prerequisites)."
elif ! at_least "$engine" 26; then
  refuse "Docker Engine $engine is older than 26: traefik:v3.6's Docker provider is refused by it, and every route answers 404."
else
  ok "Docker Engine $engine"
fi
if [ -z "$composev" ]; then
  refuse "Docker Compose does not answer (docker compose version)."
elif ! at_least "$composev" 2.24; then
  refuse "Docker Compose $composev is older than 2.24: .env's \${...} references inside env_file are not interpolated, and every database connection fails."
else
  ok "Docker Compose $composev"
fi

# --- .env must not be committable ----------------------------------------
if git -C .. check-ignore -q deploy/.env 2>/dev/null; then
  ok ".env is gitignored"
else
  refuse "deploy/.env is NOT gitignored. Every password in this deployment would be committed."
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "No known-bad state found. This does not mean the deployment is correct --"
  echo "work through GO-LIVE.md §2 (the first administrator), §4 (OpenBao seal), §5 (gateway cert)"
  echo "and §6 (back these up) before anything real touches it."
else
  echo "Refused. Fix the items above and run again."
fi
exit "$fail"
```

`.env.example`, desde la raíz de la worktree:

```bash
python3 - <<'PY'
"""Task 4, Step 3: .env.example names the public address, the ACME keys and
Novieri RMM. Run from the repository root."""
import sys
from pathlib import Path

BLOCK = """IT_SUITE_VERSION=dev

# --- The public address, and the certificate for it ---
# The ONE DNS name this deployment answers on: the console, the API and
# object storage on :443, the agent gateway on :8443. scripts/new-env.sh writes
# it, and every URL below, from `--host` (design 2026-11-23-vps-y-marca-novieri
# §3). Traefik asks Let's Encrypt for a certificate for exactly this name --
# TLS-ALPN-01, answered on :443, so :80 stays shut -- and renews it by itself,
# 30 days before it expires. scripts/preflight.sh refuses when it is not the
# host of ITS_PUBLIC_CONSOLE_URL, does not resolve to this machine or has an
# AAAA record. The gateway on :8443 does NOT use this certificate: it presents
# one from the internal CA (README step 6), which agents pin.
ITS_PUBLIC_HOST=your-host
# The contact address of the Let's Encrypt account Traefik registers.
# Required: compose will not start Traefik without it (docker-compose.yml,
# ${ITS_ACME_EMAIL:?}), and scripts/new-env.sh asks for it. Let's Encrypt
# stopped mailing expiry warnings on 2025-06-04 and no longer keeps the
# address on the account (it passes it to ISRG's mailing list), so nothing
# about renewal depends on this mailbox: scripts/check-public-tls.sh refuses
# a certificate with fewer than 28 days left.
ITS_ACME_EMAIL=
# Which ACME directory. Production by default. Let's Encrypt issues at most 5
# certificates a week for one exact set of names, and every first boot with an
# empty traefik-acme volume asks for one -- so a REHEARSAL uses staging, whose
# certificates no browser trusts:
#   ITS_ACME_CA_SERVER=https://acme-staging-v02.api.letsencrypt.org/directory
# CI points it at Pebble, Let's Encrypt's test CA (ci/acme-pebble.yml).
ITS_ACME_CA_SERVER=https://acme-v02.api.letsencrypt.org/directory
"""

EDITS = [
    ("IT_SUITE_VERSION=dev\n", BLOCK),
    (
        "# must move; if it shows a specific address, put ITMS on a different one here.\n",
        "# must move; if it shows a specific address, put Novieri RMM on a different\n"
        "# one here.\n",
    ),
    (
        "# The subnet ITMS's own container network uses. Change it only if this default\n"
        "# collides with a real network at your site.\n",
        "# The subnet Novieri RMM's own container network uses. Change it only if this\n"
        "# default collides with a real network at your site.\n",
    ),
    ("ITS_DEFAULT_ORG_SLUG=toeshee\n", "ITS_DEFAULT_ORG_SLUG=novieri\n"),
    (
        "#     backend/.venv/bin/python scripts/preflight_email_domain.py toeshee.com\n",
        "#     backend/.venv/bin/python scripts/preflight_email_domain.py novieri.com\n",
    ),
    (
        "# trailing slash, e.g. https://itms.toeshee.com. REQUIRED FOR ANY CONSOLE\n",
        "# trailing slash, e.g. https://rmm.novieri.com. REQUIRED FOR ANY CONSOLE\n",
    ),
    ("ITS_PUBLIC_CONSOLE_URL=\n", "ITS_PUBLIC_CONSOLE_URL=https://your-host\n"),
    (
        "# hop is supported. A further proxy or load balancer in front of ITMS is not\n"
        "# supported for the install link's network check: Traefik would see that\n"
        "# proxy, not the computer, and the check would compare the proxy's address.\n",
        "# hop is supported. A further proxy or load balancer in front of Novieri RMM\n"
        "# is not supported for the install link's network check: Traefik would see\n"
        "# that proxy, not the computer, and the check would compare the proxy's\n"
        "# address.\n",
    ),
]

path = Path("deploy/.env.example")
text = path.read_text()
for old, new in EDITS:
    if text.count(old) != 1:
        sys.exit(f".env.example: not there exactly once: {old[:70]!r}")
    text = text.replace(old, new)
path.write_text(text)
print("rewrote deploy/.env.example")
PY
```

`deploy.sh`, `host-check.sh` e `install-backup-timer.sh`:

```bash
python3 - <<'PY'
"""Task 4, Step 3 (second half): deploy.sh checks the public host and its
certificate; the two host scripts say Novieri RMM. Run from the repository
root."""
import re
import sys
from pathlib import Path

DEPLOY_OLD = (
    "CODE=\"$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "
    "https://itms.toeshee.com/api/v1/devices || echo 000)\"\n"
)
DEPLOY_NEW = (
    "# The host this deployment answers on, from .env (design 2026-11-23 §3):\n"
    "# until 2026-11-23 this line named one deployment's host for everybody.\n"
    "PUBLIC_HOST=\"$(sed -n 's/^ITS_PUBLIC_HOST=//p' .env | tail -1)\"\n"
    "CODE=\"$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "
    "\"https://${PUBLIC_HOST:-localhost}/api/v1/devices\" || echo 000)\"\n"
)
PROBE_OLD = 'warn "API returned $CODE (expected 401). 404 usually means api is still starting."\n'
PROBE_NEW = (
    'warn "API returned $CODE (expected 401). 503 means the api is not healthy yet '
    "(Traefik's api-unavailable router); 404 means Traefik has no router for this host.\"\n"
)
TLS_OLD = 'DEPLOY_FINISHED=1\n'
TLS_NEW = (
    "# The certificate on :443, which Traefik asks Let's Encrypt for and renews by\n"
    "# itself. A failure there does not fail a request above -- Traefik serves its\n"
    "# own self-signed default instead -- so it is checked on its own (design\n"
    "# 2026-11-23 §2).\n"
    "if ./scripts/check-public-tls.sh --wait 60; then\n"
    "    note \"the certificate on :443 is trusted and names $PUBLIC_HOST.\"\n"
    "else\n"
    "    warn \"the certificate on :443 is not one a browser accepts (above).\"\n"
    "    FAIL=1\n"
    "fi\n"
    "\n"
    "DEPLOY_FINISHED=1\n"
)
WORD = re.compile(r"(?<![-\w./])ITMS(?![-\w]|\.\w)")

deploy = Path("deploy/scripts/deploy.sh")
text = deploy.read_text()
for old, new in ((DEPLOY_OLD, DEPLOY_NEW), (PROBE_OLD, PROBE_NEW), (TLS_OLD, TLS_NEW)):
    if text.count(old) != 1:
        sys.exit(f"deploy.sh: not there exactly once: {old[:60]!r}")
    text = text.replace(old, new)
deploy.write_text(text)
for rel, expected in (("deploy/scripts/host-check.sh", 13), ("deploy/scripts/install-backup-timer.sh", 4)):
    path = Path(rel)
    body = path.read_text().replace("for an ITMS deploy", "for a Novieri RMM deploy")
    new, count = WORD.subn("Novieri RMM", body)
    if count != expected:
        sys.exit(f"{rel}: {count} ITMS, the plan says {expected}")
    path.write_text(new)
# new-env.sh's temporary file holds four fresh secrets until the hard link; a
# SIGKILL or a power cut leaves it in deploy/. (Task 3 edits another hunk of
# .gitignore, lines 28-34; the two merge without a conflict.)
ignore = Path(".gitignore")
body = ignore.read_text()
GITIGNORE_OLD = ".ruff_cache/\n.env\n"
GITIGNORE_NEW = (
    ".ruff_cache/\n.env\n"
    "# What deploy/scripts/new-env.sh writes before it links it to deploy/.env:\n"
    "# four fresh secrets, left behind only by a SIGKILL or a power cut.\n"
    "deploy/.env.new.*\n"
)
if body.count(GITIGNORE_OLD) != 1:
    sys.exit(".gitignore: the .env line is not there exactly once")
ignore.write_text(body.replace(GITIGNORE_OLD, GITIGNORE_NEW))
print("rewrote deploy/scripts/deploy.sh, host-check.sh, install-backup-timer.sh, .gitignore")
PY
chmod +x deploy/scripts/new-env.sh deploy/scripts/check-public-tls.sh
bash -n deploy/scripts/new-env.sh deploy/scripts/check-public-tls.sh deploy/scripts/preflight.sh deploy/scripts/deploy.sh deploy/scripts/host-check.sh deploy/scripts/install-backup-timer.sh && echo syntax-ok
```

Expected: `rewrote deploy/.env.example`, `rewrote deploy/scripts/deploy.sh, host-check.sh, install-backup-timer.sh, .gitignore`, `syntax-ok`.

- [ ] **Step 4: Las pruebas pasan**

Run (desde `backend/`): `.venv/bin/pytest -q tests/test_new_env_script.py tests/test_preflight_script.py tests/test_check_public_tls.py tests/test_deploy_script_steps.py tests/test_deploy_install_link.py tests/test_deploy_env_documentation.py tests/test_deploy_scripts.py tests/test_setup_api.py tests/test_openbao_seal_script.py tests/test_deploy_runbook_secrets.py && .venv/bin/ruff check tests`
Expected: `172 passed` (en una copia de `1bc0e5e`, medido de nuevo el 2026-11-24 con las cuatro pruebas que añadió la revisión previa: el temporal en `.gitignore`, `/etc/hosts`, el `AAAA` en un host sin IPv6 y los 28 días), `All checks passed!`.

Y a mano, desde `deploy/`, lo que verá el operador (con el `.env.example` de esta tarea; borrar el `.env` después):

```bash
./scripts/new-env.sh --host rmm.novieri.com --acme-email ops@novieri.example </dev/null
stat -c %a .env
grep -c '=change-me$' .env
./scripts/new-env.sh --host rmm.novieri.com --acme-email ops@novieri.example </dev/null; echo "exit $?"
rm -f .env
```

Expected:

```
wrote .env (mode 600)
  host            rmm.novieri.com
  ITS_ACME_EMAIL=ops@novieri.example
  ITS_ACME_CA_SERVER=https://acme-v02.api.letsencrypt.org/directory
  ITS_S3_PUBLIC_ENDPOINT=https://rmm.novieri.com
  ITS_GATEWAY_URL=wss://rmm.novieri.com:8443/agent
  ITS_GATEWAY_RELAY_URL=wss://rmm.novieri.com:8443/relay
  ITS_PUBLIC_CONSOLE_URL=https://rmm.novieri.com
  new secrets     POSTGRES_ADMIN_PASSWORD APP_DB_PASSWORD PORTAL_DB_PASSWORD MINIO_ROOT_PASSWORD
  still change-me ITS_OPENBAO_TOKEN OPENBAO_TOKEN (README step 3.5 fills them)
600
2
new-env.sh: .env already exists; this never overwrites one. Move it aside first if you really mean to start over.
exit 1
```

(salida real de la copia del 2026-11-23).

- [ ] **Step 5: Commit**

```bash
git add .gitignore deploy/scripts/new-env.sh deploy/scripts/check-public-tls.sh deploy/scripts/preflight.sh deploy/scripts/deploy.sh deploy/scripts/host-check.sh deploy/scripts/install-backup-timer.sh deploy/.env.example backend/tests/test_new_env_script.py backend/tests/test_preflight_script.py backend/tests/test_check_public_tls.py backend/tests/test_deploy_script_steps.py backend/tests/test_deploy_install_link.py
git commit -m "feat(deploy): new-env.sh writes a new host's .env; preflight checks the name, DNS, ports and Docker; check-public-tls.sh after start"
```

---

### Task 5: Los pendientes de comportamiento: el reuso auditado y el reloj del enlace

**Files:**
- Modify: `backend/app/core/auth/oauth/grants.py:554-572` (el código reusado) y `:676-677` (el token de refresco usado de una familia terminada)
- Modify: `backend/app/core/auth/action_tokens.py` (función nueva `start_clock`), `backend/app/core/auth/flows.py:1387-1395`, `backend/app/modules/user_admin/service.py:740-742`, `backend/app/services/contact_access.py:189-190`
- Modify (pruebas): `backend/tests/auth_support.py` (`MailBox.delay_s`), `backend/tests/test_oauth_token.py` (tres pruebas al final), `backend/tests/test_auth_password_reset.py` (una al final), `backend/tests/test_user_admin_api.py` (una al final y dos imports)

**Interfaces:**
- Consumes: nada.
- Produces: `action_tokens.start_clock(session: AsyncSession, token: str, ttl: timedelta) -> None`; los eventos `mcp.code_reused` y `mcp.refresh_reused` ganan `metadata.grant = "already_ended"` cuando la concesión ya había terminado; `tests.auth_support.MailBox.delay_s: float` (segundos que tarda el relé falso).

**Decisiones de la tarea.**
- **Qué es «el reuso … sobre un permiso de IA ya terminado» (diseño §6):** el código de autorización usado que se presenta otra vez cuando su familia ya terminó (`revoke_family` devuelve `False` y hoy no se audita nada, `grants.py:555-571`), y el token de refresco **usado** de una familia revocada o caducada (hoy `grants.py:676` devuelve `BAD_REFRESH` antes de mirar `used_at`). Se auditan con la misma acción, como «system», la persona como objetivo y `metadata.grant = "already_ended"`, igual que los demás finales que la persona no causó (`grants.py:36-44`). Un token **nunca usado** de una familia terminada no es un reuso y sigue sin auditarse; un código usado que no abrió ninguna concesión (un intercambio fallido lo gasta, `:272` de `test_oauth_token.py`) tampoco.
- **El volumen de auditoría** lo acota el límite por red del endpoint (`TOKEN_REQUESTS_PER_MINUTE`, ya probado en `test_one_network_is_held_to_its_requests_a_minute_on_both_endpoints`).
- **Qué es «el código de correo» (diseño §6):** el enlace de un solo uso que se manda por correo (`action_tokens.issue`: restablecer, 30 min; invitar, 72 h). `issue` lo fecha al acuñarlo (`:91-104`), antes del envío; un relé SMTP lento se comía ese tiempo. Ahora `start_clock` vuelve a poner `expires_at = now() + ttl` **cuando el envío ha terminado bien**, en la misma transacción, y solo si el enlace sigue sin usar. Un enlace que no se pudo enviar y se enseña en la consola (invitaciones) conserva el reloj de su acuñación: se ve en el momento. `created_at` no se toca, y el `CHECK expires_at > created_at` (`models/auth.py:315`) se sigue cumpliendo.
- **El tercer llamante** (`contact_access._issue`, la invitación de un contacto) cambia igual; lo cubre el mismo `start_clock` y las pruebas de `test_contact_access_api.py` siguen pasando.

- [ ] **Step 0: La worktree**

Run, desde la raíz de la worktree, la comprobación de Global Constraints («Pistas en paralelo») y la de esta oleada:

```bash
git fetch -q origin claude/compassionate-bardeen-vgtwus
git log --oneline -1
git merge-base --is-ancestor HEAD origin/claude/compassionate-bardeen-vgtwus && echo ON-PROJECT-BRANCH
git merge-base --is-ancestor 6a14dbc HEAD && echo HAS-PR-13
git diff --quiet 1bc0e5e HEAD -- backend agent deploy .github scripts && echo CODE-AS-MEASURED
```

Expected: `21e72ec docs(plan): project 3 plan, pre-flight fixes in progress (snapshot)` (o el commit posterior de `origin/claude/compassionate-bardeen-vgtwus` del que el coordinador creó la worktree), `ON-PROJECT-BRANCH`, `HAS-PR-13`, `CODE-AS-MEASURED`. Si la primera línea es `dd062f8 Merge pull request #12 …` o falta alguna de las otras tres, se para y se avisa al coordinador. Después, la preparación de Global Constraints.

- [ ] **Step 1: Las pruebas**

Al final de `backend/tests/test_oauth_token.py`:

```python
# --------------------------------------------------------------------------- #
# A reuse after the grant already ended (design 2026-11-23 §6)
# --------------------------------------------------------------------------- #


async def test_a_code_reused_after_its_grant_ended_is_audited_too(env: Env) -> None:
    """The grant the code opened was ended by the app's own revocation; the
    code presented again ends nothing more, and is still a reuse to audit:
    a spent code in somebody's hands is the trace of a theft whenever it
    arrives."""
    code, verifier = await _code(env)
    first = await _exchange(env, code, verifier)
    assert first.status_code == 200
    assert (await _revoke(env, first.json()["refresh_token"])).status_code == 200
    again = await _exchange(env, code, verifier)
    assert again.status_code == 400
    assert again.json()["error"] == "invalid_grant"
    (family,) = await _families(env)
    assert family.revoked_reason == "revoked_by_app"  # not overwritten
    (event,) = await _events(env, "mcp.code_reused")
    assert event.actor_type == "system"
    assert event.target_id == str(await user_id_of(env.api, env.sub))
    assert event.meta == {
        "app": env.app.client_id,
        "family": str(family.id),
        "grant": "already_ended",
    }


async def test_a_refresh_token_reused_after_its_grant_ended_is_audited_too(env: Env) -> None:
    first = await _tokens(env)
    rotated = await _refresh(env, first["refresh_token"])
    assert rotated.status_code == 200
    assert (await _revoke(env, rotated.json()["refresh_token"])).status_code == 200
    reused = await _refresh(env, first["refresh_token"])
    assert reused.status_code == 400
    assert reused.json()["error"] == "invalid_grant"
    (family,) = await _families(env)
    assert family.revoked_reason == "revoked_by_app"
    (event,) = await _events(env, "mcp.refresh_reused")
    assert event.actor_type == "system"
    assert event.meta == {
        "app": env.app.client_id,
        "family": str(family.id),
        "grant": "already_ended",
    }


async def test_the_live_token_of_an_ended_grant_is_not_a_reuse(env: Env) -> None:
    """Never used, so never reused: the token the revocation ended is refused
    plainly, and nothing is audited for it."""
    tokens = await _tokens(env)
    assert (await _revoke(env, tokens["refresh_token"])).status_code == 200
    assert (await _refresh(env, tokens["refresh_token"])).json()["error"] == "invalid_grant"
    assert await _events(env, "mcp.refresh_reused") == []
```

En `backend/tests/auth_support.py`, `import asyncio` entre los imports de la biblioteca estándar (antes de `import secrets`), y en `MailBox` y en el relé falso:

```python
@dataclass
class MailBox:
    sent: list[Sent] = field(default_factory=list)
    configured: bool = True
    failing: bool = False
    #: Seconds the relay takes before it answers: a slow SMTP server.
    delay_s: float = 0.0
```

```python
        if not box.configured:
            raise MailNotConfigured()
        if box.delay_s:
            await asyncio.sleep(box.delay_s)
        if box.failing:
            raise MailSendFailed(MailFailure.UNREACHABLE)
        box.sent.append(Sent(to=to, subject=subject, text=text))
```

Al final de `backend/tests/test_auth_password_reset.py` (ya importa `timedelta`, `select`, `AsyncSession` y `AuthActionToken`):

```python
async def test_the_links_half_hour_starts_when_the_relay_has_taken_the_mail(
    auth_env: Any,
) -> None:
    """Design 2026-11-23 §6: a relay that takes its time spends none of the
    link's 30 minutes. The clock starts when the mail is handed over, not
    when the link is minted."""
    org = await auth_env.create_org("reset-slow-relay")
    who = await signed_in(auth_env, org, "technician")
    auth_env.mail.delay_s = 1.5

    await _forgot(auth_env, who.email)

    async with AsyncSession(auth_env.admin_engine) as session:
        row = (
            await session.execute(
                select(AuthActionToken).where(
                    AuthActionToken.user_id == who.user_id, AuthActionToken.purpose == "reset"
                )
            )
        ).scalar_one()
    assert row.expires_at - row.created_at >= timedelta(minutes=30, seconds=1.4)
    assert row.expires_at - row.created_at < timedelta(minutes=30, seconds=30)
```

En `backend/tests/test_user_admin_api.py`, `from datetime import timedelta` tras `import urllib.request`, `from app.models.auth import AuthActionToken` tras `from app.models import AuditEvent, Client, Role, User, UserRole`, y al final:

```python
async def test_an_invitations_72_hours_start_when_the_relay_has_taken_the_mail(
    users_env: Any,
) -> None:
    """Design 2026-11-23 §6: the link's clock starts after the send, so a
    slow relay spends none of it."""
    org = await users_env.create_org("invite-slow-relay")
    _sub, headers, _id = await _admin(users_env, org)
    users_env.mail.delay_s = 1.5

    body = (await _invite(users_env, headers)).json()

    async with AsyncSession(users_env.admin_engine) as session:
        row = (
            await session.execute(
                select(AuthActionToken).where(
                    AuthActionToken.user_id == UUID(body["user_id"]),
                    AuthActionToken.purpose == "invite",
                )
            )
        ).scalar_one()
    assert row.expires_at - row.created_at >= timedelta(hours=72, seconds=1.4)
```

- [ ] **Step 2: Las pruebas fallan**

Run: `cd backend && .venv/bin/pytest -q tests/test_oauth_token.py tests/test_auth_password_reset.py tests/test_user_admin_api.py -k "after_its_grant_ended or live_token_of_an_ended or half_hour or 72_hours"`
Expected: `4 failed, 1 passed` (comprobado el 2026-11-23 en una copia de `1bc0e5e`): las dos de reuso con `ValueError: not enough values to unpack (expected 1, got 0)` (ningún evento), las dos del reloj con `expires_at - created_at == 30 min` (o 72 h) exactos; pasa la del token vivo, que fija lo que no cambia.

- [ ] **Step 3: La implementación**

Desde `backend/`:

```bash
python3 - <<'PY'
from pathlib import Path

EDITS = {
    "app/core/auth/oauth/grants.py": [
        (
            """    if row.used_at is not None:
        if row.family_id is not None and await revoke_family(
            session, row.family_id, "code_reused", now
        ):
            reused: dict[str, str] = {"app": app.client_id, "family": str(row.family_id)}
            if client.id != app.id:
""",
            """    if row.used_at is not None:
        if row.family_id is not None:
            # A reuse is audited whether or not it ends the grant: one that
            # an earlier act had already ended is the same trace of a theft
            # (design 2026-11-23 §6), with nothing left to revoke.
            ended = await revoke_family(session, row.family_id, "code_reused", now)
            reused: dict[str, str] = {"app": app.client_id, "family": str(row.family_id)}
            if not ended:
                reused["grant"] = "already_ended"
            if client.id != app.id:
""",
        ),
        (
            """    if family.revoked_at is not None or family.expires_at <= now:
        return BAD_REFRESH
    if token.used_at is not None:
""",
            """    if family.revoked_at is not None or family.expires_at <= now:
        if token.used_at is not None:
            # A used token of a grant that already ended: nothing to revoke,
            # and still a reuse to audit (design 2026-11-23 §6).
            await audit_service.record(  # 7. the chain, last
                session,
                org_id=org_id,
                actor_type="system",
                actor_id=None,
                action="mcp.refresh_reused",
                target_type="user",
                target_id=str(family.user_id),
                metadata={
                    "app": app.client_id,
                    "family": str(family.id),
                    "grant": "already_ended",
                },
            )
        return BAD_REFRESH
    if token.used_at is not None:
""",
        ),
    ],
    "app/core/auth/action_tokens.py": [
        (
            "def digest(token: str) -> bytes:",
            '''async def start_clock(session: AsyncSession, token: str, ttl: timedelta) -> None:
    """A MAILED LINK'S LIFETIME STARTS WHEN THE RELAY HAS TAKEN THE MAIL
    (design 2026-11-23-vps-y-marca-novieri §6). `issue` dates the link when it
    is minted, before the send; a relay that answers slowly -- the SMTP
    timeout is long -- spent that time out of the link's lifetime before
    anybody could read it. Called right after a delivery that succeeded,
    in the same transaction; a link already used or replaced does not move."""
    await session.execute(
        update(AuthActionToken)
        .where(AuthActionToken.token_hash == _digest(token), AuthActionToken.used_at.is_(None))
        .values(expires_at=datetime.now(UTC) + ttl)
    )


def digest(token: str) -> bytes:''',
        ),
    ],
    "app/core/auth/flows.py": [
        (
            """            await notices.deliver(
                session,
                org_id=user.org_id,
                to=user.email,
                subject=subject,
                text=text,
                vault=app.state.vault,
                actor_id=None,
            )
""",
            """            if await notices.deliver(
                session,
                org_id=user.org_id,
                to=user.email,
                subject=subject,
                text=text,
                vault=app.state.vault,
                actor_id=None,
            ):
                await action_tokens.start_clock(session, token, action_tokens.RESET_TTL)
""",
        ),
    ],
    "app/modules/user_admin/service.py": [
        (
            """    view = await _view(session, org_id, user)
    return InvitedUser(user=view, link=link, delivered=await deliver(view, link))""",
            """    view = await _view(session, org_id, user)
    delivered = await deliver(view, link)
    if delivered:
        await action_tokens.start_clock(session, token, action_tokens.INVITE_TTL)
    return InvitedUser(user=view, link=link, delivered=delivered)""",
        ),
    ],
    "app/services/contact_access.py": [
        (
            """    link = action_tokens.link(console_url, "invite", token)
    return link, await deliver(user, link, reset)""",
            """    link = action_tokens.link(console_url, "invite", token)
    delivered = await deliver(user, link, reset)
    if delivered:
        await action_tokens.start_clock(session, token, action_tokens.INVITE_TTL)
    return link, delivered""",
        ),
    ],
}
for rel, pairs in EDITS.items():
    path = Path(rel)
    text = path.read_text()
    for old, new in pairs:
        assert text.count(old) == 1, (rel, old[:60])
        text = text.replace(old, new)
    path.write_text(text)
    print("rewrote", rel)
PY
```

Expected: cinco líneas `rewrote app/...`.

- [ ] **Step 4: Las pruebas pasan**

Run: `.venv/bin/pytest -q tests/test_oauth_token.py tests/test_mcp_grant_lifecycle.py tests/test_oauth_retention.py tests/test_mcp_admin_api.py tests/test_audit_actor_names.py tests/test_auth_password_reset.py tests/test_user_admin_api.py tests/test_contact_access_api.py tests/test_auth_retention.py tests/test_auth_recover_command.py && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: PASS (en la copia: `209 passed` las cinco primeras, `132 passed` las cinco siguientes: 130 que recogen sobre `1bc0e5e` más las dos nuevas; recontado el 2026-11-24), `Success`, `All checks passed!`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/auth/oauth/grants.py backend/app/core/auth/action_tokens.py backend/app/core/auth/flows.py backend/app/modules/user_admin/service.py backend/app/services/contact_access.py backend/tests/auth_support.py backend/tests/test_oauth_token.py backend/tests/test_auth_password_reset.py backend/tests/test_user_admin_api.py
git commit -m "fix(auth): a reuse on an ended AI grant is audited; a mailed link's clock starts after the send"
```

---

### Task 6: La prueba Go de la desconexión espera por condición

**Files:**
- Create: `agent/internal/capabilities/concurrency_wait_test.go`
- Modify: `agent/internal/capabilities/concurrency_harness_test.go` (`awaitResults` `:364-416`, `awaitReconnect` `:624-640`, y `TestAJobSurvivesADisconnectAndItsResultArrivesAfterReconnect` `:1057-1157`)

**Interfaces:**
- Consumes: nada.
- Produces (solo en el paquete de pruebas `capabilities_test`): `waitResults(ctx context.Context, outbound <-chan []byte, ids []string) (map[string]wire.JobResult, error)`, `untilTestEnds(t *testing.T) (context.Context, context.CancelFunc)`; `awaitResults(t, ids)` y `awaitReconnect(t, after)` pierden su argumento de duración.

**Decisiones de la tarea.**
- **Qué prueba es (diseño §6, «el presupuesto fijo de 15 s de la prueba del agente Go»):** `TestAJobSurvivesADisconnectAndItsResultArrivesAfterReconnect`, la única con un plazo fijo de 15 s del que el código guarda un fallo en un *runner* cargado («no job_result arrived within 15s for 1 of 3 jobs», segunda vez el 2026-09-08, `:1057-1072`). Sus dos esperas de 15 s (`:1133`, `:1148`) pasan a ser una condición. **Es esa prueba**: el coordinador mantuvo esta tarea el 2026-11-24 (`.git/sdd/decisiones-p34.md`) tras la revisión previa, que lo comprobó: el comentario de la prueba (`:1056-1082`) llama «the budget» a esos 15 s y guarda los dos fallos de CI, y ninguna otra prueba Go tiene un plazo fijo de 15 s con un fallo registrado (`remote/remote_test.go:83` es un `select` sin fallo guardado; `backup/snapshot_test.go:504` es una cota superior que tiene que seguir fija).
- **La condición mantiene la señal que el comentario defendía** («Raising 15 s … would destroy the only signal», `:1074-1082`): un trabajo **varado** no contesta nunca y la prueba sigue fallando, en el plazo del propio binario de prueba (`go test -timeout`, 10 min por defecto) menos 30 s, con el mismo diagnóstico; uno **lento** se espera y su tiempo queda en el registro (`t.Logf`). El comentario de la prueba lo dice, fechado.
- **`waitResults` devuelve un error en lugar de llamar a `t.Fatal`**: así su fallo se puede probar (la prueba nueva lo hace con un `context` de 200 ms) sin un `testing.T` falso.

- [ ] **Step 0: La worktree**

Run, desde la raíz de la worktree, la comprobación de Global Constraints («Pistas en paralelo») y la de esta oleada:

```bash
git fetch -q origin claude/compassionate-bardeen-vgtwus
git log --oneline -1
git merge-base --is-ancestor HEAD origin/claude/compassionate-bardeen-vgtwus && echo ON-PROJECT-BRANCH
git merge-base --is-ancestor 6a14dbc HEAD && echo HAS-PR-13
git diff --quiet 1bc0e5e HEAD -- backend agent deploy .github scripts && echo CODE-AS-MEASURED
```

Expected: `21e72ec docs(plan): project 3 plan, pre-flight fixes in progress (snapshot)` (o el commit posterior de `origin/claude/compassionate-bardeen-vgtwus` del que el coordinador creó la worktree), `ON-PROJECT-BRANCH`, `HAS-PR-13`, `CODE-AS-MEASURED`. Si la primera línea es `dd062f8 Merge pull request #12 …` o falta alguna de las otras tres, se para y se avisa al coordinador. Esta tarea no necesita Postgres, Redis ni Docker: solo Go, desde `agent/`.

- [ ] **Step 1: La prueba de la espera**

Crear `agent/internal/capabilities/concurrency_wait_test.go`:

```go
package capabilities_test

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"
)

// The two waits of TestAJobSurvivesADisconnectAndItsResultArrivesAfterReconnect
// are a CONDITION, bounded by the test binary's own deadline, not a fixed 15 s
// (design 2026-11-23-vps-y-marca-novieri §6). These hold the wait to that:
// a slow answer is waited for, a missing one still fails -- with the same
// diagnosis the fixed deadline gave.

func frame(kind, id, state string) []byte {
	return []byte(fmt.Sprintf(`{"v":1,"kind":%q,"job_id":%q,"state":%q}`, kind, id, state))
}

func TestWaitResultsWaitsForASlowAnswer(t *testing.T) {
	out := make(chan []byte, 8)
	go func() {
		out <- frame("job_status", "a", "running")
		out <- frame("job_result", "a", "succeeded")
		time.Sleep(300 * time.Millisecond) // slower than nothing, well inside the bound
		out <- frame("job_result", "b", "succeeded")
	}()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	got, err := waitResults(ctx, out, []string{"a", "b"})
	if err != nil {
		t.Fatalf("a slow answer was not waited for: %v", err)
	}
	if got["a"].State != "succeeded" || got["b"].State != "succeeded" {
		t.Fatalf("results = %+v", got)
	}
}

func TestWaitResultsStillFailsAMissingAnswerAndSaysWhatItSaw(t *testing.T) {
	out := make(chan []byte, 8)
	out <- frame("job_status", "a", "running")
	out <- frame("job_status", "b", "running")
	out <- frame("job_result", "a", "succeeded")
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	_, err := waitResults(ctx, out, []string{"a", "b", "c"})
	if err == nil {
		t.Fatal("two answers missing, and the wait reported none")
	}
	for _, want := range []string{
		"2 of 3 jobs",
		"b (a job_status WAS seen for it",
		"c (no job_status ever seen for it",
		"map[job_result:1 job_status:2]",
	} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the failure does not say %q: %v", want, err)
		}
	}
}

func TestTheWaitIsBoundedByTheTestsOwnDeadline(t *testing.T) {
	ctx, cancel := untilTestEnds(t)
	defer cancel()
	deadline, ok := ctx.Deadline()
	if !ok {
		t.Fatal("the wait has no bound at all")
	}
	if testEnd, set := t.Deadline(); set && !deadline.Before(testEnd) {
		t.Fatalf("the wait ends at %s, not before the test binary's own %s", deadline, testEnd)
	}
}
```

- [ ] **Step 2: Falla**

Run: `cd agent && go vet ./internal/capabilities/`
Expected: FAIL — `vet: internal/capabilities/concurrency_wait_test.go:31:14: undefined: waitResults` (comprobado sobre `1bc0e5e`).

- [ ] **Step 3: La espera por condición**

Desde `agent/`:

```bash
python3 - <<'PY'
"""Task 6, Step 3: the disconnect test waits for a condition. Run from agent/."""
import sys
from pathlib import Path

path = Path("internal/capabilities/concurrency_harness_test.go")
s = path.read_text()

start = s.index("func (h *concurrencyHarness) awaitResults(")
end = s.index("// kindOf peeks an outbound frame's")
s = s[:start] + '''func (h *concurrencyHarness) awaitResults(t *testing.T, ids []string) map[string]wire.JobResult {
	t.Helper()
	ctx, cancel := untilTestEnds(t)
	defer cancel()
	started := time.Now()
	got, err := waitResults(ctx, h.gateway.outbound, ids)
	if err != nil {
		t.Fatal(err)
	}
	// Logged, not asserted: "slow" is data for the next reader of a failure
	// in this test, never a reason to fail it (design 2026-11-23 §6).
	t.Logf("all %d job_results arrived in %s", len(ids), time.Since(started))
	return got
}

// untilTestEnds is the bound every condition wait in this file uses: the
// test binary's own deadline (`go test -timeout`, ten minutes unless set),
// less half a minute for the failure to be written. A missing answer still
// fails -- at that bound, with the diagnosis below -- and a slow one is
// waited for. The fixed 15 s this replaced failed a loaded runner whose
// third job was merely slow (the 2026-09-08 occurrence, recorded above
// TestAJobSurvivesADisconnectAndItsResultArrivesAfterReconnect).
func untilTestEnds(t *testing.T) (context.Context, context.CancelFunc) {
	deadline, ok := t.Deadline()
	if !ok {
		deadline = time.Now().Add(10 * time.Minute)
	}
	return context.WithDeadline(context.Background(), deadline.Add(-30*time.Second))
}

// waitResults reads outbound until a job_result has arrived for every id in
// ids, and returns them keyed by job id; or, when ctx ends first, an error
// that says what a reproduction would otherwise be needed to find out:
// which ids never got a job_result, whether each of those ever produced a
// job_status (started, as opposed to never dispatched), and how many frames
// of each kind were drained while waiting.
//
// It exists because a loop over awaitResult DISCARDS every job_result that is
// not the one it was asked for, and so loses one when the answers cross the
// wire in another order -- which produced a timeout naming the wrong job
// while this file's disconnect test was being written. Anything expecting
// more than one answer must use this.
func waitResults(ctx context.Context, outbound <-chan []byte, ids []string) (map[string]wire.JobResult, error) {
	want := make(map[string]bool, len(ids))
	for _, id := range ids {
		want[id] = true
	}
	got := make(map[string]wire.JobResult, len(ids))
	started := make(map[string]bool, len(ids)) // ids for which a job_status was seen
	frameCounts := make(map[string]int)        // every frame kind drained, including ones not addressed to `ids`
	for len(got) < len(want) {
		select {
		case frame := <-outbound:
			var envelope struct {
				Kind string `json:"kind"`
			}
			if err := json.Unmarshal(frame, &envelope); err != nil {
				return nil, fmt.Errorf("outbound frame is not JSON: %v (%s)", err, frame)
			}
			frameCounts[envelope.Kind]++
			switch envelope.Kind {
			case wire.KindJobStatus:
				var s wire.JobStatus
				if err := json.Unmarshal(frame, &s); err == nil && want[s.JobID] {
					started[s.JobID] = true
				}
			case wire.KindJobResult:
				var r wire.JobResult
				if err := json.Unmarshal(frame, &r); err != nil {
					return nil, fmt.Errorf("undecodable job_result frame: %v (%s)", err, frame)
				}
				if want[r.JobID] {
					got[r.JobID] = r
				}
			}
		case <-ctx.Done():
			var missing []string
			total := 0
			for _, n := range frameCounts {
				total += n
			}
			for _, id := range ids {
				if _, ok := got[id]; ok {
					continue
				}
				status := "no job_status ever seen for it (it may never have started)"
				if started[id] {
					status = "a job_status WAS seen for it (it started, but never reported a result)"
				}
				missing = append(missing, fmt.Sprintf("%s (%s)", id, status))
			}
			return nil, fmt.Errorf("no job_result arrived before the test's deadline for %d of %d jobs: %v; "+
				"drained %d frames total while waiting, by kind: %v", len(missing), len(ids), missing, total, frameCounts)
		}
	}
	return got, nil
}

''' + s[end:]

old_rc = s[s.index("// awaitReconnect blocks until a connection NEWER"):s.index("func newHarnessGateway(")]
s = s.replace(old_rc, '''// awaitReconnect blocks until a connection NEWER than after has been accepted,
// or the test binary's own deadline is near (untilTestEnds). Polling rather
// than a channel because the accept happens inside net/http's handler
// goroutine, which this harness does not own.
func (g *harnessGateway) awaitReconnect(t *testing.T, after int) {
	t.Helper()
	ctx, cancel := untilTestEnds(t)
	defer cancel()
	for g.gen() <= after {
		select {
		case <-ctx.Done():
			t.Fatalf("the agent did not reconnect before the test's deadline (still on connection %d)", g.gen())
		case <-time.After(5 * time.Millisecond):
		}
	}
}

''')

for old, new in (
    ("h.gateway.awaitReconnect(t, gen, 15*time.Second)", "h.gateway.awaitReconnect(t, gen)"),
    ("results := h.awaitResults(t, ids, 15*time.Second)", "results := h.awaitResults(t, ids)"),
    (
        """	// awaitReconnect above would have called t.Fatalf if the reconnect did not
	// complete within 15s, so reaching this line already proves it completed;""",
        """	// awaitReconnect above would have called t.Fatalf if the reconnect did not
	// complete before the test's deadline, so reaching this line already proves it completed;""",
    ),
    (
        """// dropped connection must not strand a running job. The deadline stays; a
// third occurrence with this data is what would separate "slow" from "stuck".
""",
        """// dropped connection must not strand a running job. The deadline stays; a
// third occurrence with this data is what would separate "slow" from "stuck".
//
// SINCE 2026-11-23 THE WAIT IS A CONDITION (design 2026-11-23-vps-y-marca-
// novieri §6), and that keeps the signal rather than widening it away: a
// STRANDED job still never answers, so this still fails -- at the test
// binary's own deadline (untilTestEnds), with the same diagnosis -- while a
// SLOW one is waited for and its time logged. "Slow" and "stuck" are now told
// apart by the outcome instead of by a guess at a number.
""",
    ),
):
    if s.count(old) != 1:
        sys.exit(f"not there exactly once: {old[:60]!r}")
    s = s.replace(old, new)
path.write_text(s)
print("rewrote", path)
PY
```

Expected: `rewrote internal/capabilities/concurrency_harness_test.go`.

- [ ] **Step 4: Pasa**

Run: `gofmt -l internal/capabilities && go vet ./... && go test -count=1 -v -run 'WaitResults|TheWaitIsBounded|TestAJobSurvivesADisconnect' ./internal/capabilities/ && go test -count=1 ./internal/capabilities/`
Expected: `gofmt` no imprime nada; las cuatro `--- PASS`, entre ellas el registro `all 3 job_results arrived in …` y `reconnect completed in …`; el paquete `ok` (comprobado el 2026-11-23: `ok … 1.8s` las cuatro).

- [ ] **Step 5: Commit**

```bash
git add agent/internal/capabilities/concurrency_wait_test.go agent/internal/capabilities/concurrency_harness_test.go
git commit -m "test(agent): the disconnect test waits for its answers until the test's own deadline, not a fixed 15 s"
```

---

### Task 7: La CI `deploy` obtiene su certificado de Pebble por ACME

**Files:**
- Create: `deploy/ci/acme-pebble.yml`, `deploy/ci/pebble-config.json`
- Modify: `.github/workflows/ci.yml` (job `deploy`: el paso «Throwaway .env» y el paso «the platform's own OAuth server and MCP door, live, through Traefik»)
- Modify: `deploy/ci/mcp_flow.py` (`--resolve`, `--state-file`, `parse_resolve`, `pin_resolution`, `write_owner_state`; `org` y `client_name`)
- Modify: `backend/tests/test_ci_mcp_flow.py` (tres aserciones de antes y seis pruebas nuevas al final)

**Interfaces:**
- Consumes: de la Task 3, el resolutor `le`, `${ITS_ACME_CA_SERVER}` en el `command:` de traefik, los `Host()`, el router `api-unavailable`; de la Task 4, `new-env.sh --host … --acme-email … --acme-ca-server …` y `check-public-tls.sh --connect … --cafile … --wait …`. **Se fusiona junto con las Tasks 3 y 4.**
- Produces:
  - `deploy/ci/acme-pebble.yml`: un archivo de compose que se añade con `COMPOSE_FILE=docker-compose.yml:ci/acme-pebble.yml` y exige `PEBBLE_MINICA` (la ruta del `pebble.minica.pem` sacado de la imagen); traefik en `172.19.240.250`, challtestsrv en `.251`, pebble en `.252`.
  - `mcp_flow.py --resolve NAME:IPv4` (repetible) y `--state-file RUTA` (fase `open`: escribe `{"email", "password", "recovery_codes": [dos]}` en un archivo nuevo 0600); `parse_resolve(value: str) -> tuple[str, str]`, `pin_resolution(pins: Mapping[str, str]) -> None`, `write_owner_state(path: str, email: str, password: str, recovery: list[str]) -> None`; `Flow(client, state_file=None)`.
  - En el paso de la CI: la variable de shell `pebble_root` (`$RUNNER_TEMP/pebble-root.pem`), las funciones `get`, `wait_for_api`, `flow`, y el archivo `$RUNNER_TEMP/ci-owner.json` en el `trap`. La Task 10 añade su bloque **dentro de este mismo paso**, después del 7b.

**Decisiones de la tarea.**
- **`COMPOSE_FILE` en `$GITHUB_ENV`**: todos los `docker compose` del job, los de los guiones incluidos (`backup.sh`, `restore.sh`), leen los dos archivos sin cambiar ni una línea suya. Compose los separa con `:` en Linux.
- **Pebble y su servidor de retos en el mismo `docker compose`**, con direcciones fijas en la red del proyecto: Pebble valida conectando a `rmm.novieri.com:443`, que `pebble-challtestsrv` resuelve a traefik (`-defaultIPv4`), sin `AAAA` (`-defaultIPv6 ""`) y sin sus propios servidores de retos. `tlsPort: 443` en `pebble-config.json` (por defecto Pebble valida en el 5001 [E9]). `PEBBLE_VA_NOSLEEP=1` y `PEBBLE_WFE_NONCEREJECT=0` quitan las esperas y los rechazos aleatorios que Pebble mete a propósito [E9].
- **La raíz de Pebble se lee de su puerto de gestión** (`/roots/0`): Pebble genera raíz e intermedia nuevas en cada arranque [E9], así que no puede estar en el repositorio.
- **El runner llega a `rmm.novieri.com` en `127.0.0.1`** por `--resolve` (curl y `mcp_flow.py`), no por `/etc/hosts`: `vars.CI_RUNS_ON` puede ser un runner propio sin `sudo`.
- **`pebble_root`, no `root`**: `root` es ya el token raíz de OpenBao en ese mismo paso.
- **La prueba del 503 va en la CI** (7b): con el contenedor `api` parado, Traefik real responde `503` en `/api/v1/setup/status`; la Task 3 lo prueba en el modelo.
- **El estado del propietario** (correo, contraseña y dos códigos de recuperación) existe solo para que el `agent_flow.py` de la Task 10 vuelva a entrar sin navegador; un código de recuperación no depende del reloj como un TOTP, que no admite reutilizar el paso de 30 s en que se dio de alta.

- [ ] **Step 0: La worktree**

Run, desde la raíz de la worktree, la comprobación de Global Constraints («Pistas en paralelo») y la de esta oleada:

```bash
git fetch -q origin claude/compassionate-bardeen-vgtwus
git log --oneline -1
git merge-base --is-ancestor HEAD origin/claude/compassionate-bardeen-vgtwus && echo ON-PROJECT-BRANCH
git merge-base --is-ancestor 6a14dbc HEAD && echo HAS-PR-13
git diff --quiet 1bc0e5e HEAD -- backend agent deploy .github scripts && echo CODE-AS-MEASURED
```

Expected: `21e72ec docs(plan): project 3 plan, pre-flight fixes in progress (snapshot)` (o el commit posterior de `origin/claude/compassionate-bardeen-vgtwus` del que el coordinador creó la worktree), `ON-PROJECT-BRANCH`, `HAS-PR-13`, `CODE-AS-MEASURED`. Si la primera línea es `dd062f8 Merge pull request #12 …` o falta alguna de las otras tres, se para y se avisa al coordinador. Después, la preparación de Global Constraints con Docker y las imágenes base de la sesión.

- [ ] **Step 1: Las pruebas**

Lo que `test_ci_mcp_flow.py` ya fijaba, a los nombres nuevos, desde la raíz de la worktree:

```bash
python3 - <<'PY'
"""Task 7, Step 1: what test_ci_mcp_flow.py already pinned, moved to the new
names. Run from the repository root, before appending the new tests."""
from pathlib import Path

path = Path("backend/tests/test_ci_mcp_flow.py")
text = path.read_text()
for old, new in (
    (
        '''    assert """trap 'rm -f "$init_json" "$RUNNER_TEMP/setup-code"' EXIT""" in run''',
        '''    assert (
        """trap 'rm -f "$init_json" "$RUNNER_TEMP/setup-code" "$RUNNER_TEMP/ci-owner.json"' EXIT"""
        in run
    )''',
    ),
    (
        '    assert "https://localhost/.well-known/oauth-authorization-server" in wait',
        '    assert "https://rmm.novieri.com/.well-known/oauth-authorization-server" in wait',
    ),
    (
        '        "serverInfo": {"name": "itms", "version": "1"},',
        '        "serverInfo": {"name": "novieri-rmm", "version": "1"},',
    ),
):
    assert text.count(old) == 1, old
    text = text.replace(old, new)
path.write_text(text)
print("rewrote", path)
PY
```

Y al final de `backend/tests/test_ci_mcp_flow.py` (el archivo ya importa `json`, `Path`, `pytest` y `yaml`, y define `REPO`, `CI`, `_deploy_step`, `OAUTH_STEP` y `flow`):

```python


# --------------------------------------------------------------------------- #
# The certificate from Pebble over ACME (design 2026-11-23-vps-y-marca-novieri
# §4), and the names the runner reaches the stack by
# --------------------------------------------------------------------------- #

PEBBLE = REPO / "deploy/ci/acme-pebble.yml"


def test_the_throwaway_env_is_the_one_a_new_host_gets_with_pebble_as_its_ca() -> None:
    run = _deploy_step("Throwaway .env, written by the script a new host uses")
    assert (
        "./scripts/new-env.sh --host rmm.novieri.com --acme-email ci@novieri.example \\\n"
        "  --acme-ca-server https://pebble:14000/dir"
    ) in run
    assert 'echo "COMPOSE_FILE=docker-compose.yml:ci/acme-pebble.yml" >> "$GITHUB_ENV"' in run
    assert 'docker cp "$cid:/test/certs/pebble.minica.pem" "$RUNNER_TEMP/pebble-minica.pem"' in run
    assert "change-me" not in run, "the .env is new-env.sh's, not a sed of the example"


def test_the_deploy_job_copies_no_certificate_and_checks_the_one_acme_gave() -> None:
    steps = yaml.safe_load(CI.read_text())["jobs"]["deploy"]["steps"]
    whole = "\n".join(str(step.get("run", "")) for step in steps)
    assert "deploy/certs" not in whole and "certs/fullchain.pem" not in whole
    assert "openssl req" not in whole
    assert "https://localhost" not in whole
    run = _deploy_step(OAUTH_STEP)
    check = run.index(
        './scripts/check-public-tls.sh --connect 127.0.0.1 --cafile "$pebble_root" --wait 180'
    )
    assert run.index("https://pebble:15000/roots/0") < check < run.index("flow --phase closed")
    assert (
        "flow() { python3 ci/mcp_flow.py --base https://rmm.novieri.com "
        '--resolve rmm.novieri.com:127.0.0.1 --cafile "$pebble_root" "$@"; }'
    ) in run


def test_with_the_api_stopped_the_job_expects_503() -> None:
    run = _deploy_step(OAUTH_STEP)
    stop = run.index("docker compose stop api")
    assert run.index('[ "$answered" = 503 ]', stop) < run.index("docker compose start api", stop)
    assert run.index("flow --phase open") < stop


def test_the_override_is_pebble_and_trust_in_it_and_nothing_else() -> None:
    services = yaml.safe_load(PEBBLE.read_text())["services"]
    assert set(services) == {"traefik", "challtestsrv", "pebble"}
    assert set(services["traefik"]) == {"environment", "volumes", "networks", "depends_on"}
    assert services["traefik"]["environment"] == {
        "LEGO_CA_CERTIFICATES": "/pebble/pebble.minica.pem"
    }
    assert "command" not in services["traefik"], "the production command is not repeated"
    assert services["pebble"]["image"] == "ghcr.io/letsencrypt/pebble:2.10.1"
    assert services["challtestsrv"]["image"] == "ghcr.io/letsencrypt/pebble-challtestsrv:2.10.1"
    config = json.loads((REPO / "deploy/ci/pebble-config.json").read_text())["pebble"]
    assert config["tlsPort"] == 443, "Pebble validates TLS-ALPN-01 where Let's Encrypt does"


def test_resolve_pins_one_name_and_leaves_the_rest() -> None:
    assert flow.parse_resolve("rmm.novieri.com:127.0.0.1") == ("rmm.novieri.com", "127.0.0.1")
    for bad in ("rmm.novieri.com", "rmm.novieri.com:::1", "RMM:1.2.3.4", ":127.0.0.1"):
        with pytest.raises(flow.argparse.ArgumentTypeError):
            flow.parse_resolve(bad)
    real = flow.socket.getaddrinfo
    try:
        flow.pin_resolution({"rmm.novieri.com": "127.0.0.1"})
        pinned = flow.socket.getaddrinfo("rmm.novieri.com", 443, flow.socket.AF_INET)
        assert {entry[4][0] for entry in pinned} == {"127.0.0.1"}
        other = flow.socket.getaddrinfo("localhost", 443, flow.socket.AF_INET)
        assert other == real("localhost", 443, flow.socket.AF_INET)
    finally:
        flow.socket.getaddrinfo = real


def test_the_owners_sign_in_is_written_once_and_only_for_the_owner(tmp_path: Path) -> None:
    path = tmp_path / "ci-owner.json"
    flow.write_owner_state(str(path), "owner@ci.invalid", "pw", ["r1", "r2"])
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert json.loads(path.read_text()) == {
        "email": "owner@ci.invalid",
        "password": "pw",
        "recovery_codes": ["r1", "r2"],
    }
    with pytest.raises(FileExistsError):
        flow.write_owner_state(str(path), "x", "y", [])
```

- [ ] **Step 2: Las pruebas fallan**

Run: `cd backend && .venv/bin/pytest -q tests/test_ci_mcp_flow.py`
Expected: `8 failed, 43 passed` (comprobado sobre `1bc0e5e`): el paso del `.env` no existe con ese nombre, `certs/fullchain.pem` y `https://localhost` siguen en el job, no hay `docker compose stop api`, `acme-pebble.yml` no existe, `flow.parse_resolve` y `flow.write_owner_state` no existen, y las tres aserciones movidas.

- [ ] **Step 3: La implementación**

Crear `deploy/ci/acme-pebble.yml`:

```yaml
# Let's Encrypt's own test CA in place of Let's Encrypt, for the CI's `deploy`
# job and a rehearsal of the first boot (design 2026-11-23-vps-y-marca-novieri
# §4). NEVER on a real host: Pebble "is not suited for use as a production CA".
#
#   COMPOSE_FILE=docker-compose.yml:ci/acme-pebble.yml   (every docker compose call)
#   ITS_ACME_CA_SERVER=https://pebble:14000/dir          (in .env, by new-env.sh)
#   PEBBLE_MINICA=<the pebble image's test/certs/pebble.minica.pem, copied out>
#
# What it proves is the production path: Traefik's `le` resolver, TLS-ALPN-01
# answered on :443, a certificate for ITS_PUBLIC_HOST in traefik-acme. Two
# things stand in for the Internet. pebble-challtestsrv is the DNS Pebble's
# validator asks, and it answers every A query with traefik's address on this
# network, so the validation connects to traefik:443 exactly as Let's Encrypt
# connects to the VPS. And Traefik is told to trust Pebble's own HTTPS
# certificate (its ACME directory is https://pebble:14000/dir) through lego's
# LEGO_CA_CERTIFICATES -- an environment variable, so the production
# `command:` list is not repeated here and cannot drift from it.
#
# Addresses are pinned in the compose network (ITMS_NETWORK_SUBNET, default
# 172.19.240.0/24) at the top of the range, which Docker hands out last.
services:
  traefik:
    environment:
      LEGO_CA_CERTIFICATES: /pebble/pebble.minica.pem
    volumes:
      - ${PEBBLE_MINICA:?the pebble image's test/certs/pebble.minica.pem, copied out}:/pebble/pebble.minica.pem:ro
    networks:
      default:
        ipv4_address: 172.19.240.250
    depends_on:
      - pebble

  challtestsrv:
    image: ghcr.io/letsencrypt/pebble-challtestsrv:2.10.1
    # DNS only: every A query answers traefik's address, no AAAA, and none of
    # its own challenge servers (traefik answers TLS-ALPN-01 itself).
    command: ["-defaultIPv4", "172.19.240.250", "-defaultIPv6", "", "-http01", "", "-https01", "", "-tlsalpn01", "", "-doh", ""]
    networks:
      default:
        ipv4_address: 172.19.240.251

  pebble:
    image: ghcr.io/letsencrypt/pebble:2.10.1
    command: ["-config", "/config/pebble-config.json", "-dnsserver", "172.19.240.251:8053"]
    environment:
      # Validate at once, and reject no valid nonce: the defaults sleep and
      # reject 5% of them on purpose, which tests a client's retries, not
      # this deployment.
      PEBBLE_VA_NOSLEEP: "1"
      PEBBLE_WFE_NONCEREJECT: "0"
    volumes:
      - ./ci/pebble-config.json:/config/pebble-config.json:ro
    networks:
      default:
        ipv4_address: 172.19.240.252
    depends_on:
      - challtestsrv
```

Crear `deploy/ci/pebble-config.json` (el `test/config/pebble-config.json` de la imagen 2.10.1, con `httpPort` 80, `tlsPort` 443 y solo el perfil por defecto):

```json
{
  "pebble": {
    "listenAddress": "0.0.0.0:14000",
    "managementListenAddress": "0.0.0.0:15000",
    "certificate": "test/certs/localhost/cert.pem",
    "privateKey": "test/certs/localhost/key.pem",
    "httpPort": 80,
    "tlsPort": 443,
    "ocspResponderURL": "",
    "externalAccountBindingRequired": false,
    "domainBlocklist": ["blocked-domain.example"],
    "retryAfter": {
      "authz": 3,
      "order": 5
    },
    "keyAlgorithm": "ecdsa",
    "profiles": {
      "default": {
        "description": "The profile you know and love",
        "validityPeriod": 7776000
      }
    }
  }
}
```

`mcp_flow.py`, desde la raíz de la worktree:

```bash
python3 - <<'PY'
"""Task 7, Step 3 (second half): mcp_flow.py reaches rmm.novieri.com at an
address it is told, and leaves the owner's sign-in for agent_flow.py (Task 10).
Run from the repository root."""
import sys
from pathlib import Path

EDITS = [
    (
        """    python3 ci/mcp_flow.py --base https://localhost --cafile certs/fullchain.pem \\\\
        --phase closed
    python3 ci/mcp_flow.py --base https://localhost --cafile certs/fullchain.pem \\\\
        --phase open --setup-code-file "$RUNNER_TEMP/setup-code"
""",
        """    python3 ci/mcp_flow.py --base https://rmm.novieri.com \\\\
        --resolve rmm.novieri.com:127.0.0.1 --cafile "$RUNNER_TEMP/pebble-root.pem" \\\\
        --phase closed
    python3 ci/mcp_flow.py --base https://rmm.novieri.com \\\\
        --resolve rmm.novieri.com:127.0.0.1 --cafile "$RUNNER_TEMP/pebble-root.pem" \\\\
        --phase open --setup-code-file "$RUNNER_TEMP/setup-code" \\\\
        --state-file "$RUNNER_TEMP/ci-owner.json"

`--resolve NAME:ADDRESS` (since 2026-11-23, design 2026-11-23-vps-y-marca-
novieri §4) connects to ADDRESS wherever NAME is asked for, as curl's does:
the URL, the Host header, SNI and the certificate check all keep NAME, which
is the only name Traefik's routers answer since every one carries Host().
`--state-file`, in phase open, writes the owner's address, password and two
recovery codes to a new file, mode 0600, for `ci/agent_flow.py` to sign in
with; the step that runs both removes it however it ends.
""",
    ),
    ("import json\nimport re\n", "import json\nimport os\nimport re\n"),
    ("import secrets\nimport ssl\n", "import secrets\nimport socket\nimport ssl\n"),
    ('        "org": "toeshee",\n', '        "org": "novieri",\n'),
    ('                "client_name": "ITMS CI",\n', '                "client_name": "Novieri RMM CI",\n'),
    (
        """def ok(message: str) -> None:
""",
        """def parse_resolve(value: str) -> tuple[str, str]:
    \"\"\"`NAME:ADDRESS` for `--resolve`; an IPv4 address, the only kind the
    stack publishes on.\"\"\"
    name, _, address = value.rpartition(":")
    ipv4 = re.fullmatch(r"\\d{1,3}(\\.\\d{1,3}){3}", address)
    if not re.fullmatch(r"[a-z0-9.-]+", name) or not ipv4:
        raise argparse.ArgumentTypeError(f"{value!r} is not NAME:IPv4-ADDRESS")
    return name, address


def pin_resolution(pins: Mapping[str, str]) -> None:
    \"\"\"From here on, a connection to a pinned name goes to its address. Only
    the lookup changes: `http.client` still sends the name as SNI and checks
    the certificate against it.\"\"\"
    if not pins:
        return
    real = socket.getaddrinfo

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        return real(pins.get(host, host), *args, **kwargs)

    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]


def write_owner_state(path: str, email: str, password: str, recovery: list[str]) -> None:
    \"\"\"The owner's sign-in for `ci/agent_flow.py`: a NEW file, 0600 from its
    creation (`O_EXCL`), never printed.\"\"\"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"email": email, "password": password, "recovery_codes": recovery}, handle)


def ok(message: str) -> None:
""",
    ),
    (
        """class Flow:
    def __init__(self, client: Client) -> None:
        self.http = client
""",
        """class Flow:
    def __init__(self, client: Client, state_file: str | None = None) -> None:
        self.http = client
        self.state_file = state_file
""",
    ),
    (
        """        check(finish.json().get("recovery_codes"), "enrolment returned no recovery codes")
""",
        """        codes = finish.json().get("recovery_codes")
        check(codes, "enrolment returned no recovery codes")
        if self.state_file:
            write_owner_state(self.state_file, email, password, list(codes)[:2])
""",
    ),
    (
        """    parser.add_argument("--wait-registration-s", type=float, default=45.0)
    args = parser.parse_args(argv)
    if args.phase == "open" and not args.setup_code_file:
        parser.error("--phase open needs --setup-code-file")
    flow = Flow(Client(args.base, args.cafile))
""",
        """    parser.add_argument("--wait-registration-s", type=float, default=45.0)
    parser.add_argument(
        "--resolve", type=parse_resolve, action="append", default=[], help="NAME:ADDRESS"
    )
    parser.add_argument("--state-file", help="phase open: the owner's sign-in, for agent_flow.py")
    args = parser.parse_args(argv)
    if args.phase == "open" and not args.setup_code_file:
        parser.error("--phase open needs --setup-code-file")
    pin_resolution(dict(args.resolve))
    flow = Flow(Client(args.base, args.cafile), state_file=args.state_file)
""",
    ),
]

path = Path("deploy/ci/mcp_flow.py")
text = path.read_text()
for old, new in EDITS:
    if text.count(old) != 1:
        sys.exit(f"mcp_flow.py: not there exactly once: {old[:70]!r}")
    text = text.replace(old, new)
path.write_text(text)
print("rewrote deploy/ci/mcp_flow.py")
PY
```

Y el job, desde la raíz:

```bash
python3 - <<'PY'
"""Task 7, Step 3: the deploy job's certificate comes from Pebble over ACME.
Run from the repository root."""
import sys
from pathlib import Path

EDITS = [
    (
        """      - name: Throwaway .env
        # Values are CI-only and generated per run; nothing here is a real
        # credential and nothing is written back to the repo (.env is ignored).
        working-directory: deploy
        run: |
          sed -E "s/^([A-Z_]+)=change-me$/\\1=ci-$(openssl rand -hex 8)/" \\
            .env.example > .env
""",
        """      - name: Throwaway .env, written by the script a new host uses
        # scripts/new-env.sh (design 2026-11-23-vps-y-marca-novieri §3), as on
        # a new VPS, with Pebble -- Let's Encrypt's test CA -- as the ACME
        # directory: ci/acme-pebble.yml, which COMPOSE_FILE adds to every
        # docker compose call from here on, and which needs the CA Pebble's
        # own HTTPS is signed with, copied out of the image. Nothing here is
        # a real credential and nothing is written back to the repo (.env is
        # ignored); the last step fills the two OpenBao tokens.
        working-directory: deploy
        run: |
          ./scripts/new-env.sh --host rmm.novieri.com --acme-email ci@novieri.example \\
            --acme-ca-server https://pebble:14000/dir
          cid="$(docker create ghcr.io/letsencrypt/pebble:2.10.1)"
          docker cp "$cid:/test/certs/pebble.minica.pem" "$RUNNER_TEMP/pebble-minica.pem"
          docker rm "$cid" >/dev/null
          echo "PEBBLE_MINICA=$RUNNER_TEMP/pebble-minica.pem" >> "$GITHUB_ENV"
          echo "COMPOSE_FILE=docker-compose.yml:ci/acme-pebble.yml" >> "$GITHUB_ENV"
""",
    ),
    (
        """      - name: the platform's own OAuth server and MCP door, live, through Traefik
""",
        """      - name: the platform's own OAuth server and MCP door, live, through Traefik
        # SINCE 2026-11-23 (design 2026-11-23-vps-y-marca-novieri §4) THE
        # CERTIFICATE ON :443 IS THE PRODUCTION PATH'S: Traefik's `le`
        # resolver asks Pebble over ACME, TLS-ALPN-01 on :443, for
        # rmm.novieri.com, and check-public-tls.sh accepts it only against
        # Pebble's root -- nothing is copied into deploy/certs/ any more. The
        # runner reaches the stack as rmm.novieri.com at 127.0.0.1 (curl's
        # and mcp_flow.py's --resolve); Pebble's validator reaches traefik
        # through pebble-challtestsrv. And while the api is stopped, its
        # paths answer 503 rather than the console's page (design §6).
""",
    ),
    (
        """          trap 'rm -f "$init_json" "$RUNNER_TEMP/setup-code"' EXIT
""",
        """          trap 'rm -f "$init_json" "$RUNNER_TEMP/setup-code" "$RUNNER_TEMP/ci-owner.json"' EXIT
""",
    ),
    (
        """          sed -i -e '/^ITS_OPENBAO_TOKEN=/d' -e '/^OPENBAO_TOKEN=/d' \\
            -e '/^ITS_PUBLIC_CONSOLE_URL=/d' -e '/^ITS_MCP_REGISTRATION=/d' .env
          {
            echo "ITS_OPENBAO_TOKEN=$app_token"
            echo "OPENBAO_TOKEN=$root"
            echo "ITS_PUBLIC_CONSOLE_URL=https://localhost"
          } >> .env
""",
        """          sed -i -e '/^ITS_OPENBAO_TOKEN=/d' -e '/^OPENBAO_TOKEN=/d' \\
            -e '/^ITS_MCP_REGISTRATION=/d' .env
          {
            echo "ITS_OPENBAO_TOKEN=$app_token"
            echo "OPENBAO_TOKEN=$root"
          } >> .env
""",
    ),
    (
        """          # 3. The audit HMAC key (step 5) and a certificate for :443 (step 7).
          docker compose run --rm --no-deps --entrypoint sh api -c \\
            'head -c 32 /dev/urandom > /var/lib/its/secrets/audit-hmac.key && chmod 600 /var/lib/its/secrets/audit-hmac.key'
          openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=localhost" \\
            -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \\
            -keyout certs/privkey.pem -out certs/fullchain.pem 2>/dev/null
""",
        """          # 3. The audit HMAC key (step 5). The certificate for :443 is
          #    Traefik's to obtain (step 7).
          docker compose run --rm --no-deps --entrypoint sh api -c \\
            'head -c 32 /dev/urandom > /var/lib/its/secrets/audit-hmac.key && chmod 600 /var/lib/its/secrets/audit-hmac.key'
""",
    ),
    (
        """          get() { curl -sf --noproxy '*' --cacert certs/fullchain.pem "$@"; }
""",
        """          pebble_root="$RUNNER_TEMP/pebble-root.pem"
          get() { curl -sf --noproxy '*' --cacert "$pebble_root" --resolve rmm.novieri.com:443:127.0.0.1 "$@"; }
""",
    ),
    (
        """              meta="$(get -o /dev/null -w '%{content_type}' https://localhost/.well-known/oauth-authorization-server || true)"
""",
        """              meta="$(get -o /dev/null -w '%{content_type}' https://rmm.novieri.com/.well-known/oauth-authorization-server || true)"
""",
    ),
    (
        """                  page="$(get -o /dev/null -w '%{content_type}' https://localhost/ || true)"
""",
        """                  page="$(get -o /dev/null -w '%{content_type}' https://rmm.novieri.com/ || true)"
""",
    ),
    (
        """          docker compose up -d --build api web traefik
          wait_for_api
          flow() { python3 ci/mcp_flow.py --base https://localhost --cafile certs/fullchain.pem "$@"; }
""",
        """          docker compose up -d --build api web traefik
          # Pebble makes a new root on every start: the one that signed this
          # run's certificate is read from its management port (15000), which
          # is published nowhere and answers on its address in the network.
          for _ in $(seq 1 30); do
            curl -sf --noproxy '*' --cacert "$PEBBLE_MINICA" --resolve pebble:15000:172.19.240.252 \\
              https://pebble:15000/roots/0 -o "$pebble_root" && break
            sleep 2
          done
          test -s "$pebble_root" || { docker compose logs pebble; echo "::error::pebble never served its root"; exit 1; }
          # THE CHECK THAT GOES RED WITHOUT ACME: Traefik's own default
          # certificate, or one not for rmm.novieri.com, is refused.
          ./scripts/check-public-tls.sh --connect 127.0.0.1 --cafile "$pebble_root" --wait 180 \\
            || { docker compose logs traefik pebble; exit 1; }
          wait_for_api
          flow() { python3 ci/mcp_flow.py --base https://rmm.novieri.com --resolve rmm.novieri.com:127.0.0.1 --cafile "$pebble_root" "$@"; }
""",
    ),
    (
        """          get -o /dev/null https://localhost/api/v1/setup/status
""",
        """          get -o /dev/null https://rmm.novieri.com/api/v1/setup/status
""",
    ),
    (
        """          flow --phase open --setup-code-file "$RUNNER_TEMP/setup-code" || { docker compose logs api web traefik; exit 1; }
""",
        """          flow --phase open --setup-code-file "$RUNNER_TEMP/setup-code" --state-file "$RUNNER_TEMP/ci-owner.json" || { docker compose logs api web traefik; exit 1; }

          # 7b. While no api container is healthy, its paths answer 503 --
          #     the file provider's api-unavailable -- and never the
          #     console's index.html with a 200 (design 2026-11-23 §6).
          docker compose stop api
          answered=""
          for _ in $(seq 1 30); do
            answered="$(curl -s --noproxy '*' --cacert "$pebble_root" --resolve rmm.novieri.com:443:127.0.0.1 \\
              -o /dev/null -w '%{http_code}' https://rmm.novieri.com/api/v1/setup/status || true)"
            [ "$answered" = 503 ] && break
            sleep 2
          done
          [ "$answered" = 503 ] || { echo "::error::with the api stopped, /api answered $answered, not 503"; exit 1; }
          echo "with the api stopped, /api/v1/setup/status answers 503"
          docker compose start api
          wait_for_api
""",
    ),
]

path = Path(".github/workflows/ci.yml")
text = path.read_text()
for old, new in EDITS:
    if text.count(old) != 1:
        sys.exit(f"ci.yml: not there exactly once: {old[:70]!r}")
    text = text.replace(old, new)
path.write_text(text)
print("rewrote .github/workflows/ci.yml")
PY
```

Expected: `rewrote deploy/ci/mcp_flow.py`, `rewrote .github/workflows/ci.yml`.

- [ ] **Step 4: Las pruebas pasan**

Run (desde `backend/`): `.venv/bin/pytest -q tests/test_ci_mcp_flow.py && .venv/bin/ruff check tests && .venv/bin/ruff check --config pyproject.toml ../deploy/ci/mcp_flow.py`
Expected: `51 passed`, `All checks passed!` dos veces (comprobado sobre una copia de `1bc0e5e` con las Tasks 3 y 7; `mcp_flow.py` estaba limpio antes de esta tarea y lo sigue estando: la CI no lo lee, pero la Task 10 pasa la misma regla a su vecino `agent_flow.py`).

- [ ] **Step 5: Commit**

```bash
git add deploy/ci/acme-pebble.yml deploy/ci/pebble-config.json deploy/ci/mcp_flow.py .github/workflows/ci.yml backend/tests/test_ci_mcp_flow.py
git commit -m "ci(deploy): the certificate on :443 comes from Pebble over ACME, as production's from Let's Encrypt; /api answers 503 while the api is down"
```

- [ ] **Step 6: El job, en rojo y en verde, aquí**

En una rama de ensayo que no se fusiona: `git switch -c t7-rehearsal`, y en ella las ramas de las Tasks 4 y 3, en ese orden (el coordinador da sus nombres; `git merge --no-edit` de cada una). Después, con el guion que corre un job de `ci.yml` paso a paso (no se guarda en el repositorio; `$S` es el scratchpad de la sesión):

```bash
S=/tmp/claude-0/-home-user-Novieri-RMM/21768af2-984e-5cf8-87b2-4dc8a41a4864/scratchpad
cat > "$S/run_ci_job.py" <<'PY'
"""Run one job of .github/workflows/ci.yml here, step by step, as the runner
would: each `run:` under bash -eo pipefail in its working-directory, with
RUNNER_TEMP and GITHUB_ENV, and what a step appends to GITHUB_ENV exported to
the steps after it. `uses:` steps are skipped (the checkout is this tree).
Stops at the first step that fails; `if: always()` steps run anyway.

    python3 run_ci_job.py <repo root> deploy [--from "<step name prefix>"]

Not committed: it is the plan's way of showing a CI step red and green in
this session (plan 2026-11-23, Global Constraints)."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

root, job = Path(sys.argv[1]).resolve(), sys.argv[2]
start = sys.argv[sys.argv.index("--from") + 1] if "--from" in sys.argv else None
spec = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())["jobs"][job]
steps = spec["steps"]
default_wd = spec.get("defaults", {}).get("run", {}).get("working-directory", ".")
temp = Path(tempfile.mkdtemp(prefix="ci-runner-"))
env = {**os.environ, "RUNNER_TEMP": str(temp), "GITHUB_ENV": str(temp / "github_env")}
(temp / "github_env").touch()
failed = False
started = start is None
for step in steps:
    name = step.get("name", step.get("uses", "?"))
    started = started or name.startswith(start)
    if "run" not in step or not started or (failed and step.get("if") != "always()"):
        continue
    for line in (temp / "github_env").read_text().splitlines():
        key, _, value = line.partition("=")
        env[key] = value
    print(f"\n===== {name}", flush=True)
    cwd = root / step.get("working-directory", default_wd)
    done = subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], cwd=cwd, env=env)
    print(f"===== {name}: exit {done.returncode}", flush=True)
    failed = failed or done.returncode != 0
print(f"\nRUNNER_TEMP={temp}")
sys.exit(1 if failed else 0)
PY
```

**Rojo:** lo que en producción es un registro DNS que apunta a otra máquina. El validador de Pebble resuelve `rmm.novieri.com` a la dirección del propio Pebble, donde nadie responde al reto; Traefik no obtiene certificado y sirve el suyo, y la comprobación tiene que negarse:

```bash
cd deploy && rm -f .env && docker compose down -v 2>/dev/null; cd ..
sed -i 's|"-defaultIPv4", "172.19.240.250"|"-defaultIPv4", "172.19.240.252"|' deploy/ci/acme-pebble.yml
backend/.venv/bin/python "$S/run_ci_job.py" . deploy 2>&1 | tee "$S/t7-red.log" | grep -E '^=====|REFUSE|::error::' ; git checkout -- deploy/ci/acme-pebble.yml
```

Expected: los pasos hasta `IT_SUITE_VERSION selects the image reference` salen `exit 0`; el último sale `exit 1` con `REFUSE  Traefik is still serving its own default certificate: Let's Encrypt has not issued one for rmm.novieri.com` tras 180 s (y en `docker compose logs traefik`, `Unable to obtain ACME certificate for domains` con el error de validación de Pebble); `teardown` corre igual (`exit 0`).

**Verde:**

```bash
cd deploy && rm -f .env; cd ..
backend/.venv/bin/python "$S/run_ci_job.py" . deploy 2>&1 | tee "$S/t7-green.log" | grep -E '^=====|^ok  |REFUSE|::error::|answers 503|mcp_flow: phase'
```

Expected: cada paso `exit 0`; `ok      127.0.0.1:443 presents a trusted certificate for rmm.novieri.com` con `issuer=CN = Pebble Intermediate CA …` y `DNS:rmm.novieri.com`; `mcp_flow: phase closed: every assertion held` y `mcp_flow: phase open: every assertion held`; `with the api stopped, /api/v1/setup/status answers 503`. El informe de la tarea copia las líneas `=====` y las de `ok` de los dos registros.

Si un paso falla por algo que este plan no previó, se corrige el paso (no la comprobación) en la rama de la tarea, con su commit, y el informe dice qué fue. Al terminar: `cd deploy && docker compose down -v; cd .. && git switch - && git branch -D t7-rehearsal`.


---

### Task 8: El agente en sus dos lados (oleada 2)

**Files:**
- Rename (`git mv`): `agent/packaging/darwin/com.toeshee.itsagent.plist` → `com.novieri.rmm.agent.plist`; `agent/packaging/linux/its-agent-enroll` → `novieri-agent-enroll`; `agent/packaging/linux/its-agent.service` → `novieri-agent.service`; `agent/packaging/windows/its-agent-enroll.ps1` → `novieri-agent-enroll.ps1`; `backend/app/modules/agent_distribution/installers/darwin/com.toeshee.itsagent.plist` → `com.novieri.rmm.agent.plist`; `backend/app/modules/agent_distribution/installers/linux/its-agent.service` → `novieri-agent.service`
- Modify (por reglas, un guion): 102 archivos de `agent/` (58 de código y empaquetado, 44 pruebas; `testdata/` excluido; la lista la imprime el guion y el Apéndice D la da por archivo); `backend/app/core/i18n/es.py`, `backend/app/core/storage.py`, `backend/app/modules/agent_distribution/{link_page,managed,msi,service}.py` y sus cinco copias de instaladores, `backend/app/services/msi_build.py`; `.github/workflows/{ci.yml,release-agent.yml,agent-update-e2e.yml}`; `scripts/build_agent.sh`; `deploy/WINDOWS-PUSH.md`; `docs/kernel/wire-protocol/README.md` (una frase), `docs/modules/agent-distribution/{first-run-verification,spec,spec-self-update,spec-push-install,spec-managed-distribution,authenticode-signing}.md`, `docs/modules/agent/{README,open-questions}.md`; y 14 pruebas del backend: `test_agent_managed_deployment.py`, `test_agent_push_dispatch.py`, `test_agent_release_api.py`, `test_deploy_runbook_secrets.py`, `test_i18n_modules_ops.py`, `test_install_command_shape.py`, `test_install_link_docs.py`, `test_install_link_msi.py`, `test_install_link_page.py`, `test_installer_scripts.py`, `test_msi_build.py`, `test_objectstore_agent_release.py`, `test_storage_presign_read.py`, `test_storage_public_endpoint.py`
- Modify (a mano, en el mismo guion de la Step 3b): `agent/cmd/release-sign/main.go:113`, `agent/internal/install/msi_test.go:93`, `agent/packaging/linux/debian/control.in:4,7`, `backend/app/services/msi_build.py:68,73-76,347`, `backend/tests/test_msi_build.py:98,200-204` (las dos listas de la tabla `File`, en el orden en que `sorted` las da), y los reajustes de línea en `es.py`, `link_page.py` y `msi.py`
- Modify (por reglas propias, en el mismo guion de la Step 3a): los nombres en minúsculas que ve un cliente (resolución del coordinador del 2026-11-24): `agent/internal/capabilities/backup/{snapshot.go:29,34,snapshot_auto.go:32,restore.go:534}` y sus pruebas, el prefijo `itms:` de `backend/app/modules/agent_distribution/link_page.py:442,451,455,467,486`, y `docs/modules/backup/{agent-engine.md:109-113,spec-server-backup.md:348-349}`
- Create: `backend/tests/test_agent_names_both_sides.py`

**Interfaces:**
- Consumes: el árbol con la oleada 1 (la Task 1 ya cambió las entradas de `es.py` que no son de la distribución del agente; esta cambia las que quedan).
- Produces (los nombres de §1.1 del lado del agente, que las Tasks 9, 10 y 12 usan): el binario y el servicio `novieri-agent`; `novieri-agent-enroll`; el artefacto `novieri-agent-linux-amd64` (y los otros cinco); `/var/lib/novieri-agent` como directorio de datos por defecto en Linux y macOS, `%ProgramData%\NovieriRMM\agent` en Windows; `/var/lib/novieri-agent-snap` como raíz de los montajes de instantáneas (hermana del directorio de datos, no dentro); las instantáneas `novieri-snap-<tag>` (LVM y ZFS) y `.novieri-snap-<tag>` (btrfs); el temporal de una restauración `.novieri-restore-*`; el prefijo `novieri-agent:` de los mensajes del comando de instalación; `FilePrefix = "NovieriRMM-Agent-Setup_"`; `msi_build.UPGRADE_NAMESPACE = uuid.UUID("8814cc17-6469-4df7-93f9-0f75ecdbb6a9")`.

**Decisiones de la tarea.**
- **Todo lo que cuelga del nombre del servicio cambia con él** (P4-B-05): `/var/lib/its-agent`, `/etc/its-agent`, `/var/log/its-agent`, `/usr/share/doc/its-agent`, `.its-agent.new`, `its-agent-install` (el directorio temporal de Windows) pasan a `novieri-agent`. El diseño nombra «binario y servicio … y sus rutas»; dejar los directorios con el nombre viejo sería «lo de los equipos» sin cambiar.
- **Las instantáneas** (`toeshee-snap-`, `capabilities/backup/snapshot.go:25`) pasan a `novieri-snap-`: son nombres que un administrador ve en `lvs` y en el disco del cliente, y no hay instantáneas viejas que limpiar (B1). **Las de ZFS y btrfs también** (resolución del coordinador del 2026-11-24; el borrador las dejaba): `snapNamePrefix = "itms-"` (`snapshot_auto.go:32`, `tank/data@itms-<tag>` en `zfs list -t snapshot`) pasa a `"novieri-snap-"`, y `btrfsSnapDir = ".itms-snap-"` (`snapshot.go:34`, un subvolumen en el disco del cliente) a `".novieri-snap-"`. El de ZFS no queda en `novieri-`: el barrido de huérfanos borra todo lo que empiece por el prefijo (`snapshot_auto.go:390`), y `novieri-snap-` es más estrecho que cualquier nombre que un administrador use para sus propias instantáneas; es el mismo que el de LVM. Igual el temporal que una restauración deja junto al archivo que restaura, `.itms-restore-*` (`restore.go:534`) → `.novieri-restore-*`. Los documentos que los nombran (`docs/modules/backup/agent-engine.md:109-113`, `spec-server-backup.md:348-349`) cambian con ellos, con esas reglas y ninguna otra.
- **La raíz de los montajes, `/var/lib/toeshee-agent/snap` (`snapshot.go:29`), pasa a `/var/lib/novieri-agent-snap`, no a `/var/lib/novieri-agent/snap`.** Hoy es hermana del directorio de datos (`/var/lib/its-agent`); la regla de texto del borrador la metía **dentro** del nuevo (`/var/lib/novieri-agent`), un cambio de conducta: la desinstalación hace `os.RemoveAll` del directorio de datos (`uninstall/helper.go:95`) y bajaría a un montaje de solo lectura que un fallo hubiera dejado (EROFS), y `ReadWritePaths=/var/lib/novieri-agent` de la unidad pasaría a cubrir la raíz de los montajes, que hoy no cubre. Hermana, como hoy.
- **El prefijo de los mensajes del comando de instalación**, `itms:` (`link_page.py:442-487`; «itms: installed. This computer appears in …»), pasa a `novieri-agent:`, el nombre del programa que instala, como dicen los mensajes de una orden de shell. Una prueba nueva lo fija sobre el guion que devuelve `link_page.install_script`.
- **Las clases de ventana de Windows se quedan** (`ITMSRemoteConsent`, `ITMSRemoteIndicator`, `ITMSRemoteBorder`, `remote/helper/ui_windows.go:117-119`; revisión previa, M-4): son nombres de `RegisterClassEx` que nadie ve (lo que se ve son los títulos de las ventanas, que sí cambian), y el diseño deja lo interno (§1: «Un identificador interno no lo ve nadie, y cambiarlo solo añade riesgo»; §8). La guarda no necesita excepción: `\bITMS\b` no casa dentro de `ITMSRemote…` (Task 11).
- **Lo que no cambia** (§1.2): `its-agent-release/v1` (la regla lo excluye: es la preimagen de la firma de la versión, `update/releasesig.go:50`, `msi.py:120`); los `ITS_AGENT_*`; `X-ITS-Agent-*`; `ITMSRSP1`; el módulo Go; los `ITMS_E2E_*` de las pruebas del agente; los `Id` internos del WiX (`its_agent_exe`, `ItsMsiInstall`, `ItsMsiUninstall`), que no ve nadie y que `EXPECTED_CUSTOM_ACTIONS` fija.
- **Un defecto que el cambio de nombre destapa, arreglado aquí:** `verify_msi` compara la tabla `File` ordenada con `[AGENT_FILE, SETTINGS_FILE]` sin ordenar (`msi_build.py:346-347`). Funcionaba porque `its-agent.exe` va antes que `msi-enroll.json`; `novieri-agent.exe` va después, y **todo MSI construido se rechazaría como `wrong_files`**. Lo encontró `test_i18n_modules_ops.py::test_every_builder_error_has_a_spanish_sentence_and_an_english_one_that_is_unchanged` en la copia de prueba (le faltaban `no_service_stop` y `wrong_template`, los dos errores que van después). Pasa a `sorted([AGENT_FILE, SETTINGS_FILE])`. **Y el mismo error está en las pruebas**: `test_msi_build.py:98` y `:200-204` comparan la tabla ordenada con una lista en el orden viejo, que la regla deja `["novieri-agent.exe", "msi-enroll.json", …]`. Sin `msitools` esas pruebas se saltan y el fallo no se ve aquí; la CI los instala (`ci.yml:130-138`, «A missing msitools is a failure here») y se pondría en rojo (revisión previa, R1: `At index 0 diff: 'msi-enroll.json' != 'novieri-agent.exe'`). El Step 3b las pone en el orden de `sorted`, y el Step 4 corre con `wixl` y `msitools` instalados.
- **La clave de los artefactos en el *bucket* cambia** (revisión previa, R6): `core/storage.py:169` la deriva del nombre (`<prefijo>/<versión>/novieri-agent-<os>-<arch>`), `service.py:97` la guarda en `agent_releases.storage_ref`, y `service.py:144` y `api.py:223` la vuelven a derivar («Re-derive rather than trusting the stored ref»). En una base con versiones subidas antes de esta tarea, esas versiones se firmarían hacia un objeto que no existe (404 a cada agente y comando de instalación) y su borrado dejaría el objeto. En el host nuevo no hay ninguna (B1, P7), así que no hace falta migración; pero esta compilación no se apunta a la base de `itms.toeshee.com` (y «Lo que el despliegue tiene que saber», punto 12).
- **La regla del host no entra en dos pruebas de la oleada 1** (revisión previa, R5): `test_deploy_acme.py` (Task 3) y `test_deploy_script_steps.py` (Task 4) nombran `itms.toeshee.com` **para negarlo** (un nombre que no es el de la consola no llega a ningún router; `deploy.sh` ya no lleva el host de un despliegue). Con la regla, la primera pediría que `rmm.novieri.com` —el `PUBLIC_HOST` del modelo— no llegara a ningún router (roja), y la segunda dejaría de vigilar el host viejo (verde y vacía). El guion las excluye de esa regla (`NEGATIVE_HOST`), y el Step 4 las corre.
- **El `UpgradeCode` nuevo** (diseño §1.1): `UPGRADE_NAMESPACE` nuevo, `8814cc17-6469-4df7-93f9-0f75ecdbb6a9` (generado con `uuid.uuid4()` el 2026-11-23 para este plan); el `UpgradeCode` sigue siendo `uuid5(UPGRADE_NAMESPACE, org_id)`, uno por organización. El comentario dice el valor anterior y por qué cambió una vez.
- **`control.in`:** `Maintainer: Novieri <it@novieri.example>` (un dominio `.example`, como el de antes: no se inventa un buzón real) y **sin `Homepage:`**: el repositorio es privado y no hay una página pública que nombrar (campo opcional en Debian; `packaging_test.go` no lo pide, comprobado). **Lo confirmó el operador el 2026-11-24** (`.git/sdd/decisiones-p34.md`: no hay buzón real).
- **`docs/kernel/wire-protocol/README.md:525`** cambia el nombre del binario en la orden que el agente ejecuta en un instalador remoto; ningún esquema ni `testdata` cambia (Step 4 lo comprueba). Esa orden la construye el agente con constantes compiladas (2a(b)): no viaja en ningún campo firmado.
- **La regla de la palabra `ITMS` no se aplica a las pruebas del backend en general** (allí «ITMS» es a menudo el valor de un *fixture*: el nombre visible de un relé SMTP, un host en mayúsculas), solo a las cinco que leen los textos que esta tarea cambia; y nunca dentro de un nombre de host (`ITMS.Example.com`).

- [ ] **Step 0: La worktree, con la oleada 1**

Run, desde la raíz de la worktree (creada de `origin/claude/compassionate-bardeen-vgtwus`, nunca de la `main` local: Global Constraints, «Pistas en paralelo»), después de fusionar la rama del coordinador con la oleada 1 (`git merge --no-edit` de la rama que él nombra):

```bash
git log --oneline -1
git merge-base --is-ancestor 6a14dbc HEAD && echo HAS-PR-13
grep -c Toeshee backend/app/core/i18n/es.py
ls backend/tests/test_deploy_acme.py && grep -c 'itms.toeshee.com' backend/tests/test_deploy_acme.py backend/tests/test_deploy_script_steps.py
```

Expected: la primera línea es la fusión con la rama del coordinador (o su cabeza, si la fusión fue rápida), nunca `dd062f8 Merge pull request #12 …`; después `HAS-PR-13` y `0` (la Task 1 está dentro), `backend/tests/test_deploy_acme.py`, y `backend/tests/test_deploy_acme.py:1` y `backend/tests/test_deploy_script_steps.py:1` (las dos pruebas de las Tasks 3 y 4 que nombran el host viejo para negarlo, y que el Step 3a deja como están). Si no, se para y se avisa al coordinador. Después, la preparación de Global Constraints, y para las pruebas del MSI, `wixl` y `msitools`: `apt-get install -y wixl msitools` (la CI los instala, `ci.yml:130-138`, y sin ellos 17 pruebas se saltan en lugar de correr).

- [ ] **Step 1: La prueba de los dos lados**

Crear `backend/tests/test_agent_names_both_sides.py`:

```python
"""The agent's names, the same on both sides (design 2026-11-23-vps-y-marca-
novieri §1.1: "the installer prefix and the service change at the same time on
both sides").

The control plane NAMES what the agent RECOGNISES: the file name the install
link hands a Windows machine is parsed by the agent (`install/mode.go`), the
MSI stops the service the agent registers, the release artefacts the console
stores are the files `release-sign` signs and the release workflow uploads,
and the install command carries byte copies of the packaging. A name changed
on one side only breaks an install with no error on either side's own tests.
These read the Go source and the workflow beside the Python they pair with.
And the names a client's computer shows in lower case (decided 2026-11-24):
the install command's messages, the snapshots, a restore's temporary file.
"""

import re
import uuid
from pathlib import Path

from app.core.storage import agent_release_suffix
from app.modules.agent_distribution import link_page, msi
from app.services import msi_build

REPO = Path(__file__).resolve().parents[2]
AGENT = REPO / "agent"


def _go_const(rel: str, name: str) -> str:
    source = (AGENT / rel).read_text()
    match = re.search(rf'^\s*{name}\s*=\s*"([^"]*)"', source, re.MULTILINE)
    assert match is not None, f"{rel} has no {name}"
    return match.group(1)


def test_the_install_link_names_its_file_as_the_agent_parses_it() -> None:
    prefix = _go_const("internal/install/mode.go", "const FilePrefix")
    assert prefix == "NovieriRMM-Agent-Setup_"
    name = link_page.windows_file_name("rmm.novieri.com", "c" * 32)
    assert name == f"{prefix}rmm.novieri.com_{'c' * 32}.exe"
    assert (
        msi.msi_file_name("rmm.novieri.com", "amd64")
        == "NovieriRMM-Agent-rmm.novieri.com-amd64.msi"
    )


def test_the_service_has_one_name_in_the_agent_and_in_the_msi() -> None:
    service = _go_const("internal/service/service.go", "const Name")
    assert service == "novieri-agent"
    assert _go_const("internal/install/layout/layout.go", "serviceName") == service
    assert _go_const("internal/update/svcparse.go", "windowsService") == service
    assert _go_const("internal/update/svcparse.go", "systemdUnit") == service
    assert _go_const("internal/update/svcparse.go", "helperUnit") == "novieri-agent-update"
    assert _go_const("internal/capabilities/uninstall/system_linux.go", "const helperUnit") == (
        "novieri-agent-uninstall"
    )
    wxs = msi_build.render_wxs(upgrade=uuid.UUID(int=1), version="1.7.0")
    assert f'<ServiceControl Id="StopAgent" Name="{service}"' in wxs
    assert msi_build.AGENT_FILE == f"{service}.exe"
    assert msi_build.PRODUCT_NAME == "Novieri RMM Agent"
    assert msi_build.MANUFACTURER == "Novieri"
    assert _go_const("internal/service/service.go", "const DisplayName") == "Novieri RMM Agent"


def test_the_msi_is_a_new_product() -> None:
    """A new UpgradeCode namespace: the MSI is "Novieri RMM Agent", and no
    "ITMS Agent" MSI is assigned anywhere to upgrade (operator decision B1)."""
    assert msi_build.UPGRADE_NAMESPACE == uuid.UUID("8814cc17-6469-4df7-93f9-0f75ecdbb6a9")


def test_the_launchd_label_is_the_plists_file_name_everywhere() -> None:
    label = _go_const("internal/update/svcparse.go", "launchdLabel")
    assert label == "com.novieri.rmm.agent"
    assert (AGENT / "packaging/darwin" / f"{label}.plist").is_file()
    copies = REPO / "backend/app/modules/agent_distribution/installers"
    assert (copies / "darwin" / f"{label}.plist").is_file()
    assert (copies / "linux" / "novieri-agent.service").is_file()


def test_a_release_artefact_has_one_name_from_the_build_to_the_bucket() -> None:
    assert agent_release_suffix("1.7.0", "linux", "amd64").endswith("/novieri-agent-linux-amd64")
    sign = (AGENT / "cmd/release-sign/main.go").read_text()
    assert 'parts[0] != "novieri" || parts[1] != "agent"' in sign
    build = (AGENT / "packaging/build.sh").read_text()
    assert 'out="$dist/novieri-agent-${goos}-${goarch}"' in build
    release = (REPO / ".github/workflows/release-agent.yml").read_text()
    assert 'files+=("dist/novieri-agent-$t")' in release


def test_the_paths_on_an_endpoint_are_novieris() -> None:
    assert _go_const("internal/install/layout/push.go", "UnixPushStagingDir") == "/opt/novieri"
    config = (AGENT / "internal/config/config.go").read_text()
    assert 'filepath.Join(base, "NovieriRMM", "agent")' in config
    assert 'filepath.Join("/var", "lib", "novieri-agent")' in config
    backup = AGENT / "internal/capabilities/backup"
    snapshot = (backup / "snapshot.go").read_text()
    assert 'snapTagPrefix = "novieri-snap-"' in snapshot
    # A sibling of the data directory, as before: never inside it, where the
    # uninstaller's RemoveAll would walk into a stale read-only mount.
    assert 'snapMountRoot = "/var/lib/novieri-agent-snap"' in snapshot
    assert 'btrfsSnapDir = ".novieri-snap-"' in snapshot
    assert 'snapNamePrefix = "novieri-snap-"' in (backup / "snapshot_auto.go").read_text()
    assert '".novieri-restore-*"' in (backup / "restore.go").read_text()
    control = (AGENT / "packaging/linux/debian/control.in").read_text()
    assert control.startswith("Package: novieri-agent\n")
    assert "Maintainer: Novieri <it@novieri.example>" in control
    assert "Homepage:" not in control
    assert "Description: Novieri RMM endpoint agent" in control
    # The uninstaller removes the package by the name it was built with.
    linux = (AGENT / "internal/capabilities/uninstall/system_linux.go").read_text()
    assert '"dpkg", "--purge", "novieri-agent"' in linux
    assert '"/var/lib/dpkg/info/novieri-agent.list"' in linux


def test_the_install_command_speaks_as_the_agent() -> None:
    """What a person reads in the terminal of the computer being added."""
    script = link_page.install_script(
        console="https://rmm.novieri.com", code="c" * 32, releases={}, presign=lambda _r: ""
    )
    assert 'echo "novieri-agent: run this with sudo" >&2' in script
    assert "novieri-agent: installed. This computer appears in Novieri RMM" in script
    assert "itms" not in script and "ITMS" not in script
```

- [ ] **Step 2: Falla**

Run: `cd backend && .venv/bin/pytest -q tests/test_agent_names_both_sides.py`
Expected: `7 failed` (comprobado el 2026-11-24 sobre `1bc0e5e` con la oleada 1): `'ITMS-Agent-Setup_' == 'NovieriRMM-Agent-Setup_'`, `'its-agent' == 'novieri-agent'`, el `UPGRADE_NAMESPACE` viejo, `com.toeshee.itsagent`, `its-agent-linux-amd64`, `/opt/toeshee`, y el comando de instalación que dice `itms: run this with sudo`.

- [ ] **Step 3a: Los nombres, por reglas**

Desde la raíz de la worktree (el guion no se guarda):

```bash
python3 - <<'PY'
"""Task 8: the agent's names on the endpoint, both sides at once
(plan 2026-11-23, Task 8). Run from the repository root."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()

#: Renamed with `git mv` first; the text rules below then find nothing
#: left under the old names.
MOVES = [
    ("agent/packaging/darwin/com.toeshee.itsagent.plist", "agent/packaging/darwin/com.novieri.rmm.agent.plist"),
    ("agent/packaging/linux/its-agent-enroll", "agent/packaging/linux/novieri-agent-enroll"),
    ("agent/packaging/linux/its-agent.service", "agent/packaging/linux/novieri-agent.service"),
    ("agent/packaging/windows/its-agent-enroll.ps1", "agent/packaging/windows/novieri-agent-enroll.ps1"),
    (
        "backend/app/modules/agent_distribution/installers/darwin/com.toeshee.itsagent.plist",
        "backend/app/modules/agent_distribution/installers/darwin/com.novieri.rmm.agent.plist",
    ),
    (
        "backend/app/modules/agent_distribution/installers/linux/its-agent.service",
        "backend/app/modules/agent_distribution/installers/linux/novieri-agent.service",
    ),
]

#: Ordered: the longer names first, so a shorter rule never splits one.
RULES = [
    (r"com\.toeshee\.itsagent", "com.novieri.rmm.agent"),
    (r"(/var)?/opt/toeshee", r"\1/opt/novieri"),
    # A SIBLING of the data directory (/var/lib/novieri-agent), as today:
    # never inside it, where the uninstaller's RemoveAll would walk into a
    # stale read-only snapshot mount.
    (r"/var/lib/toeshee-agent/snap", "/var/lib/novieri-agent-snap"),
    (r"toeshee-snap-", "novieri-snap-"),
    (r"ITMS-Agent-", "NovieriRMM-Agent-"),
    # ITMSRemote* (Windows window classes) is NOT renamed: internal, never
    # shown (design §1, §8; pre-flight review M-4).
    (r"Toeshee endpoint agent", "Novieri RMM endpoint agent"),
    (r"ITMS endpoint agent", "Novieri RMM endpoint agent"),
    (r"Toeshee agent", "Novieri RMM agent"),
    (r"ITSuite", "NovieriRMM"),
    # The release signature's preimage is a cryptographic domain string
    # (design §1.2): it is not a name and it does not change.
    (r"its-agent(?!-release/v1)", "novieri-agent"),
    (r"itms\.toeshee\.com", "rmm.novieri.com"),
    # The word, never X-ITMS-* (a header), ITMS_* (an environment variable)
    # or ITMSRSP1 (the remote-screen magic): those are the protocol (§1.2).
]
#: The word, never X-ITMS-* (a header), ITMS_* (an environment variable)
#: or ITMSRSP1 (the remote-screen magic): those are the protocol (§1.2).
#: Not in backend/tests at large, where "ITMS" is a fixture's value
#: (a relay's display name, a host written in capitals) and not a name the
#: product shows; only in the tests of the texts this task renames.
WORD = (r"(?<![-\w./])ITMS(?![-\w]|\.\w)", "Novieri RMM")
WORD_TESTS = {
    "backend/tests/test_install_link_page.py",
    "backend/tests/test_install_link_msi.py",
    "backend/tests/test_msi_build.py",
    "backend/tests/test_agent_managed_deployment.py",
    "backend/tests/test_i18n_modules_ops.py",
}
#: Only in the control plane's own files: example hosts in the messages an
#: administrator reads, and the prefix of the install command's messages
#: (`echo "itms: installed. ..."`, link_page.py), which a client reads.
BACKEND_ONLY = [
    (r"itms\.example\.com", "rmm.example.com"),
    (r'"itms: ', '"novieri-agent: '),
]
#: The lower-case names a client's disk shows (decided 2026-11-24): ZFS and
#: btrfs snapshots and a restore's temporary file, in the backup capability,
#: its tests and the two documents that describe them -- and nowhere else.
SNAPSHOTS = [
    (r'snapNamePrefix = "itms-"', 'snapNamePrefix = "novieri-snap-"'),
    (r"\.itms-snap-", ".novieri-snap-"),
    (r"@itms-", "@novieri-snap-"),
    (r'"itms-T"', '"novieri-snap-T"'),
    (r"/\.zfs/snapshot/itms-", "/.zfs/snapshot/novieri-snap-"),
    (r"\.itms-restore-", ".novieri-restore-"),
]
SNAPSHOT_FILES = ("agent/internal/capabilities/backup/",)
SNAP_DOCS = (
    "docs/modules/backup/agent-engine.md",
    "docs/modules/backup/spec-server-backup.md",
)
#: Two tests of wave 1 name the OLD host on purpose, to refuse it (Tasks 3
#: and 4): the host rule must not turn them into tests of the new one.
NEGATIVE_HOST = {
    "backend/tests/test_deploy_acme.py",
    "backend/tests/test_deploy_script_steps.py",
}

#: The documents that describe these names on an endpoint, and the tests
#: that read them (`test_install_link_docs.py`). Dated plans under
#: docs/superpowers/ and docs/modules/agent/plan.md are history: untouched.
DOCS = (
    "docs/modules/agent-distribution/first-run-verification.md",
    "docs/modules/agent-distribution/spec.md",
    "docs/modules/agent-distribution/spec-self-update.md",
    "docs/modules/agent-distribution/spec-push-install.md",
    "docs/modules/agent-distribution/spec-managed-distribution.md",
    "docs/modules/agent-distribution/authenticode-signing.md",
    "docs/modules/agent/README.md",
    "docs/modules/agent/open-questions.md",
    "docs/kernel/wire-protocol/README.md",
    "deploy/WINDOWS-PUSH.md",
)

def tracked(*paths: str) -> list[str]:
    out = subprocess.run(["git", "ls-files", "--", *paths], check=True, capture_output=True, text=True)
    return [p for p in out.stdout.splitlines() if "/testdata/" not in p]

def main() -> None:
    for old, new in MOVES:
        subprocess.run(["git", "mv", old, new], check=True)
    files = tracked("agent", ".github/workflows", "scripts/build_agent.sh",
                    "backend/app/modules/agent_distribution", "backend/app/services/msi_build.py",
                    "backend/app/core/storage.py", "backend/app/core/i18n/es.py", "backend/tests",
                    *DOCS, *SNAP_DOCS)
    changed = []
    for rel in files:
        path = ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        new = text
        if rel in SNAP_DOCS:
            rules = list(SNAPSHOTS)
        else:
            rules = list(RULES)
            if rel in NEGATIVE_HOST:
                rules = [r for r in rules if r[0] != r"itms\.toeshee\.com"]
            if not rel.startswith("backend/tests/") or rel in WORD_TESTS:
                rules.append(WORD)
            if rel.startswith("backend/app/"):
                rules += BACKEND_ONLY
            if rel.startswith(SNAPSHOT_FILES):
                rules += SNAPSHOTS
        for pattern, repl in rules:
            new = re.sub(pattern, repl, new)
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed.append(rel)
    print("\n".join(changed))
    print(f"{len(changed)} files changed")

main()
PY
```

Expected: la lista de archivos cambiados y `142 files changed` (en la copia de `1bc0e5e` con la Task 1 encima; si la oleada 1 dejó otro número, se compara la lista con el Apéndice D antes de seguir).

- [ ] **Step 3b: Lo que ninguna regla puede hacer, y las líneas largas**

Desde la raíz de la worktree:

```bash
python3 - <<'PY'
"""Task 8, Step 4: wrap what the longer names pushed past 100 columns, and the
four edits no rule can make. Each old text must be there exactly once."""
import sys
from pathlib import Path

EDITS = [
    ("backend/app/core/i18n/es.py",
     '''        "El MSI necesita que se acceda a Novieri RMM mediante un nombre DNS, como rmm.example.com, y "
        "no una dirección IP. Define ITS_PUBLIC_CONSOLE_URL con ese nombre."''',
     '''        "El MSI necesita que se acceda a Novieri RMM mediante un nombre DNS, como "
        "rmm.example.com, y no una dirección IP. Define ITS_PUBLIC_CONSOLE_URL con ese nombre."'''),
    ("backend/app/core/i18n/es.py",
     '''        "El archivo existe: %ProgramFiles%\\\\NovieriRMM\\\\novieri-agent.exe. No detectes por el estado de "
        "inscripción: un agente instalado pero sin inscribir es un estado intermedio normal en "
        "esta vía."''',
     '''        "El archivo existe: %ProgramFiles%\\\\NovieriRMM\\\\novieri-agent.exe. No detectes por el "
        "estado de inscripción: un agente instalado pero sin inscribir es un estado intermedio "
        "normal en esta vía."'''),
    ("backend/app/core/i18n/es.py",
     '''        "Esto instala el agente en el equipo que estás usando ahora. Aparece en Novieri RMM en menos "
        "de un minuto y espera allí hasta que un técnico lo apruebe."''',
     '''        "Esto instala el agente en el equipo que estás usando ahora. Aparece en Novieri RMM en "
        "menos de un minuto y espera allí hasta que un técnico lo apruebe."'''),
    ("backend/app/core/i18n/es.py",
     '''        "El instalador de Windows necesita que se acceda a Novieri RMM mediante un nombre DNS, como "
        "rmm.example.com, y no una dirección IP: el instalador sabe a dónde unirse por el "
        "nombre del archivo que descarga. Pide a quien administra Novieri RMM que configure uno."''',
     '''        "El instalador de Windows necesita que se acceda a Novieri RMM mediante un nombre "
        "DNS, como rmm.example.com, y no una dirección IP: el instalador sabe a dónde unirse "
        "por el nombre del archivo que descarga. Pide a quien administra Novieri RMM que "
        "configure uno."'''),
    ("backend/app/core/i18n/es.py",
     '''        "Todavía no hay ningún instalador de Windows disponible. Pide a quien administra Novieri RMM "
        "que marque como estable una versión firmada del agente 1.6.0 o posterior."''',
     '''        "Todavía no hay ningún instalador de Windows disponible. Pide a quien administra "
        "Novieri RMM que marque como estable una versión firmada del agente 1.6.0 o posterior."'''),
    ("backend/app/modules/agent_distribution/link_page.py",
     '''        f"    printf %s '{_b64('linux/novieri-agent.service')}' | base64 -d > \\"$d/novieri-agent.service\\"",''',
     '''        f"    printf %s '{_b64('linux/novieri-agent.service')}'"
        ' | base64 -d > "$d/novieri-agent.service"',''' ),
    ("backend/app/modules/agent_distribution/link_page.py",
     '''        '  *) echo "novieri-agent: $os $arch is not a platform the agent is published for" >&2; exit 1 ;;',''',
     '''        '  *) echo "novieri-agent: $os $arch is not a platform the agent is published for"'
        " >&2; exit 1 ;;",'''),
    ("backend/app/modules/agent_distribution/msi.py",
     '''            return tr("the built MSI does not stop the novieri-agent service on install and removal")''',
     '''            return tr(
                "the built MSI does not stop the novieri-agent service on install and removal"
            )'''),
    ("agent/cmd/release-sign/main.go",
     '''parts[0] != "its" || parts[1] != "agent"''',
     '''parts[0] != "novieri" || parts[1] != "agent"'''),
    ("agent/internal/install/msi_test.go",
     r'''`c:\program files\itsuite\ITS-AGENT.EXE`''',
     r'''`c:\program files\novierirmm\NOVIERI-AGENT.EXE`'''),
    ("agent/packaging/linux/debian/control.in",
     "Maintainer: Toeshee <it@toeshee.example>\n",
     "Maintainer: Novieri <it@novieri.example>\n"),
    ("agent/packaging/linux/debian/control.in",
     "Homepage: https://github.com/Toeshee-Lab/it-suite\n",
     ""),
    ("backend/app/services/msi_build.py",
     'MANUFACTURER = "Toeshee"',
     'MANUFACTURER = "Novieri"'),
    # THE ORDER WAS AN ACCIDENT OF THE OLD NAME: "its-agent.exe" sorts before
    # "msi-enroll.json" and "novieri-agent.exe" after it, so the unsorted
    # expectation refused every MSI the renamed builder made (wrong_files).
    ("backend/app/services/msi_build.py",
     "    if files != [AGENT_FILE, SETTINGS_FILE]:",
     "    if files != sorted([AGENT_FILE, SETTINGS_FILE]):"),
    # The tests carried the same accident: sorted() compared with the old
    # order. Without msitools they skip, so only CI (which installs it) sees it.
    ("backend/tests/test_msi_build.py",
     '''    assert sorted(row[2] for row in _table(msi, "File")) == ["novieri-agent.exe", "msi-enroll.json"]''',
     '''    assert sorted(row[2] for row in _table(msi, "File")) == [
        "msi-enroll.json",
        "novieri-agent.exe",
    ]'''),
    ("backend/tests/test_msi_build.py",
     '''        "novieri-agent.exe",
        "msi-enroll.json",
        "run.ps1",
''',
     '''        "msi-enroll.json",
        "novieri-agent.exe",
        "run.ps1",
'''),
    ("backend/app/services/msi_build.py",
     '''#: UpgradeCode is uuid5(UPGRADE_NAMESPACE, str(org_id)): constant for an
#: organisation, so a later MSI upgrades an earlier one instead of installing
#: beside it (spec §2.2 item 3). This value is fixed forever: changing it
#: would make every MSI already assigned in a domain a different product.
UPGRADE_NAMESPACE = uuid.UUID("22ad0fe1-20ac-49a3-938c-25347fa54ecb")''',
     '''#: UpgradeCode is uuid5(UPGRADE_NAMESPACE, str(org_id)): constant for an
#: organisation, so a later MSI upgrades an earlier one instead of installing
#: beside it (spec §2.2 item 3). Fixed from here on: changing it makes every
#: MSI already assigned in a domain a different product. It changed once, on
#: 2026-11-23, when the product became "Novieri RMM Agent" (design
#: 2026-11-23-vps-y-marca-novieri §1.1): a new product, and no "ITMS Agent"
#: MSI is assigned anywhere to upgrade (operator decision B1). The namespace
#: before it was 22ad0fe1-20ac-49a3-938c-25347fa54ecb.
UPGRADE_NAMESPACE = uuid.UUID("8814cc17-6469-4df7-93f9-0f75ecdbb6a9")'''),
]
texts = {}
for rel, old, new in EDITS:
    text = texts.get(rel) or Path(rel).read_text()
    if text.count(old) != 1:
        sys.exit(f"{rel}: not there exactly once: {old[:70]!r}")
    texts[rel] = text.replace(old, new)
for rel, text in texts.items():
    Path(rel).write_text(text)
    print(f"rewrote {rel}")
PY
```

Expected: ocho líneas `rewrote …` (`es.py`, `link_page.py`, `msi.py`, `main.go`, `msi_test.go`, `control.in`, `msi_build.py`, `test_msi_build.py`).

- [ ] **Step 4: Todo pasa en los dos lados, y el protocolo no se movió**

Run, desde `agent/`: `gofmt -l . && go vet ./... && go test ./... -count=1`
Expected: `gofmt` no imprime nada; `go test` da `ok` en los 38 paquetes con pruebas (comprobado en la copia: 38 `ok`, ningún `FAIL`; antes del Step 3b fallaban `cmd/release-sign`, `is not named novieri-agent-<os>-<arch>`, e `internal/install`, la ruta en mayúsculas de `msi_test.go`).

Run, desde `backend/`: `.venv/bin/pytest -q tests/test_agent_names_both_sides.py tests/test_i18n_catalog.py tests/test_agent_managed_deployment.py tests/test_agent_push_dispatch.py tests/test_agent_release_api.py tests/test_agent_release_model.py tests/test_agent_update_api.py tests/test_deploy_runbook_secrets.py tests/test_i18n_modules_ops.py tests/test_install_command_shape.py tests/test_install_link_api.py tests/test_install_link_docs.py tests/test_install_link_msi.py tests/test_install_link_page.py tests/test_installer_scripts.py tests/test_msi_build.py tests/test_objectstore_agent_release.py tests/test_storage_presign_read.py tests/test_storage_public_endpoint.py && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: PASS (en la copia: `310 passed, 17 skipped` sin la prueba nueva, `6 passed` la nueva; los 17 saltos son los de siempre, msitools ausente), `Success`, `All checks passed!`.

El protocolo y los vectores, intactos, desde la raíz:

```bash
git diff --exit-code --stat -- docs/kernel/wire-protocol/'*.schema.json' docs/kernel/wire-protocol/testdata agent/internal/canonical/testdata agent/internal/rsp && echo PROTOCOL-UNCHANGED
git diff -- docs/kernel/wire-protocol/README.md | grep '^[-+] '
```

Expected: `PROTOCOL-UNCHANGED`, y las dos líneas de `README.md:525` (`its-agent install` → `novieri-agent install`).

El paquete Debian, de verdad, desde `agent/`:

```bash
VERSION=0.0.0-test ./packaging/build.sh linux/amd64 >/dev/null
ARCH=amd64 VERSION=0.0.0-test ./packaging/linux/build-deb.sh | grep -E 'novieri|its-agent'
rm -rf dist
```

Expected (salida de la copia, 2026-11-23):

```
-rwxr-xr-x root/root   9482424 … ./usr/bin/novieri-agent
-rw-r--r-- root/root      7295 … ./usr/lib/systemd/system/novieri-agent.service
-rwxr-xr-x root/root      4662 … ./usr/sbin/novieri-agent-enroll
drwxr-xr-x root/root         0 … ./usr/share/doc/novieri-agent/
-rw-r--r-- root/root     57005 … ./usr/share/doc/novieri-agent/README.md
```

y ninguna línea con `its-agent`.

- [ ] **Step 5: Commit**

```bash
git add -A agent backend/app/core/i18n/es.py backend/app/core/storage.py backend/app/modules/agent_distribution backend/app/services/msi_build.py backend/tests .github/workflows scripts/build_agent.sh deploy/WINDOWS-PUSH.md docs/kernel/wire-protocol/README.md docs/modules/agent-distribution docs/modules/agent/README.md docs/modules/agent/open-questions.md
git status --short | grep -v '^[MRAD] ' || true
git commit -m "feat(agent): the agent is novieri-agent on both sides: service, paths, packages, MSI, installer prefix, artefacts"
```

(`git status` no debe mostrar nada sin preparar: el `dist/` del Step 4 se borró.)

---

### Task 9: El runbook para un VPS nuevo

**Files:**
- Create: `backend/tests/test_runbook_first_boot.py`
- Modify: `deploy/README.md` (el estado `:9-24`; `## Prerequisites` y los pasos 1 a 1c `:116-337`; el paso 7 `:765-850`; el punto 2 del paso 8 `:870-875`; el paso 10 `:920-931`; los pasos 11b a 11f `:961-1076`; una sección nueva antes de `## Upgrading an existing deployment` `:2439`; la secuencia de este VPS al principio de esa sección; las líneas de `certs/` `:2529` y `:2720-2721`; los `curl` con `Bearer $TOKEN` de `:1554-1561`, `:2157-2197`, `:2281` y `:2310`; los nombres en todo el archivo)
- Modify: `deploy/GO-LIVE.md` (§1 `:14-21` y su segunda tabla `:39`; §3 `:87-110` y las tres `curl -sk` `:128-138`; §6 `:343-344`; los nombres)
- Modify: `deploy/host-network.md` y `deploy/scripts/issue-gateway-cert.py` (los nombres: el host del ejemplo y del docstring)
- Modify: `docs/07-backup-restore.md` (la fila 10 de la tabla `:46`; el slug del resultado de ejemplo `:301`)
- Modify (prueba): `backend/tests/test_runbook_without_keycloak.py:412` (el suelo de indicaciones `read -r`, de 7 a 6)

**Interfaces:**
- Consumes: de la Task 3, el volumen `traefik-acme` y las reglas `Host()`; de la Task 4, `new-env.sh --host … [--acme-ca-server …]`, lo que imprime `preflight.sh` y `check-public-tls.sh` (sin argumentos: `127.0.0.1:443`, nombre `ITS_PUBLIC_HOST`, espera 180 s); de la Task 8, el binario `novieri-agent` (`agent/cmd/agent`), su registro `novieri-agent starting` y la CN `Novieri RMM internal CA` (Task 1).
- Produces: los encabezados `## Prerequisites: a new VPS (Ubuntu Server 24.04 LTS)` y `## After a reboot or a restart` (ancla `#after-a-reboot-or-a-restart`), y el párrafo que empieza `**On the VPS this runbook builds**` dentro de `## Upgrading an existing deployment`: la Task 12 los ensaya y corrige, la 11 los lee, la 13 los copia en el informe. `test_runbook_first_boot.py` los fija.

**Decisiones de la tarea.**
- **Un solo host, `rmm.novieri.com`, en cada comando** (B1, B2): el runbook es de este despliegue, y un comando que se pega tal cual no lleva nada que sustituir (`CLAUDE.md`, «A copy-pasteable command contains no placeholders»). Lo que solo sabe el operador (el correo de ACME, las partes de OpenBao, sus contraseñas, el token de inscripción) se pide en un *prompt*, nunca se escribe en un comando.
- **Los requisitos son los de un VPS vacío**: una cuenta de operador con sudo (P1), ufw con 22, 443 y 8443 y nada más (P2; el 80 queda cerrado porque el reto TLS-ALPN-01 se contesta en el 443 [E1]; Docker publica sus puertos por fuera de ufw [E10]), las actualizaciones de seguridad (P3 [E12]), la hora (P4 [E13]), Docker CE del repositorio de Docker con sus propios comandos (P5 [E10]; el grupo `docker` «grants root-level privileges» [E11]) y la copia con una clave de despliegue de solo lectura (P6). **ufw no se puede ensayar en esta sesión** y el runbook lo marca ⚠ UNVERIFIED.
- **Los pasos 2 a 6, 3.5, 8 (salvo cómo se lee el código), 9 y 11a no cambian** salvo los nombres: son los que ya se ejecutaron en vivo o los que la Task 12 ensaya tal cual.
- **El paso 8 lee el código del archivo**, `docker compose exec api cat /var/lib/its/secrets/setup-code`, no del registro: el api lo registra una sola vez, al crearlo, y un contenedor recreado antes de leerlo ya no tiene esa línea; el archivo está en el volumen `its-secrets` (`app/core/setup_code.py:58`).
- **Ningún comando del runbook manda `Authorization: Bearer`**: la API de la consola solo admite la sesión desde la fase 1 de la autenticación propia (`app/api/deps.py`, `admit`, «Off the MCP channel only the session admits»). Cada uno se cambia por la consola o por un comando dentro del stack (diseño §3): el paso 10 y el 11d, por `psql` dentro de `postgres`; el 11b, por la consola; el 11e, por el mismo script de verificación de la cadena que `docs/07-backup-restore.md` ya corre tras una restauración; la confirmación del *break-glass* del dominio de correo, por `get_settings()` dentro de `api`; el *fallback* `curl` de la publicación de versiones y las dos llamadas del despliegue gestionado se quitan como comandos (no hay herramienta de línea de órdenes que las sustituya sin código nuevo) y se dicen como lo que son, las llamadas que hace la consola.
- **El agente de prueba del paso 11c** se compila en un contenedor `golang:1.24.7` (el VPS no necesita Go) y corre en `busybox:1.36.1` con `--network host`, la raíz de confianza del host montada y `--user` del operador: llega a `https://rmm.novieri.com` como un equipo de Internet, por el 8443 con mTLS, y sus archivos son del operador. El token se pide en un bloque propio, porque `test_runbook_without_keycloak.py::test_a_prompt_is_the_last_command_of_its_block` exige que la indicación sea lo último del bloque (lo encontró al ensayar esta tarea). Ese mismo archivo cuenta las indicaciones `read -r` del README con un suelo de 7; esta tarea quita una (la del id del dispositivo en 11d, que alimentaba un `curl` con token) y el suelo pasa a 6, con la razón fechada.
- **El paso 12 es la primera copia**, porque hasta entonces el host nuevo no tiene vuelta atrás (`CLAUDE.md`, «A rollback target has to already exist to be one»): `build-images.sh` lista una sola etiqueta, la que está corriendo.
- **«After a reboot or a restart» y la actualización en este VPS empiezan por `docker compose exec openbao bao status`** (`CLAUDE.md`, «Reporting to the operator»), y la prueba lo fija.
- **GO-LIVE §3** pasa de copiar `fullchain.pem` a ACME, con `check-public-tls.sh` como comprobación; sus tres `curl -sk` pierden la `-k`, porque el certificado ya es de confianza pública. El volumen `traefik-acme` **no entra en la copia** (GO-LIVE §6, `docs/07` fila 10): perderlo cuesta un certificado nuevo, no datos.
- **La renovación con las vidas nuevas de Let's Encrypt** (64 días desde 2027-02-10, 45 desde 2028-02-16 [E6]): Traefik renueva cuando quedan menos de 30 días con su `certificatesDuration` por defecto [E1], comparando con la caducidad real del certificado (`getCertificateRenewDurations` y `crt.NotAfter.Before(time.Now().Add(renewPeriod))` en `pkg/provider/acme/provider.go` [E16]); así que GO-LIVE lo dice y no cambia nada.
- **Los nombres** (`t9_names.py`, Step 4) son las mismas reglas que la Task 8 aplicó al agente, más el host, el repositorio, el dominio de los ejemplos de correo y el slug: así el runbook dice lo mismo que el código que describe. `ITMS_BIND_IP`, `ITMS_NETWORK_SUBNET`, `X-ITMS-*`, `__Host-itms_session`, `itsuite` (la base de datos y las imágenes) y `/opt/it-suite` no cambian (§1.2).

- [ ] **Step 0: La worktree**

Run: `git log --oneline -1`; fusionar la rama del coordinador (las oleadas 1 y 2). Comprobar que están dentro:

```bash
ls deploy/scripts/new-env.sh deploy/scripts/check-public-tls.sh agent/packaging/linux/novieri-agent.service
grep -c 'certresolver: le' deploy/docker-compose.yml
test -e deploy/certs/.gitkeep || echo "no deploy/certs"
```

Expected: las tres rutas, `3`, `no deploy/certs`.

Los archivos de trabajo de esta tarea (los trozos del README y los cuatro scripts) van a `/tmp/t9-runbook/`, fuera del repositorio: `mkdir -p /tmp/t9-runbook`.

- [ ] **Step 1: Las pruebas**

Crear `backend/tests/test_runbook_first_boot.py`:

```python
"""The runbook for a new VPS (design 2026-11-23, §3): what the operator
pastes on a host this repository cannot see.

**WHAT THESE PIN.** `CLAUDE.md`, "Reporting to the operator": a
copy-pasteable command contains no placeholders (the `<sha printed>` that
ended up in `.env` on 2026-09-11); every sequence after the first boot starts
with `docker compose exec openbao bao status`, because a sealed OpenBao makes
`up -d` block; and the design's §3: the `:443` certificate comes from ACME and
nothing is copied into `deploy/certs/`, and no runnable block sends
`Authorization: Bearer`, which the console's API has not read since own
authentication phase 1.

**WHAT THEY DO NOT CHECK.** That a command works: the rehearsal does (plan
2026-11-23, Task 12), and so does CI's `deploy` job for the commands it runs.
These read the text an operator reads.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
README = REPO / "deploy" / "README.md"
GO_LIVE = REPO / "deploy" / "GO-LIVE.md"
BACKUP_DOC = REPO / "docs" / "07-backup-restore.md"
RUNBOOKS = (README, GO_LIVE, BACKUP_DOC)

#: What reads as "put your value here" in a block that is pasted as it is.
PLACEHOLDER = re.compile(
    r"<[a-z][a-z0-9 _-]*>"  # <sha printed>, <host>
    r"|your-host|example\.(?:com|org|net)"
    r"|…"
    r"|\bCHANGE[-_]?ME\b|\bXXXX?\b"
)


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def _sh_blocks(text: str) -> list[str]:
    return re.findall(r"```sh\n(.*?)```", text, re.DOTALL)


def _commands(block: str) -> list[str]:
    """A block's commands: continuation lines joined, comments and blank
    lines dropped."""
    found: list[str] = []
    pending = ""
    for raw in block.splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        found.append(pending + line)
        pending = ""
    return found


PREREQUISITES = "## Prerequisites: a new VPS (Ubuntu Server 24.04 LTS)\n"


def _first_boot() -> str:
    return _section(README.read_text(), "## First boot\n")


def _after_reboot() -> str:
    return _section(README.read_text(), "## After a reboot or a restart\n")


def _vps_upgrade_block() -> str:
    text = _section(README.read_text(), "## Upgrading an existing deployment\n")
    marker = "**On the VPS this runbook builds**"
    assert marker in text, "the upgrade section names the VPS sequence"
    return _sh_blocks(text[text.index(marker) :])[0]


def test_the_first_boot_the_reboot_and_the_upgrade_have_no_placeholders() -> None:
    blocks = [
        *_sh_blocks(_section(README.read_text(), PREREQUISITES)),
        *_sh_blocks(_first_boot()),
        *_sh_blocks(_after_reboot()),
        _vps_upgrade_block(),
    ]
    assert len(blocks) >= 20, len(blocks)
    found = [
        command for block in blocks for command in _commands(block) if PLACEHOLDER.search(command)
    ]
    assert found == [], found
    # Non-vacuity: the pattern sees the form that shipped on 2026-09-11.
    assert PLACEHOLDER.search('echo "IT_SUITE_VERSION=<sha printed>" >> .env')


def test_every_sequence_after_first_boot_starts_by_asking_openbao() -> None:
    for name, block in (
        ("after a reboot", _sh_blocks(_after_reboot())[0]),
        ("the VPS upgrade", _vps_upgrade_block()),
    ):
        commands = [c for c in _commands(block) if not c.startswith("cd ")]
        assert commands[0] == "docker compose exec openbao bao status", (name, commands)


def test_after_a_reboot_unseals_starts_and_checks_the_certificate() -> None:
    commands = [c for block in _sh_blocks(_after_reboot()) for c in _commands(block)]
    assert commands.count("docker compose exec openbao bao operator unseal") == 3
    assert "docker compose up -d" in commands
    assert "./scripts/check-public-tls.sh" in commands


def test_the_first_boot_runs_the_scripts_that_check_it() -> None:
    commands = [c for block in _sh_blocks(_first_boot()) for c in _commands(block)]
    for expected in (
        "./scripts/new-env.sh --host rmm.novieri.com",
        "./scripts/build-images.sh",
        "./scripts/preflight.sh",
        "docker compose up -d",
        "./scripts/check-public-tls.sh",
        "sudo ./scripts/install-backup-timer.sh",
    ):
        assert expected in commands, expected
    order = [
        commands.index(c)
        for c in (
            "./scripts/new-env.sh --host rmm.novieri.com",
            "./scripts/preflight.sh",
            "docker compose up -d",
            "./scripts/check-public-tls.sh",
        )
    ]
    assert order == sorted(order), order
    # The tag goes into .env by a command that computes it and deletes any
    # earlier line first (CLAUDE.md: a second IT_SUITE_VERSION= silently wins).
    tag = [c for c in commands if "IT_SUITE_VERSION=" in c]
    assert len(tag) == 1 and "sed -i '/^IT_SUITE_VERSION=/d' .env" in tag[0], tag


def test_the_prerequisites_open_only_ssh_the_console_and_the_gateway() -> None:
    prereq = _section(README.read_text(), PREREQUISITES)
    commands = [c for block in _sh_blocks(prereq) for c in _commands(block)]
    allowed = sorted(c for c in commands if c.startswith("sudo ufw allow"))
    assert allowed == ["sudo ufw allow 22/tcp", "sudo ufw allow 443/tcp", "sudo ufw allow 8443/tcp"]
    assert "sudo ufw --force enable" in commands
    assert any("download.docker.com/linux/ubuntu" in c for c in commands)
    assert not any(re.search(r"apt(-get)? install\b.*\bdocker\.io\b", c) for c in commands)
    assert any("unattended-upgrades" in c for c in commands)
    assert any(c.startswith("timedatectl status") for c in commands)


def test_no_runnable_block_sends_a_bearer_token_or_skips_verification() -> None:
    found = []
    for path in RUNBOOKS:
        for block in _sh_blocks(path.read_text()):
            for command in _commands(block):
                if "Authorization: Bearer" in command or re.search(r"\bcurl\b.*\s-s?k\b", command):
                    found.append(f"{path.name}: {command[:90]}")
    assert found == [], found


def test_nothing_copies_a_certificate_into_deploy_certs() -> None:
    found = []
    for path in RUNBOOKS:
        for block in _sh_blocks(path.read_text()):
            found += [
                f"{path.name}: {c[:90]}"
                for c in _commands(block)
                if "certs/" in c and "/etc/ssl/certs/" not in c
            ]
    assert found == [], found
    tls = _section(GO_LIVE.read_text(), "## 3. TLS")
    assert "./scripts/check-public-tls.sh" in tls
    assert "traefik-acme" in tls
    assert "/etc/letsencrypt" not in tls
    row = next(line for line in BACKUP_DOC.read_text().splitlines() if line.startswith("| 10 |"))
    assert "traefik-acme" in row and "deploy/certs" not in row, row
```

Y el suelo de `test_runbook_without_keycloak.py`, desde la raíz de la worktree:

```bash
python3 - <<'PY'
"""Task 9, Step 1: the floors the old runbook tests count, where this task
removes what they counted. Run from the repository root."""
import sys
from pathlib import Path

EDITS = {
    "backend/tests/test_runbook_without_keycloak.py": [
        (
            '    prompts = re.findall(r"(?m)^\\s*(?:stty -echo; )?read -r", README.read_text())\n'
            "    assert len(prompts) >= 7, len(prompts)\n",
            '    prompts = re.findall(r"(?m)^\\s*(?:stty -echo; )?read -r", README.read_text())\n'
            "    # Six since 2026-11-23: step 11d's device-id prompt went with the\n"
            "    # bearer-token curl it fed (the job is run from the console now).\n"
            "    assert len(prompts) >= 6, len(prompts)\n",
        ),
    ],
}
for rel, pairs in EDITS.items():
    path = Path(rel)
    text = path.read_text()
    for old, new in pairs:
        if text.count(old) != 1:
            sys.exit(f"{rel}: not there exactly once: {old[:60]!r}")
        text = text.replace(old, new)
    path.write_text(text)
    print(f"rewrote {rel}")
PY
```

Expected: `rewrote backend/tests/test_runbook_without_keycloak.py`.

- [ ] **Step 2: Las pruebas fallan**

Run: `cd backend && .venv/bin/pytest -q tests/test_runbook_first_boot.py`

Expected: `7 failed`. Tres con `ValueError: substring not found` (no existen `## Prerequisites: a new VPS (Ubuntu Server 24.04 LTS)` ni `## After a reboot or a restart`), y cuatro con `AssertionError`: `'./scripts/new-env.sh --host rmm.novieri.com' in ['cp .env.example .env', 'chmod 600 .env', …]`; 17 comandos con `Authorization: Bearer` (`curl -H "Authorization: Bearer $TOKEN" https://itms.toeshee.com/api/v1/devices` el primero); 6 con `certs/` (`cp /etc/letsencrypt/live/itms.toeshee.com/fullchain.pem certs/fullchain.pem` el primero). (Medido el 2026-11-23 sobre la oleada 1.)

- [ ] **Step 3: El README — los trozos nuevos**

Ocho archivos en `/tmp/t9-runbook/`, con el texto exacto que sigue (cada uno es Markdown en inglés, como el resto del runbook).

`/tmp/t9-runbook/A-status.md` — el estado del runbook (sustituye los párrafos de «Every command in the "First boot" section below **was executed**» hasta antes de «The previous version of this runbook had never been run.»):

````markdown
**Rewritten on 2026-11-23 for a new VPS** (`rmm.novieri.com`, design
`docs/superpowers/specs/2026-11-23-vps-y-marca-novieri-design.md`): the
prerequisites, steps 1, 1b, 1c, 7, 10, 11 and 12 of First boot, [After a reboot
or a restart](#after-a-reboot-or-a-restart), and the upgrade on that VPS. The
certificate on `:443` is Let's Encrypt's, which Traefik asks for and renews
(ACME, TLS-ALPN-01); nothing is copied into `deploy/certs/` any more. The
commands that used a bearer token run inside the stack, or in the console.

⚠ **Rehearsed, not yet run on the VPS.** The plan of 2026-11-23 (Task 12)
runs this whole First boot in a container, with Let's Encrypt replaced by
Pebble, its own test CA, and records here what each step printed; until that
task has run, the outputs shown are the ones measured by the tasks that wrote
the scripts. Never run by this repository: a real VPS, the real DNS, Let's
Encrypt itself, ufw, the Entra app and an SMTP relay.

Before 2026-11-23, every command of the First boot of the time was executed,
top to bottom, from empty volumes, against Docker Engine 29.3.1 / Compose
v5.1.1, ending with a device enrolled and a signed `hello` job round-tripped
and the audit chain reporting `{"ok":true}`; step 3.5's two token writes,
step 8's password field, step 9's sign-in and steps 11c and 11d, as own
authentication phase 4 (2026-11-15) left them, were the ones no live host had
run since they changed, and step 1's `ITS_BACKUP_OLD_IDENTITY_DB=1` and step
6b of [Upgrading an existing deployment](#upgrading-an-existing-deployment)
are still not run live.
````

`/tmp/t9-runbook/B-prereq.md` — los requisitos del VPS y los pasos 1, 1b y 1c (sustituye de `## Prerequisites (Ubuntu Server 24.04 / 26.04)` hasta antes de `### Step 2 — Postgres`):

````markdown
## Prerequisites: a new VPS (Ubuntu Server 24.04 LTS)

This runbook builds one deployment: **`rmm.novieri.com`**, on a VPS that starts
empty (operator decisions B1 and B2, 2026-11-23). The deployment model is:
clone this repository onto the host, run the stack from `deploy/`, and
`git pull` to upgrade. Every command below is meant to be pasted as it is; the
values that only you can supply (the Let's Encrypt contact address, the
OpenBao shares, your passwords) are asked for by a prompt, never written into a
command.

### The machine, and its name

- **Ubuntu Server 24.04 LTS**, 4 vCPU, 8 GB of RAM, 100 GB of SSD, and one
  public IPv4 address **on its network interface**.
- **An `A` record `rmm.novieri.com` → that address, created before the first
  boot.** Traefik asks Let's Encrypt for the certificate the moment it starts,
  and a validation that reaches another machine counts against Let's Encrypt's
  limit of 5 failed authorizations per name and hour.
- **No `AAAA` record for `rmm.novieri.com`.** `:443` is published on IPv4
  only, and Let's Encrypt "will always prefer the IPv6 addresses for the
  initial connection", falling back to IPv4 only on a timeout — so an `AAAA`
  record fails the validation with "connection refused". `scripts/preflight.sh`
  refuses either mistake.
- **Inbound 22 (SSH), 443 (console, API, object storage) and 8443 (agents),
  and nothing else.** Both 443 and 8443 must be reachable **from every managed
  endpoint**, not only from operators: `8443` is the agent gateway, and `443`
  carries object storage under presigned grants, which is how backup chunks and
  `app.deploy` artifacts travel. `:80` stays shut: the certificate's challenge
  (TLS-ALPN-01) is answered on `:443`.
- **On a shared or multi-homed host, read `host-network.md` first** (the bind
  address, the reply path, and the fact that ufw does not filter
  Docker-published ports). A dedicated VPS needs none of it.

### P1 — An operator account (as root, once)

If your provider gave you a sudo account instead of `root`, use that account as
the operator and skip this step. Otherwise, logged in as `root`:

```sh
adduser --gecos "" rmmadmin
usermod -aG sudo rmmadmin
install -d -m 700 -o rmmadmin -g rmmadmin /home/rmmadmin/.ssh
install -m 600 -o rmmadmin -g rmmadmin /root/.ssh/authorized_keys /home/rmmadmin/.ssh/authorized_keys
```

`adduser` asks for the new account's password twice; `sudo` will ask for it.
Then log out, log in again as `rmmadmin` with the same key, and check:

```sh
id -nG
```

The list contains `sudo`. Every command from here on runs as this account.

### P2 — The firewall

```sh
sudo ufw allow 22/tcp
sudo ufw allow 443/tcp
sudo ufw allow 8443/tcp
sudo ufw --force enable
sudo ufw status verbose
```

Check that the output says `Status: active`, `Default: deny (incoming)`, and
`ALLOW IN` for `22/tcp`, `443/tcp` and `8443/tcp`. ⚠ **UNVERIFIED** in this
repository's rehearsal (Task 12 of the plan of 2026-11-23 ran in a container,
without ufw). Docker's own documentation warns that ports Docker publishes
bypass ufw ("when you expose container ports using Docker, these ports bypass
your firewall rules"): this stack publishes 443 and 8443 and nothing else, so
the firewall's job here is everything that is not Docker.

### P3 — Security updates

```sh
sudo apt-get update
sudo apt-get install -y unattended-upgrades
printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' | sudo tee /etc/apt/apt.conf.d/20auto-upgrades
systemctl is-active unattended-upgrades
```

The `tee` prints the two lines it wrote, and the last command prints `active`.
Ubuntu installs `unattended-upgrades` by default and these are its default two
lines; writing them makes the state explicit rather than assumed.

### P4 — The clock

```sh
timedatectl status | grep -E 'System clock synchronized|NTP service'
```

```
System clock synchronized: yes
              NTP service: active
```

If either says otherwise: `sudo timedatectl set-ntp true`, and check again.
Certificates, second-factor codes and the audit log's times all depend on it.

### P5 — Docker CE, from Docker's repository

**Do not use Ubuntu's `docker.io` package.** An Engine too old refuses
`traefik:v3.6`'s Docker provider, and Traefik then discovers no containers and
every route answers a bare 404 while everything looks healthy. These are
Docker's own Ubuntu instructions (docs.docker.com/engine/install/ubuntu):

```sh
sudo apt-get install -y ca-certificates curl git openssl iproute2
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and in again, so the new group applies, then check the two versions
that matter:

```sh
docker version --format '{{.Server.Version}}'
docker compose version --short
```

The Engine must be **26 or newer** and Compose **2.24 or newer**
(`scripts/preflight.sh` refuses older ones). Compose 2.24 is needed because
`.env` is both Compose's project env file and the services' `env_file`, and
entries like
`ITS_DATABASE_URL=postgresql+asyncpg://app_user:${APP_DB_PASSWORD}@postgres:5432/itsuite`
rely on Compose interpolating `${APP_DB_PASSWORD}` inside the env file.

The `docker` group "grants root-level privileges to the user" (Docker's
post-installation notes): that is why only the operator account is in it.

### P6 — The checkout

Over SSH with a **deploy key** — a read-only key scoped to this repository:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/novieri_deploy -C "novieri-deploy@$(hostname)"
cat ~/.ssh/novieri_deploy.pub
```

Paste that whole line (`ssh-ed25519 AAAA…`, one line) into the repository's
**Settings → Deploy keys → Add deploy key**, with "Allow write access" left
**unchecked**. A terminal soft-wraps that line, and copying the wrapped display
inserts newlines GitHub rejects as "not in OpenSSH public key format";
`ssh-keygen -l -f ~/.ssh/novieri_deploy.pub` prints a fingerprint if the file
itself is sound. Then:

```sh
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/novieri_deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
ssh -T git@github.com
```

"Hi …! You've successfully authenticated, but GitHub does not provide shell
access." is success. Then:

```sh
sudo mkdir -p /opt/it-suite && sudo chown "$USER" /opt/it-suite
git clone git@github.com:helgarpalmieri55/Novieri-RMM.git /opt/it-suite
cd /opt/it-suite/deploy
git -C .. rev-parse --abbrev-ref HEAD
```

The last line prints `main`: a deployment is always made from `main`. All
commands below are run from `/opt/it-suite/deploy`.

---

## First boot

Start from genuinely empty state. If you are re-running after a failed attempt,
`docker compose down -v` first — a half-initialised Postgres data directory or
OpenBao barrier will hide the step that actually broke — and `rm .env`, because
step 1 refuses to overwrite one. **A first boot after `down -v` asks Let's
Encrypt for a new certificate**: more than 5 in a week for `rmm.novieri.com`
and production refuses, so a rehearsal uses staging (step 1).

### Step 1 — `.env`

```sh
./scripts/new-env.sh --host rmm.novieri.com
```

It asks for one thing: the contact address Traefik gives Let's Encrypt when it
registers its account. Let's Encrypt stopped mailing expiry warnings on
2025-06-04 and no longer keeps the address on the account (it passes it to
ISRG's mailing list), so nothing about renewal depends on it. Then it
writes `.env` from `.env.example`, mode 600: a fresh random value in every
`change-me` except the two OpenBao tokens, `rmm.novieri.com` in every URL, and
the ACME keys. It prints no secret, and it refuses to overwrite an existing
`.env`.

```
wrote .env (mode 600)
  host            rmm.novieri.com
  ITS_ACME_EMAIL=…
  ITS_ACME_CA_SERVER=https://acme-v02.api.letsencrypt.org/directory
  ITS_S3_PUBLIC_ENDPOINT=https://rmm.novieri.com
  ITS_GATEWAY_URL=wss://rmm.novieri.com:8443/agent
  ITS_GATEWAY_RELAY_URL=wss://rmm.novieri.com:8443/relay
  ITS_PUBLIC_CONSOLE_URL=https://rmm.novieri.com
  new secrets     POSTGRES_ADMIN_PASSWORD APP_DB_PASSWORD PORTAL_DB_PASSWORD MINIO_ROOT_PASSWORD
  still change-me ITS_OPENBAO_TOKEN OPENBAO_TOKEN (README step 3.5 fills them)
```

```sh
stat -c %a .env
grep -c '=change-me$' .env
```

```
600
2
```

Exactly two `change-me` — the OpenBao pair, which step 3.5 fills. Real secrets
never enter git; `.env` is gitignored.

**A rehearsal** — a second first boot, a test of this runbook on another
machine — uses Let's Encrypt's staging directory instead, whose certificates no
browser trusts and whose limits are far higher:
`./scripts/new-env.sh --host rmm.novieri.com --acme-ca-server https://acme-staging-v02.api.letsencrypt.org/directory`.

`ITS_PUBLIC_CONSOLE_URL` is the origin every sign-in write is checked against;
it must stay exactly what the browser's address bar shows, `https://rmm.novieri.com`.

**The per-address sign-in limits are optional** (`ITS_AUTH_LOGIN_IP_LIMIT` and
`ITS_AUTH_FACTOR_IP_LIMIT`). Unset, one address may send 30 requests in 10
minutes to the password step, and 30 to the second-factor steps; technicians
behind one office NAT address share one budget. Raise them for that, within
5-1000 (`.env.example` gives the costs); step 7 shows the values the api
started with.

### Step 1b — Tagged images

```sh
./scripts/build-images.sh
v="$(git -C .. rev-parse --short HEAD)" && sed -i '/^IT_SUITE_VERSION=/d' .env && echo "IT_SUITE_VERSION=$v" >> .env && grep '^IT_SUITE_VERSION=' .env
```

`build-images.sh` builds `itsuite/backend` and `itsuite/web` tagged with the
checkout's commit, and ends with `Recent tags:` — on this new host, the one tag
it just built. **That list is the rollback target list, and there is no earlier
entry:** until step 12 has taken the first backup, a new host has no way back
but starting over (see [Rolling back a deployment](#rolling-back-a-deployment)).
The second line writes the tag into `.env`, deleting any earlier line first.

### Step 1c — The pre-flight

```sh
./scripts/preflight.sh
```

It checks `.env` (mode, secrets, tag), that `ITS_PUBLIC_HOST` is the console's
host, that `rmm.novieri.com` resolves to this machine and has no `AAAA`
record, that 443 and 8443 are free, and the Docker versions. A good first boot
ends:

```
  note    ITS_OPENBAO_TOKEN is still change-me -- expected before first boot; §3.2/§3.4 issue it
  note    OPENBAO_TOKEN is still change-me -- expected before first boot; §3.2/§3.4 issue it
  ...
  ok      ITS_PUBLIC_HOST=rmm.novieri.com is the console's host
  ok      ITS_ACME_EMAIL is set
  ok      ACME CA: Let's Encrypt production
  ok      rmm.novieri.com resolves to …, an address of this host
  ok      443 and 8443 are free
  ok      Docker Engine …
  ok      Docker Compose …
  ok      .env is gitignored
```

Any `REFUSE` names its fix; correct it and run the pre-flight again before
going on.

````

`/tmp/t9-runbook/C-step7.md` — el paso 7 (sustituye de `### Step 7 — Your TLS certificate for `:443`, then start everything` hasta antes de `### Step 8`):

````markdown
### Step 7 — Start everything; Traefik obtains the certificate for `:443`

```sh
docker compose up -d
docker compose ps -a --format 'table {{.Service}}\t{{.Status}}'
```

```
SERVICE             STATUS
api                 Up … (healthy)
backup-replicator   Up …
gateway             Up …
migrate             Exited (0) …
minio               Up … (healthy)
openbao             Up … (healthy)
openbao-init        Exited (0) …
postgres            Up … (healthy)
postgres-grants     Exited (0) …
redis               Up …
traefik             Up …
web                 Up … (healthy)
worker              Up …
```

The three `Exited (0)` services are the one-shot jobs; that is success.

**The certificate.** Traefik asks Let's Encrypt for `rmm.novieri.com` as soon as
it sees the routers (TLS-ALPN-01, answered on `:443`), stores it in the
`traefik-acme` volume, and renews it by itself 30 days before it expires. Until
it has one — and for good if it cannot get one — it serves its own self-signed
"TRAEFIK DEFAULT CERT" **without failing**. So check, rather than trust:

```sh
./scripts/check-public-tls.sh
```

```
ok      127.0.0.1:443 presents a trusted certificate for rmm.novieri.com
        issuer=C = US, O = Let's Encrypt, CN = …
        X509v3 Subject Alternative Name:
            DNS:rmm.novieri.com
        notAfter=…
```

It waits up to three minutes for the first issuance. On a rehearsal against
Let's Encrypt staging it refuses by design (staging's root is in no trust
store): give it that root with `--cafile`, from Let's Encrypt's staging page.
A `REFUSE` says what a browser would refuse; `docker compose logs traefik | grep -i acme` says why
Let's Encrypt did not issue (the usual causes: the `A` record is not this
machine's yet, an `AAAA` record, `:443` closed at the provider's firewall). Fix
the cause and `docker compose restart traefik`. ⚠ **UNVERIFIED with Let's
Encrypt itself**: the rehearsal (plan of 2026-11-23, Task 12) and every CI run
obtain it from Pebble, Let's Encrypt's own test CA, by the same path; the
issuer line above is the one production's will be.

**The agent-facing side must be the internal CA**, not Let's Encrypt:

```sh
echo | openssl s_client -connect 127.0.0.1:8443 -servername rmm.novieri.com 2>/dev/null \
  | openssl x509 -noout -issuer -ext subjectAltName
```

```
issuer=CN = Novieri RMM internal CA
X509v3 Subject Alternative Name:
    DNS:rmm.novieri.com
```

Two different issuers on the two ports is the correct picture. If `:8443`
shows a Let's Encrypt issuer, an agent will never connect.

**The per-address sign-in limits the api started with** (step 1):

```sh
docker compose exec api python -c "from app.core.config import get_settings; s = get_settings(); print(s.auth_login_ip_limit, s.auth_factor_ip_limit)"
```

```
30 30
```

````

`/tmp/t9-runbook/D-step8code.md` — cómo se lee el código de instalación en el paso 8 (sustituye el punto 2 de la lista):

````markdown
2. **Read the code.** The api writes it to `/var/lib/its/secrets/setup-code`,
   mode 600, when the first request asks for it (and logs it once):

   ```sh
   docker compose exec api cat /var/lib/its/secrets/setup-code
   ```
````

`/tmp/t9-runbook/E-step10.md` — la comprobación del paso 10 sin token:

````markdown
In the console: **Settings → Users** lists your account with the **org-admin**
role. Or from inside the stack, with no token at all (the console's API reads a
session cookie, not a bearer token, since own authentication phase 1, so a
`curl` with `Authorization: Bearer` is not a way to ask it):

```sh
docker compose exec -T postgres psql -U its_admin -d itsuite -tAc "select u.email, r.name from users u join user_roles ur on ur.user_id = u.id join roles r on r.id = ur.role_id where u.kind = 'staff'"
```

One line: the address you gave the setup screen, then `|org-admin`.
````

`/tmp/t9-runbook/F-step11.md` — los pasos 11b a 11f y el paso 12 (sustituye de `#### 11b` hasta antes de `## Recovery link`):

````markdown
#### 11b — Create an enrollment token

In the console, at `https://rmm.novieri.com/enrollment`: a new token named
`first endpoint`, valid for 24 hours, **one** use, for the site **Principal**
of your own organization's client. The console shows the token **once**; keep
it for the next step and nowhere else. (Since own authentication phase 1 the
console's API takes a session cookie, so the `curl -H "Authorization: Bearer …"`
this step used to show cannot mint one; the console is the way, audited as
`enrollment.token_created`.)

#### 11c — A throwaway agent on this VPS

The first endpoint is this server itself, for a few minutes: the agent built
from this checkout (Go in a container: the VPS needs no toolchain), run in a
container that shares the host's network and trusts the host's certificate
roots, so it reaches `https://rmm.novieri.com` exactly as an endpoint on the
Internet does.

```sh
docker run --rm -v "$PWD/../agent:/src" -w /src -e CGO_ENABLED=0 golang:1.24.7 go build -trimpath -o dist/novieri-agent ./cmd/agent
install -d -m 700 /tmp/smoke-agent
```

The token from 11b, at a prompt that does not echo it. This block is one
command on purpose: a prompt ends its block, so nothing pasted after it can be
read as the token.

```sh
read -rsp 'Enrollment token (11b): ' T && echo && [ -n "$T" ] && (umask 077 && printf '%s' "$T" > /tmp/smoke-agent/enroll-token); unset T
```

Then the agent itself:

```sh
docker run -d --name smoke-agent --network host --user "$(id -u):$(id -g)" \
  -v "$PWD/../agent/dist/novieri-agent:/usr/local/bin/novieri-agent:ro" \
  -v /etc/ssl/certs/ca-certificates.crt:/etc/ssl/certs/ca-certificates.crt:ro \
  -v /tmp/smoke-agent:/var/lib/novieri-agent \
  -e ITS_AGENT_ENROLL_URL=https://rmm.novieri.com -e ITS_AGENT_DATA_DIR=/var/lib/novieri-agent \
  busybox:1.36.1 /usr/local/bin/novieri-agent
for _ in $(seq 1 30); do docker logs smoke-agent 2>&1 | grep -q 'gateway connected' && break; sleep 2; done
docker logs smoke-agent 2>&1 | grep -E 'starting|enrolling|enrolled|gateway connected'
```

```
level=INFO msg="novieri-agent starting" version=dev data_dir=/var/lib/novieri-agent enrolled=false
level=INFO msg=enrolling url=https://rmm.novieri.com/enroll/v1 hostname=… os_family=linux
level=INFO msg=enrolled device_id=… gateway_url=wss://rmm.novieri.com:8443/agent cert_not_after=…
level=INFO msg="gateway connected" url=wss://rmm.novieri.com:8443/agent
```

`enrolled` then `gateway connected` is the whole handshake: the CSR signed by
the internal CA, the certificate and the CA pinned in the data directory, the
outbound mutual-TLS WebSocket up. The token file is gone (`ls /tmp/smoke-agent`
no longer lists `enroll-token`): the agent deletes it the moment it has a
certificate. The device appears in the console's **Devices**, online.

The packaged installers for real endpoints — the `.deb`, the macOS script, the
Windows installer and the Group Policy MSI — are in
[Publishing the agent to the console](#publishing-the-agent-to-the-console).
⚠ **UNVERIFIED**: no Windows or macOS endpoint has been enrolled by anybody on
this project; `docs/modules/agent-distribution/first-run-verification.md` is
the procedure for that.

#### 11d — Round-trip a signed job

In the console, on the device from 11c: **Run job → Collect inventory**. Its
row in **Job history** reaches **Succeeded** within a minute. From inside the
stack, the same fact for the job a person asked for (the platform also
schedules an inventory of its own when a device first connects, so the query
leaves that one out):

```sh
docker compose exec -T postgres psql -U its_admin -d itsuite -tAc "select type, status, signature is not null from jobs where requested_by is not null order by issued_at desc limit 1"
```

```
inventory.collect|succeeded|t
```

That is the platform working: the server signed the job envelope with the
Ed25519 key that never leaves OpenBao, the agent verified the signature before
executing anything (there is no unsigned command path), and the result came
back up the same mTLS connection.

#### 11e — The audit chain

From inside the stack, with no token (the same check `docs/07-backup-restore.md`
runs after a restore):

```sh
docker compose exec -T api python - <<'EOF'
import asyncio
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.audit import audit_service, load_audit_hmac_key
from app.core.config import get_settings
from app.models import Organization


async def main() -> None:
    settings = get_settings()
    audit_service.set_hmac_key(load_audit_hmac_key(settings))
    engine = create_async_engine(settings.database_admin_url)
    try:
        async with AsyncSession(engine) as session:
            orgs = (await session.execute(select(Organization.id, Organization.slug))).all()
            for org_id, slug in orgs:
                r = await audit_service.verify(session, org_id)
                broken = str(r.broken_at) if r.broken_at else None
                print(slug, json.dumps({"ok": r.ok, "count": r.count, "broken_at": broken}))
    finally:
        await engine.dispose()


asyncio.run(main())
EOF
```

```
novieri {"ok": true, "count": …, "broken_at": null}
```

The console shows the same check for the signed-in person's organization:
**Audit log → Verify chain**. `ok: true` over a chain that includes the
enrollment, the dispatch and the result is the end-to-end proof.

#### 11f — Retire the throwaway agent

```sh
docker rm -f smoke-agent
rm -rf /tmp/smoke-agent
```

The second line removes the device's private key and certificate. The device
stays in the console as a record, offline; remove it there if you do not want
it listed.

The MinIO bucket (`ITS_S3_BUCKET`) needs no provisioning step — `ObjectStore`
creates it on first use.

### Step 12 — The first backup, so that there is a way back

```sh
sudo ./scripts/install-backup-timer.sh
sudo systemctl start its-platform-backup.service
journalctl -u its-platform-backup.service --no-pager -n 20
sudo cat /var/lib/it-suite/backup/last-success
```

The last line prints the time of the backup just taken. From now on one runs
every night at 02:30 ([Platform
backups](#platform-backups-nightly-off-host-and-a-monthly-drill)). **Until this
step, a new host has no way back at all**: `build-images.sh` listed one tag
(step 1b), the one running. After it, the way back is restoring this backup
([Rolling back a deployment](#rolling-back-a-deployment)).

**The key material is not in that backup and goes off this host by hand, now**
— the OpenBao shares and root token (step 3.2), the `openbao-data`,
`its-secrets` and `its-ca` volumes, and `.env` — to a different place than the
database backups: GO-LIVE.md §6 and `docs/07-backup-restore.md` §1-2. The
`traefik-acme` volume is not key material: losing it costs one new certificate.

**First boot is complete.**

---

````

`/tmp/t9-runbook/G-reboot.md` — la sección nueva `## After a reboot or a restart`, justo antes de `## Upgrading an existing deployment`:

````markdown
## After a reboot or a restart

OpenBao comes back **sealed** after every host reboot and every `docker compose
up -d` that recreates it, and a sealed OpenBao makes `docker compose up -d`
**block**: `openbao-init` waits on a health check that passes only when it is
unsealed. So this sequence always starts by asking, whatever else happened:

```sh
cd /opt/it-suite/deploy
docker compose exec openbao bao status
```

`Sealed false`: skip to `docker compose up -d` below. `Sealed true`: three of the
five shares, each at the prompt, from three custodians (GO-LIVE.md §4):

```sh
docker compose exec openbao bao operator unseal
docker compose exec openbao bao operator unseal
docker compose exec openbao bao operator unseal
docker compose exec openbao bao status
```

`Sealed false`. Then:

```sh
docker compose up -d
docker compose ps -a --format 'table {{.Service}}\t{{.Status}}'
./scripts/check-public-tls.sh
curl -s -o /dev/null -w '%{http_code}\n' https://rmm.novieri.com/api/v1/devices
```

`up -d` runs `migrate` first, as `api`'s dependency, every time; after a reboot
it finds nothing to migrate and exits 0. The table is step 7's; the certificate
check prints `ok`; and the last line prints `401` (the API answering, no one
signed in). A `503` there means the api is not healthy yet (Traefik answers 503
for the api's paths while it is not): wait, and look at `docker compose logs
api` if it lasts.

If the optional static-key auto-unseal is on (`./scripts/openbao-seal.sh
status`, GO-LIVE.md §4), `bao status` already says `Sealed false` and the three
unseal lines are not needed.

---

````

`/tmp/t9-runbook/H-upgrade.md` — la secuencia de actualización en este VPS, al principio de `## Upgrading an existing deployment`:

````markdown
**On the VPS this runbook builds**, an upgrade is these commands, in this order
(the operator always deploys from `main`):

```sh
cd /opt/it-suite/deploy
docker compose exec openbao bao status
git -C .. pull --ff-only
./scripts/deploy.sh --plan
./scripts/deploy.sh
```

The first one first, always: `Sealed true` means unsealing as in [After a
reboot or a restart](#after-a-reboot-or-a-restart) before anything else, since
`deploy.sh` refuses to start on a sealed OpenBao. `--plan` shows what the
upgrade will run and changes nothing; `deploy.sh` then takes a backup, builds
the images tagged with the new commit, migrates (`migrate` also runs on every
`up -d`), restarts what changed, and verifies: the tag is HEAD, the API answers
`401`, and the certificate on `:443` passes `check-public-tls.sh`. It ends by
printing the way back from this upgrade.

````

Y `/tmp/t9-runbook/readme.py`, que los pone en su sitio:

```python
"""Task 9, Step 3: the runbook for a new VPS. Run from the repository root,
with the parts written to $PARTS (the plan's Step 3 writes them)."""
import os
import sys
from pathlib import Path

PARTS = Path(os.environ["PARTS"])
part = {name: (PARTS / f"{name}.md").read_text() for name in
        ("A-status", "B-prereq", "C-step7", "D-step8code", "E-step10", "F-step11", "G-reboot", "H-upgrade")}

readme = Path("deploy/README.md")
text = readme.read_text()


def between(start: str, end: str, new: str) -> None:
    """Replace from `start` (included) to `end` (excluded); each once."""
    global text
    if text.count(start) != 1 or text.count(end) != 1:
        sys.exit(f"README.md: {start[:50]!r} or {end[:50]!r} is not there exactly once")
    i, j = text.index(start), text.index(end)
    assert i < j, (start, end)
    text = text[:i] + new + text[j:]


def replace(old: str, new: str) -> None:
    global text
    if text.count(old) != 1:
        sys.exit(f"README.md: not there exactly once: {old[:60]!r}")
    text = text.replace(old, new)


between('Every command in the "First boot" section below **was executed**, top to\n',
        "The previous version of this runbook had never been run.", part["A-status"] + "\n")
between("## Prerequisites (Ubuntu Server 24.04 / 26.04)\n", "### Step 2 — Postgres\n", part["B-prereq"])
between("### Step 7 — Your TLS certificate for `:443`, then start everything\n",
        "### Step 8 — Claim the installation\n", part["C-step7"])
replace(
    """2. **Read the code.** The api logs it exactly once, when it is created, and
   writes it to `/var/lib/its/secrets/setup-code` mode 600:

   ```sh
   docker compose logs api | grep -i "install code"
   ```
""",
    part["D-step8code"],
)
between("In the console: **Settings → Users** lists your account with the **org-admin**\n",
        'A `403 {"detail":"requires devices.read"}` means the role is not there.',
        part["E-step10"] + "\n")
replace(
    'A `403 {"detail":"requires devices.read"}` means the role is not there. Seeded\nroles are',
    "No line, or a role other than `org-admin`, means the role is not there. Seeded\nroles are",
)
between("#### 11b — Create an enrollment token\n",
        "## Recovery link: an administrator who lost every second factor\n", part["F-step11"])
replace("\n## Upgrading an existing deployment\n",
        "\n" + part["G-reboot"] + "## Upgrading an existing deployment\n")
replace(
    "`./scripts/deploy.sh --plan` first to see the decision without acting on it.\n\n",
    "`./scripts/deploy.sh --plan` first to see the decision without acting on it.\n\n" + part["H-upgrade"],
)
replace("# 2. Pull. `.env` and `certs/` are gitignored, so neither is touched.\n",
        "# 2. Pull. `.env` is gitignored, so it is not touched.\n")
replace(
    "`its-secrets`, `its-ca`, the OpenBao shares or unseal key file, `.env` and\n"
    "`certs/` are still backed up by hand, **to a different place than the\n",
    "`its-secrets`, `its-ca`, the OpenBao shares or unseal key file and `.env`\n"
    "are still backed up by hand, **to a different place than the\n",
)
readme.write_text(text)
print("rewrote deploy/README.md")
```

Run, desde la raíz de la worktree: `PARTS=/tmp/t9-runbook python3 /tmp/t9-runbook/readme.py`

Expected: `rewrote deploy/README.md`. El script sale con un mensaje y sin escribir si un ancla no está exactamente una vez.

- [ ] **Step 4: Los nombres**

`/tmp/t9-runbook/names.py`:

```python
"""Task 9, Step 4: the names in the operator documents. Run from the
repository root, AFTER Step 3 has replaced Prerequisites and First boot."""
import re
from pathlib import Path

FILES = (
    "deploy/README.md",
    "deploy/GO-LIVE.md",
    "deploy/host-network.md",
    "deploy/scripts/issue-gateway-cert.py",
    "docs/07-backup-restore.md",
)
RULES = [
    (r"git@github\.com:Toeshee-Lab/Toeshee-RMS\.git", "git@github.com:helgarpalmieri55/Novieri-RMM.git"),
    (r"itms\.toeshee\.com", "rmm.novieri.com"),
    (r"itms\.example\.com", "rmm.example.com"),
    # Example addresses and the allowlist's worked example (`you@toeshee.com`,
    # `mail.toeshee.com`, `eviltoeshee.com`): one domain, so the example still
    # reads the same once every copy of it moves together.
    (r"toeshee\.com", "novieri.com"),
    (r"ITS_DEFAULT_ORG_SLUG` from `\.env`\*\* — `toeshee`", "ITS_DEFAULT_ORG_SLUG` from `.env`** — `novieri`"),
    # The default organization's slug in a printed result (ITS_DEFAULT_ORG_SLUG).
    (r'^toeshee (\{"ok")', r"novieri \1"),
    (r"it-suite internal CA", "Novieri RMM internal CA"),
    # The agent's own names, as Task 8 renamed them on the endpoint.
    (r"com\.toeshee\.itsagent", "com.novieri.rmm.agent"),
    (r"(/var)?/opt/toeshee", r"\1/opt/novieri"),
    (r"/var/lib/toeshee-agent", "/var/lib/novieri-agent"),
    (r"toeshee-snap-", "novieri-snap-"),
    (r"ITMS-Agent-", "NovieriRMM-Agent-"),
    (r"ITMSRemote", "NovieriRMMRemote"),
    (r"ITSuite", "NovieriRMM"),
    (r"its-agent(?!-release/v1)", "novieri-agent"),
    (r"^# Deploying it-suite \(on-prem, single node\)$", "# Deploying Novieri RMM (on-prem, single node)"),
    # The word, never ITMS_* (an environment variable) or X-ITMS-* (a header).
    (r"(?<![-\w./])ITMS(?![-\w]|\.\w)", "Novieri RMM"),
]
for rel in FILES:
    path = Path(rel)
    text = path.read_text()
    for pattern, new in RULES:
        text = re.sub(pattern, new, text, flags=re.MULTILINE)
    path.write_text(text)
    print(f"rewrote {rel}")
```

Run: `python3 /tmp/t9-runbook/names.py`

Expected: cinco líneas `rewrote …` (`README.md`, `GO-LIVE.md`, `host-network.md`, `issue-gateway-cert.py`, `07-backup-restore.md`).

- [ ] **Step 5: Los `curl` con token fuera del primer arranque**

`/tmp/t9-runbook/bearer.py`:

```python
"""Task 9, Step 5: the bearer-token curls outside First boot (design §3:
"Los ejemplos curl con Bearer $TOKEN, que no funcionan desde la fase 1, se
sustituyen por comandos que sí funcionan: el de dentro del contenedor api o la
consola"). Run from the repository root, after Step 4."""
import re
import sys
from pathlib import Path

readme = Path("deploy/README.md")
text = readme.read_text()


def replace(old: str, new: str) -> None:
    global text
    if text.count(old) != 1:
        sys.exit(f"README.md: not there exactly once: {old[:70]!r}")
    text = text.replace(old, new)


# 1. The email-domain break-glass: confirm from inside the api, then sign in.
replace(
    """Confirm you are back in before doing anything else:

```sh
curl -s -o /dev/null -w '%{http_code}\\n' -H "Authorization: Bearer $TOKEN" \\
  https://rmm.novieri.com/api/v1/devices
```

`200` is recovered; a further `403` means something other than the allowlist is
refusing you.
""",
    """Confirm the api no longer holds the rule, before doing anything else:

```sh
docker compose exec api python -c "from app.core.config import get_settings; print(repr(get_settings().console_email_domain))"
```

`''` is recovered: sign in at `https://rmm.novieri.com/` and the console opens.
A refusal after that means something other than the allowlist is refusing you.
""",
)

# 2. Publishing a release: the console is the only path since phase 1.
replace(
    """Until 2026-09-23 publishing was only
possible with `curl` and a hand-copied bearer token; that path is kept, short,
as the fallback in step 2.
""",
    """Until 2026-09-23 publishing was only
possible with `curl` and a hand-copied bearer token; since own authentication
phase 1 the API takes the console's session and no bearer token, so the
console is the only path (step 2).
""",
)
replace(
    "release…** button and the `curl` fallback fails with `403 requires\nagent.release.manage`.",
    "release…** button, and the API refuses the publication with `403 requires\nagent.release.manage`.",
)
replace(
    "If the removal itself fails the result says so, and the\n`DELETE` in the fallback below clears it.\n",
    "If the removal itself fails the result says so, and\n**Release channels → Remove…** on the same page clears it — with every other\npackage of that version, so publish those again afterwards.\n",
)
start = "#### Fallback: the same thing with `curl`\n"
end = "It answers 409 while an active update rollout still uses the release.\n"
if text.count(start) != 1 or text.count(end) != 1:
    sys.exit("README.md: the curl fallback is not there exactly once")
i, j = text.index(start), text.index(end) + len(end)
text = text[:i] + """#### No command-line fallback

The `curl` commands this section used to show sent `Authorization: Bearer
$TOKEN`, and since own authentication phase 1 the API reads the console's
session cookie and no bearer token (`backend/app/api/deps.py`, `admit`): they
answered `401`. Publish from the console, from any machine with a browser; the
VPS itself needs none. To remove a version (a wrong upload, or one you need to
clear): **Agent Packages → Release channels → Remove…**, which removes every
package of that version in one request. It is refused while an active update
rollout still uses the version.
""" + text[j:]

# 3. The managed deployment: the console's two calls, described, not run.
replace(
    "`GPO`, `Intune` or `Manual`. Against the API, the same two calls:\n",
    "`GPO`, `Intune` or `Manual`. The dialog makes two calls, described below; they take\n"
    "the console's session, so neither has a `curl` form:\n",
)
replace(
    """```sh
curl -sS -H "Authorization: Bearer $TOKEN" \\
  "https://rmm.novieri.com/api/v1/agent/releases/$REL/deployment-package?channel=gpo"
```
""",
    "`GET /api/v1/agent/releases/{id}/deployment-package?channel=gpo`.\n",
)
replace(
    """```sh
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \\
  -d '{"max_uses":260,"expires_in_hours":72,"channel":"gpo","note":"HQ rollout"}' \\
  "https://rmm.novieri.com/api/v1/agent/releases/$REL/deployment-token"
```
""",
    "`POST /api/v1/agent/releases/{id}/deployment-token`, with for example\n"
    "`{\"max_uses\":260,\"expires_in_hours\":72,\"channel\":\"gpo\",\"note\":\"HQ rollout\"}`.\n",
)
replace(
    "means a release row exists with no bytes: the `curl`\nfallback's first call in step 2 ran and its second did not. (The console's\n",
    "means a release row exists with no bytes: an upload\nthat stopped between its two requests. (The console's\n",
)

# Nothing runnable is left that sends a bearer token to the console's API.
for block in re.findall(r"```sh\n(.*?)```", text, re.DOTALL):
    if "Authorization: Bearer" in block:
        sys.exit("README.md: a sh block still sends a bearer token:\n" + block[:200])
readme.write_text(text)
print("rewrote deploy/README.md (bearer curls)")
```

Run: `python3 /tmp/t9-runbook/bearer.py`

Expected: `rewrote deploy/README.md (bearer curls)`.

- [ ] **Step 6: GO-LIVE y la tabla de la copia**

`/tmp/t9-runbook/golive.py`:

```python
"""Task 9, Step 6: GO-LIVE §1 and §3, its backup list, and the backup
document's row for the :443 certificate. Run from the repository root."""
import sys
from pathlib import Path


def edit(rel: str, pairs: list[tuple[str, str]]) -> None:
    path = Path(rel)
    text = path.read_text()
    for old, new in pairs:
        if text.count(old) != 1:
            sys.exit(f"{rel}: not there exactly once: {old[:70]!r}")
        text = text.replace(old, new)
    path.write_text(text)
    print(f"rewrote {rel}")


GO_LIVE_1_OLD = """`cp .env.example .env` leaves **six** values that must be replaced, and
**all six are literally `change-me`** — verified against `.env.example`
2026-08-16, which had said "five" here, and seven until own authentication
phase 4 (2026-11-15) took the identity provider's admin password out. The
distinction that matters is not which ones say `change-me`; it is *when* you
can replace them: four are generated before you start anything, and the two
OpenBao tokens do not exist until first boot has run (§3.2, §3.4), so they
stay `change-me` for a few minutes by necessity.
"""
GO_LIVE_1_NEW = """`./scripts/new-env.sh --host rmm.novieri.com` (`deploy/README.md` step 1)
writes `.env` from `.env.example`, mode 600, and replaces four of the **six**
values that must change — **all six are literally `change-me`** in
`.env.example`. The distinction that matters is not which ones say
`change-me`; it is *when* you can replace them: four are generated before you
start anything, and the two OpenBao tokens do not exist until first boot has
run (§3.2, §3.4), so they stay `change-me` for a few minutes by necessity.
Written by hand instead (`cp .env.example .env`), these are the six:
"""

GO_LIVE_TABLE2_OLD = """| `IT_SUITE_VERSION` | the short commit SHA, via `./scripts/build-images.sh` |"""
GO_LIVE_TABLE2_NEW = """| `ITS_PUBLIC_HOST` | the console's host, `rmm.novieri.com` | every `:443` router matches `Host()` on it, and Traefik asks Let's Encrypt for it (§3). `new-env.sh --host` writes it |
| `ITS_ACME_EMAIL` | the Let's Encrypt account's contact address | asked for by `new-env.sh`; Traefik refuses to start without it |
| `ITS_ACME_CA_SERVER` | Let's Encrypt's production directory | `new-env.sh --acme-ca-server` sets staging for a rehearsal, and CI sets Pebble |
| `IT_SUITE_VERSION` | the short commit SHA, via `./scripts/build-images.sh` |"""

GO_LIVE_3_OLD_START = "**`:443`, operator-facing — your real public certificate.**\n"
GO_LIVE_3_OLD_END = "**`:8443`, agent-facing — the internal CA, never your public certificate.**\n"
GO_LIVE_3_NEW = """**`:443`, operator-facing — a Let's Encrypt certificate that Traefik obtains
and renews itself** (since 2026-11-23; nothing is copied into `deploy/certs/`
any more, and the directory is gone). Traefik's ACME resolver `le` asks for
`ITS_PUBLIC_HOST` with the TLS-ALPN-01 challenge, which is answered on `:443`
itself, so `:80` stays shut; it keeps the account and the certificate in the
`traefik-acme` volume, and renews 30 days before expiry. It needs an `A`
record for the host pointing at this machine, **no `AAAA` record** (Let's
Encrypt tries IPv6 first, and this stack publishes IPv4 only), and `:443`
reachable from the Internet. `./scripts/preflight.sh` checks the records.

Traefik **silently serves its own self-signed certificate** while it has no
other — it does not fail. Always verify:

```sh
./scripts/check-public-tls.sh
```

`ok` names the issuer (Let's Encrypt), the name and the expiry. A `REFUSE`
naming `TRAEFIK DEFAULT CERT` means Traefik has no certificate yet;
`docker compose logs traefik | grep -i acme` says why. Every first start
asks for a new certificate when the volume is new: Let's Encrypt issues at
most 5 a week for the same name, so a rehearsal uses its staging directory
(`deploy/README.md` step 1).

Let's Encrypt is shortening its certificates (to 64 days from 2027-02-10, 45
from 2028-02-16). Traefik renews 30 days before expiry whatever the lifetime,
so neither needs a change here.

"""

GO_LIVE_3_CURLS = [
    ("curl -sk https://rmm.novieri.com/it-suite/probe -w '\\n%{http_code}\\n'",
     "curl -s https://rmm.novieri.com/it-suite/probe -w '\\n%{http_code}\\n'"),
    ("curl -sk 'https://rmm.novieri.com/it-suite?list-type=2' | grep -c '<ListBucketResult'",
     "curl -s 'https://rmm.novieri.com/it-suite?list-type=2' | grep -c '<ListBucketResult'"),
    ("curl -sk https://rmm.novieri.com/ | head -c 200",
     "curl -s https://rmm.novieri.com/ | head -c 200"),
]

path = Path("deploy/GO-LIVE.md")
text = path.read_text()
if text.count(GO_LIVE_3_OLD_START) != 1 or text.count(GO_LIVE_3_OLD_END) != 1:
    sys.exit("GO-LIVE.md: §3's :443 block is not there exactly once")
i, j = text.index(GO_LIVE_3_OLD_START), text.index(GO_LIVE_3_OLD_END)
path.write_text(text[:i] + GO_LIVE_3_NEW + text[j:])

edit("deploy/GO-LIVE.md", [
    (GO_LIVE_1_OLD, GO_LIVE_1_NEW),
    (GO_LIVE_TABLE2_OLD, GO_LIVE_TABLE2_NEW),
    ("Then:\n\n```sh\nchmod 600 deploy/.env\n```\n",
     "Either way:\n\n```sh\nchmod 600 deploy/.env\n```\n"),
    *GO_LIVE_3_CURLS,
    ("- **`deploy/.env` and `deploy/certs/`** — not in git by design, therefore not\n  in any git-based recovery.\n",
     "- **`deploy/.env`** — not in git by design, therefore not in any git-based\n"
     "  recovery. (The `:443` certificate is in the `traefik-acme` volume, which is\n"
     "  not backed up: losing it costs one new certificate, which Traefik obtains\n"
     "  at its next start.)\n"),
])

edit("docs/07-backup-restore.md", [
    ("| 10 | Operator TLS cert for `:443` | `deploy/certs/` on the host, **gitignored** | not in the compose volume set at all — it is a bind mount, so a volume-only backup misses it. Re-issuable from your public CA |",
     "| 10 | Operator TLS cert for `:443` | `traefik-acme` volume (Traefik's ACME store, since 2026-11-23) | not key material, and not backed up: a lost volume costs one new certificate, which Traefik asks Let's Encrypt for at its next start (at most 5 a week for the same name) |"),
])
```

Run: `python3 /tmp/t9-runbook/golive.py`

Expected: `rewrote deploy/GO-LIVE.md`, `rewrote docs/07-backup-restore.md`.

- [ ] **Step 7: Las pruebas pasan**

Run, desde `backend/`:

```bash
.venv/bin/pytest -q tests/test_runbook_first_boot.py tests/test_runbook_without_keycloak.py tests/test_deploy_runbook_secrets.py tests/test_email_domain_runbook.py tests/test_deploy_env_documentation.py tests/test_deploy_object_storage_route.py tests/test_install_link_docs.py tests/test_agent_managed_deployment.py tests/test_auth_phase4_falsifiers.py tests/test_deploy_script_steps.py
.venv/bin/ruff check tests/test_runbook_first_boot.py tests/test_runbook_without_keycloak.py ../deploy/scripts/issue-gateway-cert.py
.venv/bin/ruff format --check tests/test_runbook_first_boot.py
```

Expected: `130 passed` (medido sobre la oleada 1; la 2 no añade pruebas a estos archivos), `All checks passed!`, `1 file already formatted`.

Y lo que queda de los nombres viejos en los cuatro documentos, desde la raíz:

```bash
grep -cE '\bITMS\b|[Tt]oeshee|ITSuite|its-agent|it-suite internal CA' deploy/README.md deploy/GO-LIVE.md deploy/host-network.md docs/07-backup-restore.md
git diff --stat
```

Expected: `0` en los cuatro; y

```
 backend/tests/test_runbook_first_boot.py       |  177 +++++
 backend/tests/test_runbook_without_keycloak.py |    4 +-
 deploy/GO-LIVE.md                              |   79 +-
 deploy/README.md                               | 1018 ++++++++++++++----------
 deploy/host-network.md                         |   18 +-
 deploy/scripts/issue-gateway-cert.py           |    6 +-
 docs/07-backup-restore.md                      |    4 +-
```

(`test_runbook_first_boot.py` sale en el `--stat` después del `git add` del Step 8.)

Los encabezados del primer arranque, en orden: `awk '/^## First boot/,/^## Recovery link/' deploy/README.md | grep '^### '`

```
### Step 1 — `.env`
### Step 1b — Tagged images
### Step 1c — The pre-flight
### Step 2 — Postgres
### Step 3 — OpenBao: initialise, unseal, provision
### Step 4 — Migrations, then table privileges
### Step 5 — The audit HMAC key
### Step 6 — The gateway's certificate, and the client CA
### Step 7 — Start everything; Traefik obtains the certificate for `:443`
### Step 8 — Claim the installation
### Step 9 — Sign in, and enrol your second factor
### Step 10 — Confirm you really are an administrator
### Step 11 — Prove the platform actually works
### Step 12 — The first backup, so that there is a way back
```

- [ ] **Step 8: Commit**

```bash
git add backend/tests/test_runbook_first_boot.py backend/tests/test_runbook_without_keycloak.py deploy/README.md deploy/GO-LIVE.md deploy/host-network.md deploy/scripts/issue-gateway-cert.py docs/07-backup-restore.md
git commit -m "docs(deploy): the runbook for a new VPS at rmm.novieri.com (ACME, no bearer curls, after a reboot)"
rm -rf /tmp/t9-runbook
```

### Task 10: La CI — el agente de Linux compilado se inscribe y ejecuta un `hello` firmado

**Files:**
- Create: `deploy/ci/agent_flow.py`, `backend/tests/test_ci_agent_flow.py`
- Modify: `.github/workflows/ci.yml` (job `deploy`: `actions/setup-go@v5` y el paso `the Linux agent, built from this checkout` antes del paso del OAuth; en ese paso, el `trap` y el apartado 7c antes del 8)
- Modify (prueba): `backend/tests/test_ci_mcp_flow.py` (la aserción del `trap`, `:321-324`)

**Interfaces:**
- Consumes: de la Task 7, `mcp_flow.py` (`Client`, `Failed`, `Unreachable`, `check`, `ok`, `parse_resolve`, `pin_resolution`, `SESSION_COOKIE`; `--state-file` en la fase `open`, que deja el correo, la contraseña y dos códigos de recuperación del dueño), la variable `pebble_root` del paso y la dirección fija de traefik `172.19.240.250` en la red `it-suite_default` (`ci/acme-pebble.yml`); de la Task 8, el binario `novieri-agent` de `./cmd/agent`.
- Produces: `python3 ci/agent_flow.py --base URL [--resolve NOMBRE:IPv4] [--cafile RAÍZ] --state-file RUTA --phase token --token-file RUTA` y `… --phase job --hostname NOMBRE [--wait-s 120]`; `agent_flow.PHASES`, `AGENT_TRAIL`, `TERMINAL`, `write_secret(path, value)`, `read_owner(path)`, `principal_site(clients, sites_of)`, `chain_order(events)`, `trail_in_order(actions, expected=AGENT_TRAIL)`, `AgentFlow(client, owner)`.

**Decisiones de la tarea.**
- **Dentro del paso del OAuth, como 7c**, y no en un paso propio: es el único cuyo *shell* tiene el token raíz del OpenBao de la CI (para el token `gateway-issuer` del certificado del gateway) y cuyo `trap` borra el archivo del dueño (`mcp_flow.py` ya lo dice: «the step that runs both removes it however it ends»). El `trap` añade el directorio de datos del agente y su contenedor.
- **El certificado del gateway, como el paso 6 del runbook en su forma no interactiva**: la política `gateway-issuer` y su token con el raíz por *stdin* (como la CI ya hace con `its-app`), el token enmascarado antes de usarlo y llevado a `issue-gateway-cert.py` por *stdin*. Ningún secreto en un `argv` (`test_no_secret_of_the_vault_travels_in_an_argv` lo sigue comprobando).
- **El agente, como un equipo de Internet**: el binario compilado aquí (estático, como `release-agent.yml`), en un contenedor `busybox:1.36.1` en la red del stack, con `rmm.novieri.com` resuelto a traefik (`--add-host`): el 443 y el 8443 pasan por traefik igual que en el VPS. Confía solo en la raíz de Pebble, montada como el almacén de raíces del sistema; no hay `-k` ni `InsecureSkipVerify` en ningún lado. No se toca el `/etc/hosts` del *runner* (`vars.CI_RUNS_ON` puede ser un *runner* propio sin `sudo`). El token va en el archivo que el agente lee y borra al inscribirse (`config.EnrollTokenPath`, `<datos>/enroll-token`), 0600, nunca en el entorno ni en un `argv`; el contenedor corre con el usuario del *runner* y no publica ningún puerto (invariante 2).
- **Lo que prueba (diseño §4, §5)**: la inscripción por el 443 con un token de un uso; la conexión mTLS por el 8443 (el dispositivo en línea); un `hello` con un mensaje aleatorio que el servidor firma (`signed: true`), el agente verifica y contesta con su saludo (invariante 1); y la cadena de auditoría verificada por la API de la consola, con `enrollment.token_created`, `enrollment.device_enrolled`, `gateway.connected`, `jobs.dispatched` y `jobs.succeeded` en orden (invariante 4).
- **El orden de la cadena es el de sus `id`**, no el de `ts`: la primera ejecución en vivo (2026-11-23) falló aquí, porque `ts` va en segundos enteros y la API contesta de lo nuevo a lo viejo, y un `hello` despachado y contestado en el mismo segundo se leía contestado primero. `chain_order` ordena por `id` (UUIDv7, el orden que `docs/07-backup-restore.md` da por bueno) y una prueba lo fija.
- **Un código de recuperación por fase**: cada inicio de sesión gasta un segundo factor, y un código TOTP no se puede repetir dentro de su ventana. Sin servidor de correo, el aviso de código usado no se envía y no falla (`notices.deliver`, «Never raises for delivery»).

- [ ] **Step 0: La worktree**

Run: `git log --oneline -1`; fusionar la rama del coordinador (las oleadas 1 y 2) y la preparación de Global Constraints con Docker y las imágenes base de la sesión. Comprobar:

```bash
grep -c 'state-file' deploy/ci/mcp_flow.py
grep -c '172.19.240.250' deploy/ci/acme-pebble.yml
(cd agent && CGO_ENABLED=0 go build -trimpath -o /tmp/novieri-agent-check ./cmd/agent && echo built && rm /tmp/novieri-agent-check)
```

Expected: un número mayor que 0, `3`, `built`.

- [ ] **Step 1: Las pruebas**

Crear `backend/tests/test_ci_agent_flow.py`:

```python
"""`deploy/ci/agent_flow.py` and the `deploy` job's step 7c: the Linux agent
built from this checkout enrols over :443, connects over :8443 with mutual
TLS, verifies and runs a signed `hello`, and the audit chain still verifies
(design 2026-11-23-vps-y-marca-novieri §4, §5; plan 2026-11-23, Task 10).

The live run is CI's. These hold what it asserts, what it may print, and
that the job runs it the way an endpoint would: the agent's own binary, the
name it dials, the token in a 0600 file and never in an argv.
"""

import ast
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy/ci/agent_flow.py"
CI = REPO / ".github/workflows/ci.yml"
OAUTH_STEP = "the platform's own OAuth server and MCP door"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_ci_agent_flow", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


agents = _load()


def _steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = yaml.safe_load(CI.read_text())["jobs"]["deploy"]["steps"]
    return steps


def _run(name: str) -> str:
    for step in _steps():
        if str(step.get("name", "")).startswith(name):
            lines = step["run"].splitlines()
            return "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    raise AssertionError(f"the deploy job has no step named {name!r}...")


def test_it_imports_only_the_standard_library_and_its_sibling() -> None:
    """CI runs it with the runner's bare `python3`, next to `mcp_flow.py`."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(SCRIPT.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    assert names <= set(sys.stdlib_module_names) | {"__future__", "mcp_flow"}, names


def test_every_promised_section_is_marked_where_it_is_proven() -> None:
    """A section in `PHASES` is reached only by a `done` call naming it, and
    every `done` names a promised section."""
    marked = {
        node.args[0].value
        for node in ast.walk(ast.parse(SCRIPT.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "done"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    promised = {section for sections in agents.PHASES.values() for section in sections}
    assert marked == promised


def test_a_phase_that_comes_back_without_its_sections_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "owner.json"
    state.write_text(json.dumps({"email": "e", "password": "p", "recovery_codes": ["a", "b"]}))
    monkeypatch.setattr(agents.AgentFlow, "token", lambda self, token_file: None)
    rc = agents.main(
        [
            "--base",
            "https://rmm.example.com",
            "--state-file",
            str(state),
            "--phase",
            "token",
            "--token-file",
            str(tmp_path / "t"),
        ]
    )
    assert rc == 1
    assert "never reached" in capsys.readouterr().out


def test_the_token_is_written_once_0600_and_never_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "enroll-token"
    agents.write_secret(str(path), "tok-123")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert path.read_text() == "tok-123"
    with pytest.raises(FileExistsError):
        agents.write_secret(str(path), "again")
    source = SCRIPT.read_text()
    assert 'print(body["token"])' not in source and "token']" not in source.split("write_secret")[0]
    assert capsys.readouterr().out == ""


def test_an_owner_without_two_recovery_codes_is_refused(tmp_path: Path) -> None:
    state = tmp_path / "owner.json"
    state.write_text(json.dumps({"email": "e", "password": "p", "recovery_codes": ["only"]}))
    with pytest.raises(agents.Failed):
        agents.read_owner(str(state))


def test_the_site_is_the_internal_clients_principal() -> None:
    clients = [
        {"id": "c1", "kind": "customer"},
        {"id": "c2", "kind": "internal"},
        {"id": "c3", "kind": "unassigned"},
    ]
    sites = {"c2": [{"id": "s-other", "name": "Annex"}, {"id": "s-main", "name": "Principal"}]}
    assert agents.principal_site(clients, sites.__getitem__) == "s-main"
    with pytest.raises(agents.Failed):
        agents.principal_site([{"id": "c1", "kind": "customer"}], sites.__getitem__)
    with pytest.raises(agents.Failed):
        agents.principal_site([{"id": "c2", "kind": "internal"}], lambda _cid: [])


def test_the_audit_trail_is_found_in_order_and_only_in_order() -> None:
    trail = list(agents.AGENT_TRAIL)
    assert agents.trail_in_order(["auth.login", *trail, "auth.logout"])
    assert agents.trail_in_order([trail[0], "x", trail[1], trail[2], "y", trail[3], trail[4]])
    assert not agents.trail_in_order(trail[:-1])
    assert not agents.trail_in_order([trail[1], trail[0], *trail[2:]])
    assert agents.AGENT_TRAIL == (
        "enrollment.token_created",
        "enrollment.device_enrolled",
        "gateway.connected",
        "jobs.dispatched",
        "jobs.succeeded",
    )


def test_the_chain_is_read_in_its_own_order_not_by_the_second() -> None:
    """The first live run (2026-11-23) failed here: `ts` is whole seconds, and
    the audit API answers newest first, so a hello dispatched and answered in
    the same second read answered first. The chain's order is its ids'."""
    same_second = "2026-11-24T01:37:22Z"
    newest_first = [
        {
            "id": "06b04ea5-2724-7000-8000-000000000002",
            "ts": same_second,
            "action": "jobs.succeeded",
        },
        {
            "id": "06b04ea5-2723-7825-8000-000000000001",
            "ts": same_second,
            "action": "jobs.dispatched",
        },
    ]
    assert agents.chain_order(newest_first) == ["jobs.dispatched", "jobs.succeeded"]


def test_the_deploy_job_builds_the_agent_it_enrols() -> None:
    names = [str(step.get("name", step.get("uses", ""))) for step in _steps()]
    build = names.index("the Linux agent, built from this checkout")
    assert names[build - 1] == "actions/setup-go@v5"
    assert build < next(i for i, n in enumerate(names) if n.startswith(OAUTH_STEP))
    assert _run("the Linux agent, built") == (
        "CGO_ENABLED=0 go build -trimpath -o dist/novieri-agent ./cmd/agent"
    )


def test_the_agent_enrols_after_the_claim_and_before_the_last_checks() -> None:
    run = _run(OAUTH_STEP)
    order = [
        run.index("flow --phase open"),
        run.index("docker compose stop api"),
        run.index('/tmp/issue-gateway-cert.py rmm.novieri.com <<<"$gw_token"'),
        run.index("docker compose up -d gateway"),
        run.index("agents --phase token"),
        run.index("docker run -d --name ci-agent"),
        run.index("agents --phase job --hostname ci-agent"),
        run.index('test ! -e "$agent_data/enroll-token"'),
        run.index('if grep -qi keycloak <<<"$running"'),
    ]
    assert order == sorted(order), order


def test_the_agent_dials_the_public_name_and_trusts_only_pebbles_root() -> None:
    """An endpoint's view: rmm.novieri.com is traefik on the stack's network,
    both :443 and :8443 go through it, and the CA bundle the agent sees is
    the root that signed :443 -- no -k, no skip-verify, no other trust."""
    run = _run(OAUTH_STEP)
    start = run.index("docker run -d --name ci-agent")
    command = run[start : run.index("busybox:1.36.1 /usr/local/bin/novieri-agent", start)]
    assert "--network it-suite_default" in command
    assert "--add-host rmm.novieri.com:172.19.240.250" in command
    assert '-v "$pebble_root:/etc/ssl/certs/ca-certificates.crt:ro"' in command
    assert "-e ITS_AGENT_ENROLL_URL=https://rmm.novieri.com" in command
    assert "ITS_AGENT_ENROLL_TOKEN" not in run, "the token travels in a 0600 file, never in env"
    assert "--privileged" not in command and "-p " not in command, "the agent listens on nothing"


def test_the_gateway_issuer_token_is_masked_and_travels_on_stdin() -> None:
    run = _run(OAUTH_STEP)
    assert run.index('echo "::add-mask::$gw_token"') < run.index('<<<"$gw_token"')
    for line in run.splitlines():
        if "$gw_token" in line:
            assert line.strip().startswith('echo "::add-mask::') or '<<<"$gw_token"' in line, line


def test_what_the_agent_held_goes_however_the_step_ends() -> None:
    run = _run(OAUTH_STEP)
    assert (
        """trap 'rm -f "$init_json" "$RUNNER_TEMP/setup-code" "$RUNNER_TEMP/ci-owner.json"; """
        """rm -rf "$RUNNER_TEMP/agent-data"; docker rm -f ci-agent >/dev/null 2>&1 || true' EXIT"""
        in run
    )
    line = next(x.strip() for x in run.splitlines() if "agents --phase job" in x)
    assert line.endswith(
        "|| { docker logs ci-agent; docker compose logs api gateway traefik; exit 1; }"
    )
```

Y la aserción del `trap` en `test_ci_mcp_flow.py`, desde la raíz:

```bash
python3 - <<'PY'
"""Task 10, Step 1: the OAuth step's trap also removes what the agent held.
Run from the repository root."""
import sys
from pathlib import Path

path = Path("backend/tests/test_ci_mcp_flow.py")
text = path.read_text()
old = '''    assert (
        """trap 'rm -f "$init_json" "$RUNNER_TEMP/setup-code" "$RUNNER_TEMP/ci-owner.json"' EXIT"""
        in run
    )
'''
new = '''    # Since plan 2026-11-23, Task 10, the same trap removes the agent's data
    # directory and its container (test_ci_agent_flow.py).
    assert (
        """trap 'rm -f "$init_json" "$RUNNER_TEMP/setup-code" "$RUNNER_TEMP/ci-owner.json"; """
        """rm -rf "$RUNNER_TEMP/agent-data"; docker rm -f ci-agent >/dev/null 2>&1 || true' EXIT"""
        in run
    )
'''
if text.count(old) != 1:
    sys.exit("test_ci_mcp_flow.py: the trap assertion is not there exactly once")
path.write_text(text.replace(old, new))
print("rewrote backend/tests/test_ci_mcp_flow.py")
PY
```

Expected: `rewrote backend/tests/test_ci_mcp_flow.py`.

- [ ] **Step 2: Las pruebas fallan**

Run: `cd backend && .venv/bin/pytest -q tests/test_ci_agent_flow.py tests/test_ci_mcp_flow.py`

Expected: `1 error` en la recolección, `FileNotFoundError: … deploy/ci/agent_flow.py`. Y sin `test_ci_agent_flow.py` (`--deselect` no sirve para un error de recolección: `.venv/bin/pytest -q tests/test_ci_mcp_flow.py`): `1 failed, 50 passed`, `test_no_secret_of_the_vault_travels_in_an_argv` (el `trap` todavía no borra lo del agente).

- [ ] **Step 3: La implementación**

Crear `deploy/ci/agent_flow.py`:

```python
"""The CI's live check of an endpoint: the Linux agent built from this
checkout enrols over the stack's front door, connects to the gateway on :8443
with mutual TLS, verifies and runs a signed `hello`, and the audit chain still
verifies (design 2026-11-23-vps-y-marca-novieri §4 and §5; plan 2026-11-23,
Task 10).

It drives only the console's own API, signed in as the owner `mcp_flow.py`
created (its `--state-file`: the address, the password and two recovery
codes, mode 0600): one recovery code per phase, because a sign-in spends a
second factor and a TOTP code may not be replayed inside its window. The
agent itself is started by the `deploy` job between the two phases.

    python3 ci/agent_flow.py --base https://rmm.novieri.com \\
        --resolve rmm.novieri.com:127.0.0.1 --cafile "$pebble_root" \\
        --state-file "$RUNNER_TEMP/ci-owner.json" \\
        --phase token --token-file "$RUNNER_TEMP/agent-data/enroll-token"
    python3 ci/agent_flow.py ... --phase job --hostname ci-agent

`token`: a one-use, one-hour enrollment token for the organization's own
client's Principal site, written to a NEW file, 0600 (`O_EXCL`), and never
printed. `job`: the device the agent enrolled, online; a `hello` dispatched to
it, signed, verified by the agent and answered with its greeting; and the
audit chain verified, with the enrolment, the dispatch and the result in it.
It prints what held, never a token, a password or a code.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp_flow import (  # noqa: E402
    SESSION_COOKIE,
    Client,
    Failed,
    Unreachable,
    check,
    ok,
    parse_resolve,
    pin_resolution,
)

#: What each phase has to have proven before it may say "every assertion
#: held"; `main` fails a phase that came back with any of these unmarked
#: (the same rule as `mcp_flow.PHASES`). `backend/tests/test_ci_agent_flow.py`
#: holds the lists and every mark.
PHASES: dict[str, tuple[str, ...]] = {
    "token": (
        "signed in with a recovery code",
        "the Principal site of the organization's own client",
        "a one-use enrollment token, written and not printed",
    ),
    "job": (
        "signed in with a recovery code",
        "the agent's device, online",
        "a signed hello, verified and answered",
        "the audit chain verifies, with the agent in it",
    ),
}
#: The audit actions the job phase must find, in this order, after the token.
AGENT_TRAIL = (
    "enrollment.token_created",
    "enrollment.device_enrolled",
    "gateway.connected",
    "jobs.dispatched",
    "jobs.succeeded",
)
TERMINAL = frozenset({"succeeded", "failed", "expired", "refused", "cancelled"})


def write_secret(path: str, value: str) -> None:
    """A NEW file, 0600 from its creation (`O_EXCL`); nothing printed."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value)


def read_owner(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        owner = json.load(handle)
    check(
        isinstance(owner, dict)
        and isinstance(owner.get("recovery_codes"), list)
        and len(owner["recovery_codes"]) >= 2,
        "the owner's state file holds no two recovery codes",
    )
    return owner


def principal_site(
    clients: list[dict[str, Any]], sites_of: Callable[[str], list[dict[str, Any]]]
) -> str:
    """The organization's own client (`kind` internal) and its site named
    Principal (`app/services/clients.py`, `PRINCIPAL_SITE`)."""
    internal = [c for c in clients if c.get("kind") == "internal"]
    check(len(internal) == 1, f"{len(internal)} internal clients, not one")
    sites = [s for s in sites_of(internal[0]["id"]) if s.get("name") == "Principal"]
    check(len(sites) == 1, "the internal client has no site named Principal")
    return str(sites[0]["id"])


def chain_order(events: list[dict[str, Any]]) -> list[str]:
    """The actions oldest first, in the chain's own order: by `id`, a UUIDv7
    (`docs/07-backup-restore.md`, "`ORDER BY id` is the correct ordering").
    Not by `ts`, which is whole seconds: a job dispatched and answered in the
    same second would read answered first (measured on 2026-11-23)."""
    return [event["action"] for event in sorted(events, key=lambda event: str(event["id"]))]


def trail_in_order(actions: list[str], expected: tuple[str, ...] = AGENT_TRAIL) -> bool:
    """`expected` appears in `actions` as a subsequence, oldest first."""
    rest = iter(actions)
    return all(any(action == want for action in rest) for want in expected)


class AgentFlow:
    def __init__(self, client: Client, owner: dict[str, Any]) -> None:
        self.http = client
        self.owner = owner
        self.reached: set[str] = set()

    def done(self, section: str) -> None:
        self.reached.add(section)

    def get(self, path: str) -> Any:
        answer = self.http.request("GET", path)
        check(answer.status == 200, f"GET {answer.path} answered {answer.status}")
        return answer.json()

    def sign_in(self, code_index: int) -> None:
        login = self.http.request(
            "POST",
            "/api/v1/auth/login",
            body={"email": self.owner["email"], "password": self.owner["password"]},
            console=True,
        )
        check(login.status == 200, f"the sign-in answered {login.status}")
        check(login.json().get("state") == "factor_required", "the sign-in asked for no factor")
        factor = self.http.request(
            "POST",
            "/api/v1/auth/factor/recovery",
            body={"code": self.owner["recovery_codes"][code_index]},
            console=True,
        )
        check(factor.status == 200, f"the recovery code answered {factor.status}")
        check(factor.json().get("state") == "signed_in", "the recovery code did not sign in")
        check(SESSION_COOKIE in self.http.cookies.values, "no session cookie after the factor")
        ok("signed in: password and a recovery code")
        self.done("signed in with a recovery code")

    def token(self, token_file: str) -> None:
        self.sign_in(0)
        site = principal_site(
            self.get("/api/v1/clients"), lambda cid: self.get(f"/api/v1/clients/{cid}/sites")
        )
        ok("the site: the internal client's Principal")
        self.done("the Principal site of the organization's own client")
        made = self.http.request(
            "POST",
            "/api/v1/enrollment-tokens",
            body={"name": "ci agent", "expires_in_hours": 1, "max_uses": 1, "site_id": site},
            console=True,
        )
        check(made.status == 201, f"POST /api/v1/enrollment-tokens answered {made.status}")
        body = made.json()
        check(body.get("max_uses") == 1 and body.get("site_id") == site, "not the token asked for")
        write_secret(token_file, body["token"])
        ok("an enrollment token: one use, one hour, written 0600 and not printed")
        self.done("a one-use enrollment token, written and not printed")

    def device(self, hostname: str, wait_s: float) -> str:
        deadline = time.monotonic() + wait_s
        seen = "no device"
        while True:
            items = self.get("/api/v1/devices?limit=200")["items"]
            mine = [d for d in items if d.get("hostname") == hostname]
            if mine:
                seen = f"agent_status={mine[0].get('agent_status')}"
                if mine[0].get("agent_status") == "online":
                    return str(mine[0]["id"])
            if time.monotonic() > deadline:
                raise Failed(f"no online device {hostname} after {wait_s:.0f} s ({seen})")
            time.sleep(2)

    def job(self, hostname: str, wait_s: float) -> None:
        self.sign_in(1)
        device_id = self.device(hostname, wait_s)
        ok(f"the device {hostname} enrolled and is online")
        self.done("the agent's device, online")

        message = "novieri-ci-" + secrets.token_hex(4)
        made = self.http.request(
            "POST",
            "/api/v1/jobs",
            body={"type": "hello", "device_id": device_id, "params": {"message": message}},
            console=True,
        )
        check(made.status == 201, f"POST /api/v1/jobs answered {made.status}")
        job = made.json()
        check(job.get("signed") is True, "the job carries no server signature")
        deadline = time.monotonic() + wait_s
        while job.get("status") not in TERMINAL:
            if time.monotonic() > deadline:
                raise Failed(f"the hello job is still {job.get('status')} after {wait_s:.0f} s")
            time.sleep(1)
            job = self.get(f"/api/v1/jobs/{job['id']}")
        check(job["status"] == "succeeded", f"the hello job ended {job['status']}")
        check(job.get("result") == {"greeting": message}, "the hello result is not its greeting")
        ok("hello: signed, verified by the agent, answered with its greeting")
        self.done("a signed hello, verified and answered")

        verified = self.get("/api/v1/audit/verify")
        check(
            verified.get("ok") is True and verified.get("broken_at") is None, "the chain is broken"
        )
        events = self.get("/api/v1/audit?limit=200")["items"]
        check(
            trail_in_order(chain_order(events)),
            f"the audit log lacks, in order: {', '.join(AGENT_TRAIL)}",
        )
        ok(f"the audit chain verifies over {verified['count']} events, the agent's included")
        self.done("the audit chain verifies, with the agent in it")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True, help="the public entrypoint")
    parser.add_argument("--cafile", help="the root the entrypoint's certificate chains to")
    parser.add_argument(
        "--resolve", type=parse_resolve, action="append", default=[], help="NAME:ADDRESS"
    )
    parser.add_argument("--state-file", required=True, help="the owner's sign-in (mcp_flow.py)")
    parser.add_argument("--phase", choices=tuple(PHASES), required=True)
    parser.add_argument("--token-file", help="phase token: where the token goes")
    parser.add_argument("--hostname", help="phase job: the agent's hostname")
    parser.add_argument("--wait-s", type=float, default=120.0)
    args = parser.parse_args(argv)
    if args.phase == "token" and not args.token_file:
        parser.error("--phase token needs --token-file")
    if args.phase == "job" and not args.hostname:
        parser.error("--phase job needs --hostname")
    pin_resolution(dict(args.resolve))
    try:
        flow = AgentFlow(Client(args.base, args.cafile), read_owner(args.state_file))
        if args.phase == "token":
            flow.token(args.token_file)
        else:
            flow.job(args.hostname, args.wait_s)
    except Failed as exc:
        print(f"FAIL  {exc}")
        return 1
    except Unreachable as exc:
        print(f"UNREACHABLE  {exc}", file=sys.stderr)
        return 2
    missing = [section for section in PHASES[args.phase] if section not in flow.reached]
    if missing:
        print(f"FAIL  phase {args.phase} never reached: {'; '.join(missing)}")
        return 1
    print(f"agent_flow: phase {args.phase}: every assertion held")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Y el job, desde la raíz:

```bash
python3 - <<'PY'
"""Task 10, Step 3: the deploy job builds the Linux agent and, inside the
OAuth step (whose shell holds the vault's root token and whose trap removes
the owner's sign-in), enrols it and round-trips a signed hello. Run from the
repository root."""
import sys
from pathlib import Path

path = Path(".github/workflows/ci.yml")
text = path.read_text()


def replace(old: str, new: str) -> None:
    global text
    if text.count(old) != 1:
        sys.exit(f"ci.yml: not there exactly once: {old[:70]!r}")
    text = text.replace(old, new)


replace(
    "      - name: the platform's own OAuth server and MCP door, live, through Traefik\n",
    """      - uses: actions/setup-go@v5
        with:
          go-version: "1.24"
          cache-dependency-path: agent/go.sum

      - name: the Linux agent, built from this checkout
        # The binary the OAuth step's 7c enrols (design 2026-11-23-vps-y-marca-
        # novieri §4): static, as release-agent.yml builds it, so it runs in a
        # bare busybox container.
        working-directory: agent
        run: CGO_ENABLED=0 go build -trimpath -o dist/novieri-agent ./cmd/agent

      - name: the platform's own OAuth server and MCP door, live, through Traefik
""",
)
replace(
    """          trap 'rm -f "$init_json" "$RUNNER_TEMP/setup-code" "$RUNNER_TEMP/ci-owner.json"' EXIT
""",
    """          trap 'rm -f "$init_json" "$RUNNER_TEMP/setup-code" "$RUNNER_TEMP/ci-owner.json"; rm -rf "$RUNNER_TEMP/agent-data"; docker rm -f ci-agent >/dev/null 2>&1 || true' EXIT
""",
)
replace(
    """          # 8. And nothing of the old identity provider ever ran in this
""",
    """          # 7c. An endpoint (design 2026-11-23 §4, §5): the gateway's
          #     certificate from the internal CA (deploy/README.md step 6,
          #     its gateway-issuer token on stdin), the gateway, and the
          #     Linux agent built from this checkout -- enrolled over :443
          #     with a one-use token, connected over :8443 with mutual TLS,
          #     running a hello the server signed and the agent verified.
          #     The agent dials rmm.novieri.com as an endpoint does: the name
          #     is traefik's address on the stack's network (--add-host), and
          #     the root it trusts for :443 is Pebble's.
          gw_token=$(docker compose exec -T openbao sh -c 'read -r BAO_TOKEN && export BAO_TOKEN &&
            bao policy write gateway-issuer /bao/gateway-issuer.hcl >/dev/null &&
            bao token create -orphan -policy=gateway-issuer -ttl=1h -field=token' <<<"$root")
          echo "::add-mask::$gw_token"
          docker compose run --rm --no-deps -T \\
            -v "$PWD/scripts/issue-gateway-cert.py:/tmp/issue-gateway-cert.py:ro" \\
            --entrypoint python gateway /tmp/issue-gateway-cert.py rmm.novieri.com <<<"$gw_token"
          docker compose up -d gateway
          agent_data="$RUNNER_TEMP/agent-data"
          mkdir -m 700 "$agent_data"
          agent_bin="$(cd ../agent && pwd)/dist/novieri-agent"
          agents() { python3 ci/agent_flow.py --base https://rmm.novieri.com --resolve rmm.novieri.com:127.0.0.1 --cafile "$pebble_root" --state-file "$RUNNER_TEMP/ci-owner.json" "$@"; }
          agents --phase token --token-file "$agent_data/enroll-token" || { docker compose logs api traefik; exit 1; }
          docker run -d --name ci-agent --hostname ci-agent --network it-suite_default \\
            --add-host rmm.novieri.com:172.19.240.250 --user "$(id -u):$(id -g)" \\
            -v "$agent_bin:/usr/local/bin/novieri-agent:ro" \\
            -v "$pebble_root:/etc/ssl/certs/ca-certificates.crt:ro" \\
            -v "$agent_data:/var/lib/novieri-agent" \\
            -e ITS_AGENT_ENROLL_URL=https://rmm.novieri.com -e ITS_AGENT_DATA_DIR=/var/lib/novieri-agent \\
            busybox:1.36.1 /usr/local/bin/novieri-agent
          agents --phase job --hostname ci-agent || { docker logs ci-agent; docker compose logs api gateway traefik; exit 1; }
          test ! -e "$agent_data/enroll-token" || { echo "::error::the agent kept its enrollment token after enrolling"; exit 1; }
          docker logs ci-agent 2>&1 | grep -E 'msg=enrolled|gateway connected'

          # 8. And nothing of the old identity provider ever ran in this
""",
)
path.write_text(text)
print("rewrote .github/workflows/ci.yml")
PY
backend/.venv/bin/python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml')); print('yaml-ok')"
```

Expected: `rewrote .github/workflows/ci.yml`, `yaml-ok`.

- [ ] **Step 4: Las pruebas pasan**

Run, desde `backend/`:

```bash
.venv/bin/pytest -q tests/test_ci_agent_flow.py tests/test_ci_mcp_flow.py
.venv/bin/ruff check tests/test_ci_agent_flow.py tests/test_ci_mcp_flow.py
.venv/bin/ruff check --config pyproject.toml ../deploy/ci/agent_flow.py
.venv/bin/ruff format --check tests/test_ci_agent_flow.py
.venv/bin/mypy tests/test_ci_agent_flow.py
```

Expected: `64 passed` (medido sobre una copia de `1bc0e5e` con las Tasks 3, 4, 7, 9 y esta), `All checks passed!` dos veces, `1 file already formatted`, `Success: no issues found in 1 source file`.

- [ ] **Step 5: Commit**

```bash
git add deploy/ci/agent_flow.py backend/tests/test_ci_agent_flow.py backend/tests/test_ci_mcp_flow.py .github/workflows/ci.yml
git commit -m "ci(deploy): the Linux agent built from this checkout enrols over :443, connects over :8443 and runs a signed hello"
```

- [ ] **Step 6: El job, en rojo y en verde, aquí**

En una rama de ensayo que no se fusiona, como en la Task 7, Step 6 (con el mismo `$S/run_ci_job.py` y las imágenes base de la sesión): `git switch -c t10-rehearsal`, y en ella las ramas de las Tasks 4, 3 y 7, en ese orden.

**Rojo:** un gateway que no arranca. El agente se inscribe por el 443 y nunca conecta por el 8443; la fase `job` tiene que fallar:

```bash
cd deploy && rm -f .env && docker compose down -v 2>/dev/null; cd ..
sed -i '/^          docker compose up -d gateway$/d' .github/workflows/ci.yml
backend/.venv/bin/python "$S/run_ci_job.py" . deploy 2>&1 | tee "$S/t10-red.log" | grep -E '^=====|^ok    (signed in: password and|the site|an enrollment)|^FAIL|agent_flow: phase' ; git checkout -- .github/workflows/ci.yml
```

Expected:

```
===== the Linux agent, built from this checkout: exit 0
ok    signed in: password and a recovery code
ok    the site: the internal client's Principal
ok    an enrollment token: one use, one hour, written 0600 and not printed
agent_flow: phase token: every assertion held
ok    signed in: password and a recovery code
FAIL  no online device ci-agent after 120 s (agent_status=offline)
===== the platform's own OAuth server and MCP door, live, through Traefik: exit 1
===== teardown: exit 0
```

y en el registro del agente (`docker logs ci-agent`, que el paso imprime al fallar), `msg=enrolled … gateway_url=wss://rmm.novieri.com:8443/agent` seguido de `msg="gateway disconnected; reconnecting" err="transport: dial wss://rmm.novieri.com:8443/agent: …"`. (Medido el 2026-11-23.)

**Verde:**

```bash
cd deploy && rm -f .env; cd ..
backend/.venv/bin/python "$S/run_ci_job.py" . deploy 2>&1 | tee "$S/t10-green.log" | grep -E '^=====|presents a trusted|answers 503|^ok    (signed in: password and|the site|an enrollment|the device|hello|the audit)|agent_flow: phase|mcp_flow: phase|msg=enrolled|gateway connected'
```

Expected (medido el 2026-11-23 sobre esa misma combinación, sin los dos pasos de la copia, que este ensayo no necesita y la Task 7 ya corre; aquí corren todos):

```
===== the Linux agent, built from this checkout: exit 0
ok      127.0.0.1:443 presents a trusted certificate for rmm.novieri.com
mcp_flow: phase closed: every assertion held
mcp_flow: phase open: every assertion held
with the api stopped, /api/v1/setup/status answers 503
ok    signed in: password and a recovery code
ok    the site: the internal client's Principal
ok    an enrollment token: one use, one hour, written 0600 and not printed
agent_flow: phase token: every assertion held
ok    signed in: password and a recovery code
ok    the device ci-agent enrolled and is online
ok    hello: signed, verified by the agent, answered with its greeting
ok    the audit chain verifies over 34 events, the agent's included
agent_flow: phase job: every assertion held
time=… level=INFO msg=enrolled device_id=… gateway_url=wss://rmm.novieri.com:8443/agent cert_not_after=…
time=… level=INFO msg="gateway connected" url=wss://rmm.novieri.com:8443/agent
===== the platform's own OAuth server and MCP door, live, through Traefik: exit 0
===== teardown: exit 0
```

con `exit 0` en cada paso anterior. El informe de la tarea copia las líneas de los dos registros. Si un paso falla por algo que este plan no previó, se corrige el paso en la rama de la tarea, con su commit, y el informe dice qué fue. Al terminar: `cd deploy && docker compose down -v; cd .. && git switch - && git branch -D t10-rehearsal`.

### Task 11: La guarda de nombres viejos, y lo que queda

**Files:**
- Create: `backend/tests/test_no_old_brand_names.py`
- Modify: `backend/app/core/config.py:106` (el host del comentario de `public_console_url`)
- Modify: `scripts/preflight_email_domain.py:7`, `:41`, `:99` (los ejemplos del dominio)
- Modify: `README.md:1-3` (el nombre del producto), `docs/product.md:1`, `:30`, `:240-247` (el nombre, y el párrafo que decía que no estaba elegido)

**Interfaces:**
- Consumes: el árbol con las oleadas 1 a 3 (Tasks 1 y 8: los nombres; Task 9: el runbook; las demás no tocan nombres que la guarda lea). De `tests/test_runbook_without_keycloak.py`, `_dated(lines, index)`.
- Produces: `test_no_old_brand_names.py` con `OLD`, `KEPT`, `offending(text) -> bool`, `python_strings(source) -> list[tuple[int, str]]`, `go_strings(source) -> list[tuple[int, str]]` y `offenders_in(path) -> Iterator[str]`. La Task 12 la vuelve a pasar si cambia el runbook.

**Decisiones de la tarea.**
- **Lo que la guarda lee (diseño §1, último párrafo):** los textos visibles de `backend/app/`, `agent/` y `deploy/`. En Python y en Go, los literales de cadena y no los comentarios ni los *docstrings*, como `test_no_vendor_names_backend.py`: un comentario que cuenta la historia sigue siendo verdad. En los demás archivos, cada línea, salvo la que `_dated` lee como historia (fase 4 por su nombre, o una fecha de 2026-11 en adelante, en pasado). `agent/` sin sus pruebas ni `testdata/`; `backend/tests/` no se lee (ahí «ITMS» es a veces el valor de un *fixture*, Task 8).
- **Lo que no es una falta (`KEPT`, diseño §1.2):** las claves `ITS_*` e `ITMS_*`, las cabeceras `X-ITMS-*`, `ITMSRSP1`, `its-agent-release/v1`, el módulo Go `github.com/toeshee/it-suite/agent` y la cabecera `X-Toeshee-Event-Id` de los *webhooks* (Apéndice B, P4-B-09; pregunta Q2). `itms` en minúsculas no se lee: solo nombra identificadores y datos guardados, todos de §1.2.
- **Medido el 2026-11-23:** sobre `1bc0e5e` la guarda encuentra **745** nombres viejos (260 en `backend/app`, 330 en `agent/`, 155 en `deploy/`); sobre el árbol con las Tasks 1, 3, 4, 7, 8, 9 y 10, **ninguno**. Esta tarea no tiene que renombrar nada dentro de su alcance: lo hicieron la 1, la 8 y la 9; si la guarda encuentra algo aquí, es que una fusión perdió un cambio, y se corrige en el archivo, no en la guarda.
- **Lo que queda fuera de la guarda y §1.1 sí nombra:** el host `itms.toeshee.com` en el único ejemplo vigente fuera de `deploy/` (el comentario de `config.py`), los ejemplos del guion del operador `scripts/preflight_email_domain.py`, y el nombre del producto donde el repositorio se presenta (`README.md`, `docs/product.md`, cuyo §9 decía que el nombre no estaba elegido; lo eligió el operador, A3). Se quedan como están, por ser historia o de otro proyecto: `HANDOFF.md` (fechado el 2026-08-16), los diseños y revisiones fechados de `docs/design/` y `docs/reviews/`, las `spec.md` y `plan.md` de los módulos, las migraciones ya publicadas, `CLAUDE.md` (Q1), `DESIGN.md` y `.impeccable/` (el sistema de diseño de la consola: proyecto 3, Apéndice F), y las frases que dicen un hecho de Toeshee (`docs/00-vision.md`, «Toeshee's own fleet»).

- [ ] **Step 0: La worktree**

Run: `git log --oneline -1`; fusionar la rama del coordinador (las oleadas 1 a 3). `grep -c 'novieri-agent' agent/packaging/linux/novieri-agent.service` imprime un número mayor que 0 y `grep -c '## After a reboot or a restart' deploy/README.md` imprime `1`.

- [ ] **Step 1: La guarda**

Crear `backend/tests/test_no_old_brand_names.py`:

```python
"""NO OLD NAME IN WHAT A PERSON, OR A CLIENT'S COMPUTER, IS SHOWN (design
2026-11-23-vps-y-marca-novieri §1; operator decisions B4 and A3).

The product is "Novieri RMM" since 2026-11-23. This fixes the list of §1.1:
`ITMS` (the word), `Toeshee` in any case, `ITSuite`, `its-agent`,
`it-suite internal CA` and `itms.toeshee.com` (which the `toeshee` rule
covers) are refused in

  * `backend/app/`: every string literal a Python module holds -- not its
    docstrings, not its comments -- and every line of any other file (the
    installer copies the install command carries to a computer);
  * `agent/`: every string literal a Go file compiles in -- not its comments
    -- and every line of any other file (the packaging scripts, the unit,
    the plist, the Debian control file), tests and `testdata/` excepted;
  * `deploy/`: every line of every file, the Python ones by their literals.

THE SAME TECHNIQUE AS `test_no_vendor_names_backend.py`: source is parsed,
not grepped, where it can be -- so a comment that records history stays
true, and implicit concatenation and f-strings are read whole.

WHAT KEEPS ITS NAME (`KEPT`, design §1.2), removed from a line before it is
read: the `ITS_`/`ITMS_` environment keys, the `X-ITMS-*` headers and the
`ITMSRSP1` magic (the protocol with the console and the agent), the release
signature's domain string, the Go module path, and the notifications'
webhook header (a contract with whoever consumes it: Appendix B, P4-B-09).
Lower-case `itms` is never read: it names only identifiers and stored values
(`__Host-itms_session`, `itms-local:`, `itms-rsp.1`, `urn:itms:loopback`, the
crypto domain strings), every one of them in §1.2.

DATED HISTORY IS ALLOWED in the files read line by line: a line the runbooks'
rule (`test_runbook_without_keycloak._dated`) reads as history -- own
authentication's phase 4 by name, or a date of 2026-11 or later, in the past
tense -- may name what the product was called.
"""

import ast
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.test_runbook_without_keycloak import _dated

REPO = Path(__file__).resolve().parents[2]

OLD = re.compile(r"\bITMS\b|(?i:toeshee)|ITSuite|its-agent|it-suite internal CA")
KEPT = re.compile(
    r"\bITS_[A-Z0-9_]+|\bITMS_[A-Z0-9_]+"
    r"|X-ITMS-[A-Za-z]+|\bITMSRSP1\b"
    r"|its-agent-release/v1"
    r"|github\.com/toeshee/it-suite/agent"
    r"|X-Toeshee-Event-Id"
)


def offending(text: str) -> bool:
    return OLD.search(KEPT.sub("", text)) is not None


# --------------------------------------------------------------------------- #
# The extractors
# --------------------------------------------------------------------------- #


def python_strings(source: str) -> list[tuple[int, str]]:
    """`(line, text)` of every string constant but a docstring; an f-string
    is its literal parts, joined."""
    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
    found: list[tuple[int, str]] = []
    joined: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts = [v for v in node.values if isinstance(v, ast.Constant)]
            joined.update(id(v) for v in parts)
            found.append((node.lineno, "".join(str(v.value) for v in parts)))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and id(node) not in joined
        ):
            found.append((node.lineno, node.value))
    return found


def go_strings(source: str) -> list[tuple[int, str]]:
    """`(line, text)` of every interpreted, raw and rune literal; comments
    are skipped. Escapes are left as written: `C:\\\\ITSuite` still reads
    `ITSuite`."""
    found: list[tuple[int, str]] = []
    i, line, n = 0, 1, len(source)
    while i < n:
        char = source[i]
        if char == "\n":
            line += 1
            i += 1
        elif source.startswith("//", i):
            end = source.find("\n", i)
            i = n if end < 0 else end
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = n if end < 0 else end + 2
            line += source.count("\n", i, end)
            i = end
        elif char == "`":
            end = source.find("`", i + 1)
            end = n if end < 0 else end
            found.append((line, source[i + 1 : end]))
            line += source.count("\n", i, end)
            i = end + 1
        elif char in "\"'":
            end = i + 1
            while end < n and source[end] not in (char, "\n"):
                end += 2 if source[end] == "\\" else 1
            found.append((line, source[i + 1 : end]))
            i = end + 1
        else:
            i += 1
    return found


# --------------------------------------------------------------------------- #
# The walk
# --------------------------------------------------------------------------- #


def _tracked(*roots: str) -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--", *roots],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [REPO / rel for rel in out.splitlines()]


def _in_scope(path: Path) -> bool:
    rel = path.relative_to(REPO).as_posix()
    if rel.startswith("agent/"):
        return not rel.endswith("_test.go") and "/testdata/" not in rel
    return rel.startswith(("backend/app/", "deploy/"))


def offenders_in(path: Path) -> Iterator[str]:
    rel = path.relative_to(REPO).as_posix()
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return
    if path.suffix == ".py":
        pairs = python_strings(source)
    elif path.suffix == ".go":
        pairs = go_strings(source)
    else:
        lines = source.splitlines()
        pairs = [
            (index + 1, line)
            for index, line in enumerate(lines)
            if offending(line) and not _dated(lines, index)
        ]
    for line, text in pairs:
        if offending(text):
            yield f"{rel}:{line}: {text.strip()[:100]!r}"


def _files() -> list[Path]:
    return [p for p in _tracked("backend/app", "agent", "deploy") if _in_scope(p)]


# --------------------------------------------------------------------------- #
# The extractors are not vacuous, and stay inside their claim
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('X = "Install the ITMS agent"', "Install the ITMS agent"),
        ('X = ("[Toeshee] " "Ticket")', "[Toeshee] Ticket"),
        ('X = f"its-agent-{os}"', "its-agent-"),
        ('def f() -> str:\n    return "C:\\\\ITSuite"', "C:\\ITSuite"),
    ],
)
def test_the_python_extractor_reads_what_a_module_can_show(source: str, expected: str) -> None:
    assert expected in [text for _line, text in python_strings(source)]


def test_the_python_extractor_leaves_docstrings_and_comments() -> None:
    source = '"""The ITMS module."""\n# Toeshee wrote this\ndef f() -> None:\n    """ITSuite."""\n'
    assert python_strings(source) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('const a = "ITMS Agent"', "ITMS Agent"),
        ("const b = `ExecStart=/usr/local/bin/its-agent`", "ExecStart=/usr/local/bin/its-agent"),
        ('const c = "C:\\\\ITSuite\\\\agent"', "C:\\\\ITSuite\\\\agent"),
        ('const d = "say \\"Toeshee\\" twice"', 'say \\"Toeshee\\" twice'),
    ],
)
def test_the_go_extractor_reads_every_literal(source: str, expected: str) -> None:
    assert expected in [text for _line, text in go_strings(source)]


def test_the_go_extractor_leaves_comments_and_counts_lines() -> None:
    source = '// ITMS\n/* Toeshee\n */\nconst x = 1 // its-agent\nvar y = `a\nb`\nvar z = "ok"\n'
    assert go_strings(source) == [(5, "a\nb"), (7, "ok")]


@pytest.mark.parametrize(
    "kept",
    [
        "X-ITMS-Request: 1",
        "ITMS_BIND_IP=0.0.0.0",
        "ITS_AGENT_ENROLL_URL",
        "its-agent-release/v1\\n",
        '"github.com/toeshee/it-suite/agent/internal/jobs"',
        "magic ITMSRSP1",
        '{"X-Toeshee-Event-Id": "1"}',
        "__Host-itms_session",
        "itms-local:",
    ],
)
def test_what_section_1_2_keeps_is_not_an_offence(kept: str) -> None:
    assert not offending(kept)


@pytest.mark.parametrize(
    "old",
    [
        "Install the ITMS agent",
        "[Toeshee] Ticket",
        "%ProgramData%\\ITSuite",
        "/usr/local/bin/its-agent",
        "https://itms.toeshee.com",
        "com.toeshee.itsagent",
        "/opt/toeshee",
        "CN = it-suite internal CA",
        "X-ITMS-Request and then ITMS",
    ],
)
def test_every_old_name_of_section_1_1_is_an_offence(old: str) -> None:
    assert offending(old)


def test_a_planted_name_is_found_where_it_would_ship(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The walk reads the file it is given, by its kind: a Go literal, a
    script's line, a Python constant."""
    monkeypatch.setattr(f"{__name__}.REPO", tmp_path)
    for rel, body in (
        ("agent/internal/x/x.go", 'package x\nconst N = "ITMS Agent"\n'),
        ("agent/packaging/linux/x.sh", "#!/bin/sh\necho its-agent\n"),
        ("backend/app/x.py", 'X = "Sign in to Toeshee"\n'),
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        assert list(offenders_in(path)), rel


# --------------------------------------------------------------------------- #
# The guard
# --------------------------------------------------------------------------- #


def test_the_walk_reaches_all_three_trees() -> None:
    files = [p.relative_to(REPO).as_posix() for p in _files()]
    for root, floor in (("backend/app/", 400), ("agent/", 150), ("deploy/", 30)):
        count = sum(1 for rel in files if rel.startswith(root))
        assert count > floor, f"only {count} files under {root}: the walk is broken"
    assert "agent/internal/install/mode.go" in files
    assert "backend/app/modules/notification/delivery.py" in files
    assert "deploy/docker-compose.yml" in files
    assert not [rel for rel in files if rel.endswith("_test.go") or "/testdata/" in rel]


def test_no_old_name_is_shown() -> None:
    found = [hit for path in _files() for hit in offenders_in(path)]
    assert found == [], "old names (design 2026-11-23 §1.1):\n" + "\n".join(found)
```

- [ ] **Step 2: La guarda, en rojo donde había nombres y en verde donde ya no**

Primero, sobre el árbol de antes del proyecto, en una worktree aparte que se borra después:

```bash
git worktree add /tmp/guard-base 1bc0e5e
cp backend/tests/test_no_old_brand_names.py /tmp/guard-base/backend/tests/
ln -s /home/user/Novieri-RMM/backend/.venv /tmp/guard-base/backend/.venv
(cd /tmp/guard-base/backend && PYTHONPATH=$PWD .venv/bin/pytest -q tests/test_no_old_brand_names.py 2>&1 | grep -E 'passed|failed'; PYTHONPATH=$PWD .venv/bin/pytest -q tests/test_no_old_brand_names.py::test_no_old_name_is_shown 2>&1 | grep -cE '^E +(backend|agent|deploy)/')
git worktree remove --force /tmp/guard-base
```

Expected: `1 failed, 30 passed` y `745` (las tres primeras: `agent/.gitignore:3: '/its-agent'`, `agent/README.md:1: '# its-agent'`, `agent/README.md:3: 'The Toeshee endpoint agent. …'`).

Después, en esta worktree: `cd backend && .venv/bin/pytest -q tests/test_no_old_brand_names.py`

Expected: `31 passed`.

Y que lee literales y no comentarios, con un nombre plantado y quitado:

```bash
sed -i 's/^const DisplayName = "Novieri RMM Agent"/const DisplayName = "ITMS Agent"/' agent/internal/service/service.go
(cd backend && .venv/bin/pytest -q tests/test_no_old_brand_names.py 2>&1 | grep -E '^E +agent/|passed|failed')
git checkout -- agent/internal/service/service.go
sed -i 's|^// DisplayName is what the Windows Services console shows.|// DisplayName is what the Windows Services console shows (ITMS before 2026-11-23).|' agent/internal/service/service.go
(cd backend && .venv/bin/pytest -q tests/test_no_old_brand_names.py 2>&1 | tail -1)
git checkout -- agent/internal/service/service.go
```

Expected: `E         agent/internal/service/service.go:38: 'ITMS Agent'` y `1 failed, 30 passed`; luego `31 passed`.

- [ ] **Step 3: Lo que queda**

Desde la raíz de la worktree:

```bash
python3 - <<'PY'
"""Task 11, Step 3: what the guard does not walk and §1.1 still names -- the
host in the one current example outside deploy/, the operator script's
examples, and the product's name where the repository introduces itself.
Run from the repository root."""
import sys
from pathlib import Path

EDITS: dict[str, list[tuple[str, str]]] = {
    "backend/app/core/config.py": [
        (
            "    # The https address people open the console at, e.g. https://itms.toeshee.com.\n",
            "    # The https address people open the console at, e.g. https://rmm.novieri.com.\n",
        ),
    ],
    "scripts/preflight_email_domain.py": [
        (
            "        backend/.venv/bin/python scripts/preflight_email_domain.py toeshee.com\n",
            "        backend/.venv/bin/python scripts/preflight_email_domain.py novieri.com\n",
        ),
        (
            "direction that matters: `LIKE '%toeshee.com'` says `eviltoeshee.com` is fine\n",
            "direction that matters: `LIKE '%novieri.com'` says `evilnovieri.com` is fine\n",
        ),
        (
            '            "the base domain you are about to configure, e.g. toeshee.com. "\n',
            '            "the base domain you are about to configure, e.g. novieri.com. "\n',
        ),
    ],
    "README.md": [
        ("# ITMS\n\n**ITMS** — the IT Management System. A modular,",
         "# Novieri RMM\n\n**Novieri RMM** — remote monitoring and management. A modular,"),
    ],
    "docs/product.md": [
        ("# Product — what ITMS is, and what it does\n",
         "# Product — what Novieri RMM is, and what it does\n"),
        ("ITMS is a self-hosted IT management platform. You install one\n",
         "Novieri RMM is a self-hosted IT management platform. You install one\n"),
        (
            """**`ITMS` IS A PLACEHOLDER. The product name has not been chosen.** Confirmed
by the operator on 2026-08-21, when it was the visible name in 46 places
across 18 frontend source files, in three files of the sign-in theme (which
went with Keycloak in own authentication phase 4), and in `<title>`. Nothing
may harden an identity around it — no wordmark built from those four letters,
no logo whose meaning depends on them, no marketing surface that treats it as
settled. When the name is chosen it is a rename, and the count above was the
size of that job then.
""",
            """**The product is `Novieri RMM`, named by the operator on 2026-11-23**
(decision A3; `docs/superpowers/specs/2026-11-23-vps-y-marca-novieri-design.md`
§1): «Novieri RMM» in titles, in mail and on a client's computer, «Novieri» in
the console's bar. Until then `ITMS` was a placeholder, confirmed by the
operator on 2026-08-21, when it was the visible name in 46 places across 18
frontend source files, in three files of the sign-in theme (which went with
Keycloak in own authentication phase 4), and in `<title>`. The backend, the
agent and `deploy/` were renamed by the plan of 2026-11-23, and
`backend/tests/test_no_old_brand_names.py` keeps them so; the console's own
strings are the console redesign's (project 3).
""",
        ),
    ],
}

for rel, pairs in EDITS.items():
    path = Path(rel)
    text = path.read_text()
    for old, new in pairs:
        if text.count(old) != 1:
            sys.exit(f"{rel}: not there exactly once: {old[:70]!r}")
        text = text.replace(old, new)
    path.write_text(text)
    print(f"rewrote {rel}")
PY
```

Expected: cuatro líneas `rewrote …` (`config.py`, `preflight_email_domain.py`, `README.md`, `product.md`).

- [ ] **Step 4: Las pruebas pasan**

Run, desde `backend/`:

```bash
.venv/bin/pytest -q tests/test_no_old_brand_names.py tests/test_docs_without_keycloak.py tests/test_auth_phase4_falsifiers.py tests/test_domain_gate_doors.py tests/test_domain_policy.py
.venv/bin/ruff check app tests ../scripts/preflight_email_domain.py
.venv/bin/ruff format --check tests/test_no_old_brand_names.py
.venv/bin/mypy app tests/test_no_old_brand_names.py
```

Expected: `115 passed` (medido sobre el árbol con las Tasks 1, 3, 4, 7, 8, 9 y 10), `All checks passed!`, `1 file already formatted`, `Success: no issues found`.

Y el host viejo, en lo que no es historia: `git grep -n 'itms\.toeshee\.com' -- . ':!docs/superpowers' ':!docs/design' ':!docs/reviews' ':!frontend' ':!HANDOFF.md' ':!docs/modules/console-auth/spec.md'`

Expected: dos líneas, las dos pruebas que lo nombran para negarlo: `backend/tests/test_deploy_acme.py:87` (un host que no es el de la consola) y `backend/tests/test_deploy_script_steps.py:901` (`assert "itms.toeshee.com" not in steps`).

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_no_old_brand_names.py backend/app/core/config.py scripts/preflight_email_domain.py README.md docs/product.md
git commit -m "test(brand): no old product name in what backend, agent and deploy show; README and product.md name Novieri RMM"
```

### Task 12: El ensayo del primer arranque completo, aquí

**Files:**
- Modify: `deploy/README.md` (el estado del runbook, el párrafo «⚠ **Rehearsed, not yet run on the VPS.**» que escribió la Task 9; y cada paso que el ensayo desmienta)
- Nada más del repositorio. El registro del ensayo (`$R/log.md`) y los dos guiones de apoyo no se guardan en él.

**Interfaces:**
- Consumes: el árbol fusionado de las oleadas 1 a 3 (Tasks 1 a 10). De la Task 10, `deploy/ci/agent_flow.py`; de la Task 7, `deploy/ci/mcp_flow.py` y `deploy/ci/acme-pebble.yml`.
- Produces: el estado del runbook con lo que corrió y lo que imprimió, fechado; la Task 13 lo cita, y la 11 vuelve a pasar su guarda sobre el README que deje esta.

**Decisiones de la tarea.**
- **Qué es el ensayo (diseño §4):** cada bloque `sh` de «First boot», en orden, desde `deploy/` de una copia en `/opt/it-suite` (la ruta del runbook), en esta sesión, con Docker y con Pebble en el lugar de Let's Encrypt; luego «After a reboot or a restart» y `./scripts/deploy.sh --plan`. Lo que imprime cada bloque va a `$R/log.md` junto al bloque. Lo que el runbook dice que se verá se compara con lo que se vio: **una diferencia es un hallazgo**, y se corrige el runbook (o el guion, si el que está mal es el guion) en la rama de la tarea, con su commit y su línea en el registro.
- **Las únicas sustituciones permitidas** (todo lo demás, tal cual; cada una se anota en el registro donde se aplica):
  - **D1 — Pebble por Let's Encrypt:** `COMPOSE_FILE=docker-compose.yml:ci/acme-pebble.yml` y `PEBBLE_MINICA` en el entorno de cada comando, y en el paso 1 `--acme-ca-server https://pebble:14000/dir`.
  - **D2 — el registro `A`:** `192.0.2.2` en `lo` y `192.0.2.2 rmm.novieri.com` en `/etc/hosts`. El validador de Pebble no lo usa: `pebble-challtestsrv` le contesta con traefik.
  - **D3 — el correo de ACME** por `--acme-email rehearsal@novieri.example`: sin terminal, `new-env.sh` no pregunta (`[ -t 0 ] || die`).
  - **D4 — lo que el operador teclea o pega** llega por *stdin* al mismo comando: las tres partes en 3.3 (`bao write sys/unseal key=-`, la forma que usa la CI), el token raíz en 3.4 (el `sh` interactivo de `openbao` pasa a `sh -c` con los mismos comandos, y los dos `token create` con `-field=token` para leerlos), el token `gateway-issuer` en el `tee` de 3.4, los dos `read -rsp` de 3.5 y el de 11c. `stty` sin terminal se queja y el bloque sigue: se anota.
  - **D5 — la raíz de confianza del `:443`:** `check-public-tls.sh --cafile "$R/pebble-root.pem"` en el paso 7 y en «After a reboot», y en 11c el `-v` del almacén de raíces monta `"$R/ca-bundle.crt"` (el del sistema más la raíz de Pebble) en lugar de `/etc/ssl/certs/ca-certificates.crt`. Los `curl` de 11a y de «After a reboot» llevan `--cacert "$R/pebble-root.pem"`.
  - **D6 — la consola** (pasos 8, 9, 11b y la parte de consola de 11d): `rehearse_console.py`, abajo, que envía las mismas peticiones que las páginas de la consola, por traefik.
  - **D7 — `systemd`** no corre en este contenedor: en el paso 12, `ITS_NO_SYSTEMCTL=1 ./scripts/install-backup-timer.sh` y, en lugar de `systemctl start`, el `ExecStart` del servicio (`./scripts/scheduled-backup.sh`); `journalctl` no se corre.
  - **D8 — `sudo`:** la sesión es `root`; `sudo` se deja en el comando (existe y no pide nada).
- **No se ensaya**, y el estado del runbook lo dice: P1, P2 (ufw), P3, P5 y P6 de los requisitos (la cuenta, el cortafuegos, las actualizaciones, la instalación de Docker y la clave de despliegue son del VPS), el DNS y Let's Encrypt reales, el `git pull` de la actualización (esta copia no tiene remoto), y el temporizador de `systemd`. P4 (`timedatectl`) se corre y se anota lo que diga.

- [ ] **Step 0: La worktree y el host del ensayo**

Run: `git log --oneline -1`; fusionar la rama del coordinador (oleadas 1 a 3); la preparación de Global Constraints con Docker y las tres imágenes base de la sesión; `ss -ltn | grep -E ':(443|8443) '` no imprime nada.

```bash
S=/tmp/claude-0/-home-user-Novieri-RMM/21768af2-984e-5cf8-87b2-4dc8a41a4864/scratchpad
R="$S/rehearsal" && mkdir -p "$R"
git worktree add --detach /opt/it-suite HEAD
ip addr add 192.0.2.2/32 dev lo 2>/dev/null || true
grep -q ' rmm.novieri.com$' /etc/hosts || echo '192.0.2.2 rmm.novieri.com' >> /etc/hosts
getent ahosts rmm.novieri.com | head -1
cid="$(docker create ghcr.io/letsencrypt/pebble:2.10.1)" && docker cp "$cid:/test/certs/pebble.minica.pem" "$R/pebble-minica.pem" && docker rm "$cid" >/dev/null
cat > "$R/env.sh" <<EOF
export COMPOSE_FILE=docker-compose.yml:ci/acme-pebble.yml PEBBLE_MINICA=$R/pebble-minica.pem R=$R
cd /opt/it-suite/deploy
EOF
printf '# Rehearsal of deploy/README.md "First boot", %s, commit %s\n\n' "$(date -u +%F)" "$(git rev-parse --short HEAD)" > "$R/log.md"
```

Expected: `192.0.2.2       STREAM rmm.novieri.com`. Cada comando que sigue empieza por `source "$R/env.sh"` (las variables no pasan de una llamada a otra), con `R` puesto antes: `R=/tmp/claude-0/-home-user-Novieri-RMM/21768af2-984e-5cf8-87b2-4dc8a41a4864/scratchpad/rehearsal`.

`$R/rehearse_console.py` (D6):

```python
"""The console's part of the first boot (deploy/README.md steps 8, 9, 11b and
11d), for the rehearsal of plan 2026-11-23, Task 12: the same requests the
console's pages send, through Traefik on :443, with no browser. Not committed.

    python3 rehearse_console.py --checkout <repo> --cafile <pebble root> claim --setup-code-file F --state-file S
    python3 rehearse_console.py --checkout <repo> --cafile <pebble root> token --state-file S --token-file T
    python3 rehearse_console.py --checkout <repo> --cafile <pebble root> job --state-file S --hostname H

`claim` is steps 8 and 9: the setup screen (the install code, a name, an
address, the organization's name, a password) and the first sign-in with a
TOTP factor; it keeps the address, the password and three recovery codes in
a new 0600 file. `token` is 11b, `job` is 11d's **Run job → Collect
inventory**. It prints what happened, never a secret.
"""

import argparse
import secrets
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--cafile", required=True)
    parser.add_argument("--base", default="https://rmm.novieri.com")
    sub = parser.add_subparsers(dest="what", required=True)
    claim = sub.add_parser("claim")
    claim.add_argument("--setup-code-file", required=True)
    claim.add_argument("--state-file", required=True)
    token = sub.add_parser("token")
    token.add_argument("--state-file", required=True)
    token.add_argument("--token-file", required=True)
    job = sub.add_parser("job")
    job.add_argument("--state-file", required=True)
    job.add_argument("--hostname", required=True)
    args = parser.parse_args()

    sys.path.insert(0, str(Path(args.checkout) / "deploy" / "ci"))
    import agent_flow  # noqa: PLC0415
    import mcp_flow  # noqa: PLC0415

    client = mcp_flow.Client(args.base, args.cafile)
    try:
        if args.what == "claim":
            code = Path(args.setup_code_file).read_text().strip()
            status = client.request("GET", "/api/v1/setup/status")
            mcp_flow.check(status.json() == {"needed": True}, "setup is not needed")
            email = "owner@rehearsal.invalid"
            password = "rehearsal-" + secrets.token_urlsafe(18)
            made = client.request(
                "POST",
                "/api/v1/setup/claim",
                body={
                    "code": code,
                    "email": email,
                    "display_name": "Rehearsal Owner",
                    "organization_name": "Novieri",
                    "password": password,
                },
                console=True,
            )
            mcp_flow.check(made.status == 201, f"the claim answered {made.status}")
            mcp_flow.ok("step 8: claimed with the install code")
            login = client.request(
                "POST", "/api/v1/auth/login", body={"email": email, "password": password}, console=True
            )
            mcp_flow.check(login.json() == {"state": "enroll_required"}, "not enroll_required")
            start = client.request("POST", "/api/v1/auth/enroll/totp/start", console=True)
            secret = mcp_flow.totp_secret(start.json()["otpauth_uri"])
            confirm = client.request(
                "POST",
                "/api/v1/auth/enroll/totp/confirm",
                body={"code": mcp_flow.totp(secret)},
                console=True,
            )
            mcp_flow.check(confirm.status == 204, f"TOTP confirm answered {confirm.status}")
            finish = client.request("POST", "/api/v1/auth/enroll/finish", console=True)
            codes = finish.json()["recovery_codes"]
            mcp_flow.check(len(codes) == 10, f"{len(codes)} recovery codes, not ten")
            mcp_flow.write_owner_state(args.state_file, email, password, list(codes)[:3])
            mcp_flow.ok("step 9: signed in, a TOTP enrolled, ten recovery codes shown")
        elif args.what == "token":
            flow = agent_flow.AgentFlow(client, agent_flow.read_owner(args.state_file))
            flow.token(args.token_file)
            mcp_flow.ok("step 11b: an enrollment token for the Principal site, one use")
        else:
            flow = agent_flow.AgentFlow(client, agent_flow.read_owner(args.state_file))
            flow.sign_in(1)
            device_id = flow.device(args.hostname, 120.0)
            made = client.request(
                "POST",
                "/api/v1/jobs",
                body={"type": "inventory.collect", "device_id": device_id, "params": {}},
                console=True,
            )
            mcp_flow.check(made.status == 201, f"POST /api/v1/jobs answered {made.status}")
            body = made.json()
            deadline = time.monotonic() + 120
            while body["status"] not in agent_flow.TERMINAL and time.monotonic() < deadline:
                time.sleep(2)
                body = flow.get(f"/api/v1/jobs/{body['id']}")
            mcp_flow.check(body["status"] == "succeeded", f"the job ended {body['status']}")
            mcp_flow.ok("step 11d: Run job -> Collect inventory: succeeded")
    except mcp_flow.Failed as exc:
        print(f"FAIL  {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 1: Los pasos 1 a 1c**

Lo que corre (cada uno con su salida al registro, `… 2>&1 | tee -a "$R/log.md"`):

1. P4: `timedatectl status | grep -E 'System clock synchronized|NTP service'`.
2. Paso 1 con D1 y D3: `./scripts/new-env.sh --host rmm.novieri.com --acme-email rehearsal@novieri.example --acme-ca-server https://pebble:14000/dir`; después, tal cual, `stat -c %a .env` y `grep -c '=change-me$' .env`.
3. Paso 1b, tal cual: los dos comandos del bloque. `build-images.sh` termina con `Recent tags:` y una sola etiqueta: se anota, es la prueba de lo que el runbook dice de la vuelta atrás.
4. Paso 1c, tal cual: `./scripts/preflight.sh`.

Expected: lo que muestra el runbook en cada uno, con `ACME CA: Pebble` (o lo que `preflight.sh` diga de un directorio que no es de Let's Encrypt) en lugar de `Let's Encrypt production`, y `rmm.novieri.com resolves to 192.0.2.2, an address of this host`. Ningún `REFUSE`.

- [ ] **Step 2: Los pasos 2 a 6**

Tal cual, salvo D4 en 3.3, 3.4, 3.5 y el `tee` del token del gateway:

```bash
source "$R/env.sh"
# 3.2: la salida va a un archivo 0600 del ensayo, no al registro (las partes y el token raíz).
(umask 077 && docker compose exec -T openbao bao operator init -key-shares=5 -key-threshold=3 > "$R/bao-init.txt")
grep -c '^Unseal Key' "$R/bao-init.txt"
# 3.3, D4: tres de las cinco partes, por stdin.
for n in 1 2 3; do sed -n "s/^Unseal Key $n: //p" "$R/bao-init.txt" | tr -d '\n' | docker compose exec -T openbao bao write sys/unseal key=- >/dev/null; done
docker compose exec openbao bao status
# 3.4, D4: los comandos del bloque, con el token raíz por stdin.
root="$(sed -n 's/^Initial Root Token: //p' "$R/bao-init.txt")"
docker compose exec -T openbao sh -c 'read -r BAO_TOKEN && export BAO_TOKEN && bao policy write its-app /bao/its-app.hcl && bao policy write gateway-issuer /bao/gateway-issuer.hcl && bao auth tune -max-lease-ttl=87600h token/' <<<"$root"
(umask 077 && docker compose exec -T openbao sh -c 'read -r BAO_TOKEN && export BAO_TOKEN && bao token create -orphan -policy=its-app -ttl=87600h -field=token' <<<"$root" > "$R/its-app.token")
(umask 077 && docker compose exec -T openbao sh -c 'read -r BAO_TOKEN && export BAO_TOKEN && bao token create -orphan -policy=gateway-issuer -ttl=87600h -field=token' <<<"$root" > "$R/gateway-issuer.token")
```

Y después, del runbook: el bloque de `/etc/it-suite/gateway-issuer.token` con `< "$R/gateway-issuer.token"` en su `tee`; los dos bloques de 3.5 con `< "$R/its-app.token"` y con `<<<"$root"` (en la misma llamada, `root` vuelve a leerse de `$R/bao-init.txt` como arriba); los de comprobación de 3.5 y 3.6 tal cual; el paso 4, el 5 y el paso 6 en su forma no interactiva (`sudo cat /etc/it-suite/gateway-issuer.token | …`), tal cual.

Expected: lo que muestra el runbook en cada bloque; `5` partes; `Sealed false`; en el paso 6, `gateway.crt … (CN=rmm.novieri.com, 365d)`.

- [ ] **Step 3: El paso 7**

`docker compose up -d` y la tabla, tal cual; `./scripts/check-public-tls.sh --cafile "$R/pebble-root.pem"` (D5) después de leer la raíz de Pebble como la CI:

```bash
source "$R/env.sh"
for _ in $(seq 1 30); do curl -sf --noproxy '*' --cacert "$PEBBLE_MINICA" --resolve pebble:15000:172.19.240.252 https://pebble:15000/roots/0 -o "$R/pebble-root.pem" && break; sleep 2; done
cat /etc/ssl/certs/ca-certificates.crt "$R/pebble-root.pem" > "$R/ca-bundle.crt"
```

y los dos bloques que siguen (el `:8443` y los límites), tal cual.

Expected: la tabla del runbook, con dos filas más (`challtestsrv` y `pebble`, de D1); `ok      127.0.0.1:443 presents a trusted certificate for rmm.novieri.com` con `issuer=CN = Pebble Intermediate CA …`; en el `:8443`, `issuer=CN = Novieri RMM internal CA` y `DNS:rmm.novieri.com`; `30 30`.

- [ ] **Step 4: Los pasos 8 a 11 y el 12**

```bash
source "$R/env.sh"
curl -s --cacert "$R/pebble-root.pem" -o /dev/null -w '%{http_code}\n' https://rmm.novieri.com/api/v1/setup/status
docker compose exec api cat /var/lib/its/secrets/setup-code > "$R/setup-code"
python3 "$R/rehearse_console.py" --checkout /opt/it-suite --cafile "$R/pebble-root.pem" claim --setup-code-file "$R/setup-code" --state-file "$R/owner.json"
```

(El segundo comando es el del paso 8, con su salida a un archivo del ensayo en lugar de la pantalla.) Después, tal cual: el bloque del paso 10; el de 11a con `--cacert "$R/pebble-root.pem"` (D5); 11b por `rehearse_console.py … token --state-file "$R/owner.json" --token-file "$R/enroll-token"`; los tres bloques de 11c, el segundo con `< "$R/enroll-token"` y el tercero con D5; 11d por `rehearse_console.py … job --state-file "$R/owner.json" --hostname "$h"`, con `h="$(docker logs smoke-agent 2>&1 | sed -n 's/.*msg=enrolling .*hostname=\([^ ]*\).*/\1/p' | head -1)"` (el nombre con que el agente se inscribió), y su `psql` tal cual; 11e y 11f tal cual; el paso 12 con D7:

```bash
source "$R/env.sh"
sudo ITS_NO_SYSTEMCTL=1 ./scripts/install-backup-timer.sh
sudo ./scripts/scheduled-backup.sh
sudo cat /var/lib/it-suite/backup/last-success
```

Expected: `200`; `ok    step 8: claimed with the install code` y `ok    step 9: signed in, a TOTP enrolled, ten recovery codes shown`; en el paso 10, `owner@rehearsal.invalid|org-admin`; los cuatro `401` de 11a; en 11c, las cuatro líneas del runbook (`novieri-agent starting`, `enrolling`, `enrolled`, `gateway connected`) y `ls /tmp/smoke-agent` sin `enroll-token`; en 11d, `ok    step 11d: Run job -> Collect inventory: succeeded` e `inventory.collect|succeeded|t`; en 11e, `novieri {"ok": true, "count": …, "broken_at": null}`; en el paso 12, la fecha de la copia.

- [ ] **Step 5: Después de un reinicio, y la actualización**

Un reinicio de OpenBao lo deja sellado, como el de un host:

```bash
source "$R/env.sh"
docker compose restart openbao
```

Y la sección «After a reboot or a restart», bloque a bloque: `bao status` (tiene que decir `Sealed true`), las tres partes con D4 (el bucle del Step 2), `bao status` (`Sealed false`), y el último bloque con D5 en `check-public-tls.sh` y en el `curl`. Después, de la actualización en este VPS, `docker compose exec openbao bao status` y `./scripts/deploy.sh --plan`, tal cual (el `git -C .. pull --ff-only` no se corre: no hay remoto).

Y el 503: `docker compose stop api`, el `curl` del último bloque (con D5) hasta que diga `503`, y `docker compose start api`.

Expected: lo que dice el runbook; el `curl` del final, `401`; `deploy.sh --plan` dice qué haría sin hacerlo.

- [ ] **Step 6: El runbook dice lo que pasó**

En `deploy/README.md`, el párrafo «⚠ **Rehearsed, not yet run on the VPS.** …» pasa a decir, fechado: que el primer arranque de este runbook se ejecutó entero en el ensayo de la Task 12 del plan de 2026-11-23 (`rehearse_console.py` en el lugar de la consola; Pebble en el de Let's Encrypt), con la lista D1–D8 de lo sustituido, lo que no se ensayó (la lista de las decisiones de esta tarea) y, para cada paso, si lo que imprimió fue lo que el runbook muestra. Cada salida que el ensayo desmintió ya está corregida en su paso (Steps 1 a 5). Luego:

```bash
cd backend && .venv/bin/pytest -q tests/test_runbook_first_boot.py tests/test_runbook_without_keycloak.py tests/test_deploy_runbook_secrets.py tests/test_email_domain_runbook.py tests/test_deploy_env_documentation.py tests/test_auth_phase4_falsifiers.py tests/test_no_old_brand_names.py
```

Expected: todo pasa.

- [ ] **Step 7: Recoger, y commit**

```bash
source "$R/env.sh" && docker compose down -v && docker rm -f smoke-agent 2>/dev/null; cd -
git worktree remove --force /opt/it-suite
sed -i '/ rmm.novieri.com$/d' /etc/hosts && ip addr del 192.0.2.2/32 dev lo
rm -f "$R"/bao-init.txt "$R"/*.token "$R"/owner.json "$R"/setup-code "$R"/enroll-token
git add deploy/README.md
git commit -m "docs(deploy): what the first-boot rehearsal of 2026-11-23 ran and printed"
```

El informe de la tarea adjunta `$R/log.md` (sin secretos: las partes, los tokens y los códigos fueron a archivos que este paso borra) y la lista de hallazgos con su commit.

### Task 13: El informe del proyecto y del programa

**Files:**
- Ninguno del repositorio. El informe va al coordinador, que lo lleva al operador (`CLAUDE.md`, «Reporting to the operator»; diseño §7).

**Interfaces:**
- Consumes: la rama del proyecto con las Tasks 1 a 12 fusionadas, en el orden de las oleadas; su `deploy/README.md` (el de la Task 9 con lo que corrigió la 12) es la única fuente de los comandos del informe.
- Produces: el informe, en español, con las seis partes del Step 3.

**Decisiones de la tarea.**
- **Los comandos del informe salen del runbook, no se reescriben**: `report_sequence.py` (abajo) lee cada bloque `sh` de los requisitos, del primer arranque, de «After a reboot or a restart» y de la actualización en este VPS, en orden, con la salida que el runbook muestra detrás de cada uno. Así el informe y el runbook no pueden decir cosas distintas, y lo que la Task 12 corrigió llega al informe solo.
- **«Toda secuencia empieza por `bao status`» en un host nuevo.** En el primer arranque no hay OpenBao hasta el paso 3.1, que lo arranca y pregunta `bao status` a continuación; un `docker compose exec openbao bao status` antes de eso falla porque el contenedor no existe. El informe lo dice así, en lugar de abrir la secuencia con un comando que falla. Las secuencias de después de un reinicio y de una actualización sí empiezan por él, y la prueba de la Task 9 lo fija.
- **Lo que solo hace el operador es un paso numerado propio** (`CLAUDE.md`: lo que hay que sustituir no puede confundirse con algo que se pega): el «Paso 0», antes de la secuencia, y en la secuencia cada paso de consola marcado como suyo.

- [ ] **Step 1: El árbol fusionado**

Worktree nueva desde la rama del coordinador con las Tasks 1–12. Comprobar: `git log --oneline -1`; `grep -c '## After a reboot or a restart' deploy/README.md` imprime `1`; `cd backend && .venv/bin/pytest -q tests/test_runbook_first_boot.py tests/test_no_old_brand_names.py tests/test_ci_agent_flow.py` pasa.

- [ ] **Step 2: Las secuencias, del runbook**

`$S/report_sequence.py`:

```python
"""Plan 2026-11-23, Task 13: the report's copy-paste sequences, read out of
deploy/README.md as it is on the merged branch, so the report and the runbook
cannot say different things. Not committed.

    python3 report_sequence.py deploy/README.md > sequence.md

Every `sh` block of "Prerequisites: a new VPS", "First boot", "After a reboot
or a restart" and the VPS upgrade, in order, under its heading, each followed
by the output the runbook shows right after it (the check). A heading with no
`sh` block is a step done in the console or by hand: it is listed, with the
runbook's first paragraph, as the operator's.
"""

import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text()


def section(heading: str, stop: str = "\n## ") -> str:
    start = text.index(heading)
    end = text.find(stop, start + len(heading))
    return text[start:] if end == -1 else text[start:end]


def steps(body: str) -> list[tuple[str, str]]:
    """(heading, body) for each ###/#### heading of `body`, but a ### heading
    whose own text runs no command and is followed by its #### parts: that
    one is an introduction, and its parts are the steps."""
    parts = re.split(r"(?m)^(#{3,4} .+)$", body)
    found = []
    for i in range(1, len(parts), 2):
        heading, own = parts[i], parts[i + 1]
        nxt = parts[i + 2] if i + 2 < len(parts) else ""
        if heading.startswith("### ") and nxt.startswith("#### ") and "```sh" not in own:
            continue
        found.append((heading.lstrip("# ").strip(), own))
    return found


FENCE = re.compile(r"```(sh|text|json)?\n(.*?)```", re.DOTALL)


def render(title: str, body: str, out: list[str], n: list[int]) -> None:
    fences = list(FENCE.finditer(body))
    runnable = [f for f in fences if f.group(1) == "sh"]
    n[0] += 1
    out.append(f"**{n[0]}. {title}**\n")
    if not runnable:
        first = next((p.strip() for p in body.strip().split("\n\n") if p.strip()), "")
        out.append("Lo hace el operador (consola o a mano): " + " ".join(first.split()) + "\n")
        return
    for index, fence in enumerate(fences):
        if fence.group(1) != "sh":
            continue
        out.append("```sh\n" + fence.group(2) + "```\n")
        following = fences[index + 1] if index + 1 < len(fences) else None
        between = body[fence.end() : following.start()] if following else ""
        if following is not None and following.group(1) in (None, "json") and not between.strip():
            out.append("Comprobación — el runbook muestra:\n\n```\n" + following.group(2) + "```\n")


out: list[str] = []
n = [0]
out.append("### El primer arranque de rmm.novieri.com\n")
for title, body in steps(section("## Prerequisites: a new VPS (Ubuntu Server 24.04 LTS)\n")):
    render(title, body, out, n)
for title, body in steps(section("## First boot\n")):
    render(title, body, out, n)
out.append("\n### Después de un reinicio\n")
render("After a reboot or a restart", section("## After a reboot or a restart\n"), out, n)
upgrade = section("## Upgrading an existing deployment\n")
marker = "**On the VPS this runbook builds**"
block = FENCE.search(upgrade, upgrade.index(marker))
out.append("\n### Una actualización en este VPS\n")
out.append("```sh\n" + block.group(2) + "```\n")
print("\n".join(out))
```

Run: `python3 "$S/report_sequence.py" deploy/README.md > "$S/sequence.md" && grep -c '^```sh' "$S/sequence.md"`

Expected: un número cerca de 54 (el de la Task 9; la 12 puede haber añadido o partido algún bloque). Cada bloque de `$S/sequence.md` es un bloque del README, sin cambiar un carácter.

- [ ] **Step 3: El informe**

Al coordinador, en este orden:

1. **El camino hasta `main`.** Las ramas de las tareas se fusionan en la del proyecto en el orden de las oleadas (con las Tasks 3, 4 y 7 juntas, en el orden 4, 3, 7); la rama del proyecto va a `main` en una PR nueva (decisión del operador del 2026-11-23: «P3/P4 en PR nueva»), con la CI en verde, el job `deploy` incluido (los pasos de las Tasks 7 y 10) y los tres del agente; **la fusiona el operador**. El operador despliega siempre desde `main` (2026-09-11), y nada se despliega hasta que estén los cuatro proyectos (2026-10-31): si el proyecto 3 (la consola) no está en `main`, el despliegue espera. La copia del VPS se clona de `main` en el paso P6 del runbook, y el `git -C .. rev-parse --abbrev-ref HEAD` de ese paso tiene que imprimir `main`.
2. **Paso 0 — lo que solo puede hacer el operador**, numerado:
   1. Contratar el VPS: Ubuntu Server 24.04 LTS, 4 vCPU, 8 GB, 100 GB, una IPv4 pública en su interfaz.
   2. En el DNS de `novieri.com`: un registro `A` `rmm.novieri.com` → esa IPv4, y **ningún `AAAA`**; esperar a que desde otro equipo `getent ahostsv4 rmm.novieri.com` devuelva esa dirección antes del paso 7.
   3. En el cortafuegos del proveedor, si lo tiene: entrada 22, 443 y 8443, nada más.
   4. En GitHub, la clave de despliegue de solo lectura que imprime el paso P6.
   5. En el paso 1, la dirección de contacto de Let's Encrypt, en la pregunta de `new-env.sh`.
   6. En el paso 3.2, la custodia de las cinco partes de OpenBao y del token raíz, fuera del VPS, en manos distintas; y en el paso 12, la de `.env` y los volúmenes de clave (GO-LIVE §6).
   7. En la consola: el paso 8 (abrir `https://rmm.novieri.com/` y escribir el código), el 9 (la contraseña, el segundo factor y los diez códigos de recuperación, guardados con las partes de OpenBao), el 11b (el token de inscripción) y el 11d (**Run job → Collect inventory**).
   8. Después del primer arranque, si se quiere: la app de Entra (`deploy/README.md`, «Sign in with Microsoft») y el relé SMTP (**Settings → Email**).
   9. Cuando el nuevo funcione: apagar `itms.toeshee.com` (B1). Nada se migra desde él (P7).
3. **La secuencia completa del primer arranque**, el contenido de `$S/sequence.md` hasta «Después de un reinicio», copiable, en orden, sin marcadores, con la comprobación de cada paso; con la nota de la decisión de arriba sobre `bao status` en un host nuevo, y con lo que el CLAUDE.md pide que se diga aunque no haga nada: **este proyecto no añade ninguna migración**, y aun así `migrate` corre como dependencia de `api` en cada `up -d` (en un host nuevo, todas desde `0001`); **las claves de `.env` nuevas** (`ITS_PUBLIC_HOST`, `ITS_ACME_EMAIL`, `ITS_ACME_CA_SERVER`) las escribe `new-env.sh`, y en un host nuevo todas las claves son nuevas.
4. **Después de un reinicio y una actualización**, las dos de `$S/sequence.md`, las dos empezando por `docker compose exec openbao bao status`, con lo que significa `Sealed true` (las tres partes, de tres custodios) y que un OpenBao sellado hace que `up -d` se quede esperando (GO-LIVE §4).
5. **La vuelta atrás, como es:** en este host no hay etiqueta anterior. `build-images.sh` lista una sola, la que corre; hasta la primera copia del paso 12 no hay vuelta atrás ninguna (empezar de nuevo es `docker compose down -v`, borrar `.env` y otro primer arranque, que pide otro certificado: 5 por semana). Desde el paso 12, la vuelta atrás es restaurar esa copia (`docs/07-backup-restore.md` §3). En cada actualización posterior, `deploy.sh` termina diciendo la suya: la etiqueta anterior si las migraciones lo permiten, o la copia que tomó.
6. **Lo verificado y lo no:**
   - verificado en las sesiones del plan y de las tareas: las pruebas de cada tarea en rojo y en verde; el job `deploy` de la CI entero en esta sesión con Pebble (el certificado por ACME, el 503 con la API parada, el agente compilado inscrito por el 443, conectado por el 8443 y con un `hello` firmado, verificado y contestado; la cadena de auditoría verificada); y el ensayo del primer arranque de la Task 12, con su lista de sustituciones y de hallazgos;
   - **sin verificar**, y el operador lo verá por primera vez en el VPS: el VPS, el DNS real, Let's Encrypt de producción, ufw, el temporizador de `systemd` de la copia, la app de Entra, el SMTP y cualquier equipo Windows o macOS;
   - la vida de los certificados de Let's Encrypt baja a 64 días el 2027-02-10 y a 45 el 2028-02-16 [E6]; Traefik renueva cuando quedan 30 días [E1][E16], así que no hace falta tocar nada, pero conviene mirar en febrero de 2027 que la primera renovación de 64 días ocurrió (`check-public-tls.sh` imprime `notAfter`);
   - las imágenes base «de la sesión» de Global Constraints llevan la CA del proxy de la sesión y no salieron de ella.

---

## Apéndice A — Contratos que una tarea fija y otra consume

| Lo fija | Contrato | Lo consumen |
|---|---|---|
| Task 1 | `notices.PRODUCT == "Novieri RMM"`; `serverInfo.name == "novieri-rmm"`; `FastAPI(title="Novieri RMM API")`; la CN `Novieri RMM internal CA` (`deploy/openbao/init.sh`, `app/core/ca.py`) | Task 8 (es.py después de la 1), Task 9 (la salida del `:8443` en el paso 7), Task 11 (la guarda) |
| Task 2 | `tests.redis_support.TEST_REDIS_URL`; `unique_version(major=2) -> "2.<n % 10**12>.0"` | la suite entera |
| Task 3 | el resolutor `le`; `${ITS_PUBLIC_HOST:?…}` y `${ITS_ACME_EMAIL:?…}` en compose; el volumen `traefik-acme`; el router de fichero `api-unavailable` (prioridad 2, 503); `tests/traefik_routes.py` (`PUBLIC_HOST`, `routers()`, `matches(..., host=)`, `winner(..., host=, down=())`) | Tasks 4, 7, 9 y 12 |
| Task 4 | `new-env.sh --host … [--acme-email …] [--acme-ca-server …]`; `check-public-tls.sh [--connect] [--cafile] [--wait] [--port]` y su línea `ok      <ip>:<puerto> presents a trusted certificate for <host>`; las líneas nuevas de `preflight.sh`; las tres claves nuevas de `.env.example` y `ITS_DEFAULT_ORG_SLUG=novieri` | Tasks 7, 9, 10, 12 y 13 |
| Task 5 | `action_tokens.start_clock(session, token, ttl)`; `metadata.grant = "already_ended"` en `mcp.code_reused` y `mcp.refresh_reused`; `MailBox.delay_s` | — |
| Task 6 | `waitResults`, `untilTestEnds`; `awaitResults(t, ids)` y `awaitReconnect(t, after)` sin duración | — |
| Task 7 | `deploy/ci/acme-pebble.yml` (traefik `.250`, challtestsrv `.251`, pebble `.252`; `PEBBLE_MINICA`); `mcp_flow.py --resolve` y `--state-file` (`{"email", "password", "recovery_codes": [dos]}`, 0600); `pebble_root`, `get`, `wait_for_api`, `flow` y el `trap` en el paso del OAuth | Task 10 (dentro del mismo paso), Task 12 |
| Task 8 | `novieri-agent` (binario, servicio, `-enroll`, artefactos `novieri-agent-<os>-<arch>`); `/var/lib/novieri-agent`; `%ProgramData%\NovieriRMM\agent`; `FilePrefix = "NovieriRMM-Agent-Setup_"`; `UPGRADE_NAMESPACE = 8814cc17-6469-4df7-93f9-0f75ecdbb6a9` | Tasks 9, 10, 11, 12; el proyecto 3 (Apéndice F) |
| Task 9 | `## Prerequisites: a new VPS (Ubuntu Server 24.04 LTS)`, `## After a reboot or a restart`, el párrafo `**On the VPS this runbook builds**` | Tasks 11, 12 y 13 |
| Task 10 | `agent_flow.py --phase token|job`; `chain_order`, `trail_in_order`, `principal_site`, `write_secret`, `read_owner` | Task 12 (`rehearse_console.py` la importa) |
| Task 11 | `test_no_old_brand_names.py` (`OLD`, `KEPT`, los extractores) | Task 12 la vuelve a pasar |
| Task 12 | el estado del runbook, con lo que el ensayo imprimió | Task 13 |

---

## Apéndice B — Decisiones

**Del operador** (`.git/sdd/decisiones-p34.md`, 2026-11-23): **B1** VPS nuevo desde cero, Ubuntu 24.04 + Docker CE; el viejo se apaga después. **B2** el host es `rmm.novieri.com`. **B3** HTTPS por Let's Encrypt automático (Traefik ACME, TLS-ALPN-01 en el 443; el 8443 sigue con la CA interna). **B4** se renombra lo visible y lo de los equipos, no el prefijo `ITS_`, las imágenes, el módulo Go ni el protocolo con el agente. **A3** el nombre es «Novieri RMM» (barra de la consola: «Novieri»). **P7** (diseño §8): nada se migra desde `itms.toeshee.com`. Y, de antes: el operador despliega desde `main` (2026-09-11); nada se despliega hasta terminar los cuatro proyectos (2026-10-31); los frontend de los nombres son del proyecto 3 (resolución del coordinador del 2026-11-24, «P3 owns the frontend edits; reconcile with P4 plan Appendix F»).

**De este plan:**

| # | Decisión | Por qué | Dónde |
|---|---|---|---|
| P4-B-01 | Pebble solo en la CI y en el ensayo, nunca en un host; la confianza de Traefik en Pebble por `LEGO_CA_CERTIFICATES`, una variable de entorno del archivo de compose de la CI | la configuración estática de Traefik no se reparte entre CLI y entorno [E2]; así la lista `command:` de producción no se repite y no puede divergir | Tasks 3 y 7 |
| P4-B-02 | TLS-ALPN-01 en el 443; el 80 sigue cerrado | el reto se contesta en el 443 [E1]; abrir el 80 sería un puerto más sin otro uso | Task 3 |
| P4-B-03 | el 503 de la API caída es un router de fichero (`api-unavailable`, prioridad 2, sin servidores) que cubre todas las rutas de `api`, no solo `/api` y `/mcp` | el proveedor Docker no crea servidores para un contenedor no sano [E15]; sin él, la ruta caía en el *catch-all* de `web` (diseño §6). Cubre las 11 rutas de `api` | Task 3 |
| P4-B-04 | `new-env.sh` deja los dos tokens de OpenBao en `change-me` | no existen hasta el paso 3.4; los escribe el 3.5 con `read -rsp`, sin marcadores | Task 4 |
| P4-B-05 | todo lo que cuelga del nombre del servicio cambia con él (`/var/lib`, `/etc`, `/var/log`, `/usr/share/doc`, `.novieri-agent.new`, el temporal de Windows) | §1.1 nombra el binario y el servicio; dejar sus rutas con el nombre viejo es la mitad de un cambio de nombre, y no hay flota que conservar (B1, P7) | Task 8 |
| P4-B-06 | un `UpgradeCode` nuevo fijo (espacio `8814cc17-…`) | §1.1: «un producto nuevo»; un MSI de `ITSuite` no se actualiza a este | Task 8 |
| P4-B-07 | `O = it-suite` de la CA local de desarrollo se queda | §1.1 nombra la CN; la O no la ve nadie en un despliegue con OpenBao | Task 1 |
| P4-B-08 | `ITMS_BIND_IP` e `ITMS_NETWORK_SUBNET` se quedan | la misma razón que el prefijo `ITS_` de §1.2: los leen el compose y los guiones, y no los ve nadie | Tasks 4 y 9 |
| P4-B-09 | la cabecera `X-Toeshee-Event-Id` de los *webhooks* se queda | es un contrato con quien los consume (`docs/modules/notification/spec.md:190,479`), como las de §1.2; Q2 | Task 1 |
| P4-B-10 | los identificadores que no son nombres se quedan: `__Host-itms_session`, el prefijo `itms_` de los `client_id` de MCP (guardados), `itms-local:`, `itms-rsp.1`, `urn:itms:loopback`, `itms-ci` | §1.2: datos guardados y protocolo; la guarda no lee `itms` en minúsculas | Tasks 1, 8 y 11 |

---

## Apéndice C — Pruebas, por tarea

| Task | Nuevas | Cambiadas | Rojo → verde (medido el 2026-11-23) |
|---|---|---|---|
| 1 | `test_brand_names_backend.py` | `test_i18n_modules_desk.py`, `test_notification_delivery.py`, `test_notification_email_language.py`, `test_platform_backup_status.py`, `test_mcp_server.py`, `test_realm_settings_api.py` (los nombres) | las seis nuevas y las de asunto, rojas → `688 passed, 1 skipped` |
| 2 | `test_suite_support.py`, `redis_support.py` | 43 archivos (la URL de Redis), `agent_update_support.py` | `6 failed` → las seis y `63 passed` |
| 3 | `test_deploy_acme.py` | `traefik_routes.py`, `test_public_surface.py`, `test_auth_phase4_falsifiers.py`, `test_deploy_object_storage_route.py` | → `65 passed` |
| 4 | `test_new_env_script.py`, `test_preflight_script.py`, `test_check_public_tls.py` | `test_deploy_script_steps.py` (una renombrada, una nueva), `test_deploy_install_link.py` | `48 failed, 42 passed` → `172 passed` |
| 5 | tres en `test_oauth_token.py`, una en `test_auth_password_reset.py`, una en `test_user_admin_api.py` | `auth_support.py` | `4 failed, 1 passed` → `209` y `132 passed` |
| 6 | `concurrency_wait_test.go` | `concurrency_harness_test.go` | `undefined: waitResults` → las cuatro `--- PASS`, `ok` |
| 7 | seis en `test_ci_mcp_flow.py` | tres aserciones de `test_ci_mcp_flow.py` | `8 failed, 43 passed` → `51 passed`; el job, rojo con el DNS de Pebble a otra máquina y verde |
| 8 | `test_agent_names_both_sides.py` | las pruebas del agente y del backend que fijan los nombres (por reglas) | `6 failed` → `6 passed`; `310 passed, 17 skipped`; `go test` 38 ok |
| 9 | `test_runbook_first_boot.py` | `test_runbook_without_keycloak.py` (el suelo de 7 a 6) | `7 failed` → `130 passed` |
| 10 | `test_ci_agent_flow.py` | `test_ci_mcp_flow.py` (el `trap`) | error de recolección → `64 passed`; el job, rojo sin gateway (`FAIL  no online device ci-agent after 120 s (agent_status=offline)`) y verde |
| 11 | `test_no_old_brand_names.py` | — | `745` nombres sobre `1bc0e5e` → `31 passed`; el nombre plantado en un literal de Go, rojo; en un comentario, verde |
| 12 | — | las del runbook, si el ensayo cambia texto | todo pasa tras el ensayo |

---

## Apéndice D — El inventario de nombres, con recuentos (en `1bc0e5e`)

Cada celda es «apariciones/archivos». `docs(history)` son los planes, diseños y revisiones fechados, que no se tocan. `frontend/src` es del proyecto 3 (Apéndice F).

| Zona | its-agent | release/v1 | launchd | ITSuite | /opt/toeshee | ITMS-Agent- | toeshee-agent/snap | host | CA CN | Toeshee | toeshee (otro) | ITMS |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `backend/app` | 112/13 | 1/1 | 7/4 | 12/4 | 0 | 5/3 | 0 | 3/2 | 1/1 | 116/12 | 12/3 | 50/15 |
| `agent(code)` | 107/33 | 1/1 | 10/5 | 48/15 | 4/2 | 3/2 | 5/2 | 0 | 0 | 6/5 | 2/1 | 11/9 |
| `agent/packaging` | 190/17 | 0 | 5/4 | 30/4 | 0 | 2/1 | 0 | 0 | 0 | 10/9 | 1/1 | 5/4 |
| `deploy` | 19/2 | 0 | 0 | 4/1 | 0 | 1/1 | 0 | 66/7 | 2/2 | 0 | 11/3 | 62/11 |
| `.github` | 42/3 | 0 | 0 | 4/1 | 0 | 0 | 0 | 0 | 0 | 1/1 | 0 | 6/1 |
| `scripts` | 4/1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 4/1 | 1/1 |
| `docs(current)` | 124/10 | 2/2 | 12/3 | 12/2 | 5/1 | 1/1 | 1/1 | 10/3 | 0 | 34/16 | 18/3 | 48/9 |
| `backend/tests` | 28/11 | 2/1 | 3/2 | 6/3 | 0 | 10/4 | 0 | 7/2 | 0 | 25/8 | 202/33 | 37/22 |
| `agent(test)` | 215/38 | 0 | 22/5 | 124/19 | 13/3 | 51/5 | 0 | 25/5 | 0 | 3/2 | 11/6 | 18/6 |
| `backend/migrations` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1/1 | 1/1 |
| `frontend/src` | 101/8 | 0 | 0 | 0 | 0 | 11/4 | 1/1 | 1/1 | 0 | 78/29 | 108/30 | 83/39 |
| `docs(history)` | 147/7 | 4/2 | 8/2 | 75/6 | 8/2 | 41/6 | 11/2 | 54/14 | 1/1 | 78/15 | 133/10 | 233/29 |

#### `backend/app` — 30 archivos

| Archivo | Apariciones |
|---|---|
| `backend/app/api/v1/orgs.py` | ITMS 1, toeshee (otro) 2 |
| `backend/app/api/v1/realm_settings.py` | ITMS 2 |
| `backend/app/api/v1/setup.py` | ITMS 1 |
| `backend/app/core/ai/providers/openai_compatible.py` | Toeshee 1 |
| `backend/app/core/ca.py` | CA CN 1 |
| `backend/app/core/config.py` | host 1 |
| `backend/app/core/i18n/es.py` | ITMS 22, ITSuite 2, Toeshee 68, its-agent 8 |
| `backend/app/core/identity/email_domain.py` | toeshee (otro) 8 |
| `backend/app/core/identity/policy.py` | toeshee (otro) 2 |
| `backend/app/core/storage.py` | ITMS-Agent- 1, host 2, its-agent 3 |
| `backend/app/main.py` | ITMS 1 |
| `backend/app/modules/agent_distribution/installers/darwin/com.toeshee.itsagent.plist` | Toeshee 1, its-agent 6, launchd 2 |
| `backend/app/modules/agent_distribution/installers/darwin/install.sh` | Toeshee 1, its-agent 14, launchd 1 |
| `backend/app/modules/agent_distribution/installers/linux/install.sh` | Toeshee 1, its-agent 14 |
| `backend/app/modules/agent_distribution/installers/linux/its-agent.service` | ITMS 1, Toeshee 1, its-agent 9 |
| `backend/app/modules/agent_distribution/installers/windows/install.ps1` | ITMS 1, ITSuite 8, Toeshee 2, its-agent 19 |
| `backend/app/modules/agent_distribution/link_page.py` | ITMS 8, ITMS-Agent- 2, its-agent 6, launchd 2 |
| `backend/app/modules/agent_distribution/managed.py` | ITSuite 1, Toeshee 1, its-agent 13 |
| `backend/app/modules/agent_distribution/msi.py` | ITMS 1, ITMS-Agent- 2, its-agent 1, release/v1 1 |
| `backend/app/modules/agent_distribution/service.py` | its-agent 11, launchd 2 |
| `backend/app/modules/agent_push/preflight.py` | its-agent 1 |
| `backend/app/modules/backup/recovery.py` | ITMS 2 |
| `backend/app/modules/notification/delivery.py` | Toeshee 37 |
| `backend/app/modules/people/checklists.py` | ITMS 1 |
| `backend/app/modules/sla/breach.py` | Toeshee 1 |
| `backend/app/modules/user_admin/api.py` | ITMS 4 |
| `backend/app/modules/user_admin/service.py` | ITMS 1 |
| `backend/app/services/enrollment.py` | Toeshee 1 |
| `backend/app/services/install_link.py` | ITMS 1 |
| `backend/app/services/msi_build.py` | ITMS 3, ITSuite 1, Toeshee 1, its-agent 7 |

#### `agent(code)` — 42 archivos

| Archivo | Apariciones |
|---|---|
| `agent/.gitignore` | its-agent 1 |
| `agent/README.md` | ITSuite 1, Toeshee 1, its-agent 4 |
| `agent/cmd/agent/main.go` | Toeshee 1, its-agent 6 |
| `agent/cmd/release-sign/main.go` | its-agent 3 |
| `agent/internal/capabilities/backup/snapshot.go` | toeshee-agent/snap 2 |
| `agent/internal/capabilities/backup/snapshot_apfs.go` | toeshee-agent/snap 3 |
| `agent/internal/capabilities/provision/provision.go` | toeshee (otro) 2 |
| `agent/internal/capabilities/pushinstall/pushinstall.go` | its-agent 1 |
| `agent/internal/capabilities/pushinstall/script.go` | ITSuite 6, its-agent 5 |
| `agent/internal/capabilities/pushinstall/winrm_capability.go` | ITSuite 1, its-agent 1 |
| `agent/internal/capabilities/registry.go` | its-agent 1 |
| `agent/internal/capabilities/uninstall/helper.go` | its-agent 2 |
| `agent/internal/capabilities/uninstall/msi.go` | ITMS 1, ITSuite 8 |
| `agent/internal/capabilities/uninstall/msi_other.go` | its-agent 1 |
| `agent/internal/capabilities/uninstall/msi_windows.go` | ITSuite 1, its-agent 5 |
| `agent/internal/capabilities/uninstall/system_darwin.go` | launchd 1 |
| `agent/internal/capabilities/uninstall/system_linux.go` | its-agent 7 |
| `agent/internal/capabilities/uninstall/system_windows.go` | ITSuite 4, its-agent 1 |
| `agent/internal/capabilities/uninstall/targets.go` | ITSuite 5, its-agent 10, launchd 2 |
| `agent/internal/config/config.go` | ITSuite 1, its-agent 1 |
| `agent/internal/install/install_other.go` | ITMS 1 |
| `agent/internal/install/install_windows.go` | ITMS 3, ITSuite 4, its-agent 3 |
| `agent/internal/install/layout/layout.go` | ITMS 1, ITSuite 5, Toeshee 1, its-agent 2 |
| `agent/internal/install/layout/push.go` | /opt/toeshee 1, its-agent 1 |
| `agent/internal/install/mode.go` | ITMS 1, ITMS-Agent- 2, ITSuite 2, its-agent 1 |
| `agent/internal/install/msi.go` | ITMS 1, ITSuite 4, its-agent 2 |
| `agent/internal/install/msi_other.go` | ITMS 1, its-agent 1 |
| `agent/internal/install/msi_windows.go` | ITSuite 1, its-agent 4 |
| `agent/internal/install/name.go` | ITMS-Agent- 1 |
| `agent/internal/install/push_other.go` | its-agent 1 |
| `agent/internal/install/push_unix.go` | its-agent 4 |
| `agent/internal/install/pushunix.go` | /opt/toeshee 3, its-agent 11, launchd 2 |
| `agent/internal/install/pushunix_files.go` | ITMS 1, Toeshee 2, its-agent 16, launchd 3 |
| `agent/internal/secdir/append_windows.go` | ITSuite 2, its-agent 1 |
| `agent/internal/secdir/restrict_windows.go` | ITSuite 3 |
| `agent/internal/service/service.go` | ITMS 1, Toeshee 1, its-agent 1 |
| `agent/internal/service/service_windows.go` | its-agent 3 |
| `agent/internal/update/install.go` | its-agent 1 |
| `agent/internal/update/releasesig.go` | release/v1 1 |
| `agent/internal/update/service_linux.go` | its-agent 1 |
| `agent/internal/update/state.go` | its-agent 2 |
| `agent/internal/update/svcparse.go` | its-agent 3, launchd 2 |

#### `agent/packaging` — 17 archivos

| Archivo | Apariciones |
|---|---|
| `agent/packaging/README.md` | ITMS 2, ITMS-Agent- 2, ITSuite 15, its-agent 49, launchd 1 |
| `agent/packaging/build.sh` | its-agent 3 |
| `agent/packaging/darwin/com.toeshee.itsagent.plist` | Toeshee 1, its-agent 6, launchd 2 |
| `agent/packaging/darwin/install.sh` | Toeshee 1, its-agent 14, launchd 1 |
| `agent/packaging/darwin/uninstall.sh` | Toeshee 1, its-agent 5, launchd 1 |
| `agent/packaging/linux/build-deb.sh` | its-agent 20 |
| `agent/packaging/linux/debian/control.in` | ITMS 1, Toeshee 1, its-agent 2, toeshee (otro) 1 |
| `agent/packaging/linux/debian/postinst` | its-agent 7 |
| `agent/packaging/linux/debian/postrm` | its-agent 6 |
| `agent/packaging/linux/debian/prerm` | its-agent 2 |
| `agent/packaging/linux/install.sh` | Toeshee 1, its-agent 14 |
| `agent/packaging/linux/its-agent-enroll` | its-agent 12 |
| `agent/packaging/linux/its-agent.service` | ITMS 1, Toeshee 1, its-agent 9 |
| `agent/packaging/linux/uninstall.sh` | Toeshee 1, its-agent 6 |
| `agent/packaging/windows/install.ps1` | ITMS 1, ITSuite 8, Toeshee 2, its-agent 19 |
| `agent/packaging/windows/its-agent-enroll.ps1` | ITSuite 4, its-agent 11 |
| `agent/packaging/windows/uninstall.ps1` | ITSuite 3, Toeshee 1, its-agent 5 |

#### `deploy` — 15 archivos

| Archivo | Apariciones |
|---|---|
| `deploy/.env.example` | ITMS 3, host 1, toeshee (otro) 2 |
| `deploy/GO-LIVE.md` | ITMS 2, host 9 |
| `deploy/README.md` | CA CN 1, ITMS 26, host 48, its-agent 17, toeshee (otro) 8 |
| `deploy/WINDOWS-PUSH.md` | ITMS 1, ITMS-Agent- 1, ITSuite 4, its-agent 2 |
| `deploy/ci/mcp_flow.py` | ITMS 1, toeshee (otro) 1 |
| `deploy/docker-compose.yml` | ITMS 2, host 2 |
| `deploy/host-network.md` | ITMS 7 |
| `deploy/minio/Dockerfile` | ITMS 1 |
| `deploy/openbao/init.sh` | CA CN 1 |
| `deploy/scripts/deploy.sh` | host 1 |
| `deploy/scripts/host-check.sh` | ITMS 14 |
| `deploy/scripts/install-backup-timer.sh` | ITMS 4 |
| `deploy/scripts/issue-gateway-cert.py` | host 3 |
| `deploy/scripts/preflight.sh` | ITMS 1 |
| `deploy/traefik/dynamic.yml` | host 2 |

#### `.github` — 3 archivos

| Archivo | Apariciones |
|---|---|
| `.github/workflows/agent-update-e2e.yml` | ITSuite 4, its-agent 11 |
| `.github/workflows/ci.yml` | ITMS 6, Toeshee 1, its-agent 23 |
| `.github/workflows/release-agent.yml` | its-agent 8 |

#### `scripts` — 3 archivos

| Archivo | Apariciones |
|---|---|
| `scripts/build_agent.sh` | its-agent 4 |
| `scripts/itms_recover.py` | ITMS 1 |
| `scripts/preflight_email_domain.py` | toeshee (otro) 4 |

#### `docs(current)` — 30 archivos

| Archivo | Apariciones |
|---|---|
| `docs/00-vision.md` | Toeshee 2 |
| `docs/02-tech-stack.md` | Toeshee 1 |
| `docs/07-backup-restore.md` | toeshee (otro) 1 |
| `docs/design/itms-prototype-v3.html` | ITMS 3, Toeshee 1, its-agent 3, toeshee (otro) 3 |
| `docs/design/itms-prototype.html` | Toeshee 13, toeshee (otro) 14, toeshee-agent/snap 1 |
| `docs/design/itms-redesign-2026-09.html` | ITMS 5, host 6 |
| `docs/design/itms-redesign-v2.html` | ITMS 2, host 3 |
| `docs/kernel/wire-protocol/README.md` | its-agent 1, release/v1 1 |
| `docs/modules/agent-distribution/authenticode-signing.md` | Toeshee 1, its-agent 7 |
| `docs/modules/agent-distribution/first-run-verification.md` | ITSuite 11, its-agent 66, launchd 8 |
| `docs/modules/agent-distribution/spec-managed-distribution.md` | Toeshee 2, its-agent 4 |
| `docs/modules/agent-distribution/spec-push-install.md` | /opt/toeshee 5, Toeshee 2 |
| `docs/modules/agent-distribution/spec-self-update.md` | its-agent 18, launchd 3, release/v1 1 |
| `docs/modules/agent-distribution/spec.md` | ITMS-Agent- 1, its-agent 9 |
| `docs/modules/agent/README.md` | its-agent 5 |
| `docs/modules/agent/open-questions.md` | its-agent 1 |
| `docs/modules/agent/plan.md` | ITSuite 1, its-agent 10, launchd 1 |
| `docs/modules/ai-admin/spec.md` | Toeshee 2 |
| `docs/modules/asset-inventory/spec.md` | ITMS 1 |
| `docs/modules/backup/spec-server-backup.md` | ITMS 18, Toeshee 1 |
| `docs/modules/change-mgmt.md` | Toeshee 1 |
| `docs/modules/console-auth/spec.md` | ITMS 14, host 1 |
| `docs/modules/notification/plan.md` | Toeshee 1 |
| `docs/modules/notification/spec.md` | Toeshee 2 |
| `docs/modules/people/spec.md` | ITMS 1 |
| `docs/modules/portal/spec.md` | Toeshee 2 |
| `docs/modules/remote-control/spec-screen-share.md` | ITMS 1 |
| `docs/modules/service-desk/spec.md` | Toeshee 1 |
| `docs/modules/sla/spec.md` | Toeshee 1 |
| `docs/product.md` | ITMS 3, Toeshee 1 |



---

## Apéndice E — Fuentes externas

Cada afirmación externa del plan, con la fuente leída el 2026-11-23 (o el 2026-11-24, donde se dice).

| | Fuente | Lo que sostiene |
|---|---|---|
| E1 | Traefik, «ACME» (certificate resolvers), https://doc.traefik.io/traefik/reference/install-configuration/tls/certificate-resolvers/acme/ | «when using the `TLS-ALPN-01` challenge, Traefik must be reachable by Let's Encrypt through port 443»; `email` obligatorio; `caServer` por defecto `https://acme-v02.api.letsencrypt.org/directory`; `caCertificates`; «By default, Traefik manages 90-day certificates and starts renewing them 30 days before their expiry»; `certificatesDuration` 2160 h por defecto |
| E2 | Traefik, «Configuration Overview», https://doc.traefik.io/traefik/getting-started/configuration-overview/ (2026-11-24) | «There are three different, mutually exclusive (i.e. you can use only one at the same time), ways to define static configuration» |
| E3 | Let's Encrypt, «Rate Limits», https://letsencrypt.org/docs/rate-limits/ | «Up to 5 certificates can be issued per exact same set of identifiers every 7 days»; «Up to 5 authorization failures per identifier can be incurred by one account every hour» |
| E4 | Let's Encrypt, «Staging Environment», https://letsencrypt.org/docs/staging-environment/ | la URL de staging; 30.000 certificados por semana; raíces que ningún almacén de confianza tiene |
| E5 | Let's Encrypt, «IPv6 Support», https://letsencrypt.org/docs/ipv6-support/ | «will always prefer the IPv6 addresses for the initial connection»; vuelve al IPv4 solo ante fallos de red como un *timeout*, «not on other types of error» |
| E6 | Let's Encrypt, «Decreasing Certificate Lifetimes to 45 Days» (2025-12-02), https://letsencrypt.org/2025/12/02/from-90-to-45.html (2026-11-24) | 64 días desde el «February 10, 2027»; 45 días desde el «February 16, 2028» |
| E7 | Let's Encrypt, «Expiration notification service has ended» (2025-06-26), https://letsencrypt.org/2025/06/26/expiration-notification-service-has-ended.html (2026-11-24) | «This service ended on June 4, 2025»; la dirección no se guarda, se pasa a la lista de correo de ISRG |
| E8 | lego, «Options», https://go-acme.github.io/lego/advanced/options/index.html (2026-11-24) | «The environment variable `LEGO_CA_CERTIFICATES` allows to specify the path to PEM-encoded CA certificates that can be used to authenticate an ACME server with an HTTPS certificate» |
| E9 | Pebble, README, https://github.com/letsencrypt/pebble | «not suited for use as a production CA»; `tlsPort` (5001 por defecto); `-dnsserver`; `PEBBLE_VA_NOSLEEP`; `PEBBLE_WFE_NONCEREJECT` (5 % por defecto); la raíz en `:15000/roots/0`; raíz e intermedia nuevas en cada arranque; imágenes en `ghcr.io/letsencrypt/pebble` |
| E10 | Docker, «Install Docker Engine on Ubuntu», https://docs.docker.com/engine/install/ubuntu/ | los comandos del repositorio en formato deb822 y los paquetes (P5 del runbook, tal cual); Ubuntu 24.04 y 26.04 soportadas; «when you expose container ports using Docker, these ports bypass your firewall rules» |
| E11 | Docker, «Linux post-installation steps», https://docs.docker.com/engine/install/linux-postinstall/ | `sudo usermod -aG docker $USER`; «The `docker` group grants root-level privileges to the user» |
| E12 | Ubuntu Server, «Automatic updates», https://ubuntu.com/server/docs/how-to/software/automatic-updates/ | `unattended-upgrades` «is installed by default»; el contenido de `20auto-upgrades` |
| E13 | Ubuntu Server, «timedatectl and timesyncd», https://ubuntu.com/server/docs/how-to/networking/timedatectl-and-timesyncd/ | `timedatectl status` con «System clock synchronized: yes» y «NTP service: active»; chrony en lugar de timesyncd desde 25.10 |
| E14 | Ubuntu Server, «Firewalls», https://ubuntu.com/server/docs/how-to/security/firewalls/ | `sudo ufw enable`, `sudo ufw allow 22`, `sudo ufw status verbose`; ufw empieza desactivado |
| E15 | Traefik, «Docker» provider, https://doc.traefik.io/traefik/reference/install-configuration/providers/docker/ | `allowEmptyServices` (por defecto `false`): sin él, un contenedor no sano no da servidores a su servicio |
| E16 | Traefik v3.6, `pkg/provider/acme/provider.go`, https://raw.githubusercontent.com/traefik/traefik/v3.6/pkg/provider/acme/provider.go (2026-11-24) | `getCertificateRenewDurations`: con `certificatesDuration >= 90 días`, se renueva a 30 días del final y se mira cada día; `crt.NotAfter.Before(time.Now().Add(renewPeriod))` compara con la caducidad real del certificado |

**Comprobado aquí, no leído** (sesión del 2026-11-23 y 2026-11-24, Docker 29.9.3): `traefik:v3.6` (3.6.25) obtiene de Pebble 2.10.1 un certificado por TLS-ALPN-01 con `LEGO_CA_CERTIFICATES` («Server responded with a certificate»; sin ella, «x509: certificate signed by unknown authority»); Pebble acepta un correo vacío o de `.example`; el router sin servidores contesta `503`; `starlette` 0.47.3 no tiene `HTTP_422_UNPROCESSABLE_CONTENT` y 0.48.0 sí; y el job `deploy` entero de la CI con las Tasks 3, 4, 7, 9 y 10, salvo sus dos pasos de copia, en verde (el certificado de Pebble, las dos fases de `mcp_flow.py`, el 503, el agente inscrito por el 443 y conectado por el 8443, el `hello` firmado y contestado, la cadena de auditoría verificada sobre 34 eventos).

**Sin verificar, y lo dice el runbook:** Let's Encrypt de producción y de staging, un VPS, el DNS real, ufw, `systemd` (el temporizador de la copia), la app de Entra, un relé SMTP y cualquier equipo Windows o macOS.

---

## Apéndice F — Las líneas de `frontend/src` que este proyecto obliga a cambiar (para el proyecto 3)

El proyecto 3 es el dueño de estos cambios (resolución del coordinador del 2026-11-24). Lo que este proyecto cambia del otro lado está en la columna «Por».

**F.1 — Acoplamientos que fallan si no cambian** (la consola lee un nombre que el agente o el backend escriben distinto desde las Tasks 8):

| Archivo:línea | Hoy | Después | Por |
|---|---|---|---|
| `frontend/src/modules/agent-releases/publish.ts:63` | `parts[0] !== "its" \|\| parts[1] !== "agent"` | `parts[0] !== "novieri" \|\| parts[1] !== "agent"` | los artefactos son `novieri-agent-<os>-<arch>[.exe]` (`release-agent.yml`, `release-sign` `platformOf`): sin el cambio, el diálogo de publicar no reconoce ningún archivo |
| `publish.ts:6`, `:57` (comentarios) | `its-agent-<os>-<arch>` | `novieri-agent-<os>-<arch>` | lo mismo |
| `frontend/src/lib/api.ts:4344` | `` `ITMS-Agent-${body.arch}.msi` `` (nombre de reserva del MSI) | `` `NovieriRMM-Agent-${body.arch}.msi` `` | `msi.py` escribe `NovieriRMM-Agent-<host>[@<puerto>]-<arch>.msi`; el prefijo es el que el agente reconoce (`FilePrefix`) |
| `api.ts:1472` (comentario) | `ITMS-Agent-<host>[@<port>]-<arch>.msi` | `NovieriRMM-Agent-<host>[@<port>]-<arch>.msi` | lo mismo |

**F.2 — Datos de ejemplo:** `frontend/src/modules/agent-releases/fixtures.ts:104,107,116,120,121,124,125,128,207` (`its-agent…`, `ITMS-Agent-…`, `console.toeshee.example`) y `frontend/src/modules/deploy/fixtures.ts:47` (`toeshee-agent`, que es el paquete `.deb` `novieri-agent`).

**F.3 — Las pruebas que fijan lo de F.1 y F.2** (87 líneas en 8 archivos): `publish.test.ts` (27), `PublishReleaseDialog.test.tsx` (20), `agent-releases.es.test.tsx` (19), `AgentReleasesPage.test.tsx` (6), `MsiDownload.test.tsx` (6), `ManagedDeploymentDialog.test.tsx` (5, entre ellas `.\its-agent-enroll.ps1`, que pasa a `novieri-agent-enroll.ps1`), `api.test.ts` (2) y `ReleaseChannels.test.tsx` (2).

**F.4 — El nombre del producto en la consola** («ITMS» 83 apariciones en 39 archivos, «Toeshee» 78 en 29, otros `toeshee` 108 en 30; Apéndice D), `DESIGN.md` y `.impeccable/design.json`: es el cambio de marca del propio proyecto 3, no un acoplamiento con este.

---

## Autorrevisión

**Cobertura del encargo.**
- **A, el cambio de nombre de §1.1 y nada de §1.2:** backend (Task 1), agente en sus dos lados con el `UpgradeCode` nuevo y el prefijo del instalador en los dos lados (Task 8), documentos del despliegue (Task 9), lo que queda fuera de la guarda (Task 11); la guarda con la técnica de `test_no_vendor_names_backend.py` (Task 11, roja con 745 nombres sobre `1bc0e5e`, verde sobre el árbol renombrado); los recuentos por archivo (Apéndice D); las líneas del frontend para el proyecto 3 (Apéndice F).
- **B, HTTPS:** Task 3 (`certResolver` `le` con `tlsChallenge`, `traefik-acme`, las tres claves, `Host()` en cada router, fuera `deploy/certs/`, el 503), Task 4 (la comprobación previa y la de después del arranque), el 8443 sin tocar (Task 3 lo prueba; Task 9 lo comprueba en el paso 7); fuentes de Traefik [E1][E2][E15][E16].
- **C, el VPS:** `new-env.sh` y las comprobaciones de `preflight.sh` (Task 4); el runbook nuevo con ufw, actualizaciones, hora, usuario sin `root` y Docker CE del repositorio de Docker [E10]–[E14]; ningún `curl` con `Bearer $TOKEN` ejecutable (Task 9, con prueba); «After a reboot» y la actualización empezando por `bao status` (Task 9, con prueba).
- **D, la verificación:** el ensayo del primer arranque entero aquí (Task 12); el job `deploy` contra Pebble (Task 7) y con el agente compilado, el token sin navegador, el 8443, el `hello` firmado y la cadena (Task 10), cada uno rojo antes que verde, y los dos **corridos de verdad en esta sesión** al escribir el plan.
- **E, los pendientes de §6:** ocho de nueve (Tasks 2, 3, 5 y 6), cada uno con su prueba; el noveno es la pregunta Q3.
- **F, el informe:** Task 13, con las secuencias leídas del runbook fusionado, el paso del operador numerado y aparte, y la verdad sobre la vuelta atrás.

**Marcadores.** El plan no tiene `TBD`, `TODO` ni «rellenar después»; los `…` que aparecen están dentro de salidas esperadas (un número de serie, una fecha), nunca en un comando. En los comandos que el operador pega no hay ningún `<…>`: `test_runbook_first_boot.py` lo comprueba sobre el runbook, y el informe sale de él.

**Nombres y firmas.** Las que una tarea fija y otra consume están en el Apéndice A y coinciden con el código de las tareas: `agent_flow.chain_order`/`trail_in_order`/`AgentFlow.sign_in(i)`/`device(hostname, wait_s)`/`token(path)`, que importa `rehearse_console.py` (Task 12); `mcp_flow.write_owner_state`, `Client`, `totp`, `totp_secret` (Task 7); `check-public-tls.sh --cafile/--connect/--wait`; los encabezados del runbook que leen las Tasks 11, 12 y 13.

**Lo comprobado al escribir el plan** (copias de `1bc0e5e` en el scratchpad, nunca el árbol del repositorio):
- Tasks 1–8: cada una sola sobre la base, con los números de sus Steps.
- Task 9 sobre la oleada 1 (Tasks 3, 4 y 7): rojo (`7 failed`), las cuatro piezas del guion aplicadas en orden con cada ancla una sola vez, `130 passed`, cero nombres viejos en los cuatro documentos.
- Task 10 sobre la oleada 1 y la 9: rojo y verde de las pruebas (`64 passed`), y el **job `deploy` entero** (salvo sus dos pasos de copia) corrido aquí con el guion de la Task 7: verde, y rojo sin gateway. La primera ejecución destapó el error de orden de la cadena (`ts` en segundos), corregido en la tarea con su prueba.
- Task 11 sobre el árbol con las Tasks 1, 3, 4, 7, 8, 9 y 10: `31 passed`; 745 sobre la base; el nombre plantado, rojo; `115 passed` con las vecinas.
- **No se ha ejecutado entero el ensayo de la Task 12**: es la tarea. Lo que ese ensayo arriesga más (el certificado por ACME, la inscripción y el 8443, la firma del `hello`, el 503) ya corrió en el job de la CI de esta sesión; el script de verificación de la cadena del paso 11e es el de `docs/07-backup-restore.md`, carácter por carácter.
- **La suite entera del backend** sobre el árbol fusionado es del coordinador (Global Constraints).

---

## Preguntas para el operador

- **Q1 — `CLAUDE.md` habla de Toeshee y de `it-suite`** («Internal-first for Toeshee», el árbol `it-suite/`). Es tu archivo y este plan no lo toca. ¿Quieres cambiarlo tú, o que lo proponga una tarea aparte?
- **Q2 — la cabecera `X-Toeshee-Event-Id` de los *webhooks* de notificación** se queda, como contrato con quien los consume (P4-B-09). En un despliegue nuevo no hay consumidores todavía (B1, P7): ¿se renombra ahora (`X-Novieri-Event-Id`), mientras no cuesta nada, o se queda?
- **Q3 — el pendiente «una `app_session` puede quedar con el alcance vacío»** (diseño §6): no hay nada con ese nombre. Las tres lecturas: (a) `org_session` con un `ClientScope` vacío, que por diseño ve cero filas (invariante 5a: «Una sesión sin alcance ve cero filas»), y rechazarla cambiaría ese fallo seguro por un error; (b) `auth_sessions`, que no guarda alcance; (c) `ai_session`, que guarda `persona` y `mode`, no alcance. ¿A qué se refería?
- **Q4 — «el presupuesto fijo de 15 s de la prueba del agente Go»:** la Task 6 entiende `TestAJobSurvivesADisconnectAndItsResultArrivesAfterReconnect`, la única con un plazo fijo de 15 s cuyo fallo está registrado. ¿Es esa?
- **Q5 — el paquete `.deb`:** `Maintainer: Novieri <it@novieri.example>` (un dominio `.example`, como el de antes) y sin `Homepage:`. ¿Hay un buzón y una página reales que poner?

---

## Lo que el diseño dice y el código desmiente

1. **«Un secreto aleatorio nuevo en cada clave `change-me`»** (§3): los dos tokens de OpenBao no existen hasta el paso 3.4 y no se pueden generar antes; `new-env.sh` los deja en `change-me` y el paso 3.5 los escribe sin marcadores (P4-B-04).
2. **Las rutas del agente de §1.1 no son las del código:** no hay `/opt/toeshee/agent` ni `/opt/toeshee/enroll`; son `/opt/toeshee/agent.pkg`, `/opt/toeshee/enroll.token` y `/var/opt/toeshee` (`agent/internal/install/layout/push.go:27`, `agent/internal/install/pushunix.go:17`). Y §1.1 no nombra las que cuelgan del servicio (`/var/lib/its-agent`, `/etc/its-agent`, `/var/log/its-agent`, …), que cambian con él (P4-B-05).
3. **El 503 «en `/api` y `/mcp`»** (§6): `api` atiende once rutas por Traefik (`/enroll`, `/.well-known`, `/oauth`, …); el router de reserva las cubre todas (P4-B-03).
4. **El `AAAA`:** el diseño pide que el DNS resuelva a esta máquina, pero no dice que un `AAAA` rompe la validación: Let's Encrypt prueba IPv6 primero y no vuelve al IPv4 ante un *connection refused* [E5], y el stack publica solo en IPv4. `preflight.sh` se niega ante cualquier `AAAA`.
5. **La comprobación del DNS tras un NAT:** «resuelve a una IP de esta máquina» falla en un VPS cuya IP pública no está en su interfaz; `preflight.sh` lo anota en vez de negarse cuando todas las direcciones del host son privadas.
6. **«El demonio de Docker funciona en el contenedor de la sesión»** (§4): funciona, pero el HTTPS de la sesión pasa por un proxy que vuelve a firmar los certificados, y un `docker build` no puede bajar nada sin las imágenes base «de la sesión» (Global Constraints); y Docker Hub limita las descargas anónimas (`429`, visto dos veces).
7. **No es un error del diseño, pero sí algo que decir:** la regla de `CLAUDE.md` («the sequence always begins with `docker compose exec openbao bao status`») no se puede cumplir al pie de la letra en el primer arranque de un host nuevo, porque no hay contenedor de OpenBao hasta el paso 3.1. El diseño (§3) ya la limita a las secuencias posteriores; el informe lo dice (Task 13).
8. **El cambio de nombre destapa un defecto:** `msi_build.verify_msi` compara la tabla `File` ordenada con una lista sin ordenar; con `novieri-agent.exe` todo MSI se rechazaría (Task 8 lo arregla).
9. **El frontend no es independiente de este proyecto:** `publish.ts:63` reconoce los artefactos por el prefijo `its-agent`; con los artefactos nuevos, el diálogo de publicar no reconoce ningún archivo hasta que el proyecto 3 cambie esa línea (Apéndice F).
10. **`ITMS_BIND_IP` e `ITMS_NETWORK_SUBNET`** no están en §1.2 ni en §1.1; se quedan por la razón de §1.2 (P4-B-08).
11. **La vida de los certificados** baja a 64 días en 2027 y a 45 en 2028 [E6]; el diseño no lo menciona. Traefik lo absorbe sin cambios [E16], y el informe lo deja para revisar en febrero de 2027.
