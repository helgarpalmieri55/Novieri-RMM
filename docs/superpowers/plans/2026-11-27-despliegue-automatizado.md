# Despliegue automatizado del primer arranque de `rmm.novieri.com` — plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que el primer arranque de un VPS nuevo sean tres órdenes —`sudo ./scripts/prepare-host.sh`, `./scripts/first-boot.sh` y `./scripts/first-boot-check.sh`— más los momentos que solo una persona puede hacer, con OpenBao desellándose solo desde ese primer arranque, sin perder ninguna comprobación del runbook, y con los dos últimos guiones ejecutados por el trabajo `deploy` de la CI en cada subida, contra Pebble.

**Architecture:** Tres guiones `bash` nuevos en `deploy/scripts/`, en el estilo de los que ya hay, que ejecutan los bloques del runbook de hoy y no los reescriben: llaman a `new-env.sh`, `build-images.sh`, `preflight.sh`, `check-public-tls.sh`, `issue-gateway-cert.py`, `openbao-seal.sh`, `install-backup-timer.sh` y `scheduled-backup.sh`. Cada tramo mira antes si ya está hecho y lo salta diciéndolo, así que relanzar el guion es la forma de seguir tras un fallo. Los secretos (las cinco partes, el token raíz, los dos tokens de ejecución, el código de instalación, el token de inscripción) viven en la memoria del guion y llegan a `bao`, a `sudo tee` y al agente por la entrada estándar; un arnés de pruebas con un `docker` falso y una cuña delante de cada orden demuestra que ninguno aparece nunca en la línea de órdenes de ningún proceso ni en un archivo que el runbook no nombre. La CI deja de hacer su propio primer arranque (con una sola parte) y llama a los dos guiones, con un conductor que contesta como el operador y enmascara cada secreto; el runbook pasa a ser el recorrido de tres órdenes, con los bloques de hoy en un apéndice.

**Tech Stack:** Bash 5.2 (el de Ubuntu 24.04), shellcheck 0.9.0, Python 3.12 con pytest 9.2.0 para las pruebas, Python 3 de la biblioteca estándar para el conductor de la CI, Docker Engine 29.9.3 y Compose 5.7.1 en esta sesión, OpenBao 2.6.1, Pebble 2.10.1, GitHub Actions. Ninguna dependencia nueva.

**Diseño:** `docs/superpowers/specs/2026-11-27-despliegue-automatizado-design.md`, aprobado por el operador el 2026-11-27 («Sí, avanza en lo que puedas avanzar mientras»): §2 es el recorrido del operador, §3 los tres guiones, §4 el desellado automático, §5 las pruebas y la CI, §6 la documentación, §8 los criterios de aceptación. Las decisiones del operador del mismo día («Automatizar antes», «Activarlo siempre», DNS con Cloudflare) ya están en `.git/sdd/decisiones-p34.md`. **Y las resoluciones del coordinador del mismo día** a las ocho preguntas de la primera versión de este plan (al final, «Resoluciones del coordinador»): cuatro de ellas corrigen el diseño (Q3 en §5 y §8.2, Q5 en §3, Q6 en §2, §3 y §4, Q7 en §6), y cada corrección la escribe la tarea a la que pertenece, en un paso propio, «El diseño» (Tasks 1, 2, 5 y 6).

## Global Constraints

- **Seguridad.** Las invariantes 1, 1b, 1c, 2, 2a, 3, 4, 5, 5a y 6 de `CLAUDE.md` no se tocan, y en concreto:
  - **1:** ningún cambio toca la firma, los bytes canónicos ni la verificación. El trabajo de la CI sigue siendo un `hello` que el servidor firma y el agente compilado verifica antes de ejecutarlo, y desde la resolución Q3 prueba también la otra mitad en cada subida: el mismo binario, con una clave de firma fijada que el servidor no tiene, rechaza un `hello` firmado con `bad_signature` y no lo ejecuta (Task 5, 7c-bis). El falsador, en el ensayo, es un agente que no verifica (Task 5, Step 7, Rojo 3), en una rama que no se fusiona: `agent/` no cambia.
  - **2:** el agente de prueba (`smoke-agent`) marca hacia fuera por el 443 y el 8443; `--network host` y ningún `-p`: no escucha en nada. El del caso negativo (`ci-agent`), en la red del stack, tampoco publica nada.
  - **3 y diseño §3, «Secretos»:** ninguna parte, token o código en la línea de órdenes de un proceso, en un registro, en un archivo temporal ni en el historial. Solo se escriben donde ya van hoy: `deploy/.env` (0600), `/etc/it-suite/gateway-issuer.token` (0600, de root) y el archivo de clave de `openbao-seal.sh`, `/etc/it-suite/openbao-unseal/unseal.key` (0400, del usuario de OpenBao). Al reescribir `.env`, `sed -i` crea su propio temporal en `deploy/`, con el modo y el dueño de `.env` (GNU sed 4.9 los conserva: medido el 2026-11-27 con un archivo de `nobody`, 0600, editado como root), y lo renombra sobre él: es lo que hacen hoy el paso 3.5 del runbook y `openbao-seal.sh`.
  - **Diseño §3:** `set -x` prohibido, y los dos guiones de primer arranque **se niegan** a correr con xtrace (`bash -x`). Sin `<<<`, y todo *here-document* con el delimitador entre comillas (`<<'EOF'`): bash, antes de la 5.1 siempre y después por encima del tamaño de una tubería, los escribe en un archivo temporal. Un secreto llega a otro proceso con `printf '%s\n' "$x" | …`, que es una orden interna y no arranca proceso.
  - **Diseño §3:** las cinco partes y el token raíz se imprimen solo si la salida es una terminal (`[ -t 1 ]`); con la salida redirigida, `first-boot.sh` se niega **antes** de `bao operator init`, salvo con `ITS_FIRST_BOOT_ALLOW_NONTTY=1`, que solo pone `deploy/ci/first_boot_answers.py`, el conductor de la CI, que enmascara cada secreto (`::add-mask::`) antes de imprimir la línea que lo lleva. El código de instalación sigue la misma regla.
- **Estilo de los guiones** (diseño §3): `bash` con `set -euo pipefail`, ejecutables (`chmod +x` antes de `git add`), `cd` a `deploy/` por su propia ruta, salida en inglés. Las líneas, exactas, que la CI y el runbook leen: `  ok      ` (una comprobación que se cumplió), `  skip    ` (un tramo ya hecho), `  run     ` (un tramo que cambia algo), `  note    `, `  WARNING ` (a stderr) y `FAILED  <qué falló>` seguido de las órdenes para seguir, sangradas ocho espacios (a stderr). Las cabeceras de tramo (`say`) van en negrita solo en una terminal. Ninguna orden de un `FAILED` lleva marcadores (`CLAUDE.md`, «A copy-pasteable command contains no placeholders»).
- **`| grep -q` nunca al final de una tubería con `pipefail`:** `grep` sale en cuanto encuentra, la orden de antes recibe SIGPIPE y la tubería entera da «no». Se compara la salida guardada (`[[ $out == *…* ]]`) o se usa `grep -c`/*here-string* sobre datos que no son secretos.
- **OpenBao:** cinco partes y umbral tres **también en la CI** (hasta hoy, una); el desellado automático con clave estática siempre, en cada primer arranque (decisión del operador del 2026-11-27, «Activarlo siempre»); un OpenBao ya inicializado nunca se vuelve a inicializar. Imagen `quay.io/openbao/openbao:2.6.1`.
- **Versiones:** Docker Engine 26 o más y Compose 2.24 o más (lo que `preflight.sh` ya exige); `busybox:1.36.1` y `golang:1.24-bookworm` para el agente de prueba, como el paso 11c; Pebble `ghcr.io/letsencrypt/pebble:2.10.1`.
- **Interruptores para la CI, un ensayo o las pruebas, y solo para eso** (Apéndice A): `ITS_FIRST_BOOT_ALLOW_NONTTY=1`, `ITS_PREFLIGHT_SKIP_DNS=1` (que `preflight.sh` rechaza con Let's Encrypt de producción como CA, o sin `ITS_ACME_CA_SERVER`, que es lo mismo: la de un host real; resolución Q4), `ITS_NO_SYSTEMCTL=1`, `ITS_SYSTEMD_DIR`, `ITS_ETC_DIR`; y, solo para las pruebas, `ITS_OS_RELEASE` (`prepare-host.sh`), `ITS_SMOKE_AGENT_DIR` y `ITS_BACKUP_STATE_DIR` (`first-boot-check.sh`; el segundo ya es de `scheduled-backup.sh`). Ninguno se pone en un VPS, y ninguno debilita una comprobación sin decirlo en una línea `note`.
- **Lo que no cambia:** ni la aplicación (`backend/app`, `frontend/`, `agent/`), ni las migraciones (ninguna nueva), ni el protocolo del agente (`docs/kernel/wire-protocol/` intacto), ni `.env.example` (las dos claves que añade el desellado, `COMPOSE_FILE` y `OPENBAO_UNSEAL_KEY_FILE`, las escribe `openbao-seal.sh` en el `.env` del host, como hasta hoy), ni `CLAUDE.md`, ni los planes fechados de `docs/superpowers/`, ni sus diseños salvo el de este proyecto, y ese solo en los párrafos que el coordinador corrigió (Tasks 1, 2, 5 y 6, paso «El diseño»). Fuera de alcance (diseño §7): un KMS, credenciales de Cloudflare en el VPS o en la CI, automatizar el navegador o la custodia.
- **Pruebas.** Ninguna se salta ni se debilita: cada aserción de una prueba adaptada sigue existiendo donde ahora vive la orden que fijaba, y la tarea lo dice. Cada prueba nueva se ve en rojo antes que en verde. Las pruebas de los guiones corren contra órdenes simuladas en el `PATH`; las de `first-boot.sh` y `first-boot-check.sh` ponen una cuña que registra la línea de órdenes delante de **cada** orden externa (si el guion usa una que la prueba no conoce, falla con «command not found»). Las cuñas apuntan antes a `/usr/bin` y `/bin` que al `PATH` de la sesión: esta sesión pone un envoltorio de `rm` en el `PATH` que vuelve a llamar a `rm` por su nombre, y una cuña que apuntara a él se llamaría a sí misma sin fin (medido el 2026-11-27).
- **Textos:** los de `deploy/` (guiones, runbook, GO-LIVE) en inglés; el plan y el informe en español. Las pruebas pasan `ruff check` a 100 columnas.
- **Verificación, desde `backend/` de la worktree:** `ITS_TEST_REDIS_URL=redis://localhost:6379/<la base de la tarea> .venv/bin/python -m pytest -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning -p no:cacheprovider -q <archivos>` (cada tarea da su número de base, del 1 al 6, para que las worktrees no compartan Redis) y `.venv/bin/ruff check app tests`. **Desde la raíz:** `shellcheck deploy/scripts/prepare-host.sh deploy/scripts/first-boot.sh deploy/scripts/first-boot-check.sh` (lo que ya exista). La suite completa la corre el coordinador en el árbol fusionado.
- **Worktrees, una por tarea** (subagent-driven development). La worktree nace de **`origin/claude/compassionate-bardeen-vgtwus`**, que está en `6f616aa docs(spec): the first boot of rmm.novieri.com in three scripts` (la `main` de la PR #14 más el diseño), o de la rama del coordinador con este plan encima; nunca de la `main` local. El coordinador la crea con `git fetch -q origin claude/compassionate-bardeen-vgtwus && git worktree add -b p5-t1 ../wt-p5-t1 origin/claude/compassionate-bardeen-vgtwus` (y así con el número de cada tarea: las ramas son `p5-t1` a `p5-t6`, y las tareas que dependen de otras las fusionan por esos nombres en su Step 0; si el coordinador ya las integró en otra rama, se fusiona esa en su lugar). Preparación, desde la raíz de la worktree:

  ```bash
  ln -s /home/user/Novieri-RMM/backend/.venv backend/.venv
  cd backend && export PYTHONPATH=$PWD && .venv/bin/python -c "import app; print(app.__file__)" && cd ..
  pg_ctlcluster 16 main start 2>/dev/null; redis-cli ping >/dev/null 2>&1 || redis-server --daemonize yes
  ```

  La segunda línea imprime una ruta de la worktree. Las tareas de la oleada 1 comprueban además, en su Step 0, que el código es el medido (`CODE-AS-MEASURED`); las demás fusionan antes, por sus nombres `p5-tN`, las ramas de las que dependen, y comprueban lo que dice su Step 0.
- **Docker (solo la Task 5, Step 7, el ensayo del trabajo de la CI aquí):** `docker version >/dev/null 2>&1 || (nohup dockerd >/tmp/dockerd.log 2>&1 &)`, y esperar a que `docker version --format '{{.Server.Version}}'` imprima `29.9.3`. **Docker Hub puede contestar `429 Too Many Requests`** a las descargas anónimas (`postgres:16`, `traefik:v3.6`, `busybox:1.36.1`, `python:3.12-slim`…); `mirror.gcr.io` funciona como espejo (comprobado por el coordinador): se pone en `/etc/docker/daemon.json` y se reinicia `dockerd` (Task 5, Step 7). Y las tres imágenes base de los `Dockerfile` se tapan en el almacén local con una copia que confía en la CA del proxy de la sesión, igual que en el plan del 2026-11-23 (Global Constraints, «Las imágenes base de la sesión»); esas imágenes son solo de esta sesión y no se publican.
- **Commits.** Uno por paso «Commit», con el formato de los anteriores (`feat(deploy): …`, `test(deploy): …`, `ci(deploy): …`, `docs(deploy): …`) y estas dos líneas al final del mensaje:

  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_013TnTdGAz4rSe1YRXXjEqyr
  ```

  Se hace commit en la rama de la worktree; no se hace push, no se usa `git stash` y no se toca el árbol principal. Nada se despliega en un host real.

---

## Lo que hace este proyecto, y lo que no

**Hace.** `prepare-host.sh` (P2 a P5; P1 sigue a mano, resolución Q5, Task 1); `first-boot.sh`, en dos tareas: del estado mezclado a la provisión de OpenBao, 3.6 incluido, y justo después el desellado automático y el reinicio que lo prueba (resolución Q6, Task 2), y de las migraciones al código del paso 8 (Task 3); `first-boot-check.sh` (pasos 10, 11a, 11c a 11f y 12, Task 4); el trabajo `deploy` de la CI sobre los dos guiones, con sus comprobaciones de hoy y tres nuevas: el reinicio de OpenBao, la segunda ejecución y el rojo con la firma mala (resolución Q3, Task 5); el runbook, GO-LIVE §1, §4 y §6 y `docs/07-backup-restore.md`, con las pruebas que los fijan adaptadas (Task 6); el informe de despliegue (Task 7).

**No hace.**
1. **No crea el registro `A`.** Lo crea el coordinador con el conector de Cloudflare el día del despliegue, tras enseñar el cambio y con el «sí» del operador (diseño, cabecera y §2.0). No es código.
2. **No automatiza el navegador** (reclamar la instalación, el segundo factor, el token de inscripción) **ni la custodia** (diseño §7). En la CI esos clics los hacen `mcp_flow.py` y `agent_flow.py`, que ya existen.
3. **No verifica de verdad** activar ufw, instalar Docker (diseño §5, «queda como sin verificar, igual que hoy») ni los temporizadores de systemd del paso 6b y del 12: la CI sigue con `ITS_NO_SYSTEMCTL=1` (resolución Q2), escribe las unidades en su propio directorio y no las activa. El plan y el informe lo dicen así.
4. **No despliega.** La secuencia la da el informe (Task 7), y el operador la corre desde `main`.

## Lo que el código dijo al medirlo (2026-11-27)

Medido en esta sesión antes de escribir el plan, con `quay.io/openbao/openbao:2.6.1`, Compose 5.7.1 y los guiones del árbol. Cada hecho tiene su consecuencia en una tarea.

- **M1. `bao operator init -status` sale con 2 tanto para «no inicializado» como para una conexión rechazada** (`dial tcp 127.0.0.1:8200: connect: connection refused`, código 2). La espera de la CI de hoy (`[ "$rc" = 2 ] && break`) se fía de lo contrario, y la prueba `test_openbao_is_waited_for_until_it_answers_not_initialised` lo afirma en su docstring. `bao status -format=json` sí distingue: 0 desellado, 2 sellado (inicializado o no, con el JSON impreso), 1 sin respuesta. `first-boot.sh` lee `initialized` y `sealed` de ese JSON (Task 2), y la prueba se rehace con la medida (Task 5).
- **M2. Formatos.** `bao status -format=json` imprime `  "initialized": false,` y `  "type": "shamir",` con dos espacios; `bao operator init -key-shares=5 -key-threshold=3` imprime `Unseal Key 1: <44 caracteres en base64>` … `Unseal Key 5: …`, una línea en blanco, `Initial Root Token: s.<24>`. `bao token lookup -format=json` lleva `"policies": [` y `"root"` en líneas distintas. `bao token create … -field=token` imprime solo el token. **`bao token lookup -field=policies` no existe** (`flag provided but not defined: -field`).
- **M3. Una parte mala se acepta hasta el umbral**: con la tercera, OpenBao contesta 400, vuelve el progreso a 0 y sigue sellado. Una parte repetida no cuenta. `bao write sys/unseal reset=true` falla (`cannot unmarshal string … of type bool`); `printf '{"reset":true}' | bao write -format=json sys/unseal -` funciona. Por eso `first-boot.sh` comprueba cada parte contra las cinco que acaba de imprimir, no admite una repetida y, sin ellas en memoria, reinicia el progreso antes de cada ronda (Task 2).
- **M4. El `COMPOSE_FILE` del entorno gana al de `.env`.** La CI exporta `COMPOSE_FILE=docker-compose.yml:ci/acme-pebble.yml`; `openbao-seal.sh enable` escribe `COMPOSE_FILE=docker-compose.yml:openbao/auto-unseal.yml` en `.env` y se niega si `.env` ya tiene otro. Si `openbao-seal.sh` heredara el de la CI, recrearía OpenBao sin la configuración del sello al que migra; y si después la CI siguiera con el suyo, cualquier `up` haría lo mismo. Así que `first-boot.sh` llama a `sudo env -u COMPOSE_FILE ./scripts/openbao-seal.sh …` (explícito, sin fiarse de que `sudo` limpie el entorno) y, si heredó un `COMPOSE_FILE`, le añade `openbao/auto-unseal.yml` en cuanto `.env` lo tiene (Task 2); la CI hace lo mismo en `GITHUB_ENV` tras el primer arranque (Task 5).
- **M5. Pebble crea una raíz nueva cada vez que arranca, y ninguna orden de compose corre sin `.env`** (`${ITS_PUBLIC_HOST:?}`, `${ITS_ACME_EMAIL:?}`). La comprobación del paso 7 necesita esa raíz y el paso 7 es de `first-boot.sh`. Por eso la CI escribe `.env` con el mismo `new-env.sh` que el paso 1 (su paso de hoy, sin cambios), arranca Pebble, lee su raíz y pasa `--tls-cafile`; `first-boot.sh` encuentra `.env` y dice `skip`, y el conductor de la CI exige esa línea (resolución Q1).
- **M6. `preflight.sh` se niega en la CI:** `getent -s dns ahostsv4 rmm.novieri.com` no devuelve nada desde un ejecutor, y si el registro `A` ya existiera devolvería la IPv4 del VPS, que no es del ejecutor. Una CI que dependiera del DNS real cambiaría de color el día en que se crea el registro. `ITS_PREFLIGHT_SKIP_DNS=1` deja la pregunta sin hacer y lo dice en una línea `note`; y se niega si la CA de ACME es la de producción de Let's Encrypt o no está puesta, porque eso es un host real (Task 2; resolución Q4).
- **M7. Compose 5.7.1:** `docker compose ps -a --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}'` imprime `long|running|healthy|0` y `one|exited||0`; `docker compose up <servicio de una vez>` vuelve con 0 cuando su contenedor termina; un contenedor con `--network host` lleva el nombre de máquina del host (`hostname` dentro y fuera: `vm`).
- **M8. GNU sed 4.9 conserva el dueño y el modo** de un archivo que edita con `-i` como root (un archivo de `nobody:nogroup` 0600 sigue así): el `.env` del operador no pasa a ser de root cuando `sudo openbao-seal.sh` lo reescribe.
- **M9. `docker compose up -d` vuelve a arrancar los contenedores de una vez que ya salieron** (`migrate`, `postgres-grants`, `openbao-init`): el mismo `Id`, con `StartedAt` nuevo (Compose 5.7.1). Una huella con la hora de arranque de cada contenedor cambiaría en cada segunda ejecución sin que nada hubiera cambiado. La de la CI (Task 5) toma de cada contenedor su `Id` y, si está en marcha, su hora de arranque; si salió, su código de salida.
- **M10. Una clave fijada que nadie firma.** El agente lee `job_signing_public_key_pem` de su `pins.json` con `x509.ParsePKIXPublicKey` y exige que sea Ed25519 (`agent/internal/enroll/enroll.go`); 32 bytes al azar dentro del envoltorio de RFC 8410 (`302a300506032b6570032100` y los 32 bytes, en PEM) se leen siempre como una clave válida, y ninguna firma del servidor verifica contra ella (Go 1.24, 2000 claves: todas se leen, ninguna verifica). `agent_flow.py` la hace con la biblioteca estándar de Python, sin `openssl`. El rechazo se ve en tres sitios: el trabajo acaba `refused` con el resultado `{"refused": "bad_signature"}`; la auditoría tiene `jobs.refused` con el id del trabajo; y el agente escribe `level=WARN msg="job refused" job_id=… job_type=hello reason=bad_signature`. La comprobación es `if !ed25519.Verify(v.key, msg, sig) {`, `agent/internal/jobs/verify.go:180`.

## Oleadas y paralelismo

| Oleada | Tareas | En paralelo | Depende de | Se fusiona |
|---|---|---|---|---|
| 1 | 1, 2, 4 | sí, cada una en su worktree: archivos disjuntos | — | en cualquier orden |
| 2 | 3 | sola (mismo `first-boot.sh` y mismo archivo de pruebas que la 2) | la 2 fusionada; la 1 y la 4 pueden seguir abiertas | — |
| 3 | 5, 6 | sí, en worktrees distintas: archivos disjuntos | 1, 2, 3 y 4 fusionadas (las dos leen el código de los tres guiones) | **primero la 5, después la 6**, que vuelve a pasar sus pruebas sobre el árbol fusionado |
| 4 | 7 | sola | todas | — |

## Tareas

| Task | Qué | Diseño | Base Redis |
|---|---|---|---|
| 1 | `prepare-host.sh` y sus pruebas con órdenes simuladas; el diseño, §3 | §3, §5 | 1 |
| 2 | `first-boot.sh`, primera parte: estado mezclado, pasos 1 a 3.6, el desellado automático y su reinicio justo después, la negativa sin terminal; el arnés de pruebas; `ITS_PREFLIGHT_SKIP_DNS` y su negativa con producción; el diseño, §2, §3 y §4 | §2, §3, §4, §5 | 2 |
| 3 | `first-boot.sh`, segunda parte: pasos 4 a 7, sin partes al relanzarlos, y el código del paso 8 | §3, §5 | 3 |
| 4 | `first-boot-check.sh` y sus pruebas | §3, §5 | 4 |
| 5 | La CI `deploy` sobre los guiones, con las tres comprobaciones nuevas (la firma mala, en el paso 7c-bis); el conductor `first_boot_answers.py`; la fase `refused` de `agent_flow.py`; las pruebas de la CI adaptadas; el ensayo aquí en rojo y en verde; el diseño, §5 y §8.2 | §5, §8.2 | 5 |
| 6 | El runbook (recorrido de tres órdenes, el apéndice), GO-LIVE §1, §4 y §6, `docs/07`; las pruebas que los fijan; el diseño, §6 | §4, §6, §8.5 | 6 |
| 7 | El informe de despliegue, rehecho sobre el runbook nuevo | §6, §8 | — |

**Por qué este reparto.** El que proponía el coordinador, con dos ajustes que salen del código. `first-boot.sh` se parte tras el desellado automático, que desde la resolución Q6 va justo después del paso 3.6: la primera parte es la de los secretos, los estados que hay que negar y el cambio de sello (la que más merece una revisión aparte), la segunda es mecánica y, como OpenBao ya se desella solo, nunca pide una parte; las dos comparten archivo, así que van seguidas. El arnés de pruebas (un `docker` falso que hace de compose, OpenBao y Postgres) nace entero en la Task 2 aunque la 2 no use todo: es la interfaz del guion entero, y así la 3 solo añade pruebas. `preflight.sh` cambia tres líneas y va con la Task 2, que es la que necesita el interruptor. `first-boot-check.sh` lleva su propio `docker` falso, más pequeño, para no depender de la Task 2 y poder ir en paralelo. Y el runbook y la CI van en paralelo porque no comparten archivo; el riesgo de que la Task 5 corrija una línea que el runbook cita lo cubre una prueba de la 6 (las líneas que el runbook cita son líneas que los guiones imprimen) al fusionar en el orden 5, 6.

## Notas de integración del coordinador

- **Oleada 1, choques esperados: ninguno.** La 1 crea `deploy/scripts/prepare-host.sh` y `backend/tests/test_prepare_host_script.py`; la 2, `deploy/scripts/first-boot.sh`, `backend/tests/first_boot_support.py`, `backend/tests/test_first_boot_script.py` y toca `deploy/scripts/preflight.sh` y `backend/tests/test_preflight_script.py`; la 4, `deploy/scripts/first-boot-check.sh` y `backend/tests/test_first_boot_check_script.py`. **La 1 y la 2 tocan las dos el diseño**, `docs/superpowers/specs/2026-11-27-despliegue-automatizado-design.md`, en párrafos distintos (la 1, el de `prepare-host.sh` en §3; la 2, §2, el de `first-boot.sh` en §3 y §4): se fusionan en cualquier orden sin conflicto (medido el 2026-11-27 con `git merge` de las dos ediciones sobre `6f616aa`).
- **Oleada 2:** la 3 edita los dos archivos de la 2 y ninguno más.
- **Oleada 3:** la 5 es dueña de `.github/workflows/ci.yml`, `deploy/ci/first_boot_answers.py`, `deploy/ci/agent_flow.py`, `deploy/ci/mcp_flow.py`, `backend/tests/test_ci_first_boot.py`, `backend/tests/test_ci_mcp_flow.py` y `backend/tests/test_ci_agent_flow.py`; la 6, de `deploy/README.md`, `deploy/GO-LIVE.md`, `docs/07-backup-restore.md`, `backend/tests/test_runbook_first_boot.py` y `backend/tests/test_deploy_runbook_secrets.py`. Las dos tocan el diseño, en párrafos distintos (la 5, §5 y §8.2; la 6, §6), y se fusionan sin conflicto en el orden 5, 6 (medido como el de la oleada 1). **Si el ensayo de la 5 (Step 7) obliga a corregir un guion**, la corrección va en la rama de la 5 con su commit y su prueba, la 5 se fusiona antes que la 6, y la 6 vuelve a pasar `tests/test_runbook_first_boot.py` sobre el árbol fusionado (su `test_the_lines_the_runbook_quotes_are_lines_the_scripts_print` se pone en rojo si una línea citada cambió).
- **Las pruebas que una tarea escribe y otra lee:** `tests/test_first_boot_script.py::test_step_6b_writes_the_units_the_runbook_writes` (Task 3) lee los dos *here-documents* de las unidades del paso 6b en el README **donde estén**; la Task 6 los mueve al apéndice sin cambiar un carácter de su contenido. `tests/test_ci_mcp_flow.py` y `tests/test_ci_agent_flow.py` (Task 5) leen texto de `first-boot.sh` y `first-boot-check.sh` (las Tasks 2 a 4): sus nombres y líneas están en el Apéndice A.
- **Contratos que una tarea fija y otras consumen:** el Apéndice A.

---

## Las tareas, una a una

### Task 1: `prepare-host.sh` (oleada 1, en paralelo con las Tasks 2 y 4)

**Files:**
- Create: `deploy/scripts/prepare-host.sh` (ejecutable)
- Test: `backend/tests/test_prepare_host_script.py`
- Modify: `docs/superpowers/specs/2026-11-27-despliegue-automatizado-design.md` (§3, el párrafo de `prepare-host.sh`: resolución Q5)

**Interfaces:**
- Consumes: nada de otras tareas. Lo que ejecuta es lo de P2 a P5 de `deploy/README.md` tal como está en `6f616aa` (la Task 6 lo mueve al apéndice sin cambiarlo).
- Produces:
  - `sudo ./scripts/prepare-host.sh` desde `deploy/` (o `sudo deploy/scripts/prepare-host.sh` desde la raíz: hace `cd` a su propia carpeta), sin opciones salvo `--help`. Sale 0 con el host listo, 1 con `FAILED` y las órdenes para seguir. Como root sin `sudo` (sin `SUDO_USER`) se niega con `FAILED  which account is the operator? Run this with sudo from that account`, seguida de `sudo ./scripts/prepare-host.sh` y `a VPS with only root makes the account first, by hand: deploy/README.md, P1`; y `--create-user`, como cualquier otra opción, con `FAILED  unknown argument: --create-user`.
  - Las líneas que la Task 6 cita en el runbook, exactas: `Docker's versions`, `  ok      Docker Engine <versión> (26 or newer)`, `  ok      Docker Compose <versión> (2.24 or newer)`, `The host is ready for the first boot`, `  note    log out, and log in again as <cuenta>: the docker group applies to a new login only` y `  note    then, as <cuenta>: cd <deploy/ de la copia> && ./scripts/first-boot.sh`.
  - `ITS_OS_RELEASE` (solo pruebas; por defecto `/etc/os-release`).
  - Para la Task 5: el archivo pasa `shellcheck` sin avisos.

**Decisiones de la tarea.**
- **La cuenta del operador es `SUDO_USER`, y el guion no crea ninguna** (resolución Q5 del coordinador, 2026-11-27): P1 se hace a mano, como dice el diseño §2.0, porque el guion vive en la copia que P6 hace desde esa cuenta. Sin `SUDO_USER` (root directo) se niega antes de cambiar nada y dice dónde está P1. La prueba `test_it_has_no_way_to_make_the_account` fija que la opción no existe y que el guion no llama a `adduser`.
- **Cada tramo se comprueba antes por una orden simulable**, nunca leyendo un archivo de `/etc` directamente: `ufw status verbose`, `dpkg-query`, `apt-config dump`, `systemctl is-active`, `timedatectl show`, `docker version`, `id -nG`. Así la segunda ejecución de la prueba ve lo que hizo la primera, y la de un host real ve lo que el host tiene.
- **P5 se salta solo con Docker CE del repositorio de Docker** (`dpkg-query` de `docker-ce`) **y** las dos versiones mínimas: un `docker.io` de Ubuntu que contesta a `docker version` no basta, porque es justo lo que el runbook dice que no se use.
- **`DEBIAN_FRONTEND=noninteractive`**: un paquete que preguntara a mitad dejaría el guion esperando sin decir por qué; el runbook lo dice en el apéndice (Task 6).
- **`apt-config dump` se guarda antes de buscar en ella**, en vez de `apt-config dump | grep -qx …`: con `pipefail`, el `grep -q` que sale pronto deja a `apt-config` con SIGPIPE, y la comprobación diría «no está» con P3 hecho (Global Constraints).

- [ ] **Step 0: La worktree**

Desde la raíz de la worktree:

```bash
git fetch -q origin claude/compassionate-bardeen-vgtwus
git log --oneline -1
git merge-base --is-ancestor HEAD origin/claude/compassionate-bardeen-vgtwus && echo ON-PROJECT-BRANCH
git merge-base --is-ancestor 6f616aa HEAD && echo HAS-SPEC
git diff --quiet 6f616aa HEAD -- backend agent deploy .github && echo CODE-AS-MEASURED
```

Expected: `6f616aa docs(spec): the first boot of rmm.novieri.com in three scripts` (o el commit posterior de la rama del coordinador del que salió la worktree), `ON-PROJECT-BRANCH`, `HAS-SPEC`, `CODE-AS-MEASURED`. Si falta alguna, se para y se avisa al coordinador sin tocar nada. Después, la preparación de Global Constraints («Worktrees»).

- [ ] **Step 1: Las pruebas**

Crear `backend/tests/test_prepare_host_script.py`:

```python
"""`deploy/scripts/prepare-host.sh`, on a stubbed host (design
2026-11-27-despliegue-automatizado §3, §5).

Every command that changes or asks the host -- `apt-get`, `ufw`,
`timedatectl`, `usermod`, `curl`, `install`, `chmod`, `tee`, `dpkg`,
`dpkg-query`, `apt-config`, `systemctl`, `docker`, `id` -- is a stub on PATH
that logs its arguments and answers from a small state directory, so a
second run sees what the first one did. What is pinned: the commands and
their order, which are the runbook's P2 to P5; a second run that changes
nothing; and the refusals. P1 is not the script's: the operator account is
made by hand, as root, before the checkout it would run from exists
(coordinator's ruling Q5, 2026-11-27). Turning ufw on and installing Docker for real are
not run anywhere by this repository: they stay unverified, as before.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "scripts" / "prepare-host.sh"

STUBS = {
    "id": r"""#!/bin/sh
echo "id $*" >> "$STUB_LOG"
case "$1" in
  -u) if [ $# -eq 1 ]; then echo "$FAKE_UID"
      elif [ -e "$STUB_STATE/user-$2" ]; then echo 1001
      else exit 1; fi ;;
  -nG) cat "$STUB_STATE/groups-$2" 2>/dev/null || echo "$2" ;;
esac
""",
    "usermod": r"""#!/bin/sh
echo "usermod $*" >> "$STUB_LOG"
groups="$(cat "$STUB_STATE/groups-$3" 2>/dev/null || echo "$3")"
echo "$groups $2" > "$STUB_STATE/groups-$3"
""",
    "ufw": r"""#!/bin/sh
echo "ufw $*" >> "$STUB_LOG"
case "$1" in
  allow) echo "$2" >> "$STUB_STATE/ufw-rules" ;;
  --force) touch "$STUB_STATE/ufw-on" ;;
  status)
    [ -e "$STUB_STATE/ufw-on" ] || { echo "Status: inactive"; exit 0; }
    printf 'Status: active\nLogging: on (low)\n'
    printf 'Default: deny (incoming), allow (outgoing), disabled (routed)\n\n'
    printf 'To                         Action      From\n'
    printf '%s\n' '--                         ------      ----'
    while read -r rule; do printf '%-26s ALLOW IN    Anywhere\n' "$rule"
    done < "$STUB_STATE/ufw-rules" ;;
esac
""",
    "apt-get": r"""#!/bin/sh
echo "apt-get $*" >> "$STUB_LOG"
case "$*" in
  *"install -y unattended-upgrades"*) touch "$STUB_STATE/pkg-unattended-upgrades" ;;
  *"install -y docker-ce "*) touch "$STUB_STATE/pkg-docker-ce" ;;
esac
""",
    "dpkg-query": r"""#!/bin/sh
echo "dpkg-query $*" >> "$STUB_LOG"
for last; do :; done
[ -e "$STUB_STATE/pkg-$last" ] && printf 'install ok installed' || exit 1
""",
    "dpkg": r"""#!/bin/sh
echo "dpkg $*" >> "$STUB_LOG"
case "$1" in
  --print-architecture) echo amd64 ;;
  --get-selections) printf '%b' "$FAKE_CONFLICTS" ;;
esac
""",
    "apt-config": r"""#!/bin/sh
echo "apt-config $*" >> "$STUB_LOG"
cat "$STUB_STATE/tee-20auto-upgrades" 2>/dev/null
""",
    "tee": r"""#!/bin/sh
echo "tee $*" >> "$STUB_LOG"
cat > "$STUB_STATE/tee-$(basename "$1")"
cat "$STUB_STATE/tee-$(basename "$1")"
""",
    "systemctl": r"""#!/bin/sh
echo "systemctl $*" >> "$STUB_LOG"
case "$*" in
  "is-active unattended-upgrades")
    [ -e "$STUB_STATE/pkg-unattended-upgrades" ] && echo active || echo inactive ;;
esac
""",
    "timedatectl": r"""#!/bin/sh
echo "timedatectl $*" >> "$STUB_LOG"
case "$*" in
  "show -p NTP --value") [ -e "$STUB_STATE/ntp-on" ] && echo yes || echo no ;;
  "show -p NTPSynchronized --value")
    [ -e "$STUB_STATE/ntp-on" ] && [ -z "$FAKE_NEVER_SYNC" ] && echo yes || echo no ;;
  "set-ntp true") touch "$STUB_STATE/ntp-on" ;;
  status) [ -e "$STUB_STATE/ntp-on" ] && s=yes n=active || s=no n=inactive
          printf 'System clock synchronized: %s\n              NTP service: %s\n' "$s" "$n" ;;
esac
""",
    "docker": r"""#!/bin/sh
echo "docker $*" >> "$STUB_LOG"
[ -e "$STUB_STATE/pkg-docker-ce" ] || { [ -n "$FAKE_OLD_ENGINE" ] || exit 1; }
case "$*" in
  "version --format {{.Server.Version}}") echo "${FAKE_ENGINE:-29.1.2}" ;;
  "compose version --short") echo "${FAKE_COMPOSE:-v5.0.1}" ;;
esac
""",
    "sleep": "#!/bin/sh\nexit 0\n",
}
for _name in ("curl", "install", "chmod"):
    STUBS[_name] = f'#!/bin/sh\necho "{_name} $*" >> "$STUB_LOG"\n'

#: What a host bare of everything the runbook adds is made to run, in order:
#: the runbook's P2, P3, P4 and P5 (deploy/README.md's appendix).
FIRST_RUN = [
    "ufw allow 22/tcp",
    "ufw allow 443/tcp",
    "ufw allow 8443/tcp",
    "ufw --force enable",
    "apt-get update",
    "apt-get install -y unattended-upgrades",
    "tee /etc/apt/apt.conf.d/20auto-upgrades",
    "timedatectl set-ntp true",
    "apt-get remove -y docker.io",
    "apt-get install -y ca-certificates curl git openssl iproute2",
    "install -m 0755 -d /etc/apt/keyrings",
    "curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc",
    "chmod a+r /etc/apt/keyrings/docker.asc",
    "tee /etc/apt/sources.list.d/docker.sources",
    "apt-get update",
    "apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin"
    " docker-compose-plugin",
    "usermod -aG docker rmmadmin",
]
#: The commands that change something. A second run logs none of them.
CHANGING = ("usermod", "ufw allow", "ufw --force", "apt-get ",
            "tee ", "timedatectl set-ntp", "curl ", "install ", "chmod ")  # fmt: skip

UBUNTU = 'ID=ubuntu\nVERSION_ID="24.04"\nUBUNTU_CODENAME=noble\nVERSION_CODENAME=noble\n'


class Box:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.state = tmp / "state"
        self.state.mkdir()
        self.log = tmp / "calls.log"
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for name, body in STUBS.items():
            stub = self.bin / name
            stub.write_text(body)
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.os_release = tmp / "os-release"
        self.os_release.write_text(UBUNTU)
        deploy = tmp / "repo" / "deploy" / "scripts"
        deploy.mkdir(parents=True)
        self.script = deploy / "prepare-host.sh"
        self.script.write_bytes(SCRIPT.read_bytes())

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        self.log.write_text("")
        base = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "STUB_LOG": str(self.log),
            "STUB_STATE": str(self.state),
            "ITS_OS_RELEASE": str(self.os_release),
            "FAKE_UID": "0",
            "FAKE_CONFLICTS": r"docker.io\tinstall\n",
            "SUDO_USER": "rmmadmin",
        }
        base.update(env)
        return subprocess.run(
            [shutil.which("bash") or "/bin/bash", str(self.script), *args],
            capture_output=True,
            text=True,
            env=base,
            stdin=subprocess.DEVNULL,
        )

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def changes(self) -> list[str]:
        return [c for c in self.calls() if c.startswith(CHANGING)]


def test_the_script_parses() -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_a_bare_host_gets_the_runbooks_p2_to_p5_in_order(tmp_path: Path) -> None:
    box = Box(tmp_path)
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert box.changes() == FIRST_RUN
    assert "ok      Docker Engine 29.1.2 (26 or newer)" in done.stdout
    assert "ok      Docker Compose 5.0.1 (2.24 or newer)" in done.stdout
    assert "log out, and log in again as rmmadmin" in done.stdout
    assert f"cd {tmp_path}/repo/deploy && ./scripts/first-boot.sh" in done.stdout
    sources = (box.state / "tee-docker.sources").read_text()
    assert sources == (
        "Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: noble\n"
        "Components: stable\nArchitectures: amd64\nSigned-By: /etc/apt/keyrings/docker.asc\n"
    )
    assert (box.state / "tee-20auto-upgrades").read_text() == (
        'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n'
    )


def test_a_second_run_changes_nothing_and_says_so(tmp_path: Path) -> None:
    box = Box(tmp_path)
    assert box.run().returncode == 0
    again = box.run()
    assert again.returncode == 0, again.stdout + again.stderr
    assert box.changes() == []
    skipped = [line.split(":")[0].strip() for line in again.stdout.splitlines()
               if line.startswith("  skip    ")]  # fmt: skip
    assert skipped == ["skip    P2", "skip    P3", "skip    P4", "skip    P5", "skip    P5"]
    assert "log out, and log in again" not in again.stdout


def test_ntp_already_on_is_not_set_again(tmp_path: Path) -> None:
    box = Box(tmp_path)
    (box.state / "ntp-on").touch()
    assert box.run().returncode == 0
    assert "timedatectl set-ntp true" not in box.changes()


def test_a_clock_that_never_synchronises_stops_the_run(tmp_path: Path) -> None:
    box = Box(tmp_path)
    done = box.run(FAKE_NEVER_SYNC="1")
    assert done.returncode == 1
    assert "the clock is not synchronised after 120 s" in done.stderr
    assert not any(c.startswith("apt-get install -y docker-ce ") for c in box.calls())


def test_it_refuses_to_run_as_anyone_but_root(tmp_path: Path) -> None:
    box = Box(tmp_path)
    done = box.run(FAKE_UID="1000")
    assert done.returncode == 1
    assert "it runs as root" in done.stderr and "sudo ./scripts/prepare-host.sh" in done.stderr
    assert box.changes() == []


def test_without_sudo_user_it_asks_for_the_operator(tmp_path: Path) -> None:
    """Run from a root login, there is no operator to put in `docker`: the
    account is P1's, made by hand first."""
    box = Box(tmp_path)
    done = box.run(SUDO_USER="")
    assert done.returncode == 1
    assert "by hand: deploy/README.md, P1" in done.stderr
    assert box.changes() == []


def test_it_has_no_way_to_make_the_account(tmp_path: Path) -> None:
    """Coordinator's ruling Q5 (2026-11-27): P1 stays manual, so the option
    that made the account is gone and refused like any unknown one."""
    box = Box(tmp_path)
    done = box.run("--create-user", "rmmadmin", SUDO_USER="")
    assert done.returncode == 1
    assert "unknown argument: --create-user" in done.stderr
    assert box.changes() == []
    assert "adduser" not in SCRIPT.read_text()


def test_a_host_that_is_not_ubuntu_is_refused_before_anything(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.os_release.write_text('ID=debian\nVERSION_ID="12"\nVERSION_CODENAME=bookworm\n')
    done = box.run()
    assert done.returncode == 1 and "this is not Ubuntu" in done.stderr
    assert box.changes() == []


def test_an_engine_too_old_after_the_install_stops_the_run(tmp_path: Path) -> None:
    box = Box(tmp_path)
    done = box.run(FAKE_ENGINE="24.0.7")
    assert done.returncode == 1
    assert "Docker Engine is 24.0.7, and it must be 26 or newer" in done.stderr


def test_ubuntus_own_docker_is_replaced_not_kept(tmp_path: Path) -> None:
    """docker.io answers `docker version` too, but it is not docker-ce."""
    box = Box(tmp_path)
    done = box.run(FAKE_OLD_ENGINE="1", FAKE_ENGINE="26.1.3")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "apt-get remove -y docker.io" in box.changes()


def test_an_extra_open_port_is_warned_about(tmp_path: Path) -> None:
    box = Box(tmp_path)
    (box.state / "ufw-rules").write_text("80/tcp\n")
    done = box.run()
    assert "ufw also lets in: 80/tcp" in done.stderr
```

- [ ] **Step 2: Las pruebas fallan**

Run, desde `backend/`: `ITS_TEST_REDIS_URL=redis://localhost:6379/1 .venv/bin/python -m pytest -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning -p no:cacheprovider -q tests/test_prepare_host_script.py`

Expected: `12 failed` (el guion no existe: `FileNotFoundError` al copiarlo y `bash -n` con 127).

- [ ] **Step 3: El guion**

Crear `deploy/scripts/prepare-host.sh` y hacerlo ejecutable (`chmod +x deploy/scripts/prepare-host.sh`):

```bash
#!/usr/bin/env bash
#
# Prepare a new Ubuntu Server 24.04 VPS for Novieri RMM: deploy/README.md,
# Prerequisites, P2 to P5 (design 2026-11-27-despliegue-automatizado §3).
#
#   sudo ./scripts/prepare-host.sh        # from the operator account
#
# WHAT IT DOES, in the runbook's order, each part skipped when it is done
# already: P2 the firewall (22, 443 and 8443 in, nothing else; ufw on); P3
# unattended security updates; P4 the clock (NTP on, if it is not); P5 Docker
# CE from Docker's own repository, and the operator account -- the one that
# ran sudo -- in the `docker` group. It ends by checking Docker Engine 26 and
# Compose 2.24, the two versions preflight.sh refuses below.
#
# WHAT IT DOES NOT DO: make the operator account (P1, by hand, as root: the
# checkout this script lives in is made by that account, in P6), clone the
# repository (P6: the deploy key is yours to add in GitHub), touch DNS, or
# open any other port. Its commands are the runbook's, in deploy/README.md's
# appendix, "What each script does, by hand".
#
# Prints `run` before what it changes, `skip` for what it finds done, `ok`
# for a check that held. Run it again after any failure.
set -euo pipefail

if [ -t 1 ]; then bold=$'\033[1m'; plain=$'\033[0m'; else bold=""; plain=""; fi
say()  { printf '\n%s%s%s\n' "$bold" "$*" "$plain"; }
ok()   { printf '  ok      %s\n' "$*"; }
skip() { printf '  skip    %s\n' "$*"; }
act()  { printf '  run     %s\n' "$*"; }
note() { printf '  note    %s\n' "$*"; }
warn() { printf '  WARNING %s\n' "$*" >&2; }
die() {
  printf '\nFAILED  %s\n' "$1" >&2
  shift
  local line
  for line in "$@"; do printf '        %s\n' "$line" >&2; done
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" "./scripts/prepare-host.sh --help" ;;
  esac
done

deploy_dir="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
os_release="${ITS_OS_RELEASE:-/etc/os-release}"
# Debconf asks nothing in the middle of a run; every answer is the package's default.
export DEBIAN_FRONTEND=noninteractive

[ "$(id -u)" = 0 ] || die "this changes the firewall, the packages and the groups: it runs as root" \
  "sudo ./scripts/prepare-host.sh"
operator="${SUDO_USER:-}"
if [ -z "$operator" ] || [ "$operator" = root ]; then
  die "which account is the operator? Run this with sudo from that account" \
      "sudo ./scripts/prepare-host.sh" \
      "a VPS with only root makes the account first, by hand: deploy/README.md, P1"
fi
ID="" VERSION_ID="" UBUNTU_CODENAME="" VERSION_CODENAME=""
# shellcheck disable=SC1090  # the system's own description of itself
. "$os_release"
[ "$ID" = ubuntu ] || die "this is not Ubuntu ($os_release says ID=$ID): P5 installs Docker from its Ubuntu repository" \
  "deploy/README.md, Prerequisites: Ubuntu Server 24.04 LTS"
[ "$VERSION_ID" = 24.04 ] || warn "Ubuntu $VERSION_ID, not 24.04: the runbook was written and rehearsed for 24.04"

# No `| grep -q` under pipefail: grep leaving early is a SIGPIPE upstream, and a false "no".
in_group() { [[ " $(id -nG "$1") " == *" $2 "* ]]; }
at_least() { [ -n "$1" ] && [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }

# --------------------------------------------------------------- P2

# P2 is done when ufw is on, refuses what comes in, and lets in 22, 443 and 8443.
firewall_done() {
  local status="$1" port
  grep -q '^Status: active' <<<"$status" || return 1
  grep -q '^Default: deny (incoming)' <<<"$status" || return 1
  for port in 22 443 8443; do
    grep -qE "^$port/tcp +ALLOW IN" <<<"$status" || return 1
  done
}

say "P2 — the firewall"
status="$(ufw status verbose 2>/dev/null || true)"
if firewall_done "$status"; then
  skip "P2: ufw is on, denies what comes in, and allows 22/tcp, 443/tcp and 8443/tcp"
else
  act "ufw allow 22/tcp, 443/tcp and 8443/tcp; ufw --force enable"
  ufw allow 22/tcp
  ufw allow 443/tcp
  ufw allow 8443/tcp
  ufw --force enable
  status="$(ufw status verbose)"
  printf '%s\n' "$status"
  firewall_done "$status" || die "ufw is not what P2 needs (above)" \
    "it needs Status: active, Default: deny (incoming), and ALLOW IN for 22/tcp, 443/tcp and 8443/tcp" \
    "sudo ufw status verbose"
  ok "P2: ufw is on, denies what comes in, and allows 22/tcp, 443/tcp and 8443/tcp"
fi
extra="$(grep -E 'ALLOW IN' <<<"$status" | grep -vE '^(22|443|8443)/tcp ' | awk '{print $1}' | tr '\n' ' ' || true)"
[ -z "$extra" ] || warn "ufw also lets in: ${extra% }. This stack needs 22, 443 and 8443 and nothing else."

# --------------------------------------------------------------- P3

say "P3 — security updates"
apt_conf="$(apt-config dump 2>/dev/null || true)"
if [ "$(dpkg-query -W -f='${Status}' unattended-upgrades 2>/dev/null || true)" = "install ok installed" ] \
   && grep -qx 'APT::Periodic::Update-Package-Lists "1";' <<<"$apt_conf" \
   && grep -qx 'APT::Periodic::Unattended-Upgrade "1";' <<<"$apt_conf" \
   && [ "$(systemctl is-active unattended-upgrades 2>/dev/null || true)" = active ]; then
  skip "P3: unattended-upgrades is installed, on, and active"
else
  act "apt-get install unattended-upgrades, and its two lines in /etc/apt/apt.conf.d/20auto-upgrades"
  apt-get update
  apt-get install -y unattended-upgrades
  printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' | tee /etc/apt/apt.conf.d/20auto-upgrades
  [ "$(systemctl is-active unattended-upgrades 2>/dev/null || true)" = active ] \
    || die "unattended-upgrades is not active" "sudo systemctl enable --now unattended-upgrades" "sudo ./scripts/prepare-host.sh"
  ok "P3: unattended-upgrades is active"
fi

# --------------------------------------------------------------- P4

say "P4 — the clock"
if [ "$(timedatectl show -p NTP --value 2>/dev/null || true)" = yes ] \
   && [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)" = yes ]; then
  skip "P4: NTP is on and the clock is synchronised"
else
  if [ "$(timedatectl show -p NTP --value 2>/dev/null || true)" != yes ]; then
    act "timedatectl set-ntp true"
    timedatectl set-ntp true
  fi
  for _ in $(seq 1 24); do
    [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)" = yes ] && break
    sleep 5
  done
  [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)" = yes ] \
    || die "the clock is not synchronised after 120 s: certificates, second-factor codes and the audit log depend on it" \
           "timedatectl status" \
           "check that the provider lets this VPS out to NTP (UDP 123), then: sudo ./scripts/prepare-host.sh"
  ok "P4: NTP is on and the clock is synchronised"
fi
timedatectl status | grep -E 'System clock synchronized|NTP service'

# --------------------------------------------------------------- P5

say "P5 — Docker CE, from Docker's repository"
engine="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
composev="$(docker compose version --short 2>/dev/null | sed 's/^v//' || true)"
if [ "$(dpkg-query -W -f='${Status}' docker-ce 2>/dev/null || true)" = "install ok installed" ] \
   && at_least "$engine" 26 && at_least "$composev" 2.24; then
  skip "P5: Docker CE $engine and Compose $composev, from Docker's repository"
else
  act "the distribution's conflicting packages out; Docker's apt repository; docker-ce and its plugins"
  # shellcheck disable=SC2046  # one word per package the selection names
  apt-get remove -y $(dpkg --get-selections docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc 2>/dev/null | cut -f1)
  apt-get install -y ca-certificates curl git openssl iproute2
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  printf 'Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' \
    "${UBUNTU_CODENAME:-$VERSION_CODENAME}" "$(dpkg --print-architecture)" | tee /etc/apt/sources.list.d/docker.sources
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
relogin=no
if in_group "$operator" docker; then
  skip "P5: $operator is in the docker group"
else
  act "usermod -aG docker $operator"
  usermod -aG docker "$operator"
  relogin=yes
fi
note "the docker group grants root-level privileges (Docker's post-installation notes): $operator alone is in it"

# ------------------------------------------------------------- versions

say "Docker's versions"
engine="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
composev="$(docker compose version --short 2>/dev/null | sed 's/^v//' || true)"
at_least "$engine" 26 || die "Docker Engine is ${engine:-not answering}, and it must be 26 or newer: traefik:v3.6's Docker provider refuses an older one" \
  "sudo systemctl status docker --no-pager" "sudo ./scripts/prepare-host.sh"
ok "Docker Engine $engine (26 or newer)"
at_least "$composev" 2.24 || die "Docker Compose is ${composev:-not answering}, and it must be 2.24 or newer: .env's \${...} references inside env_file need it" \
  "sudo apt-get install -y docker-compose-plugin" "sudo ./scripts/prepare-host.sh"
ok "Docker Compose $composev (2.24 or newer)"

say "The host is ready for the first boot"
if [ "$relogin" = yes ]; then
  note "log out, and log in again as $operator: the docker group applies to a new login only"
fi
note "then, as $operator: cd $deploy_dir && ./scripts/first-boot.sh"
```

- [ ] **Step 4: Las pruebas pasan, y shellcheck no dice nada**

Run, desde `backend/`: la orden del Step 2, y `.venv/bin/ruff check app tests`. Desde la raíz: `shellcheck deploy/scripts/prepare-host.sh && echo SHELLCHECK-CLEAN` y `test -x deploy/scripts/prepare-host.sh && echo EXECUTABLE`.

Expected: `12 passed`; `All checks passed!`; `SHELLCHECK-CLEAN`; `EXECUTABLE`.

- [ ] **Step 5: El diseño, §3** (resolución Q5)

Con este guion, desde la raíz (no se guarda en el repositorio):

```bash
S=/tmp/claude-0/-home-user-Novieri-RMM/21768af2-984e-5cf8-87b2-4dc8a41a4864/scratchpad
cat > "$S/task1_spec.py" <<'TASK1_SPEC'
"""Plan 2026-11-27, Task 1: the design's §3 says what prepare-host.sh is now
(coordinator's ruling Q5, 2026-11-27): no --create-user, P1 by hand. One anchor."""

from pathlib import Path

SPEC = Path("docs/superpowers/specs/2026-11-27-despliegue-automatizado-design.md")
OLD = """\
**`prepare-host.sh`** (como `root` o con `sudo`): P2 cortafuegos, P3
actualizaciones, P4 reloj (activa NTP si hace falta), P5 Docker CE del
repositorio de Docker, y el grupo `docker` para la cuenta que lo llama
(`SUDO_USER`). Con `--create-user <nombre>` y lanzado como `root`, hace también
P1. Termina comprobando las versiones mínimas (Engine 26, Compose 2.24) y
diciendo que hay que salir y volver a entrar.
"""
NEW = """\
**`prepare-host.sh`** (con `sudo`, desde la cuenta de operador): P2 cortafuegos,
P3 actualizaciones, P4 reloj (activa NTP si hace falta), P5 Docker CE del
repositorio de Docker, y el grupo `docker` para la cuenta que lo llama
(`SUDO_USER`). No crea ninguna cuenta: P1 se hace a mano (§2, paso 0), y
lanzado como `root` sin `sudo` se niega y lo dice (decisión del coordinador del
2026-11-27, pregunta Q5 del plan). Termina comprobando las versiones mínimas
(Engine 26, Compose 2.24) y diciendo que hay que salir y volver a entrar.
"""
text = SPEC.read_text()
assert text.count(OLD) == 1, "§3's prepare-host.sh paragraph is not there exactly once"
SPEC.write_text(text.replace(OLD, NEW))
print("design §3: prepare-host.sh makes no account")
TASK1_SPEC
python3 "$S/task1_spec.py"
grep -c 'create-user' docs/superpowers/specs/2026-11-27-despliegue-automatizado-design.md
```

Expected: `design §3: prepare-host.sh makes no account` y `0`.

- [ ] **Step 6: Commit**

```bash
git add deploy/scripts/prepare-host.sh backend/tests/test_prepare_host_script.py docs/superpowers/specs/2026-11-27-despliegue-automatizado-design.md
git commit -m "feat(deploy): prepare-host.sh, the runbook's P2 to P5 in one command

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013TnTdGAz4rSe1YRXXjEqyr"
```

---

### Task 2: `first-boot.sh`, primera parte — el estado del host, los pasos 1 a 3.6 y el desellado automático (oleada 1, en paralelo con las Tasks 1 y 4)

**Files:**
- Create: `deploy/scripts/first-boot.sh` (ejecutable)
- Create: `backend/tests/first_boot_support.py` (el arnés: un `docker` falso, los guiones hermanos simulados, las cuñas)
- Test: `backend/tests/test_first_boot_script.py`
- Modify: `deploy/scripts/preflight.sh` (una rama nueva antes de la pregunta al DNS), `backend/tests/test_preflight_script.py` (dos pruebas al final, una con dos casos)
- Modify: `docs/superpowers/specs/2026-11-27-despliegue-automatizado-design.md` (§2, el párrafo de `first-boot.sh` en §3, y §4: resolución Q6)

**Interfaces:**
- Consumes: `./scripts/new-env.sh --host <nombre> [--acme-email …] [--acme-ca-server …]`, `./scripts/build-images.sh` y `./scripts/preflight.sh` tal como están; `./scripts/openbao-seal.sh enable --key-file RUTA --yes` sin cambios (las partes, una por línea, por la entrada estándar cuando no es una terminal; escribe `COMPOSE_FILE` y `OPENBAO_UNSEAL_KEY_FILE` en `.env`); `deploy/openbao/its-app.hcl` y `gateway-issuer.hcl`, montados en `/bao/` del contenedor `openbao`.
- Produces:
  - **La línea de órdenes, que la Task 3 completa y la 5 y la 6 usan:** `./scripts/first-boot.sh [--host NOMBRE] [--acme-email DIRECCIÓN] [--acme-ca-server URL] [--tls-cafile PEM]` desde `deploy/` (o con su ruta desde cualquier sitio). `--host` solo vale para un `.env` nuevo (por defecto `rmm.novieri.com`); con `.env`, el host es su `ITS_PUBLIC_HOST`, y un `--host` distinto se niega. `--tls-cafile` se acepta y se resuelve a ruta absoluta antes del `cd` (la Task 3 se la pasa a `check-public-tls.sh`). Sale 0 con todo hecho, 1 con `FAILED`.
  - **Variables de entorno:** `ITS_FIRST_BOOT_ALLOW_NONTTY=1` (imprime partes y token sin terminal); `ITS_ETC_DIR` (por defecto `/etc/it-suite`: el archivo del token `gateway-issuer`, `$ITS_ETC_DIR/gateway-issuer.token`, y el archivo de clave del desellado, `--key-file "$ITS_ETC_DIR/openbao-unseal/unseal.key"`, el de `openbao-seal.sh`). La Task 3 añade `ITS_SYSTEMD_DIR` e `ITS_NO_SYSTEMCTL`. La cabecera del guion ya describe el guion entero, pasos 4 a 8 incluidos: la Task 3 no la toca.
  - **Líneas que leen otros**, exactas: `  skip    step 1: .env exists (new-env.sh never overwrites one), for <host>` (la Task 5 la exige en la CI); cada tramo ya hecho, `  skip    step <n>: …`; cada tramo que cambia algo, `  run     …`; las partes, `  share <n>     <parte>` (cinco espacios tras el número) y `  root token  <token>` (las lee `first_boot_answers.py`, Task 5); la petición `Type SAVED once the five shares and the root token are stored off this VPS: ` y las de partes `Unseal key share <n> of 3 (hidden): ` y `OpenBao root token, printed by step 3.2 (hidden): ` (por la entrada estándar; el texto de la pregunta solo se ve en una terminal). **El desellado, justo después del 3.6** (resolución Q6): la cabecera `OpenBao's auto-unseal, with a static key (GO-LIVE.md §4; decided 2026-11-27)`, la línea `  run     sudo ./scripts/openbao-seal.sh enable --key-file <archivo> --yes, the three shares on its stdin`; después `OpenBao restarts and unseals itself`, `  run     docker compose restart openbao` y `  ok      OpenBao restarted and came back unsealed by itself (Seal Type static, Sealed false): no share was typed` (Task 6); hechos ya, `  skip    auto-unseal: on, and OpenBao is unsealed with its static key` y `  skip    the restart that proves it ran when the auto-unseal was turned on`. Sin las tres partes en memoria (otra ejecución, que paró tras el 3.3 con OpenBao desellado), `  note    openbao-seal.sh needs three shares to move the seal, and this run holds none: type them` y las tres peticiones de partes. **Una segunda ejecución con todo hecho no imprime ninguna `  run     ` y sus diez primeras `  skip    ` son**, en este orden, `step 1`, `step 1b`, `step 2`, `step 3.1`, `step 3.2`, `step 3.3`, `step 3.4`, `step 3.6`, `auto-unseal` y `the restart that proves it…`. En esta tarea la última línea es `Steps 1 to 3.6 and the auto-unseal: done.`; la Task 3 la sustituye.
  - **Lo que el código deja para la Task 3 y para las pruebas de la 5:** `dc()` (`docker compose "$@" </dev/null`), `env_get`, `env_put`, `env_token_set`, `auto_unseal_on`, `follow_auto_unseal`, `bao_state` (pone `bao_up`, `bao_init`, `bao_sealed`, `bao_type`), `wait_unsealed`, `bao_with TOKEN ORDEN…`, `is_root_token`, `psql_admin`, `service_health`, `oneshot_exit`, `read_share N` (deja la parte en `$share`), `auto_unseal`, `restart_proof`, y los arreglos `shares` (las cinco, si esta ejecución las imprimió) y `typed` (las tres de 3.3, vacío tras el desellado). El bloque `main` acaba en `step_3_6`, `auto_unseal`, `restart_proof`: la Task 3 pone sus pasos detrás. Las pruebas de la Task 5 buscan en el código, sin comentarios, `dc exec -T openbao bao operator init -key-shares=5 -key-threshold=3`, `dc up openbao-init`, `dc exec -T openbao bao status -format=json`, `"initialized"`, `[ "$(stat -c %a .env)" = 600 ]` y `env_put ITS_OPENBAO_TOKEN "$app"`.
  - **`tests/first_boot_support.py`**: `Host(tmp_path)`, `Host.run(*args, stdin=…, env=…)`, `Host.state`, `Host.secrets` (`shares`, `root`, `its-app`, `gateway-issuer`, `code`), `Host.argv()`, `Host.all_secrets()`, `Host.files_holding(valor)`, `Host.write_env(**claves)`, `Host.env()`, y `lines(salida, prefijo)`. Simula el guion entero, pasos 4 a 8 incluidos, para que la Task 3 solo añada pruebas.
  - **`preflight.sh`:** con `ITS_PREFLIGHT_SKIP_DNS=1` y una CA de ACME que no es la de producción (`ITS_ACME_CA_SERVER` de `.env`: Pebble en la CI, *staging* en un ensayo), en lugar del registro `A` y el `AAAA` imprime `  note    <host> was not asked of DNS (ITS_PREFLIGHT_SKIP_DNS=1): the CI or a rehearsal, never a real host`; todo lo demás igual. **Con la de producción, `https://acme-v02.api.letsencrypt.org/directory`, o sin `ITS_ACME_CA_SERVER`** (que es la misma), se niega como sus otras negativas, con `ITS_PREFLIGHT_SKIP_DNS=1 with Let's Encrypt production as the ACME CA (ITS_ACME_CA_SERVER is <valor o unset>): …` (resolución Q4).

**Decisiones de la tarea.**
- **El estado mezclado, en tres formas** (diseño §3, «se niega si `.env` ya existe y OpenBao está inicializado con otro `.env`»), cada una con la orden para empezar de cero: (a) sin `.env` y con volúmenes del proyecto `it-suite` (un `.env` nuevo no abriría los de Postgres ni los de OpenBao: se dice cómo devolver el `.env` de su custodia o, destruyendo los volúmenes, cómo quitarlos aunque `docker compose` ya no pueda leer el archivo); (b) `.env` con tokens y un OpenBao sin inicializar; (c) `.env` con un `OPENBAO_TOKEN` que no es el token raíz de este OpenBao (se comprueba en cuanto está desellado). Un `.env` con los tokens en `change-me` y un OpenBao inicializado **no** es mezclado: es una ejecución que paró tras `bao operator init`, y se sigue.
- **«¿Está inicializado?» se pregunta con `bao status -format=json`**, nunca con `operator init -status` (M1).
- **El desellado automático va justo después del paso 3.6** (resolución Q6 del coordinador, 2026-11-27; el diseño §2, §3 y §4 lo dicen desde el Step 5). Así, si un paso del 4 al 7 falla (lo más probable en un VPS real es el 7: el registro `A`, el cortafuegos del proveedor), la siguiente ejecución encuentra OpenBao desellándose solo y no pide ninguna parte. Necesita OpenBao provisionado (3.6) y desellado con Shamir; si no, se niega con `sudo ./scripts/openbao-seal.sh status`.
- **`sudo env -u COMPOSE_FILE ./scripts/openbao-seal.sh enable --key-file … --yes`**, con las tres partes de 3.3 por su entrada estándar (M4). `--yes` porque la confirmación ya la dio el operador (diseño §2, «sin preguntar»); `openbao-seal.sh` sigue haciendo su instantánea, su migración y su propio reinicio de prueba. Y después, **un reinicio más, del propio `first-boot.sh`**, que comprueba `Sealed false` y `Seal Type static` (diseño §4). En una ejecución en la que el desellado ya estaba hecho, ni lo uno ni lo otro: `skip`. Si el guion heredó un `COMPOSE_FILE` (la CI), le añade `openbao/auto-unseal.yml` en cuanto `.env` lo tiene (M4).
- **`ITS_PREFLIGHT_SKIP_DNS=1` nunca contra Let's Encrypt de producción** (resolución Q4): un nombre que nadie preguntó al DNS gastaría los 5 certificados por semana que producción da a `rmm.novieri.com`, y sin `ITS_ACME_CA_SERVER` la CA es esa, la de un host real. La comprobación está en `preflight.sh`, no en `first-boot.sh`: así vale también para quien lance `preflight.sh` a mano.
- **SAVED, en mayúsculas**, y la pantalla se borra después (también el historial de la terminal, `\033[3J`): «enseñar una vez» del diseño §2. Si la entrada se acaba antes, el guion para diciendo que las partes están arriba, en pantalla, y que la próxima vez pedirá el token raíz y tres partes.
- **Cada parte tecleada se compara con las cinco impresas** y no se admite una repetida (M3): un error de copia se ve al teclearla, no tres partes después. Sin las cinco en memoria (otra ejecución), hasta tres rondas de tres, reiniciando el progreso antes de cada una.
- **El token raíz nunca se vuelve a pedir si se tiene**: de la memoria (esta ejecución), de `.env` (`OPENBAO_TOKEN`, que el paso 3.5 escribe como hoy), o, solo si no está en ninguno, con entrada oculta y comprobado contra OpenBao.
- **Los dos tokens del 3.4 se crean solo si faltan**: el de `its-app` si `.env` no lo tiene, el de `gateway-issuer` si su archivo no existe. Primero el archivo, después `.env`: si algo falla entre los dos, la siguiente ejecución lo ve.
- **El arnés registra la línea de órdenes de todo**: `docker`, `sudo` y los guiones hermanos falsos escriben la suya; las órdenes reales van detrás de una cuña que la escribe y llama a la de `/usr/bin` o `/bin`. La prueba de los secretos busca cada parte, el token raíz, los dos tokens y el código en ese registro (ninguno) y en cada archivo de la máquina simulada (solo los que el runbook nombra). Con `TMPDIR` dentro de la máquina simulada, un temporal de bash también se vería.

- [ ] **Step 0: La worktree**

Desde la raíz de la worktree:

```bash
git fetch -q origin claude/compassionate-bardeen-vgtwus
git log --oneline -1
git merge-base --is-ancestor HEAD origin/claude/compassionate-bardeen-vgtwus && echo ON-PROJECT-BRANCH
git merge-base --is-ancestor 6f616aa HEAD && echo HAS-SPEC
git diff --quiet 6f616aa HEAD -- backend agent deploy .github && echo CODE-AS-MEASURED
```

Expected: `6f616aa docs(spec): the first boot of rmm.novieri.com in three scripts` (o el commit posterior de la rama del coordinador), `ON-PROJECT-BRANCH`, `HAS-SPEC`, `CODE-AS-MEASURED`. Si falta alguna, se para y se avisa al coordinador. Después, la preparación de Global Constraints.

- [ ] **Step 1: El arnés y las pruebas**

Crear `backend/tests/first_boot_support.py`:

```python
"""A stand-in host for `deploy/scripts/first-boot.sh` (design
2026-11-27-despliegue-automatizado §5): a fake `docker` that plays compose,
OpenBao and Postgres from one JSON state file, stubs for the sibling scripts
and for `sudo`, `systemctl` and `openssl`, and a shim in front of every other
command that logs its argv -- the `ps` a test reads to prove no secret was
ever an argument.

`Host(tmp_path)` builds it: a scratch checkout (`repo/deploy/scripts/` with the
real first-boot.sh, a git commit, the migration directory), `etc/` and
`systemd/` for ITS_ETC_DIR and ITS_SYSTEMD_DIR, and a PATH of nothing but the
shims and the stubs, so a command the script uses without the test knowing
fails with "command not found" instead of passing unseen. The secrets the
fake OpenBao hands out are fixed per host and listed in `Host.secrets`.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "scripts" / "first-boot.sh"
PY = sys.executable
BASH = shutil.which("bash") or "/bin/bash"

#: Every external command the script, its stubs and bash reach, each behind a
#: logging shim. A new one in the script fails a test until it is listed.
SHIMMED = (
    "cat", "chmod", "cut", "diff", "dirname", "env", "find", "git", "grep", "head",
    "install", "mkdir", "mv", "readlink", "rm", "sed", "sort", "stat", "tail",
    "tee", "test", "tr",
)  # fmt: skip

#: The long-running services `docker compose up -d` starts, and the one-shots.
LONG = ("api", "backup-replicator", "gateway", "minio", "openbao", "postgres", "redis",
        "traefik", "web", "worker")  # fmt: skip
ONESHOTS = ("migrate", "postgres-grants", "openbao-init")
HEALTHCHECKED = {"api", "gateway", "minio", "openbao", "postgres", "web", "worker"}
NEWEST_REVISION = "0248"

FAKE_DOCKER = r'''
import json, os, sys

STATE = os.environ["FAKE_STATE"]
with open(STATE) as fh:
    s = json.load(fh)
argv = sys.argv[1:]
with open(os.environ["ARGV_LOG"], "a") as log:
    log.write("docker " + " ".join(argv) + "\n")
    log.write("  COMPOSE_FILE=" + os.environ.get("COMPOSE_FILE", "") + "\n")


def save():
    with open(STATE, "w") as fh:
        json.dump(s, fh)


def out(text):
    sys.stdout.write(text)


def svc(name):
    return s["services"].setdefault(name, {"state": "absent", "health": "", "exit": 0,
                                           "restarts": 0, "id": "c" + str(len(s["services"]))})


def run_long(name):
    service = svc(name)
    if service["state"] != "running":
        service["id"] = "c%d-%s" % (s["serial"], name)
        s["serial"] += 1
    service["state"] = "running"
    service["health"] = "healthy" if name in s["healthchecked"] else ""


def run_once(name):
    service = svc(name)
    service["id"] = "c%d-%s" % (s["serial"], name)
    s["serial"] += 1
    service["state"] = "exited"
    service["exit"] = s["oneshot_exit"].get(name, 0)
    if name == "migrate" and service["exit"] == 0:
        s["pg"]["alembic"] = s["newest"]
    if name == "postgres-grants" and service["exit"] == 0:
        s["pg"]["narrow"] = "t"


def bao_json():
    b = s["bao"]
    return json.dumps({"type": b["type"], "initialized": b["init"], "sealed": b["sealed"],
                       "t": 3 if b["init"] else 0, "n": 5 if b["init"] else 0}, indent=2) + "\n"


def bao(args, token):
    b = s["bao"]
    if args[:2] == ["token", "lookup"]:
        if token == b["root"] and b["init"]:
            out(json.dumps({"data": {"policies": ["root"]}, "policies": ["root"]}, indent=2) + "\n")
            return 0
        return 2
    if token != b["root"]:
        sys.stderr.write("permission denied\n")
        return 2
    if args[:2] == ["policy", "write"]:
        b["policies"].append(args[2])
        out("Success! Uploaded policy: %s\n" % args[2])
        return 0
    if args[:2] == ["auth", "tune"]:
        out("Success! Tuned the auth method at: token/\n")
        return 0
    if args[:2] == ["token", "create"]:
        policy = next(a.split("=", 1)[1] for a in args if a.startswith("-policy="))
        value = s["issue"][policy]
        b["tokens"][value] = policy
        out(value + "\n")
        return 0
    return 1


def compose(args):
    sub, rest = args[0], args[1:]
    if sub == "ps":
        names = [a for a in rest if not a.startswith("-") and "{{" not in a]
        fmt = rest[rest.index("--format") + 1] if "--format" in rest else None
        shown = [(n, v) for n, v in sorted(s["services"].items())
                 if v["state"] != "absent" and (not names or n in names)]
        if "--status" in rest:
            shown = [(n, v) for n, v in shown if v["state"] == "running"]
        for name, v in shown:
            if fmt is None:
                out(v["id"] + "\n")
            elif fmt.startswith("table"):
                out("%s\t%s\n" % (name, v["state"]))
            else:
                line = (fmt.replace("{{.Service}}", name).replace("{{.State}}", v["state"])
                        .replace("{{.Health}}", v["health"])
                        .replace("{{.ExitCode}}", str(v["exit"])))
                out(line + "\n")
        return 0
    if sub == "up":
        names = [a for a in rest if not a.startswith("-") and not a.isdigit()]
        if not names:
            for name in s["long"]:
                run_long(name)
            for name in s["oneshots"]:
                if svc(name)["state"] != "exited" or svc(name)["exit"] != 0:
                    run_once(name)
            return 0
        for name in names:
            (run_once if name in s["oneshots"] else run_long)(name)
        return 0
    if sub == "restart":
        for name in rest:
            svc(name)["restarts"] += 1
            if name == "openbao":
                s["bao"]["sealed"] = s["bao"]["type"] != "static"
        return 0
    if sub == "exec":
        rest = [a for a in rest if a != "-T"]
        name, cmd = rest[0], rest[1:]
        if svc(name)["state"] != "running":
            sys.stderr.write('service "%s" is not running\n' % name)
            return 1
        if name == "openbao":
            b = s["bao"]
            if cmd[:2] == ["bao", "status"]:
                out(bao_json())
                return 2 if b["sealed"] else 0
            if cmd[:3] == ["bao", "operator", "init"]:
                if b["init"]:
                    return 2
                b["init"] = True
                for i, share in enumerate(b["shares"], 1):
                    out("Unseal Key %d: %s\n" % (i, share))
                out("\nInitial Root Token: %s\n\nVault initialized with 5 key shares\n" % b["root"])
                return 0
            if cmd[:3] == ["bao", "write", "-format=json"] and cmd[3] == "sys/unseal":
                body = json.loads(sys.stdin.read())
                if body.get("reset"):
                    b["progress"] = []
                    return 0
                key = body["key"]
                if key not in b["progress"]:
                    b["progress"].append(key)
                if len(b["progress"]) >= 3:
                    good = all(k in b["shares"] for k in b["progress"])
                    b["progress"] = []
                    if not good:
                        return 2
                    b["sealed"] = False
                return 0
            if cmd[:2] == ["sh", "-c"]:
                token = sys.stdin.readline().strip()
                return bao(cmd[5:], token)
            if cmd[:2] == ["id", "-u"] or cmd[:2] == ["id", "-g"]:
                out("100\n")
                return 0
        if name == "postgres" and cmd[0] == "psql":
            sql = cmd[-1]
            if "pg_roles" in sql:
                out("%d\n" % s["pg"]["roles"])
            elif "alembic_version" in sql:
                if not s["pg"]["alembic"]:
                    sys.stderr.write('relation "alembic_version" does not exist\n')
                    return 1
                out(s["pg"]["alembic"] + "\n")
            elif "has_table_privilege" in sql:
                out(s["pg"]["narrow"] + "\n")
            return 0
        if name == "traefik" and cmd[0] == "stat":
            out(s["acme_mode"] + "\n")
            return 0
        if name == "api" and cmd[0] == "python":
            if "setup/status" in cmd[-1]:
                out(("True" if s["setup"]["needed"] else "False") + "\n")
            else:
                out("30 30\n")
            return 0
        if name == "api" and cmd[0] == "cat":
            out(s["setup"]["code"] + "\n")
            return 0
        return 0
    if sub == "run":
        script = rest[-1]
        if "--entrypoint" in rest and rest[rest.index("--entrypoint") + 1] == "python":
            token = sys.stdin.readline().strip()
            if s["bao"]["tokens"].get(token) != "gateway-issuer":
                sys.stderr.write("403 from OpenBao\n")
                return 1
            s["files"]["gateway"] = True
            out("gateway.crt  /var/lib/its/tls/gateway.crt  (CN=%s, 365d)\n" % rest[-1])
            return 0
        if "audit-hmac.key" in script:
            if script.startswith("test -s"):
                return 0 if s["files"]["audit"] else 1
            if script.startswith("set -C"):
                if s["files"]["audit"]:
                    sys.stderr.write("sh: cannot create audit-hmac.key: File exists\n")
                    return 1
                s["files"]["audit"] = True
                return 0
            out("-rw------- 1 its its 32 Nov 27 10:00 /var/lib/its/secrets/audit-hmac.key\n")
            return 0
        if "gateway.crt" in script:
            return 0 if s["files"]["gateway"] else 1
        return 0
    return 0


rc = 0
if argv[:2] == ["volume", "ls"]:
    out("".join(v + "\n" for v in s["volumes"]))
elif argv[:2] == ["image", "inspect"]:
    rc = 0 if all(ref in s["images"] for ref in argv[2:]) else 1
elif argv[0] == "inspect":
    fmt = argv[argv.index("-f") + 1]
    by_id = {v["id"]: (n, v) for n, v in s["services"].items()}
    for ident in [a for a in argv[1:] if a not in ("-f", fmt)]:
        name, v = by_id[ident]
        if "ExitCode" in fmt:
            out("%d\n" % v["exit"])
        else:
            out("/it-suite-%s-1 %d\n" % (name, v["restarts"] + s["restart_drift"]))
    s["restart_drift"] += s["restart_step"]
elif argv[0] == "compose":
    rc = compose(argv[1:])
save()
sys.exit(rc)
'''

#: The sibling scripts first-boot.sh runs, stood in for: their own behaviour
#: is their own tests' (test_new_env_script.py, test_preflight_script.py,
#: test_check_public_tls.py, test_openbao_seal_script.py).
FAKE_NEW_ENV = r'''
import os, shutil, sys
with open(os.environ["ARGV_LOG"], "a") as log:
    log.write("new-env.sh " + " ".join(sys.argv[1:]) + "\n")
os.umask(0o077)
shutil.copyfile(os.environ["FAKE_ENV_TEMPLATE"], ".env")
os.chmod(".env", 0o600)
print("wrote .env (mode 600)")
'''

FAKE_BUILD_IMAGES = r'''
import json, os, subprocess
with open(os.environ["ARGV_LOG"], "a") as log:
    log.write("build-images.sh\n")
tag = subprocess.run(["git", "-C", "..", "rev-parse", "--short", "HEAD"],
                     capture_output=True, text=True, check=True).stdout.strip()
with open(os.environ["FAKE_STATE"]) as fh:
    s = json.load(fh)
s["images"] = ["itsuite/backend:" + tag, "itsuite/web:" + tag]
with open(os.environ["FAKE_STATE"], "w") as fh:
    json.dump(s, fh)
print("Recent tags:\n      itsuite/backend:" + tag)
'''

FAKE_SIMPLE = r'''
import os, sys
name = os.path.basename(sys.argv[0])
with open(os.environ["ARGV_LOG"], "a") as log:
    log.write(name + " " + " ".join(sys.argv[1:]) + "\n")
print("  ok      %s (stub)" % name)
sys.exit(int(os.environ.get("FAKE_RC_" + name.split(".")[0].replace("-", "_").upper(), "0")))
'''

FAKE_OPENBAO_SEAL = r'''
import json, os, sys
with open(os.environ["ARGV_LOG"], "a") as log:
    log.write("openbao-seal.sh " + " ".join(sys.argv[1:]) + "\n")
    log.write("  COMPOSE_FILE=" + os.environ.get("COMPOSE_FILE", "") + "\n")
with open(os.environ["FAKE_STATE"]) as fh:
    s = json.load(fh)
given = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
if os.environ.get("FAKE_SEAL_FAIL") == "1":
    print("FAILED  OpenBao did not come back", file=sys.stderr)
    sys.exit(1)
s["seal_stdin"] = len(given)
if len(given) != 3 or len(set(given)) != 3 or not all(g in s["bao"]["shares"] for g in given):
    print("FAILED  still sealed after the shares given", file=sys.stderr)
    sys.exit(1)
key = sys.argv[sys.argv.index("--key-file") + 1]
os.makedirs(os.path.dirname(key), exist_ok=True)
with open(key, "wb") as fh:
    fh.write(os.urandom(32))
lines = [l for l in open(".env").read().splitlines()
         if not l.startswith(("COMPOSE_FILE=", "OPENBAO_UNSEAL_KEY_FILE="))]
lines += ["COMPOSE_FILE=docker-compose.yml:openbao/auto-unseal.yml",
          "OPENBAO_UNSEAL_KEY_FILE=" + key]
open(".env", "w").write("\n".join(lines) + "\n")
s["bao"]["type"] = "static"
with open(os.environ["FAKE_STATE"], "w") as fh:
    json.dump(s, fh)
print("Auto-unseal is ON")
'''

FAKE_SUDO = r'''#!/bin/sh
printf 'sudo %s\n' "$*" >> "$ARGV_LOG"
exec "$@"
'''

FAKE_SYSTEMCTL = r'''
import os, sys
with open(os.environ["ARGV_LOG"], "a") as log:
    log.write("systemctl " + " ".join(sys.argv[1:]) + "\n")
if sys.argv[1:2] == ["is-enabled"]:
    print("disabled")
'''

FAKE_OPENSSL = r'''
import os, sys
with open(os.environ["ARGV_LOG"], "a") as log:
    log.write("openssl " + " ".join(sys.argv[1:]) + "\n")
sys.stdin.read()
if sys.argv[1] == "x509":
    print("issuer=CN = Novieri RMM internal CA")
    print("X509v3 Subject Alternative Name: ")
    print("    DNS:" + os.environ.get("FAKE_GATEWAY_HOST", "rmm.novieri.com"))
'''


def real_command(name: str, path: str) -> str | None:
    """The system's own binary, before anything a session put on PATH: a
    wrapper that runs `name` again by name would loop through the shim."""
    for directory in ("/usr/bin", "/bin"):
        candidate = Path(directory) / name
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(name, path=path)


def _exe(path: Path, body: str, python: bool = True) -> None:
    path.write_text((f"#!{PY}\n" if python else "") + body.lstrip("\n"))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _secret() -> str:
    return secrets.token_urlsafe(32)


class Host:
    """One scratch host. `run()` runs first-boot.sh in it; `state` is the fake's."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.repo = tmp / "repo"
        self.deploy = self.repo / "deploy"
        scripts = self.deploy / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "first-boot.sh").write_bytes(SCRIPT.read_bytes())
        _exe(scripts / "new-env.sh", FAKE_NEW_ENV)
        _exe(scripts / "build-images.sh", FAKE_BUILD_IMAGES)
        for name in ("preflight.sh", "check-public-tls.sh"):
            _exe(scripts / name, FAKE_SIMPLE)
        _exe(scripts / "openbao-seal.sh", FAKE_OPENBAO_SEAL)
        (scripts / "issue-gateway-cert.py").write_text("# stand-in\n")
        versions = self.repo / "backend" / "migrations" / "versions"
        versions.mkdir(parents=True)
        for name in ("0001_core_schema.py", f"{NEWEST_REVISION}_newest.py", "__init__.py"):
            (versions / name).write_text("")
        (self.repo / ".gitignore").write_text(".env\n")
        git = ["git", "-C", str(self.repo), "-c", "user.email=t@example.org", "-c", "user.name=t"]
        subprocess.run([*git[:3], "init", "-q"], check=True)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "scratch"], check=True)
        self.tag = subprocess.run(
            [*git[:3], "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()

        self.etc = tmp / "etc"
        self.units = tmp / "systemd"
        self.home = tmp / "home"
        self.tmpdir = tmp / "tmpdir"
        for d in (self.home, self.tmpdir):
            d.mkdir()
        self.log = tmp / "argv.log"
        self.state_file = tmp / "fake-state.json"
        self.env_template = tmp / "env.template"
        self.env_template.write_text(
            "ITS_PUBLIC_HOST=rmm.novieri.com\n"
            "ITS_PUBLIC_CONSOLE_URL=https://rmm.novieri.com\n"
            "ITS_ACME_EMAIL=ops@novieri.example\n"
            "IT_SUITE_VERSION=dev\n"
            "POSTGRES_ADMIN_PASSWORD=" + "a" * 48 + "\n"
            "ITS_OPENBAO_TOKEN=change-me\n"
            "OPENBAO_TOKEN=change-me\n"
        )

        self.bin = tmp / "bin"
        self.bin.mkdir()
        real_path = os.environ["PATH"]
        for name in SHIMMED:
            real = real_command(name, real_path)
            assert real, f"{name} is not on this machine's PATH"
            _exe(
                self.bin / name,
                f'#!/bin/sh\nprintf \'%s %s\\n\' {name} "$*" >> "$ARGV_LOG"\nexec {real} "$@"\n',
                python=False,
            )
        _exe(self.bin / "sleep", "#!/bin/sh\nexit 0\n", python=False)
        _exe(self.bin / "docker", FAKE_DOCKER)
        _exe(self.bin / "sudo", FAKE_SUDO, python=False)
        _exe(self.bin / "systemctl", FAKE_SYSTEMCTL)
        _exe(self.bin / "openssl", FAKE_OPENSSL)

        self.secrets = {
            "shares": [_secret() for _ in range(5)],
            "root": "s." + _secret(),
            "its-app": "s." + _secret(),
            "gateway-issuer": "s." + _secret(),
            "code": "CODE" + secrets.token_hex(8).upper(),
        }
        self.state: dict[str, Any] = {
            "volumes": [],
            "images": [],
            "services": {},
            "serial": 1,
            "long": list(LONG),
            "oneshots": list(ONESHOTS),
            "healthchecked": sorted(HEALTHCHECKED),
            "oneshot_exit": {},
            "newest": NEWEST_REVISION,
            "bao": {
                "init": False,
                "sealed": True,
                "type": "shamir",
                "progress": [],
                "shares": self.secrets["shares"],
                "root": self.secrets["root"],
                "tokens": {},
                "policies": [],
            },
            "issue": {
                "its-app": self.secrets["its-app"],
                "gateway-issuer": self.secrets["gateway-issuer"],
            },
            "pg": {"roles": 2, "alembic": "", "narrow": "f"},
            "files": {"audit": False, "gateway": False},
            "acme_mode": "600",
            "setup": {"needed": True, "code": self.secrets["code"]},
            "restart_drift": 0,
            "restart_step": 0,
        }
        self.save()

    # ----------------------------------------------------------------- state

    def save(self) -> None:
        self.state_file.write_text(json.dumps(self.state))

    def load(self) -> dict[str, Any]:
        self.state = json.loads(self.state_file.read_text())
        return self.state

    def write_env(self, **values: str) -> None:
        """`.env` as new-env.sh writes it, with VALUES on top (step 1 done)."""
        lines = self.env_template.read_text().splitlines()
        keys = {line.split("=", 1)[0] for line in lines}
        lines = [f"{k}={values[k]}" if k in values else line
                 for line in lines for k in [line.split("=", 1)[0]]]  # fmt: skip
        lines += [f"{k}={v}" for k, v in values.items() if k not in keys]
        env = self.deploy / ".env"
        env.write_text("\n".join(lines) + "\n")
        env.chmod(0o600)

    def env(self) -> dict[str, str]:
        return dict(
            line.split("=", 1)
            for line in (self.deploy / ".env").read_text().splitlines()
            if "=" in line
        )

    # ------------------------------------------------------------------ run

    def run(
        self, *args: str, stdin: str = "", env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.save()
        self.log.write_text("")
        environ = {
            "PATH": str(self.bin),
            "HOME": str(self.home),
            "TMPDIR": str(self.tmpdir),
            "LANG": "C.UTF-8",
            "ARGV_LOG": str(self.log),
            "FAKE_STATE": str(self.state_file),
            "FAKE_ENV_TEMPLATE": str(self.env_template),
            "ITS_ETC_DIR": str(self.etc),
            "ITS_SYSTEMD_DIR": str(self.units),
            "ITS_NO_SYSTEMCTL": "1",
            **(env or {}),
        }
        done = subprocess.run(
            [BASH, str(self.deploy / "scripts" / "first-boot.sh"), *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=environ,
            cwd=self.tmp,
            timeout=100,
        )
        self.load()
        return done

    def argv(self) -> str:
        return self.log.read_text()

    def all_secrets(self) -> list[str]:
        s = self.secrets
        return [*s["shares"], s["root"], s["its-app"], s["gateway-issuer"], s["code"]]

    def files_holding(self, value: str) -> list[str]:
        """Every file under the scratch host that holds VALUE, but the fake's
        own state (the stand-in OpenBao) and the argv log (read on its own)."""
        found = []
        for path in self.tmp.rglob("*"):
            if not path.is_file() or path in (self.state_file, self.log):
                continue
            if ".git" in path.parts:
                continue
            if value in path.read_text(errors="replace"):
                found.append(str(path.relative_to(self.tmp)))
        return found


def lines(output: str, prefix: str) -> list[str]:
    """The `  run     ` / `  skip    ` / `  ok      ` lines, prefix removed."""
    tag = f"  {prefix:<8}"
    return [line[len(tag):] for line in output.splitlines() if line.startswith(tag)]
```

Crear `backend/tests/test_first_boot_script.py`:

```python
"""`deploy/scripts/first-boot.sh`, on a stand-in host (design
2026-11-27-despliegue-automatizado §3, §5; `tests/first_boot_support.py`).

The CI's `deploy` job runs the script end to end against a real stack and
Pebble. What only a stand-in can show is pinned here: the refusals -- no
terminal, a mixed state, xtrace -- that a happy run never reaches; that a run
again skips every step, saying so; and that no secret is ever an argument of
any process (every command but bash's builtins goes through a logging shim) or
lands in a file the runbook does not name.
"""

import re
import shutil
import subprocess
from pathlib import Path

from tests.first_boot_support import SCRIPT, Host, lines

ALLOW = {"ITS_FIRST_BOOT_ALLOW_NONTTY": "1"}
RESTART_SKIP = "the restart that proves it ran when the auto-unseal was turned on"


def _saved_and_three(host: Host, picks: tuple[int, ...] = (1, 3, 4)) -> str:
    shares = host.secrets["shares"]
    return "SAVED\n" + "".join(shares[i] + "\n" for i in picks)


def test_the_script_parses() -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_it_refuses_to_run_under_xtrace(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.log.write_text("")
    done = subprocess.run(
        [shutil.which("bash") or "/bin/bash", "-x", str(host.deploy / "scripts" / "first-boot.sh")],
        capture_output=True,
        text=True,
        env={"PATH": str(host.bin), "ARGV_LOG": str(host.log)},
    )
    assert done.returncode == 1
    assert "refusing to run under xtrace" in done.stderr
    assert "docker" not in host.argv()


def test_nothing_in_it_traces_or_spills_a_here_string() -> None:
    """`set -x` would print every secret; a here-string or an unquoted
    here-document can land in a temporary file (bash before 5.1 always, after
    it above the pipe's size). The script uses neither."""
    text = SCRIPT.read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"set -[a-wyz]*x|set -o xtrace|BASH_XTRACEFD", code)
    assert "<<<" not in code
    for heredoc in re.findall(r"<<-?\s*(\S+)", code):
        assert heredoc.startswith("'"), heredoc


def test_a_new_host_runs_every_step_to_the_auto_unseal_and_asks_only_what_it_must(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    done = host.run(stdin=_saved_and_three(host), env=ALLOW)
    assert done.returncode == 0, done.stdout + done.stderr
    ran = lines(done.stdout, "run")
    assert [r.split(":")[0].split(",")[0] for r in ran][:5] == [
        "./scripts/new-env.sh --host rmm.novieri.com",
        "./scripts/build-images.sh",
        "docker compose up -d --wait postgres",
        "docker compose up -d openbao",
        "bao operator init -key-shares=5 -key-threshold=3",
    ]
    assert "docker compose up openbao-init" in ran
    shares = host.secrets["shares"]
    for n, share in enumerate(shares, 1):
        assert f"  share {n}     {share}" in done.stdout
    assert f"  root token  {host.secrets['root']}" in done.stdout
    env = host.env()
    assert env["ITS_OPENBAO_TOKEN"] == host.secrets["its-app"]
    assert env["OPENBAO_TOKEN"] == host.secrets["root"]
    assert env["IT_SUITE_VERSION"] == host.tag
    assert "change-me" not in (host.deploy / ".env").read_text()
    token = host.etc / "gateway-issuer.token"
    assert token.read_text() == host.secrets["gateway-issuer"] + "\n"
    assert oct(token.stat().st_mode & 0o777) == "0o600"
    assert host.state["bao"]["sealed"] is False
    assert host.state["bao"]["policies"] == ["its-app", "gateway-issuer"]
    # Right after 3.6 (coordinator's ruling Q6): the auto-unseal, with the three
    # shares of step 3.3 on openbao-seal.sh's stdin, and the restart that proves it.
    assert ran.index("docker compose up openbao-init") < ran.index("docker compose restart openbao")
    assert any(r.startswith("sudo ./scripts/openbao-seal.sh enable --key-file ") for r in ran)
    assert host.state["seal_stdin"] == 3
    assert host.state["bao"]["type"] == "static"
    assert env["COMPOSE_FILE"] == "docker-compose.yml:openbao/auto-unseal.yml"
    assert env["OPENBAO_UNSEAL_KEY_FILE"] == str(host.etc / "openbao-unseal" / "unseal.key")
    assert "ok      OpenBao restarted and came back unsealed by itself" in done.stdout


def test_a_second_run_skips_every_step_to_the_auto_unseal(tmp_path: Path) -> None:
    host = Host(tmp_path)
    assert host.run(stdin=_saved_and_three(host), env=ALLOW).returncode == 0
    again = host.run()
    assert again.returncode == 0, again.stdout + again.stderr
    assert lines(again.stdout, "run") == []
    skipped = [s.split(":")[0] for s in lines(again.stdout, "skip")]
    # The first ten: the steps Task 3 adds after them skip too, in their turn.
    assert skipped[:10] == [
        "step 1", "step 1b", "step 2", "step 3.1", "step 3.2", "step 3.3", "step 3.4",
        "step 3.6", "auto-unseal", RESTART_SKIP,
    ]  # fmt: skip
    for changing in ("compose restart", "openbao-seal.sh", "operator init", "tee "):
        assert changing not in host.argv(), changing


def test_no_secret_is_an_argument_or_in_a_file_the_runbook_does_not_name(tmp_path: Path) -> None:
    host = Host(tmp_path)
    done = host.run(stdin=_saved_and_three(host), env=ALLOW)
    assert done.returncode == 0, done.stdout + done.stderr
    argv = host.argv()
    assert argv.count("\n") > 50, "the shims logged nothing: the check is blind"
    for value in host.all_secrets():
        assert value not in argv
    allowed = {
        host.secrets["root"]: ["repo/deploy/.env"],
        host.secrets["its-app"]: ["repo/deploy/.env"],
        host.secrets["gateway-issuer"]: ["etc/gateway-issuer.token"],
    }
    for value in host.all_secrets():
        assert host.files_holding(value) == allowed.get(value, []), value


def test_the_shares_are_printed_to_a_terminal_only(tmp_path: Path) -> None:
    host = Host(tmp_path)
    done = host.run(stdin="")
    assert done.returncode == 1
    assert "the output is not a terminal" in done.stderr
    assert "operator init" not in host.argv()
    assert host.state["bao"]["init"] is False


def test_a_typed_share_must_be_one_of_the_five_and_not_typed_twice(tmp_path: Path) -> None:
    host = Host(tmp_path)
    shares = host.secrets["shares"]
    typed = ("not-a-share", shares[0], shares[0], shares[2], shares[3])
    stdin = "SAVED\n" + "".join(x + "\n" for x in typed)
    done = host.run(stdin=stdin, env=ALLOW)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "that is not one of the five shares printed above" in done.stdout
    assert "that share was typed already" in done.stdout
    assert host.state["bao"]["sealed"] is False


def test_saved_is_asked_until_it_is_typed(tmp_path: Path) -> None:
    host = Host(tmp_path)
    shares = host.secrets["shares"]
    done = host.run(stdin="yes\nsaved\nSAVED\n" + "".join(s + "\n" for s in shares[:3]), env=ALLOW)
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.count("type SAVED, in capitals, to go on") == 2


def test_volumes_without_an_env_are_a_mixed_state(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.state["volumes"] = ["it-suite_openbao-data", "it-suite_postgres-data"]
    done = host.run(env=ALLOW)
    assert done.returncode == 1
    assert "mixed state" in done.stderr and "it-suite_openbao-data" in done.stderr
    assert "docker volume ls -q --filter label=com.docker.compose.project=it-suite" in done.stderr
    assert not (host.deploy / ".env").exists()
    assert "new-env.sh" not in host.argv() and "compose up" not in host.argv()


def test_tokens_in_env_and_an_openbao_never_initialised_are_a_mixed_state(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.write_env(IT_SUITE_VERSION=host.tag, ITS_OPENBAO_TOKEN="s.old", OPENBAO_TOKEN="s.oldroot")
    host.state["images"] = [f"itsuite/backend:{host.tag}", f"itsuite/web:{host.tag}"]
    done = host.run(env=ALLOW)
    assert done.returncode == 1
    assert "mixed state: .env holds OpenBao tokens" in done.stderr
    assert "docker compose down -v && rm .env" in done.stderr
    assert "operator init" not in host.argv()


def test_a_root_token_from_another_openbao_is_a_mixed_state(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.write_env(IT_SUITE_VERSION=host.tag, ITS_OPENBAO_TOKEN="s.old", OPENBAO_TOKEN="s.oldroot")
    host.state["images"] = [f"itsuite/backend:{host.tag}", f"itsuite/web:{host.tag}"]
    host.state["bao"].update(init=True, sealed=False)
    host.state["services"]["openbao"] = {
        "state": "running", "health": "healthy", "exit": 0, "restarts": 0, "id": "c-openbao"
    }  # fmt: skip
    done = host.run(env=ALLOW)
    assert done.returncode == 1
    assert "OPENBAO_TOKEN in .env is not this OpenBao's root token" in done.stderr


def test_a_run_after_init_asks_for_three_shares_and_the_root_token(tmp_path: Path) -> None:
    """A run that stopped after `bao operator init`: the shares and the root
    token are no longer in memory. It never initialises again."""
    host = Host(tmp_path)
    host.write_env(IT_SUITE_VERSION=host.tag)
    host.state["images"] = [f"itsuite/backend:{host.tag}", f"itsuite/web:{host.tag}"]
    host.state["bao"]["init"] = True
    shares = host.secrets["shares"]
    stdin = "".join(s + "\n" for s in (shares[4], shares[1], shares[0], host.secrets["root"]))
    done = host.run(stdin=stdin)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "skip    step 3.2: OpenBao is initialised" in done.stdout
    assert "operator init" not in host.argv()
    assert host.env()["OPENBAO_TOKEN"] == host.secrets["root"]


def test_a_wrong_root_token_is_refused(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.write_env(IT_SUITE_VERSION=host.tag)
    host.state["images"] = [f"itsuite/backend:{host.tag}", f"itsuite/web:{host.tag}"]
    host.state["bao"].update(init=True, sealed=False)
    done = host.run(stdin="s.not-the-root\n")
    assert done.returncode == 1
    assert "that is not this OpenBao's root token" in done.stderr
    assert host.env()["OPENBAO_TOKEN"] == "change-me"


def test_wrong_shares_are_asked_again_three_rounds_then_refused(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.write_env(IT_SUITE_VERSION=host.tag)
    host.state["images"] = [f"itsuite/backend:{host.tag}", f"itsuite/web:{host.tag}"]
    host.state["bao"]["init"] = True
    done = host.run(stdin="".join(f"bad-{n}\n" for n in range(9)))
    assert done.returncode == 1
    assert done.stderr.count("OpenBao is still sealed: one of those three") == 3
    assert "still sealed after three rounds" in done.stderr


def test_the_preflight_refusing_stops_before_postgres(tmp_path: Path) -> None:
    host = Host(tmp_path)
    done = host.run(env={**ALLOW, "FAKE_RC_PREFLIGHT": "1"})
    assert done.returncode == 1
    assert "the pre-flight refused" in done.stderr
    assert "compose up" not in host.argv()


def test_an_inherited_compose_file_gains_the_auto_unseal_overlay(tmp_path: Path) -> None:
    """The CI's COMPOSE_FILE (the Pebble overlay) wins over .env's: after the
    auto-unseal, every docker call carries openbao/auto-unseal.yml too, and
    openbao-seal.sh itself sees no inherited COMPOSE_FILE at all."""
    host = Host(tmp_path)
    pebble = "docker-compose.yml:ci/acme-pebble.yml"
    done = host.run(stdin=_saved_and_three(host), env={**ALLOW, "COMPOSE_FILE": pebble})
    assert done.returncode == 0, done.stdout + done.stderr
    log = host.argv().splitlines()
    seal = next(i for i, line in enumerate(log) if line.startswith("openbao-seal.sh enable"))
    assert log[seal + 1] == "  COMPOSE_FILE="
    before = {line for line in log[:seal] if line.startswith("  COMPOSE_FILE=")}
    after = {line for line in log[seal + 2 :] if line.startswith("  COMPOSE_FILE=")}
    assert before == {f"  COMPOSE_FILE={pebble}"}
    assert after == {f"  COMPOSE_FILE={pebble}:openbao/auto-unseal.yml"}


def test_without_an_inherited_compose_file_none_is_set(tmp_path: Path) -> None:
    host = Host(tmp_path)
    done = host.run(stdin=_saved_and_three(host), env=ALLOW)
    assert done.returncode == 0, done.stdout + done.stderr
    composed = {line for line in host.argv().splitlines() if line.startswith("  COMPOSE_FILE=")}
    assert composed == {"  COMPOSE_FILE="}


def test_a_run_after_a_failed_auto_unseal_asks_for_three_shares(tmp_path: Path) -> None:
    host = Host(tmp_path)
    first = host.run(stdin=_saved_and_three(host), env={**ALLOW, "FAKE_SEAL_FAIL": "1"})
    assert first.returncode == 1 and "openbao-seal.sh enable failed" in first.stderr
    shares = host.secrets["shares"]
    again = host.run(stdin="".join(s + "\n" for s in (shares[0], shares[2], shares[4])))
    assert again.returncode == 0, again.stdout + again.stderr
    assert "this run holds none: type them" in again.stdout
    assert host.state["seal_stdin"] == 3 and host.state["bao"]["type"] == "static"
```

Y al final de `backend/tests/test_preflight_script.py`, tras `test_an_s3_public_endpoint_on_the_public_host_has_no_note`, dos líneas en blanco y las dos pruebas del interruptor: la que lo deja pasar con Pebble como CA, y la que lo rechaza con Let's Encrypt de producción, puesta o por omisión:

```python
def test_the_ci_can_leave_the_name_unasked_and_is_told_so(tmp_path: Path) -> None:
    """ITS_PREFLIGHT_SKIP_DNS=1 (the CI's `deploy` job, design
    2026-11-27-despliegue-automatizado §5): no public DNS holds the name, and
    the refusal it would earn is not this run's to fix. Said, not silent; and
    every other check still runs. Only against a CA that is not Let's Encrypt
    production: Pebble here, as in CI."""
    pebble = {**GOOD_ENV, "ITS_ACME_CA_SERVER": "https://pebble:14000/dir"}
    host = {**GOOD_HOST, "FAKE_V4": "", "FAKE_V6": "2001:db8::7"}
    run = _run(tmp_path, pebble, {**host, "ITS_PREFLIGHT_SKIP_DNS": "1"})
    assert run.returncode == 0, run.stdout
    assert (
        "note    rmm.novieri.com was not asked of DNS (ITS_PREFLIGHT_SKIP_DNS=1): "
        "the CI or a rehearsal, never a real host"
    ) in run.stdout
    assert "ok      Docker Engine 29.9.3" in run.stdout
    no_email = {**pebble, "ITS_ACME_EMAIL": ""}
    refused = _run(tmp_path, no_email, {**host, "ITS_PREFLIGHT_SKIP_DNS": "1"})
    assert refused.returncode == 1 and "ITS_ACME_EMAIL is empty" in refused.stdout
    # Without it, the same host is refused: the knob is what made the difference.
    assert _run(tmp_path, pebble, host).returncode == 1


@pytest.mark.parametrize(
    "ca",
    [
        pytest.param("https://acme-v02.api.letsencrypt.org/directory", id="production"),
        pytest.param(None, id="unset"),
    ],
)
def test_the_dns_is_never_left_unasked_against_lets_encrypt_production(
    tmp_path: Path, ca: str | None
) -> None:
    """Coordinator's ruling Q4 (2026-11-27): a skipped DNS check against
    production would spend its 5-certificates-a-week limit on a name nobody
    checked. Refused, with the fix, whatever DNS would have said; an unset
    ITS_ACME_CA_SERVER is production, as Traefik reads it."""
    env = {k: v for k, v in GOOD_ENV.items() if k != "ITS_ACME_CA_SERVER"}
    if ca is not None:
        env["ITS_ACME_CA_SERVER"] = ca
    run = _run(tmp_path, env, {**GOOD_HOST, "ITS_PREFLIGHT_SKIP_DNS": "1"})
    assert run.returncode == 1, run.stdout
    refusal = "REFUSE  ITS_PREFLIGHT_SKIP_DNS=1 with Let's Encrypt production as the ACME CA"
    assert refusal in run.stdout
    assert "was not asked of DNS" not in run.stdout
```

- [ ] **Step 2: Las pruebas fallan**

Run, desde `backend/`: `ITS_TEST_REDIS_URL=redis://localhost:6379/2 .venv/bin/python -m pytest -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning -p no:cacheprovider -q tests/test_first_boot_script.py tests/test_preflight_script.py`

Expected: `22 failed, 25 passed`: las 19 de `test_first_boot_script.py` (el guion no existe), `test_the_ci_can_leave_the_name_unasked_and_is_told_so` (la nota no sale y el guion se niega por el DNS) y los dos casos de `test_the_dns_is_never_left_unasked_against_lets_encrypt_production` (se niega, pero por el DNS, no por el interruptor: falta su mensaje).

- [ ] **Step 3: El guion, y la rama de `preflight.sh`**

Crear `deploy/scripts/first-boot.sh` y hacerlo ejecutable (`chmod +x deploy/scripts/first-boot.sh`):

```bash
#!/usr/bin/env bash
#
# The first boot of a new Novieri RMM host, as deploy/README.md's appendix
# numbers it: steps 1 to 3.6; OpenBao's auto-unseal and the restart that
# proves it; steps 4 to 7; and step 8's install code (design
# 2026-11-27-despliegue-automatizado §2 to §4).
#
#   ./scripts/first-boot.sh                    # rmm.novieri.com, from deploy/
#   ./scripts/first-boot.sh --acme-ca-server https://acme-staging-v02.api.letsencrypt.org/directory \
#       --tls-cafile /tmp/le-staging-roots.pem # a rehearsal against staging
#
# WHERE IT STOPS FOR YOU. The Let's Encrypt contact address (new-env.sh asks
# it); the five OpenBao shares and the root token, shown ONCE, until you type
# SAVED; three of those shares, hidden, which unseal OpenBao and prove your
# copies; and your password, when sudo asks for it.
#
# RUN IT AGAIN after any failure. Each step checks first whether it is done
# and prints `skip` when it is; `run` when it changes something; `ok` for a
# check that held. It never initialises an OpenBao twice and never replaces
# .env, the audit key or a token file. Run again after `bao operator init`,
# it asks for what it no longer holds in memory: the root token (hidden) if
# .env does not hold the tokens yet, and three shares if OpenBao is sealed and
# the auto-unseal is not on yet. From step 4 on OpenBao unseals itself, so a
# failure there is run again with no share at all.
#
# IT REFUSES a mixed state -- .env and OpenBao (or the volumes) from two
# different first boots -- and says how to start from zero. And it refuses to
# print the shares anywhere but a terminal: with its output redirected it
# stops before `bao operator init`, unless ITS_FIRST_BOOT_ALLOW_NONTTY=1 (the
# CI's `deploy` job, whose log goes with its runner).
#
# SECRETS. None goes on a command line, into a log, a temporary file or the
# shell history: each reaches `bao` and `sudo tee` on stdin. They are written
# where the runbook writes them: .env (0600), the gateway-issuer token file
# (0600, root) and openbao-seal.sh's key file (0400). It refuses to run under
# `bash -x`.
#
# Options: --host NAME (a new .env only; default rmm.novieri.com),
# --acme-email ADDRESS and --acme-ca-server URL (passed to new-env.sh),
# --tls-cafile PEM (passed to check-public-tls.sh: Let's Encrypt staging's
# roots on a rehearsal, Pebble's in CI). Environment, for the CI and a
# rehearsal: ITS_FIRST_BOOT_ALLOW_NONTTY=1, ITS_NO_SYSTEMCTL=1 (step 6b writes
# its units and touches no systemd), ITS_SYSTEMD_DIR, ITS_ETC_DIR.
set -euo pipefail

case "$-" in
  *x*) printf 'first-boot.sh: refusing to run under xtrace (bash -x): it would print every secret\n' >&2
       exit 1 ;;
esac

if [ -t 1 ]; then bold=$'\033[1m'; plain=$'\033[0m'; else bold=""; plain=""; fi
say()  { printf '\n%s%s%s\n' "$bold" "$*" "$plain"; }
ok()   { printf '  ok      %s\n' "$*"; }
skip() { printf '  skip    %s\n' "$*"; }
act()  { printf '  run     %s\n' "$*"; }
note() { printf '  note    %s\n' "$*"; }
warn() { printf '  WARNING %s\n' "$*" >&2; }
# die WHAT [NEXT...]: what failed, then the commands to go on with.
die() {
  printf '\nFAILED  %s\n' "$1" >&2
  shift
  local line
  for line in "$@"; do printf '        %s\n' "$line" >&2; done
  exit 1
}

host="" email="" ca_server="" tls_cafile=""
while [ $# -gt 0 ]; do
  case "$1" in
    --host) [ $# -ge 2 ] || die "--host needs a value"; host="$2"; shift 2 ;;
    --acme-email) [ $# -ge 2 ] || die "--acme-email needs a value"; email="$2"; shift 2 ;;
    --acme-ca-server) [ $# -ge 2 ] || die "--acme-ca-server needs a value"; ca_server="$2"; shift 2 ;;
    --tls-cafile) [ $# -ge 2 ] || die "--tls-cafile needs a file"; tls_cafile="$2"; shift 2 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" "./scripts/first-boot.sh --help" ;;
  esac
done
# Resolved before the cd below: a relative path names the caller's directory.
if [ -n "$tls_cafile" ]; then
  [ -s "$tls_cafile" ] || die "--tls-cafile $tls_cafile is missing or empty"
  tls_cafile="$(readlink -f "$tls_cafile")"
fi

cd "$(dirname "$(readlink -f "$0")")/.."   # deploy/
etc_dir="${ITS_ETC_DIR:-/etc/it-suite}"
allow_nontty="${ITS_FIRST_BOOT_ALLOW_NONTTY:-0}"
token_file="$etc_dir/gateway-issuer.token"
key_file="$etc_dir/openbao-unseal/unseal.key"
command -v docker >/dev/null 2>&1 || die "docker is not on PATH" "sudo ./scripts/prepare-host.sh, then log out and in again"

# ------------------------------------------------------------- helpers

# Every compose call that must not read this script's stdin: what the
# operator types, or the CI pipes, is for the prompts.
dc() { docker compose "$@" </dev/null; }

env_get() { sed -n "s/^$1=//p" .env 2>/dev/null | tail -1; }
# The key's earlier line goes first: a second KEY= further down silently wins.
env_put() { sed -i "/^$1=/d" .env && printf '%s=%s\n' "$1" "$2" >> .env; }
env_token_set() { local v; v="$(env_get "$1")"; [ -n "$v" ] && [ "$v" != change-me ]; }
auto_unseal_on() { grep -qE '^COMPOSE_FILE=.*openbao/auto-unseal\.yml' .env 2>/dev/null; }

# A COMPOSE_FILE inherited from the environment (the CI's Pebble overlay, a
# rehearsal's) wins over the one openbao-seal.sh writes into .env: once .env
# has the auto-unseal overlay, it is added to the inherited one too, or the
# next `docker compose up` would recreate OpenBao without the seal its data
# is under.
inherited_compose_file="${COMPOSE_FILE:-}"
follow_auto_unseal() {
  [ -n "$inherited_compose_file" ] || return 0
  auto_unseal_on || return 0
  case ":$COMPOSE_FILE:" in *":openbao/auto-unseal.yml:"*) return 0 ;; esac
  export COMPOSE_FILE="$COMPOSE_FILE:openbao/auto-unseal.yml"
  note "COMPOSE_FILE comes from the environment and wins over .env's: openbao/auto-unseal.yml is added to it for this run"
}

# bao_state sets bao_up (yes|no), bao_init, bao_sealed (true|false) and
# bao_type. `bao status` exits 0 unsealed and 2 sealed, initialised or not, and
# prints its JSON either way; anything else is "not answering". (`bao operator
# init -status` is no help: it exits 2 for "not initialised" AND for a refused
# connection, measured with OpenBao 2.6.1 on 2026-11-27.)
bao_state() {
  local out rc=0
  out="$(dc exec -T openbao bao status -format=json 2>/dev/null)" || rc=$?
  bao_up=no bao_init="" bao_sealed="" bao_type=""
  case "$rc" in 0|2) ;; *) return 0 ;; esac
  bao_init="$(printf '%s\n' "$out" | sed -n 's/^ *"initialized": *\(true\|false\).*/\1/p' | head -1)"
  bao_sealed="$(printf '%s\n' "$out" | sed -n 's/^ *"sealed": *\(true\|false\).*/\1/p' | head -1)"
  bao_type="$(printf '%s\n' "$out" | sed -n 's/^ *"type": *"\([a-z-]*\)".*/\1/p' | head -1)"
  if [ -n "$bao_init" ] && [ -n "$bao_sealed" ]; then bao_up=yes; fi
  return 0
}

wait_bao_up() {
  local i
  for ((i = 0; i < 90; i += 3)); do
    bao_state
    [ "$bao_up" = yes ] && return 0
    sleep 3
  done
  die "OpenBao does not answer after 90 s" "docker compose logs --tail 50 openbao"
}

# wait_unsealed: up to 90 s for OpenBao to report itself unsealed.
wait_unsealed() {
  local i
  for ((i = 0; i < 90; i += 3)); do
    bao_state
    [ "$bao_sealed" = false ] && return 0
    sleep 3
  done
  return 1
}

# bao_with TOKEN COMMAND...: one command in the openbao container, run with
# TOKEN, which reaches it on stdin and its environment there, never an argv.
bao_with() {
  local token="$1"
  shift
  # shellcheck disable=SC2016  # expanded by the container's shell
  printf '%s\n' "$token" \
    | docker compose exec -T openbao sh -c 'read -r BAO_TOKEN && export BAO_TOKEN && exec "$@"' sh "$@"
}

# No `| grep -q` under pipefail: grep leaving early is a SIGPIPE upstream, and a false "no".
is_root_token() {
  local out
  out="$(bao_with "$1" bao token lookup -format=json 2>/dev/null)" || return 1
  out="${out//[[:space:]]/}"
  [[ $out == *'"policies":["root"]'* ]]
}

is_one_of() {
  local want="$1" have
  shift
  for have in "$@"; do [ "$have" = "$want" ] && return 0; done
  return 1
}

psql_admin() { dc exec -T postgres psql -X -U its_admin -d itsuite -tAc "$1"; }

# service_health SERVICE: healthy | running | the container's state | absent
service_health() {
  local svc state health
  IFS='|' read -r svc state health < <(dc ps -a --format '{{.Service}}|{{.State}}|{{.Health}}' "$1" 2>/dev/null; true) || true
  if [ -z "${svc:-}" ]; then echo absent
  elif [ "$state" = running ] && [ "$health" = healthy ]; then echo healthy
  elif [ "$state" = running ] && [ -z "$health" ]; then echo running
  else echo "$state${health:+ ($health)}"
  fi
}

# oneshot_exit SERVICE: the exit code of its last container, or "none".
oneshot_exit() {
  local id
  id="$(dc ps -aq "$1" 2>/dev/null | head -1)"
  [ -n "$id" ] || { echo none; return 0; }
  docker inspect -f '{{.State.ExitCode}}' "$id" 2>/dev/null || echo none
}

# ------------------------------------------------------- the shares

shares=()   # the five, when this run printed them; this shell's memory only
typed=()    # the three typed at step 3.3, kept for openbao-seal.sh
root=""
share=""

show_shares() {
  local i rule="================================================================================"
  printf '\n%s\n' "$rule"
  printf '%s\n' " OpenBao: the five unseal key shares and the root token. Printed ONCE, never again."
  printf '%s\n' " Each share to a different custodian, the root token to your password manager:"
  printf '%s\n' " all OFF this VPS. Three are needed; three lost is a lost vault."
  printf '%s\n' "$rule"
  for i in 0 1 2 3 4; do printf '  share %d     %s\n' "$((i + 1))" "${shares[$i]}"; done
  printf '  root token  %s\n' "$root"
  printf '%s\n\n' "$rule"
}

confirm_saved() {
  local answer=""
  while :; do
    read -r -p "Type SAVED once the five shares and the root token are stored off this VPS: " answer \
      || die "the input ended before SAVED" \
             "OpenBao is initialised and its shares were printed above, once: copy them now." \
             "./scripts/first-boot.sh asks for the root token and three shares when it runs again"
    [ "$answer" = SAVED ] && break
    note "type SAVED, in capitals, to go on"
  done
  # The screen and its scrollback: the shares stay only where you copied them.
  if [ -t 1 ]; then printf '\033[H\033[2J\033[3J'; fi
  ok "step 3.2: the shares and the root token are in your custody"
}

# read_share N: one share, hidden, into $share. When this run printed the
# five, it must be one of them; and never one typed already.
read_share() {
  local n="$1" wrong=0
  while :; do
    share=""
    IFS= read -r -s -p "Unseal key share $n of 3 (hidden): " share || [ -n "$share" ] \
      || die "the input ended before three shares" "./scripts/first-boot.sh asks for them again when it runs again"
    if [ -t 0 ]; then echo; fi
    share="${share//[[:space:]]/}"
    if [ -z "$share" ]; then note "that was empty: type the share"; continue; fi
    if [ "${#typed[@]}" -gt 0 ] && is_one_of "$share" "${typed[@]}"; then
      note "that share was typed already: type a different one"; continue
    fi
    if [ "${#shares[@]}" = 5 ] && ! is_one_of "$share" "${shares[@]}"; then
      wrong=$((wrong + 1))
      [ "$wrong" -lt 3 ] || die "three times not one of the five shares printed above" \
        "check your copies against what you stored, then run ./scripts/first-boot.sh again"
      note "that is not one of the five shares printed above: check your copy"; continue
    fi
    return 0
  done
}

# Each share reaches `bao` on stdin, as JSON, never in an argv.
submit_share() {
  printf '{"key":"%s"}' "$1" \
    | docker compose exec -T openbao bao write -format=json sys/unseal - >/dev/null 2>&1 || true
}
reset_unseal() {
  printf '{"reset":true}' \
    | docker compose exec -T openbao bao write -format=json sys/unseal - >/dev/null 2>&1 || true
}

unseal_with_three() {
  local round n
  for round in 1 2 3; do
    typed=()
    reset_unseal
    for n in 1 2 3; do
      read_share "$n"
      typed+=("$share")
      submit_share "$share"
    done
    share=""
    bao_state
    if [ "$bao_sealed" = false ]; then
      ok "step 3.3: OpenBao is unsealed: three shares, and your copies of them are good"
      return 0
    fi
    warn "OpenBao is still sealed: one of those three is not a share of this OpenBao (round $round of 3)"
  done
  die "OpenBao is still sealed after three rounds of three shares" \
      "check the copies with their custodians, then run ./scripts/first-boot.sh again"
}

read_root_token() {
  local t=""
  IFS= read -r -s -p "OpenBao root token, printed by step 3.2 (hidden): " t || [ -n "$t" ] \
    || die "the input ended before the root token" "run ./scripts/first-boot.sh again and paste it at the prompt"
  if [ -t 0 ]; then echo; fi
  t="${t//[[:space:]]/}"
  is_root_token "$t" || die "that is not this OpenBao's root token" \
    "run ./scripts/first-boot.sh again and paste the root token step 3.2 printed"
  root="$t"
}

start_from_zero=("start from zero, which DESTROYS this stack's volumes and everything in them:"
                 "docker compose down -v && rm .env" "./scripts/first-boot.sh")

# --------------------------------------------------------------- steps

check_state_before_anything() {
  say "Before anything: the state this host is in"
  local volumes
  if [ -e .env ]; then
    ok ".env exists: this is a run after step 1"
    return 0
  fi
  volumes="$(docker volume ls -q --filter label=com.docker.compose.project=it-suite 2>/dev/null | tr '\n' ' ' || true)"
  if [ -n "${volumes// /}" ]; then
    die "mixed state: this host has the volumes of an earlier first boot (${volumes% }) but no .env, and a new .env would not open them" \
        "if you have that first boot's .env (in its off-host custody), put it back as deploy/.env, mode 600, and run this again;" \
        "to start from zero instead, which DESTROYS those volumes and everything in them:" \
        "docker ps -aq --filter label=com.docker.compose.project=it-suite | xargs -r docker rm -f" \
        "docker volume ls -q --filter label=com.docker.compose.project=it-suite | xargs -r docker volume rm" \
        "./scripts/first-boot.sh"
  fi
  ok "no .env and no it-suite volume: a new host"
}

step_1() {
  say "Step 1 — .env"
  local in_env args
  if [ -e .env ]; then
    in_env="$(env_get ITS_PUBLIC_HOST)"
    if [ -n "$host" ] && [ "$host" != "$in_env" ]; then
      die "--host $host, but .env is for ${in_env:-no host}" "./scripts/first-boot.sh, without --host" "${start_from_zero[@]}"
    fi
    host="$in_env"
    [ -n "$host" ] || die ".env has no ITS_PUBLIC_HOST" "${start_from_zero[@]}"
    if [ -n "$email$ca_server" ]; then note "--acme-email and --acme-ca-server are for a new .env; this one stays as it is"; fi
    skip "step 1: .env exists (new-env.sh never overwrites one), for $host"
  else
    host="${host:-rmm.novieri.com}"
    args=(--host "$host")
    [ -z "$email" ] || args+=(--acme-email "$email")
    [ -z "$ca_server" ] || args+=(--acme-ca-server "$ca_server")
    act "./scripts/new-env.sh ${args[*]}"
    ./scripts/new-env.sh "${args[@]}"
  fi
  [ "$(stat -c %a .env)" = 600 ] || die ".env is mode $(stat -c %a .env), not 600: it holds every password of this deployment" "chmod 600 .env" "./scripts/first-boot.sh"
  follow_auto_unseal
}

step_1b() {
  say "Step 1b — tagged images"
  local head_tag tag v
  head_tag="$(git -C .. rev-parse --short HEAD)"
  tag="$(env_get IT_SUITE_VERSION)"
  if [ "$tag" = "$head_tag" ] && docker image inspect "itsuite/backend:$tag" "itsuite/web:$tag" >/dev/null 2>&1; then
    skip "step 1b: IT_SUITE_VERSION=$tag is this checkout's commit, and both images exist"
    return 0
  fi
  act "./scripts/build-images.sh, then IT_SUITE_VERSION=$head_tag in .env"
  ./scripts/build-images.sh </dev/null
  v="$(git -C .. rev-parse --short HEAD)" && sed -i '/^IT_SUITE_VERSION=/d' .env && echo "IT_SUITE_VERSION=$v" >> .env && grep '^IT_SUITE_VERSION=' .env
}

step_1c() {
  say "Step 1c — the pre-flight"
  ./scripts/preflight.sh </dev/null \
    || die "the pre-flight refused (above)" "fix each REFUSE line, then run ./scripts/first-boot.sh again"
}

step_2() {
  say "Step 2 — Postgres"
  local roles
  if [ "$(service_health postgres)" = healthy ]; then
    skip "step 2: postgres is up and healthy"
  else
    act "docker compose up -d --wait postgres"
    dc up -d --wait --wait-timeout 120 postgres || die "postgres did not become healthy" "docker compose logs --tail 50 postgres"
  fi
  roles="$(psql_admin "select count(*) from pg_roles where rolname in ('app_user', 'portal_user')" | tr -d '[:space:]')"
  [ "$roles" = 2 ] || die "postgres has no app_user and portal_user: its data directory is older than this .env, so postgres/init-app-user.sh never ran on it" \
    "docker compose down -v postgres" "./scripts/first-boot.sh"
  ok "step 2: postgres has app_user and portal_user"
}

step_3_1() {
  say "Step 3.1 — OpenBao"
  bao_state
  if [ "$bao_up" = yes ]; then
    skip "step 3.1: OpenBao is up"
  else
    act "docker compose up -d openbao"
    dc up -d openbao
    wait_bao_up
  fi
  ok "step 3.1: OpenBao answers: initialised $bao_init, sealed $bao_sealed, seal $bao_type"
  if [ "$bao_init" = false ] && { env_token_set ITS_OPENBAO_TOKEN || env_token_set OPENBAO_TOKEN; }; then
    die "mixed state: .env holds OpenBao tokens, but this OpenBao was never initialised: .env is from another first boot" "${start_from_zero[@]}"
  fi
}

step_3_2() {
  say "Step 3.2 — initialise OpenBao: the only copy of its shares"
  local init_out line
  if [ "$bao_init" = true ]; then
    skip "step 3.2: OpenBao is initialised, and this never initialises it again"
    return 0
  fi
  if [ ! -t 1 ] && [ "$allow_nontty" != 1 ]; then
    die "the output is not a terminal, and this step prints the five shares and the root token, once" \
        "run ./scripts/first-boot.sh in a terminal, with nothing redirected"
  fi
  act "bao operator init -key-shares=5 -key-threshold=3"
  init_out="$(dc exec -T openbao bao operator init -key-shares=5 -key-threshold=3)" \
    || die "bao operator init failed" "docker compose logs --tail 50 openbao"
  while IFS= read -r line; do
    case "$line" in
      "Unseal Key "[1-5]": "*) shares+=("${line#*: }") ;;
      "Initial Root Token: "*) root="${line#*: }" ;;
    esac
  done < <(printf '%s\n' "$init_out")
  if [ "${#shares[@]}" != 5 ] || [ -z "$root" ]; then
    printf '%s\n' "$init_out"
    die "bao operator init did not print five shares and a root token (its output is above: the only copy)" \
        "copy all of it off this VPS now; ./scripts/first-boot.sh then asks for the root token and three shares"
  fi
  init_out=""
  show_shares
  confirm_saved
}

step_3_3() {
  say "Step 3.3 — unseal"
  local root_in_env
  bao_state
  if [ "$bao_sealed" = false ]; then
    skip "step 3.3: OpenBao is unsealed"
  elif auto_unseal_on; then
    note "the auto-unseal is on: waiting for OpenBao to unseal itself"
    wait_unsealed || die "OpenBao is sealed although the auto-unseal is on: its key file is missing or wrong" \
      "sudo ./scripts/openbao-seal.sh status" "docker compose logs --tail 50 openbao" \
      "deploy/README.md, After a reboot or a restart, says how to put the key file back"
    ok "step 3.3: OpenBao unsealed itself (seal $bao_type)"
  else
    unseal_with_three
  fi
  if env_token_set OPENBAO_TOKEN; then
    root_in_env="$(env_get OPENBAO_TOKEN)"
    is_root_token "$root_in_env" || die "mixed state: OPENBAO_TOKEN in .env is not this OpenBao's root token: .env and OpenBao come from two first boots" "${start_from_zero[@]}"
    [ -n "$root" ] || root="$root_in_env"
  fi
}

step_3_4() {
  say "Step 3.4 — the runtime policies and their tokens; 3.5 — both in .env"
  local need_app=no need_gw=no app gw
  env_token_set ITS_OPENBAO_TOKEN || need_app=yes
  sudo test -s "$token_file" || need_gw=yes
  if [ "$need_app" = no ] && [ "$need_gw" = no ] && env_token_set OPENBAO_TOKEN; then
    skip "step 3.4: ITS_OPENBAO_TOKEN and OPENBAO_TOKEN are in .env, and $token_file exists"
    return 0
  fi
  [ -n "$root" ] || read_root_token
  act "bao policy write its-app and gateway-issuer; bao auth tune; the two tokens"
  bao_with "$root" bao policy write its-app /bao/its-app.hcl
  bao_with "$root" bao policy write gateway-issuer /bao/gateway-issuer.hcl
  bao_with "$root" bao auth tune -max-lease-ttl=87600h token/
  if [ "$need_gw" = yes ]; then
    gw="$(bao_with "$root" bao token create -orphan -policy=gateway-issuer -ttl=87600h -field=token)"
    [ -n "$gw" ] || die "OpenBao issued no gateway-issuer token" "docker compose logs --tail 50 openbao"
    sudo install -d -m 700 "$etc_dir"
    sudo install -m 600 /dev/null "$token_file"
    printf '%s\n' "$gw" | sudo tee "$token_file" >/dev/null
    gw=""
    ok "step 3.4: the gateway-issuer token is in $token_file (mode 0600, root's)"
  fi
  if [ "$need_app" = yes ]; then
    app="$(bao_with "$root" bao token create -orphan -policy=its-app -ttl=87600h -field=token)"
    [ -n "$app" ] || die "OpenBao issued no its-app token" "docker compose logs --tail 50 openbao"
    env_put ITS_OPENBAO_TOKEN "$app"
    app=""
  fi
  env_token_set OPENBAO_TOKEN || env_put OPENBAO_TOKEN "$root"
  [ "$(grep -c 'change-me' .env || true)" = 0 ] || die ".env still has a change-me value" "grep -n change-me .env"
  ok "step 3.5: ITS_OPENBAO_TOKEN (its-app) and OPENBAO_TOKEN (root) are in .env, two different values"
}

step_3_6() {
  say "Step 3.6 — the transit key and the PKI"
  local code
  if [ "$(oneshot_exit openbao-init)" = 0 ]; then
    skip "step 3.6: openbao-init exited 0 (provisioning complete)"
    return 0
  fi
  act "docker compose up openbao-init"
  dc up openbao-init
  code="$(oneshot_exit openbao-init)"
  [ "$code" = 0 ] || die "openbao-init exited $code" "docker compose logs openbao-init"
  ok "step 3.6: openbao provisioning complete"
}

enabled_now=no
auto_unseal() {
  say "OpenBao's auto-unseal, with a static key (GO-LIVE.md §4; decided 2026-11-27)"
  local n
  bao_state
  if auto_unseal_on && [ "$bao_type" = static ] && [ "$bao_sealed" = false ]; then
    skip "auto-unseal: on, and OpenBao is unsealed with its static key"
    return 0
  fi
  if [ "$bao_type" != shamir ] || [ "$bao_sealed" != false ]; then
    die "OpenBao reports seal $bao_type, sealed $bao_sealed: openbao-seal.sh enable needs it unsealed, with shares" \
        "sudo ./scripts/openbao-seal.sh status"
  fi
  if [ "${#typed[@]}" != 3 ]; then
    note "openbao-seal.sh needs three shares to move the seal, and this run holds none: type them"
    typed=()
    for n in 1 2 3; do read_share "$n"; typed+=("$share"); done
    share=""
  fi
  act "sudo ./scripts/openbao-seal.sh enable --key-file $key_file --yes, the three shares on its stdin"
  # env -u: openbao-seal.sh writes COMPOSE_FILE into .env, and an inherited
  # one would win over it and recreate OpenBao without the seal it migrates to.
  printf '%s\n' "${typed[@]}" | sudo env -u COMPOSE_FILE ./scripts/openbao-seal.sh enable --key-file "$key_file" --yes \
    || die "openbao-seal.sh enable failed (above, with its way back)" "sudo ./scripts/openbao-seal.sh status"
  typed=()
  auto_unseal_on || die "openbao-seal.sh enable did not put the auto-unseal overlay into .env" "sudo ./scripts/openbao-seal.sh status"
  follow_auto_unseal
  enabled_now=yes
}

restart_proof() {
  say "OpenBao restarts and unseals itself"
  if [ "$enabled_now" != yes ]; then
    skip "the restart that proves it ran when the auto-unseal was turned on"
    return 0
  fi
  act "docker compose restart openbao"
  dc restart openbao
  wait_unsealed || die "OpenBao came back sealed after a restart: the auto-unseal does not work" \
    "sudo ./scripts/openbao-seal.sh status" "docker compose logs --tail 50 openbao"
  [ "$bao_type" = static ] || die "OpenBao unsealed, but its seal is $bao_type, not static" "sudo ./scripts/openbao-seal.sh status"
  ok "OpenBao restarted and came back unsealed by itself (Seal Type static, Sealed false): no share was typed"
}

# ------------------------------------------------------------------ main

check_state_before_anything
step_1
step_1b
step_1c
step_2
step_3_1
step_3_2
step_3_3
step_3_4
step_3_6
auto_unseal
restart_proof
say "Steps 1 to 3.6 and the auto-unseal: done."
```

Y la rama de `preflight.sh`, con este guion, desde la raíz (no se guarda en el repositorio; `$S` es el scratchpad de la sesión):

```bash
S=/tmp/claude-0/-home-user-Novieri-RMM/21768af2-984e-5cf8-87b2-4dc8a41a4864/scratchpad
cat > "$S/task2_preflight.py" <<'TASK2_PREFLIGHT'
"""Plan 2026-11-27, Task 2: preflight.sh can leave the public name unasked,
for the CI and a rehearsal, and says so (design 2026-11-27 §5). One anchor."""

from pathlib import Path

PREFLIGHT = Path("deploy/scripts/preflight.sh")
OLD = '''# for the name is read as an AAAA, and refused like one. `|| true`: getent
# exits 2 for a name with no address, which pipefail would turn into this
# script dying silently here.
if [ -n "$public_host" ] && [ "$public_host" != "your-host" ]; then
'''
NEW = '''# for the name is read as an AAAA, and refused like one. `|| true`: getent
# exits 2 for a name with no address, which pipefail would turn into this
# script dying silently here.
#
# ITS_PREFLIGHT_SKIP_DNS=1 leaves the name unasked, and says so: for the CI's
# `deploy` job and a rehearsal, where no public DNS holds the name and Pebble's
# validator asks its own (design 2026-11-27-despliegue-automatizado §5). Never
# against Let's Encrypt production, and so never on a real host: a name nobody
# checked spends the 5 certificates a week production allows it, and that CA
# is the one an unset ITS_ACME_CA_SERVER means (coordinator's ruling Q4,
# 2026-11-27).
if [ "${ITS_PREFLIGHT_SKIP_DNS:-0}" = 1 ]; then
  case "${acme_ca:-https://acme-v02.api.letsencrypt.org/directory}" in
    https://acme-v02.api.letsencrypt.org/directory)
      refuse "ITS_PREFLIGHT_SKIP_DNS=1 with Let's Encrypt production as the ACME CA (ITS_ACME_CA_SERVER is ${acme_ca:-unset}): a name nobody asked DNS about would spend the 5 certificates a week production allows it. Unset ITS_PREFLIGHT_SKIP_DNS on a real host; a rehearsal sets ITS_ACME_CA_SERVER to staging or Pebble." ;;
    *)
      printf '  note    %s was not asked of DNS (ITS_PREFLIGHT_SKIP_DNS=1): the CI or a rehearsal, never a real host\\n' "$public_host" ;;
  esac
elif [ -n "$public_host" ] && [ "$public_host" != "your-host" ]; then
'''
text = PREFLIGHT.read_text()
assert text.count(OLD) == 1, "the AAAA comment and the DNS branch are not there exactly once"
PREFLIGHT.write_text(text.replace(OLD, NEW))
print("preflight.sh: ITS_PREFLIGHT_SKIP_DNS=1")
TASK2_PREFLIGHT
python3 "$S/task2_preflight.py"
```

Expected: `preflight.sh: ITS_PREFLIGHT_SKIP_DNS=1`.

- [ ] **Step 4: Las pruebas pasan**

Run, desde `backend/`: la orden del Step 2, y `.venv/bin/ruff check app tests`. Desde la raíz: `shellcheck deploy/scripts/first-boot.sh && echo SHELLCHECK-CLEAN` y `test -x deploy/scripts/first-boot.sh && echo EXECUTABLE`.

Expected: `47 passed`; `All checks passed!`; `SHELLCHECK-CLEAN`; `EXECUTABLE`. (`shellcheck deploy/scripts/preflight.sh` da dos avisos de nivel *info*, SC2086, en las líneas 49 y 191, que ya estaban: esta tarea no las toca, y el paso de shellcheck de la CI, Task 5, solo mira los tres guiones nuevos.)

- [ ] **Step 5: El diseño, §2, §3 y §4** (resolución Q6)

Con este guion, desde la raíz (no se guarda en el repositorio):

```bash
S=/tmp/claude-0/-home-user-Novieri-RMM/21768af2-984e-5cf8-87b2-4dc8a41a4864/scratchpad
cat > "$S/task2_spec.py" <<'TASK2_SPEC'
"""Plan 2026-11-27, Task 2: the design's §2, §3 and §4 put the auto-unseal
right after step 3.6 (coordinator's ruling Q6, 2026-11-27). Each anchor once;
nothing is written unless all are there."""

from pathlib import Path

SPEC = Path("docs/superpowers/specs/2026-11-27-despliegue-automatizado-design.md")
EDITS = (
    (
        """\
   - Al final: el desellado automático (`openbao-seal.sh enable`), un reinicio
     de OpenBao que demuestra que vuelve solo, y el código de instalación del
     paso 8 impreso en pantalla.
""",
        """\
   - Justo después del paso 3.6, antes de que el paso 4 arranque nada: el
     desellado automático (`openbao-seal.sh enable`, con esas tres partes) y un
     reinicio de OpenBao que demuestra que vuelve solo. Desde ahí, un fallo se
     relanza sin teclear ninguna parte (decisión del coordinador del
     2026-11-27, pregunta Q6 del plan).
   - Al final: el código de instalación del paso 8 impreso en pantalla.
""",
    ),
    (
        """\
**`first-boot.sh`** (la cuenta de operador, desde `deploy/`): pasos 1, 1b, 1c,
2, 3.1 a 3.6, 4, 4b, 5, 6, 6b y 7 del runbook; el desellado automático; el
código del paso 8. Se niega si `.env` ya existe y OpenBao está inicializado
""",
        """\
**`first-boot.sh`** (la cuenta de operador, desde `deploy/`): pasos 1, 1b, 1c,
2 y 3.1 a 3.6 del runbook; el desellado automático; los pasos 4, 4b, 5, 6, 6b y
7; el código del paso 8. Se niega si `.env` ya existe y OpenBao está inicializado
""",
    ),
    (
        """\
pide las tres partes. Nunca vuelve a inicializar un OpenBao ya inicializado.
""",
        """\
pide las tres partes. Desde el paso 4, OpenBao se desella solo, y un fallo se
relanza sin partes. Nunca vuelve a inicializar un OpenBao ya inicializado.
""",
    ),
    (
        """\
- `first-boot.sh` llama a `openbao-seal.sh enable` con las tres partes que ya
  tiene, sin volver a pedirlas, y comprueba `Sealed false` tras reiniciar
  `openbao`.
""",
        """\
- `first-boot.sh` llama a `openbao-seal.sh enable` justo después del paso 3.6,
  con las tres partes que ya tiene, sin volver a pedirlas, y comprueba `Sealed
  false` tras reiniciar `openbao`, antes del paso 4.
""",
    ),
)
text = SPEC.read_text()
for old, _ in EDITS:
    assert text.count(old) == 1, old[:60]
for old, new in EDITS:
    text = text.replace(old, new)
SPEC.write_text(text)
print("design §2, §3 and §4: the auto-unseal right after step 3.6")
TASK2_SPEC
python3 "$S/task2_spec.py"
grep -ci 'justo después del paso 3.6' docs/superpowers/specs/2026-11-27-despliegue-automatizado-design.md
```

Expected: `design §2, §3 and §4: the auto-unseal right after step 3.6` y `2`.

- [ ] **Step 6: Commit**

```bash
git add deploy/scripts/first-boot.sh deploy/scripts/preflight.sh backend/tests/first_boot_support.py backend/tests/test_first_boot_script.py backend/tests/test_preflight_script.py docs/superpowers/specs/2026-11-27-despliegue-automatizado-design.md
git commit -m "feat(deploy): first-boot.sh, steps 1 to 3.6 and the auto-unseal, and the states it refuses

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013TnTdGAz4rSe1YRXXjEqyr"
```

---

### Task 3: `first-boot.sh`, segunda parte — pasos 4 a 7 y el código del paso 8 (oleada 2, tras la Task 2)

**Files:**
- Modify: `deploy/scripts/first-boot.sh` (tres variables tras el `cd`; los pasos 4 a 8 en lugar del bloque `main` de la Task 2, que acaba en el desellado)
- Test: `backend/tests/test_first_boot_script.py` (pruebas nuevas al final; el arnés de la Task 2 no cambia)

**Interfaces:**
- Consumes: de la Task 2, todo lo que su bloque Interfaces lista; de `deploy/README.md` (en cualquier sección), los dos *here-documents* `sudo tee /etc/systemd/system/its-gateway-cert.service >/dev/null <<'EOF'` y `… its-gateway-cert.timer …`; `./scripts/check-public-tls.sh [--cafile PEM]` e `issue-gateway-cert.py <host>` (el token por la entrada estándar), sin cambios. Del orden de la Task 2: cuando el paso 4 empieza, OpenBao ya tiene el sello estático y `.env` su `COMPOSE_FILE`, así que ningún paso de esta tarea pide ni usa una parte.
- Produces:
  - **Variables de entorno nuevas:** `ITS_SYSTEMD_DIR` (por defecto `/etc/systemd/system`) e `ITS_NO_SYSTEMCTL=1` (el paso 6b escribe sus dos unidades y no llama a `systemctl`), los mismos nombres que `install-backup-timer.sh`.
  - **Líneas que leen otros**, exactas: la última cabecera de una ejecución buena, `First boot: every step is done.` (Tasks 5 y 6), seguida de seis `note`; las cabeceras de los pasos, como `Step 4 — migrations` (la Task 6 la cita); el código, `  install code  <código>` (lo enmascara `first_boot_answers.py`, Task 5); `  note    open https://<host>/ in a browser you trust and enter it (deploy/README.md, First boot, 2): it is a password until you have` (Task 6); y sin terminal ni `ITS_FIRST_BOOT_ALLOW_NONTTY`, en lugar del código, `  note    the install code is not printed, the output is not a terminal; read it with: docker compose exec api cat /var/lib/its/secrets/setup-code`. **Una segunda ejecución con todo hecho no imprime ninguna línea `  run     ` e imprime 16 `  skip    `**, en este orden: `step 1`, `step 1b`, `step 2`, `step 3.1`, `step 3.2`, `step 3.3`, `step 3.4`, `step 3.6`, `auto-unseal`, `the restart that proves it…`, `step 4`, `step 4b`, `step 5`, `step 6`, `step 6b`, `step 7` (la CI lo comprueba, Task 5).
  - **Para las pruebas de la Task 5:** el paso 6 es `if ! sudo cat "$token_file" | docker compose run --rm --no-deps -T \` … `--entrypoint python gateway /tmp/issue-gateway-cert.py "$host"; then`; el 6b, `sudo systemctl daemon-reload` antes de `sudo systemctl enable --now its-gateway-cert.timer`.

**Decisiones de la tarea.**
- **Ningún paso de esta tarea pide una parte** (resolución Q6: el desellado es de la Task 2, justo después del 3.6). Si el paso 7 falla (lo más probable en un VPS real: el registro `A`, el cortafuegos del proveedor), la siguiente ejecución encuentra OpenBao desellado solo, salta del 1 al desellado y sigue sin preguntar nada: `test_a_failed_step_7_runs_again_with_no_share` lo fija, y `test_the_auto_unseal_comes_before_step_4`, el orden.
- **El paso 5 escribe la clave HMAC con `set -C`** (*noclobber*): el bloque del runbook, tal cual, la sobrescribiría si existiera, y una clave nueva deja sin verificar toda la cadena de auditoría escrita hasta entonces. La Task 6 pone el mismo `set -C` en el bloque del apéndice.
- **El paso 7 no se fía de un vistazo**: espera hasta cinco minutos a que cada servicio esté en marcha (y sano, si tiene comprobación) y las tres de una vez hayan salido con 0, y después **15 segundos más sin que ningún contenedor se reinicie**: un servicio que se reinicia para siempre está en marcha un momento cada vez (fila 15 de «What was broken», el `worker` del ensayo del 2026-11-24). Si un servicio de una vez sale con otro código, o el tiempo se acaba, `FAILED` nombra los servicios y da sus `docker compose logs`.
- **El paso 8 genera el código él mismo**, preguntando a la api por `http://localhost:8000/api/v1/setup/status` desde dentro de su contenedor (el `healthcheck` de `api` hace lo mismo con `/livez`): esa primera petición es la que crea el código (`app/api/v1/setup.py`), y así no hace falta ni DNS ni la confianza en el certificado. Con la instalación ya reclamada, `skip`.
- **El `:8443`** se comprueba como el runbook (`openssl s_client … | openssl x509 -noout -issuer -ext subjectAltName`), en `ITMS_BIND_IP` si nombra una dirección y si no en `127.0.0.1`, que es lo que hace `check-public-tls.sh` con el `:443`.

- [ ] **Step 0: La worktree, con la Task 2**

Desde la raíz de la worktree:

```bash
git merge --no-edit p5-t2
git log --oneline -3
test -x deploy/scripts/first-boot.sh && grep -c '^say "Steps 1 to 3.6 and the auto-unseal: done."$' deploy/scripts/first-boot.sh
grep -c '^def test_' backend/tests/test_first_boot_script.py
```

Expected: la fusión con la Task 2 entre los tres primeros; `1`; `19`. Si no, se para y se avisa al coordinador. Después, la preparación de Global Constraints.

- [ ] **Step 1: Las pruebas**

Al final de `backend/tests/test_first_boot_script.py`, dos líneas en blanco y:

```python
# --------------------------------------------------------------------------- #
# Steps 4 to 8, the auto-unseal and its restart (plan 2026-11-27, Task 3)
# --------------------------------------------------------------------------- #

README = Path(__file__).resolve().parents[2] / "deploy" / "README.md"


def _done_host(tmp_path: Path) -> tuple[Host, str]:
    """A host after one whole, successful run."""
    host = Host(tmp_path)
    done = host.run(stdin=_saved_and_three(host), env=ALLOW)
    assert done.returncode == 0, done.stdout + done.stderr
    return host, done.stdout


def test_a_new_host_runs_every_step_to_the_install_code(tmp_path: Path) -> None:
    host, out = _done_host(tmp_path)
    ran = lines(out, "run")
    for expected in (
        "docker compose up migrate",
        "docker compose up postgres-grants",
        "32 random bytes into /var/lib/its/secrets/audit-hmac.key, mode 600",
        "docker compose up -d: it started or recreated what was not up",
        "docker compose restart openbao",
    ):
        assert expected in ran, (expected, ran)
    assert any(r.startswith("issue-gateway-cert.py rmm.novieri.com") for r in ran)
    assert any(r.startswith("sudo ./scripts/openbao-seal.sh enable --key-file ") for r in ran)
    assert f"  install code  {host.secrets['code']}" in out
    assert out.rstrip().splitlines()[-7] == "First boot: every step is done."
    env = host.env()
    assert env["COMPOSE_FILE"] == "docker-compose.yml:openbao/auto-unseal.yml"
    assert env["OPENBAO_UNSEAL_KEY_FILE"] == str(host.etc / "openbao-unseal" / "unseal.key")
    assert host.state["seal_stdin"] == 3, "openbao-seal.sh got the three shares of step 3.3"
    assert host.state["bao"]["type"] == "static" and host.state["bao"]["sealed"] is False
    assert host.state["files"] == {"audit": True, "gateway": True}
    assert "ok      OpenBao restarted and came back unsealed by itself" in out


def test_a_second_run_skips_every_step_and_changes_nothing(tmp_path: Path) -> None:
    host, _ = _done_host(tmp_path)
    before = (host.state["services"], host.state["bao"], host.state["files"], host.env())
    again = host.run()
    assert again.returncode == 0, again.stdout + again.stderr
    assert lines(again.stdout, "run") == []
    skipped = [s.split(":")[0] for s in lines(again.stdout, "skip")]
    assert skipped == [
        "step 1", "step 1b", "step 2", "step 3.1", "step 3.2", "step 3.3", "step 3.4",
        "step 3.6", "auto-unseal", RESTART_SKIP,
        "step 4", "step 4b", "step 5", "step 6", "step 6b", "step 7",
    ]  # fmt: skip
    assert (host.state["services"], host.state["bao"], host.state["files"], host.env()) == before
    argv = host.argv()
    for changing in ("compose restart", "openbao-seal.sh", "operator init", "set -C", "tee "):
        assert changing not in argv, changing


def test_without_a_terminal_the_install_code_is_not_printed(tmp_path: Path) -> None:
    host, _ = _done_host(tmp_path)
    again = host.run()
    assert host.secrets["code"] not in again.stdout + again.stderr
    assert "docker compose exec api cat /var/lib/its/secrets/setup-code" in again.stdout


def test_a_claimed_installation_skips_step_8(tmp_path: Path) -> None:
    host, _ = _done_host(tmp_path)
    host.state["setup"]["needed"] = False
    again = host.run(env=ALLOW)
    assert "skip    step 8: the installation is claimed" in again.stdout
    assert host.secrets["code"] not in again.stdout


def test_the_audit_key_is_never_replaced(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.state["files"]["audit"] = True
    done = host.run(stdin=_saved_and_three(host), env=ALLOW)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "skip    step 5: the audit HMAC key exists, and it is never replaced" in done.stdout
    assert "set -C" not in host.argv()


def test_the_gateway_certificate_reads_its_token_from_the_file_on_stdin(tmp_path: Path) -> None:
    host, _ = _done_host(tmp_path)
    argv = host.argv().splitlines()
    cat = argv.index(f"sudo cat {host.etc}/gateway-issuer.token")
    issue = "/tmp/issue-gateway-cert.py rmm.novieri.com"
    run = next(i for i, line in enumerate(argv) if issue in line)
    assert run > cat
    assert argv[run].startswith("docker compose run --rm --no-deps -T -v ")


def _heredoc(text: str, opener: str) -> str:
    start = text.index(opener) + len(opener) + 1
    return text[start : text.index("\nEOF\n", start)] + "\n"


def test_step_6b_writes_the_units_the_runbook_writes(tmp_path: Path) -> None:
    host, _ = _done_host(tmp_path)
    readme = README.read_text()
    for unit in ("its-gateway-cert.service", "its-gateway-cert.timer"):
        expected = _heredoc(readme, f"sudo tee /etc/systemd/system/{unit} >/dev/null <<'EOF'")
        expected = expected.replace("/opt/it-suite/deploy", str(host.deploy))
        expected = expected.replace("/etc/it-suite/", f"{host.etc}/")
        assert (host.units / unit).read_text() == expected, unit


def test_with_systemd_the_timer_is_enabled(tmp_path: Path) -> None:
    host = Host(tmp_path)
    done = host.run(stdin=_saved_and_three(host), env={**ALLOW, "ITS_NO_SYSTEMCTL": "0"})
    assert done.returncode == 0, done.stdout + done.stderr
    argv = host.argv()
    reload = argv.index("sudo systemctl daemon-reload")
    assert reload < argv.index("sudo systemctl enable --now its-gateway-cert.timer")
    assert "sudo systemctl list-timers its-gateway-cert.timer --no-pager" in argv


def test_a_failed_one_shot_stops_the_run(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.state["oneshot_exit"] = {"migrate": 1}
    done = host.run(stdin=_saved_and_three(host), env=ALLOW)
    assert done.returncode == 1
    assert "migrate exited 1" in done.stderr and "docker compose logs migrate" in done.stderr


def test_a_service_that_keeps_restarting_stops_step_7(tmp_path: Path) -> None:
    """Row 15 of "What was broken": a worker restarting for ever is up for a
    moment at a time. Up in every sample is not enough; its restarts must
    stand still."""
    host = Host(tmp_path)
    host.state["restart_step"] = 1
    done = host.run(stdin=_saved_and_three(host), env=ALLOW)
    assert done.returncode == 1
    assert "after 300 s, still waiting on:" in done.stderr and "(restarted)" in done.stderr


def test_the_tls_cafile_reaches_the_certificate_check(tmp_path: Path) -> None:
    host = Host(tmp_path)
    root = tmp_path / "root.pem"
    root.write_text("-----BEGIN CERTIFICATE-----\n")
    done = host.run("--tls-cafile", "root.pem", stdin=_saved_and_three(host), env=ALLOW)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"check-public-tls.sh --cafile {root}" in host.argv()


def test_the_auto_unseal_comes_before_step_4(tmp_path: Path) -> None:
    """Coordinator's ruling Q6 (2026-11-27): right behind step 3.6, so the
    stack step 7 starts is already static-sealed."""
    host, _ = _done_host(tmp_path)
    argv = host.argv().splitlines()
    seal = next(i for i, line in enumerate(argv) if line.startswith("openbao-seal.sh enable"))
    restart = argv.index("docker compose restart openbao")
    migrate = argv.index("docker compose up migrate")
    assert seal < restart < migrate < argv.index("docker compose up -d")


def test_a_failed_step_7_runs_again_with_no_share(tmp_path: Path) -> None:
    """The point of ruling Q6: step 7 is where a real VPS fails (the A record,
    the provider's firewall), and by then OpenBao unseals itself. Run again
    with nothing on stdin, it asks for no share and finishes."""
    host = Host(tmp_path)
    refused_tls = {**ALLOW, "FAKE_RC_CHECK_PUBLIC_TLS": "1"}
    first = host.run(stdin=_saved_and_three(host), env=refused_tls)
    assert first.returncode == 1
    assert "the certificate on :443 is not one a browser accepts" in first.stderr
    assert host.state["bao"]["type"] == "static"
    again = host.run(stdin="")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "skip    auto-unseal: on, and OpenBao is unsealed with its static key" in again.stdout
    assert "openbao-seal.sh" not in host.argv()
```

- [ ] **Step 2: Las pruebas fallan**

Run, desde `backend/`: `ITS_TEST_REDIS_URL=redis://localhost:6379/3 .venv/bin/python -m pytest -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning -p no:cacheprovider -q tests/test_first_boot_script.py`

Expected: `13 failed, 19 passed`: las 19 de la Task 2 pasan, y las 13 nuevas fallan (el guion para tras el desellado, sin pasos 4 a 8).

- [ ] **Step 3: Los pasos 4 a 8**

Con este guion, desde la raíz (no se guarda en el repositorio):

````bash
S=/tmp/claude-0/-home-user-Novieri-RMM/21768af2-984e-5cf8-87b2-4dc8a41a4864/scratchpad
cat > "$S/task3_edit.py" <<'TASK3_EDIT'
"""Plan 2026-11-27, Task 3: first-boot.sh gets steps 4 to 7 and step 8's
install code, after the auto-unseal Task 2 put right behind step 3.6 (design
2026-11-27-despliegue-automatizado §3, §4). Two anchors, each exactly once;
nothing is written unless both are."""

from pathlib import Path

SCRIPT = Path("deploy/scripts/first-boot.sh")
text = SCRIPT.read_text()

VARS_A = r'''cd "$(dirname "$(readlink -f "$0")")/.."   # deploy/
etc_dir="${ITS_ETC_DIR:-/etc/it-suite}"
allow_nontty="${ITS_FIRST_BOOT_ALLOW_NONTTY:-0}"
token_file="$etc_dir/gateway-issuer.token"
key_file="$etc_dir/openbao-unseal/unseal.key"
'''
VARS = r'''cd "$(dirname "$(readlink -f "$0")")/.."   # deploy/
deploy_dir="$PWD"
etc_dir="${ITS_ETC_DIR:-/etc/it-suite}"
unit_dir="${ITS_SYSTEMD_DIR:-/etc/systemd/system}"
no_systemctl="${ITS_NO_SYSTEMCTL:-0}"
allow_nontty="${ITS_FIRST_BOOT_ALLOW_NONTTY:-0}"
token_file="$etc_dir/gateway-issuer.token"
key_file="$etc_dir/openbao-unseal/unseal.key"
'''
MAIN_A = r'''# ------------------------------------------------------------------ main

check_state_before_anything
step_1
step_1b
step_1c
step_2
step_3_1
step_3_2
step_3_3
step_3_4
step_3_6
auto_unseal
restart_proof
say "Steps 1 to 3.6 and the auto-unseal: done."
'''
STEPS_4_TO_8 = r'''step_4() {
  say "Step 4 — migrations"
  local newest current code
  newest="$(find ../backend/migrations/versions -maxdepth 1 -name '[0-9][0-9][0-9][0-9]_*.py' -printf '%f\n' | cut -c1-4 | sort | tail -1)"
  [ -n "$newest" ] || die "no revision file in ../backend/migrations/versions: this is not a checkout of the repository"
  current="$(psql_admin 'select version_num from alembic_version' 2>/dev/null | tr -d '[:space:]' || true)"
  if [ "$current" = "$newest" ]; then
    skip "step 4: the schema is at $newest, this checkout's newest revision"
    return 0
  fi
  act "docker compose up migrate"
  dc up migrate
  code="$(oneshot_exit migrate)"
  [ "$code" = 0 ] || die "migrate exited $code" "docker compose logs migrate"
  current="$(psql_admin 'select version_num from alembic_version' | tr -d '[:space:]')"
  [ "$current" = "$newest" ] || die "after migrate the schema is at ${current:-nothing}, not $newest" "docker compose logs migrate"
  ok "step 4: the schema is at $current"
}

step_4b() {
  say "Step 4b — app_user's table privileges"
  local narrow code query
  query="select has_table_privilege('app_user', 'devices', 'SELECT') and not has_table_privilege('app_user', 'audit_events', 'UPDATE')"
  narrow="$(psql_admin "$query" 2>/dev/null | tr -d '[:space:]' || true)"
  if [ "$narrow" = t ]; then
    skip "step 4b: app_user reads devices and cannot rewrite audit_events"
    return 0
  fi
  act "docker compose up postgres-grants"
  dc up postgres-grants
  code="$(oneshot_exit postgres-grants)"
  [ "$code" = 0 ] || die "postgres-grants exited $code" "docker compose logs postgres-grants"
  narrow="$(psql_admin "$query" | tr -d '[:space:]')"
  [ "$narrow" = t ] || die "after postgres-grants, app_user is not narrowed as postgres/grant-app-privileges.sql says" "docker compose logs postgres-grants"
  ok "step 4b: app_user reads devices and cannot rewrite audit_events"
}

step_5() {
  say "Step 5 — the audit HMAC key"
  if dc run --rm --no-deps --entrypoint sh api -c 'test -s /var/lib/its/secrets/audit-hmac.key' >/dev/null 2>&1; then
    skip "step 5: the audit HMAC key exists, and it is never replaced"
  else
    act "32 random bytes into /var/lib/its/secrets/audit-hmac.key, mode 600"
    # `set -C`: the shell refuses to write over a key that is already there.
    dc run --rm --no-deps --entrypoint sh api -c \
      'set -C && head -c 32 /dev/urandom > /var/lib/its/secrets/audit-hmac.key && chmod 600 /var/lib/its/secrets/audit-hmac.key'
  fi
  dc run --rm --no-deps --entrypoint sh api -c 'ls -l /var/lib/its/secrets/audit-hmac.key'
}

step_6() {
  say "Step 6 — the gateway's certificate, and the client CA"
  if dc run --rm --no-deps --entrypoint sh gateway -c \
       'test -s /var/lib/its/tls/gateway.crt && test -s /var/lib/its/tls/gateway.key && test -s /var/lib/its/ca/root.crt' >/dev/null 2>&1; then
    skip "step 6: the gateway's certificate and the client CA exist (step 6b's timer renews them)"
    return 0
  fi
  act "issue-gateway-cert.py $host, the gateway-issuer token on its stdin from $token_file"
  if ! sudo cat "$token_file" | docker compose run --rm --no-deps -T \
         -v "$PWD/scripts/issue-gateway-cert.py:/tmp/issue-gateway-cert.py:ro" \
         --entrypoint python gateway /tmp/issue-gateway-cert.py "$host"; then
    die "the gateway's certificate was not issued (above)" "docker compose logs --tail 50 openbao"
  fi
}

# Step 6b's two units, exactly as deploy/README.md's appendix writes them.
gateway_cert_service() {
  # shellcheck disable=SC2016,SC1003  # $PWD and the backslashes are the unit's
  printf '%s\n' \
    '[Unit]' \
    'Description=Renew the it-suite gateway server certificate' \
    '[Service]' \
    'Type=oneshot' \
    "WorkingDirectory=$deploy_dir" \
    '# The token arrives on STDIN from a 0600 file, never as an argument. `-T` is' \
    '# what keeps stdin available to redirect.' \
    "ExecStart=/bin/bash -lc 'docker compose run --rm --no-deps -T \\" \
    '  -v "$PWD/scripts/issue-gateway-cert.py:/tmp/issue-gateway-cert.py:ro" \' \
    "  --entrypoint python gateway /tmp/issue-gateway-cert.py $host \\" \
    "  < $token_file \\" \
    "  && docker compose restart gateway'"
}
gateway_cert_timer() {
  printf '%s\n' \
    '[Unit]' \
    'Description=Monthly it-suite gateway certificate renewal' \
    '[Timer]' \
    'OnCalendar=monthly' \
    'Persistent=true' \
    '[Install]' \
    'WantedBy=timers.target'
}

timer_enabled() {
  [ "$no_systemctl" = 1 ] || [ "$(systemctl is-enabled its-gateway-cert.timer 2>/dev/null || true)" = enabled ]
}

step_6b() {
  say "Step 6b — renew the gateway's certificate every month"
  local service="$unit_dir/its-gateway-cert.service" timer="$unit_dir/its-gateway-cert.timer"
  local enabled=", and the timer is enabled"
  [ "$no_systemctl" != 1 ] || enabled=" (ITS_NO_SYSTEMCTL=1: systemd not asked)"
  if [ "$(cat "$service" 2>/dev/null || true)" = "$(gateway_cert_service)" ] \
     && [ "$(cat "$timer" 2>/dev/null || true)" = "$(gateway_cert_timer)" ] && timer_enabled; then
    skip "step 6b: its-gateway-cert.service and .timer are as the runbook writes them$enabled"
    return 0
  fi
  act "its-gateway-cert.service and its-gateway-cert.timer in $unit_dir"
  sudo install -d -m 755 "$unit_dir"
  gateway_cert_service | sudo tee "$service" >/dev/null
  gateway_cert_timer | sudo tee "$timer" >/dev/null
  if [ "$no_systemctl" = 1 ]; then
    note "ITS_NO_SYSTEMCTL=1: units written, systemd not touched"
  else
    sudo systemctl daemon-reload
    sudo systemctl enable --now its-gateway-cert.timer
    sudo systemctl list-timers its-gateway-cert.timer --no-pager
  fi
}

oneshots=" migrate postgres-grants openbao-init "

# stack_state sets stack (ok | wait | fail), stack_text and stack_names.
stack_state() {
  local svc state health code
  stack=ok stack_text="" stack_names=""
  while IFS='|' read -r svc state health code; do
    [ -n "$svc" ] || continue
    case "$state" in
      exited)
        if [[ "$oneshots" == *" $svc "* ]] && [ "$code" = 0 ]; then continue; fi
        stack="fail" stack_text="$stack_text $svc(exited $code)" stack_names="$stack_names $svc" ;;
      running)
        case "$health" in ''|healthy) continue ;; esac
        [ "$stack" = fail ] || stack="wait"
        stack_text="$stack_text $svc($health)" stack_names="$stack_names $svc" ;;
      *)
        [ "$stack" = fail ] || stack="wait"
        stack_text="$stack_text $svc($state)" stack_names="$stack_names $svc" ;;
    esac
  done < <(dc ps -a --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}')
  return 0
}

restart_counts() {
  local ids
  mapfile -t ids < <(dc ps -aq | sort)
  [ "${#ids[@]}" = 0 ] || docker inspect -f '{{.Name}} {{.RestartCount}}' "${ids[@]}"
}

# Every service up -- healthy where it has a health check -- and the three
# one-shots exited 0; and still so 15 s later, with no restart in between: a
# service that restarts for ever is up for a moment at a time (row 15 of
# "What was broken").
wait_stack() {
  local i before after name
  for ((i = 0; i < 300; i += 5)); do
    stack_state
    if [ "$stack" = fail ]; then
      die "a service stopped:$stack_text" "docker compose logs --tail 50$stack_names"
    fi
    if [ "$stack" = ok ]; then
      before="$(restart_counts)"
      sleep 15
      stack_state
      after="$(restart_counts)"
      if [ "$stack" = ok ] && [ "$before" = "$after" ]; then return 0; fi
      if [ "$stack" = ok ]; then
        stack_text="" stack_names=""
        while read -r name _; do
          name="${name#/it-suite-}" && name="${name%-1}"
          stack_text="$stack_text $name(restarted)" stack_names="$stack_names $name"
        done < <(diff <(printf '%s\n' "$before") <(printf '%s\n' "$after") | sed -n 's/^> //p')
      fi
    fi
    sleep 5
  done
  die "after 300 s, still waiting on:$stack_text" "docker compose ps -a" "docker compose logs --tail 50$stack_names"
}

step_7() {
  say "Step 7 — start everything; Traefik obtains the certificate for :443"
  local before after mode connect gateway_cert limits tls_args=()
  before="$(dc ps -aq | sort)"
  dc up -d
  after="$(dc ps -aq | sort)"
  if [ -n "$before" ] && [ "$before" = "$after" ]; then
    skip "step 7: docker compose up -d found every service as it should be, and changed nothing"
  else
    act "docker compose up -d: it started or recreated what was not up"
  fi
  wait_stack
  dc ps -a --format 'table {{.Service}}\t{{.Status}}'
  [ -z "$tls_cafile" ] || tls_args=(--cafile "$tls_cafile")
  ./scripts/check-public-tls.sh "${tls_args[@]}" </dev/null \
    || die "the certificate on :443 is not one a browser accepts (above)" \
           "the usual causes: the A record of $host is not this machine's, :443 is closed at the provider's firewall, or an AAAA record names another machine." \
           "fix the cause, then: docker compose restart traefik && ./scripts/first-boot.sh"
  mode="$(dc exec -T traefik stat -c %a /etc/traefik/acme/acme.json 2>/dev/null | tr -d '[:space:]' || true)"
  [ "$mode" = 600 ] || die "Traefik's acme.json is mode ${mode:-missing}, not 600" "docker compose logs traefik | grep -i acme"
  ok "step 7: Traefik's certificate store, acme.json, is mode 600"
  connect="$(env_get ITMS_BIND_IP)"
  case "$connect" in ''|0.0.0.0) connect=127.0.0.1 ;; esac
  gateway_cert="$(openssl s_client -connect "$connect:8443" -servername "$host" </dev/null 2>/dev/null \
                  | openssl x509 -noout -issuer -ext subjectAltName 2>/dev/null || true)"
  printf '%s\n' "$gateway_cert" | sed 's/^/        /'
  case "$gateway_cert" in
    *"issuer=CN = Novieri RMM internal CA"*"DNS:$host"*) ok "step 7: :8443 presents the internal CA's certificate for $host" ;;
    *) die ":8443 does not present the internal CA's certificate for $host (above), so no agent would connect" \
           "docker compose ps gateway" "docker compose logs --tail 50 gateway traefik" ;;
  esac
  limits="$(dc exec -T api python -c "from app.core.config import get_settings; s = get_settings(); print(s.auth_login_ip_limit, s.auth_factor_ip_limit)" | tr -d '\r')"
  note "step 7: the per-address sign-in limits the api started with: $limits"
}

step_8() {
  say "Step 8 — the install code"
  local needed code
  needed="$(dc exec -T api python -c "import json, urllib.request; print(json.load(urllib.request.urlopen('http://localhost:8000/api/v1/setup/status', timeout=10))['needed'])" 2>/dev/null | tr -d '[:space:]' || true)"
  case "$needed" in
    False) skip "step 8: the installation is claimed" ;;
    True)
      code="$(dc exec -T api cat /var/lib/its/secrets/setup-code | tr -d '[:space:]')"
      [ -n "$code" ] || die "the api wrote no install code" "docker compose logs api | grep -i 'install code'"
      if [ -t 1 ] || [ "$allow_nontty" = 1 ]; then
        printf '\n  install code  %s\n\n' "$code"
        note "open https://$host/ in a browser you trust and enter it (deploy/README.md, First boot, 2): it is a password until you have"
      else
        note "the install code is not printed, the output is not a terminal; read it with: docker compose exec api cat /var/lib/its/secrets/setup-code"
      fi
      code="" ;;
    *) die "the api did not answer /api/v1/setup/status" "docker compose logs --tail 50 api" ;;
  esac
}

finish() {
  say "First boot: every step is done."
  note "Next, in a browser (deploy/README.md, First boot, 2): https://$host/ with the install code;"
  note "your password, your second factor and its ten recovery codes; then an enrollment token at https://$host/enrollment."
  note "Then: ./scripts/first-boot-check.sh"
  note "Custody, off this VPS (GO-LIVE.md §4 and §6): the five shares and the root token, RECOVERY keys now"
  note "(openbao-seal.sh disable, bao operator generate-root), not unseal keys; the key file $key_file,"
  note "apart from any copy of openbao-data (lose it and OpenBao cannot be unsealed); .env, its-secrets and its-ca."
}

# ------------------------------------------------------------------ main

check_state_before_anything
step_1
step_1b
step_1c
step_2
step_3_1
step_3_2
step_3_3
step_3_4
step_3_6
auto_unseal
restart_proof
step_4
step_4b
step_5
step_6
step_6b
step_7
step_8
finish
'''

for anchor in (VARS_A, MAIN_A):
    assert text.count(anchor) == 1, anchor.splitlines()[0]
text = text.replace(VARS_A, VARS).replace(MAIN_A, STEPS_4_TO_8)
SCRIPT.write_text(text)
print("first-boot.sh: steps 4 to 8")
TASK3_EDIT
python3 "$S/task3_edit.py"
````

Expected: `first-boot.sh: steps 4 to 8`. El archivo resultante tiene 814 líneas (`wc -l deploy/scripts/first-boot.sh`).

- [ ] **Step 4: Las pruebas pasan**

Run, desde `backend/`: la orden del Step 2, y `.venv/bin/ruff check app tests`. Desde la raíz: `shellcheck deploy/scripts/first-boot.sh && echo SHELLCHECK-CLEAN`.

Expected: `32 passed` (unos 50 s: cada prueba arranca el guion entero contra el arnés); `All checks passed!`; `SHELLCHECK-CLEAN`.

- [ ] **Step 5: Commit**

```bash
git add deploy/scripts/first-boot.sh backend/tests/test_first_boot_script.py
git commit -m "feat(deploy): first-boot.sh, steps 4 to 8, with OpenBao unsealing itself

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013TnTdGAz4rSe1YRXXjEqyr"
```

---

### Task 4: `first-boot-check.sh` (oleada 1, en paralelo con las Tasks 1 y 2)

**Files:**
- Create: `deploy/scripts/first-boot-check.sh` (ejecutable)
- Test: `backend/tests/test_first_boot_check_script.py` (con su propio `docker` falso, para no depender de la Task 2)

**Interfaces:**
- Consumes: el stack que deja `first-boot.sh` (no su código); `./scripts/install-backup-timer.sh` y `./scripts/scheduled-backup.sh` tal como están (`ITS_NO_SYSTEMCTL`, `ITS_SYSTEMD_DIR`, `ITS_BACKUP_STATE_DIR` ya son suyos); la línea del agente `msg=enrolled device_id=<uuid>` (`agent/internal/enroll/enroll.go:493`, con el `slog` de texto de `cmd/agent/main.go:267`).
- Produces:
  - `./scripts/first-boot-check.sh [--tls-cafile PEM] [--connect IPv4] [--wait-job SEGUNDOS]` desde `deploy/`. `--tls-cafile` es la única CA en la que confían los `curl` del 11a y el agente (montada como su `/etc/ssl/certs/ca-certificates.crt`); `--connect` hace que los `curl` llamen a `<host>:443` en esa dirección (`--resolve`, `--noproxy '*'`) y que el agente la tenga como `--add-host <host>:<dirección>`; `--wait-job` (por defecto 600) es lo que espera el 11d. Lee el token de inscripción por la entrada estándar (oculto en una terminal; termine o no en salto de línea). Sale 0 o 1 con `FAILED`.
  - **Sus dos primeras líneas, siempre** (resolución Q8), antes de preguntar nada a OpenBao: `  note    this enrols a throwaway agent on this VPS (step 11c) and retires it, with its key (11f).` y `  note    It is safe to run again: a second run enrols and retires another; the first stays in the console, offline.` (la Task 6 lo dice en el runbook).
  - **Líneas que lee la CI (Task 5) y cita el runbook (Task 6)**, exactas: `  ok      step 11c: the agent enrolled as device <uuid>, connected over :8443, and its token file is gone` (la CI espera esta línea antes de pedir el trabajo); `Step 12 — the first backup, so that there is a way back`; `  ok      step 12: the newest backup: <hora>`; las dos `note` de la copia fuera del host; y la última cabecera, `First boot is complete.`, seguida de tres `note`.
  - **El agente**: `docker run -d --name smoke-agent --network host --user <uid>:<gid>` con el binario `../agent/dist/novieri-agent`, la CA, su directorio de datos y `ITS_AGENT_ENROLL_URL=https://<host>`; el token, en `<datos>/enroll-token` (0600), nunca en su entorno. Para las pruebas de la Task 5, la orden empieza en `  run_args=(-d --name smoke-agent --network host` y contiene `-v "${tls_cafile:-/etc/ssl/certs/ca-certificates.crt}:/etc/ssl/certs/ca-certificates.crt:ro"`, `-e "ITS_AGENT_ENROLL_URL=https://$host"` y `[ -z "$connect" ] || run_args+=(--add-host "$host:$connect")`.
  - `ITS_SMOKE_AGENT_DIR` (solo pruebas; por defecto `/tmp/smoke-agent`, el del runbook).

**Decisiones de la tarea.**
- **11d acepta cualquier trabajo que haya pedido una persona** para **ese** dispositivo (el `device_id` de la línea `msg=enrolled` del agente, comprobado con su forma de UUID antes de entrar en la consulta), firmado y terminado en `succeeded`. En el VPS es el `inventory.collect` del botón; en la CI, el `hello` de `agent_flow.py`. La consulta del runbook mira el último trabajo pedido de toda la base; la del guion, el de su dispositivo, para no tomar por bueno otro que hubiera.
- **Relanzarlo tras un fallo no pide otro token si el agente sigue en marcha** (de la ejecución que paró en el 11d): lo reutiliza. Tras una ejecución completa, el 11f ya lo retiró, y una segunda inscribe y retira otro agente de prueba; el primero queda en la consola, desconectado (resolución Q8 del coordinador: así está bien, y el guion lo dice en sus dos primeras líneas, en cada ejecución, también en la que para porque OpenBao está sellado). `test_a_second_run_retires_its_own_agent_and_keeps_the_backup_it_has` fija que la segunda inscribe el suyo, lo retira y conserva la copia que encuentra (el 12 dice `skip`); `test_its_first_lines_say_it_enrols_and_retires_a_throwaway_agent`, las dos líneas, también cuando OpenBao está sellado.
- **El binario no se vuelve a compilar si es más nuevo que cada archivo de `agent/`**: en la CI lo compila antes el paso nativo de Go, y así el guion no repite la compilación en un contenedor.
- **El 12 con `sudo journalctl`**: una cuenta creada por `adduser` no está en `adm` ni en `systemd-journal`, y sin `sudo` el `journalctl` del runbook no enseña nada. Con `ITS_NO_SYSTEMCTL=1` corre el `ExecStart` del servicio, `sudo ./scripts/scheduled-backup.sh`, como el ensayo del 2026-11-24 (D7).
- **Lo primero, `bao status`** (`CLAUDE.md`: toda secuencia tras el primer arranque empieza preguntando a OpenBao): sellado, el guion para con la orden de «After a reboot or a restart».

- [ ] **Step 0: La worktree**

Desde la raíz de la worktree:

```bash
git fetch -q origin claude/compassionate-bardeen-vgtwus
git log --oneline -1
git merge-base --is-ancestor HEAD origin/claude/compassionate-bardeen-vgtwus && echo ON-PROJECT-BRANCH
git merge-base --is-ancestor 6f616aa HEAD && echo HAS-SPEC
git diff --quiet 6f616aa HEAD -- backend agent deploy .github && echo CODE-AS-MEASURED
```

Expected: `6f616aa docs(spec): the first boot of rmm.novieri.com in three scripts` (o el commit posterior de la rama del coordinador), `ON-PROJECT-BRANCH`, `HAS-SPEC`, `CODE-AS-MEASURED`. Si falta alguna, se para y se avisa al coordinador. Después, la preparación de Global Constraints.

- [ ] **Step 1: Las pruebas**

Crear `backend/tests/test_first_boot_check_script.py`:

```python
"""`deploy/scripts/first-boot-check.sh`, on a stubbed host (design
2026-11-27-despliegue-automatizado §3, §5).

CI's `deploy` job runs it end to end: a real agent, enrolled over :443,
connected over :8443, a job a person asked for, the audit chain, the backup.
What a stub shows that a green run cannot: the refusals (OpenBao sealed, the
installation not claimed, a route not refused, a job refused), the reuse of an
agent left running by a run that stopped, and that the enrollment token is
never an argument, never printed, and only ever in the agent's 0600 file.
A fake `docker` plays compose and the agent; every other command but bash's
builtins passes through a shim that logs its argv.
"""

import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "scripts" / "first-boot-check.sh"
BASH = shutil.which("bash") or "/bin/bash"
SHIMMED = ("cat", "date", "env", "find", "grep", "hostname", "id", "install", "readlink",
           "rm", "sed", "tail", "tr", "dirname")  # fmt: skip
DEVICE = "0194f3a2-7c1d-7e55-9a0b-3c4d5e6f7a8b"

FAKE_DOCKER = r'''
import json, os, sys
STATE = os.environ["FAKE_STATE"]
s = json.load(open(STATE))
argv = sys.argv[1:]
open(os.environ["ARGV_LOG"], "a").write("docker " + " ".join(argv) + "\n")
rc = 0
def out(text):
    sys.stdout.write(text)
if argv[:3] == ["compose", "exec", "-T"]:
    svc, cmd = argv[3], argv[4:]
    if svc == "openbao":
        rc = s["bao_rc"]
    elif svc == "postgres":
        sql = cmd[-1]
        if "user_roles" in sql:
            out(s["admins"])
        elif "from jobs" in sql:
            s["polls"] += 1
            assert "target_id = '%s'" % s["device"] in sql, sql
            out(s["job"] + "\n" if s["polls"] >= s["job_after"] else "")
    elif svc == "api":
        sys.stdin.read()
        out(s["chain"])
elif argv[:2] == ["ps", "-q"]:
    out("c0ffee\n" if s["agent_running"] else "")
elif argv[:3] == ["rm", "-f", "smoke-agent"]:
    s["agent_running"] = False
elif argv[0] == "run" and "golang:1.24-bookworm" in argv:
    s["built"] = True
elif argv[0] == "run" and "busybox:1.36.1" in argv:
    data = next(a.split(":")[0] for a in argv if a.endswith(":/var/lib/novieri-agent"))
    token_file = os.path.join(data, "enroll-token")
    s["token_seen"] = open(token_file).read()
    s["token_mode"] = oct(os.stat(token_file).st_mode & 0o777)
    os.remove(token_file)
    s["agent_args"] = argv
    s["agent_running"] = True
elif argv[0] == "logs":
    out("time=1 level=INFO msg=\"novieri-agent starting\" version=dev enrolled=false\n")
    out("time=2 level=INFO msg=enrolling url=https://rmm.novieri.com/enroll/v1\n")
    out("time=3 level=INFO msg=enrolled device_id=%s gateway_url=wss://rmm.novieri.com:8443/agent\n"
        % s["device"])
    if s["connects"]:
        out("time=4 level=INFO msg=\"gateway connected\" url=wss://rmm.novieri.com:8443/agent\n")
json.dump(s, open(STATE, "w"))
sys.exit(rc)
'''

FAKE_CURL = r'''
import os, sys
open(os.environ["ARGV_LOG"], "a").write("curl " + " ".join(sys.argv[1:]) + "\n")
path = sys.argv[-1].split("rmm.novieri.com", 1)[1]
sys.stdout.write(os.environ.get("FAKE_CODE_" + path.strip("/").replace("/", "_").replace("-", "_"),
                                "401"))
'''

FAKE_BACKUP = r'''
import os, sys
name = os.path.basename(sys.argv[0])
open(os.environ["ARGV_LOG"], "a").write(name + " " + " ".join(sys.argv[1:]) + "\n")
if name == "scheduled-backup.sh":
    state = os.environ["ITS_BACKUP_STATE_DIR"]
    os.makedirs(state, exist_ok=True)
    open(os.path.join(state, "last-success"), "w").write("2026-11-27T10:00:00Z\n")
if name == "install-backup-timer.sh":
    units = os.environ["ITS_SYSTEMD_DIR"]
    os.makedirs(units, exist_ok=True)
    open(os.path.join(units, "its-platform-backup.timer"), "w").write("[Timer]\n")
'''

CHAIN_OK = 'novieri {"ok": true, "count": 34, "broken_at": null}\n'


def real_command(name: str, path: str) -> str | None:
    """The system's own binary, before anything a session put on PATH: a
    wrapper that runs `name` again by name would loop through the shim."""
    for directory in ("/usr/bin", "/bin"):
        candidate = Path(directory) / name
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(name, path=path)


def _exe(path: Path, body: str, python: bool = True) -> None:
    path.write_text((f"#!{sys.executable}\n" if python else "") + body.lstrip("\n"))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Box:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        deploy = tmp / "repo" / "deploy"
        (deploy / "scripts").mkdir(parents=True)
        self.script = deploy / "scripts" / "first-boot-check.sh"
        self.script.write_bytes(SCRIPT.read_bytes())
        for name in ("install-backup-timer.sh", "scheduled-backup.sh"):
            _exe(deploy / "scripts" / name, FAKE_BACKUP)
        (deploy / ".env").write_text("ITS_PUBLIC_HOST=rmm.novieri.com\n")
        agent = tmp / "repo" / "agent"
        (agent / "cmd" / "agent").mkdir(parents=True)
        (agent / "cmd" / "agent" / "main.go").write_text("package main\n")
        self.agent_data = tmp / "smoke-agent"
        self.backup_state = tmp / "backup-state"
        self.units = tmp / "systemd"
        self.log = tmp / "argv.log"
        self.state_file = tmp / "state.json"
        self.token = "ENROLL-" + secrets.token_urlsafe(24)
        self.state = {
            "bao_rc": 0,
            "admins": "first@novieri.example|org-admin\n",
            "device": DEVICE,
            "job": "inventory.collect|succeeded|t",
            "job_after": 2,
            "polls": 0,
            "chain": CHAIN_OK,
            "agent_running": False,
            "connects": True,
            "built": False,
        }
        self.bin = tmp / "bin"
        self.bin.mkdir()
        real_path = os.environ["PATH"]
        for name in SHIMMED:
            real = real_command(name, real_path)
            assert real, name
            _exe(
                self.bin / name,
                f'#!/bin/sh\nprintf \'%s %s\\n\' {name} "$*" >> "$ARGV_LOG"\nexec {real} "$@"\n',
                python=False,
            )
        _exe(self.bin / "sleep", "#!/bin/sh\nexit 0\n", python=False)
        _exe(self.bin / "sudo", '#!/bin/sh\nprintf \'sudo %s\\n\' "$*" >> "$ARGV_LOG"\nexec "$@"\n',
             python=False)  # fmt: skip
        _exe(self.bin / "docker", FAKE_DOCKER)
        _exe(self.bin / "curl", FAKE_CURL)

    def run(
        self, *args: str, stdin: str | None = None, **env: str
    ) -> subprocess.CompletedProcess[str]:
        self.state_file.write_text(json.dumps(self.state))
        self.log.write_text("")
        done = subprocess.run(
            [BASH, str(self.script), *args],
            input=self.token + "\n" if stdin is None else stdin,
            capture_output=True,
            text=True,
            env={
                "PATH": str(self.bin),
                "ARGV_LOG": str(self.log),
                "FAKE_STATE": str(self.state_file),
                "ITS_SMOKE_AGENT_DIR": str(self.agent_data),
                "ITS_BACKUP_STATE_DIR": str(self.backup_state),
                "ITS_SYSTEMD_DIR": str(self.units),
                "ITS_NO_SYSTEMCTL": "1",
                **env,
            },
            timeout=100,
        )
        self.state = json.loads(self.state_file.read_text())
        return done

    def argv(self) -> str:
        return self.log.read_text()


def _ok(out: str) -> list[str]:
    return [line[10:].split(":")[0] for line in out.splitlines() if line.startswith("  ok      ")]


def test_the_script_parses() -> None:
    assert subprocess.run([BASH, "-n", str(SCRIPT)]).returncode == 0


def test_nothing_in_it_traces_or_spills_a_here_string() -> None:
    text = SCRIPT.read_text().splitlines()
    code = "\n".join(line for line in text if not line.lstrip().startswith("#"))
    assert not re.search(r"set -[a-wyz]*x|set -o xtrace|BASH_XTRACEFD", code)
    assert "<<<" not in code
    for heredoc in re.findall(r"<<-?\s*(\S+)", code):
        assert heredoc.startswith("'"), heredoc


def test_a_claimed_installation_passes_every_check(tmp_path: Path) -> None:
    box = Box(tmp_path)
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert _ok(done.stdout) == [
        "OpenBao answers, unsealed (bao status",
        "step 10", "step 11a", "step 11c", "step 11d", "step 11e", "step 11f", "step 12",
    ]  # fmt: skip
    assert "        inventory.collect|succeeded|t" in done.stdout
    assert "NOW, in the console: Devices ->" in done.stdout
    assert done.stdout.rstrip().splitlines()[-4] == "First boot is complete."
    assert box.state["built"] is True


def test_the_token_is_only_ever_in_the_agents_0600_file(tmp_path: Path) -> None:
    box = Box(tmp_path)
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert box.state["token_seen"] == box.token
    assert box.state["token_mode"] == "0o600"
    assert box.token not in box.argv() + done.stdout + done.stderr
    assert not box.agent_data.exists(), "step 11f removes the agent's key and certificate"


def test_the_agent_listens_on_nothing_and_trusts_the_hosts_roots(tmp_path: Path) -> None:
    box = Box(tmp_path)
    assert box.run().returncode == 0
    args = " ".join(box.state["agent_args"])
    assert "--network host" in args
    assert "-v /etc/ssl/certs/ca-certificates.crt:/etc/ssl/certs/ca-certificates.crt:ro" in args
    assert "-e ITS_AGENT_ENROLL_URL=https://rmm.novieri.com" in args
    assert "ITS_AGENT_ENROLL_TOKEN" not in args
    assert "--privileged" not in args and " -p " not in args and "--add-host" not in args


def test_the_ci_dials_an_address_and_trusts_only_the_root_it_gives(tmp_path: Path) -> None:
    box = Box(tmp_path)
    root = tmp_path / "pebble-root.pem"
    root.write_text("-----BEGIN CERTIFICATE-----\n")
    done = box.run("--tls-cafile", str(root), "--connect", "127.0.0.1")
    assert done.returncode == 0, done.stdout + done.stderr
    args = " ".join(box.state["agent_args"])
    assert f"-v {root}:/etc/ssl/certs/ca-certificates.crt:ro" in args
    assert "--add-host rmm.novieri.com:127.0.0.1" in args
    curls = [line for line in box.argv().splitlines() if line.startswith("curl ")]
    assert len(curls) == 4
    for line in curls:
        assert f"--cacert {root}" in line and "--resolve rmm.novieri.com:443:127.0.0.1" in line


def test_a_sealed_openbao_stops_it_before_anything(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["bao_rc"] = 2
    done = box.run()
    assert done.returncode == 1 and "OpenBao is sealed" in done.stderr
    assert "psql" not in box.argv()


def test_an_unclaimed_installation_is_sent_to_the_browser(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["admins"] = ""
    done = box.run()
    assert done.returncode == 1
    assert "no staff account holds org-admin" in done.stderr
    assert "docker compose exec api cat /var/lib/its/secrets/setup-code" in done.stderr


def test_a_route_that_is_not_refused_stops_it(tmp_path: Path) -> None:
    box = Box(tmp_path)
    done = box.run(FAKE_CODE_api_v1_jobs="404")
    assert done.returncode == 1
    assert "did not answer 401: /api/v1/jobs=404" in done.stderr


def test_a_refused_job_stops_it(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["job"] = "inventory.collect|refused|t"
    done = box.run()
    assert done.returncode == 1 and "the job ended: inventory.collect|refused|t" in done.stderr


def test_no_job_in_time_says_what_to_press(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["job_after"] = 10**6
    done = box.run("--wait-job", "1")
    assert done.returncode == 1
    assert "no job a person asked for reached this device in 1 s" in done.stderr
    assert "Run job -> Collect inventory" in done.stderr


def test_an_agent_left_running_is_reused_and_no_token_is_asked(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["agent_running"] = True
    done = box.run(stdin="")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "skip    steps 11b and 11c: the throwaway agent smoke-agent is running" in done.stdout
    assert "busybox" not in box.argv()


def test_an_agent_that_never_connects_stops_it(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["connects"] = False
    done = box.run()
    assert done.returncode == 1
    assert "the agent did not connect to the gateway within 60 s" in done.stderr


def test_a_broken_audit_chain_stops_it(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["chain"] = CHAIN_OK + 'other {"ok": false, "count": 3, "broken_at": "x"}\n'
    done = box.run()
    assert done.returncode == 1 and "an audit chain is broken" in done.stderr


def test_a_second_run_retires_its_own_agent_and_keeps_the_backup_it_has(tmp_path: Path) -> None:
    """Coordinator's ruling Q8 (2026-11-27): safe to run again. A second whole
    run enrols another throwaway agent and retires it too; the backup it
    finds is kept."""
    box = Box(tmp_path)
    assert box.run().returncode == 0
    again = box.run()
    assert again.returncode == 0, again.stdout + again.stderr
    assert "busybox:1.36.1" in box.argv(), "the second run enrols an agent of its own"
    assert box.state["agent_running"] is False and not box.agent_data.exists()
    kept = "skip    step 12: the nightly backup timer is installed, and a backup exists"
    assert kept in again.stdout
    assert "scheduled-backup.sh" not in box.argv()


OPENING = [
    "  note    this enrols a throwaway agent on this VPS (step 11c) and retires it, with its key"
    " (11f).",
    "  note    It is safe to run again: a second run enrols and retires another; the first stays"
    " in the console, offline.",
]


def test_its_first_lines_say_it_enrols_and_retires_a_throwaway_agent(tmp_path: Path) -> None:
    """Ruling Q8: read before anything happens -- on a run that stops at the
    first check too."""
    box = Box(tmp_path)
    assert box.run().stdout.splitlines()[:2] == OPENING
    box.state["bao_rc"] = 2
    stopped = box.run()
    assert stopped.returncode == 1 and stopped.stdout.splitlines()[:2] == OPENING


def test_a_binary_newer_than_its_sources_is_not_built_again(tmp_path: Path) -> None:
    box = Box(tmp_path)
    binary = tmp_path / "repo" / "agent" / "dist" / "novieri-agent"
    binary.parent.mkdir()
    binary.write_text("elf")
    binary.chmod(0o755)
    later = (tmp_path / "repo" / "agent" / "cmd" / "agent" / "main.go").stat().st_mtime + 10
    os.utime(binary, (later, later))
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "skip    step 11c: agent/dist/novieri-agent is newer than every file" in done.stdout
    assert box.state["built"] is False
```

- [ ] **Step 2: Las pruebas fallan**

Run, desde `backend/`: `ITS_TEST_REDIS_URL=redis://localhost:6379/4 .venv/bin/python -m pytest -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning -p no:cacheprovider -q tests/test_first_boot_check_script.py`

Expected: `17 failed` (el guion no existe).

- [ ] **Step 3: El guion**

Crear `deploy/scripts/first-boot-check.sh` y hacerlo ejecutable (`chmod +x deploy/scripts/first-boot-check.sh`):

```bash
#!/usr/bin/env bash
#
# Prove that the first boot worked: deploy/README.md, First boot, 3 -- steps
# 10, 11a, 11c to 11f and 12 of its appendix (design
# 2026-11-27-despliegue-automatizado §2, §3).
#
#   ./scripts/first-boot-check.sh
#
# Run it after first-boot.sh and the browser: the installation claimed (step
# 8), your second factor enrolled (step 9), an enrollment token in hand (step
# 11b). It asks for that token (hidden), starts a throwaway agent on this VPS
# (11c), says when to press Run job -> Collect inventory and waits for the job
# (11d), verifies the audit chain (11e), retires the agent (11f) and takes the
# first backup (12). It prints `run` before what it changes, `skip` for what
# it finds done, `ok` for a check that held. It is safe to run again, after a
# failure or after a success: a run that stopped finds its agent still up and
# goes on from 11d; a second whole run enrols, and retires, another throwaway
# agent, and the first stays in the console as a record, offline. Its first
# two lines say so.
#
# The token reaches the agent in a 0600 file in its data directory, as the
# runbook puts it there, and nowhere else: never an argument, never printed.
#
# Options: --tls-cafile PEM (the root :443 chains to, for the curls and the
# agent: Let's Encrypt staging's on a rehearsal, Pebble's in CI); --connect
# ADDRESS (dial ITS_PUBLIC_HOST at this address instead of asking DNS: the
# CI); --wait-job SECONDS (default 600, step 11d). Environment, for the CI and
# a rehearsal: ITS_NO_SYSTEMCTL=1 (step 12 runs the backup service's command
# itself) and ITS_SYSTEMD_DIR; for its tests: ITS_SMOKE_AGENT_DIR (the agent's
# data directory, /tmp/smoke-agent) and ITS_BACKUP_STATE_DIR
# (scheduled-backup.sh's own).
set -euo pipefail

case "$-" in
  *x*) printf 'first-boot-check.sh: refusing to run under xtrace (bash -x): it would print the token\n' >&2
       exit 1 ;;
esac

if [ -t 1 ]; then bold=$'\033[1m'; plain=$'\033[0m'; else bold=""; plain=""; fi
say()  { printf '\n%s%s%s\n' "$bold" "$*" "$plain"; }
ok()   { printf '  ok      %s\n' "$*"; }
skip() { printf '  skip    %s\n' "$*"; }
act()  { printf '  run     %s\n' "$*"; }
note() { printf '  note    %s\n' "$*"; }
die() {
  printf '\nFAILED  %s\n' "$1" >&2
  shift
  local line
  for line in "$@"; do printf '        %s\n' "$line" >&2; done
  exit 1
}

tls_cafile="" connect="" wait_job=600
while [ $# -gt 0 ]; do
  case "$1" in
    --tls-cafile) [ $# -ge 2 ] || die "--tls-cafile needs a file"; tls_cafile="$2"; shift 2 ;;
    --connect) [ $# -ge 2 ] || die "--connect needs an address"; connect="$2"; shift 2 ;;
    --wait-job) [ $# -ge 2 ] || die "--wait-job needs seconds"; wait_job="$2"; shift 2 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" "./scripts/first-boot-check.sh --help" ;;
  esac
done
[[ $wait_job =~ ^[1-9][0-9]*$ ]] || die "--wait-job needs a whole number of seconds"
if [ -n "$connect" ]; then
  [[ $connect =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "--connect needs an IPv4 address, such as 127.0.0.1"
fi
# Resolved before the cd below, and absolute because docker mounts it.
if [ -n "$tls_cafile" ]; then
  [ -s "$tls_cafile" ] || die "--tls-cafile $tls_cafile is missing or empty"
  tls_cafile="$(readlink -f "$tls_cafile")"
fi

cd "$(dirname "$(readlink -f "$0")")/.."   # deploy/
note "this enrols a throwaway agent on this VPS (step 11c) and retires it, with its key (11f)."
note "It is safe to run again: a second run enrols and retires another; the first stays in the console, offline."
unit_dir="${ITS_SYSTEMD_DIR:-/etc/systemd/system}"
no_systemctl="${ITS_NO_SYSTEMCTL:-0}"
backup_state="${ITS_BACKUP_STATE_DIR:-/var/lib/it-suite/backup}"   # scheduled-backup.sh's
agent_data="${ITS_SMOKE_AGENT_DIR:-/tmp/smoke-agent}"
agent_bin="$PWD/../agent/dist/novieri-agent"
[ -f .env ] || die ".env is missing: this runs after first-boot.sh" "./scripts/first-boot.sh"
host="$(sed -n 's/^ITS_PUBLIC_HOST=//p' .env | tail -1)"
[ -n "$host" ] || die ".env has no ITS_PUBLIC_HOST" "./scripts/first-boot.sh"

dc() { docker compose "$@" </dev/null; }
psql_admin() { dc exec -T postgres psql -X -U its_admin -d itsuite -tAc "$1"; }

# ------------------------------------------------------------ OpenBao

say "OpenBao"
rc=0
dc exec -T openbao bao status >/dev/null 2>&1 || rc=$?
case "$rc" in
  0) ok "OpenBao answers, unsealed (bao status: Sealed false)" ;;
  2) die "OpenBao is sealed" "deploy/README.md, After a reboot or a restart: sudo ./scripts/openbao-seal.sh status" ;;
  *) die "OpenBao does not answer" "docker compose up -d && docker compose exec openbao bao status" ;;
esac

# ------------------------------------------------------------ step 10

say "Step 10 — an administrator"
admins="$(psql_admin "select u.email, r.name from users u join user_roles ur on ur.user_id = u.id join roles r on r.id = ur.role_id where u.kind = 'staff'" | tr -d '\r')"
printf '%s\n' "$admins" | sed '/^$/d; s/^/        /'
case "$admins" in
  *"|org-admin"*) ok "step 10: a staff account holds org-admin" ;;
  *) die "no staff account holds org-admin: the installation is not claimed, or the claim did not complete" \
         "in a browser: https://$host/ with the install code (docker compose exec api cat /var/lib/its/secrets/setup-code)," \
         "then sign in and enrol your second factor (deploy/README.md, First boot, 2); then: ./scripts/first-boot-check.sh" ;;
esac

# ----------------------------------------------------------- step 11a

say "Step 11a — authorization is enforced server-side"
curl_args=(-s -o /dev/null -w '%{http_code}')
[ -z "$tls_cafile" ] || curl_args+=(--cacert "$tls_cafile")
[ -z "$connect" ] || curl_args+=(--noproxy '*' --resolve "$host:443:$connect")
refused=""
for path in /api/v1/devices /api/v1/jobs /api/v1/audit/verify /api/v1/enrollment-tokens; do
  code="$(curl "${curl_args[@]}" "https://$host$path" </dev/null || true)"
  printf '  %-28s %s\n' "$path" "$code"
  [ "$code" = 401 ] || refused="$refused $path=$code"
done
[ -z "$refused" ] || die "unauthenticated, these did not answer 401:$refused" \
  "a 404 on all four means Traefik is not routing (deploy/README.md, What was broken, defect 7): docker compose logs --tail 50 traefik"
ok "step 11a: all four answer 401 to a request with no session"

# ------------------------------------------------------ steps 11b, 11c

read_token() {
  token=""
  IFS= read -r -s -p "Enrollment token from step 11b (hidden): " token || [ -n "$token" ] \
    || die "the input ended before the enrollment token" "./scripts/first-boot-check.sh, and paste it at the prompt"
  if [ -t 0 ]; then echo; fi
  token="${token//[[:space:]]/}"
  [ -n "$token" ] || die "the enrollment token was empty" "./scripts/first-boot-check.sh, and paste it at the prompt"
}

say "Step 11c — a throwaway agent on this VPS"
if [ -n "$(docker ps -q --filter name='^smoke-agent$' --filter status=running </dev/null)" ]; then
  skip "steps 11b and 11c: the throwaway agent smoke-agent is running, from an earlier run"
else
  docker rm -f smoke-agent >/dev/null 2>&1 </dev/null || true
  rm -rf "$agent_data"
  if [ -x "$agent_bin" ] && [ -z "$(find ../agent -path ../agent/dist -prune -o -type f -newer "$agent_bin" -print -quit)" ]; then
    skip "step 11c: agent/dist/novieri-agent is newer than every file of agent/"
  else
    act "go build, in golang:1.24-bookworm, as $(id -un)"
    docker run --rm --user "$(id -u):$(id -g)" -v "$PWD/../agent:/src" -w /src -e CGO_ENABLED=0 \
      -e GOCACHE=/tmp/go-cache -e GOMODCACHE=/tmp/go-mod golang:1.24-bookworm \
      go build -trimpath -o dist/novieri-agent ./cmd/agent </dev/null
  fi
  note "step 11b, in the console: https://$host/enrollment -- a token named first endpoint, 24 hours, one use,"
  note "for the site Principal of your own organization's client. It is shown once: paste it here."
  read_token
  install -d -m 700 "$agent_data"
  (umask 077 && printf '%s' "$token" > "$agent_data/enroll-token")
  token=""
  act "the agent, in busybox:1.36.1 on this host's network: it dials https://$host"
  run_args=(-d --name smoke-agent --network host --user "$(id -u):$(id -g)"
    -v "$agent_bin:/usr/local/bin/novieri-agent:ro"
    -v "${tls_cafile:-/etc/ssl/certs/ca-certificates.crt}:/etc/ssl/certs/ca-certificates.crt:ro"
    -v "$agent_data:/var/lib/novieri-agent"
    -e "ITS_AGENT_ENROLL_URL=https://$host" -e ITS_AGENT_DATA_DIR=/var/lib/novieri-agent)
  [ -z "$connect" ] || run_args+=(--add-host "$host:$connect")
  docker run "${run_args[@]}" busybox:1.36.1 /usr/local/bin/novieri-agent </dev/null >/dev/null
fi
for _ in $(seq 1 30); do
  docker logs smoke-agent 2>&1 </dev/null | grep -c 'gateway connected' >/dev/null && break
  sleep 2
done
logs="$(docker logs smoke-agent 2>&1 </dev/null || true)"
printf '%s\n' "$logs" | grep -E 'starting|enrolling|enrolled|gateway connected' | sed 's/^/        /' || true
case "$logs" in
  *"gateway connected"*) ;;
  *) die "the agent did not connect to the gateway within 60 s" "docker logs smoke-agent" \
         "a token that was used, or is past its 24 hours, is refused: make a new one, then: docker rm -f smoke-agent && ./scripts/first-boot-check.sh" ;;
esac
device_id="$(printf '%s\n' "$logs" | sed -n 's/.*msg=enrolled device_id=\([0-9a-f-]*\).*/\1/p' | tail -1)"
[[ $device_id =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] \
  || die "the agent's log names no device it enrolled as" "docker logs smoke-agent"
[ ! -e "$agent_data/enroll-token" ] || die "the agent kept its enrollment token after enrolling" "docker logs smoke-agent"
ok "step 11c: the agent enrolled as device $device_id, connected over :8443, and its token file is gone"

# ----------------------------------------------------------- step 11d

say "Step 11d — round-trip a signed job"
note "NOW, in the console: Devices -> $(hostname) -> Run job -> Collect inventory."
note "Waiting up to $wait_job s for a job a person asked for to reach this device and come back."
deadline=$(( $(date +%s) + wait_job ))
query="select type, status, signature is not null from jobs where target_type = 'device' and target_id = '$device_id' and requested_by is not null order by issued_at desc limit 1"
while :; do
  row="$(psql_admin "$query" | tr -d '\r')"
  case "$row" in
    *"|succeeded|t") break ;;
    *"|failed|"*|*"|expired|"*|*"|refused|"*|*"|cancelled|"*)
      die "the job ended: $row" "docker logs smoke-agent" ;;
  esac
  [ "$(date +%s)" -lt "$deadline" ] || die "no job a person asked for reached this device in $wait_job s" \
    "in the console: Devices -> $(hostname) -> Run job -> Collect inventory; then ./scripts/first-boot-check.sh (the agent stays up)"
  sleep 3
done
printf '        %s\n' "$row"
ok "step 11d: signed by the server, verified by the agent before it ran, and its result back over :8443"

# ----------------------------------------------------------- step 11e

say "Step 11e — the audit chain"
# Not through dc(): its </dev/null would take the place of this here-document.
chain="$(docker compose exec -T api python - <<'EOF'
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
)" || die "the audit chain could not be read" "docker compose logs --tail 50 api"
chain="$(printf '%s\n' "$chain" | tr -d '\r')"
printf '%s\n' "$chain" | sed 's/^/        /'
case "$chain" in
  *'"ok": true'*) ;;
  *) die "no organization's audit chain verifies" "docker compose logs --tail 50 api" ;;
esac
case "$chain" in
  *'"ok": false'*) die "an audit chain is broken (above): restoring the database without its-secrets does this" "docs/07-backup-restore.md §4" ;;
esac
ok "step 11e: every organization's audit chain verifies"

# ----------------------------------------------------------- step 11f

say "Step 11f — retire the throwaway agent"
act "docker rm -f smoke-agent; rm -rf $agent_data"
docker rm -f smoke-agent >/dev/null </dev/null
rm -rf "$agent_data"
ok "step 11f: the agent and its key are gone; the device stays in the console as a record, offline"

# ------------------------------------------------------------ step 12

backup_timer_installed() {
  if [ "$no_systemctl" = 1 ]; then [ -e "$unit_dir/its-platform-backup.timer" ]
  else [ "$(systemctl is-enabled its-platform-backup.timer 2>/dev/null || true)" = enabled ]; fi
}

say "Step 12 — the first backup, so that there is a way back"
last="$(sudo cat "$backup_state/last-success" 2>/dev/null || true)"
if [ -n "$last" ] && backup_timer_installed; then
  skip "step 12: the nightly backup timer is installed, and a backup exists"
else
  act "install-backup-timer.sh, then a backup now"
  if [ "$no_systemctl" = 1 ]; then
    sudo env ITS_NO_SYSTEMCTL=1 ITS_SYSTEMD_DIR="$unit_dir" ./scripts/install-backup-timer.sh </dev/null
    note "ITS_NO_SYSTEMCTL=1: the backup service's command, run here"
    sudo ./scripts/scheduled-backup.sh </dev/null
  else
    sudo ./scripts/install-backup-timer.sh </dev/null
    sudo systemctl start its-platform-backup.service
    sudo journalctl -u its-platform-backup.service --no-pager -n 20
  fi
  last="$(sudo cat "$backup_state/last-success" 2>/dev/null || true)"
  [ -n "$last" ] || die "the first backup did not succeed" "sudo journalctl -u its-platform-backup.service --no-pager -n 50"
fi
ok "step 12: the newest backup: $last"
note "That backup is on this VPS and goes with it. The off-host copy is recommended before anything"
note "real is enrolled: deploy/README.md, The off-host copy, step by step."

say "First boot is complete."
note "Custody, off this VPS, now (deploy/README.md, First boot, 4): the five shares and the root token"
note "(recovery keys), the unseal key file, the ten recovery codes, .env and the its-secrets, its-ca"
note "and openbao-data volumes -- GO-LIVE.md §6."
```

- [ ] **Step 4: Las pruebas pasan**

Run, desde `backend/`: la orden del Step 2, y `.venv/bin/ruff check app tests`. Desde la raíz: `shellcheck deploy/scripts/first-boot-check.sh && echo SHELLCHECK-CLEAN` y `test -x deploy/scripts/first-boot-check.sh && echo EXECUTABLE`.

Expected: `17 passed`; `All checks passed!`; `SHELLCHECK-CLEAN`; `EXECUTABLE`.

- [ ] **Step 5: Commit**

```bash
git add deploy/scripts/first-boot-check.sh backend/tests/test_first_boot_check_script.py
git commit -m "feat(deploy): first-boot-check.sh, steps 10 to 12 of the first boot

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013TnTdGAz4rSe1YRXXjEqyr"
```

---

### Task 5: La CI `deploy` sobre los guiones (oleada 3, en paralelo con la Task 6; se fusiona antes que ella)

**Files:**
- Create: `deploy/ci/first_boot_answers.py` (el operador de la CI ante `first-boot.sh`)
- Create: `backend/tests/test_ci_first_boot.py`
- Modify: `.github/workflows/ci.yml` (el trabajo `deploy`, líneas 369 a 876 en `6f616aa`; ningún otro trabajo)
- Modify: `deploy/ci/agent_flow.py` (entero: dos tokens con una sola sesión, y la fase nueva `refused`), `deploy/ci/mcp_flow.py` (tres códigos de recuperación para `agent_flow.py`, no dos)
- Modify: `backend/tests/test_ci_mcp_flow.py` (seis pruebas), `backend/tests/test_ci_agent_flow.py` (cinco pruebas, y ocho nuevas al final)
- Modify: `docs/superpowers/specs/2026-11-27-despliegue-automatizado-design.md` (§5, la lista de comprobaciones, y §8.2: resolución Q3)

**Interfaces:**
- Consumes: de las Tasks 2 y 3, la línea de órdenes de `first-boot.sh`, sus líneas `  share <n>     …`, `  root token  …`, `  install code  …`, `  skip    step 1: .env exists (new-env.sh never overwrites one), for rmm.novieri.com`, `First boot: every step is done.`, las 16 `skip` de una segunda ejecución y ninguna `run`; las variables `ITS_FIRST_BOOT_ALLOW_NONTTY`, `ITS_NO_SYSTEMCTL`, `ITS_SYSTEMD_DIR`; los textos de su código que las pruebas buscan (Task 2, Interfaces). De la Task 4, `--tls-cafile`, `--connect`, `--wait-job`, la línea `  ok      step 11c:` y `First boot is complete.`, y el texto de `run_args`. De la Task 2, `ITS_PREFLIGHT_SKIP_DNS=1` (con Pebble como CA no se niega). De la Task 1, nada más que pasar `shellcheck`. Del agente, sin cambios (M10): `pins.json` en su directorio de datos, con `job_signing_public_key_pem`; la línea `gateway connected` de cada conexión; y el rechazo, `refused` con `{"refused": "bad_signature"}`, `jobs.refused` en la auditoría y `msg="job refused" … job_type=hello … reason=bad_signature` en su registro.
- Produces:
  - `python3 ci/first_boot_answers.py [--script RUTA] [--require LÍNEA]… [--timeout S] -- <argumentos de first-boot.sh>`, desde `deploy/`: la salida del guion, con cada secreto enmascarado antes de su línea; `SAVED` y las partes 1, 3 y 5 por la entrada estándar del guion en cuanto imprime el token raíz; sale con el código del guion, o con 1 si falta una línea de `--require`, si imprimió un número de partes que no es cinco con su token raíz, o si pasó `--timeout` (por defecto 2400 s).
  - En `GITHUB_ENV`, para los pasos siguientes: `PEBBLE_MINICA`, `COMPOSE_FILE` (primero `docker-compose.yml:ci/acme-pebble.yml`, y tras el primer arranque `docker-compose.yml:ci/acme-pebble.yml:openbao/auto-unseal.yml`), `ITS_PREFLIGHT_SKIP_DNS=1`, `ITS_NO_SYSTEMCTL=1`, `ITS_SYSTEMD_DIR=$RUNNER_TEMP/systemd` y `PEBBLE_ROOT=$RUNNER_TEMP/pebble-root.pem`.
  - `python3 ci/agent_flow.py … --phase token --token-file A [--token-file B]…` (un token de un uso por archivo, con una sola sesión) y `… --phase refused --hostname H --container C --pins-file P` (las tres obligatorias; imprime `agent_flow: phase refused: every assertion held` o `FAIL  …` y sale con 1). `mcp_flow.py` guarda tres códigos de recuperación en el `--state-file`: uno por sesión de `agent_flow.py` (`token`, `job`, `refused`).
  - **El nombre del trabajo no cambia**: `deploy (compose first boot · app_user privileges)`, y tampoco su lista de pasos más allá de los del primer arranque: el caso negativo es el apartado 7c-bis del paso del OAuth, junto al 7c.

**Decisiones de la tarea.**
- **El paso «Throwaway .env» se queda como está** (es el paso 1 de `first-boot.sh`, con Pebble como CA), porque Pebble no arranca sin `.env` y la comprobación del paso 7 necesita su raíz (M5). `first-boot.sh` lo encuentra y dice `skip`, y el conductor exige esa línea (`--require '  skip    step 1: .env exists (new-env.sh never overwrites one), for rmm.novieri.com'`; `test_the_first_boot_is_answered_and_must_end_as_a_good_one_does` la fija, y el ensayo verde la enseña); el paso 1 dentro del guion lo cubren las pruebas de la Task 2 (resolución Q1: aceptado así).
- **Cinco partes y umbral tres en la CI**, como en el VPS: el conductor lee las cinco de la salida del guion, enmascara cada una y devuelve la primera, la tercera y la quinta (no las tres primeras, para que un guion que se quedara solo con las primeras se viera).
- **Todas las comprobaciones de hoy siguen**, cada una donde está ahora su orden: el certificado por ACME (`check-public-tls.sh` en el paso 7 del guion y otra vez en el paso del OAuth, contra la raíz de Pebble); el 503 con la api parada; el agente compilado inscrito por el 443 con un token de un uso, conectado por el 8443 y ejecutando un `hello` firmado, con la cadena de auditoría (`agent_flow.py`, y ahora además el 11e de `first-boot-check.sh`); los privilegios de `app_user` (los mismos tres pasos); la copia y la restauración (los mismos dos pasos, que ahora van al final y paran antes la capa de aplicación); el `worker` sano. **Tres nuevas** (diseño §5, con la resolución Q3): «OpenBao unseals itself after a restart»; «first-boot.sh again», con huella de `.env`, de los dos archivos de secretos, de cada contenedor (su id y, en marcha, su hora de arranque; salido, su código: M9) y del esquema antes y después, ninguna línea `run` y al menos 16 `skip`; y el rojo con la firma mala, el 7c-bis (abajo).
- **El rojo con la firma mala pasa a ser un paso permanente de la CI** (resolución Q3 del coordinador, 2026-11-27; hasta hoy fue un ensayo, plan del 2026-11-23, Task 10, Step 6, «Rojo 2: la firma»), y el diseño §5 lo cuenta como añadido desde el Step 5 de esta tarea. Es el apartado **7c-bis** del paso del OAuth, cuando `first-boot-check.sh` ya terminó: un segundo agente, `ci-agent`, el mismo binario, inscrito con el segundo token de un uso que `--phase token` emitió en la misma sesión que el primero, en la red del stack con `--add-host rmm.novieri.com:172.19.240.250` (la dirección de Traefik en `ci/acme-pebble.yml`, como el `ci-agent` de la CI de hoy) y la raíz de Pebble como única CA. `agent_flow.py --phase refused`, con un tercer código de recuperación: el dispositivo en línea; para el agente, cambia en su `pins.json` la clave de firma de trabajos por una que nadie firma (M10) y lo arranca otra vez; espera a que vuelva a conectar; pide un `hello` que el servidor firma como siempre; y exige `refused`, `{"refused": "bad_signature"}`, que el saludo no vuelva (no se ejecutó), la cadena verificada con `jobs.refused` y sin `jobs.succeeded` para ese trabajo. El paso exige además la línea del rechazo en el registro del agente.
  - *Por qué otro agente y no el de `first-boot-check.sh`:* el 11f lo retira y borra sus datos, y cambiarle la clave a mitad de su ejecución haría fallar el 11d, que es justo lo que ese guion prueba.
  - *Su rojo:* en el Step 2, las pruebas nuevas de `test_ci_agent_flow.py` con un cliente y un `docker` falsos (un agente que ejecuta el `hello`, o que lo rechaza por otra razón, ponen la fase en rojo; sin el tercer código, se niega antes de nada); y en el Step 7, **Rojo 3**, un agente compilado que no verifica la firma: el paso del OAuth se pone en rojo en el 7c-bis, y solo ahí.
  - *Su coste:* unos dos minutos por ejecución (la inscripción, un reinicio del agente y un trabajo).
- **Los temporizadores de systemd no se activan en el ejecutor** (`ITS_NO_SYSTEMCTL=1`): las unidades se escriben en `$RUNNER_TEMP/systemd` y el 12 ejecuta el `ExecStart` del servicio. **El 6b y el 12 quedan sin verificar**, como hasta hoy (resolución Q2: se mantiene así); lo dicen el comentario del paso del primer arranque y el informe (Task 7).
- **El agente del caso bueno es el de `first-boot-check.sh`**: en la red del ejecutor (`--network host`) con `--add-host rmm.novieri.com:127.0.0.1`, donde Traefik publica el 443 y el 8443, en lugar del `ci-agent` de hoy, en la red del stack con la dirección de Traefik (que sigue, para el caso negativo del 7c-bis). Lo que se comprueba es lo mismo: el nombre público, el 443 y el 8443 a través de Traefik, la raíz de Pebble como única CA. `agent_flow.py` sigue pidiendo el token (11b) y el `hello` (11d) con la sesión del dueño que crea `mcp_flow.py`; el `hostname` del agente es el del ejecutor (M7).
- **El trabajo no guarda ningún secreto del almacén**: ni la parte, ni el token raíz, ni los dos tokens; los cuatro son de `first-boot.sh`. La prueba de que no viajan en una línea de órdenes es la de la Task 2, sobre cada orden del guion; la de que el registro los enmascara, la de esta tarea.
- **La espera de OpenBao de la CI con `operator init -status` desaparece** con el resto de su primer arranque, y su prueba se rehace con la medida (M1).
- **La huella de la segunda ejecución** toma de cada contenedor su `Id` y, en marcha, su hora de arranque; salido, su código de salida (M9): `docker compose up -d` vuelve a arrancar los de una vez con el mismo `Id`, y una huella con su hora no se repetiría nunca.
- **El ejecutor no es un host de despliegue** (lo da por hecho ya el `down -v` de `teardown`): `teardown` borra con `sudo` lo que los guiones escribieron como root fuera de la copia.

- [ ] **Step 0: La worktree, con las Tasks 1 a 4**

Desde la raíz de la worktree (`p5-t3` lleva ya la `p5-t2`):

```bash
git merge --no-edit p5-t1 p5-t3 p5-t4
git log --oneline -6
for s in prepare-host.sh first-boot.sh first-boot-check.sh; do test -x deploy/scripts/$s && echo "$s ok"; done
grep -c '^  say "First boot: every step is done."$' deploy/scripts/first-boot.sh
cd backend && ITS_TEST_REDIS_URL=redis://localhost:6379/5 .venv/bin/python -m pytest -p no:cacheprovider -q tests/test_prepare_host_script.py tests/test_first_boot_script.py tests/test_first_boot_check_script.py tests/test_preflight_script.py; cd ..
```

Expected: las fusiones; `prepare-host.sh ok`, `first-boot.sh ok`, `first-boot-check.sh ok`; `1` (la última cabecera, en `finish()`, la de la Task 3); y `89 passed` (12, 32, 17 y 28). Si no, se para y se avisa al coordinador.

- [ ] **Step 1: Las pruebas**

Crear `backend/tests/test_ci_first_boot.py`:

```python
"""`deploy/ci/first_boot_answers.py` and the `deploy` job's first boot by the
operator's scripts (design 2026-11-27-despliegue-automatizado §5).

The live run is CI's. These hold what the driver may print and type -- each
secret masked before the line that carries it, the answers on the script's
stdin -- and that the job runs the scripts the way the runbook's operator
does, plus the two checks the design adds: OpenBao unsealing itself after a
restart, and first-boot.sh run again changing nothing.
"""

import ast
import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
DRIVER = REPO / "deploy/ci/first_boot_answers.py"
CI = REPO / ".github/workflows/ci.yml"
BASH = shutil.which("bash") or "/bin/bash"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_ci_first_boot_answers", DRIVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


answers = _load()

#: A stand-in first-boot.sh: prints five shares and a root token as the real
#: one does, then reads SAVED and three shares, which must be the first,
#: third and fifth, and says so without printing them.
FAKE = r"""#!/bin/bash
set -euo pipefail
[ "${ITS_FIRST_BOOT_ALLOW_NONTTY:-}" = 1 ] || { echo "not allowed"; exit 9; }
shares=(S1-aaaa S2-bbbb S3-cccc S4-dddd S5-eeee)
for i in 0 1 2 3 4; do printf '  share %d     %s\n' "$((i + 1))" "${shares[$i]}"; done
printf '  root token  %s\n' s.ROOT-zzzz
read -r answer; [ "$answer" = SAVED ] || { echo "no SAVED"; exit 2; }
for want in S1-aaaa S3-cccc S5-eeee; do
  read -r got; [ "$got" = "$want" ] || { echo "wrong share"; exit 3; }
done
echo "got the three shares"
echo "args: $*"
printf '\n  install code  %s\n\n' CODE-1234
echo "First boot: every step is done."
exit "${FAKE_EXIT:-0}"
"""


def _fake(tmp_path: Path, body: str = FAKE) -> Path:
    script = tmp_path / "first-boot.sh"
    script.write_text(body)
    script.chmod(0o755)
    return script


def _steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = yaml.safe_load(CI.read_text())["jobs"]["deploy"]["steps"]
    return steps


def _run(name: str) -> str:
    for step in _steps():
        if str(step.get("name", "")).startswith(name):
            lines = step["run"].splitlines()
            return "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    raise AssertionError(f"the deploy job has no step named {name!r}...")


def test_it_imports_only_the_standard_library() -> None:
    """CI runs it with the runner's bare `python3`."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(DRIVER.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    assert names <= set(sys.stdlib_module_names) | {"__future__"}, names


def test_every_secret_is_masked_before_its_line_and_the_answers_are_typed(
    tmp_path: Path, capsys: Any
) -> None:
    rc = answers.main(["--script", str(_fake(tmp_path)), "--require", "got the three shares"])
    out = capsys.readouterr().out.splitlines()
    assert rc == 0, out
    secrets = ("S1-aaaa", "S2-bbbb", "S3-cccc", "S4-dddd", "S5-eeee", "s.ROOT-zzzz", "CODE-1234")
    for secret in secrets:
        mask = out.index(f"::add-mask::{secret}")
        shown = next(i for i, line in enumerate(out) if secret in line and "::" not in line)
        assert mask == shown - 1, secret
    assert "got the three shares" in out


def test_its_own_arguments_reach_the_script_after_a_double_dash(
    tmp_path: Path, capsys: Any
) -> None:
    rc = answers.main(["--script", str(_fake(tmp_path)), "--", "--tls-cafile", "/r.pem"])
    assert rc == 0
    assert "args: --tls-cafile /r.pem" in capsys.readouterr().out


def test_a_line_that_never_came_fails_the_run(tmp_path: Path, capsys: Any) -> None:
    rc = answers.main(["--script", str(_fake(tmp_path)), "--require", "something else"])
    assert rc == 1
    assert "FAIL  first-boot.sh never printed: something else" in capsys.readouterr().out


def test_the_scripts_own_failure_is_the_runs(tmp_path: Path) -> None:
    script = _fake(tmp_path, FAKE.replace('exit "${FAKE_EXIT:-0}"', "exit 4"))
    assert answers.main(["--script", str(script)]) == 4


def test_four_shares_and_a_root_token_fail_the_run(tmp_path: Path, capsys: Any) -> None:
    body = FAKE.replace("for i in 0 1 2 3 4;", "for i in 0 1 2 3;")
    assert answers.main(["--script", str(_fake(tmp_path, body))]) == 1
    assert "printed 4 shares with its root token" in capsys.readouterr().out


def test_a_script_that_hangs_is_stopped(tmp_path: Path, capsys: Any) -> None:
    body = f"#!{BASH}\nread -r never\n"
    assert answers.main(["--script", str(_fake(tmp_path, body)), "--timeout", "1"]) == 1
    assert "still running after 1 s" in capsys.readouterr().out


def test_the_answers_are_saved_and_shares_one_three_and_five() -> None:
    shares = {n: f"share-{n}" for n in range(1, 6)}
    assert answers.answers(shares) == "SAVED\nshare-1\nshare-3\nshare-5\n"


# --------------------------------------------------------------------------- #
# The deploy job
# --------------------------------------------------------------------------- #


def test_the_job_runs_the_first_boot_before_anything_else_touches_the_stack() -> None:
    names = [str(step.get("name", step.get("uses", ""))) for step in _steps()]
    order = [
        names.index("shellcheck the first-boot scripts"),
        names.index("Throwaway .env, written by the script a new host uses"),
        names.index("Pebble, and the root it signs this run's certificate with"),
        names.index("first boot -- scripts/first-boot.sh, answered as an operator would"),
        names.index("OpenBao unseals itself after a restart"),
        names.index("first-boot.sh again -- every step done, nothing changed"),
        names.index("grant-app-privileges.sql (twice — must be idempotent)"),
        next(i for i, n in enumerate(names) if n.startswith("the platform's own OAuth server")),
        names.index("backup.sh -> restore.sh round trip, invoked AS DOCUMENTED"),
        names.index("teardown"),
    ]
    assert order == sorted(order), order
    gone = ("postgres — first boot (init-app-user.sh)", "alembic upgrade head")
    assert not set(gone) & set(names), "the first boot is the scripts', not the job's own lines"


def test_the_scripts_are_shellchecked() -> None:
    run = _run("shellcheck the first-boot scripts")
    assert (
        "shellcheck deploy/scripts/prepare-host.sh deploy/scripts/first-boot.sh "
        "deploy/scripts/first-boot-check.sh"
    ) in run


def test_the_ci_knobs_are_the_ones_the_scripts_read() -> None:
    run = _run("Throwaway .env")
    for line in (
        'echo "ITS_PREFLIGHT_SKIP_DNS=1" >> "$GITHUB_ENV"',
        'echo "ITS_NO_SYSTEMCTL=1" >> "$GITHUB_ENV"',
        'echo "ITS_SYSTEMD_DIR=$RUNNER_TEMP/systemd" >> "$GITHUB_ENV"',
    ):
        assert line in run, line
    whole = "\n".join(str(step.get("run", "")) for step in _steps())
    assert "ITS_FIRST_BOOT_ALLOW_NONTTY" not in whole, "the driver sets it, for its one run"
    assert '"ITS_FIRST_BOOT_ALLOW_NONTTY": "1"' in DRIVER.read_text()


def test_the_first_boot_is_answered_and_must_end_as_a_good_one_does() -> None:
    run = _run("first boot -- scripts/first-boot.sh")
    assert "python3 ci/first_boot_answers.py" in run
    assert "--require 'First boot: every step is done.'" in run
    # Ruling Q1 (2026-11-27): the job wrote .env with new-env.sh, as step 1
    # does, so that Pebble could start first; the script must say it skipped it.
    assert (
        "--require '  skip    step 1: .env exists (new-env.sh never overwrites one), "
        "for rmm.novieri.com'"
    ) in run
    assert '-- --tls-cafile "$PEBBLE_ROOT"' in run
    assert "tee" not in run, "the shares go to the masked log, and to no file"
    assert run.index("python3 ci/first_boot_answers.py") < run.index(
        'echo "COMPOSE_FILE=docker-compose.yml:ci/acme-pebble.yml:openbao/auto-unseal.yml" >> '
        '"$GITHUB_ENV"'
    )


def test_openbao_is_restarted_and_must_come_back_unsealed_alone() -> None:
    run = _run("OpenBao unseals itself after a restart")
    assert run.index("docker compose restart openbao") < run.index("bao status -format=json")
    assert "operator unseal" not in run and "sys/unseal" not in run, "no share is given"
    assert "grep -q '\"type\": \"static\"' <<<\"$state\"" in run
    assert "grep -q '\"sealed\": false' <<<\"$state\"" in run


def test_the_second_run_must_change_nothing_and_run_nothing() -> None:
    run = _run("first-boot.sh again")
    for part in (
        "sha256sum .env",
        "sudo sha256sum /etc/it-suite/gateway-issuer.token "
        "/etc/it-suite/openbao-unseal/unseal.key",
        # A one-shot runs again on every `up -d`, under the same id (measured
        # 2026-11-27): its exit code is what must hold, a service's start time.
        "xargs docker inspect -f '{{.Name}} {{.Id}} {{if .State.Running}}{{.State.StartedAt}}"
        "{{else}}exit={{.State.ExitCode}}{{end}}'",
        "select version_num from alembic_version",
        './scripts/first-boot.sh --tls-cafile "$PEBBLE_ROOT" </dev/null',
        '[ "$before" = "$after" ]',
        "grep -q '^  run ' \"$RUNNER_TEMP/again.log\"",
        "[ \"$(grep -c '^  skip ' \"$RUNNER_TEMP/again.log\")\" -ge 16 ]",
    ):
        assert part in run, part
    assert "first_boot_answers" not in run, "no terminal and no answers: it must ask nothing"


def test_the_teardown_removes_what_the_scripts_wrote_as_root() -> None:
    run = _run("teardown")
    assert "docker compose down -v" in run
    written = (
        "/etc/it-suite/gateway-issuer.token",
        "/etc/it-suite/openbao-unseal",
        "/var/backups/it-suite",
        "/var/lib/it-suite",
        '"$RUNNER_TEMP/systemd"',
    )
    for path in written:
        assert path in run, path
```

Y las pruebas de la CI que fijaban el primer arranque propio del trabajo, más las ocho nuevas del caso negativo al final de `test_ci_agent_flow.py`, con este guion, desde la raíz (no se guarda):

````bash
S=/tmp/claude-0/-home-user-Novieri-RMM/21768af2-984e-5cf8-87b2-4dc8a41a4864/scratchpad
cat > "$S/task5_tests.py" <<'TASK5_TESTS'
"""Plan 2026-11-27, Task 5: the CI tests that pinned the job's own first boot
now pin what the job does with the operator's scripts. Each anchor once."""

from pathlib import Path

MCP = Path("backend/tests/test_ci_mcp_flow.py")
AGENT = Path("backend/tests/test_ci_agent_flow.py")


def swap(path: Path, start: str, end: str, new: str) -> None:
    """Replace the text from START up to END (END kept) with NEW."""
    text = path.read_text()
    assert text.count(start) == 1, (path, start)
    i = text.index(start)
    j = text.index(end, i)
    path.write_text(text[:i] + new + text[j:])


# --------------------------------------------------------------- mcp_flow

swap(MCP, "def test_ci_runs_both_phases_through_traefik_on_a_real_openbao", "#: Lines that hold a vault secret", '''\
def _first_boot_code() -> str:
    """deploy/scripts/first-boot.sh without its comment lines."""
    text = (REPO / "deploy/scripts/first-boot.sh").read_text()
    return "\\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_ci_runs_both_phases_through_traefik_on_a_real_openbao() -> None:
    """The deploy job runs the closed phase before the claim and the open
    phase after the api restarted with the opt-in, against the OpenBao
    first-boot.sh initialised: five shares and a threshold of three, as on the
    VPS (design 2026-11-27-despliegue-automatizado §5; until then the job made
    its own, with one share). And it names Keycloak only to assert that
    nothing of it is defined or ran (design §10, falsifier 11)."""
    run = _deploy_step(OAUTH_STEP)
    assert "python3 ci/mcp_flow.py" in run
    closed = run.index("flow --phase closed")
    opened = run.index("flow --phase open")
    restart = run.index('echo "ITS_MCP_REGISTRATION=open" >> .env')
    assert closed < restart < opened, "closed, then the opt-in and a restart, then open"
    first_boot = _deploy_step("first boot -- scripts/first-boot.sh")
    assert "python3 ci/first_boot_answers.py" in first_boot
    code = _first_boot_code()
    assert "dc exec -T openbao bao operator init -key-shares=5 -key-threshold=3" in code
    assert "dc up openbao-init" in code
    assert "operator init" not in run and "openbao-init" not in run, "OpenBao is first-boot.sh's"
    named = [line.strip() for line in run.splitlines() if re.search("keycloak", line, re.I)]
    assert len(named) == 2, named
    assert all(line.startswith("if grep -qi keycloak <<<") for line in named), named


''')

swap(MCP, "#: Lines that hold a vault secret", "def test_ci_restores_the_old_identity_providers_database", '''\
def test_no_secret_of_the_vault_is_in_the_job_any_more() -> None:
    """Since 2026-11-27 the shares, the root token and the runtime tokens are
    first-boot.sh's alone: no step of the job holds one in a variable, so none
    can reach an argv here. That none reaches an argv inside the script is
    `test_first_boot_script.py`'s, over every command it runs; that the job's
    log masks each one first_boot_answers.py sees is `test_ci_first_boot.py`'s.
    The files that held a secret here go however the step ends."""
    steps = yaml.safe_load(CI.read_text())["jobs"]["deploy"]["steps"]
    whole = "\\n".join(str(step.get("run", "")) for step in steps)
    for name in ("$unseal", "$root", "$app_token", "$gw_token", "init_json", "BAO_TOKEN"):
        assert name not in whole, name
    # The rest of the trap (the agent's container and data directory) is
    # asserted whole in test_ci_agent_flow.py, its owner.
    assert (
        """trap 'rm -f "$RUNNER_TEMP/setup-code" "$RUNNER_TEMP/ci-owner.json" """
        in _deploy_step(OAUTH_STEP)
    )


''')

swap(MCP, "def test_openbao_is_waited_for_until_it_answers_not_initialised", "def test_the_api_is_waited_for_until_it_answers_itself", '''\
def test_openbao_is_asked_whether_it_is_initialised_by_bao_status() -> None:
    """Fix round 1 (review minor 4) had the job wait with `operator init
    -status`, held to exit 2 only for a vault not initialised. Measured with
    OpenBao 2.6.1 on 2026-11-27 (plan 2026-11-27): it exits 2 for a refused
    connection too. first-boot.sh reads `initialized` out of `bao status
    -format=json`, which exits 1 when it cannot reach the server, and never
    asks `operator init -status`; the job asks OpenBao nothing of the kind."""
    code = _first_boot_code()
    assert "init -status" not in code
    assert "dc exec -T openbao bao status -format=json" in code
    assert '"initialized"' in code
    assert "bao " not in _deploy_step(OAUTH_STEP)


''')

swap(MCP, "def test_the_files_that_hold_a_secret_are_the_runners_alone", "# ------", '''\
def test_the_files_that_hold_a_secret_are_the_runners_alone() -> None:
    """The install code is masked before anything can print it -- the api
    logs it once when it is created (`app/core/setup_code.py`) -- and so is
    the enrollment token, before first-boot-check.sh is handed it. The
    vault's tokens go into .env from first-boot.sh's step 3.5, which checks
    .env is 0600 first, as step 1 makes it."""
    run = _deploy_step(OAUTH_STEP)
    assert "umask 077" in run
    masked = run.index('echo "::add-mask::$(<"$RUNNER_TEMP/setup-code")"')
    copied = run.index('cat /var/lib/its/secrets/setup-code > "$RUNNER_TEMP/setup-code"')
    assert copied < masked < run.index("flow --phase open")
    token = run.index('echo "::add-mask::$(<"$RUNNER_TEMP/enroll-token")"')
    assert run.index("agents --phase token") < token < run.index("./scripts/first-boot-check.sh")
    code = _first_boot_code()
    mode = code.index('[ "$(stat -c %a .env)" = 600 ]')
    assert mode < code.index('env_put ITS_OPENBAO_TOKEN "$app"')


''')

swap(MCP, "def test_the_deploy_job_copies_no_certificate_and_checks_the_one_acme_gave", "def test_with_the_api_stopped_the_job_expects_503", '''\
def test_the_deploy_job_copies_no_certificate_and_checks_the_one_acme_gave() -> None:
    steps = yaml.safe_load(CI.read_text())["jobs"]["deploy"]["steps"]
    whole = "\\n".join(str(step.get("run", "")) for step in steps)
    assert "deploy/certs" not in whole and "certs/fullchain.pem" not in whole
    assert "openssl req" not in whole
    assert "https://localhost" not in whole
    names = [str(step.get("name", "")) for step in steps]
    pebble = names.index("Pebble, and the root it signs this run's certificate with")
    first_boot = next(i for i, n in enumerate(names) if n.startswith("first boot -- scripts"))
    oauth = next(i for i, n in enumerate(names) if n.startswith(OAUTH_STEP))
    assert pebble < first_boot < oauth
    assert "https://pebble:15000/roots/0" in _deploy_step("Pebble, and the root")
    assert '-- --tls-cafile "$PEBBLE_ROOT"' in _deploy_step("first boot -- scripts/first-boot.sh")
    run = _deploy_step(OAUTH_STEP)
    check = run.index(
        './scripts/check-public-tls.sh --connect 127.0.0.1 --cafile "$pebble_root" --wait 180'
    )
    assert run.index('pebble_root="$PEBBLE_ROOT"') < check < run.index("flow --phase closed")
    assert (
        "flow() { python3 ci/mcp_flow.py --base https://rmm.novieri.com "
        '--resolve rmm.novieri.com:127.0.0.1 --cafile "$pebble_root" "$@"; }'
    ) in run


''')

swap(MCP, "def test_the_address_the_job_asks_pebble_at_is_the_one_the_override_gives_it", "    traefik = services", '''\
def test_the_address_the_job_asks_pebble_at_is_the_one_the_override_gives_it() -> None:
    services = yaml.safe_load(PEBBLE.read_text())["services"]
    pinned = services["pebble"]["networks"]["default"]["ipv4_address"]
    run = _deploy_step("Pebble, and the root")
    assert f"--resolve pebble:15000:{pinned}" in run
''')

# ------------------------------------------------------------- agent_flow

swap(AGENT, "def test_the_agent_enrols_after_the_claim_and_before_the_last_checks", "def test_what_the_agent_held_goes_however_the_step_ends", '''\
CHECK = (
    '| ./scripts/first-boot-check.sh --tls-cafile "$pebble_root" --connect 127.0.0.1 '
    "--wait-job 180"
)
REFUSED = (
    "agents --phase refused --hostname ci-agent --container ci-agent "
    '--pins-file "$agent_data/pins.json"'
)


def test_the_agent_enrols_after_the_claim_and_before_the_last_checks() -> None:
    """first-boot-check.sh is the endpoint since 2026-11-27 (design
    2026-11-27-despliegue-automatizado §5): it starts once the token exists,
    agent_flow.py runs the job once the script says the agent connected, and
    the job waits for the script to finish before its own last checks."""
    run = _run(OAUTH_STEP)
    order = [
        run.index("flow --phase open"),
        run.index("docker compose stop api"),
        run.index('agents --phase token --token-file "$RUNNER_TEMP/enroll-token"'),
        run.index(CHECK),
        run.index("grep -q '^  ok      step 11c:' \\"$RUNNER_TEMP/check.log\\""),
        run.index('agents --phase job --hostname "$(hostname)"'),
        run.index('wait "$check"'),
        run.index("grep -qx 'First boot is complete.' \\"$RUNNER_TEMP/check.log\\""),
        run.index("docker run -d --name ci-agent"),
        run.index(REFUSED),
        run.index("grep -E 'msg=\\"job refused\\".*job_type=hello.*reason=bad_signature'"),
        run.index('if grep -qi keycloak <<<"$running"'),
    ]
    assert order == sorted(order), order


def test_the_agent_dials_the_public_name_and_trusts_only_pebbles_root() -> None:
    """An endpoint's view: rmm.novieri.com at 127.0.0.1, where traefik
    publishes both :443 and :8443, and the CA bundle the agent sees is the root
    that signed :443 -- no -k, no skip-verify, no other trust. The script turns
    the two flags into the agent's `--add-host` and its only CA file
    (`test_first_boot_check_script.py` runs it); the token reaches it on
    stdin, never in its environment."""
    run = _run(OAUTH_STEP)
    assert CHECK in run
    assert '{ cat "$RUNNER_TEMP/enroll-token"; echo; } \\\\' in run
    assert "ITS_AGENT_ENROLL_TOKEN" not in run, "the token travels in a 0600 file, never in env"
    script = (REPO / "deploy/scripts/first-boot-check.sh").read_text()
    start = script.index("  run_args=(-d --name smoke-agent --network host")
    command = script[start : script.index("busybox:1.36.1 /usr/local/bin/novieri-agent", start)]
    bundle = "/etc/ssl/certs/ca-certificates.crt"
    assert f'-v "${{tls_cafile:-{bundle}}}:{bundle}:ro"' in command
    assert '-e "ITS_AGENT_ENROLL_URL=https://$host"' in command
    assert '[ -z "$connect" ] || run_args+=(--add-host "$host:$connect")' in command
    assert "ITS_AGENT_ENROLL_TOKEN" not in script
    assert "--privileged" not in command and "-p " not in command, "the agent listens on nothing"


def test_the_gateway_issuer_token_is_first_boots_and_travels_on_stdin() -> None:
    """The gateway's certificate is first-boot.sh's step 6 since 2026-11-27:
    the gateway-issuer token goes from its 0600 file to issue-gateway-cert.py
    on stdin, as the runbook's step 6 sends it, and the job holds none."""
    assert "gw_token" not in _run(OAUTH_STEP)
    script = (REPO / "deploy/scripts/first-boot.sh").read_text()
    assert 'if ! sudo cat "$token_file" | docker compose run --rm --no-deps -T \\\\' in script
    line = next(x for x in script.splitlines() if "/tmp/issue-gateway-cert.py \\"$host\\"" in x)
    assert line.strip() == '--entrypoint python gateway /tmp/issue-gateway-cert.py "$host"; then'


''')

swap(AGENT, "def test_what_the_agent_held_goes_however_the_step_ends", "def test_the_deploy_job_waits_for_a_healthy_worker", '''\
def test_what_the_agent_held_goes_however_the_step_ends() -> None:
    run = _run(OAUTH_STEP)
    # The one place the whole trap is asserted (test_ci_mcp_flow.py holds only
    # its first half): the container goes before the directory it writes to.
    assert (
        """trap 'rm -f "$RUNNER_TEMP/setup-code" "$RUNNER_TEMP/ci-owner.json" """
        """"$RUNNER_TEMP/enroll-token" "$RUNNER_TEMP/check.log"; """
        """docker rm -f smoke-agent ci-agent >/dev/null 2>&1 || true; """
        """rm -rf /tmp/smoke-agent "$RUNNER_TEMP/agent-data"' EXIT""" in run
    )
    line = next(x.strip() for x in run.splitlines() if "agents --phase job" in x)
    assert line.endswith(
        '|| { kill "$check" 2>/dev/null || true; cat "$RUNNER_TEMP/check.log"; '
        "docker logs smoke-agent; docker compose logs api gateway traefik; exit 1; }"
    )


''')

OLD_WORKER = """    assert run.index('test ! -e "$agent_data/enroll-token"') < run.index(line)\n"""
NEW_WORKER = (
    """    done = run.index("grep -qx 'First boot is complete.' \\"$RUNNER_TEMP/check.log\\"")\n"""
    """    assert done < run.index(line)\n"""
)
text = AGENT.read_text()
assert text.count(OLD_WORKER) == 1
AGENT.write_text(text.replace(OLD_WORKER, NEW_WORKER))
NEW_TESTS = r'''

# --------------------------------------------------------------------------- #
# The negative half of invariant 1, end to end (coordinator's ruling Q3,
# plan 2026-11-27, Task 5): an agent made to pin a key the server does not
# hold refuses the signed hello with bad_signature and runs nothing.
# --------------------------------------------------------------------------- #


def test_the_negative_case_runs_on_its_own_agent_as_an_endpoint_would() -> None:
    """Its own agent, enrolled with the second token: first-boot-check.sh
    retired its one at 11f. It dials rmm.novieri.com as traefik's address on
    the stack's network and trusts Pebble's root alone; the token is in its
    0600 file, never in its environment; it listens on nothing."""
    run = _run(OAUTH_STEP)
    assert (
        'agents --phase token --token-file "$RUNNER_TEMP/enroll-token" '
        '--token-file "$agent_data/enroll-token"'
    ) in run
    assert 'echo "::add-mask::$(<"$agent_data/enroll-token")"' in run
    start = run.index("docker run -d --name ci-agent")
    command = run[start : run.index("busybox:1.36.1 /usr/local/bin/novieri-agent", start)]
    assert "--network it-suite_default" in command
    assert "--add-host rmm.novieri.com:172.19.240.250" in command
    assert '-v "$pebble_root:/etc/ssl/certs/ca-certificates.crt:ro"' in command
    assert '-v "$agent_data:/var/lib/novieri-agent"' in command
    assert "ITS_AGENT_ENROLL_TOKEN" not in command
    assert "--privileged" not in command and "-p " not in command
    assert run.index('test ! -e "$agent_data/enroll-token"') < run.index(REFUSED)
    line = next(x.strip() for x in run.splitlines() if x.strip().startswith(REFUSED))
    assert line.endswith(
        "|| { docker logs ci-agent; docker compose logs api gateway traefik; exit 1; }"
    )


def test_the_owner_keeps_a_third_recovery_code_for_it() -> None:
    """One recovery code per sign-in: token, job and refused."""
    source = (REPO / "deploy/ci/mcp_flow.py").read_text()
    assert "self.remember_owner(email, password, list(codes)[:3])" in source


class _TokenClient(_FakeClient):
    """Mints a different token each time, and counts the sign-ins."""

    def __init__(self) -> None:
        self.minted = 0
        self.logins = 0

    def request(self, method: str, path: str, **kwargs: Any) -> _Answer:
        if path == "/api/v1/auth/login":
            self.logins += 1
        if path == "/api/v1/enrollment-tokens":
            self.minted += 1
            body = {"token": f"TOKEN-{self.minted}", "max_uses": 1, "site_id": "s1"}
            return _Answer(201, body, path)
        return super().request(method, path, **kwargs)


def test_one_sign_in_mints_one_one_use_token_per_file(tmp_path: Path) -> None:
    client = _TokenClient()
    owner = {"email": "e", "password": "p", "recovery_codes": ["a", "b", "c"]}
    flow = agents.AgentFlow(client, owner)
    first, second = tmp_path / "one", tmp_path / "two"
    flow.token(str(first), str(second))
    assert (first.read_text(), second.read_text()) == ("TOKEN-1", "TOKEN-2")
    assert client.logins == 1 and client.minted == 2
    for path in (first, second):
        assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_the_distrusted_pin_is_an_ed25519_key_nobody_signs_with() -> None:
    """RFC 8410's SubjectPublicKeyInfo around 32 bytes: the agent parses it as
    it parses the real one (x509.ParsePKIXPublicKey, then the Ed25519 type
    check), and no signature verifies against it. The rest of pins.json stays."""
    pins = {"ca_bundle_pem": "CA", "job_signing_public_key_pem": "OLD"}
    new = agents.distrusted(pins, raw=bytes(range(32)))
    assert new["ca_bundle_pem"] == "CA"
    pem = new["job_signing_public_key_pem"].splitlines()
    assert pem[0] == "-----BEGIN PUBLIC KEY-----" and pem[-1] == "-----END PUBLIC KEY-----"
    der = base64.b64decode("".join(pem[1:-1]))
    assert der == bytes.fromhex("302a300506032b6570032100") + bytes(range(32))
    assert agents.distrusted(pins) != agents.distrusted(pins), "a fresh key each time"
    same = {"job_signing_public_key_pem": new["job_signing_public_key_pem"]}
    with pytest.raises(agents.Failed):
        agents.distrusted(same, raw=bytes(range(32)))


class _RefusedClient(_FakeClient):
    """The refused phase's calls: ci-agent online, a hello that ends STATUS
    with RESULT ("GREETING": the greeting it was sent), and an audit log with
    its dispatch and its ending."""

    def __init__(self, status: str, result: Any) -> None:
        self.status, self.result = status, result
        self.sent: dict[str, Any] = {}

    def request(self, method: str, path: str, **kwargs: Any) -> _Answer:
        if path == "/api/v1/devices?limit=200":
            device = {"id": "dev-1", "hostname": "ci-agent", "agent_status": "online"}
            return _Answer(200, {"items": [device]}, path)
        if path == "/api/v1/jobs" and method == "POST":
            self.sent = kwargs["body"]
            return _Answer(201, {"id": "job-1", "signed": True, "status": "queued"}, path)
        if path == "/api/v1/jobs/job-1":
            result = self.result
            if result == "GREETING":
                result = {"greeting": self.sent["params"]["message"]}
            body = {"id": "job-1", "signed": True, "status": self.status, "result": result}
            return _Answer(200, body, path)
        if path == "/api/v1/audit/verify":
            return _Answer(200, {"ok": True, "broken_at": None, "count": 9}, path)
        if path == "/api/v1/audit?limit=200":
            ending = "jobs.refused" if self.status == "refused" else "jobs.succeeded"
            trail = [
                ("enrollment.token_created", "token-1"),
                ("enrollment.device_enrolled", "dev-1"),
                ("gateway.connected", "dev-1"),
                ("jobs.dispatched", "job-1"),
                (ending, "job-1"),
            ]
            items = [
                {"id": f"06b04ea5-2723-7825-8000-00000000000{n}", "action": a, "target_id": t}
                for n, (a, t) in enumerate(trail, 1)
            ]
            return _Answer(200, {"items": items}, path)
        return super().request(method, path, **kwargs)


class _FakeDocker:
    """`docker logs` counts one `gateway connected` per start."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.starts = 1

    def __call__(self, *args: str) -> str:
        self.calls.append(args)
        if args[0] == "start":
            self.starts += 1
        if args[0] == "logs":
            return 'level=INFO msg="gateway connected"\n' * self.starts
        return ""


THREE = {"email": "e", "password": "p", "recovery_codes": ["a", "b", "c"]}


def _refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: _RefusedClient) -> Any:
    monkeypatch.setattr(agents.time, "sleep", lambda _s: None)
    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps({"ca_bundle_pem": "CA", "job_signing_public_key_pem": "OLD"}))
    docker = _FakeDocker()
    flow = agents.AgentFlow(client, THREE, docker=docker)
    flow.refused("ci-agent", "ci-agent", str(pins), 5.0)
    return flow, docker, json.loads(pins.read_text())


def test_a_hello_refused_with_bad_signature_and_not_run_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow, docker, pins = _refused(
        tmp_path, monkeypatch, _RefusedClient("refused", {"refused": "bad_signature"})
    )
    assert flow.reached == set(agents.PHASES["refused"])
    assert [call[0] for call in docker.calls] == ["logs", "stop", "start", "logs"]
    assert pins["ca_bundle_pem"] == "CA" and pins["job_signing_public_key_pem"] != "OLD"


def test_a_hello_the_agent_ran_fails_the_negative_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The falsifier: an agent that stopped verifying runs it, and answers."""
    with pytest.raises(agents.Failed, match="the hello it does not trust ended succeeded"):
        _refused(tmp_path, monkeypatch, _RefusedClient("succeeded", "GREETING"))


def test_a_refusal_for_another_reason_fails_the_negative_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(agents.Failed, match="refused, but not for its signature"):
        _refused(tmp_path, monkeypatch, _RefusedClient("refused", {"refused": "expired"}))


def test_the_negative_case_needs_its_own_recovery_code(tmp_path: Path) -> None:
    owner = {"email": "e", "password": "p", "recovery_codes": ["a", "b"]}
    flow = agents.AgentFlow(_RefusedClient("refused", {}), owner, docker=_FakeDocker())
    with pytest.raises(agents.Failed, match="no third recovery code"):
        flow.refused("ci-agent", "ci-agent", str(tmp_path / "pins.json"), 1.0)
'''
AGENT.write_text(AGENT.read_text().rstrip("\n") + "\n" + NEW_TESTS)
text = AGENT.read_text()
OLD_IMPORTS = "import ast\nimport importlib.util\nimport json\n"
assert text.count(OLD_IMPORTS) == 1
AGENT.write_text(text.replace(OLD_IMPORTS, "import ast\nimport base64\nimport importlib.util\nimport json\n"))
print("the CI tests pin the job that runs the scripts")
TASK5_TESTS
python3 "$S/task5_tests.py"
````

Expected: `the CI tests pin the job that runs the scripts`. Lo que cada prueba adaptada fijaba y dónde lo fija ahora:

| Prueba | Fijaba | Ahora |
|---|---|---|
| `test_ci_runs_both_phases_through_traefik_on_a_real_openbao` | `operator init` con una parte y `run --rm openbao-init` en el paso del OAuth | el orden cerrado → registro abierto → abierto, igual; el OpenBao es el de `first-boot.sh`, cinco partes y umbral tres, y `dc up openbao-init` en su código |
| `test_no_secret_of_the_vault_travels_in_an_argv` → `test_no_secret_of_the_vault_is_in_the_job_any_more` | `$unseal` y `$root` solo por la entrada estándar | ningún paso del trabajo guarda uno; el «nunca en una línea de órdenes» es de `test_first_boot_script.py`, sobre cada orden del guion |
| `test_openbao_is_waited_for_until_it_answers_not_initialised` → `test_openbao_is_asked_whether_it_is_initialised_by_bao_status` | la espera con `operator init -status` | M1: `bao status -format=json` y `"initialized"` en el guion; ningún `operator init -status` |
| `test_the_files_that_hold_a_secret_are_the_runners_alone` | `chmod 600 .env` antes del token raíz; el código enmascarado | el código enmascarado, igual; y el token de inscripción enmascarado antes de dárselo al guion; `.env` comprobado 0600 en el guion antes del primer token |
| `test_the_deploy_job_copies_no_certificate_and_checks_the_one_acme_gave` | `roots/0` antes de la comprobación del certificado, en el paso del OAuth | `roots/0` en el paso de Pebble, antes del primer arranque, que pasa `--tls-cafile "$PEBBLE_ROOT"`; la comprobación, igual |
| `test_the_address_the_job_asks_pebble_at_is_the_one_the_override_gives_it` | `--resolve pebble:15000:` en el paso del OAuth | en el paso de Pebble |
| `test_the_agent_enrols_after_the_claim_and_before_the_last_checks` | el orden emitir certificado → gateway → token → `ci-agent` → trabajo | el orden token → `first-boot-check.sh` → su `ok step 11c` → trabajo → fin del guion → `ci-agent` → fase `refused` → la línea del rechazo en su registro → comprobaciones finales |
| `test_the_agent_dials_the_public_name_and_trusts_only_pebbles_root` | `--network it-suite_default`, `--add-host` a Traefik, la raíz de Pebble como única CA | los mismos tres hechos en `first-boot-check.sh` (`--add-host`, la CA, la URL) y las banderas con que la CI lo llama; sin `ITS_AGENT_ENROLL_TOKEN`, sin `-p`, sin `--privileged` |
| `test_the_gateway_issuer_token_is_masked_and_travels_on_stdin` → `test_the_gateway_issuer_token_is_first_boots_and_travels_on_stdin` | `gw_token` enmascarado y por la entrada estándar | el trabajo no tiene ninguno; `first-boot.sh` lo manda de su archivo 0600 por la entrada estándar |
| `test_what_the_agent_held_goes_however_the_step_ends` | la trampa con `ci-agent` y `agent-data` | la trampa con `enroll-token`, `check.log`, `smoke-agent`, `/tmp/smoke-agent`, `ci-agent` y `$RUNNER_TEMP/agent-data` |
| `test_the_deploy_job_waits_for_a_healthy_worker` | el `worker` tras el agente | el `worker` tras `First boot is complete.` |

Y, nuevas al final de `test_ci_agent_flow.py`, las del caso negativo (resolución Q3):

| Prueba nueva | Fija |
|---|---|
| `test_the_negative_case_runs_on_its_own_agent_as_an_endpoint_would` | los dos tokens con una sesión, los dos enmascarados; `ci-agent` en `it-suite_default` con `--add-host rmm.novieri.com:172.19.240.250`, la raíz de Pebble como única CA y sus datos en `$agent_data`; sin `ITS_AGENT_ENROLL_TOKEN`, sin `-p`, sin `--privileged`; el token borrado antes de la fase `refused`, y los registros si falla |
| `test_the_owner_keeps_a_third_recovery_code_for_it` | `mcp_flow.py` guarda tres códigos |
| `test_one_sign_in_mints_one_one_use_token_per_file` | una sesión, dos tokens distintos, dos archivos 0600 |
| `test_the_distrusted_pin_is_an_ed25519_key_nobody_signs_with` | M10: el DER es el prefijo de RFC 8410 y los 32 bytes; una clave nueva cada vez; el resto de `pins.json` igual; una clave igual a la de antes se rechaza |
| `test_a_hello_refused_with_bad_signature_and_not_run_holds` | la fase, en verde, con un trabajo `refused` por `bad_signature` y un agente que vuelve a conectar |
| `test_a_hello_the_agent_ran_fails_the_negative_case` | un `hello` que acabó `succeeded` (un agente que no verifica) pone la fase en rojo |
| `test_a_refusal_for_another_reason_fails_the_negative_case` | un `refused` por otra razón (`wrong_device`) también |
| `test_the_negative_case_needs_its_own_recovery_code` | sin tercer código, la fase se niega antes de iniciar sesión |

- [ ] **Step 2: Las pruebas fallan**

Run, desde `backend/`, por separado (un error al importar un archivo detiene toda la sesión de pytest):

```bash
ITS_TEST_REDIS_URL=redis://localhost:6379/5 .venv/bin/python -m pytest -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning -p no:cacheprovider -q tests/test_ci_first_boot.py
ITS_TEST_REDIS_URL=redis://localhost:6379/5 .venv/bin/python -m pytest -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning -p no:cacheprovider -q tests/test_ci_mcp_flow.py tests/test_ci_agent_flow.py
```

Expected: `1 error` (al importar: `deploy/ci/first_boot_answers.py` no existe); y `19 failed, 61 passed`: las once de la tabla y las ocho nuevas (`agents.distrusted` no existe, `token` no admite dos archivos, no hay fase `refused` ni `ci-agent` en el trabajo, y `mcp_flow.py` guarda dos códigos).

- [ ] **Step 3: El conductor, los dos flujos y el trabajo**

Crear `deploy/ci/first_boot_answers.py`:

```python
"""The CI's operator at `deploy/scripts/first-boot.sh` (design
2026-11-27-despliegue-automatizado §5).

It runs the script with its output on a pipe -- which the script allows only
with ITS_FIRST_BOOT_ALLOW_NONTTY=1 -- and answers what a person at its
prompts would: `SAVED` once the five shares and the root token are printed,
then three of those shares, on the script's stdin and never in an argv. Every
share, the root token and the install code is masked in the job's log
(`::add-mask::`) before the line that carries it is printed, so the log shows
`***` where a terminal would have shown the secret.

    ITS_FIRST_BOOT_ALLOW_NONTTY=1 python3 ci/first_boot_answers.py \\
        --require 'First boot: every step is done.' -- --tls-cafile "$PEBBLE_ROOT"

Run from deploy/. The exit status is the script's; or 1 when a --require line
never appeared, when it printed shares that were not five with a root token,
or when it was still running after --timeout seconds (then it is killed).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading

SHARE = re.compile(r"^  share ([1-5])     (\S+)$")
ROOT = re.compile(r"^  root token  (\S+)$")
CODE = re.compile(r"^  install code  (\S+)$")
#: The three shares typed back: not the first three, so a script that kept
#: only the first ones it printed would be caught.
TYPED = (1, 3, 5)


def answers(shares: dict[int, str]) -> str:
    return "SAVED\n" + "".join(shares[n] + "\n" for n in TYPED)


def secret_of(line: str) -> str | None:
    for pattern in (SHARE, ROOT, CODE):
        found = pattern.match(line)
        if found:
            return found.group(found.lastindex or 0)
    return None


def run(command: list[str], required: list[str], timeout: float) -> int:
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env={**os.environ, "ITS_FIRST_BOOT_ALLOW_NONTTY": "1"},
    )
    assert proc.stdin is not None and proc.stdout is not None
    timed_out = threading.Event()

    def stop() -> None:
        timed_out.set()
        proc.kill()

    killer = threading.Timer(timeout, stop)
    killer.start()
    shares: dict[int, str] = {}
    root = False
    answered = False
    seen: set[str] = set()
    try:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            secret = secret_of(line)
            if secret is not None:
                print(f"::add-mask::{secret}", flush=True)
            print(line, flush=True)
            share = SHARE.match(line)
            if share:
                shares[int(share.group(1))] = share.group(2)
            root = root or ROOT.match(line) is not None
            if line in required:
                seen.add(line)
            # The root token comes after the five shares: answer then, or, with
            # fewer than five, close stdin so the script's prompt ends at once.
            if root and not answered:
                try:
                    if len(shares) == 5:
                        proc.stdin.write(answers(shares))
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
                answered = True
        code = proc.wait()
    finally:
        killer.cancel()
        if not proc.stdin.closed:
            proc.stdin.close()
    if timed_out.is_set():
        print(f"FAIL  first-boot.sh was still running after {timeout:.0f} s", flush=True)
        return 1
    if root and len(shares) != 5:
        print(f"FAIL  first-boot.sh printed {len(shares)} shares with its root token", flush=True)
        return 1
    missing = [line for line in required if line not in seen]
    if code == 0 and missing:
        print(f"FAIL  first-boot.sh never printed: {'; '.join(missing)}", flush=True)
        return 1
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--script", default="./scripts/first-boot.sh")
    parser.add_argument("--require", action="append", default=[], help="a line it must print")
    parser.add_argument("--timeout", type=float, default=2400.0)
    parser.add_argument("args", nargs="*", help="first-boot.sh's own arguments, after --")
    options = parser.parse_args(argv)
    return run([options.script, *options.args], options.require, options.timeout)


if __name__ == "__main__":
    sys.exit(main())
```

Sustituir `deploy/ci/agent_flow.py` entero por:

```python
"""The CI's live check of an endpoint: the Linux agent built from this
checkout enrols over the stack's front door, connects to the gateway on :8443
with mutual TLS, verifies and runs a signed `hello`, and the audit chain still
verifies (design 2026-11-23-vps-y-marca-novieri §4 and §5; plan 2026-11-23,
Task 10). And the negative half of invariant 1, end to end (coordinator's
ruling Q3, plan 2026-11-27, Task 5): the same binary, made to pin a
job-signing key the server does not hold, refuses a signed `hello` with
`bad_signature` and never runs it.

It drives only the console's own API, signed in as the owner `mcp_flow.py`
created (its `--state-file`: the address, the password and three recovery
codes, mode 0600): one recovery code per phase, because a sign-in spends a
second factor and a TOTP code may not be replayed inside its window. The
agents themselves are started by the `deploy` job between the phases.

    python3 ci/agent_flow.py --base https://rmm.novieri.com \\
        --resolve rmm.novieri.com:127.0.0.1 --cafile "$pebble_root" \\
        --state-file "$RUNNER_TEMP/ci-owner.json" \\
        --phase token --token-file "$RUNNER_TEMP/enroll-token" \\
        --token-file "$RUNNER_TEMP/agent-data/enroll-token"
    python3 ci/agent_flow.py ... --phase job --hostname "$(hostname)"
    python3 ci/agent_flow.py ... --phase refused --hostname ci-agent \\
        --container ci-agent --pins-file "$RUNNER_TEMP/agent-data/pins.json"

`token`: for each `--token-file`, a one-use, one-hour enrollment token for the
organization's own client's Principal site, written to a NEW file, 0600
(`O_EXCL`), and never printed. `job`: the device the agent enrolled, online; a
`hello` dispatched to it, signed by the server and answered by the agent with
its greeting; and the audit chain verified, with the enrolment, the dispatch
and the result in it. `refused`: the device online; its agent stopped, the
job-signing key it pinned at enrollment (`pins.json`) replaced by one nobody
holds the private half of, and started again; a `hello` the server signs as
always, refused with `bad_signature` and no greeting; and the chain verified,
with the dispatch and the refusal in it and no success. It prints what held,
never a token, a password or a code.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import subprocess
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
        "a server-signed hello, answered by the agent",
        "the audit chain verifies, with the agent in it",
    ),
    "refused": (
        "signed in with a recovery code",
        "the agent's device, online",
        "the agent's pinned job-signing key, one the server does not hold",
        "a server-signed hello, refused with bad_signature and never run",
        "the audit chain verifies, with the refusal in it",
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
#: The refused phase's, for its device and its hello.
REFUSAL_TRAIL = (
    "enrollment.device_enrolled",
    "gateway.connected",
    "jobs.dispatched",
    "jobs.refused",
)
TERMINAL = frozenset({"succeeded", "failed", "expired", "refused", "cancelled"})
#: SubjectPublicKeyInfo for Ed25519 (RFC 8410), before the 32 key bytes.
ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


def write_secret(path: str, value: str) -> None:
    """A NEW file, 0600 from its creation (`O_EXCL`); nothing printed."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value)


def distrusted(pins: dict[str, Any], raw: bytes | None = None) -> dict[str, Any]:
    """`pins` with `job_signing_public_key_pem` replaced by an Ed25519 key
    nobody holds the private half of: 32 random bytes in the RFC 8410
    wrapper. The agent parses it as it parses the real one
    (`agent/internal/enroll/enroll.go`, `parseEd25519PublicKey`), and no
    signature the server makes verifies against it."""
    key = raw if raw is not None else os.urandom(32)
    check(len(key) == 32, "an Ed25519 public key is 32 bytes")
    body = base64.b64encode(ED25519_SPKI_PREFIX + key).decode("ascii")
    pem = "-----BEGIN PUBLIC KEY-----\n" + body + "\n-----END PUBLIC KEY-----\n"
    check(pem != pins.get("job_signing_public_key_pem"), "the new key is the old one")
    return {**pins, "job_signing_public_key_pem": pem}


def run_docker(*args: str) -> str:
    """One `docker` command on the runner; its output, both streams."""
    done = subprocess.run(["docker", *args], capture_output=True, text=True, check=True)
    return done.stdout + done.stderr


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


def trail_for(events: list[dict[str, Any]], device_id: str, job_id: str) -> list[str]:
    """The chain's actions, oldest first, that belong to THIS agent and THIS
    hello: the device's enrolment and connection (`target_id` the device's) and
    the hello's `jobs.*` events (`target_id` the job's). The gateway sends the
    device an `inventory.collect` of its own on connect, so a check on names
    alone is satisfied by a job that is not the hello."""
    target = {
        "enrollment.device_enrolled": device_id,
        "gateway.connected": device_id,
        "jobs.dispatched": job_id,
        "jobs.succeeded": job_id,
    }
    return [
        event["action"]
        for event in sorted(events, key=lambda event: str(event["id"]))
        if event["action"] == "enrollment.token_created"
        or target.get(event["action"]) == event.get("target_id")
        or (event["action"] == "jobs.refused" and event.get("target_id") == job_id)
    ]


def trail_in_order(actions: list[str], expected: tuple[str, ...] = AGENT_TRAIL) -> bool:
    """`expected` appears in `actions` as a subsequence, oldest first."""
    rest = iter(actions)
    return all(any(action == want for action in rest) for want in expected)


class AgentFlow:
    def __init__(
        self,
        client: Client,
        owner: dict[str, Any],
        docker: Callable[..., str] = run_docker,
    ) -> None:
        self.http = client
        self.owner = owner
        self.docker = docker
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

    def token(self, *token_files: str) -> None:
        """One one-use token per file: one sign-in, however many agents."""
        check(len(token_files) >= 1, "no --token-file to write a token to")
        self.sign_in(0)
        site = principal_site(
            self.get("/api/v1/clients"), lambda cid: self.get(f"/api/v1/clients/{cid}/sites")
        )
        ok("the site: the internal client's Principal")
        self.done("the Principal site of the organization's own client")
        for token_file in token_files:
            made = self.http.request(
                "POST",
                "/api/v1/enrollment-tokens",
                body={"name": "ci agent", "expires_in_hours": 1, "max_uses": 1, "site_id": site},
                console=True,
            )
            check(made.status == 201, f"POST /api/v1/enrollment-tokens answered {made.status}")
            body = made.json()
            check(
                body.get("max_uses") == 1 and body.get("site_id") == site, "not the token asked for"
            )
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

    def distrust(self, container: str, pins_file: str, wait_s: float) -> None:
        """Stop the agent, replace the key it pinned, start it, and wait for it
        to connect again (one more `gateway connected` in its log)."""
        before = self.docker("logs", container).count("gateway connected")
        self.docker("stop", container)
        with open(pins_file, encoding="utf-8") as handle:
            pins = json.load(handle)
        with open(pins_file, "w", encoding="utf-8") as handle:
            json.dump(distrusted(pins), handle)
        self.docker("start", container)
        deadline = time.monotonic() + wait_s
        while self.docker("logs", container).count("gateway connected") <= before:
            if time.monotonic() > deadline:
                raise Failed(f"{container} did not connect again in {wait_s:.0f} s")
            time.sleep(2)
        ok("the agent pins a job-signing key the server does not hold, and is connected again")
        self.done("the agent's pinned job-signing key, one the server does not hold")

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
        ok("hello: server-signed, answered by the agent with its greeting")
        self.done("a server-signed hello, answered by the agent")

        verified = self.get("/api/v1/audit/verify")
        check(
            verified.get("ok") is True and verified.get("broken_at") is None, "the chain is broken"
        )
        events = self.get("/api/v1/audit?limit=200")["items"]
        check(
            trail_in_order(trail_for(events, device_id, str(job["id"]))),
            f"the audit log lacks, for this device and this hello, in order: "
            f"{', '.join(AGENT_TRAIL)}",
        )
        ok(f"the audit chain verifies over {verified['count']} events, the agent's included")
        self.done("the audit chain verifies, with the agent in it")


    def refused(self, hostname: str, container: str, pins_file: str, wait_s: float) -> None:
        check(
            len(self.owner["recovery_codes"]) >= 3,
            "the owner's state file holds no third recovery code for this phase",
        )
        self.sign_in(2)
        device_id = self.device(hostname, wait_s)
        ok(f"the device {hostname} enrolled and is online")
        self.done("the agent's device, online")
        self.distrust(container, pins_file, wait_s)

        message = "novieri-ci-refused-" + secrets.token_hex(4)
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
        check(job["status"] == "refused", f"the hello it does not trust ended {job['status']}")
        check(
            job.get("result") == {"refused": "bad_signature"},
            f"refused, but not for its signature: {job.get('result')}",
        )
        check(message not in json.dumps(job), "the refused hello's greeting came back: it ran")
        ok("hello: server-signed, refused by the agent with bad_signature, never run")
        self.done("a server-signed hello, refused with bad_signature and never run")

        verified = self.get("/api/v1/audit/verify")
        check(
            verified.get("ok") is True and verified.get("broken_at") is None, "the chain is broken"
        )
        events = self.get("/api/v1/audit?limit=200")["items"]
        check(
            trail_in_order(trail_for(events, device_id, str(job["id"])), REFUSAL_TRAIL),
            f"the audit log lacks, for this device and this hello, in order: "
            f"{', '.join(REFUSAL_TRAIL)}",
        )
        check(
            not any(
                e["action"] == "jobs.succeeded" and e.get("target_id") == str(job["id"])
                for e in events
            ),
            "the audit log says the refused hello succeeded",
        )
        ok(f"the audit chain verifies over {verified['count']} events, the refusal included")
        self.done("the audit chain verifies, with the refusal in it")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True, help="the public entrypoint")
    parser.add_argument("--cafile", help="the root the entrypoint's certificate chains to")
    parser.add_argument(
        "--resolve", type=parse_resolve, action="append", default=[], help="NAME:ADDRESS"
    )
    parser.add_argument("--state-file", required=True, help="the owner's sign-in (mcp_flow.py)")
    parser.add_argument("--phase", choices=tuple(PHASES), required=True)
    parser.add_argument(
        "--token-file", action="append", default=[], help="phase token: where a token goes"
    )
    parser.add_argument("--hostname", help="phases job and refused: the agent's hostname")
    parser.add_argument("--container", help="phase refused: the agent's container")
    parser.add_argument("--pins-file", help="phase refused: the agent's pins.json")
    parser.add_argument("--wait-s", type=float, default=120.0)
    args = parser.parse_args(argv)
    if args.phase == "token" and not args.token_file:
        parser.error("--phase token needs --token-file")
    if args.phase == "job" and not args.hostname:
        parser.error("--phase job needs --hostname")
    if args.phase == "refused" and not (args.hostname and args.container and args.pins_file):
        parser.error("--phase refused needs --hostname, --container and --pins-file")
    pin_resolution(dict(args.resolve))
    try:
        flow = AgentFlow(Client(args.base, args.cafile), read_owner(args.state_file))
        if args.phase == "token":
            flow.token(*args.token_file)
        elif args.phase == "job":
            flow.job(args.hostname, args.wait_s)
        else:
            flow.refused(args.hostname, args.container, args.pins_file, args.wait_s)
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

Y `mcp_flow.py`, con este guion, desde la raíz (no se guarda):

```bash
S=/tmp/claude-0/-home-user-Novieri-RMM/21768af2-984e-5cf8-87b2-4dc8a41a4864/scratchpad
cat > "$S/task5_mcp.py" <<'TASK5_MCP'
"""Plan 2026-11-27, Task 5: mcp_flow.py leaves a third recovery code for
agent_flow.py's `refused` phase (coordinator's ruling Q3). Two anchors."""

from pathlib import Path

FLOW = Path("deploy/ci/mcp_flow.py")
text = FLOW.read_text()
for old, new in (
    (
        "`--state-file`, in phase open, writes the owner's address, password and two\n"
        "recovery codes to a new file,",
        "`--state-file`, in phase open, writes the owner's address, password and three\n"
        "recovery codes to a new file,",
    ),
    (
        "        self.remember_owner(email, password, list(codes)[:2])\n",
        "        self.remember_owner(email, password, list(codes)[:3])\n",
    ),
):
    assert text.count(old) == 1, old
    text = text.replace(old, new)
FLOW.write_text(text)
print("mcp_flow.py: three recovery codes for agent_flow.py")
TASK5_MCP
python3 "$S/task5_mcp.py"
```

Expected: `mcp_flow.py: three recovery codes for agent_flow.py`.

Y el trabajo `deploy`, con este guion, desde la raíz (no se guarda):

````bash
S=/tmp/claude-0/-home-user-Novieri-RMM/21768af2-984e-5cf8-87b2-4dc8a41a4864/scratchpad
cat > "$S/task5_ci.py" <<'TASK5_CI'
"""Plan 2026-11-27, Task 5: the `deploy` job runs the operator's scripts.
Each anchor must occur exactly once; nothing is written unless all do."""

from pathlib import Path

CI = Path(".github/workflows/ci.yml")
text = CI.read_text()


def cut(start: str, end: str) -> str:
    """The text from START up to, not including, END (both once in the file)."""
    assert text.count(start) == 1, start
    assert text.count(end) == 1, end
    i = text.index(start)
    j = text.index(end, i)
    return text[i:j]


ROUND_TRIP = "      - name: backup.sh -> restore.sh round trip, invoked AS DOCUMENTED\n"
K1 = "      - name: the old identity provider's database -- kept by the pre-upgrade backup only, and restored\n"
VERSION = "      - name: IT_SUITE_VERSION selects the image reference (interpolation only)\n"
POSTGRES = "      - name: postgres — first boot (init-app-user.sh)\n"
GRANTS = "      - name: grant-app-privileges.sql (twice — must be idempotent)\n"
OAUTH = "      - name: the platform's own OAuth server and MCP door, live, through Traefik\n"
TEARDOWN = "      - name: teardown\n        if: always()\n"
FRONTEND = "\n  frontend:\n"

round_trip = cut(ROUND_TRIP, K1)
k1 = cut(K1, VERSION)
first_boot_by_hand = cut(POSTGRES, GRANTS)
oauth = cut(OAUTH, TEARDOWN)
teardown = cut(TEARDOWN, FRONTEND)

# 1. The job's own comment.
OLD_COMMENT = cut("    # Exercises the documented first-boot path", "    #\n    # THE NAME STAYS AS IT WAS.")
NEW_COMMENT = """\
    # SINCE 2026-11-27 (design 2026-11-27-despliegue-automatizado §5) THE FIRST
    # BOOT IS THE OPERATOR'S OWN: deploy/scripts/first-boot.sh, answered by
    # ci/first_boot_answers.py as a person at its prompts would, then
    # deploy/scripts/first-boot-check.sh, with the console's clicks made
    # through its API (ci/mcp_flow.py claims the installation, ci/agent_flow.py
    # mints the enrollment token and runs the job). Every check this job made
    # before stays: the certificate by ACME from Pebble, the 503 with the api
    # stopped, the compiled agent enrolled over :443 and connected over :8443
    # running a signed `hello`, the audit chain, app_user's privileges, the
    # backup and the restore. And two are new: OpenBao unseals itself after a
    # restart, and first-boot.sh run again changes nothing.
    #
    # It exists because the test suite provisions app_user's privileges itself
    # (backend/tests/conftest.py::_run_migrations), so the backend job above
    # stays green no matter how broken the deployment scripts are -- exactly
    # how a fresh boot came to abort on an unset APP_DB_PASSWORD unnoticed.
    # Since own authentication phase 4 (plan 2026-11-15, Task 7) it also
    # proves, live: that a pre-upgrade backup keeps the old identity
    # provider's database and a forced restore brings it back (operator
    # decision K1); and the platform's own OAuth server and MCP door through
    # Traefik (deploy/ci/mcp_flow.py).
"""

# 2. shellcheck, first.
CHECKOUT = (
    "    name: deploy (compose first boot · app_user privileges)\n"
    "    runs-on: ${{ vars.CI_RUNS_ON || 'ubuntu-latest' }}\n"
    "    steps:\n      - uses: actions/checkout@v4\n\n"
)
SHELLCHECK = CHECKOUT + """\
      - name: shellcheck the first-boot scripts
        # Design 2026-11-27 §5. The other deploy scripts predate this step and
        # are not held to it here.
        run: |
          command -v shellcheck >/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq shellcheck; }
          shellcheck deploy/scripts/prepare-host.sh deploy/scripts/first-boot.sh deploy/scripts/first-boot-check.sh

"""

# 3. The throwaway .env step: the knobs every later step shares.
OLD_ENV_TAIL = """\
          echo "PEBBLE_MINICA=$RUNNER_TEMP/pebble-minica.pem" >> "$GITHUB_ENV"
          echo "COMPOSE_FILE=docker-compose.yml:ci/acme-pebble.yml" >> "$GITHUB_ENV"
"""
NEW_ENV_TAIL = OLD_ENV_TAIL + """\
          # The scripts' CI knobs (design 2026-11-27 §5): rmm.novieri.com is in
          # no public DNS here, so the pre-flight leaves it unasked and says
          # so; and no systemd is touched -- the units are written to this
          # run's own directory, and step 12 runs the backup's command itself.
          echo "ITS_PREFLIGHT_SKIP_DNS=1" >> "$GITHUB_ENV"
          echo "ITS_NO_SYSTEMCTL=1" >> "$GITHUB_ENV"
          echo "ITS_SYSTEMD_DIR=$RUNNER_TEMP/systemd" >> "$GITHUB_ENV"
"""
OLD_ENV_COMMENT = """\
        # a real credential and nothing is written back to the repo (.env is
        # ignored); the last step fills the two OpenBao tokens.
"""
NEW_ENV_COMMENT = """\
        # a real credential and nothing is written back to the repo (.env is
        # ignored). It is step 1 of first-boot.sh, run here because Pebble
        # cannot start without .env and first-boot.sh's step 7 needs Pebble's
        # root: first-boot.sh then finds .env and says `skip`.
"""

PEBBLE_AND_FIRST_BOOT = """\
      - name: Pebble, and the root it signs this run's certificate with
        # Pebble makes a new root on every start: the one that will sign this
        # run's certificate is read from its management port (15000), which is
        # published nowhere and answers on its address in the network. Every
        # later step trusts that file, and that file alone, for :443.
        working-directory: deploy
        run: |
          set -euo pipefail
          docker compose up -d pebble
          for _ in $(seq 1 30); do
            curl -sf --noproxy '*' --cacert "$PEBBLE_MINICA" --resolve pebble:15000:172.19.240.252 \\
              https://pebble:15000/roots/0 -o "$RUNNER_TEMP/pebble-root.pem" && break
            sleep 2
          done
          test -s "$RUNNER_TEMP/pebble-root.pem" || { docker compose logs pebble; echo "::error::pebble never served its root"; exit 1; }
          echo "PEBBLE_ROOT=$RUNNER_TEMP/pebble-root.pem" >> "$GITHUB_ENV"

      - name: first boot -- scripts/first-boot.sh, answered as an operator would
        # deploy/README.md, First boot, 1: steps 1 to 3.6, OpenBao's
        # auto-unseal, steps 4 to 7 and step 8's install code, with FIVE shares
        # and a threshold of three, as on the VPS. Step 1 is the step above's
        # (Pebble starts only with a .env, and step 7 needs its root), so the
        # script must say it skipped it: the second --require. The systemd
        # timers of steps 6b and 12 are written, never enabled here
        # (ITS_NO_SYSTEMCTL=1): unverified, as before 2026-11-27.
        # ITS_FIRST_BOOT_ALLOW_NONTTY=1 lets it print the shares
        # and the root token with no terminal; first_boot_answers.py masks
        # each one before its line is printed, types SAVED and three shares
        # back on the script's stdin, and fails unless the run ends as a good
        # one does. Nothing it read is written anywhere.
        working-directory: deploy
        timeout-minutes: 45
        run: |
          set -euo pipefail
          python3 ci/first_boot_answers.py \\
            --require 'First boot: every step is done.' \\
            --require '  skip    step 1: .env exists (new-env.sh never overwrites one), for rmm.novieri.com' \\
            -- --tls-cafile "$PEBBLE_ROOT"
          # openbao-seal.sh put the auto-unseal overlay into .env, and the
          # COMPOSE_FILE this job exports wins over .env's: every later step
          # loads it too, or an `up` would recreate OpenBao without its seal.
          grep -qx 'COMPOSE_FILE=docker-compose.yml:openbao/auto-unseal.yml' .env
          echo "COMPOSE_FILE=docker-compose.yml:ci/acme-pebble.yml:openbao/auto-unseal.yml" >> "$GITHUB_ENV"

      - name: OpenBao unseals itself after a restart
        # New with design 2026-11-27 (§4, §5): what the operator chose. No
        # share is given: a static-sealed OpenBao that comes back sealed is a
        # first boot that would need a person after every power cut.
        working-directory: deploy
        run: |
          set -euo pipefail
          docker compose restart openbao
          rc=1
          for _ in $(seq 1 45); do
            rc=0; state="$(docker compose exec -T openbao bao status -format=json 2>/dev/null)" || rc=$?
            [ "$rc" = 0 ] && break
            sleep 2
          done
          [ "$rc" = 0 ] || { docker compose logs --tail 50 openbao; echo "::error::OpenBao did not unseal itself after a restart"; exit 1; }
          grep -q '"type": "static"' <<<"$state"
          grep -q '"sealed": false' <<<"$state"
          echo "OpenBao restarted and came back unsealed, with no share given"

      - name: first-boot.sh again -- every step done, nothing changed
        # New with design 2026-11-27 (§3, §5): a run again after a failure is
        # the script's whole recovery story. With no terminal and nothing on
        # stdin it may print no secret, ask nothing, and change nothing: the
        # same .env, token and key files, containers and schema before and
        # after, no `run` line, and every step a `skip`. A container is its id
        # and, while it runs, its start time; a one-shot (migrate,
        # postgres-grants, openbao-init) runs again on every `up -d` under the
        # same id -- measured with Compose 5.7.1 on 2026-11-27 -- so for one
        # its exit code is what must not change.
        working-directory: deploy
        run: |
          set -euo pipefail
          fingerprint() {
            sha256sum .env
            sudo sha256sum /etc/it-suite/gateway-issuer.token /etc/it-suite/openbao-unseal/unseal.key
            docker compose ps -aq | sort | xargs docker inspect -f '{{.Name}} {{.Id}} {{if .State.Running}}{{.State.StartedAt}}{{else}}exit={{.State.ExitCode}}{{end}}'
            docker compose exec -T postgres psql -U its_admin -d itsuite -tAc 'select version_num from alembic_version'
          }
          before="$(fingerprint)"
          ./scripts/first-boot.sh --tls-cafile "$PEBBLE_ROOT" </dev/null | tee "$RUNNER_TEMP/again.log"
          after="$(fingerprint)"
          [ "$before" = "$after" ] || { diff <(echo "$before") <(echo "$after") || true; echo "::error::first-boot.sh run again changed something"; exit 1; }
          if grep -q '^  run ' "$RUNNER_TEMP/again.log"; then
            echo "::error::first-boot.sh run again ran a step"; exit 1
          fi
          [ "$(grep -c '^  skip ' "$RUNNER_TEMP/again.log")" -ge 16 ] || { echo "::error::first-boot.sh run again skipped too few steps"; exit 1; }
          grep -qx 'First boot: every step is done.' "$RUNNER_TEMP/again.log"
          rm -f "$RUNNER_TEMP/again.log"

"""

# 4. The OAuth step: the stack is the scripts'; the agent is first-boot-check.sh's.
OAUTH_COMMENT_OLD = cut("        # OPENBAO WITH ONE SHARE, because CI has nobody", "        # TWO PHASES:")
OAUTH_COMMENT_NEW = """\
        # THE STACK IS THE OPERATOR'S SCRIPTS' since 2026-11-27 (design
        # 2026-11-27-despliegue-automatizado §5): first-boot.sh brought it up,
        # five shares and the auto-unseal included, and first-boot-check.sh
        # runs below with the console's two clicks made through its API --
        # agent_flow.py's token for step 11b, and its `hello` for 11d. Every
        # secret this step reads is masked before anything could print it,
        # reaches its reader on stdin or in a 0600 file, and goes however the
        # step ends (the trap).
        #
"""
OAUTH_RUN_OLD = cut("          set -euo pipefail\n          umask 077\n          init_json=", "          # 4. The api, the console and the edge.")
OAUTH_RUN_NEW = """\
          set -euo pipefail
          umask 077
          trap 'rm -f "$RUNNER_TEMP/setup-code" "$RUNNER_TEMP/ci-owner.json" "$RUNNER_TEMP/enroll-token" "$RUNNER_TEMP/check.log"; docker rm -f smoke-agent ci-agent >/dev/null 2>&1 || true; rm -rf /tmp/smoke-agent "$RUNNER_TEMP/agent-data"' EXIT

          # 1. Nothing of the old identity provider is defined, at all.
          defined="$(docker compose config --services) $(docker compose config --images)"
          if grep -qi keycloak <<<"$defined"; then
            echo "::error::the compose stack still defines an identity provider service or image"; exit 1
          fi

"""
OAUTH_EDGE_OLD = cut("          # 4. The api, the console and the edge.", "          # THE CHECK THAT GOES RED WITHOUT ACME")
OAUTH_EDGE_NEW = """\
          # 4. The api, the console and the edge, as first-boot.sh left them.
          #    Both behind Traefik, which routes only a healthy container: the
          #    api's metadata, and the console's page for `/`. `--noproxy '*'`:
          #    the runner's proxy, if it has one, is not the way to localhost.
          pebble_root="$PEBBLE_ROOT"
          get() { curl -sf --noproxy '*' --cacert "$pebble_root" --resolve rmm.novieri.com:443:127.0.0.1 "$@"; }
          # The api's OWN answer, not just a 200: Traefik leaves out a
          # container whose health check has not passed yet, and meanwhile
          # the console's catch-all router answers the metadata path with
          # its HTML page and a 200 (the first phase-4 run waited on exactly
          # that, and its POST /mcp reached nginx: 405).
          wait_for_api() {
            local meta page
            for _ in $(seq 1 60); do
              meta="$(get -o /dev/null -w '%{content_type}' https://rmm.novieri.com/.well-known/oauth-authorization-server || true)"
              case "$meta" in
                application/json*)
                  page="$(get -o /dev/null -w '%{content_type}' https://rmm.novieri.com/ || true)"
                  case "$page" in text/html*) return 0 ;; esac
                  ;;
              esac
              sleep 3
            done
            docker compose logs api web traefik
            echo "::error::the api and the console never both answered through Traefik"
            return 1
          }
"""
OPT_IN_OLD = """\
          echo "ITS_MCP_REGISTRATION=open" >> .env
"""
OPT_IN_NEW = """\
          sed -i '/^ITS_MCP_REGISTRATION=/d' .env
          echo "ITS_MCP_REGISTRATION=open" >> .env
"""
AGENT_OLD = cut("          # 7c. An endpoint (design 2026-11-23 §4, §5)", "          # 7d. The Celery worker")
AGENT_NEW = """\
          # 7c. An endpoint (design 2026-11-23 §4, §5), by the runbook's own
          #     acceptance test: first-boot-check.sh (deploy/README.md, First
          #     boot, 3). Its agent is the Linux binary built from this
          #     checkout, on the runner's network, dialling rmm.novieri.com at
          #     127.0.0.1 (--connect) and trusting Pebble's root alone
          #     (--tls-cafile): enrolled over :443 with agent_flow.py's
          #     one-use token, which reaches the script on stdin, connected
          #     over :8443 with mutual TLS. Once it says the agent connected,
          #     agent_flow.py presses "Run job" -- a `hello` the server signs
          #     and the agent verifies -- and the script waits for it (11d),
          #     verifies the chain (11e), retires the agent (11f) and takes the
          #     first backup (12). One sign-in mints two one-use tokens: this
          #     agent's, and 7c-bis's.
          agents() { python3 ci/agent_flow.py --base https://rmm.novieri.com --resolve rmm.novieri.com:127.0.0.1 --cafile "$pebble_root" --state-file "$RUNNER_TEMP/ci-owner.json" "$@"; }
          agent_data="$RUNNER_TEMP/agent-data"
          mkdir -m 700 "$agent_data"
          agents --phase token --token-file "$RUNNER_TEMP/enroll-token" --token-file "$agent_data/enroll-token" || { docker compose logs api traefik; exit 1; }
          echo "::add-mask::$(<"$RUNNER_TEMP/enroll-token")"
          echo "::add-mask::$(<"$agent_data/enroll-token")"
          { cat "$RUNNER_TEMP/enroll-token"; echo; } \\
            | ./scripts/first-boot-check.sh --tls-cafile "$pebble_root" --connect 127.0.0.1 --wait-job 180 \\
              > "$RUNNER_TEMP/check.log" 2>&1 &
          check=$!
          for _ in $(seq 1 120); do
            grep -q '^  ok      step 11c:' "$RUNNER_TEMP/check.log" && break
            kill -0 "$check" 2>/dev/null || break
            sleep 2
          done
          agents --phase job --hostname "$(hostname)" || { kill "$check" 2>/dev/null || true; cat "$RUNNER_TEMP/check.log"; docker logs smoke-agent; docker compose logs api gateway traefik; exit 1; }
          wait "$check" || { cat "$RUNNER_TEMP/check.log"; docker compose logs api gateway; exit 1; }
          cat "$RUNNER_TEMP/check.log"
          grep -qx 'First boot is complete.' "$RUNNER_TEMP/check.log"

          # 7c-bis. Invariant 1, the negative half, end to end (coordinator's
          #     ruling Q3, 2026-11-27; until then a rehearsal only, plan
          #     2026-11-23, Task 10). The same binary, enrolled with the second
          #     token on the stack's network -- its own agent, since
          #     first-boot-check.sh retired its one at 11f -- is stopped, made
          #     to pin a job-signing key nobody holds the private half of, and
          #     started again; the server signs a hello as always, and the agent
          #     must refuse it with bad_signature and run nothing: no greeting
          #     comes back, the chain holds jobs.refused and no success, and the
          #     agent's own log says why.
          agent_bin="$(cd ../agent && pwd)/dist/novieri-agent"
          docker run -d --name ci-agent --hostname ci-agent --network it-suite_default \\
            --add-host rmm.novieri.com:172.19.240.250 --user "$(id -u):$(id -g)" \\
            -v "$agent_bin:/usr/local/bin/novieri-agent:ro" \\
            -v "$pebble_root:/etc/ssl/certs/ca-certificates.crt:ro" \\
            -v "$agent_data:/var/lib/novieri-agent" \\
            -e ITS_AGENT_ENROLL_URL=https://rmm.novieri.com -e ITS_AGENT_DATA_DIR=/var/lib/novieri-agent \\
            busybox:1.36.1 /usr/local/bin/novieri-agent
          for _ in $(seq 1 60); do
            docker logs ci-agent 2>&1 | grep -c 'gateway connected' >/dev/null && break
            sleep 2
          done
          test ! -e "$agent_data/enroll-token" || { docker logs ci-agent; echo "::error::the agent kept its enrollment token after enrolling"; exit 1; }
          agents --phase refused --hostname ci-agent --container ci-agent --pins-file "$agent_data/pins.json" || { docker logs ci-agent; docker compose logs api gateway traefik; exit 1; }
          docker logs ci-agent 2>&1 | grep -E 'msg="job refused".*job_type=hello.*reason=bad_signature'

"""
STOP_FIRST = """\
        working-directory: deploy
        run: |
          set -euo pipefail
          ./scripts/backup.sh /tmp/ci-backup
"""
STOP_FIRST_NEW = """\
        #
        # SINCE 2026-11-27 it runs last, on the stack first-boot.sh built and
        # first-boot-check.sh used: the application tier stops first, as
        # deploy/README.md "Rolling back a deployment" stops it -- a restore
        # under a running app is how half-written state happens -- and
        # nothing after this needs it.
        working-directory: deploy
        run: |
          set -euo pipefail
          docker compose stop api gateway worker backup-replicator web traefik
          ./scripts/backup.sh /tmp/ci-backup
"""
TEARDOWN_NEW = """\
      - name: teardown
        if: always()
        working-directory: deploy
        run: |
          docker compose down -v
          # What the scripts wrote as root outside the checkout. This runner is
          # no deployment host -- the `down -v` above assumes as much.
          sudo rm -rf /etc/it-suite/gateway-issuer.token /etc/it-suite/openbao-unseal /etc/it-suite/backup.conf \\
            /var/backups/it-suite /var/lib/it-suite "$RUNNER_TEMP/systemd" /tmp/smoke-agent
"""

for old in (OPT_IN_OLD, STOP_FIRST, OLD_ENV_TAIL, OLD_ENV_COMMENT, CHECKOUT):
    assert text.count(old) == 1, old

new_oauth = oauth
for old, new in (
    (OAUTH_COMMENT_OLD, OAUTH_COMMENT_NEW),
    (OAUTH_RUN_OLD, OAUTH_RUN_NEW),
    (OAUTH_EDGE_OLD, OAUTH_EDGE_NEW),
    (OPT_IN_OLD, OPT_IN_NEW),
    (AGENT_OLD, AGENT_NEW),
):
    assert new_oauth.count(old) == 1, old[:60]
    new_oauth = new_oauth.replace(old, new)
assert round_trip.count(STOP_FIRST) == 1
new_round_trip = round_trip.replace(STOP_FIRST, STOP_FIRST_NEW)

text = text.replace(OLD_COMMENT, NEW_COMMENT)
text = text.replace(CHECKOUT, SHELLCHECK)
text = text.replace(OLD_ENV_TAIL, NEW_ENV_TAIL).replace(OLD_ENV_COMMENT, NEW_ENV_COMMENT)
text = text.replace(first_boot_by_hand, PEBBLE_AND_FIRST_BOOT)
text = text.replace(round_trip + k1, "")
text = text.replace(oauth, new_oauth + new_round_trip + k1)
text = text.replace(teardown, TEARDOWN_NEW)
CI.write_text(text)
print("the deploy job runs first-boot.sh and first-boot-check.sh")
TASK5_CI
python3 "$S/task5_ci.py"
python3 -c "import yaml; j = yaml.safe_load(open('.github/workflows/ci.yml'))['jobs']['deploy']; print(j['name']); [print('-', s.get('name', s.get('uses'))) for s in j['steps']]"
````

Expected:

```
the deploy job runs first-boot.sh and first-boot-check.sh
deploy (compose first boot · app_user privileges)
- actions/checkout@v4
- shellcheck the first-boot scripts
- Throwaway .env, written by the script a new host uses
- Pebble, and the root it signs this run's certificate with
- first boot -- scripts/first-boot.sh, answered as an operator would
- OpenBao unseals itself after a restart
- first-boot.sh again -- every step done, nothing changed
- grant-app-privileges.sql (twice — must be idempotent)
- assert app_user is RLS-constrained and correctly narrowed
- assert every tenant table is RLS-protected
- IT_SUITE_VERSION selects the image reference (interpolation only)
- actions/setup-go@v5
- the Linux agent, built from this checkout
- the platform's own OAuth server and MCP door, live, through Traefik
- backup.sh -> restore.sh round trip, invoked AS DOCUMENTED
- the old identity provider's database -- kept by the pre-upgrade backup only, and restored
- teardown
```

`git diff --stat .github/workflows/ci.yml` da `1 file changed, 309 insertions(+), 226 deletions(-)`, todo dentro del trabajo `deploy` (`git diff -U0 .github/workflows/ci.yml | grep '^@@'` no muestra ningún trozo por encima de la línea 369 ni por debajo de la que empieza `  frontend:`).

- [ ] **Step 4: Las pruebas pasan**

Run, desde `backend/`: `ITS_TEST_REDIS_URL=redis://localhost:6379/5 .venv/bin/python -m pytest -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning -p no:cacheprovider -q tests/test_ci_first_boot.py tests/test_ci_mcp_flow.py tests/test_ci_agent_flow.py` y `.venv/bin/ruff check app tests`. Desde la raíz, el paso nuevo de la CI: `shellcheck deploy/scripts/prepare-host.sh deploy/scripts/first-boot.sh deploy/scripts/first-boot-check.sh && echo SHELLCHECK-CLEAN`.

Expected: `95 passed` (15, y 80 de los dos archivos de la CI); `All checks passed!`; `SHELLCHECK-CLEAN`.

- [ ] **Step 5: El diseño, §5 y §8.2** (resolución Q3)

Con este guion, desde la raíz (no se guarda en el repositorio):

```bash
S=/tmp/claude-0/-home-user-Novieri-RMM/21768af2-984e-5cf8-87b2-4dc8a41a4864/scratchpad
cat > "$S/task5_spec.py" <<'TASK5_SPEC'
"""Plan 2026-11-27, Task 5: the design's §5 says the bad-signature check is
added, not kept (coordinator's ruling Q3, 2026-11-27), and §8.2 counts it.
Each anchor once; nothing is written unless both are there."""

from pathlib import Path

SPEC = Path("docs/superpowers/specs/2026-11-27-despliegue-automatizado-design.md")
EDITS = (
    (
        """\
  por el 443 y conectado por el 8443 con un `hello` firmado, rojo con la firma
  mala, cadena de auditoría, privilegios de `app_user`, copia y restauración) y
  añade: OpenBao se desella solo tras `docker compose restart openbao`; y
  `first-boot.sh` lanzado por segunda vez sale bien sin cambiar nada.
""",
        """\
  por el 443 y conectado por el 8443 con un `hello` firmado, cadena de
  auditoría, privilegios de `app_user`, copia y restauración) y añade: OpenBao
  se desella solo tras `docker compose restart openbao`; `first-boot.sh`
  lanzado por segunda vez sale bien sin cambiar nada; y el rojo con la firma
  mala, hasta hoy un ensayo: el agente compilado, con una clave de firma fijada
  que el servidor no tiene, rechaza un `hello` firmado con `bad_signature` y no
  lo ejecuta (decisión del coordinador del 2026-11-27, pregunta Q3 del plan).
""",
    ),
    (
        """\
   comprobaciones de hoy más las dos nuevas.
""",
        """\
   comprobaciones de hoy más las tres nuevas.
""",
    ),
)
text = SPEC.read_text()
for old, _ in EDITS:
    assert text.count(old) == 1, old[:60]
for old, new in EDITS:
    text = text.replace(old, new)
SPEC.write_text(text)
print("design §5 and §8.2: the bad signature is added, a third new check")
TASK5_SPEC
python3 "$S/task5_spec.py"
grep -c 'rojo con la firma\|tres nuevas' docs/superpowers/specs/2026-11-27-despliegue-automatizado-design.md
```

Expected: `design §5 and §8.2: the bad signature is added, a third new check` y `2`.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/ci.yml deploy/ci/first_boot_answers.py deploy/ci/agent_flow.py deploy/ci/mcp_flow.py backend/tests/test_ci_first_boot.py backend/tests/test_ci_mcp_flow.py backend/tests/test_ci_agent_flow.py docs/superpowers/specs/2026-11-27-despliegue-automatizado-design.md
git commit -m "ci(deploy): the deploy job's first boot is first-boot.sh and first-boot-check.sh

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013TnTdGAz4rSe1YRXXjEqyr"
```

- [ ] **Step 7: El trabajo, en rojo y en verde, aquí**

En una rama de ensayo que no se fusiona: `git switch -c t5-rehearsal`. Docker, con el espejo de Docker Hub y las imágenes base de la sesión (Global Constraints, «Docker»):

```bash
mkdir -p /etc/docker && printf '{"registry-mirrors": ["https://mirror.gcr.io"]}\n' > /etc/docker/daemon.json
pkill dockerd; sleep 3; (nohup dockerd >/tmp/dockerd.log 2>&1 &)
until docker version --format '{{.Server.Version}}' >/dev/null 2>&1; do sleep 1; done; docker version --format '{{.Server.Version}}'
S=/tmp/claude-0/-home-user-Novieri-RMM/21768af2-984e-5cf8-87b2-4dc8a41a4864/scratchpad
mkdir -p "$S/shadow" && cp /root/.ccr/ca-bundle.crt "$S/shadow/session-ca.crt"
for base in python:3.12-slim node:22-slim golang:1.24-bookworm; do
  docker pull -q "$base" >/dev/null
  printf 'FROM %s\nCOPY session-ca.crt /usr/local/share/session-ca.crt\nENV SSL_CERT_FILE=/usr/local/share/session-ca.crt PIP_CERT=/usr/local/share/session-ca.crt REQUESTS_CA_BUNDLE=/usr/local/share/session-ca.crt NODE_EXTRA_CA_CERTS=/usr/local/share/session-ca.crt npm_config_cafile=/usr/local/share/session-ca.crt GIT_SSL_CAINFO=/usr/local/share/session-ca.crt CURL_CA_BUNDLE=/usr/local/share/session-ca.crt\n' "$base" > "$S/shadow/Dockerfile"
  docker build -q -t "$base" "$S/shadow"
done
```

Expected: `29.9.3` y tres identificadores `sha256:…`. (El orden de cada `cd deploy && … down -v … && rm -f .env` de abajo importa: sin `.env`, compose no lee el archivo y `down -v` no borra nada; un `teardown` que corrió ya lo hizo, y esto es por si no corrió.) Un `429 Too Many Requests` de Docker Hub a pesar del espejo no es un fallo del plan: se espera y se repite el paso, sin tocar nada.

El guion que corre un trabajo de `ci.yml` paso a paso (el del plan del 2026-11-23, con el proxy de la sesión rodeado para el stack; no se guarda en el repositorio):

```bash
cat > "$S/run_ci_job.py" <<'RUN_CI_JOB'
"""Run one job of .github/workflows/ci.yml here, step by step, as the runner
would: each `run:` under bash -eo pipefail in its working-directory, with
RUNNER_TEMP and GITHUB_ENV, and what a step appends to GITHUB_ENV exported to
the steps after it. `uses:` steps are skipped (the checkout is this tree; Go is
this session's). Stops at the first step that fails; `if: always()` steps run
anyway. The session's HTTPS proxy is no way to the stack: rmm.novieri.com,
pebble and the loopback go around it.

    python3 run_ci_job.py . deploy                       # from the repository root
    python3 run_ci_job.py . deploy --from "first boot"  # from the step whose name starts so

Not committed: plan 2026-11-23's runner (Task 7, Step 6), as plan
2026-11-27, Task 5 runs it."""
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
around = "rmm.novieri.com,pebble,127.0.0.1,localhost,172.19.240.0/24"
env = {**os.environ, "RUNNER_TEMP": str(temp), "GITHUB_ENV": str(temp / "github_env"),
       "NO_PROXY": around, "no_proxy": around}
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
RUN_CI_JOB
```

**Rojo 1, la comprobación nueva del reinicio:** un archivo de clave que no es el de OpenBao. El reinicio tiene que dejarlo sellado y el paso, en rojo:

```bash
python3 - <<'PLANT'
from pathlib import Path
ci = Path(".github/workflows/ci.yml"); text = ci.read_text()
anchor = "          docker compose restart openbao\n          rc=1\n"
assert text.count(anchor) == 1
ci.write_text(text.replace(anchor, "          sudo sh -c 'head -c 32 /dev/urandom > /etc/it-suite/openbao-unseal/unseal.key'\n" + anchor))
print("planted: a key file that is not OpenBao's")
PLANT
cd deploy && { docker compose down -v 2>/dev/null || true; } && rm -f .env; cd ..
backend/.venv/bin/python "$S/run_ci_job.py" . deploy 2>&1 | tee "$S/t5-red-restart.log" | grep -E '^=====|::error::|First boot: every step is done'; git checkout -- .github/workflows/ci.yml
```

Expected: `exit 0` en cada paso hasta `first boot -- scripts/first-boot.sh, answered as an operator would` (con `First boot: every step is done.` en su salida); después `::error::OpenBao did not unseal itself after a restart`, `===== OpenBao unseals itself after a restart: exit 1` y `===== teardown: exit 0`.

**Rojo 2, la comprobación nueva de la segunda ejecución:** un `.env` al que le falta `IT_SUITE_VERSION` antes de la segunda ejecución. El paso 1b vuelve a escribirla, imprime `run`, y la huella cambia:

```bash
python3 - <<'PLANT'
from pathlib import Path
ci = Path(".github/workflows/ci.yml"); text = ci.read_text()
anchor = '          before="$(fingerprint)"\n'
assert text.count(anchor) == 1
ci.write_text(text.replace(anchor, "          sed -i '/^IT_SUITE_VERSION=/d' .env\n" + anchor))
print("planted: .env without IT_SUITE_VERSION before the second run")
PLANT
cd deploy && { docker compose down -v 2>/dev/null || true; } && rm -f .env; cd ..
backend/.venv/bin/python "$S/run_ci_job.py" . deploy 2>&1 | tee "$S/t5-red-again.log" | grep -E '^=====|::error::|^  run '; git checkout -- .github/workflows/ci.yml
```

Expected: hasta `===== OpenBao unseals itself after a restart: exit 0`, cada paso `exit 0`; en el siguiente, `  run     ./scripts/build-images.sh, then IT_SUITE_VERSION=… in .env`, `::error::first-boot.sh run again changed something` y `===== first-boot.sh again -- every step done, nothing changed: exit 1`; `teardown` corre igual (`exit 0`).

**Rojo 3, la firma: el caso negativo nuevo** (resolución Q3; el falsador de la invariante 1 sobre el binario): un agente que no verifica la firma. Se planta en su código, solo en esta rama de ensayo, `if false && ` delante de la comprobación de `agent/internal/jobs/verify.go:180` (M10); el paso de la CI que compila el agente lo compila así. El `hello` bueno del 7c sigue saliendo bien (el servidor lo firmó), y el 7c-bis tiene que ponerse en rojo, porque el agente ejecuta el `hello` que no debía:

```bash
python3 - <<'PLANT'
from pathlib import Path
verify = Path("agent/internal/jobs/verify.go"); text = verify.read_text()
anchor = "\tif !ed25519.Verify(v.key, msg, sig) {\n"
assert text.count(anchor) == 1
verify.write_text(text.replace(anchor, "\tif false && !ed25519.Verify(v.key, msg, sig) {\n"))
print("planted: an agent that never checks a job's signature")
PLANT
(cd agent && go test -run '^TestSignatureIsCheckedBeforeAnythingElseIsBelieved$' ./internal/jobs/ 2>&1 | head -1)
cd deploy && { docker compose down -v 2>/dev/null || true; } && rm -f .env; cd ..
backend/.venv/bin/python "$S/run_ci_job.py" . deploy 2>&1 | tee "$S/t5-red-signature.log" | grep -E '^=====|^ok    (the device|the agent pins|hello)|^FAIL|agent_flow: phase'; git checkout -- agent/internal/jobs/verify.go
```

Expected: `planted: an agent that never checks a job's signature`; `--- FAIL: TestSignatureIsCheckedBeforeAnythingElseIsBelieved (0.00s)` (la suite del propio agente lo ve también, en su trabajo de la CI, y `TestEveryRefusalIsMadeByItsOwnCheck` con ella; este rojo es el del trabajo `deploy`, que no depende de esa suite; medido el 2026-11-27 con Go 1.24.7: el binario plantado compila con `CGO_ENABLED=0 go build -trimpath`, la orden del paso de la CI); cada paso hasta el del OAuth sale `exit 0`, y en él `agent_flow: phase token: every assertion held`, `agent_flow: phase job: every assertion held`, `ok    the device ci-agent enrolled and is online`, `ok    the agent pins a job-signing key the server does not hold, and is connected again`, `FAIL  the hello it does not trust ended succeeded`, `===== the platform's own OAuth server and MCP door, live, through Traefik: exit 1` y `===== teardown: exit 0`. Y `git status --short agent` no imprime nada.

**Verde:**

```bash
cd deploy && { docker compose down -v 2>/dev/null || true; } && rm -f .env; cd ..
backend/.venv/bin/python "$S/run_ci_job.py" . deploy 2>&1 | tee "$S/t5-green.log" | grep -E '^=====|  skip    step 1:|presents a trusted|First boot: every step is done|First boot is complete|OpenBao restarted and came back|answers 503|^ok    (signed in|the site|an enrollment|the device|the agent pins|hello|the audit)|agent_flow: phase|mcp_flow: phase|  ok      step 1[01]|msg="job refused"'
grep -c '::add-mask::' "$S/t5-green.log"
```

Expected: cada paso `exit 0`; en el primer arranque, **`  skip    step 1: .env exists (new-env.sh never overwrites one), for rmm.novieri.com`** (resolución Q1: el paso 1 es el de «Throwaway .env»), `  ok      OpenBao restarted and came back unsealed by itself (Seal Type static, Sealed false): no share was typed` (justo después del 3.6), `ok      127.0.0.1:443 presents a trusted certificate for rmm.novieri.com` y `First boot: every step is done.`; `OpenBao restarted and came back unsealed, with no share given`; `mcp_flow: phase closed: every assertion held`, `mcp_flow: phase open: every assertion held`, `with the api stopped, /api/v1/setup/status answers 503`; `agent_flow: phase token: every assertion held`, las líneas `  ok      step 10`, `step 11a`, `step 11c`, `step 11d`, `step 11e`, `step 11f`, `agent_flow: phase job: every assertion held` y `First boot is complete.`; y el 7c-bis: `ok    the device ci-agent enrolled and is online`, `ok    the agent pins a job-signing key the server does not hold, and is connected again`, `ok    hello: server-signed, refused by the agent with bad_signature, never run`, `ok    the audit chain verifies over … events, the refusal included`, `agent_flow: phase refused: every assertion held` y la línea del agente, `… level=WARN msg="job refused" job_id=… job_type=hello reason=bad_signature`. El recuento de `::add-mask::` es al menos 10 (cinco partes, el token raíz, el código dos veces —el del guion y el que lee la CI— y los dos tokens de inscripción). **Los temporizadores del 6b y del 12 no se activan** (`ITS_NO_SYSTEMCTL=1`, resolución Q2): este verde no dice nada de ellos. **Este ensayo no se corrió al escribir el plan**: las líneas esperadas son las que los guiones imprimen, medidas contra el arnés. Si un paso falla por algo que el plan no previó, se corrige el guion o el paso (nunca la comprobación) **en la rama de la tarea**, no en la de ensayo, con su commit y su prueba, y el informe de la tarea dice qué fue. El informe copia las líneas `=====` y las de `ok` de los cuatro registros.

Al terminar: `cd deploy && docker compose down -v; cd .. && git switch - && git branch -D t5-rehearsal`.

---

### Task 6: El runbook, GO-LIVE y `docs/07` dicen lo que hacen los guiones (oleada 3, en paralelo con la Task 5; se fusiona después de ella)

**Files:**
- Modify: `deploy/README.md` («Status of this runbook»; «Prerequisites»: P1, P6 delante, «P2 to P5 — `prepare-host.sh`»; «First boot», reescrito; «After a reboot or a restart», reescrito; una frase de «Upgrading an existing deployment»; el apéndice nuevo al final)
- Modify: `deploy/GO-LIVE.md` (§1, una frase; §4, reescrito hasta «The host must not swap»; §6, un punto)
- Modify: `docs/07-backup-restore.md` (la fila 5 de §1, §2.3, el paso 5 de §3, §5 «What to back up»)
- Modify: `backend/tests/test_runbook_first_boot.py` (el docstring, tres ayudas, cuatro pruebas reescritas, dos nuevas), `backend/tests/test_deploy_runbook_secrets.py` (los tres guiones en `RUNBOOKS`)
- Modify: `docs/superpowers/specs/2026-11-27-despliegue-automatizado-design.md` (§6, un punto: resolución Q7)

**Interfaces:**
- Consumes: de las Tasks 1 a 4, las líneas que el runbook cita (Interfaces de cada una): `Docker's versions`, `The host is ready for the first boot` y sus dos `note` (Task 1); `OpenBao restarts and unseals itself`, `  run     docker compose restart openbao` y la línea `ok` del reinicio (Task 2); `Step 4 — migrations`, `Step 8 — the install code`, la `note` del navegador, `First boot: every step is done.` (Task 3); `Step 12 — the first backup, so that there is a way back`, sus dos `note` y `First boot is complete.` (Task 4). Del código: los `ufw allow` de `prepare-host.sh`, las órdenes de los pasos 1 a 7 en `first-boot.sh` y su bloque `main` (`step_3_6`, `auto_unseal`, `restart_proof`, `step_4`, en ese orden), `sudo systemctl daemon-reload` antes de `enable`, `sudo ./scripts/install-backup-timer.sh </dev/null` en `first-boot-check.sh`.
- Produces:
  - Las anclas nuevas, que los guiones y el informe citan por su nombre: `#first-boot`, `#prepare-hostsh-p2-to-p5`, `#appendix-what-each-script-does-by-hand`, `#if-bao-status-says-sealed-true`. Las de los pasos no cambian (los encabezados conservan su texto: `#step-6b--renew-the-gateways-certificate-every-month`, que cita GO-LIVE §5, sigue valiendo).
  - La estructura que lee el informe (Task 7): en «Prerequisites», `### The machine, and its name`, `### P1 — …`, `### P6 — The checkout`, `### P2 to P5 — \`prepare-host.sh\``; en «First boot», `### 1. \`first-boot.sh\``, `### 2. In the browser: …`, `### 3. \`first-boot-check.sh\``, `### 4. Custody: …`, un bloque `sh` en la 1 y en la 3 y ninguno en la 2 ni en la 4; «After a reboot or a restart» con cuatro bloques `sh`: `bao status` solo, el de `up -d`, el de `openbao-seal.sh status`, el de devolver el archivo de clave.

**Decisiones de la tarea.**
- **Los bloques no se reescriben: se mueven** al apéndice, con un nivel menos de encabezado, en el orden de siempre, partidos en cuatro partes (`prepare-host.sh`, `first-boot.sh`, el navegador, `first-boot-check.sh`). Donde el guion hace algo distinto, el apéndice lo dice junto al bloque (diseño §3, «donde el guion cambia algo, el runbook lo dice»): 3.3 (las partes las pide el guion), 3.4 y 3.5 (el token raíz, de la memoria), 7 (la espera hasta que nada se reinicia), 8 (el código lo genera el guion), 11d (la consulta por dispositivo). Y en tres bloques el apéndice **cambia la orden** porque la del guion es mejor también a mano: el 2 (`up -d --wait postgres`), el 5 (`set -C`: no sobrescribir la clave HMAC) y el 6b (`daemon-reload` antes de `enable`); el 12 lleva `sudo journalctl`. El texto del *here-document* de las unidades del 6b no cambia en un carácter (la prueba de la Task 3 lo compara con lo que escribe el guion).
- **P6 va antes de P2 a P5**: `prepare-host.sh` está en la copia que hace P6. La numeración P1 a P6 se queda (el diseño la usa).
- **«After a reboot or a restart» ya no desella con partes**: `bao status` tiene que decir `Sealed false`; con `Sealed true`, el archivo de clave falta o no es el suyo, y la sección dice cómo devolverlo desde su custodia, sin marcadores (`~/unseal.key`, copiado con `scp`). Las partes solo se nombran, en prosa, para un host que siga en Shamir.
- **GO-LIVE §4** pasa a decir que el desellado automático es lo de cada primer arranque desde el 2026-11-27, con la fecha y la decisión del operador, que las cinco partes son claves de recuperación, y la custodia del archivo de clave (diseño §4, §6). **`docs/07-backup-restore.md`** se cambia también, aunque el diseño §6 no lo nombra: su paso 5 de §3 desellaba con partes, y tras este proyecto eso ya no funciona en ningún host nuevo (criterio §8.5: «dicen lo mismo que los guiones»; resolución Q7: de acuerdo, y el diseño §6 lo nombra desde el Step 5).
- **La prueba que ata el runbook a los guiones** (`test_the_lines_the_runbook_quotes_are_lines_the_scripts_print`): cada línea `ok`, `run`, `note` o cabecera que «First boot» y «Prerequisites» citan en un bloque `text` está, con `$host`, `$operator` o `$deploy_dir` en lugar de sus valores, en el código de uno de los tres guiones. Una línea con `…` no se compara.
- **El desellado, en el apéndice, entre el paso 3.6 y el 4** (resolución Q6), como lo corre el guion: la parte del apéndice se llama `### \`first-boot.sh\`: steps 1 to 7, with the auto-unseal after 3.6`, y su bloque «The auto-unseal, and the restart that proves it» va justo antes de `#### Step 4 — Migrations, then table privileges`. «First boot, 1» dice el orden, que un fallo desde el paso 4 se relanza sin partes, y enseña la prueba del reinicio seguida de `Step 4 — migrations`. `test_the_first_boot_runs_the_scripts_that_check_it` ata ese orden en el apéndice (`up openbao-init`, `openbao-seal.sh enable`, `restart openbao`, `up migrate`) y en el bloque `main` del guion; el Step 4 lo enseña en rojo con el bloque movido tras el paso 7, donde estaba antes de la resolución.
- **P1 sigue a mano** (resolución Q5): la nota de P1 dice que `prepare-host.sh` no crea ninguna cuenta y que, lanzado como `root` solo, se niega y manda aquí.
- **`first-boot-check.sh` se puede relanzar** (resolución Q8): «First boot, 3» lo dice en negrita, con lo que dicen sus dos primeras líneas (inscribe un agente de prueba en este VPS y lo retira), y qué pasa al relanzarlo tras un fallo y tras terminar bien.
- **`test_deploy_runbook_secrets.py` lee también los tres guiones**, enteros (comentarios incluidos), como ya leía `issue-gateway-cert.py`: ninguno asigna un token en una línea de órdenes. Pasa desde el principio (los guiones ya cumplen); el Step 2 lo enseña en rojo con una línea plantada.

- [ ] **Step 0: La worktree, con las Tasks 1 a 4**

Desde la raíz de la worktree (`p5-t3` lleva ya la `p5-t2`):

```bash
git merge --no-edit p5-t1 p5-t3 p5-t4
git log --oneline -6
for s in prepare-host.sh first-boot.sh first-boot-check.sh; do test -x deploy/scripts/$s && echo "$s ok"; done
grep -c '^## First boot$' deploy/README.md && grep -c '^### Step 3 — OpenBao: initialise, unseal, provision$' deploy/README.md
```

Expected: las fusiones; los tres `ok`; `1` y `1`. Si no, se para y se avisa al coordinador.

- [ ] **Step 1: Las pruebas**

Con este guion, desde la raíz (no se guarda):

````bash
S=/tmp/claude-0/-home-user-Novieri-RMM/21768af2-984e-5cf8-87b2-4dc8a41a4864/scratchpad
cat > "$S/task6_tests.py" <<'TASK6_TESTS'
"""Plan 2026-11-27, Task 6: the tests that pin deploy/README.md's first boot
read it where it is now -- the three scripts in First boot, their blocks in
the appendix -- and tie the two together. No assertion is dropped: each one
now holds where its command lives. Each anchor once."""

from pathlib import Path

FIRST = Path("backend/tests/test_runbook_first_boot.py")
SECRETS = Path("backend/tests/test_deploy_runbook_secrets.py")


def replace(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert text.count(old) == 1, (path, old[:70])
    path.write_text(text.replace(old, new))


def swap(path: Path, start: str, end: str, new: str) -> None:
    text = path.read_text()
    assert text.count(start) == 1, (path, start)
    i = text.index(start)
    j = text.index(end, i)
    path.write_text(text[:i] + new + text[j:])


replace(FIRST, '''\
**WHAT THEY DO NOT CHECK.** That a command works: the rehearsal does (plan
2026-11-23, Task 12), and so does CI's `deploy` job for the commands it runs.
These read the text an operator reads.
"""''', '''\
**SINCE 2026-11-27** (design 2026-11-27-despliegue-automatizado §6) the first
boot is three scripts and the moments between them, and the blocks the scripts
run are the appendix, "What each script does, by hand". What the runbook said
of a command, it still says, in the appendix; and the scripts are held to it:
the commands a step's block runs are the commands its script runs, the lines
the runbook quotes are lines the scripts print.

**WHAT THEY DO NOT CHECK.** That a command works: the rehearsal does (plan
2026-11-23, Task 12), and so does CI's `deploy` job for the commands it runs.
These read the text an operator reads.
"""''')

replace(FIRST, '''\
def _first_boot() -> str:
    return _section(README.read_text(), "## First boot\\n")
''', '''\
SCRIPTS = REPO / "deploy" / "scripts"
APPENDIX = "## Appendix: what each script does, by hand\\n"


def _first_boot() -> str:
    return _section(README.read_text(), "## First boot\\n")


def _appendix() -> str:
    return _section(README.read_text(), APPENDIX)


def _part(heading: str) -> str:
    """One script's part of the appendix: from its ### heading to the next."""
    text = _appendix()
    start = text.index(heading)
    end = text.find("\\n### ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def _code(script: str) -> str:
    """A deploy script without its comment lines."""
    lines = (SCRIPTS / script).read_text().splitlines()
    return "\\n".join(line for line in lines if not line.lstrip().startswith("#"))
''')

replace(FIRST, '''\
        *_sh_blocks(_section(README.read_text(), PREREQUISITES)),
        *_sh_blocks(_first_boot()),
''', '''\
        *_sh_blocks(_section(README.read_text(), PREREQUISITES)),
        *_sh_blocks(_first_boot()),
        *_sh_blocks(_appendix()),
''')

swap(FIRST, "def test_after_a_reboot_unseals_starts_and_checks_the_certificate", "def test_the_prerequisites_open_only_ssh_the_console_and_the_gateway", '''\
def test_after_a_reboot_openbao_unseals_itself_and_the_key_file_is_the_way_back() -> None:
    """Design 2026-11-27 §4: `bao status` must say `Sealed false` alone; with
    `Sealed true` the key file is missing or wrong, and the shares -- recovery
    keys now -- cannot unseal. Only a host still on Shamir is told to use them,
    in prose, never in a block pasted after a reboot."""
    commands = [c for block in _sh_blocks(_after_reboot()) for c in _commands(block)]
    assert "docker compose exec openbao bao operator unseal" not in commands
    assert "docker compose up -d" in commands
    assert "./scripts/check-public-tls.sh" in commands
    assert "sudo ./scripts/openbao-seal.sh status" in commands
    restore = "sudo install -D -m 400 ~/unseal.key /etc/it-suite/openbao-unseal/unseal.key"
    assert any(c.startswith(restore) for c in commands)
    assert commands.index("docker compose restart openbao") > commands.index(
        "sudo ./scripts/openbao-seal.sh status"
    )
    assert "### If `bao status` says `Sealed true`" in _after_reboot()


def test_the_first_boot_is_three_scripts() -> None:
    """Design §1 and §8, criterion 1: `prepare-host.sh` in the prerequisites,
    then `first-boot.sh` and `first-boot-check.sh`, each alone in its block,
    and nothing else to paste in First boot itself."""
    blocks = _sh_blocks(_section(README.read_text(), PREREQUISITES))
    prereq = [c for b in blocks for c in _commands(b)]
    assert "sudo ./scripts/prepare-host.sh" in prereq
    assert [_commands(b) for b in _sh_blocks(_first_boot())] == [
        ["./scripts/first-boot.sh"],
        ["./scripts/first-boot-check.sh"],
    ]
    for script in ("prepare-host.sh", "first-boot.sh", "first-boot-check.sh"):
        assert (SCRIPTS / script).stat().st_mode & 0o111, f"{script} is not executable"


def test_the_first_boot_runs_the_scripts_that_check_it() -> None:
    """The commands of steps 1 to 7, in the appendix's first-boot.sh part, in
    their order, with the auto-unseal between step 3.6 and step 4; and
    first-boot.sh runs each of them, in that order."""
    part = _part("### `first-boot.sh`: steps 1 to 7, with the auto-unseal after 3.6\\n")
    commands = [c for block in _sh_blocks(part) for c in _commands(block)]
    for expected in (
        "./scripts/new-env.sh --host rmm.novieri.com",
        "./scripts/build-images.sh",
        "./scripts/preflight.sh",
        "docker compose up -d",
        "./scripts/check-public-tls.sh",
        "docker compose exec traefik stat -c %a /etc/traefik/acme/acme.json",
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
    code = _code("first-boot.sh")
    assert tag[0] in code, "first-boot.sh writes the tag as step 1b does"
    in_script = [
        code.index(c)
        for c in (
            './scripts/new-env.sh "${args[@]}"',
            "./scripts/build-images.sh </dev/null",
            "./scripts/preflight.sh </dev/null",
            "  dc up -d\\n",
            './scripts/check-public-tls.sh "${tls_args[@]}" </dev/null',
            "dc exec -T traefik stat -c %a /etc/traefik/acme/acme.json",
        )
    ]
    assert in_script == sorted(in_script), in_script
    # The auto-unseal right after step 3.6, before step 4 starts anything
    # (coordinator's ruling Q6, 2026-11-27): a failure from step 4 on is run
    # again with no share. In the appendix, and in the script's own order.
    seal = (
        "sudo env -u COMPOSE_FILE ./scripts/openbao-seal.sh enable "
        "--key-file /etc/it-suite/openbao-unseal/unseal.key"
    )
    around = [
        commands.index(c)
        for c in ("docker compose up openbao-init", seal, "docker compose restart openbao",
                  "docker compose up migrate")
    ]  # fmt: skip
    assert around == sorted(around), around
    calls = [line.strip() for line in code.splitlines()]
    main = calls[calls.index("check_state_before_anything") :]
    assert main.index("step_3_6") < main.index("auto_unseal") < main.index("restart_proof")
    assert main.index("restart_proof") < main.index("step_4")
    check = [c for b in _sh_blocks(_part("### `first-boot-check.sh`: steps 10 to 12\\n"))
             for c in _commands(b)]  # fmt: skip
    assert "sudo ./scripts/install-backup-timer.sh" in check
    assert "sudo ./scripts/install-backup-timer.sh </dev/null" in _code("first-boot-check.sh")


def test_the_first_boot_installs_the_gateway_certificates_renewal() -> None:
    """The gateway's certificate is 365 days, does not renew itself, and is
    the only server certificate every agent dials: without the timer, the
    whole fleet disconnects at once a year after the first boot. It goes in
    before step 7, from the 0600 token step 3.4 wrote; and first-boot.sh
    reloads systemd before enabling it, as the block does."""
    commands = [c for block in _sh_blocks(_appendix()) for c in _commands(block)]
    enable = "sudo systemctl enable --now its-gateway-cert.timer"
    assert enable in commands
    assert commands.index("sudo systemctl daemon-reload") < commands.index(enable)
    assert commands.index(enable) < commands.index("docker compose up -d")
    assert "sudo systemctl list-timers its-gateway-cert.timer --no-pager" in commands
    unit = next(c for c in commands if c.startswith("ExecStart="))
    assert "issue-gateway-cert.py rmm.novieri.com" in unit
    assert "< /etc/it-suite/gateway-issuer.token" in unit
    code = _code("first-boot.sh")
    assert code.index("sudo systemctl daemon-reload") < code.index(enable)


def test_the_lines_the_runbook_quotes_are_lines_the_scripts_print() -> None:
    """First boot shows how a good run of each script ends. A quoted `ok`,
    `run` or `note` line, or a heading, is one its script prints (with the
    host where the script has `$host`); `…` stands for what differs per run."""
    sources = {
        "first-boot.sh": (SCRIPTS / "first-boot.sh").read_text(),
        "first-boot-check.sh": (SCRIPTS / "first-boot-check.sh").read_text(),
        "prepare-host.sh": (SCRIPTS / "prepare-host.sh").read_text(),
    }
    quoted = re.findall(r"```text\\n(.*?)```", _first_boot(), re.DOTALL)
    prereq = _section(README.read_text(), PREREQUISITES)
    quoted += re.findall(r"```text\\n(.*?)```", prereq, re.DOTALL)
    checked = 0
    for block in quoted:
        for line in block.splitlines():
            message = re.sub(r"^  (ok|run|note|skip) {2,}", "", line).strip()
            if not message or "…" in message:
                continue
            message = message.replace("rmm.novieri.com", "$host").replace("rmmadmin", "$operator")
            message = message.replace("/opt/it-suite/deploy", "$deploy_dir")
            assert any(message in source for source in sources.values()), line
            checked += 1
    assert checked >= 10, checked


''')

swap(FIRST, "def test_the_prerequisites_open_only_ssh_the_console_and_the_gateway", "def test_the_off_host_copy_is_offered_and_asks_for_what_only_the_operator_has", '''\
def test_the_prerequisites_open_only_ssh_the_console_and_the_gateway() -> None:
    """P2 to P5 are prepare-host.sh's since 2026-11-27, and their blocks the
    appendix's: the firewall opens 22, 443 and 8443 and nothing else, in both;
    Docker comes from Docker's repository, never Ubuntu's docker.io; and
    GitHub's host key is pinned before the first ssh to it (P6)."""
    prereq = _section(README.read_text(), PREREQUISITES)
    part = _part("### `prepare-host.sh`: P2 to P5\\n")
    commands = [c for block in _sh_blocks(part) for c in _commands(block)]
    allowed = sorted(c for c in commands if c.startswith("sudo ufw allow"))
    assert allowed == ["sudo ufw allow 22/tcp", "sudo ufw allow 443/tcp", "sudo ufw allow 8443/tcp"]
    assert "sudo ufw --force enable" in commands
    assert any("download.docker.com/linux/ubuntu" in c for c in commands)
    assert not any(re.search(r"apt(-get)? install\\b.*\\bdocker\\.io\\b", c) for c in commands)
    assert any("unattended-upgrades" in c for c in commands)
    assert any(c.startswith("timedatectl status") for c in commands)
    # Docker's own first step: remove what conflicts with its packages.
    assert any(
        c.startswith("sudo apt-get remove -y $(dpkg --get-selections docker.io") for c in commands
    )
    code = _code("prepare-host.sh")
    opened = [line.strip() for line in code.splitlines() if line.strip().startswith("ufw allow")]
    assert sorted(opened) == [
        "ufw allow 22/tcp", "ufw allow 443/tcp", "ufw allow 8443/tcp"
    ]  # fmt: skip
    assert "apt-get remove -y $(dpkg --get-selections docker.io" in code
    assert "docker.io" not in code.replace("dpkg --get-selections docker.io", "")
    # GitHub's published Ed25519 host key, before the first ssh to it.
    p6 = [c for block in _sh_blocks(prereq) for c in _commands(block)]
    assert any(GITHUB_ED25519 in c for c in p6)
    assert prereq.index("### P6 — The checkout") < prereq.index("### P2 to P5 — `prepare-host.sh`")


''')

replace(SECRETS, '''\
RUNBOOKS = (
    REPO / "deploy" / "README.md",
    REPO / "deploy" / "GO-LIVE.md",
    REPO / "deploy" / "scripts" / "issue-gateway-cert.py",
)''', '''\
RUNBOOKS = (
    REPO / "deploy" / "README.md",
    REPO / "deploy" / "GO-LIVE.md",
    REPO / "deploy" / "scripts" / "issue-gateway-cert.py",
    # Since 2026-11-27 the first boot's commands are these scripts' too
    # (design 2026-11-27-despliegue-automatizado §3: no secret on a command line).
    REPO / "deploy" / "scripts" / "prepare-host.sh",
    REPO / "deploy" / "scripts" / "first-boot.sh",
    REPO / "deploy" / "scripts" / "first-boot-check.sh",
)''')
replace(SECRETS, '''\
EXPECTED_MARKED_LINES = {"README.md": 0, "GO-LIVE.md": 0, "issue-gateway-cert.py": 1}''', '''\
EXPECTED_MARKED_LINES = {
    "README.md": 0,
    "GO-LIVE.md": 0,
    "issue-gateway-cert.py": 1,
    "prepare-host.sh": 0,
    "first-boot.sh": 0,
    "first-boot-check.sh": 0,
}''')
replace(SECRETS, '''\
    scan = _all_lines(path) if path.suffix == ".py" else _runnable_lines(path)''', '''\
    scan = _all_lines(path) if path.suffix in (".py", ".sh") else _runnable_lines(path)''')
print("the runbook's tests read the first boot where it is now")
TASK6_TESTS
python3 "$S/task6_tests.py"
````

Expected: `the runbook's tests read the first boot where it is now`. Lo que fijaba cada prueba adaptada y dónde lo fija ahora:

| Prueba | Fijaba | Ahora |
|---|---|---|
| `test_the_first_boot_the_reboot_and_the_upgrade_have_no_placeholders` | ningún marcador en los bloques de los requisitos, el primer arranque, el reinicio, la actualización y la copia fuera del host | lo mismo, y además en todos los bloques del apéndice |
| `test_after_a_reboot_unseals_starts_and_checks_the_certificate` → `test_after_a_reboot_openbao_unseals_itself_and_the_key_file_is_the_way_back` | tres `bao operator unseal`, `up -d`, `check-public-tls.sh` | ningún `bao operator unseal` en un bloque; `up -d`, `check-public-tls.sh`; `openbao-seal.sh status` y la devolución del archivo de clave, antes del reinicio |
| `test_the_first_boot_runs_the_scripts_that_check_it` | las órdenes de los pasos 1 a 7 y su orden, en «First boot» | las mismas, en la parte de `first-boot.sh` del apéndice; y `first-boot.sh` las ejecuta en ese orden; el desellado entre el 3.6 y el 4, en el apéndice y en el bloque `main` del guion (Q6); `install-backup-timer.sh` en la parte y en `first-boot-check.sh` |
| `test_the_first_boot_installs_the_gateway_certificates_renewal` | `enable --now` del temporizador antes del `up -d`, la unidad con el token del archivo | lo mismo en el apéndice, con `daemon-reload` antes de `enable`, en el apéndice y en el guion |
| `test_the_prerequisites_open_only_ssh_the_console_and_the_gateway` | 22, 443 y 8443 y nada más; Docker de su repositorio; la clave de GitHub | lo mismo en la parte de `prepare-host.sh` del apéndice **y** en el código del guion; la clave de GitHub, en P6, y P6 antes de P2 a P5 |
| nueva: `test_the_first_boot_is_three_scripts` | — | `sudo ./scripts/prepare-host.sh` en los requisitos; «First boot» son dos bloques, `./scripts/first-boot.sh` y `./scripts/first-boot-check.sh`; los tres guiones, ejecutables |
| nueva: `test_the_lines_the_runbook_quotes_are_lines_the_scripts_print` | — | cada línea citada está en un guion (al menos 10) |

- [ ] **Step 2: Las pruebas fallan, y la guarda de los secretos ve una línea plantada**

Run, desde `backend/`: `ITS_TEST_REDIS_URL=redis://localhost:6379/6 .venv/bin/python -m pytest -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning -p no:cacheprovider -q tests/test_runbook_first_boot.py tests/test_deploy_runbook_secrets.py`

Expected: `7 failed, 18 passed`: las siete de la tabla (la de los marcadores, porque no encuentra el apéndice); `test_deploy_runbook_secrets.py` pasa entero, porque los guiones ya cumplen.

Y la extensión de la guarda, en rojo con una línea que no se guarda (desde la raíz):

```bash
sed -i '1a # docker compose exec -e BAO_TOKEN="$t" openbao bao status' deploy/scripts/first-boot.sh
cd backend && ITS_TEST_REDIS_URL=redis://localhost:6379/6 .venv/bin/python -m pytest -p no:cacheprovider -q tests/test_deploy_runbook_secrets.py; cd ..
git checkout -- deploy/scripts/first-boot.sh
```

Expected: `1 failed, 13 passed`, la que falla `test_no_runbook_command_assigns_a_token_on_the_command_line[first-boot.sh]`; y `git status --short deploy/scripts` no imprime nada.

- [ ] **Step 3: El runbook, GO-LIVE y `docs/07`**

Con estos dos guiones, desde la raíz (no se guardan):

````bash
S=/tmp/claude-0/-home-user-Novieri-RMM/21768af2-984e-5cf8-87b2-4dc8a41a4864/scratchpad
cat > "$S/task6_readme.py" <<'TASK6_README'
"""Plan 2026-11-27, Task 6: deploy/README.md's first boot becomes the three
scripts and the moments in between; the blocks they run move, unchanged but
for what the scripts do differently, to the appendix "What each script does,
by hand" (design 2026-11-27-despliegue-automatizado §6). Every anchor must
occur exactly once where it is looked for; nothing is written unless all do.
"""

import re
from pathlib import Path

README = Path("deploy/README.md")
text = README.read_text()


def once(haystack: str, needle: str, where: str) -> int:
    assert haystack.count(needle) == 1, (where, needle[:70])
    return haystack.index(needle)


def between(haystack: str, start: str, end: str, where: str) -> str:
    i = once(haystack, start, where)
    j = haystack.index(end, i + len(start))
    return haystack[i:j]


def demote(block: str) -> str:
    """Every heading outside a code fence one level down: ### -> ####."""
    out, fenced = [], False
    for line in block.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and re.match(r"^#{2,5} ", line):
            line = "#" + line
        out.append(line)
    return "".join(out)


def edit(block: str, old: str, new: str, where: str) -> str:
    once(block, old, where)
    return block.replace(old, new)


PREREQ = "## Prerequisites: a new VPS (Ubuntu Server 24.04 LTS)\n"
FIRST_BOOT = "## First boot\n"
RECOVERY = "## Recovery link: an administrator who lost every second factor\n"
REBOOT = "## After a reboot or a restart\n"
UPGRADE = "## Upgrading an existing deployment\n"

prereq = between(text, PREREQ, "\n---\n\n" + FIRST_BOOT, "prerequisites")
first_boot = between(text, FIRST_BOOT, "\n---\n\n" + RECOVERY, "first boot")
reboot = between(text, REBOOT, "\n---\n\n" + UPGRADE, "after a reboot")

machine = between(prereq, PREREQ, "### P1 — ", "the machine")
p1 = between(prereq, "### P1 — ", "### P2 — ", "P1")
p2_to_p5 = between(prereq, "### P2 — ", "### P6 — ", "P2 to P5")
p6 = prereq[once(prereq, "### P6 — ", "P6") :]

# ------------------------------------------------------------ prerequisites

P1_NOTE = """\
The list contains `sudo`. Every command from here on runs as this account.
"""
P1_NOTE_NEW = """\
The list contains `sudo`. Every command from here on runs as this account.
`prepare-host.sh`, below, makes no account: it runs with `sudo` from this one,
and run as `root` alone it refuses and points here. On a VPS that comes with
only `root`, these lines, by hand, are the way.
"""
p1 = edit(p1, P1_NOTE, P1_NOTE_NEW, "P1")

P6_HEAD = "### P6 — The checkout\n"
P6_HEAD_NEW = """\
### P6 — The checkout

**Before P2 to P5** since 2026-11-27: `prepare-host.sh`, which does them, is in
this checkout. Ubuntu Server 24.04 ships `git` and `ssh-keygen`; if `git
--version` says the command is not found, `sudo apt-get install -y git` first.
"""
p6 = edit(p6, P6_HEAD, P6_HEAD_NEW, "P6")

PREPARE = """\
### P2 to P5 — `prepare-host.sh`

From `/opt/it-suite/deploy`, the checkout P6 made:

```sh
sudo ./scripts/prepare-host.sh
```

It runs P2 to P5 of [the appendix](#prepare-hostsh-p2-to-p5), in that order,
and skips what it finds done: the firewall (22, 443 and 8443 in, nothing else,
ufw on), unattended security updates, the clock (NTP on, if it is not), Docker
CE from Docker's own repository, and your account in the `docker` group. It
prints `run` before what it changes, `skip` for what is done already and `ok`
for a check that held; a `FAILED` line says what failed and the command to go
on with, and a second run after the fix picks up where it stopped. It ends:

```text
Docker's versions
  ok      Docker Engine … (26 or newer)
  ok      Docker Compose … (2.24 or newer)

The host is ready for the first boot
  note    log out, and log in again as rmmadmin: the docker group applies to a new login only
  note    then, as rmmadmin: cd /opt/it-suite/deploy && ./scripts/first-boot.sh
```

Log out and log in again, as it says. ⚠ **UNVERIFIED** on a real VPS: its
tests run it against stubbed commands (`backend/tests/test_prepare_host_script.py`);
turning ufw on and installing Docker have still been run by nobody in this
repository, as before 2026-11-27.

"""

new_prereq = machine + p1 + p6.rstrip("\n") + "\n\n" + PREPARE.rstrip("\n") + "\n"

# --------------------------------------------------------------- first boot

old_intro = between(first_boot, FIRST_BOOT, "### Step 1 — `.env`\n", "first boot intro")
steps_1_to_7 = between(first_boot, "### Step 1 — `.env`\n", "### Step 8 — Claim the installation\n", "1 to 7")
steps_8_9 = between(first_boot, "### Step 8 — Claim the installation\n", "### Step 10 — ", "8 and 9")
steps_10_12 = first_boot[once(first_boot, "### Step 10 — ", "10 to 12") :]
steps_10_12 = edit(steps_10_12, "\n**First boot is complete.**\n", "\n", "the end of step 12")

NEW_FIRST_BOOT = """\
## First boot

Three commands from `/opt/it-suite/deploy`, after the prerequisites, and the
moments that need you in between: the secrets only you may see or keep, and
what is done in the browser. Each script runs the blocks of [What each script
does, by hand](#appendix-what-each-script-does-by-hand), the appendix, in the
order they are numbered there, and checks each step before it runs it: a step
done already prints `skip`, one it does prints `run`, a check that held prints
`ok`. **Run a script again after any failure**, once its `FAILED` line's fix is
made: it picks up where it stopped, and never initialises OpenBao twice or
overwrites `.env`, the audit key or a token file. CI's `deploy` job runs
`first-boot.sh` and `first-boot-check.sh` on every push, against Pebble
(design `docs/superpowers/specs/2026-11-27-despliegue-automatizado-design.md`).

Start from genuinely empty state. If you are starting over after a failed
attempt rather than running the script again, `docker compose down -v` first
-- a half-initialised Postgres data directory or OpenBao barrier will hide the
step that actually broke -- and `rm .env`; `first-boot.sh` refuses a `.env`
and an OpenBao, or volumes, from two different first boots, and says this.
**A first boot after `down -v` asks Let's Encrypt for a new certificate**:
more than 5 in a week for `rmm.novieri.com` and production refuses.

**A rehearsal** -- a second first boot, a test of this runbook on another
machine -- uses Let's Encrypt's staging directory, whose certificates no
browser trusts and whose limits are far higher, and gives the check of step 7
staging's roots (the `curl` in [step 7 of the
appendix](#step-7--start-everything-traefik-obtains-the-certificate-for-443)
fetches them into `/tmp/le-staging-roots.pem`): `./scripts/first-boot.sh
--acme-ca-server https://acme-staging-v02.api.letsencrypt.org/directory
--tls-cafile /tmp/le-staging-roots.pem`.

### 1. `first-boot.sh`

```sh
./scripts/first-boot.sh
```

Steps 1 to 3.6; then OpenBao's auto-unseal -- `openbao-seal.sh enable`, and a
restart of OpenBao that proves it comes back unsealed on its own; then steps 4
to 7, with OpenBao unsealing itself, and step 8's install code. It stops for
you four times:

1. **The Let's Encrypt contact address** (step 1: `new-env.sh`'s question).
   It is required, but Let's Encrypt no longer mails expiry warnings nor keeps
   it on the account: a role mailbox will do.
2. **The five unseal key shares and the root token, shown once** (step 3.2).
   Copy each share to a different custodian and the root token to your
   password manager, all of it **off this VPS**, then type `SAVED`. The screen
   and its scrollback are cleared after it. With its output redirected rather
   than on a terminal, the script refuses before it initialises anything.
3. **Three of the five shares, hidden** (step 3.3). They unseal OpenBao and
   prove your copies are good: each must be one of the five just printed. The
   script keeps the same three for the auto-unseal, right after step 3.6, so
   you are not asked again. The root token is not asked either: it is kept
   from step 3.2 and written into `.env` (step 3.5).
4. **Your password, when `sudo` asks** -- for the gateway-issuer token's file
   (step 3.4), step 6b's timer and the auto-unseal's key file.

A good run proves the auto-unseal right after step 3.6, before step 4 starts
anything, and ends with the code to type in the browser:

```text
OpenBao restarts and unseals itself
  run     docker compose restart openbao
  ok      OpenBao restarted and came back unsealed by itself (Seal Type static, Sealed false): no share was typed

Step 4 — migrations
…

Step 8 — the install code

  install code  …

  note    open https://rmm.novieri.com/ in a browser you trust and enter it (deploy/README.md, First boot, 2): it is a password until you have

First boot: every step is done.
```

**Run again after `bao operator init`** -- it stopped somewhere after step 3.2
-- it no longer holds what it printed: if `.env` does not have the two tokens
yet it asks for the root token, hidden, and checks it is this OpenBao's; if
the auto-unseal is not on yet, it asks for three shares, once: they unseal
OpenBao if it is sealed, and move its seal. **From step 4 on, OpenBao unseals
itself**: a failure there -- step 7's certificate while DNS does not answer
with this VPS yet, say -- is run again with no share at all, and the five
shares are recovery keys now ([GO-LIVE.md §4](GO-LIVE.md#4-openbao-seal)).

### 2. In the browser: claim, your second factor, an enrollment token

1. **Claim the installation** (step 8): open `https://rmm.novieri.com/` and
   enter the install code `first-boot.sh` printed (`docker compose exec api
   cat /var/lib/its/secrets/setup-code` prints it again), with your name,
   email, organization name and the password you will sign in with. The
   organization's slug is `novieri`, from `.env`. Do it promptly, from a
   browser you trust: the code is the only thing between a stranger and this
   box. [Step 8 of the appendix](#step-8--claim-the-installation) says why.
2. **Sign in and enrol your second factor** (step 9): at
   `https://rmm.novieri.com/sign-in`, an authenticator app or a passkey, then
   ten recovery codes, shown once: keep them where you keep the shares.
3. **An enrollment token** (step 11b): at `https://rmm.novieri.com/enrollment`,
   named `first endpoint`, valid for 24 hours, **one** use, for the site
   **Principal** of your own organization's client. It is shown once: keep it
   for the next command and nowhere else.

### 3. `first-boot-check.sh`

```sh
./scripts/first-boot-check.sh
```

It asks OpenBao first (`bao status`: it must say `Sealed false`), then runs
step 10 (an `org-admin` exists) and 11a (the API refuses a request with no
session, `401`), asks for the token of 11b, hidden, and starts the throwaway
agent of 11c on this VPS. Then it says **when to press Run job → Collect
inventory** on that device in the console, and waits for the job to come back
signed and succeeded (11d); then it verifies the audit chain (11e), retires the
agent (11f) and takes the first backup (12). A good run ends:

```text
Step 12 — the first backup, so that there is a way back
  ok      step 12: the newest backup: …
  note    That backup is on this VPS and goes with it. The off-host copy is recommended before anything
  note    real is enrolled: deploy/README.md, The off-host copy, step by step.

First boot is complete.
```

**It is safe to run again**, and its first lines say what it does to this VPS:
it enrols a throwaway agent here (11c) and retires it, with its key (11f). Run
again after it stopped, it finds that agent still running and goes on from
11d; run again after it finished, it enrols and retires another, and the first
stays in the console as a record, offline.

### 4. Custody: off this VPS, now

- **The five shares and the root token** (step 3.2). With the auto-unseal they
  are **recovery keys**: they authorize `openbao-seal.sh disable` and `bao
  operator generate-root`, and they cannot unseal OpenBao.
- **The auto-unseal's key file, `/etc/it-suite/openbao-unseal/unseal.key`**, off
  this VPS and **apart from any copy of `openbao-data`**: someone with both
  holds the job-signing key and the CA. If it is lost, OpenBao cannot be
  unsealed at all, by anyone (GO-LIVE.md §4).
- **The audit HMAC key** (the `its-secrets` volume), the `its-ca` and
  `openbao-data` volumes and `.env`, to a different place than the database
  backups: GO-LIVE.md §6 and `docs/07-backup-restore.md` §1-2.
- **The ten recovery codes** of step 9, with the shares.

The nightly backup is on this VPS and goes with it: [the off-host
copy](#the-off-host-copy-step-by-step) is recommended before anything real is
enrolled. Until step 12, a new host has no way back at all; after it, the way
back is restoring that backup ([Rolling back a
deployment](#rolling-back-a-deployment)).

**First boot is complete.**
"""

# ----------------------------------------------------------------- appendix

APPENDIX_HEAD = """\
## Appendix: what each script does, by hand

The blocks the three scripts of [First boot](#first-boot) run, in their order:
until 2026-11-27 they were the first boot itself, pasted one by one. For a
step a script stopped at, or for a first boot without the scripts. A script
runs exactly these commands, but where a step says what the script does
instead (it holds a secret in memory where you would paste it, for one), and
it checks each step before running it. Run from `/opt/it-suite/deploy`.

"""

PREPARE_PART = """\
### `prepare-host.sh`: P2 to P5

As root, from the operator account (`sudo ./scripts/prepare-host.sh`). The
script adds `DEBIAN_FRONTEND=noninteractive`, so no package asks a question
in the middle of a run.

"""
p2_to_p5_demoted = demote(p2_to_p5)

FIRST_BOOT_PART = """\
### `first-boot.sh`: steps 1 to 7, with the auto-unseal after 3.6

"""

AUTO_UNSEAL = """\
#### The auto-unseal, and the restart that proves it

On every first boot since 2026-11-27 (the operator's decision, GO-LIVE.md §4),
right after step 3.6 and before step 4 starts anything else: OpenBao's seal
moves from the five shares to a static key in a file on this host, and OpenBao
unseals itself after every restart and reboot -- so a step from 4 on that
fails is run again with no share. Three shares, hidden, at its prompts:

```sh
sudo env -u COMPOSE_FILE ./scripts/openbao-seal.sh enable --key-file /etc/it-suite/openbao-unseal/unseal.key
```

It snapshots `openbao-data` to `/var/backups/it-suite/openbao-seal/` first,
writes the key (`0400`, the OpenBao user's), adds `COMPOSE_FILE` and
`OPENBAO_UNSEAL_KEY_FILE` to `.env`, migrates the seal with the shares, and
proves it with a restart of its own. `first-boot.sh` adds `--yes` and gives it
the three shares of step 3.3 on its stdin. `env -u COMPOSE_FILE` changes
nothing on this VPS, where only `.env` sets `COMPOSE_FILE`; it matters where
the environment sets one (CI's Pebble overlay, a rehearsal's), which would win
over the line the script writes into `.env` and recreate OpenBao without its
new seal. Then the proof, by hand as the script does it:

```sh
docker compose restart openbao
docker compose exec openbao bao status
```

```
Seal Type          static
Initialized        true
Sealed             false
```

`Sealed false`, with no share typed. The five shares are **recovery keys**
from now on: they authorize `openbao-seal.sh disable` and `bao operator
generate-root`, and cannot unseal.

"""

BROWSER_PART = """\
### In the browser: steps 8 and 9

`first-boot.sh` asks the api for `/api/v1/setup/status` itself -- that first
request is what generates the install code -- and prints the code; by hand,
the browser's first visit does it, as below.

"""

CHECK_PART = """\
### `first-boot-check.sh`: steps 10 to 12

"""

# What the scripts do differently, said where the block is.
appendix_1_to_7 = demote(steps_1_to_7)
appendix_1_to_7 = edit(appendix_1_to_7, """\
```sh
docker compose up -d postgres
```
""", """\
```sh
docker compose up -d --wait postgres
```
""", "step 2")
appendix_1_to_7 = edit(appendix_1_to_7, """\
#### 3.3 Unseal — three of the five shares
""", """\
#### 3.3 Unseal — three of the five shares

`first-boot.sh` asks for the three shares itself, hidden, and sends each to
`bao write sys/unseal` on stdin, never in an argv; when the same run printed
the five, each one typed must be one of them.
""", "step 3.3")
appendix_1_to_7 = edit(appendix_1_to_7, """\
> **This happens again every restart.** OpenBao returns **sealed** after any
""", """\
> **Without the auto-unseal, this happens again every restart** -- the
> auto-unseal, right after step 3.6, turns that off. OpenBao returns
> **sealed** after any
""", "the restart note")
appendix_1_to_7 = edit(appendix_1_to_7, """\
#### 3.4 The runtime policies and their tokens
""", """\
#### 3.4 The runtime policies and their tokens

`first-boot.sh` opens no shell: it sends the root token it kept from 3.2 on
the stdin of each `bao` command (`sh -c 'read -r BAO_TOKEN && export
BAO_TOKEN && exec "$@"'`), reads each new token with `-field=token`, and
writes the `gateway-issuer` one with `printf | sudo tee`, as below.
""", "step 3.4")
appendix_1_to_7 = edit(appendix_1_to_7, """\
#### 3.5 Put both tokens in `.env`
""", """\
#### 3.5 Put both tokens in `.env`

`first-boot.sh` writes them the same way, from memory. Run again after 3.2 in
another run, it asks for the root token once, hidden, checks it is this
OpenBao's root token, and mints the `its-app` token afresh.
""", "step 3.5")
appendix_1_to_7 = edit(appendix_1_to_7, """\
```sh
docker compose run --rm --no-deps --entrypoint sh api -c \\
  'head -c 32 /dev/urandom > /var/lib/its/secrets/audit-hmac.key && \\
   chmod 600 /var/lib/its/secrets/audit-hmac.key'
""", """\
`set -C` makes the shell refuse to write over a key that is there already:
replacing it would leave every audit chain written so far unverifiable.

```sh
docker compose run --rm --no-deps --entrypoint sh api -c \\
  'set -C && head -c 32 /dev/urandom > /var/lib/its/secrets/audit-hmac.key && \\
   chmod 600 /var/lib/its/secrets/audit-hmac.key'
""", "step 5")
appendix_1_to_7 = edit(appendix_1_to_7, """\
sudo systemctl enable --now its-gateway-cert.timer
""", """\
sudo systemctl daemon-reload
sudo systemctl enable --now its-gateway-cert.timer
""", "step 6b")
appendix_1_to_7 = edit(appendix_1_to_7, """\
The three `Exited (0)` services are the one-shot jobs; that is success.
""", """\
The three `Exited (0)` services are the one-shot jobs; that is success.
`first-boot.sh` waits up to five minutes for that table, and then 15 seconds
more with no container restarting: a service that restarts for ever is up for
a moment at a time (row 15 of [What
was broken](#what-was-broken-and-how-you-would-recognise-it)).
""", "step 7")
STEP_4 = "#### Step 4 — Migrations, then table privileges\n"
appendix_1_to_7 = appendix_1_to_7.replace(STEP_4, AUTO_UNSEAL + STEP_4)
once(appendix_1_to_7, AUTO_UNSEAL + STEP_4, "the auto-unseal, before step 4")
appendix_8_9 = demote(steps_8_9)
appendix_10_12 = demote(steps_10_12)
appendix_10_12 = edit(appendix_10_12, """\
journalctl -u its-platform-backup.service --no-pager -n 20
""", """\
sudo journalctl -u its-platform-backup.service --no-pager -n 20
""", "step 12")
appendix_10_12 = edit(appendix_10_12, """\
##### 11d — Round-trip a signed job
""", """\
##### 11d — Round-trip a signed job

`first-boot-check.sh` reads the device's id from the agent's `msg=enrolled`
line, says when to press the button, and asks for that device's newest job a
person asked for until it ends, for up to ten minutes (`--wait-job`).
""", "step 11d")

APPENDIX = (
    APPENDIX_HEAD
    + PREPARE_PART
    + p2_to_p5_demoted.rstrip("\n") + "\n\n"
    + FIRST_BOOT_PART
    + appendix_1_to_7
    + BROWSER_PART
    + appendix_8_9
    + CHECK_PART
    + appendix_10_12.rstrip("\n") + "\n"
)
# The old intro's two paragraphs (empty state, staging) are reworded in NEW_FIRST_BOOT.
assert "Start from genuinely empty state." in old_intro

# --------------------------------------------------------- after a reboot

NEW_REBOOT = """\
## After a reboot or a restart

OpenBao **unseals itself** on this VPS since 2026-11-27: `first-boot.sh`
turned the static-key auto-unseal on (GO-LIVE.md §4). Ask it first anyway,
alone, whatever else happened: a sealed OpenBao makes `docker compose up -d`
**block** -- `openbao-init` waits on a health check that passes only when it is
unsealed. (`.env` must already hold `ITS_PUBLIC_HOST` and `ITS_ACME_EMAIL`, as
it does on this VPS from step 1: Compose interpolates the whole file for every
subcommand, `bao status` included.)

```sh
cd /opt/it-suite/deploy
docker compose exec openbao bao status
```

`Sealed false`, with `Seal Type static`: then

```sh
docker compose up -d
docker compose ps -a --format 'table {{.Service}}\\t{{.Status}}'
./scripts/check-public-tls.sh
curl -s -o /dev/null -w '%{http_code}\\n' https://rmm.novieri.com/api/v1/devices
```

`up -d` runs `migrate` first, as `api`'s dependency, every time; after a reboot
it finds nothing to migrate and exits 0. The table is step 7's; the certificate
check prints `ok`; and the last line prints `401` (the API answering, no one
signed in). A `503` there means the api is not healthy yet (Traefik answers 503
for the api's paths while it is not): wait, and look at `docker compose logs
api` if it lasts.

### If `bao status` says `Sealed true`

The auto-unseal's key file is missing or not this OpenBao's. **The five shares
cannot unseal it**: they are recovery keys since the auto-unseal went on. Look:

```sh
sudo ./scripts/openbao-seal.sh status
docker compose logs --tail 50 openbao
```

`the key file is missing`, or a file that is not OpenBao's key: put back the
copy you keep off this VPS. Copy it from your computer to this VPS as
`~/unseal.key` (with `scp`), then:

```sh
sudo install -D -m 400 ~/unseal.key /etc/it-suite/openbao-unseal/unseal.key && shred -u ~/unseal.key
sudo chown "$(docker compose exec -T openbao id -u openbao | tr -d '\\r'):$(docker compose exec -T openbao id -g openbao | tr -d '\\r')" /etc/it-suite/openbao-unseal/unseal.key
docker compose restart openbao
docker compose exec openbao bao status
```

`Sealed false`: go on with `docker compose up -d` above. **If the key file is
lost for good, OpenBao cannot be unsealed by anyone**, with the shares or
without them: the job-signing key, the agent CA and every sealed credential go
with it, and the way back is a restore with a copy of `openbao-data` **and** its
key file (`docs/07-backup-restore.md` §3).

A host still on Shamir -- one from before 2026-11-27 that never ran
`openbao-seal.sh enable`, or one that ran `disable` -- unseals with three of the
five shares, each at the prompt, from three custodians:
`docker compose exec openbao bao operator unseal`, three times, then `bao status`.
"""

# ----------------------------------------------------------------- upgrading

UPGRADE_SEALED = """\
`Sealed true` means unsealing as in [After a reboot or a
restart](#after-a-reboot-or-a-restart) before anything else, since `deploy.sh`
refuses to start on a sealed OpenBao. `Sealed false`:
"""
UPGRADE_SEALED_NEW = """\
`Sealed true` means OpenBao's key file is missing or not its own: [After a
reboot or a restart](#if-bao-status-says-sealed-true) says what to do, before
anything else, since `deploy.sh` refuses to start on a sealed OpenBao.
`Sealed false`:
"""

# ---------------------------------------------------------------- the status

STATUS = "## Status of this runbook\n\n"
STATUS_NEW = """\
## Status of this runbook

**Since 2026-11-27 the first boot is three scripts** (design
`docs/superpowers/specs/2026-11-27-despliegue-automatizado-design.md`):
`prepare-host.sh` (P2 to P5), `first-boot.sh` (steps 1 to 3.6, OpenBao's
auto-unseal, steps 4 to 7 and step 8's install code) and `first-boot-check.sh`
(steps 10 to 12), with the moments that need a person in between ([First
boot](#first-boot)). The blocks they run are in [the
appendix](#appendix-what-each-script-does-by-hand), for a step a script stopped
at or a first boot without them. CI's `deploy` job runs the last two on every
push, against Pebble, with five shares; `prepare-host.sh` runs only against
stubs. **OpenBao unseals itself**: the operator chose the static-key
auto-unseal for every first boot on 2026-11-27, knowing its cost (GO-LIVE.md
§4), and the five shares are recovery keys.

"""
REHEARSED = "⚠ **Rehearsed on 2026-11-24, not yet run on the VPS.** The whole First boot\nbelow was run block by block"
REHEARSED_NEW = "⚠ **Rehearsed on 2026-11-24, not yet run on the VPS.** The whole First boot\nof that day, now [the appendix](#appendix-what-each-script-does-by-hand)'s\nblocks, was run block by block"

text = text.replace(prereq, new_prereq)
text = text.replace(first_boot, NEW_FIRST_BOOT)
text = text.replace(reboot, NEW_REBOOT)
once(text, UPGRADE_SEALED, "upgrade")
text = text.replace(UPGRADE_SEALED, UPGRADE_SEALED_NEW)
once(text, STATUS, "status")
text = text.replace(STATUS, STATUS_NEW)
once(text, REHEARSED, "rehearsed")
text = text.replace(REHEARSED, REHEARSED_NEW)
text = text.rstrip("\n") + "\n\n---\n\n" + APPENDIX
README.write_text(text)
print("deploy/README.md: First boot is the three scripts; the blocks are the appendix")
TASK6_README
python3 "$S/task6_readme.py"
````

Expected: `deploy/README.md: First boot is the three scripts; the blocks are the appendix`.

````bash
S=/tmp/claude-0/-home-user-Novieri-RMM/21768af2-984e-5cf8-87b2-4dc8a41a4864/scratchpad
cat > "$S/task6_docs.py" <<'TASK6_DOCS'
"""Plan 2026-11-27, Task 6: GO-LIVE.md §1, §4 and §6 and
docs/07-backup-restore.md say what first-boot.sh does: the auto-unseal on
every first boot, the shares as recovery keys, the key file's custody
(design 2026-11-27-despliegue-automatizado §4, §6). Each anchor once."""

from pathlib import Path

GO_LIVE = Path("deploy/GO-LIVE.md")
BACKUP = Path("docs/07-backup-restore.md")


def replace(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert text.count(old) == 1, (path, old[:70])
    path.write_text(text.replace(old, new))


def replace_between(path: Path, start: str, end: str, new: str) -> None:
    text = path.read_text()
    assert text.count(start) == 1, (path, start)
    i = text.index(start)
    j = text.index(end, i)
    path.write_text(text[:i] + new + text[j:])


# ------------------------------------------------------------- GO-LIVE §1

replace(GO_LIVE, """\
`./scripts/new-env.sh --host rmm.novieri.com` (`deploy/README.md` step 1)
writes `.env` from `.env.example`, mode 600,""", """\
`./scripts/new-env.sh --host rmm.novieri.com` (`deploy/README.md`, appendix,
step 1, which `first-boot.sh` runs) writes `.env` from `.env.example`, mode 600,""")

# ------------------------------------------------------------- GO-LIVE §4

replace_between(GO_LIVE, "## 4. OpenBao seal\n", "- **The host must not swap", """\
## 4. OpenBao seal

**Every first boot turns on the auto-unseal with a static key, since
2026-11-27.** The operator decided it that day ("Activarlo siempre"), knowing
the cost in the table below: right after step 3.6, before step 4 starts
anything else, `first-boot.sh` runs `openbao-seal.sh enable` with the three
shares of step 3.3, then restarts OpenBao to prove it comes back unsealed on
its own (`deploy/README.md`, First boot). No invariant of
`CLAUDE.md` changes: jobs stay signed by the server and verified by the agent
(1), and stored credentials stay encrypted in the vault (3). What changes is
how the vault is protected **at rest**, below.

- **The 5 shares and the root token, OFF this host**, split across different
  custodians. `bao operator init` prints them **exactly once**. With the
  auto-unseal they are **recovery keys**: they authorize `openbao-seal.sh
  disable` and `bao operator generate-root`, and they **cannot unseal**
  OpenBao.
- **The key file, `/etc/it-suite/openbao-unseal/unseal.key`** (mode `0400`,
  owned by the OpenBao user): off this host, **apart from any copy of
  `openbao-data`**. Whoever has both has the job-signing key and the CA. **If
  it is lost, OpenBao cannot be unsealed at all**, by anyone: the shares do not
  unseal, and the way back is a restore of `openbao-data` with its key file
  (`docs/07-backup-restore.md` §3).
- **After a reboot OpenBao unseals itself.** `docker compose exec openbao bao
  status` says `Sealed false` and `Seal Type static`; `Sealed true` means the
  key file is missing or not its own, and `deploy/README.md`, "After a reboot
  or a restart", says how to put it back. `./scripts/deploy.sh` checks first
  and stops with the same instructions.
- **What the static key costs**, against the Shamir seal a host had before
  2026-11-27:

  | | Shamir (before 2026-11-27) | Static key (every first boot since) |
  |---|---|---|
  | After a reboot or power cut | job signing, the CA and the vault are **dead** until a person enters 3 of 5 shares | back on its own in seconds |
  | Someone copies the disk or an `openbao-data` backup | useless without the shares | useless **unless they also get the key file** |
  | Someone gets the key file **and** `openbao-data` | — | every vault key, the job-signing key and the CA: domain-admin over the fleet |
  | The key file is lost | — | **OpenBao cannot be unsealed at all.** The shares become recovery keys and cannot unseal |

  So the key file lives outside the repository, outside Docker's data root
  and outside the backup root (`openbao-seal.sh` refuses those), and is backed
  up off-host apart from any copy of `openbao-data`. For one operator on one
  host, a platform that comes back after a power cut is worth more than
  protection against someone who already has root on the host, which is who
  can read that file. A TPM- or KMS-backed seal would remove the file; neither
  is wired up here (there is no KMS on this VPS).

  ```sh
  cd /opt/it-suite/deploy
  sudo ./scripts/openbao-seal.sh status
  sudo ./scripts/openbao-seal.sh disable   # back to Shamir, with 3 of the 5 recovery keys
  sudo ./scripts/openbao-seal.sh enable    # a host from before 2026-11-27: asks for 3 shares (hidden)
  ```

  `enable` refuses unless OpenBao is running, unsealed and Shamir-sealed,
  snapshots `openbao-data` to `/var/backups/it-suite/openbao-seal/` first,
  writes the key to `/etc/it-suite/openbao-unseal/unseal.key` (or
  `--key-file`), adds `COMPOSE_FILE` and `OPENBAO_UNSEAL_KEY_FILE` to `.env`,
  and finishes by restarting OpenBao and checking it unsealed with no shares.
""")

# ------------------------------------------------------------- GO-LIVE §6

replace(GO_LIVE, """\
- **The OpenBao unseal shares and root token** (§4). These are not on the host
  and must not be. **With auto-unseal, also the key file**
  (`/etc/it-suite/openbao-unseal/unseal.key`): off-host, and never beside a
  copy of `openbao-data`.
""", """\
- **The OpenBao shares and root token** (§4), recovery keys since the
  auto-unseal: not on the host, and must not be. **And the auto-unseal's key
  file** (`/etc/it-suite/openbao-unseal/unseal.key`), on every host since
  2026-11-27: off-host, and never beside a copy of `openbao-data`.
""")

# ------------------------------------------------------------------ docs/07

replace(BACKUP, """\
| 5 | **OpenBao unseal keys + root token** | *operator-held, outside the stack* | the volume in #4 becomes undecryptable ballast |
""", """\
| 5 | **OpenBao's auto-unseal key file** (every host since 2026-11-27), **its five shares** (recovery keys with it; unseal keys on a host still on Shamir) **+ root token** | *operator-held, outside the stack*; the key file also at `/etc/it-suite/openbao-unseal/unseal.key` | without the key file, or on Shamir without three shares, the volume in #4 becomes undecryptable ballast |
""")
replace(BACKUP, """\
And, **once, at install time, not per-backup**: store the OpenBao unseal keys
and root token from `bao operator init` in a separate secret store (a password
manager or sealed envelope). They are printed once and never again. A backup
of `openbao-data` without them restores nothing.
""", """\
And, **once, at install time, not per-backup**: store the OpenBao shares and
root token from `bao operator init` in a separate secret store (a password
manager or sealed envelope). They are printed once and never again. **And the
auto-unseal's key file** (`/etc/it-suite/openbao-unseal/unseal.key`, on every
host since 2026-11-27), apart from any copy of `openbao-data`: with it,
OpenBao unseals itself and the shares only recover; a backup of
`openbao-data` without it restores nothing.
""")
replace(BACKUP, """\
# 5. Unseal OpenBao with the operator-held keys, then bring the stack up.
docker compose up -d openbao
docker compose exec openbao bao operator unseal   # repeat to threshold
docker compose up -d
""", """\
# 5. OpenBao: its auto-unseal key file back first, at
#    /etc/it-suite/openbao-unseal/unseal.key (deploy/README.md, "After a
#    reboot or a restart"), and it unseals itself -- `bao status` says
#    Sealed false. A host still on Shamir unseals instead with three shares:
#    docker compose exec openbao bao operator unseal, three times.
docker compose up -d openbao
docker compose exec openbao bao status
docker compose up -d
""")
replace(BACKUP, """\
- the **unseal keys and root token** from `bao operator init`, held outside
  the stack (§2.3), **and**
""", """\
- the **auto-unseal key file** (every host since 2026-11-27), and the
  **shares and root token** from `bao operator init`, held outside the stack
  (§2.3), **and**
""")
replace(BACKUP, """\
The unseal keys are not optional. OpenBao's file storage is encrypted at rest
by keys derived from them; `openbao-data` without them restores nothing.
""", """\
The key file -- on a host still on Shamir, three shares -- is not optional.
OpenBao's file storage is encrypted at rest by keys it unwraps; `openbao-data`
without it restores nothing.
""")
print("GO-LIVE.md §1, §4 and §6 and docs/07 say what first-boot.sh does")
TASK6_DOCS
python3 "$S/task6_docs.py"
````

Expected: `GO-LIVE.md §1, §4 and §6 and docs/07 say what first-boot.sh does`. Y la forma del README:

```bash
grep -n '^## \(Status of this runbook\|Prerequisites\|First boot\|After a reboot\|Appendix\)' deploy/README.md
grep -c '' deploy/README.md
```

Expected: las cinco cabeceras, con `## Appendix: what each script does, by hand` la última del archivo; y `4893` líneas.

- [ ] **Step 4: Las pruebas pasan**

Run, desde `backend/`: `ITS_TEST_REDIS_URL=redis://localhost:6379/6 .venv/bin/python -m pytest -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning -p no:cacheprovider -q tests/test_runbook_first_boot.py tests/test_deploy_runbook_secrets.py tests/test_runbook_without_keycloak.py tests/test_no_old_brand_names.py tests/test_deploy_env_documentation.py tests/test_email_domain_runbook.py tests/test_install_link_docs.py tests/test_first_boot_script.py::test_step_6b_writes_the_units_the_runbook_writes` y `.venv/bin/ruff check app tests`.

Expected: `106 passed` (entre ellas `test_every_in_page_link_lands_on_a_heading`: cada ancla nueva lleva a un encabezado; la guarda de los nombres viejos sobre el README nuevo y los tres guiones; y la de las unidades del 6b sobre el apéndice); `All checks passed!`.

Y el orden del desellado en rojo (resolución Q6), con el bloque del apéndice movido tras el paso 7, donde estaba antes de la resolución, y el README de vuelta después (desde la raíz; la copia no se guarda):

```bash
S=/tmp/claude-0/-home-user-Novieri-RMM/21768af2-984e-5cf8-87b2-4dc8a41a4864/scratchpad
cp deploy/README.md "$S/README.good"
python3 - <<'PLANT'
from pathlib import Path
readme = Path("deploy/README.md"); text = readme.read_text()
start = text.index("#### The auto-unseal, and the restart that proves it\n")
end = text.index("#### Step 4 — Migrations, then table privileges\n")
part = text[start:end]
text = text[:start] + text[end:]
browser = "### In the browser: steps 8 and 9\n"
assert text.count(browser) == 1
readme.write_text(text.replace(browser, part + browser))
print("planted: the auto-unseal after step 7, where it was before the ruling")
PLANT
cd backend && ITS_TEST_REDIS_URL=redis://localhost:6379/6 .venv/bin/python -m pytest -p no:cacheprovider -q tests/test_runbook_first_boot.py 2>&1 | tail -2; cd ..
cp "$S/README.good" deploy/README.md && rm "$S/README.good"
cd backend && ITS_TEST_REDIS_URL=redis://localhost:6379/6 .venv/bin/python -m pytest -p no:cacheprovider -q tests/test_runbook_first_boot.py 2>&1 | tail -1; cd ..
```

Expected: `planted: the auto-unseal after step 7, where it was before the ruling`; `FAILED tests/test_runbook_first_boot.py::test_the_first_boot_runs_the_scripts_that_check_it` y `1 failed, 10 passed`; y, con el README de vuelta, `11 passed`.

- [ ] **Step 5: El diseño, §6** (resolución Q7)

Con este guion, desde la raíz (no se guarda en el repositorio):

```bash
S=/tmp/claude-0/-home-user-Novieri-RMM/21768af2-984e-5cf8-87b2-4dc8a41a4864/scratchpad
cat > "$S/task6_spec.py" <<'TASK6_SPEC'
"""Plan 2026-11-27, Task 6: the design's §6 names docs/07-backup-restore.md
(coordinator's ruling Q7, 2026-11-27). One anchor."""

from pathlib import Path

SPEC = Path("docs/superpowers/specs/2026-11-27-despliegue-automatizado-design.md")
OLD = """\
- `deploy/GO-LIVE.md` §4: desellado automático por defecto, con la fecha de la
  decisión y la custodia del archivo de clave.
"""
NEW = OLD + """\
- `docs/07-backup-restore.md`: la fila 5 de §1, §2.3, el paso 5 de §3 y §5, que
  desellaban con partes; con el desellado automático las partes ya no desellan
  (decisión del coordinador del 2026-11-27, pregunta Q7 del plan).
"""
text = SPEC.read_text()
assert text.count(OLD) == 1, "§6's GO-LIVE bullet is not there exactly once"
SPEC.write_text(text.replace(OLD, NEW))
print("design §6: docs/07-backup-restore.md too")
TASK6_SPEC
python3 "$S/task6_spec.py"
grep -c '^- `docs/07-backup-restore.md`' docs/superpowers/specs/2026-11-27-despliegue-automatizado-design.md
```

Expected: `design §6: docs/07-backup-restore.md too` y `1`.

- [ ] **Step 6: Commit**

```bash
git add deploy/README.md deploy/GO-LIVE.md docs/07-backup-restore.md backend/tests/test_runbook_first_boot.py backend/tests/test_deploy_runbook_secrets.py docs/superpowers/specs/2026-11-27-despliegue-automatizado-design.md
git commit -m "docs(deploy): the first boot is three scripts; their blocks are the appendix

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013TnTdGAz4rSe1YRXXjEqyr"
```

---

### Task 7: El informe de despliegue, sobre el runbook nuevo (oleada 4)

**Files:**
- Ninguno del repositorio. El informe va al coordinador, que lo lleva al operador (`CLAUDE.md`, «Reporting to the operator»; diseño §6).

**Interfaces:**
- Consumes: la rama del proyecto con las Tasks 1 a 6 fusionadas, en el orden de las oleadas (5 antes que 6); su `deploy/README.md` es la única fuente de los comandos del informe (Interfaces de la Task 6: la estructura que lee el extractor).
- Produces: el informe, en español, con las ocho partes del Step 3.

**Decisiones de la tarea.**
- **Los comandos salen del runbook, no se reescriben** (como en el plan del 2026-11-23, Task 13): `report_sequence.py`, adaptado a la estructura nueva, lee cada bloque `sh` de «Prerequisites» y «First boot» en orden, con el bloque `text` con que termina cada uno, y las secuencias de «After a reboot or a restart» y de la actualización. El apéndice no entra en la secuencia: es lo que los guiones ejecutan. Ya no hace falta el `SKIP` de antes: la forma interactiva del paso 6 y las raíces de *staging* del 7 están en el apéndice.
- **«Toda secuencia empieza por `bao status`»** (`CLAUDE.md`): en un host nuevo no hay OpenBao hasta el paso 3.1, que `first-boot.sh` arranca y pregunta él mismo; `first-boot-check.sh` pregunta antes que nada; las secuencias de después de un reinicio y de una actualización empiezan por `docker compose exec openbao bao status` en un bloque propio. El informe lo dice así.
- **Lo que solo hace el operador es un paso numerado propio**: el «Paso 0», antes de la secuencia, y en ella los momentos del navegador y de la custodia, marcados como suyos.

- [ ] **Step 1: El árbol fusionado**

Worktree nueva desde `origin/claude/compassionate-bardeen-vgtwus` (`git worktree add -b p5-t7 ../wt-p5-t7 origin/claude/compassionate-bardeen-vgtwus`), con la preparación de Global Constraints. Desde su raíz (`p5-t5` y `p5-t6` llevan ya las cuatro primeras):

```bash
git merge --no-edit p5-t5 p5-t6
git log --oneline -1
git merge-base --is-ancestor 6f616aa HEAD && echo HAS-SPEC
grep -c '^## Appendix: what each script does, by hand$' deploy/README.md
cd backend && ITS_TEST_REDIS_URL=redis://localhost:6379/7 .venv/bin/python -m pytest -p no:cacheprovider -q tests/test_runbook_first_boot.py tests/test_ci_first_boot.py tests/test_first_boot_script.py; cd ..
```

Expected: esa fusión; `HAS-SPEC`; `1`; `58 passed` (11, 15 y 32).

- [ ] **Step 2: Las secuencias, del runbook**

`$S/report_sequence.py` (no se guarda en el repositorio):

````bash
S=/tmp/claude-0/-home-user-Novieri-RMM/21768af2-984e-5cf8-87b2-4dc8a41a4864/scratchpad
cat > "$S/report_sequence.py" <<'REPORT_SEQUENCE'
"""Plan 2026-11-27, Task 7: the report's copy-paste sequences, read out of
deploy/README.md as it is on the merged branch, so the report and the runbook
cannot say different things. Not committed. (Plan 2026-11-23's Task 13
extractor, adapted to the runbook of three scripts.)

    python3 report_sequence.py deploy/README.md > sequence.md

Every `sh` block of "Prerequisites: a new VPS" and "First boot", in order,
under its heading; a heading with one `sh` block shows the `text` block its
section ends with, as how a good run ends; a heading with none is a step done
in the browser or by hand, listed with the runbook's first paragraph as the
operator's. Then "After a reboot or a restart" and the VPS upgrade, each
opening with `bao status` alone and saying, between the blocks, what `Sealed
false` and `Sealed true` mean. Then, apart and optional, "The off-host copy,
step by step", as the runbook words it. The appendix is not a sequence: it is
what the scripts run.
"""

import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text()
FENCE = re.compile(r"```(sh|text|json)?\n(.*?)```", re.DOTALL)


def section(heading: str, stop: str = "\n## ") -> str:
    start = text.index(heading)
    end = text.find(stop, start + len(heading))
    return text[start:] if end == -1 else text[start:end]


def steps(body: str) -> list[tuple[str, str]]:
    parts = re.split(r"(?m)^(### .+)$", body)
    # "### 1. `first-boot.sh`": the runbook's own number goes; the sequence numbers.
    titles = [re.sub(r"^\d+\. ", "", parts[i].lstrip("# ").strip()) for i in range(1, len(parts), 2)]
    return [(title, parts[i + 1]) for title, i in zip(titles, range(1, len(parts), 2))]


def sh(body: str) -> list[str]:
    return [f.group(2) for f in FENCE.finditer(body) if f.group(1) == "sh"]


def render(title: str, body: str, out: list[str], n: list[int]) -> None:
    n[0] += 1
    out.append(f"**{n[0]}. {title}**\n")
    blocks = sh(body)
    if not blocks:
        first = next((p.strip() for p in body.strip().split("\n\n") if p.strip()), "")
        out.append("Lo hace el operador (navegador o a mano): " + " ".join(first.split()) + "\n")
        return
    for block in blocks:
        out.append("```sh\n" + block + "```\n")
    ends = [f.group(2) for f in FENCE.finditer(body) if f.group(1) == "text"]
    if len(blocks) == 1 and ends:
        out.append("Una ejecución buena termina así:\n\n```\n" + ends[-1] + "```\n")


out: list[str] = []
n = [0]
out.append("### El primer arranque de rmm.novieri.com\n")
for title, body in steps(section("## Prerequisites: a new VPS (Ubuntu Server 24.04 LTS)\n")):
    render(title, body, out, n)
for title, body in steps(section("## First boot\n")):
    render(title, body, out, n)

reboot = sh(section("## After a reboot or a restart\n"))
assert len(reboot) == 4, f"After a reboot has {len(reboot)} sh blocks, not 4: read it again"
out.append("\n### Después de un reinicio\n")
n[0] += 1
out.append(f"**{n[0]}. After a reboot or a restart**\n")
out.append("```sh\n" + reboot[0] + "```\n")
out.append("Tiene que decir `Sealed false` (y `Seal Type static`). Entonces:\n")
out.append("```sh\n" + reboot[1] + "```\n")
out.append("Si dice `Sealed true`, falta el archivo de clave del desellado o no es el suyo "
           "(las cinco partes ya no desellan):\n")
out.append("```sh\n" + reboot[2] + "```\n")
out.append("Y, con la copia del archivo de clave traída de su custodia como `~/unseal.key`:\n")
out.append("```sh\n" + reboot[3] + "```\n")
out.append("Con `Sealed false`, el bloque de `docker compose up -d` de arriba.\n")

upgrade = section("## Upgrading an existing deployment\n")
marker = "**On the VPS this runbook builds**"
first = FENCE.search(upgrade, upgrade.index(marker))
assert first is not None
second = FENCE.search(upgrade, first.end())
assert second is not None
out.append("\n### Una actualización en este VPS\n")
out.append("```sh\n" + first.group(2) + "```\n")
out.append("Si dice `Sealed true`, primero lo de «Después de un reinicio». Con `Sealed false`:\n")
out.append("```sh\n" + second.group(2) + "```\n")
out.append("\n### Opcional, recomendado: la copia fuera del host\n")
out.append(section("### The off-host copy, step by step\n", stop="\n## ").strip() + "\n")
print("\n".join(out))
REPORT_SEQUENCE
python3 "$S/report_sequence.py" deploy/README.md > "$S/sequence.md" && grep -c '^```sh' "$S/sequence.md" && grep -c 'letsencrypt.org/certs/staging' "$S/sequence.md"; grep -n '^\*\*[0-9]\.' "$S/sequence.md" | head -9
````

Expected: `22` (6 de los requisitos: P1 dos, P6 tres, `prepare-host.sh` uno; 2 del primer arranque; 4 de «After a reboot»; 2 de la actualización; 8 de la copia fuera del host), `0`, y los nueve primeros pasos: `1. The machine, and its name`, `2. P1 — An operator account (as root, once)`, `3. P6 — The checkout`, `4. P2 to P5 — \`prepare-host.sh\``, `5. \`first-boot.sh\``, `6. In the browser: claim, your second factor, an enrollment token`, `7. \`first-boot-check.sh\``, `8. Custody: off this VPS, now`, `9. After a reboot or a restart` (medido el 2026-11-27 sobre el README que deja la Task 6, en una copia). Cada bloque de `$S/sequence.md` es un bloque del README, sin cambiar un carácter.

- [ ] **Step 3: El informe**

Al coordinador, en este orden:

1. **El camino hasta `main`.** Las ramas de las tareas se fusionan en la del proyecto en el orden de las oleadas (1, 2 y 4; la 3; la 5 y después la 6; la 7 no tiene rama); la rama del proyecto va a `main` en una PR nueva (decisión del operador del 2026-11-27, «Automatizar antes»: los guiones entran en `main` **antes** del primer despliegue del VPS nuevo), con la CI en verde, el trabajo `deploy` incluido con sus dos pasos nuevos; **la fusiona el operador**. El operador despliega siempre desde `main` (2026-09-11): la copia del VPS se clona de `main` en P6, y su `git -C .. rev-parse --abbrev-ref HEAD` tiene que imprimir `main`.
2. **Paso 0 — lo que solo puede hacer el operador**, numerado:
   1. El VPS: Ubuntu Server 24.04 LTS, 4 vCPU, 8 GB, 100 GB, una IPv4 pública en su interfaz.
   2. El registro `A`: el operador da la IPv4 al coordinador, que lo crea con el conector de Cloudflare de la cuenta de `novieri.com` (servidores `piers.ns.cloudflare.com` y `zainab.ns.cloudflare.com`): enseña el cambio, lo aplica tras el «sí» del operador, `proxied: false`, **ningún `AAAA`**, y comprueba la respuesta en un DNS público (`nslookup -type=A rmm.novieri.com 1.1.1.1` devuelve la IPv4 y `nslookup -type=AAAA rmm.novieri.com 1.1.1.1` ninguna). Antes de `first-boot.sh`, cuyo paso 1c (`preflight.sh`) se niega sin él. Ninguna credencial de Cloudflare llega al VPS ni al repositorio (diseño, cabecera).
   3. En el cortafuegos del proveedor, si lo tiene: entrada 22, 443 y 8443, nada más.
   4. Si el VPS solo trae `root`: P1 a mano (las cuatro líneas del runbook). `prepare-host.sh` no crea cuentas (resolución Q5): lanzado como `root` solo, se niega y manda a P1.
   5. P6: la clave de despliegue de solo lectura en GitHub, y la copia.
   6. Las preguntas de `first-boot.sh`: la dirección de contacto de Let's Encrypt (vale un buzón de rol); las cinco partes y el token raíz, que se enseñan una vez, a cinco custodios y al gestor de contraseñas, fuera del VPS, antes de teclear `SAVED`; tres de esas partes, ocultas.
   7. El navegador: el código de instalación, la contraseña, el segundo factor, los diez códigos de recuperación; el token de inscripción (11b); y, cuando `first-boot-check.sh` lo dice, **Run job → Collect inventory**.
   8. Desde un equipo fuera del VPS, tras `first-boot.sh`: que el `:8443` llega desde Internet (`openssl s_client -connect rmm.novieri.com:8443 -servername rmm.novieri.com </dev/null 2>/dev/null | openssl x509 -noout -issuer` imprime `issuer=CN = Novieri RMM internal CA`; en Windows, `Test-NetConnection rmm.novieri.com -Port 8443` imprime `TcpTestSucceeded : True`).
   9. La custodia, fuera del VPS (First boot, 4): las partes y el token raíz, **claves de recuperación**; **el archivo de clave del desellado**, `/etc/it-suite/openbao-unseal/unseal.key`, aparte de cualquier copia de `openbao-data` (si se pierde, OpenBao no se puede desellar); la clave HMAC de auditoría, los volúmenes `its-secrets`, `its-ca` y `openbao-data`, y `.env`.
3. **La secuencia completa del primer arranque**, el contenido de `$S/sequence.md` hasta «Después de un reinicio», copiable, en orden, sin marcadores, con cómo termina cada guion cuando va bien; y lo que `CLAUDE.md` pide que se diga aunque no haga nada: **este proyecto no añade ninguna migración**, y aun así `migrate` corre como dependencia de `api` en cada `up -d` (en un host nuevo, todas desde `0001`, en el paso 4); **las claves de `.env`**: en un host nuevo todas son nuevas (`new-env.sh`), y `openbao-seal.sh` añade `COMPOSE_FILE=docker-compose.yml:openbao/auto-unseal.yml` y `OPENBAO_UNSEAL_KEY_FILE=/etc/it-suite/openbao-unseal/unseal.key`, que no se tocan a mano; **OpenBao**: en un host nuevo no existe hasta el paso 3.1 de `first-boot.sh`, que lo arranca y le pregunta él mismo, y `first-boot-check.sh` le pregunta antes que nada. Y que relanzar un guion tras un `FAILED` es la forma de seguir: el desellado automático va justo después del paso 3.6 (resolución Q6), así que un fallo del 4 en adelante (el más probable, el certificado del paso 7 si el DNS aún no contesta con el VPS) se relanza sin teclear ninguna parte; y `first-boot-check.sh` se puede relanzar siempre (resolución Q8: cada ejecución completa inscribe y retira su propio agente de prueba, y los anteriores quedan en la consola, desconectados).
4. **Después de un reinicio y una actualización**, las dos de `$S/sequence.md`, las dos empezando por `docker compose exec openbao bao status` en un bloque propio. Con el desellado automático, `Sealed false` es lo esperado; `Sealed true` es el archivo de clave (y las partes ya no desellan). Un OpenBao sellado hace que `up -d` se quede esperando (GO-LIVE §4).
5. **La vuelta atrás, como es:** en un host nuevo no hay etiqueta anterior: `build-images.sh` lista una sola, la que corre. Hasta el paso 12 de `first-boot-check.sh` no hay vuelta atrás ninguna (empezar de nuevo es `docker compose down -v`, `rm .env` y otro primer arranque, que pide otro certificado: 5 por semana); desde el 12, restaurar esa copia (`docs/07-backup-restore.md` §3), que necesita el archivo de clave del desellado. **Y esa copia está en el mismo VPS**: si se pierde la máquina o su disco, se pierde con ella (parte 8).
6. **Lo verificado y lo no:**
   - verificado: las pruebas de cada tarea en rojo y en verde (con sus recuentos); el trabajo `deploy` de la CI entero en esta sesión con Pebble, en verde y en los tres rojos de la Task 5, Step 7 (el reinicio con una clave que no es la suya, una segunda ejecución que cambia algo, un agente que no verifica la firma); y, en GitHub, el trabajo `deploy` de la PR, que desde este proyecto prueba en cada subida las dos mitades de la invariante 1 sobre el binario compilado: el `hello` firmado se ejecuta (7c) y el que no verifica contra la clave fijada se rechaza con `bad_signature` sin ejecutarse (7c-bis, resolución Q3);
   - **sin verificar**, y el operador lo verá por primera vez en el VPS: `prepare-host.sh` de verdad (activar ufw, instalar Docker: solo contra órdenes simuladas); **los temporizadores de `systemd` del 6b (la renovación del certificado del gateway) y del 12 (la copia nocturna)**: la CI corre con `ITS_NO_SYSTEMCTL=1`, escribe las unidades y no las activa (resolución Q2), así que la primera vez que se activan es en el VPS, y el informe dice cómo mirarlos allí: `sudo systemctl list-timers its-gateway-cert.timer its-platform-backup.timer --no-pager` tras `first-boot-check.sh` lista los dos con su próxima hora; el DNS real y el registro de Cloudflare; Let's Encrypt de producción; y cualquier equipo Windows o macOS;
   - lo que cambió respecto del diseño y por qué: las resoluciones Q1 a Q8 del coordinador (al final del plan) y las cuatro correcciones del diseño que escriben las Tasks 1 (§3), 2 (§2, §3, §4), 5 (§5, §8.2) y 6 (§6).
7. **Qué cambia para el operador desde hoy**: OpenBao se desella solo tras cada reinicio; las cinco partes son claves de recuperación (para `openbao-seal.sh disable` y `bao operator generate-root`), no de desellado; el archivo de clave es lo nuevo que custodiar, aparte de `openbao-data`; y la decisión y su coste están en GO-LIVE §4 con fecha.
8. **Opcional, recomendado antes de inscribir nada real: la copia fuera del host**, los pasos de `$S/sequence.md` bajo «Opcional, recomendado», numerados y aparte de la secuencia, con el riesgo dicho claro: la copia nocturna está en el mismo VPS. ⚠ Sin ensayar en esta sesión (no hay destino).

---

## Apéndice A — Contratos que una tarea fija y otra consume

| Contrato | Lo fija | Lo consumen |
|---|---|---|
| `sudo ./scripts/prepare-host.sh`, sin opciones (P1 a mano: resolución Q5); sus líneas finales (`Docker's versions`, dos `ok`, `The host is ready for the first boot`, dos `note`) | 1 | 6 (las cita), 7 |
| `./scripts/first-boot.sh [--host] [--acme-email] [--acme-ca-server] [--tls-cafile]`; sale 0 o 1 | 2 (3 lo completa) | 5, 6, 7 |
| `  share <n>     <parte>`, `  root token  <token>`, `  install code  <código>` | 2, 3 | 5 (`first_boot_answers.py` las enmascara) |
| `Type SAVED …`, luego tres partes, por la entrada estándar | 2 | 5 (contesta `SAVED` y las partes 1, 3 y 5) |
| `  skip    step 1: .env exists (new-env.sh never overwrites one), for rmm.novieri.com` | 2 | 5 (`--require`) |
| El desellado justo tras el 3.6 (resolución Q6): `OpenBao restarts and unseals itself`, `  run     docker compose restart openbao`, `  ok      OpenBao restarted and came back unsealed by itself (Seal Type static, Sealed false): no share was typed`; el bloque `main` `step_3_6`, `auto_unseal`, `restart_proof`, `step_4` | 2 (3 pone sus pasos detrás) | 5, 6 (las cita y ata el orden) |
| `First boot: every step is done.`; en una segunda ejecución, ninguna `  run     ` y 16 `  skip    `, el desellado y su reinicio entre `step 3.6` y `step 4` | 3 | 5, 6 |
| `ITS_FIRST_BOOT_ALLOW_NONTTY=1` | 2 | 5 (lo pone el conductor, no el trabajo) |
| `ITS_ETC_DIR`, `ITS_SYSTEMD_DIR`, `ITS_NO_SYSTEMCTL=1` | 2, 3 (y ya `install-backup-timer.sh`) | 4, 5 |
| `ITS_PREFLIGHT_SKIP_DNS=1` y su línea `note … was not asked of DNS …`; su negativa con Let's Encrypt de producción o sin `ITS_ACME_CA_SERVER` (resolución Q4) | 2 | 5 (con Pebble como CA) |
| `sudo env -u COMPOSE_FILE ./scripts/openbao-seal.sh enable --key-file "$ITS_ETC_DIR/openbao-unseal/unseal.key" --yes`, tres partes por la entrada estándar; tras él, un `COMPOSE_FILE` heredado gana `:openbao/auto-unseal.yml` | 2 | 5 (hace lo mismo en `GITHUB_ENV`), 6 (el apéndice lo cuenta) |
| `./scripts/first-boot-check.sh [--tls-cafile] [--connect] [--wait-job]`; el token por la entrada estándar | 4 | 5, 6, 7 |
| `  ok      step 11c: the agent enrolled as device <uuid>, connected over :8443, and its token file is gone`; `First boot is complete.` | 4 | 5 (espera la primera; exige la segunda), 6 |
| Las dos primeras líneas de `first-boot-check.sh`, `  note    this enrols a throwaway agent on this VPS (step 11c) and retires it, with its key (11f).` y `  note    It is safe to run again: …` (resolución Q8) | 4 | 6 (lo dice en «First boot, 3») |
| El agente `smoke-agent`: `--network host`, la CA en `/etc/ssl/certs/ca-certificates.crt`, `ITS_AGENT_ENROLL_URL=https://$host`, `--add-host` con `--connect`, el token en `<datos>/enroll-token` 0600 | 4 | 5 (sus pruebas leen `run_args`) |
| `python3 ci/first_boot_answers.py [--script] [--require]… [--timeout] -- <args>` | 5 | — |
| `python3 ci/agent_flow.py … --phase token --token-file A --token-file B`, `--phase job --hostname H`, `--phase refused --hostname H --container C --pins-file P`; `mcp_flow.py` guarda tres códigos de recuperación (resolución Q3) | 5 | el paso del OAuth, 7c y 7c-bis |
| `GITHUB_ENV`: `PEBBLE_ROOT`, y `COMPOSE_FILE=docker-compose.yml:ci/acme-pebble.yml:openbao/auto-unseal.yml` tras el primer arranque | 5 | los pasos posteriores del trabajo |
| Los *here-documents* de `its-gateway-cert.service` y `.timer` en el README, sin cambiar un carácter | (ya existen) | 3 (los compara con lo que escribe el guion), 6 (los mueve) |
| Las anclas `#first-boot`, `#prepare-hostsh-p2-to-p5`, `#appendix-what-each-script-does-by-hand`, `#if-bao-status-says-sealed-true`; la estructura de «Prerequisites», «First boot» y «After a reboot» | 6 | 7 (`report_sequence.py`) |

## Autorrevisión

Contra el diseño, sección por sección, con las correcciones que resolvió el coordinador, y contra lo que un revisor llamaría defecto. Esta es la segunda versión del plan: la primera terminaba con ocho preguntas, y sus resoluciones (al final) están aplicadas en cada tarea, con su prueba en rojo y en verde donde cambian lo que hace el código.

**1. Cobertura del diseño.**
- §1, el objetivo (tres órdenes y los momentos de una persona, ninguna comprobación perdida, guiones probados por la CI): Tasks 1 a 4, 5 y 6.
- §2, el recorrido: el paso 0 lo hace el operador (Task 7, Paso 0; el registro `A` lo crea el coordinador, no el código; P1 a mano); el 1, Task 1; el 2, Tasks 2 y 3 (las cuatro paradas; el desellado y su reinicio justo después del 3.6, Task 2; el código, Task 3); el 3, el navegador, lo describe la Task 6 y en la CI lo hacen `mcp_flow.py` y `agent_flow.py`; el 4, Task 4; el 5, la custodia, Task 6 («First boot, 4») y Task 7.
- §3, comunes: repetibles (cada tramo con su `skip`; la prueba de la segunda ejecución en las Tasks 1, 2, 3 y 4, y en la CI, Task 5), los mismos comandos que el runbook (las pruebas de la Task 6 atan las órdenes del apéndice al código de los guiones, el orden del desellado incluido, y las líneas citadas a las impresas), los secretos (la prueba de la línea de órdenes y de los archivos de la Task 2, la de la Task 4 para el token, el registro enmascarado de la Task 5, la guarda de `test_deploy_runbook_secrets.py` sobre los tres guiones), la negativa sin terminal (Task 2), `set -x` (las dos negativas y las dos pruebas), cada `die` con su orden. `prepare-host.sh`: P2 a P5, `SUDO_USER`, ninguna cuenta (Q5), las versiones, el aviso de volver a entrar. `first-boot.sh`: todos los pasos nombrados en el orden corregido (Q6), la negativa por estado mezclado, la repetición después de `bao operator init`, nunca reinicializar. `first-boot-check.sh`: 10, 11a, 11c a 11f y 12, y lo que dice al empezar (Q8).
- §4: `openbao-seal.sh enable` justo después del 3.6, con las tres partes que ya tiene (Task 2); el reinicio y `Sealed false` (Task 2, y la CI, Task 5); «después de un reinicio» (Task 6); las partes como claves de recuperación (Tasks 2 y 6, GO-LIVE §4); la decisión con fecha en GO-LIVE §4 (Task 6) y en `.git/sdd/decisiones-p34.md` (ya está); sin cambio en las invariantes (Global Constraints).
- §5: las pruebas de `prepare-host.sh` con órdenes simuladas, su orden, la repetición, la negativa sin root y shellcheck (Task 1, y el paso de la CI en la 5); el trabajo `deploy` con los dos guiones, `ITS_FIRST_BOOT_ALLOW_NONTTY=1`, todas las comprobaciones de hoy y tres nuevas: el reinicio, la segunda ejecución y el rojo con la firma mala, ahora un paso permanente (Task 5, 7c-bis: Q3); las pruebas de backend de lo que la CI no alcanza (Tasks 2 a 4).
- §6: el runbook y su apéndice, las pruebas que lo fijan adaptadas sin debilitarse (Task 6, con la tabla de qué fijaba cada una y dónde lo fija ahora); GO-LIVE §4 y `docs/07-backup-restore.md` (Task 6, Q7); el informe (Task 7).
- §7: nada de lo de fuera de alcance aparece en ninguna tarea.
- §8: 1 (Tasks 1 a 4, 6); 2 (Task 5, con las tres nuevas); 3 (Tasks 2, 4 y 5); 4 (Tasks 1, 2, 3, 4 y 5); 5 (Task 6, y la Task 7 lee el runbook).

**2. Marcadores.** Cada paso de código lleva el código entero; cada orden, su salida esperada; ningún «TBD», ningún «como en la Task N». Los recuentos de los Steps 2 y 4 (`12 failed`, `22 failed, 25 passed`, `47 passed`, `13 failed, 19 passed`, `32 passed`, `17 failed`, `89 passed`, `19 failed, 61 passed`, `95 passed`, `7 failed, 18 passed`, `106 passed`, `1 failed, 10 passed`, `58 passed`, …) se midieron otra vez el 2026-11-27, tras las resoluciones, tarea a tarea sobre copias nuevas de `git archive` de `6f616aa`, en el orden de las oleadas, con este mismo código: no están calculados. **Y el plan entero se siguió al pie de la letra** sobre otro `git archive` limpio de `6f616aa`, por un guion que toma de este documento cada bloque «Crear» y «Sustituir … entero», cada «al final de» y cada guion de edición (los del diseño incluidos), en el orden de las tareas: los 22 archivos que resultan son, byte a byte, los del árbol construido aparte con los archivos que pasaron sus pruebas; los tres guiones quedan ejecutables; `ruff check app tests` y `shellcheck` no dicen nada; y 752 pruebas pasan sobre ese árbol (las de los 44 archivos que tocan `deploy/`, la CI, el runbook o sus guiones, `test_public_surface.py` incluido). Las ediciones del diseño de las Tasks 1 y 2, y las de las 5 y 6, se fusionaron con `git merge` en el orden de las oleadas sin conflicto. **Lo único no medido** son las salidas del ensayo del trabajo de la CI (Task 5, Step 7), que construye las imágenes y arranca el stack entero: sus líneas esperadas son las que los guiones y `agent_flow.py` imprimen (medidas contra el arnés y contra sus clientes falsos) y las que la CI de hoy imprime; el binario plantado del Rojo 3 sí se compiló y su suite se vio en rojo. La tarea lo dice y dice qué hacer si difieren.

**3. Coherencia de nombres.** Las funciones, variables y líneas que una tarea usa de otra están en su bloque Interfaces y en el Apéndice A, copiadas del código que pasó las pruebas; los textos que las pruebas de la Task 5 buscan en los guiones (`dc exec -T openbao bao operator init -key-shares=5 -key-threshold=3`, `run_args=(-d --name smoke-agent --network host`, `if ! sudo cat "$token_file" | docker compose run --rm --no-deps -T \`) y los que la prueba de la Task 6 busca en el bloque `main` (`step_3_6`, `auto_unseal`, `restart_proof`, `step_4`) se comprobaron contra ese código. Las dos tareas que cambiaron de contenido con Q6 cambiaron también de nombre y de mensaje de commit (Task 2, «… steps 1 to 3.6 and the auto-unseal …»; Task 3, «… steps 4 to 8, with OpenBao unsealing itself»), y su última línea de control (`Steps 1 to 3.6 and the auto-unseal: done.`) es la que busca el Step 0 de la Task 3.

**4. Revisión previa de conflictos** (lo que un revisor señalaría):
- *Dos tareas en un archivo:* `first-boot.sh` y `test_first_boot_script.py` son de la 2 y la 3, que van seguidas. **El diseño lo tocan cuatro tareas**, en párrafos distintos: la 1 y la 2 en paralelo (oleada 1), la 5 y la 6 en paralelo (oleada 3); las dos fusiones se midieron sin conflicto. Las Tasks 5 y 6 en paralelo no comparten otro archivo, pero sí leen los guiones: si el ensayo de la 5 corrige un guion, se fusiona primero y la 6 vuelve a pasar sus pruebas (Notas de integración).
- *El arnés de la Task 2 simula pasos que la 2 no tiene* (4 a 8). Es a propósito: es la interfaz del guion entero, y la 3 solo añade pruebas. El arnés de la Task 4 repite la idea de las cuñas en pequeño, para poder ir en paralelo.
- *Un interruptor que apaga una comprobación* (`ITS_PREFLIGHT_SKIP_DNS`): lo dice en una línea `note`, solo cubre la pregunta al DNS, su prueba demuestra que el resto sigue negándose, y **se niega con Let's Encrypt de producción o sin CA puesta** (Q4), con dos casos de prueba.
- *La CI escribe `.env` ella misma* (Q1: aceptado; el conductor exige la línea `skip` del paso 1) y *no activa systemd* (Q2: se mantiene; el 6b y el 12 quedan sin verificar): las dos cosas se dicen en la tarea, en el comentario del paso y en el informe.
- *El caso negativo de la invariante 1 en la CI* (Q3) necesita un segundo agente y un segundo token de inscripción: de un uso, emitido en la misma sesión que el primero, enmascarado, en un archivo 0600 que la trampa borra; el agente no publica nada, y el trabajo y su base de datos son efímeros. Su falsador (un agente que no verifica) se planta solo en la rama de ensayo; `agent/` no cambia en ninguna tarea.
- *El orden del desellado* cambió con Q6 (justo tras el 3.6): el diseño se corrige en la Task 2, y lo fijan `test_a_second_run_skips_every_step_to_the_auto_unseal` (Task 2), `test_the_auto_unseal_comes_before_step_4` y `test_a_failed_step_7_runs_again_with_no_share` (Task 3) y `test_the_first_boot_runs_the_scripts_that_check_it` (Task 6), esta con su rojo plantado.
- *`--create-user`* ya no existe (Q5): `test_it_has_no_way_to_make_the_account` lo fija, y el diseño §3 lo dice.
- *`first-boot-check.sh` relanzado inscribe otro agente* (Q8: así está bien): lo dice en sus dos primeras líneas, y el runbook, que es seguro relanzarlo.
- *Una prueba de hoy afirma algo que la medida desmiente* (`operator init -status`, M1): la Task 5 la rehace y lo dice.
- *Un archivo que el diseño no nombraba* (`docs/07-backup-restore.md`) cambia, y el diseño §6 lo nombra ya (Q7).
- *Cambios en bloques del runbook* (`--wait`, `set -C`, `daemon-reload`, `sudo journalctl`): el diseño pide que el runbook diga lo que el guion hace distinto; aquí, además, la orden del apéndice es la del guion, porque es mejor también a mano. La Task 6 los lista.
- *La salida de `first-boot.sh` en la CI* contiene las partes: el conductor las enmascara antes de imprimir cada línea y ningún paso las guarda en un archivo (`test_the_first_boot_is_answered_and_must_end_as_a_good_one_does` comprueba que no hay `tee`).
- *La huella de la segunda ejecución* no usa la hora de arranque de los contenedores de una vez, que `up -d` vuelve a arrancar (M9).

## Resoluciones del coordinador (2026-11-27)

A las ocho preguntas de la primera versión de este plan. Cada una, con dónde está aplicada.

- **Q1. La CI escribe `.env` con `new-env.sh` antes de `first-boot.sh`.** *Aceptado.* `first-boot.sh` salta el paso 1 y lo dice; el conductor exige esa línea (`--require '  skip    step 1: .env exists (new-env.sh never overwrites one), for rmm.novieri.com'`) y el verde del ensayo la enseña (Task 5).
- **Q2. Los temporizadores de systemd no se activan en la CI.** *Se mantiene `ITS_NO_SYSTEMCTL=1`.* Los del 6b y del 12 quedan sin verificar, y lo dicen la Task 5 (decisiones, comentario del paso, verde del ensayo), «Lo que hace este proyecto, y lo que no» y el informe (Task 7, parte 6).
- **Q3. El rojo con la firma mala.** *Paso permanente de la CI.* El 7c-bis del paso del OAuth, con su propio agente y la fase `refused` de `agent_flow.py`; su rojo, en las pruebas nuevas de `test_ci_agent_flow.py` y en el Rojo 3 del ensayo; el diseño §5 dice «añade», y §8.2, «las tres nuevas» (Task 5).
- **Q4. `ITS_PREFLIGHT_SKIP_DNS=1`.** *Aceptado, pero se niega con Let's Encrypt de producción o sin `ITS_ACME_CA_SERVER`.* La rama de `preflight.sh` y sus dos casos de prueba (Task 2).
- **Q5. `prepare-host.sh --create-user`.** *Se quita; P1 sigue a mano.* El guion, su prueba `test_it_has_no_way_to_make_the_account`, la nota de P1 del runbook (Task 6) y el diseño §3 (Task 1).
- **Q6. El orden del desellado.** *Justo después del 3.6, con su reinicio; un paso 7 que falla se relanza sin partes.* El desellado pasa de la Task 3 a la 2, con su prueba; la Task 3 prueba el orden y el relanzamiento sin partes; el apéndice del runbook lo pone entre el 3.6 y el 4, con su prueba y su rojo plantado (Task 6); el diseño §2, §3 y §4 (Task 2).
- **Q7. `docs/07-backup-restore.md`.** *De acuerdo.* Cambia en la Task 6, y el diseño §6 lo nombra (Task 6).
- **Q8. `first-boot-check.sh` lanzado otra vez.** *Así está bien.* El agente de cada ejecución se retira al final; las dos primeras líneas del guion dicen que inscribe y retira un agente de prueba y que es seguro relanzarlo, y el runbook lo dice (Tasks 4 y 6).

**Lo que queda por decidir: nada que bloquee.** Una sola nota: la corrección de Q3 en el diseño toca también §8.2 («más las dos nuevas» pasa a «más las tres nuevas»), porque si no el criterio contaría dos comprobaciones nuevas donde §5 cuenta tres. Si se prefiere §5 solo, se quita el segundo par de `task5_spec.py` y el `grep` del Step 5 de la Task 5 espera `1`.
