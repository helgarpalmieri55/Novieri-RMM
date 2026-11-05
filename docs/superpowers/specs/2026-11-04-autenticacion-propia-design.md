# Autenticación propia (sin Keycloak) — diseño

**Estado:** aprobado por el operador (Helgar Palmieri, Novieri): sección por sección entre el 2026-11-02 y el 2026-11-04,
y como documento completo el 2026-11-04.

Es el **proyecto 2 de 4** del plan acordado el 2026-10-23
(`docs/superpowers/specs/2026-10-23-msp-clientes-design.md`):

1. Modelo multi-cliente (fases 1–3, construido).
2. **Autenticación propia, eliminando Keycloak (este documento).**
3. Rediseño de la interfaz al estilo Atera.
4. Despliegue en un VPS y cambio de marca a Novieri.

No se despliega nada hasta terminar los cuatro (operador, 2026-10-31). Como no hay datos que conservar (P7 del
proyecto 1), no hay usuarios de Keycloak que migrar.

**Enmienda una decisión registrada.** `docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §0 eligió
«A — mantener Keycloak como componente invisible» y dejó «B — quitar Keycloak, autenticación propia» como *revisar más
adelante*. Con este documento el operador elige **B**. La lista de costes que §0 anotaba para B (hash de contraseñas,
protección contra fuerza bruta, sesiones, TOTP, servidor WebAuthn y federación) es exactamente lo que este diseño
construye; la federación se limita a Microsoft (§4).

---

## 0. Decisiones del operador

| # | Pregunta | Respuesta (fecha) |
|---|---|---|
| Q1 | ¿Cómo inician sesión los técnicos? | Contraseña + segundo factor obligatorio: llave de acceso (se ofrece primero) o app TOTP; recuperación con códigos de un solo uso y otro administrador (2026-11-02). |
| Q2 | ¿Cómo inician sesión los contactos en el portal? | Llave de acceso **o** contraseña de 15+ caracteres comprobada contra contraseñas filtradas; invitación por enlace de un solo uso; segundo factor opcional (2026-11-02). |
| Q3 | ¿«Iniciar sesión con Microsoft»? | Sí, para técnicos (tenant de Novieri) y para contactos (tenant de cada cliente) (2026-11-02). |
| Q4 | ¿Qué clientes de IA se conectan por MCP? | Cualquiera: registro dinámico abierto, como hoy; se anuncia también CIMD (2026-11-04). |
| Q5 | Si un técnico pierde su segundo factor | 10 códigos de un solo uso; otro administrador restablece sus factores; si es el único, un comando en el servidor crea un enlace de un solo uso. Nunca solo por correo (2026-11-04). |
| Enfoque | ¿Cómo mantiene la sesión la consola? | **A:** la plataforma es su propio proveedor de identidad y la consola usa una sesión por cookie; el navegador no guarda tokens (2026-11-04). |
| S2 | Sesiones | Inactividad 30 min por defecto; ajustable en Configuración (2026-11-04). |
| S2b | Recordar este equipo | 15 días sin pedir el segundo factor, con límites (§2.6) (2026-11-04). |

**Lo que el operador sabe y aceptó (S2b).** NIST SP 800-63B-4 §5.1.1 dice que *«Cookies and similar "remember my
browser" features SHALL NOT be used instead of authentication»*, salvo la reautenticación con contraseña tras
inactividad dentro del plazo máximo [S1]. En un equipo recordado la plataforma **no cumple AAL2**. Microsoft Entra
ofrece la misma función con la advertencia *«If an account or device is compromised, remembering MFA for trusted
devices can affect security»* [S62]. Los límites de §2.6 acotan ese riesgo; no lo eliminan.

---

## 1. Arquitectura

### 1.1 Las piezas

Un paquete nuevo del núcleo, `backend/app/core/auth/`, reemplaza a Keycloak:

1. **Credenciales:** contraseñas (argon2id), llaves de acceso (WebAuthn), TOTP y códigos de recuperación.
2. **Sesiones:** en el servidor (PostgreSQL); la cookie lleva solo un identificador aleatorio.
3. **Límites de intentos:** por cuenta y por factor (base de datos) y por dirección IP (Redis).
4. **Proveedores externos:** Microsoft (Entra ID), enlazado por `tid` + `oid`.
5. **Servidor OAuth 2.1 para MCP:** metadatos, registro abierto, CIMD, PKCE, consentimiento, rotación y revocación.
6. **Correo de cuentas:** invitaciones, restablecimientos y avisos, por el envío propio de la plataforma.

La consola deja de usar `oidc-client-ts`: inicia sesión con pantallas propias y la sesión viaja en una cookie que el
JavaScript no puede leer.

### 1.2 Por qué una sesión por cookie

- RFC 10017 (BCP 212, agosto de 2026) ordena los patrones *«in decreasing order of security»* y pone el cliente OAuth
  dentro del navegador en último lugar; para una app de un solo dominio dice que *«the application can rely on
  server-side cookie-based session state»* [S23].
- OWASP: *«Do not store authentication tokens … in localStorage or sessionStorage»* [S6]. Hoy
  `frontend/src/lib/auth.ts:74` guarda el usuario de OIDC (con su token) en `sessionStorage`, y el comentario de la
  línea 2 dice lo contrario.

### 1.3 Lo que se va

Los servicios `keycloak` y `keycloak-init` y su base de datos; `deploy/keycloak/` (realm, `init-clients.py`, temas);
las rutas de Traefik de Keycloak; `backend/app/modules/keycloak_events/` y la tabla `keycloak_event_ingest`;
`user_admin/kc_client.py` y `realm_config.py`; el espejo del idioma en Keycloak (`me.py`); `oidc-client-ts`.

---

## 2. Técnicos

### 2.1 Contraseña

- Mínimo **12** caracteres (NIST exige 8 cuando hay segundo factor [S1 §3.1.1.2]; se sube por ser cuentas de
  administración), máximo 128, cualquier carácter imprimible y espacio; cada punto de código cuenta como uno.
- Sin reglas de composición ni cambio periódico; se permite pegar desde un gestor [S1].
- Se compara entera contra una lista de contraseñas comunes y filtradas que viaja con la plataforma y contra palabras
  del contexto (Novieri, el correo de la persona) [S1].
- Hash: **argon2id** con los parámetros por defecto de `argon2-cffi` (64 MiB, t=3, p=4; la segunda opción recomendada
  de RFC 9106 [S11, S41]), sal de 16 bytes, y un **HMAC con un pepper** guardado en OpenBao y versionado [S1, S4].

### 2.2 Segundo factor, obligatorio

La persona no llega a la consola hasta registrar uno.

- **Llave de acceso (WebAuthn, se ofrece primero).** Verificación del usuario **requerida** (y comprobada en el
  servidor [S7, S14]); sin atestación; varias por cuenta [S14]; las sincronizadas valen (NIST las admite en AAL2,
  Apéndice B [S1]). Desafíos aleatorios de al menos 16 bytes, de un solo uso y con caducidad [S10, S14].
- **App TOTP.** RFC 6238: paso de 30 s, 6 dígitos, tolerancia de un paso; secreto de 160 bits [S12, S13]. Un código
  aceptado no se acepta otra vez: la plataforma guarda el último paso aceptado, porque `pyotp` 2.10.0 no trae esa
  protección [S43]. El secreto se guarda cifrado por OpenBao.
- **10 códigos de recuperación** de un solo uso, mostrados una vez y guardados como hash [S7].

### 2.3 Intentos fallidos

- Contados por cuenta y por factor, nunca solo por IP [S5].
- Desde el 5.º fallo, espera creciente: 1 s, duplicando hasta 15 min.
- **La contraseña lleva dos contadores** (decisión del operador del 2026-11-05, «Cookie de equipo», sobre la revisión
  final B-I2). Un intento que llega con una cookie `__Host-itms_device` que `devices.recognised` acepta para esa
  cuenta (§2.6: del equipo recordado de esa persona, sin revocar, sin caducar, recordado después del último cambio de
  contraseña y con «Recordar este equipo» activado en la organización) espera y cuenta en el contador propio de ese
  equipo (`auth_trusted_devices.password_failures`). Cualquier otro navegador espera y cuenta en el de la cuenta
  (`auth_failures`). Quien conoce una dirección pero no tiene la cookie de esa persona solo deja esperando a los
  navegadores que la plataforma no reconoce: OWASP, «Temporary account lockout after several failed attempts is too
  simple of a target for DoS attacks against legitimate users» [S65]; NIST, «Additional techniques MAY be used to
  reduce the likelihood that an attacker will lock the legitimate claimant out» [S1 §3.2.2]. La cookie de otra
  cuenta, una revocada o caducada, o cualquier cookie con el recordatorio desactivado no da contador propio. La regla
  vale donde se prueba la contraseña: al iniciar sesión y en «Cambiar contraseña» (Mi seguridad), para que quien
  mantiene la espera no retenga también el remedio. Los dos contadores comparten un único bloqueo por persona y
  factor, así que una ráfaga en paralelo no supera ningún límite.
- **Lo que no cubre.** Una persona sin ningún equipo recordado, o en una organización con el recordatorio
  desactivado, solo tiene el contador de la cuenta: quien conozca su dirección puede seguir manteniéndola en espera
  desde navegadores no reconocidos. El remedio ahí sigue siendo el borde (bloquear la IP), el enlace de
  restablecimiento (que borra ambos contadores, decisión 9 del plan) o el comando de recuperación. Y la parada de 100
  sigue al alcance de cualquiera: quien conozca la dirección puede llegar a ella desde navegadores no reconocidos en
  aproximadamente un día de intentos espaciados, si la persona no entra con su contraseña entretanto; la contraseña
  queda entonces desactivada para todos los navegadores, el recordado incluido, y la restablece el enlace de
  restablecimiento.
- **El segundo factor no lleva contador por equipo.** Solo se llega a él con una sesión pendiente, que abre la
  contraseña correcta: quien no la sabe no puede fallar el segundo factor, y quien la sabe ya ha comprometido la
  cuenta, y el remedio es cambiarla. Además, cada factor cuenta aparte (bloquear la app TOTP no bloquea la llave ni
  los códigos), y un equipo recordado no pide segundo factor al iniciar sesión.
- A los **100** fallos seguidos ese factor queda desactivado [S1 §3.2.2]. La parada es **por cuenta**: para la
  contraseña suma los dos contadores (los fallos seguidos desde el último acierto, vengan del navegador que vengan),
  y la desactivación vale para todos los navegadores y se audita una sola vez. La restablecen un administrador
  («Restablecer factores»), el enlace de recuperación del servidor y, para la contraseña, el enlace de
  restablecimiento.
- Un acierto borra los fallos previos de ese factor [S1]; para la contraseña, un acierto desde cualquier navegador
  borra los dos contadores.
- Mensaje genérico, exista o no la cuenta, y mismo trabajo de argon2 [S5, S8]. Un navegador en espera, reconocido o
  no, recibe el mismo 401 genérico antes de probar la contraseña: los dos contadores no añaden ningún oráculo. Queda
  una diferencia de tiempo de unos 10-15 ms entre una dirección conocida y una desconocida (las consultas de la
  cuenta), aceptada en la revisión final C-M2: el trabajo de argon2 es el mismo y los límites por IP la hacen débil.

### 2.4 Sesión

- **Configuración → Seguridad → «Sesiones»** (global; editable solo con el permiso en todos los clientes):
  inactividad (por defecto **30 min**), duración máxima (por defecto **12 h**), «Recordar este equipo» (activado,
  **15 días**) y duración máxima en un equipo recordado.
- Tras la inactividad se pide solo la contraseña dentro de la duración máxima (lo que NIST permite en AAL2 [S1 §2.2.3]);
  al vencer la duración máxima, inicio de sesión completo.
- **Qué cuenta como actividad** (ronda de corrección 2, revisión de la consola P1). Solo lo que hace la persona. NIST SP
  800-63B-4 §5.2 (https://pages.nist.gov/800-63-4/sp800-63b/session/): «An inactivity timeout terminates a session
  without activity from the subscriber for a specific period» y «Session activity SHALL reset the inactivity timeout».
  La consola envía `X-ITMS-Background: 1` en toda petición hecha sin que la persona haya tocado el puntero o el
  teclado en los últimos 60 s (los sondeos de cada minuto, las recargas al volver a la pestaña), y el servidor no
  renueva la sesión con ella (`sessions.resolve`, en toda puerta HTTP). La cabecera solo puede declinar una renovación,
  nunca concederla; cualquier otro cliente sigue igual. No son actividad: una pestaña abierta (el WebSocket de estado),
  ni una pantalla remota abierta, cuyo vigilante tampoco renueva la sesión (esto invierte la decisión del coordinador
  de la Tarea 6): quien trabaja en el escritorio remoto mantiene viva la sesión con su puntero y su teclado en la
  página del visor, que la consola cuenta como cualquier otra. Así, una consola desatendida, con la pestaña visible,
  llega al límite de inactividad. La gestión del tiempo de espera se impone en el servidor (OWASP [S6]: «Session
  timeout management and expiration must be enforced server-side»).
- El identificador de sesión cambia al iniciar sesión y al confirmar el segundo factor [S6].
- Desactivar a una persona corta sus sesiones, sus tokens de IA y sus equipos recordados al instante.

### 2.5 Confirmación reciente del segundo factor

Exigida en los últimos 10 minutos para: cambiar la propia contraseña o los factores; que un administrador restablezca
los factores de otro; sacar una credencial de la bóveda [S5, S7].

### 2.6 Recordar este equipo (decisión S2b)

- Una cookie aparte, `__Host-itms_device` (`HttpOnly`, `Secure`, `SameSite=Strict`), con un identificador aleatorio
  atado a la persona y guardado como hash en el servidor.
- En ese equipo no se pide el segundo factor durante los días configurados (15 por defecto).
- Aplica a cualquier cuenta con segundo factor: técnicos y contactos que lo activaron.
- **Límites:** la persona lo revoca en «Mis equipos de confianza» y un administrador también; se revoca solo al cambiar
  la contraseña, al restablecer los factores o al desactivar a la persona; las acciones de §2.5 siguen pidiendo el
  segundo factor; cada equipo recordado se audita y se avisa por correo.

### 2.7 Avisos por correo

Al cambiar la contraseña, añadir o quitar un factor, recordar un equipo y usar un código de recuperación [S7]; toda
recuperación avisa al titular [S1 §4.2.3].

### 2.8 Restablecer la contraseña por correo

Permitido: un enlace de un solo uso que caduca a los 30 min; después **sigue pidiendo el segundo factor**. El segundo
factor nunca se restablece por correo [S10]. Por eso **no se envía enlace a una cuenta sin segundo factor** (ni llave
ni app TOTP confirmada; los códigos de recuperación solos no cuentan) y un enlace enviado antes de que los factores
desaparecieran ya no restablece nada (410): tras el restablecimiento de un administrador, una invitación o un enlace
de recuperación abandonados, o el dueño del primer arranque antes de su primer inicio, el buzón solo pondría la
contraseña y registraría su propio factor (revisión final C-I1). La respuesta sigue siendo el mismo 202, al mismo
tiempo.

---

## 3. Contactos del portal

- **Invitación** desde la ficha del cliente (pestaña Contactos), por quien tenga `clients.manage` en ese cliente: enlace
  aleatorio, de un solo uso, que caduca a las 72 h [S8]. Al aceptarla, el contacto recibe en **su** cliente el rol de
  portal (`portal.ticket.read` y `portal.ticket.write`), en ningún otro.
- **Entrada:** llave de acceso o contraseña de **15+** caracteres (NIST, sin segundo factor [S1 §3.1.1.2]) con la misma
  lista de bloqueo; segundo factor opcional.
- **Olvidé mi contraseña:** enlace de un solo uso de 30 min; misma respuesta exista o no la cuenta; al cambiarla se
  cierran sus otras sesiones [S8].
- **Sesión:** valores propios en «Sesiones»: inactividad 60 min, máximo 7 días (configurable hasta 30, el máximo de NIST
  para AAL1 [S1 §2]).
- Un contacto nunca pasa la puerta de la consola; solo la del portal (`admit(contacts=True)`).
- Mismos límites de intentos y mensajes genéricos que los técnicos.

---

## 4. Entrar con Microsoft (Entra ID)

- **Una app** registrada por Novieri, multi-tenant (autoridad `organizations` [S62]). Su secreto va a OpenBao
  (invariante 3). Código de autorización con PKCE, `state` y `nonce` [S62].
- **Técnicos:** solo el tenant de Novieri (configurado en Ajustes). La plataforma pide después su propio segundo factor
  (salvo equipo recordado), porque el token de Microsoft no trae el dato que diría si hubo doble factor: la tabla de
  claims de Microsoft no incluye `amr` [S61].
- **Contactos:** cada cliente lista en su ficha sus tenants permitidos.
- **Nunca se crea una cuenta por entrar con Microsoft.** La persona tiene que estar invitada; el enlace se hace la
  primera vez desde la invitación o desde «Mi perfil» y guarda `tid` + `oid`, que Microsoft recomienda porque no cambian;
  **nunca se enlaza por correo**, porque Microsoft dice del correo *«Never use it for authorization»* [S61].
- Según la política de cada tenant, el administrador de Microsoft 365 del cliente puede tener que aprobar la app una vez.

---

## 5. Servidor OAuth para MCP y el asistente interno

### 5.1 Endpoints y metadatos

- La plataforma es su propio emisor: `/oauth/authorize`, `/oauth/token`, `/oauth/register`, `/oauth/revoke`.
- Metadatos RFC 8414 en `/.well-known/oauth-authorization-server` [S18]: `code_challenge_methods_supported: ["S256"]`
  [S17, S30]; `registration_endpoint` (RFC 7591 [S19]); `client_id_metadata_document_supported: true` y `none` en
  `token_endpoint_auth_methods_supported`, que es la condición de Claude para usar CIMD en vez de registro [S29, S33].
- El servidor MCP sigue publicando RFC 9728 [S21], apuntando a la plataforma.

### 5.2 Autorizar

- La persona tiene que haber iniciado sesión en la consola.
- Pantalla de consentimiento con el nombre del cliente de IA y **el dominio de su redirect**; aviso extra si solo hay
  redirects locales [S30, S33].
- Código de un solo uso, 60 s.
- Redirects comparados exactos, salvo el puerto en los de loopback (Claude Code usa `localhost`/`127.0.0.1` con
  cualquier puerto) [S16, S33].
- La respuesta incluye `iss` (RFC 9207), que MCP pide [S27].

### 5.3 Tokens

- **Acceso:** JWT ES256 de 10 min, audiencia la URL del servidor MCP (RFC 8707 [S22, S27]); otro token se rechaza
  [S27, S30]. `iss`, `aud`, `kid` y algoritmo fijados [S38].
- **Refresco:** opaco, guardado como hash, **rota en cada uso**; reutilizar uno ya usado revoca toda la familia;
  caduca a los 30 días sin uso [S16, S30, S33]. Un refresh inválido responde `invalid_grant` [S33].
- Revocar un cliente de IA en Ajustes corta su acceso en la siguiente llamada (1c(d)).

### 5.4 Registro abierto, con límites

- Registrarse no da acceso: sin el consentimiento de la persona no hay token.
- Límite de registros por IP.
- Un registro sin uso en 30 días caduca (RFC 7591 lo permite [S19]; Anthropic avisa de que el registro abierto crea un
  cliente en cada conexión nueva [S33]).
- MCP sigue apagado hasta que un org-admin lo enciende (1c(d)).

### 5.5 CIMD

El documento del cliente se descarga por la protección de salida existente (`app/core/egress.py`) contra SSRF; se
comprueba que `client_id` coincide exactamente con la URL y se guarda en caché según sus cabeceras [S29, S30].

### 5.6 El asistente interno

Hoy reenvía el token de Keycloak de la persona. Ahora, en cada turno, la plataforma acuña un token interno de 5 min,
audiencia `loopback`, atado a la sesión de la persona; la puerta lo admite solo en las llamadas internas. Las reglas de
1c no cambian (canal `mcp` auditado, ningún secreto en las respuestas).

---

## 6. Datos, claves y la puerta

### 6.1 Tablas nuevas (una migración, hacia adelante)

Todas con `org_id` y RLS (invariante 5) y en el catálogo de alcance por cliente; las de un contacto, dentro de su
cliente (5a): contraseñas; TOTP; llaves de acceso; códigos de recuperación; sesiones; equipos recordados; identidades
de Microsoft (`tid` + `oid`, únicas); fallos por factor; tokens de un solo uso (invitar, restablecer, recuperar: solo
hash); clientes OAuth; códigos OAuth; familias de refresh tokens. Se borra `keycloak_event_ingest`;
`users.external_subject` deja de identificar a nadie.

### 6.2 La puerta antes de saber quién es la persona

Sin organización fijada, RLS no deja leer nada, y eso es correcto. El único camino es una función `SECURITY DEFINER`
que recibe un correo y devuelve solo `(user_id, org_id, tipo, estado)`; con eso la plataforma fija la organización y lee
las credenciales con RLS normal. Es el patrón, acotado, del primer arranque (`api/v1/setup.py`, motor de
administración), y queda en la lista revisada de excepciones.

### 6.3 Claves

Tokens de MCP y del asistente firmados con ES256; clave privada en OpenBao; JWKS en `/.well-known/jwks.json` con `kid`;
la rotación conserva la clave anterior durante la vida de los tokens. El pepper, en OpenBao y versionado.

### 6.4 Cookies y CSRF

- `__Host-itms_session` y `__Host-itms_device`: `HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/`, solo un identificador
  aleatorio de 256 bits [S1 §5.1.1, S6, S36].
- Toda escritura exige una cabecera propia: el navegador hace preflight de una petición con cabecera propia (*«All
  modern browsers designate requests with custom headers as "to be preflighted"»*) y la plataforma no abre CORS [S63].
- Además se comprueban `Origin` y `Sec-Fetch-Site`; una escritura sin `Origin` o con `Origin: null` se rechaza
  (*«Missing headers and Origin: null are not evidence of a same-origin request»*) [S63]. SameSite es defensa adicional,
  no la defensa [S6, S63].

### 6.5 La puerta en cada petición

`admit()` busca la sesión por hash y comprueba inactividad, caducidad y estado de la persona; RBAC, alcance y auditoría
no cambian. El socket en vivo usa la cookie y comprueba `Origin` en el handshake; se mantiene la revisión cada 60 s.
MCP valida su JWT (`iss`, `aud`, `kid`, revocación).

---

## 7. Cuentas, correo y auditoría

- **Invitar técnicos** (Ajustes → Usuarios): correo y asignaciones de rol por cliente; enlace de un solo uso de 72 h para
  elegir contraseña, registrar el segundo factor y guardar los códigos. Reenviar o cancelar. **Cancelar quema la
  dirección para siempre** (revisión final C-M4): la cuenta queda marcada como eliminada y su fila se conserva, como la
  de toda cuenta eliminada, y una dirección que ya tiene fila (eliminada incluida) no se puede volver a invitar en esa
  organización.
- **Restablecer factores de otro técnico:** con confirmación reciente propia (§2.5), auditado, con aviso por correo.
  Después de un restablecimiento, la persona entra con su contraseña y registra un factor nuevo; el correo solo no
  basta: no se envía enlace de restablecimiento a una cuenta sin llave ni app TOTP confirmada (revisión final C-I1,
  §2.8).
  - **El residuo de la decisión 5, y cómo se cierra.** Quien tiene el buzón puede restablecer la contraseña por correo
    mientras la cuenta aún tiene su segundo factor (está permitido: el factor se sigue pidiendo). Si después un
    administrador restablece los factores, la contraseña que eligió el buzón abre el registro de un factor propio, y
    la cuenta es suya. Es la consecuencia natural: el correo de restablecimiento cierra las sesiones de la persona y
    cambia su contraseña, ella dice «no puedo entrar», y lo esperable es que un administrador restablezca sus
    factores. Los avisos de «contraseña cambiada» y «factores restablecidos» llegan al mismo buzón.
  - **Decisión del operador del 2026-11-05 («Rechazar y usar recuperación»).** «Restablecer métodos de inicio de
    sesión» (`user_admin.service.reset_factors`, el mismo servicio para la consola y para MCP) se rechaza con 409
    `reset_needs_recovery` mientras la contraseña actual de la persona se haya puesto con un enlace de
    restablecimiento enviado por correo y desde entonces no se haya verificado ningún segundo factor (una llave, una
    app TOTP confirmada o un código de recuperación, al iniciar sesión o en una confirmación reciente). Sin ventana de
    tiempo. No cubre las contraseñas de una invitación: eso es el alta, por diseño. La marca
    (`auth_passwords.mail_reset_at`) la borran cualquier segundo factor verificado después, un cambio de contraseña
    con la sesión abierta y el enlace de recuperación, que sigue siendo el camino de vuelta. La comprobación del
    restablecimiento por correo («¿tiene segundo factor?») y la del administrador («¿la puso el buzón?») toman el
    mismo bloqueo por persona (`factors.lock_person`), así que ningún orden de esas dos deja una cuenta sin
    factores con la contraseña del buzón. Y un segundo factor verificado no borra la marca en una sesión que el
    restablecimiento por correo acaba de cerrar: el paso de inicio de sesión y la confirmación reciente deshacen
    toda su transacción (401) cuando su sesión ya no está viva (revisión de la ronda 1, Important 1). Por MCP la
    respuesta no llega a ser el 409: esa ruta exige la confirmación reciente del propio administrador en la
    consola, así que MCP recibe antes un 403 `step_up_required`. Fuente: NIST SP 800-63B-4 §4.2.2.2
    (https://pages.nist.gov/800-63-4/sp800-63b/events/): en AAL2, la recuperación necesita dos códigos de
    recuperación obtenidos por métodos distintos, o un código de recuperación más la autenticación con un
    autenticador de un factor vinculado a la cuenta, o una nueva verificación de identidad. Un buzón solo no es
    ninguna de las tres.
  - **Decisión del operador del 2026-11-05 («Pedir contraseña nueva si hay marca»).** Mientras la marca esté puesta,
    el enlace de recuperación del servidor obliga a elegir una contraseña nueva antes de terminar, y la del buzón deja
    de valer. `POST /auth/recover/details` dice `password_required` (deja el enlace sin usar);
    `POST /auth/recover/accept` sin contraseña responde 409 `recovery_needs_password` y una contraseña que la regla
    rechaza, el 422 `password_rejected` de las demás rutas: en los dos casos el enlace no se gasta y la página vuelve
    a pedirla. Una contraseña aceptable se guarda con `staff_password.store` (que borra la marca) en la misma
    transacción que quita los factores, auditada `auth.password_changed` `{"via": "recover"}` y con el aviso
    `password_changed`, como el restablecimiento por correo. Los dos rechazos llegan antes de cualquier aviso, y los
    avisos son los últimos actos de la transacción, después de crear la sesión nueva: primero `password_changed`,
    después `factors_reset` (ronda 3: un aviso es correo, y el correo no se deshace). La marca se lee bajo
    `factors.lock_person` y antes de `clear_second_factors`, que la borra, así que un restablecimiento por correo que
    llega entre `details` y la pulsación se espera y se ve (409), no se lee como ausente. **Sin la marca, la decisión 10 del plan sigue en pie:** quien recupera perdió los factores, no la
    contraseña, y la contraseña se queda; una contraseña enviada igualmente se ignora. **Por qué la marca lo cambia:**
    la marca dice que la contraseña actual pudo ponerla quien tiene el buzón, y la recuperación es el camino de vuelta
    que nombra `reset_needs_recovery`. Si la conservara, quien tiene el buzón la seguiría sabiendo después de que la
    persona recupere la cuenta; la marca desaparece con el primer segundo factor que la persona verifique, así que un
    restablecimiento de factores posterior (por ejemplo, uno que pida quien tiene el buzón haciéndose pasar por la
    persona) volvería a abrir el agujero; y la persona puede no conocer siquiera su propia contraseña.
  - Un enlace de restablecimiento rechazado porque los factores ya no están se gasta (410), como cualquier enlace
    muerto: no vuelve a valer cuando la persona registra un factor nuevo (revisión final, Minor 5).
- **Comando de recuperación** (único administrador sin factores):
  `docker compose exec api python -m app.core.auth.recover --email <correo>` imprime un enlace de un solo uso de 15 min.
  No existe por HTTP; exige acceso al servidor; queda auditado. **El enlace es la cuenta:** no pide la contraseña
  actual (si la puso un enlace por correo, pide una nueva: arriba), así que quien lo abra en esos 15 minutos se queda
  con ella. Se entrega a la persona en mano, nunca por correo (Q5); el
  comando lo advierte en stderr.
- **Primer arranque:** la pantalla con el código de instalación (que ya existe) crea al dueño con contraseña y segundo
  factor.
- **Correo de cuentas** (Ajustes → Correo): por el envío propio (el código SMTP / Microsoft 365 XOAUTH2 de
  `notification/delivery.py`); la configuración sale del realm de Keycloak; la contraseña SMTP, a la bóveda
  (invariante 3). Sin correo configurado, el administrador ve el enlace de invitación **una sola vez** para entregarlo
  por otra vía.
- **Auditoría** (invariante 4): inicios de sesión correctos; bloqueo de un factor; cambios de contraseña, factores y
  equipos recordados; invitaciones, restablecimientos y recuperaciones; consentimientos y revocaciones de clientes de IA;
  cierre de sesiones por un administrador. Los fallos se cuentan aparte para no inundar la cadena: se audita el bloqueo.

---

## 8. Fases

Cada fase con su plan, su PR y su revisión:

1. **Núcleo de identidad:** §2, §6, §7, la consola sin `oidc-client-ts`, Ajustes → Sesiones y Correo.
2. **Portal de contactos y Microsoft:** §3 y §4.
3. **Servidor OAuth para MCP y token interno del asistente:** §5.
4. **Retiro de Keycloak:** servicios, base de datos, `deploy/keycloak/`, rutas de Traefik, respaldo y restauración,
   preflight, secretos `keycloak-*`, claves `.env` `KEYCLOAK_*`, `ITS_OIDC_*`, `ENTRA_*`; la prueba de despliegue de la
   CI y `ci_mcp_flow.py`, reescrita contra el servidor propio.

La 1 primero; la 2 y la 3 pueden ir en paralelo después; la 4 al final. Sin usuarios que migrar, la consola pasa
directamente a la sesión propia.

## 9. Documentos que cambian a propósito

- `docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §0: pasa a la opción B, con esta fecha.
- `docs/modules/console-auth/spec.md` §1: decía que las contraseñas nunca entran en la plataforma; ahora entran y se
  verifican aquí. El documento hará el balance: la plataforma pasa a custodiar verificadores (hash con pepper) y
  secretos TOTP (cifrados por OpenBao), y su compromiso expone esos verificadores; a cambio desaparece un segundo
  servicio con administración propia.
