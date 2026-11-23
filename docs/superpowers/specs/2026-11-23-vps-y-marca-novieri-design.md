# Despliegue en un VPS nuevo y cambio de marca a Novieri — diseño

**Estado:** aprobado por el operador (Helgar Palmieri, Novieri) el 2026-11-23, como
diseño conjunto de los proyectos 3 y 4 ("Aprobado tal cual").

Es el **proyecto 4 de 4** del plan acordado el 2026-10-23
(`2026-10-23-msp-clientes-design.md`): despliegue en un VPS y cambio de marca a
Novieri. El proyecto 3 (rediseño de la consola) tiene su propio diseño,
`2026-11-23-rediseno-novieri-design.md`, y comparte con este la decisión del
nombre del producto.

**Decisiones del operador (2026-11-23):**

| # | Pregunta | Respuesta |
|---|---|---|
| B1 | ¿Dónde se despliega? | **Un VPS nuevo, desde cero** (Ubuntu 24.04 y Docker CE). El servidor viejo, `itms.toeshee.com`, se apaga cuando el nuevo funcione. No hay datos que migrar (proyecto 1, P7). |
| B2 | ¿Qué nombre de host? | **`rmm.novieri.com`**. |
| B3 | ¿Cómo se obtiene el certificado HTTPS? | **Let's Encrypt automático**: Traefik lo pide y lo renueva con el reto TLS-ALPN-01 en el puerto 443, sin abrir el 80. El 8443 de los agentes sigue con la CA interna. |
| B4 | ¿Hasta dónde se renombra? | **Lo visible y lo de los equipos**: consola, correos, el servicio del agente, sus rutas, el paquete y el MSI, y el CN de la CA interna. **No** cambian el prefijo `ITS_`, las imágenes, las bases, el módulo Go ni el protocolo con el agente. |
| A3 | ¿Cómo se llama el producto? (compartida con el proyecto 3) | **Novieri RMM**: «Novieri» en la barra de la consola y «Novieri RMM» en títulos, correos y equipos. |

Las propuso el coordinador y el operador las aprobó con el diseño:

- **Las dos líneas en paralelo.** Este proyecto toca `backend/` (textos y
  nombres), `agent/`, `deploy/` y la CI. El proyecto 3 toca `frontend/`. Casi no
  comparten archivos, así que se construyen a la vez y llegan a `main` en una
  sola PR.
- **La prueba del agente compilado.** Cierra el hueco que viene desde la fase 1
  de la autenticación propia: nada probaba que el agente Go compilado se inscribe
  contra el gateway real y ejecuta un trabajo firmado.
- **Los pendientes pequeños** del programa se cierran de paso (§6).

---

## 1. Qué se renombra y qué no

La regla es la de B4: cambia **todo lo que una persona ve**, en la consola, en un
correo o en el equipo de un cliente. **No cambia nada que sea un contrato o un
dato guardado**. El protocolo con el agente es un contrato con los agentes
desplegados (`CLAUDE.md`, «The wire protocol is still a contract»). Un
identificador interno no lo ve nadie, y cambiarlo solo añade riesgo.

### 1.1 Lo que cambia

