# Clientes MSP, fase 3: la consola — diseño

**Estado:** el operador aprobó este diseño sección por sección entre el 2026-10-30 y el 2026-10-31. Falta su
revisión del documento escrito.

Es la última fase del **proyecto 1 de 4** (`docs/superpowers/specs/2026-10-23-msp-clientes-design.md`).
- La fase 1 creó clientes y sedes y la política de RLS por cliente.
- La fase 2 (plan `docs/superpowers/plans/2026-10-26-msp-clientes-fase2-modulos.md`) llevó todas las
  familias de datos bajo esa política.
- Esta fase hace que la **consola** trabaje con clientes. Concreta para la consola los §3.1 y §3.4 y la
  prueba 14 del §5 del diseño del proyecto, y suma cuatro extras que el operador eligió.

La interfaz no se rediseña: eso es el proyecto 3. Esta fase usa los componentes que ya existen
(`frontend/src/components/ui.tsx`, `frontend/src/shell/components.tsx`).

## Decisiones del operador

| # | Pregunta | Respuesta |
|---|---|---|
| Q1 | Con «Todos mis clientes», ¿dónde se crea un registro nuevo? | **Se elige en el formulario**: un campo Cliente obligatorio. Con un cliente elegido arriba, va a ese sin preguntar. Nunca va al cliente interno sin que lo diga la persona. |
| Q2 | ¿Se abre `GET /audit` a quien tiene `audit.read` en algunos clientes (C6)? | **No.** Se queda solo para quien lo tiene en todos los clientes. `audit_events` no tiene política de cliente (es una cadena con hash, solo de añadir), y filtrar en el código rompería 5a. |
| Q3 | ¿Qué extras entran en la fase 3? | **Los cuatro**: el perfil de salud por defecto de cada cliente, que el asistente siga al selector, un código propio de supervisor remoto y los pendientes de despliegues y respaldos. |
| Q4 | ¿Cómo lleva la consola el cliente elegido hasta la API? | **Cabecera `X-Client-Id`** (§3.1 del diseño del proyecto), salvo en las rutas de configuración global. |
| Q5 | ¿Qué pasa con los puntos de un repositorio de respaldo retirado? | **Se quedan y se pueden restaurar.** El espacio sigue ocupado. |

Además, el 2026-10-31: **no se despliega nada hasta terminar los cuatro proyectos**, y el trabajo que no
choca se hace en paralelo, con varios agentes (§9).

---

## 1. La base: selector, cabecera, `/me`, caché y canal en vivo

### 1.1 Selector

- Va en la barra superior (`frontend/src/shell/AppShell.tsx`, en `.top-actions`), antes de los glifos de
  salud y plataforma.
- Ofrece **«Todos mis clientes»** y, después, los clientes de `/me` (§1.3) por orden de nombre. Los
  archivados no aparecen.
- **«Sin asignar»** solo aparece para quien tiene asignaciones en todos los clientes, igual que en
  `GET /clients`. Va al final de la lista.
- La elección se guarda en `localStorage`, en la clave `itms.client`, con el id del cliente o `all`. El
  cambio de marca del proyecto 4 la renombrará junto con `itms.sidebar.collapsed`.
- Si el cliente guardado no está en `/me.clients`, porque se archivó o porque la persona perdió el
  acceso, la consola vuelve a «Todos mis clientes» y lo avisa una vez.
- Un cambio en una pestaña llega a las demás por el evento `storage` del navegador, y esas pestañas
  hacen lo mismo que en §1.4.

### 1.2 Cabecera y rutas globales

- `requestHeaders` (`frontend/src/lib/api.ts`) es el único sitio que arma las cabeceras. Añade
  `X-Client-Id` cuando hay un cliente elegido. `lib/api.language.test.ts` ya obliga a que todo `fetch`
  pase por ahí.
- **Las rutas de configuración global nunca llevan la cabecera**: las 95 de `GLOBAL_WRITES` y las 4 de
  `GLOBAL_READS` en `backend/tests/test_global_writes.py`. Con ella, darían 403 (refinamiento 3 del §7 del
  diseño del proyecto).