- `docs/03-security-model.md`: se corrige; hoy promete más de lo que existe (SAML, federación con AD, doble factor
  exigido).
- `CLAUDE.md` 1c(a) no cambia: ya está redactada sin nombrar proveedor y sigue siendo cierta con la plataforma como
  emisor.
- OpenBao: rutas nuevas para el pepper, la clave de firma, la contraseña SMTP y el secreto de Entra; `openbao-init` las
  crea.
- El despliegue lo absorbe el proyecto 4.

## 10. Falsificadores

Cada uno con una prueba que falla si se quita la protección:

1. Una sesión de una organización no lee nada de otra; una sesión de contacto no pasa la puerta de la consola.
2. El inicio de sesión responde igual, y en el mismo tiempo, exista o no la cuenta.
3. A los 100 fallos el factor queda desactivado; un código TOTP no se acepta dos veces.
4. Una llave de acceso sin verificación del usuario se rechaza. En la CI, con el autenticador virtual de Chromium
   (dominio WebAuthn del protocolo de DevTools, *«to test the WebAuthn API»*, con `setUserVerified`; marcado
   experimental) [S64].
5. Un equipo recordado deja de valer al cambiar la contraseña; la bóveda sigue pidiendo el segundo factor.
6. Reutilizar un refresh token revoca su familia; un token MCP con otra audiencia se rechaza; el token interno no vale
   en rutas públicas.
