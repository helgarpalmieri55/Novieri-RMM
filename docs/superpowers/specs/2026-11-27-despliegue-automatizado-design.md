# Despliegue automatizado de `rmm.novieri.com` — diseño

**Fecha:** 2026-11-27. **Estado:** aprobado por el operador el 2026-11-27
(«Sí, avanza en lo que puedas avanzar mientras»), con estas decisiones suyas del
mismo día:

- **«Automatizar antes»**: los guiones van en una PR nueva, probada y fusionada
  en `main`, **antes** del primer despliegue del VPS nuevo.
- **Desellado automático: «Activarlo siempre»**. El primer arranque deja
  OpenBao con la clave estática (el `openbao-seal.sh` que ya existe), sin
  preguntar. Lo eligió conociendo el coste (GO-LIVE §4): quien consiga el
  archivo de clave **y** `openbao-data` tiene la clave de firma de trabajos y
  la CA.
- **DNS con Cloudflare**: `novieri.com` está en Cloudflare (servidores
  `piers.ns.cloudflare.com` y `zainab.ns.cloudflare.com`) en una cuenta que el
  conector de la sesión del 2026-11-27 no ve; el operador conecta esa cuenta y
  el coordinador crea el registro `A` el día del despliegue. **No es código**:
  ninguna credencial de Cloudflare llega al VPS ni al repositorio.

## 1. Qué problema resuelve

El primer arranque del runbook (`deploy/README.md`) son 33 números con 64
bloques `sh` que el operador pega uno a uno. Casi todos son mecánicos y la CI
(`.github/workflows/ci.yml`, trabajo `deploy`) ya los ejecuta sin persona. Lo
que de verdad necesita a una persona son unos pocos momentos: secretos que
solo ella debe ver o custodiar, y lo que se hace en el navegador.

**Objetivo:** el primer arranque queda en **tres órdenes** más esos momentos,
sin perder ninguna comprobación del runbook y con los guiones probados por la
CI en cada cambio.

## 2. El recorrido del operador

Las tres órdenes se lanzan desde `deploy/` de la copia que hace P6
(`/opt/it-suite/deploy`), como `./scripts/…` (decisión del coordinador del
2026-11-27, Task 6 del plan): es donde el runbook deja al operador tras P6, y
donde corren los bloques del apéndice.

0. **Antes** (operador): el VPS; la clave de despliegue en GitHub (P6); si el
   VPS no trae una cuenta con `sudo`, P1 a mano (cuatro líneas como `root`).
   El registro `A` lo crea el coordinador con el conector de Cloudflare cuando
   el operador le da la IPv4: enseña el cambio, lo aplica tras el «sí» del
   operador, `proxied: false`, ningún `AAAA`, y comprueba la respuesta en un DNS
   público.
1. `sudo ./scripts/prepare-host.sh` — P2 a P5. Salir y volver a entrar
   (grupo `docker`).
2. `./scripts/first-boot.sh` — pasos 1 a 7 del runbook y el desellado
   automático. Se para solo para:
   - la dirección de contacto de Let's Encrypt (la pregunta de `new-env.sh`);
   - **enseñar una vez** las cinco partes de OpenBao y el token raíz, y esperar
     a que el operador escriba que los ha guardado fuera del VPS;
   - **pedir tres partes** (entrada oculta), que desellan OpenBao y prueban
     que las copias son buenas. El token raíz lo toma de la memoria del guion:
     no se vuelve a teclear, y se escribe en `.env` como hoy (paso 3.5).
   - Justo después del paso 3.6, antes de que el paso 4 arranque nada: el
     desellado automático (`openbao-seal.sh enable`, con esas tres partes) y un
     reinicio de OpenBao que demuestra que vuelve solo. Desde ahí, un fallo se
     relanza sin teclear ninguna parte (decisión del coordinador del
     2026-11-27, pregunta Q6 del plan).
   - Al final: el código de instalación del paso 8 impreso en pantalla.
3. **Navegador** (operador): reclamar la instalación con ese código, la
   contraseña, el segundo factor y los diez códigos de recuperación; el token
   de inscripción (11b).
4. `./scripts/first-boot-check.sh` — pasos 10, 11 y 12: pide el token de
   inscripción (oculto), levanta el agente de prueba (11c), dice cuándo pulsar
   **Run job → Collect inventory** y espera el resultado (11d), verifica la
   cadena de auditoría (11e), retira el agente (11f) y hace la primera copia
   (12).