- Su lista no se copia a mano en la consola. Vive en `frontend/src/lib/globalRoutes.json`, con el
  formato `[{"method": "POST", "path": "/clients/{client_id}/archive"}, …]`, rutas relativas a
  `/api/v1`.
  - Una prueba del backend falla si ese archivo no es exactamente la unión de las dos listas.
  - Una prueba de la consola comprueba que la comparación por plantilla funciona, con ruta y método.
  - Así, una ruta global nueva sin clasificar la para CI, no un 403 en producción.

### 1.3 `/me`

`GET /me` (`backend/app/api/v1/me.py`, `backend/app/core/identity/own_profile.py`) cambia de
significado. **Hoy** devuelve solo los códigos que la persona tiene en todos los clientes, así que un
técnico limitado recibe `permissions: []` y la consola le enseña solo el Panel. **Desde esta fase**
devuelve:

- **`clients`**: los clientes en los que la persona tiene algún rol, sin los archivados. Cada uno lleva
  `id`, `name`, `kind`, `enabled_modules` y `permissions`, los códigos que tiene en ese cliente. Es el
  «mapa de permisos por cliente» que la fase 2 dejó para esta fase. No exige `clients.read`: son datos de
  la propia persona.
- **`permissions`**: los códigos que tiene en **al menos un** cliente del alcance de la petición, es
  decir, de los clientes de `clients` estrechados por `X-Client-Id` si viene. Es la regla de
  `require_permission`.
- **`permissions_everywhere`**: los códigos que tiene en **todos** los clientes, la regla de
  `require_permission_on_every_client`. No depende de la cabecera.

Para un administrador con asignaciones en todos los clientes, `permissions` sale igual que hoy. La
consola sigue sin proteger nada: lo que protege es el servidor.

### 1.4 Caché

Al cambiar de cliente, la consola llama a `queryClient.clear()` (`App.tsx`) y las pantallas montadas
vuelven a pedir sus datos, `/me` incluido. Ninguna pantalla puede seguir mostrando datos del cliente
anterior.

### 1.5 Canal en vivo

- `liveUrl()` (`frontend/src/lib/ws.ts`) añade `?client=<id>` cuando hay un cliente elegido, y los
  sockets abiertos se reconectan al cambiar de cliente.
- El servidor ya lo admite desde la fase 1 (`backend/app/api/v1/ws.py`): un valor malformado cierra con
  4400, y un alcance vacío o cambiado, con 4403.
- **Un 4403 ya no entra en la espera exponencial.** La consola vuelve a pedir `/me` y ajusta el selector
  como en §1.1.

## 2. Menú y permisos visibles

- **El menú sigue a la selección.** `visibleGroups` (`frontend/src/shell/nav.ts`) usa los `permissions`
  de `/me` para la elección actual.
- **Módulos opcionales.** Presupuesto, Continuidad y Cambios necesitan, además de su permiso, el módulo
  activo: en el cliente elegido, o en al menos uno de los clientes de `/me` con «Todos mis clientes».
  `NavItem` gana un campo `module` opcional, y `visibleGroups` recibe los módulos activos.
- **Imaging sale del menú y de la búsqueda** (decisión P3 del proyecto: depende de PXE, que no está
  construido). Sus rutas y su código se quedan.
- **Pantallas de configuración global:**
  - Se muestran a quien tiene el permiso de lectura.
  - Sus acciones de escritura solo aparecen con el código en `permissions_everywhere`.
  - Afecta a las pestañas de Ajustes, `/sla`, `/notifications`, los perfiles y acciones de `/health`,
    `/agent-packages`, `/service-desk/admin`, `/deploy/packages`, `/licenses/vendors` y
    `/licenses/products`, el cambio de divisas de `/budget/admin`, `/provisioning/*`, `/email-intake`,
    `/error-monitor` y `/assistant/*`.
  - La lista exacta sale de §1.2: cada pantalla que llama a una ruta global la condiciona por
    `permissions_everywhere`.
- **Lo que no cambia.** El modo portal de los contactos sigue igual. Los componentes de requisitos
  (`useMePermissions`) leen `/me` y siguen a la selección sin más trabajo.

## 3. Crear filas en el cliente correcto

### 3.1 Una sola regla