| Dónde | Hoy | Después |
|---|---|---|
| Nombre del producto en el backend (`app/core/auth/notices.py` `PRODUCT`: emisor TOTP, `rp_name` de WebAuthn) | `Novieri` | `Novieri RMM` |
| Asunto de los avisos (`delivery.py`, unos 45, con su `msgid` en `app/core/i18n/es.py`) | `[Toeshee] …` | `[Novieri RMM] …` |
| Nombre del servidor MCP (`app/mcp/server.py`) | `itms` | `novieri-rmm` |
| CN de la CA interna (`deploy/openbao/init.sh`, `app/core/ca.py`) | `it-suite internal CA` | `Novieri RMM internal CA` |
| Organización por defecto (`.env.example` `ITS_DEFAULT_ORG_SLUG`) | `toeshee` | `novieri` |
| Binario y servicio del agente (systemd, servicio de Windows; también `its-agent-enroll`, `its-agent-uninstall`, `its-agent-update.service` y los artefactos `its-agent-<os>-<arch>`) | `its-agent` | `novieri-agent` |
| Rutas del agente en Linux y macOS | `/opt/toeshee/agent`, `/opt/toeshee/enroll` | `/opt/novieri/agent`, `/opt/novieri/enroll` |
| Carpetas del agente en Windows | `%ProgramFiles%\ITSuite`, `%ProgramData%\ITSuite` | `%ProgramFiles%\NovieriRMM`, `%ProgramData%\NovieriRMM`. Van sin espacio, para no abrir el riesgo de rutas de servicio sin comillas (CWE-428). El nombre que se ve en Windows es «Novieri RMM Agent». |
| Etiqueta launchd (macOS) | `com.toeshee.itsagent` | `com.novieri.rmm.agent` |
| Paquete `.deb` y producto MSI | `it-suite` / `ITSuite` | `novieri-agent` / «Novieri RMM Agent», con un UpgradeCode nuevo |
| Prefijo del instalador que el agente reconoce | `ITMS-Agent-Setup_` | `NovieriRMM-Agent-Setup_` |
| Nombre de host en documentos, ejemplos y fixtures de despliegue | `itms.toeshee.com` | `rmm.novieri.com` |
| «ITMS» en textos visibles del backend, del agente y de `deploy/` | `ITMS` | `Novieri RMM` |

**El prefijo del instalador y el servicio cambian a la vez en los dos lados**:
el control plane que publica el artefacto y el agente que lo reconoce. Esto no
es un cambio de protocolo con agentes en el campo, porque no hay flota que
conservar (B1, P7). El MSI estrena UpgradeCode porque es un producto nuevo. Un
MSI viejo de `ITSuite` no se actualiza a este, y no hay ninguno que actualizar.

### 1.2 Lo que no cambia, y por qué

| Identificador | Por qué se queda |
|---|---|
| Prefijo `ITS_` del `.env`, `IT_SUITE_VERSION` | Es interno y lo leen todos los guiones y servicios; renombrarlo no se ve y rompe cualquier `.env` existente. |
| Imágenes `itsuite/*`, base `itsuite`, proyecto y volúmenes `it-suite` | Internos; renombrar un volumen es mover datos. |
| Módulo Go `github.com/toeshee/it-suite/agent` | Interno; no aparece en ningún equipo. |
| `itms-rsp.1`, `ITMSRSP1`, cabeceras `X-ITS-Agent-*` y `X-ITMS-*`, `ITS_AGENT_*` | Son el protocolo con el agente: un contrato (`CLAUDE.md`). |
| `its-agent-release/v1`, `itms recovery key wrap v1` | Son cadenas de separación de dominio criptográfico; cambiarlas cambia lo que se firma. |
| `itms-local:` guardado en `users` | Es un dato guardado, no un texto. |

Una prueba fija la lista §1.1. Recorre el árbol y no acepta `ITMS`, `Toeshee`,
`ITSuite` ni `itms.toeshee.com` en textos visibles de `backend/app`, `agent/` y
`deploy/`. Excluye lo de §1.2 y los nombres en pruebas e historia fechada. Es la
misma técnica que `test_no_vendor_names_backend.py`.

---

## 2. HTTPS con Let's Encrypt (B3)

- **Traefik pide el certificado** de `rmm.novieri.com` con un `certResolver`
  ACME y el reto `tlsChallenge` (TLS-ALPN-01). Valida por el 443, así que el 80
  sigue cerrado.
  - El certificado se guarda en un volumen propio (`traefik-acme`, archivo
    `acme.json`, modo 600).
  - Traefik lo renueva solo, antes de que caduque.