5. **Custodia** (operador), fuera del VPS: las cinco partes y el token raíz;
   la clave HMAC de auditoría, los volúmenes de clave y `.env` (Paso 0.6); y
   **el archivo de clave del desellado**, aparte de cualquier copia de
   `openbao-data`. Si se pierde el archivo, OpenBao no se puede desellar.

## 3. Los tres guiones

Los tres en `deploy/scripts/`, `bash` con `set -euo pipefail`, en el estilo de
los que ya hay (`new-env.sh`, `openbao-seal.sh`, `preflight.sh`): mensajes
`say/note/warn/die`, salida en inglés como el resto de `deploy/`.

**Comunes:**
- **Repetibles.** Cada tramo comprueba primero si ya está hecho y lo salta con
  una línea que lo dice. Un fallo a mitad se arregla y se vuelve a lanzar el
  mismo guion.
- **Los mismos comandos que el runbook.** Lo que el guion ejecuta en cada
  tramo es lo que dice el bloque del runbook de ese tramo; donde el guion
  cambia algo (leer el token de la memoria en vez de pedirlo), el runbook lo
  dice.
- **Secretos.** Ninguno en la línea de órdenes, en un registro, en un archivo
  temporal ni en el historial. Solo se escriben donde ya van hoy: `.env` (0600)
  y el archivo de clave del desellado (0400, en `/etc/it-suite/openbao-unseal/`).
  `set -x` prohibido. Las partes y el token se imprimen **solo si la salida es
  una terminal** (`[ -t 1 ]`); con la salida redirigida, el guion se niega antes
  de `bao operator init`, salvo en la CI (§5), que lo pide explícitamente.
- **Errores.** Cada `die` dice qué falló y la orden para seguir.
- **Desde `deploy/`.** Los tres se lanzan desde `deploy/` de la copia, como
  `./scripts/…` (§2), y cada uno hace `cd` a `deploy/` por su propia ruta. Lo
  que imprimen para seguir: `prepare-host.sh` se nombra por su ruta absoluta
  (`sudo /opt/it-suite/deploy/scripts/prepare-host.sh`), que vale desde
  cualquier carpeta; cada bloque `FAILED` de `first-boot.sh` empieza con un
  `cd` absoluto a `deploy/`; y `first-boot-check.sh` da
  `./scripts/first-boot-check.sh`, desde `deploy/`.

**`prepare-host.sh`** (con `sudo`, desde la cuenta de operador y desde
`deploy/`): P2 cortafuegos, P3 actualizaciones, P4 reloj (activa NTP si hace
falta), P5 Docker CE del repositorio de Docker, y el grupo `docker` para la
cuenta que lo llama
(`SUDO_USER`). No crea ninguna cuenta: P1 se hace a mano (§2, paso 0), y
lanzado como `root` sin `sudo` se niega y lo dice (decisión del coordinador del
2026-11-27, pregunta Q5 del plan). Termina comprobando las versiones mínimas
(Engine 26, Compose 2.24) y diciendo que hay que salir y volver a entrar.

**`first-boot.sh`** (la cuenta de operador, desde `deploy/`): pasos 1, 1b, 1c,
2 y 3.1 a 3.6 del runbook; el desellado automático; los pasos 4, 4b, 5, 6, 6b y
7; el código del paso 8. Se niega si `.env` ya existe y OpenBao está inicializado
con otro `.env` (estado mezclado), con la orden para empezar de cero.
**Al repetirlo después de `bao operator init`** (un fallo a mitad), el token
raíz ya no está en memoria: si `.env` todavía no tiene los tokens, los pide
con entrada oculta; y si OpenBao está sellado y aún sin desellado automático,
pide las tres partes. Desde el paso 4, OpenBao se desella solo, y un fallo se
relanza sin partes. Nunca vuelve a inicializar un OpenBao ya inicializado.

**`first-boot-check.sh`** (la cuenta de operador, desde `deploy/`): pasos 10
(las comprobaciones de API que no necesitan navegador), 11a, 11c a 11f y 12.

## 4. El desellado automático, siempre

- `first-boot.sh` llama a `openbao-seal.sh enable` justo después del paso 3.6,
  con las tres partes que ya tiene, sin volver a pedirlas, y comprueba `Sealed
  false` tras reiniciar `openbao`, antes del paso 4.