Los 45 puntos marcados `// PHASE 3` (`internalClientId(me)`, `internalSiteId(me)`, `useIpamClient`,
`useBudgetClient`) pasan a una pieza compartida, `frontend/src/lib/clientSelection.ts`. Decide así:

1. **Si el registro cuelga de algo que tiene cliente** (un equipo, un grupo, una VRF, una subred, la
   política de un grupo…), usa ese cliente y el formulario no pregunta. Es la regla que el operador
   aprobó el 2026-10-28.
2. **Si hay un cliente elegido arriba**, usa ese.
3. **Con «Todos mis clientes»**, el formulario muestra un campo **Cliente** obligatorio (Q1). Solo ofrece
   los clientes de `/me.clients` cuyo `permissions` contiene el código de esa escritura. «Sin asignar»
   nunca se ofrece (C10).

La construcción del cuerpo sigue dentro de la mutación, como hoy (`requireKnown`): nada se envía antes
de saber el cliente.

### 3.2 Sedes

Donde hace falta una sede (token de inscripción, enlace e instrucción de instalación, activo creado a
mano, despliegue gestionado del agente), el formulario muestra un campo **Sede** con las sedes no
archivadas del cliente (`GET /clients/{id}/sites`). Si hay una sola, viene ya elegida. La sede
«Principal» del cliente interno deja de ser la única que se puede usar.

### 3.3 Selectores dentro del cliente de la fila

Los selectores de repositorios de respaldo, credenciales de push, sedes, VLAN, VRF y los elementos de BCP
y cambios ofrecen solo lo del cliente de la fila. Cuando la API admite una fila global, la ofrecen
también, con la etiqueta «Todos los clientes». Las VRF muestran `{vrf} · {cliente}`.

### 3.4 Columna Cliente

- Con «Todos mis clientes», toda lista que mezcla clientes muestra una columna **Cliente**, la misma en
  todos los módulos.
- El nombre sale de `/me.clients`. Un id que no está ahí, por ejemplo el de una fila global, se muestra
  como «Todos los clientes».
- `useClientName` deja de necesitar `clients.read`.
- Con un cliente elegido, la columna se oculta.

### 3.5 Lo que se queda

- La casilla «Para todos los clientes» (`components/forEveryClient.tsx`) de documentos y aprobaciones de
  parche sigue igual.
- La vista previa de SLA (`sla/PolicyResolverPanel.tsx`) deja de pasar `null` y usa el cliente elegido,
  con la regla D2 de la fase 2.

### 3.6 Para que no vuelva

Una prueba de la consola falla si queda algún `PHASE 3` o alguna llamada a `internalClientId` o
`internalSiteId` en `frontend/src`. `lib/internalClient.ts` se borra, y `requireKnown` y
`loadingUntilClient` pasan a `clientSelection.ts`.

## 4. Pantallas

### 4.1 Página Clientes (`/clients`)

Va en el área de administración del menú, con `clients.read`. Usa las rutas de la fase 1
(`backend/app/api/v1/clients.py`), que ya dan `device_count` y `site_count`.

- **Lista:** nombre, tipo, equipos, sedes y estado, con la opción de ver también los archivados.
- **«Nuevo cliente»:** solo con `clients.manage` en todos los clientes (ruta global). Pide nombre, dominios
  de correo, teléfono, dirección, web, notas y módulos.
- **Ficha `/clients/{id}`, con cuatro pestañas:**
  - **Resumen:** los datos y su edición, con `clients.manage` en ese cliente. Muestra también el perfil de
    salud por defecto (§5.1). **Archivar** exige el permiso en todos los clientes; el 409 por equipos
    activos se explica en la pantalla, y no se ofrece para `internal` ni `unassigned`.
  - **Sedes:** lista, alta, edición y archivo, con `sites.manage`. Cada sede tiene su enlace de instalación
    (`/sites/{id}/install-link`). El 409 por equipos se explica.
  - **Contactos:** lista, alta, edición y desactivación (`GET/POST /clients/{id}/contacts`,
    `PATCH /contacts/{id}`).
  - **Módulos:** activar y desactivar Presupuesto, Continuidad y Cambios (`PATCH /clients/{id}`,
    `enabled_modules`).