- **Claves nuevas del `.env`:**
  - `ITS_PUBLIC_HOST`: el nombre que Traefik pide.
  - `ITS_ACME_EMAIL`: el correo de aviso de Let's Encrypt.
  - `ITS_ACME_CA_SERVER`: por defecto el servidor de producción de Let's
    Encrypt. Se cambia por el de staging para un ensayo, o por Pebble en la CI.
- **Las reglas de los routers pasan a llevar el host.** El certificado se pide
  para el dominio del router. La comprobación previa se niega a seguir si
  `ITS_PUBLIC_HOST` no coincide con el host de `ITS_PUBLIC_CONSOLE_URL`.
- **El camino de copiar `deploy/certs/` a mano desaparece.** Con él desaparece
  la conducta de servir en silencio el certificado propio de Traefik. Si ACME
  falla, la comprobación de después del arranque lo dice, en lugar de dejar un
  certificado que los navegadores rechazan.
- **El gateway de los agentes no cambia:** el 8443 usa mTLS con la CA interna,
  como siempre. Su certificado de servidor lleva `rmm.novieri.com` como CN y SAN.
- **Límites de Let's Encrypt.** La documentación lo dice y el runbook lo repite:
  hay un límite de certificados duplicados por semana. Un ensayo se hace contra
  staging (`ITS_ACME_CA_SERVER`) y no contra producción.

---

## 3. El VPS nuevo (B1)

- **Requisitos que documenta el runbook:**
  - Ubuntu 24.04 LTS, 4 vCPU, 8 GB de RAM, 100 GB de SSD y una IPv4 pública;
  - Docker CE desde el repositorio de Docker, con Engine 26 o superior y
    Compose 2.24 o superior, como ya exige el runbook;
  - un registro DNS A de `rmm.novieri.com` a esa IP, **antes** del primer
    arranque;
  - entrada por 22 (SSH), 443 (consola) y 8443 (agentes), y nada más.
- **Preparación del sistema.** Es un bloque copiable del runbook:
  - cortafuegos `ufw` con esos tres puertos;
  - actualizaciones automáticas de seguridad;
  - hora sincronizada;
  - un usuario sin root para operar;
  - la instalación de Docker CE.
- **`deploy/scripts/new-env.sh` genera el `.env`** a partir de `.env.example`:
  - pone el host, las URLs y el correo de ACME;
  - pone un secreto aleatorio nuevo en cada clave `change-me`;
  - lo deja con modo 600 y no imprime ningún secreto;
  - se niega a sobrescribir un `.env` que ya existe.

  Así el primer arranque no tiene marcadores que sustituir (`CLAUDE.md`,
  «A copy-pasteable command contains no placeholders»).
- **`preflight.sh` comprueba además:**
  - que `rmm.novieri.com` resuelve a una IP de esta máquina;
  - que el 443 y el 8443 están libres;
  - las versiones de Docker y Compose;
  - que `ITS_PUBLIC_HOST` coincide con la URL de la consola.
- **El primer arranque del runbook se reescribe para este caso.** Cada paso
  tiene su comprobación.
  - Los pasos que la fase 4 de la autenticación propia dejó sin ejecutar en
    vivo (3.5, 8, 9, 11c y 11d) se ejecutan aquí, en el ensayo local (§4).
  - Los ejemplos `curl` con `Bearer $TOKEN`, que no funcionan desde la fase 1,
    se sustituyen por comandos que sí funcionan: el de dentro del contenedor
    `api` o la consola.
- **OpenBao.**
  - En el primer arranque se inicializa y se abre, y el runbook dice cómo
    custodiar las partes de la clave y el token raíz.
  - Toda secuencia posterior empieza por `docker compose exec openbao bao
    status`: actualización, redespliegue y reinicio del host (`CLAUDE.md`,
    «Reporting to the operator»).

---

## 4. Cómo se verifica