7. Una escritura sin la cabecera propia, o con `Origin: null`, se rechaza.
8. Microsoft con `tid`/`oid` desconocido se rechaza; nunca se crea cuenta ni se enlaza por correo.
9. El comando de recuperación no existe por HTTP.
10. Ninguna pantalla dice «Keycloak» (la guarda que ya existe se mantiene).

## 11. Fuera de alcance

- SAML y federación con Active Directory local.
- Otros proveedores de identidad además de Microsoft.
- Co-administración (TI del cliente en la consola), como en el proyecto 1.

---

## Fuentes

El dossier completo, con citas literales y fecha de lectura, está en
`docs/superpowers/specs/2026-11-04-autenticacion-propia-fuentes.md` (S1–S60, más la sección "Controller additions"
con S61–S62); se resume aquí. S61–S64 los verificó el coordinador directamente.

- [S1] NIST SP 800-63B-4, julio de 2025 (final). https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-63B-4.pdf
- [S4] OWASP Password Storage Cheat Sheet. https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
- [S5] OWASP Authentication Cheat Sheet. https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html
- [S6] OWASP Session Management Cheat Sheet. https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html
- [S7] OWASP Multifactor Authentication Cheat Sheet. https://cheatsheetseries.owasp.org/cheatsheets/Multifactor_Authentication_Cheat_Sheet.html
- [S8] OWASP Forgot Password Cheat Sheet. https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html
- [S10] OWASP Passkey Security Cheat Sheet. https://cheatsheetseries.owasp.org/cheatsheets/Passkey_Security_Cheat_Sheet.html
- [S11] RFC 9106, Argon2. https://www.rfc-editor.org/rfc/rfc9106.txt
- [S12] RFC 6238, TOTP. https://www.rfc-editor.org/rfc/rfc6238.txt
- [S13] RFC 4226, HOTP. https://www.rfc-editor.org/rfc/rfc4226.txt
- [S14] W3C Web Authentication Level 3, Recomendación del 25 de agosto de 2026. https://www.w3.org/TR/webauthn-3/
- [S16] RFC 9700, Best Current Practice for OAuth 2.0 Security. https://www.rfc-editor.org/rfc/rfc9700.txt
- [S17] RFC 7636, PKCE. https://www.rfc-editor.org/rfc/rfc7636.txt
- [S18] RFC 8414, Authorization Server Metadata. https://www.rfc-editor.org/rfc/rfc8414.txt
- [S19] RFC 7591, Dynamic Client Registration. https://www.rfc-editor.org/rfc/rfc7591.txt
- [S21] RFC 9728, Protected Resource Metadata. https://www.rfc-editor.org/rfc/rfc9728.txt
- [S22] RFC 8707, Resource Indicators. https://www.rfc-editor.org/rfc/rfc8707.txt
- [S23] RFC 10017, OAuth 2.0 for Browser-Based Applications (BCP 212, agosto de 2026). https://www.rfc-editor.org/rfc/rfc10017.txt
- [S27] MCP 2026-07-28, Authorization. https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- [S29] MCP 2026-07-28, Client Registration. https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration
- [S30] MCP 2026-07-28, Security Considerations. https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations
- [S33] Anthropic, Authentication for connectors. https://claude.com/docs/connectors/building/authentication
- [S36] draft-ietf-httpbis-rfc6265bis-22 (prefijo `__Host-`). https://www.ietf.org/archive/id/draft-ietf-httpbis-rfc6265bis-22.txt
- [S38] RFC 8725, JWT Best Current Practices. https://www.rfc-editor.org/rfc/rfc8725.txt
- [S41] argon2-cffi, documentación y perfiles. https://argon2-cffi.readthedocs.io/en/stable/parameters.html
- [S43] PyOTP 2.10.0 (sin protección contra reutilizar un código). https://pypi.org/project/pyotp/
- [S61] Microsoft, ID token claims reference (actualizada 2026-06-15). https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference
- [S62] Microsoft, OpenID Connect on the Microsoft identity platform (2026-10-06) y Configure Microsoft Entra MFA
  (2026-03-27, «Remember multifactor authentication»). https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc ·
  https://learn.microsoft.com/en-us/entra/identity/authentication/howto-mfa-mfasettings
- [S63] OWASP Cross-Site Request Forgery Prevention Cheat Sheet. https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html
- [S64] ChromeDevTools/devtools-protocol, `json/browser_protocol.json`, dominio WebAuthn. https://github.com/ChromeDevTools/devtools-protocol
- [S65] OWASP, Slow Down Online Guessing Attacks with Device Cookies. https://community.owasp.org/Slow_Down_Online_Guessing_Attacks_with_Device_Cookies (owasp.org/www-community/… redirige con 308). Cita comprobada contra la página el 2026-11-05.