- Los textos que hoy remiten «a la API o MCP» para contactos (`i18n/*/people.ts`) y sedes
  (`i18n/*/ipam.ts`) enlazan a esta página.

### 4.2 Usuarios

El editor de asignaciones (rol + «Todos los clientes» o un cliente, `settings/UsersTab.tsx`) existe desde
la fase 1 y su formato no cambia. Se añade una cosa: los permisos efectivos de cada persona
(`GET /users/{id}/effective-permissions`, que ya devuelve `client_id`) se agrupan por cliente.

### 4.3 Equipos

- **Lista** (`devices/DevicesPage.tsx`): columnas Cliente (§3.4) y Sede, y un filtro por sede.
  `GET /devices` gana el parámetro `site_id`.
- **Ficha:** se puede cambiar la sede de un equipo **dentro de su cliente** (`PATCH /devices/{id}`, que ya
  acepta `site_id`). Mover un equipo a otro cliente sigue fuera de alcance (§6 del diseño del proyecto).
- El comentario obsoleto de `DevicesPage.tsx:15-26` sobre la columna Sede se corrige.

### 4.4 Agregar un equipo (`/devices/onboard`)

Lo primero que pide es cliente y sede. Con un cliente elegido arriba solo pide la sede, y con una sola
sede no pide nada. El recorrido y las páginas a las que lleva usan ese cliente y esa sede: la red y la
autorización de barrido de IPAM, el token de inscripción, el enlace y la instrucción de instalación, y el
despliegue gestionado. El cliente y la sede viajan en la URL (`?client=…&site=…`).

### 4.5 Tickets

- Crear un ticket sin equipo, con «Todos mis clientes», pide el cliente (§3.1).
- La acción «Mover a otro cliente» ya existe desde la fase 2 (`TicketDetailPage.tsx`).
- Las listas de tickets muestran la columna Cliente (§3.4).

### 4.6 Auditoría

Sin cambios de permiso (Q2). Se corrige el comentario obsoleto de `frontend/src/modules/audit/api.ts:3-6`,
que habla de `require_permission("audit.read")`.

## 5. Extras

### 5.1 Perfil de salud por defecto de cada cliente

- `AssignmentCreate` (`backend/app/modules/health/schema.py`) gana `client_id`. Una asignación nombra un
  equipo, un grupo, un cliente o nada. «Nada» es el defecto global, que sigue exigiendo el alcance de
  todos los clientes.
- Una asignación a un cliente exige `health.admin` **en ese cliente** (comprobación sobre el registro).
  Rechaza «Sin asignar» y los clientes archivados (422 y 409), y su clave es
  `uq_health_assignment_client_org`.
- `AssignmentOut` gana `client_id`, y `ResolutionLevel` gana `client`. La resolución ya lo contempla:
  equipo → grupo → cliente → organización (`health/resolution.py`).
- En la consola, la asignación de perfiles ofrece «Por defecto del cliente», y la ficha del cliente
  muestra cuál tiene.

### 5.2 El asistente sigue al selector

- Las llamadas internas del asistente para técnicos (`backend/app/core/ai/executor_staff.py`) reenvían el
  `X-Client-Id` de la petición que inició el turno, igual que `backend/app/mcp/invoke.py`.
- `TurnContext` gana el cliente pedido. Lo rellenan las rutas de `ai_copilot` y `ai_admin`, incluida la
  sonda de visibilidad de `act.py`.
- El portal de los contactos (`executor_portal.py`) no cambia.
- La prueba que exige una sola cabecera (`tests/test_ai_admin_executor.py`) pasa a exigir exactamente
  `{authorization}`, o `{authorization, x-client-id}` cuando la petición venía estrechada.
- Nunca amplía: la ruta interna vuelve a pasar por la misma puerta con el mismo token.

### 5.3 Código de supervisor remoto

- Nuevo código **`remote.supervise`**: cortar la sesión remota de otro técnico, o unirse a ella, en el
  cliente de la sesión.
- `owner_or_admin` (`backend/app/modules/remote/tunnel.py`) pasa a pedir `remote.supervise` con
  `client_id` de la sesión, y `orgs.manage` deja de dar ese poder.