- **En esta sesión, con Docker.** El demonio de Docker funciona en el
  contenedor de la sesión desde el 2026-11-23. El primer arranque completo del
  runbook se ejecuta aquí, paso a paso, contra un `rmm.novieri.com` que resuelve
  localmente. Let's Encrypt se sustituye por **Pebble**, el servidor ACME de
  pruebas de Let's Encrypt, con su servidor de retos para el DNS.
- **En la CI, el job `deploy`** pasa a obtener su certificado de Pebble por
  ACME, en lugar del autofirmado de `deploy/certs/`. Así se prueba en cada
  subida el mismo camino que usa producción.
- **El agente compilado, de extremo a extremo, en la CI.** El job compila el
  agente de Linux y crea un token de inscripción. El agente se inscribe por el
  8443 contra el gateway del stack de compose y ejecuta un trabajo firmado. El
  job comprueba el resultado y que la cadena de auditoría verifica.
- **Lo que no se puede verificar desde aquí** y el informe lo dice así:
  - el certificado real de Let's Encrypt;
  - el DNS real;
  - el VPS real;
  - la app de Entra y el SMTP del operador.

---

## 5. Pruebas

- La prueba de nombres de §1.
- `test_deploy_script_steps.py` y sus vecinas siguen fijando `deploy.sh`,
  `preflight.sh` y el nuevo `new-env.sh`, con un `docker` simulado donde haga
  falta.
- Los pasos nuevos de la CI (ACME con Pebble y el agente compilado) son
  comprobaciones que fallan: cada una se ve en rojo antes de verse en verde.
- El job `migrations` y el resto de la CI no cambian.

---

## 6. Pendientes que se cierran

| Pendiente | Qué se hace |
|---|---|
| `tests/agent_update_support.unique_version` usa `% 100000` | `% 10**12`, sigue siendo semver estricto |
| Tests que fijan Redis en la base 9 y la vacían (`test_gateway_mtls`, `test_dispatch`, `test_worker` y otros) | Leen `ITS_TEST_REDIS_URL`, como ya hace `test_e2e_kernel` |
| Aviso de pytest-asyncio por el alcance del bucle sin fijar | Se fija en la configuración de pytest |
| `HTTP_422_UNPROCESSABLE_ENTITY` obsoleto en Starlette | `HTTP_422_UNPROCESSABLE_CONTENT` |
| Mientras `api` reinicia, la SPA responde en las rutas de la API | Traefik responde 503 en `/api` y `/mcp` cuando `api` no está, en lugar de pasar a `web` |
| El reuso de un código o token sobre un permiso de IA ya terminado no se audita | Se audita como los demás finales que la persona no causó |
| El tiempo de vida del código de correo empieza antes del envío SMTP | Empieza cuando el envío termina |
| Una `app_session` puede quedar con el alcance vacío | Se rechaza al crearla |
| El presupuesto fijo de 15 s de la prueba del agente Go | Pasa a ser una espera por condición |

---

## 7. El informe final

Cierra el programa de cuatro proyectos y da:

- el camino hasta `main`, que fusiona el operador;
- **la secuencia completa del primer arranque en `rmm.novieri.com`**, copiable,
  en orden y sin marcadores, desde el VPS recién creado hasta la consola con su
  primer técnico, y con la comprobación de cada paso;
- la secuencia de después de un reinicio y la de una actualización, las dos
  empezando por `docker compose exec openbao bao status`;
- lo que solo puede hacer el operador: contratar el VPS, crear el registro DNS,
  custodiar las claves de OpenBao, registrar la app de Entra, configurar el
  SMTP y apagar `itms.toeshee.com`;
- la verdad sobre la vuelta atrás: en un host recién creado no hay etiqueta
  anterior, así que el único camino de vuelta es restaurar una copia
  (`CLAUDE.md`, «A rollback target has to already exist to be one»).

## 8. Fuera de alcance

- Migrar datos o agentes desde `itms.toeshee.com` (P7: se empieza desde cero).
- Renombrar lo interno o el protocolo (§1.2).
- Automatizar la contratación del VPS o el DNS.