- La secuencia «después de un reinicio» del runbook pasa a: `bao status`
  (debe decir `Sealed false` solo); si dice `Sealed true`, el archivo de clave
  falta o está mal, y el runbook dice cómo seguir.
- Las cinco partes pasan a ser **claves de recuperación**: autorizan
  `openbao-seal.sh disable` y `bao operator generate-root`, no desellan. El
  runbook y GO-LIVE §4 lo dicen.
- La decisión del operador queda en GO-LIVE §4 con fecha, y en
  `.git/sdd/decisiones-p34.md` de la sesión.
- Sin cambio en las invariantes de `CLAUDE.md`: los trabajos siguen firmados y
  verificados (1); la credencial almacenada sigue cifrada en el almacén (3). Lo
  que cambia es la protección del almacén **en frío**, ya descrita en GO-LIVE §4.

## 5. Pruebas y CI

- **`prepare-host.sh`**: pruebas de backend (pytest, como
  `test_new_env_script.py`) con órdenes simuladas en el `PATH` (`apt-get`,
  `ufw`, `timedatectl`, `usermod`, `curl`, `install`…) que comprueban las
  órdenes y su orden, la repetición sin efectos y la negativa sin `root`;
  `shellcheck`. Activar ufw o instalar Docker de verdad no se prueba en la CI;
  queda como sin verificar, igual que hoy.
- **`first-boot.sh` y `first-boot-check.sh`**: el trabajo `deploy` de la CI
  deja de hacer el primer arranque con sus propias líneas y llama a los dos
  guiones contra Pebble, con las respuestas por la entrada estándar y
  `ITS_FIRST_BOOT_ALLOW_NONTTY=1` (solo la CI: permite imprimir partes y token
  sin terminal, al registro efímero del trabajo). Conserva **todas** sus
  comprobaciones de hoy (certificado por ACME, 503, agente compilado inscrito
  por el 443 y conectado por el 8443 con un `hello` firmado, rojo con la firma
  mala, cadena de auditoría, privilegios de `app_user`, copia y restauración) y
  añade: OpenBao se desella solo tras `docker compose restart openbao`; y
  `first-boot.sh` lanzado por segunda vez sale bien sin cambiar nada.
- Pruebas de backend para lo que no llega a la CI de extremo a extremo: la
  negativa sin terminal, la negativa por estado mezclado, la ausencia de
  `set -x` y de secretos en la línea de órdenes (`ps`), los mensajes de salto.

## 6. Documentación

- `deploy/README.md`: el primer arranque pasa a ser el recorrido de §2; los
  bloques de hoy se mueven a un apéndice «Lo que hace cada guion, a mano», para
  depurar o hacerlo sin guion. Las pruebas que fijan el runbook
  (`test_runbook_first_boot.py` y las demás) se adaptan sin debilitarse.
- `deploy/GO-LIVE.md` §4: desellado automático por defecto, con la fecha de la
  decisión y la custodia del archivo de clave.
- `docs/07-backup-restore.md`: la fila 5 de §1, §2.3, el paso 5 de §3 y §5, que
  desellaban con partes; con el desellado automático las partes ya no desellan
  (decisión del coordinador del 2026-11-27, pregunta Q7 del plan).
- El informe de despliegue se rehace sobre el runbook nuevo (mismo
  `report_sequence.py`, adaptado si hace falta).

## 7. Fuera de alcance

- Un KMS externo para el desellado (no lo hay en este VPS).
- Credenciales de Cloudflare en el VPS o en la CI.
- Automatizar el navegador (reclamar, segundo factor) o la custodia.
- Cambios en la aplicación, en las migraciones o en el protocolo del agente.

## 8. Criterios de aceptación

1. Un primer arranque de un VPS nuevo son `prepare-host.sh`,
   `first-boot.sh` y `first-boot-check.sh`, más los momentos humanos de §2.
2. El trabajo `deploy` de la CI usa esos dos guiones y pasa, con todas sus
   comprobaciones de hoy más las dos nuevas.
3. Ningún secreto en registros, archivos temporales, línea de órdenes ni
   historial; solo donde ya iban.
4. Lanzar cualquier guion dos veces no rompe nada.
5. Runbook, GO-LIVE e informe dicen lo mismo que los guiones.