- **Migración 0244.** Crea el código y lo concede a **todo rol que hoy tenga `orgs.manage`**, global
  (super-admin, org-admin) o propio de una organización. Así nadie pierde el poder sin enterarse. Su
  `downgrade` borra el código y sus concesiones.
- `rbac.PERMISSIONS`, `tests/test_rbac.py` y los fixtures de la consola lo incluyen.
- **Se corrige un defecto:** al cortar la sesión de otro, `remote.session.ended` se audita con quien la
  corta como autor, y con `client_id` de la sesión. Hoy figura el dueño.
- Las especificaciones `docs/modules/remote-control/spec.md` y `spec-screen-share.md` se enmiendan.

### 5.4 Despliegues: ejecutar uno que no se ve es un 404

`POST /deploy/deployments/{id}/execute` responde **404 «no such deployment»** cuando el despliegue no
existe o es de un cliente que no se ve, igual que el GET. Hoy responde 200 `{"job_ids": []}`. La prueba
que fijaba el 200 (`tests/test_client_scope_deployments.py:312`) se corrige, y la descripción MCP se
actualiza.

### 5.5 Respaldos: cliente en la auditoría y retirar un repositorio

**Auditoría con cliente.**
- Llevan `client_id` del registro que ya tienen a mano:
  - `backup.restore_dispatched`, `restore_result` y `test_restore_dispatched`;
  - `backup.verify`, `replicated` y `replication_failed` (este último con una lectura más);
  - `backup.run_reconciled` y `restore_reconciled`.
- `backup.read_refused` y `dedup_refused` lo llevan cuando el trabajo se resolvió. Si no, quedan sin
  cliente, porque no hay de dónde sacarlo.
- `backup.offsite_gc` sigue sin cliente: es un resumen de toda la organización, y queda documentado así.

**Retirar un repositorio** (Q5):
- `POST /backup/repositories/{id}/retire`, con `backup.policy.manage` en el cliente del repositorio.
- Da **409** mientras alguna política activa lo nombre.
- Pone `state = 'retired'` (la columna existe desde 0014, sin restricción `CHECK`) y audita
  `backup.repository_retired` con su cliente.
- Un repositorio retirado:
  - no acepta respaldos nuevos: el despacho lo rechaza y lo audita como `backup.dispatch_refused`, motivo
    `repository_retired`, la forma que ya usa ese despacho;
  - no recibe permisos de deduplicación (`dedup.py` ya rechaza todo lo que no es `active`);
  - **sí** recibe permisos de lectura para restaurar: `read.py` pasa a aceptar `active` y `retired`;
  - sigue con la verificación, la limpieza y la copia externa de sus puntos.
- No hay vuelta a `active` en esta fase.
- En la consola, la página de repositorios muestra el estado y un botón «Retirar», y el editor de
  políticas ya no ofrece repositorios retirados.

## 6. Errores y casos límite

- **Cliente guardado que ya no corresponde**, o archivado: «Todos mis clientes» y un aviso (§1.1). El
  servidor nunca amplía: con un cliente ajeno da listas vacías y 404 (prueba 6 del diseño del proyecto).
- **Acceso retirado con la sesión abierta:** el socket cierra con 4403 en 60 s como mucho, y la consola
  vuelve a pedir `/me` y ajusta el selector (§1.5).
- **Pantallas globales:** nunca llevan la cabecera, así que el selector no les causa un 403.
- **Campo Cliente sin opciones**, porque la persona no tiene el permiso en ningún cliente: el botón de
  crear no aparece. `permissions` ya lo dice.
- **Archivar un cliente o una sede con equipos:** el 409 de la API se explica en la pantalla, con el
  número de equipos.

## 7. Pruebas

Cada una tiene que fallar si se quita lo que protege.

**Servidor**
- `/me` sin cabecera y con ella: `clients` con los permisos de cada cliente, `permissions` como unión de
  la elección, `permissions_everywhere` como intersección. Un técnico limitado deja de recibir `[]`.
- `frontend/src/lib/globalRoutes.json` es exactamente `GLOBAL_WRITES ∪ GLOBAL_READS`.
- Salud: un técnico con `health.admin` en X pone el defecto de X y no el de Y (404). «Sin asignar» da 422.
  La resolución usa el defecto del cliente antes que el global.
- Asistente: reenvía la cabecera cuando la petición venía estrechada, y la ruta interna no ve otro
  cliente.
- `remote.supervise`:
  - `orgs.manage` solo ya no corta ni se une;
  - `remote.supervise` en X corta las sesiones de X y no las de Y;
  - la auditoría lleva como autor a quien corta;
  - 0244 conserva el poder de quien ya lo tenía, roles propios incluidos, y su `downgrade` funciona.
- Ejecutar un despliegue invisible o inexistente da 404 y no crea trabajos.
- Las auditorías de respaldo de §5.5 llevan cliente.
- Retirar un repositorio: 409 con una política activa; retirado, rechaza el despacho y la deduplicación
  y admite restaurar.

**Consola** (prueba 14 del diseño del proyecto, ampliada)
- El selector manda `X-Client-Id`, salvo en las rutas de `globalRoutes.json`.
- Cambiar de cliente vacía la caché y reconecta el socket con `?client=`. Un 4403 recarga `/me`.
- El menú sigue a `permissions` y a los módulos. Imaging no aparece.
- Con «Todos mis clientes», el campo Cliente es obligatorio y solo ofrece clientes con el permiso.
- Los selectores solo ofrecen elementos del cliente de la fila.
- Página Clientes: lista, ficha y sus cuatro pestañas, con sus 409.
- No queda ningún `PHASE 3` ni ningún `internalClientId` o `internalSiteId`.
- Todos los textos están en inglés y en español (`i18n/coverage.test.ts`).

Ninguna prueba se borra, se salta ni se debilita. Las que fijan el comportamiento viejo que esta fase
cambia a propósito se corrigen, y el informe lo dice: el 200 vacío de `execute`, la cabecera única del
asistente y el cliente interno de los formularios.

## 8. Migración y despliegue

- **Una migración: 0244** (`remote.supervise`). Todo lo demás usa columnas que ya existen: el `state` de
  los repositorios y el `client_id` de las asignaciones de salud.
- No hay claves nuevas en `.env`.
- No se despliega hasta terminar los cuatro proyectos (decisión del 2026-10-31). El informe de
  despliegue de esta fase se suma al de las anteriores cuando llegue ese momento.

## 9. Orden de trabajo y paralelismo

El plan ordena el trabajo en estas pistas. Lo que no choca va en paralelo, cada agente en su copia aislada
del repositorio (worktree), y el coordinador lo fusiona en la rama de trabajo.

1. **Servidor, base:** `/me` (§1.3) y `globalRoutes.json` (§1.2). Las pistas 2 y 3 dependen de esto.
2. **Servidor, extras, en paralelo entre sí:** salud (§5.1), asistente (§5.2), supervisor y 0244 (§5.3), y
   despliegues y respaldos (§5.4, §5.5). Tocan módulos distintos. Los archivos compartidos son
   `app/core/i18n/es.py`, las descripciones MCP, `test_global_writes.py` y `rbac.py`, y el coordinador
   resuelve sus choques al fusionar.
3. **Consola, base:** selector, cabecera, caché, socket y menú (§1, §2). Va en serie, porque comparte
   `lib/api.ts`, `App.tsx` y el shell.
4. **Consola, filas:** `clientSelection.ts` y los 45 puntos (§3), en serie o repartidos por carpetas de
   módulo una vez exista la pieza compartida.
5. **Consola, pantallas:** Clientes, equipos, alta y tickets (§4), y las partes de consola de los extras.
6. **Revisión final** de toda la rama y una ronda de arreglos.

Mientras se construye, el operador y el coordinador hacen el diseño del proyecto 2 (autenticación
propia). El proyecto 3 (rediseño) se construye después de esta fase, porque toca las mismas pantallas.

## 10. Fuera de alcance

- Abrir el registro de auditoría a quien no tiene el permiso en todos los clientes (Q2).
- Mover equipos entre clientes y la co-administración (§6 del diseño del proyecto).
- Volver a poner en `active` un repositorio retirado, y borrar los puntos de un repositorio (Q5).
- El rediseño visual (proyecto 3), la autenticación propia (proyecto 2), el VPS y la marca (proyecto 4).
