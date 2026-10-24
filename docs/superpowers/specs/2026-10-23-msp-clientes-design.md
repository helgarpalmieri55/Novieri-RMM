# Clientes (modelo MSP) — diseño

**Estado:** aprobado por el operador (Helgar Palmieri, Novieri) el 2026-10-23:
primero sección por sección (partes 1 a 4) y después este documento escrito
como un todo, incluidos el paso 0 (§0) y la enmienda 5a a `CLAUDE.md` (§2.7).

Es el **proyecto 1 de 4** del plan acordado el 2026-10-23:

1. Modelo multi-cliente (este documento).
2. Autenticación propia, eliminando Keycloak.
3. Rediseño de la interfaz al estilo Atera, con la skill impeccable.
4. Despliegue en un VPS y cambio de marca a Novieri.

El modelo de clientes va primero porque la autenticación (proyecto 2) tiene que
responder "¿a qué clientes puede entrar este técnico?", y esa respuesta la
define este documento.

**Decisiones del operador (2026-10-23):**

| # | Pregunta | Respuesta |
|---|---|---|
| P1 | ¿Cómo se modelan los clientes? | **A** — una sola organización (Novieri) y "Cliente" como entidad dentro, al estilo Atera. Si algún día se vende a otros MSP, cada MSP es una organización. |
| P2 | ¿Quién inicia sesión? | Técnicos de Novieri en la consola y usuarios finales de cada cliente en el portal. La co-administración (TI del cliente en la consola) queda para después, sin cerrarle la puerta. |
| P3 | ¿Qué módulos se quedan? | Por cliente, global u oculto según la tabla de §1.4. Presupuesto, Continuidad (BCP) y Gestión de cambios pasan a ser **opcionales por cliente**, activos para Novieri. Imaging queda oculto. |
| P4 | ¿Qué hay dentro de un cliente? | **Cliente → Sedes → Equipos.** Cada cliente nace con la sede "Principal". |
| P5 | ¿Cómo se define a qué clientes entra un técnico? | **Asignaciones por técnico:** rol + "todos los clientes" o un cliente concreto, varias por técnico. |
| P6 | Escala del primer año | Hasta 10 clientes, menos de 200 equipos, 1 a 3 técnicos. |
| P7 | ¿Hay datos que conservar? | No. Se empieza desde cero. |
| P8 | ¿Cómo se garantiza la separación entre clientes? | **Opción 1:** en la base de datos (RLS), en todas las tablas que pertenecen a un cliente. |

---

## 0. Antes de construir: el repositorio y la CI

**Hoy ninguna prueba se ejecuta en GitHub.** Los workflows están en
`RMS-main/.github/workflows/`, y GitHub Actions solo lee `.github/workflows/`
en la raíz del repositorio. Por eso el PR #1 no mostró ninguna comprobación.

**Decisión:** se mueve el contenido de `RMS-main/` a la raíz del repositorio con
`git mv`, en un commit propio que no cambia ningún contenido, solo rutas. Va en
el mismo PR que este documento, que solo contiene documentación. La carpeta
`UI/`, con las capturas y el recorrido de Atera, pasa a
`docs/design/referencias-atera/`. El plan está en
`docs/superpowers/plans/2026-10-23-msp-clientes-paso0-raiz.md`.

Mover es mejor que reescribir las rutas de los workflows: `CLAUDE.md`,
`deploy/README.md`, los scripts de `deploy/scripts/` y los propios workflows
asumen que la raíz del repositorio es la raíz del proyecto. Dejar `RMS-main/` como
subcarpeta obliga a corregir esa suposición en cada sitio, y alguno se olvidaría.

**Criterio de terminado del paso 0:** el PR del movimiento muestra los cuatro
jobs de `ci.yml` ejecutándose en GitHub. Si alguno falla, el fallo se documenta
en ese PR **antes** de empezar §1: el punto de partida tiene que ser conocido.

---

## 1. Entidades

### 1.1 Cliente — tabla `clients`

| Columna | Tipo | Notas |
|---|---|---|
| `id` | UUID v7 | |
| `org_id` | FK `organizations` | RLS por organización, como el resto |
| `name` | texto, único por organización | |
| `kind` | `customer` \| `internal` \| `unassigned` | Ver abajo |
| `status` | `active` \| `archived` | No se borran clientes, solo se archivan |
| `email_domains` | lista de texto | Dominios para asignar tickets entrantes (§3.3). Un dominio no puede estar en dos clientes de la misma organización. |
| `phone`, `address`, `website`, `notes` | texto, opcionales | Datos de contacto |
| `enabled_modules` | lista de texto | Módulos opcionales activos: subconjunto de `budget`, `bcp`, `change` |
| `created_at`, `updated_at` | | |

**Dos clientes de sistema se crean al reclamar la instalación** (en el mismo
flujo que hoy crea la organización, `backend/app/api/v1/setup.py:392`):

- **Novieri (interno)**, `kind = internal`: la TI propia de Novieri. Se crea con
  `enabled_modules = [budget, bcp, change]`. El nombre se toma del nombre de la
  organización que se escribe al reclamar.
- **Sin asignar**, `kind = unassigned`: donde caen los tickets de remitentes
  desconocidos (§3.3). No puede tener sedes ni equipos. Solo lo ven los técnicos
  con alcance "todos los clientes" (§2.2).

Solo puede existir un cliente `internal` y uno `unassigned` por organización
(índice único parcial). Ninguno de los dos se puede archivar.

### 1.2 Sede — tabla `sites`

| Columna | Tipo | Notas |
|---|---|---|
| `id`, `org_id` | | |
| `client_id` | FK `clients`, obligatorio | |
| `name` | texto, único por cliente | |
| `address`, `description` | texto, opcionales | |
| `egress_networks` | lista de CIDR | IP públicas de salida de esa oficina. Las usa el enlace de instalación (§3.2). |
| `doc_record_id` | FK `doc_records`, opcional | Lo que hoy tiene `IpamSite` |
| `created_at`, `updated_at` | | |

Cada cliente `customer` o `internal` se crea con una sede llamada **Principal**.
Una sede con equipos no se puede eliminar (409).

**`ipam_site` desaparece y se fusiona en `sites`**
(`backend/app/modules/ipam/models.py:118`). Las referencias a `ipam_site_id`
(`ipam_vlan`, `ipam_subnet`, `site_server`) pasan a `site_id`. Como no hay datos
que conservar (P7), se hace con una migración que borra la tabla vieja y sus
columnas y crea las nuevas, sin copiar filas.

### 1.3 Contacto

Un contacto es una persona de un cliente que usa el portal. Se modela como un
**usuario** (`users`) con dos columnas nuevas:

- `kind`: `staff` (técnico de Novieri) o `contact` (persona de un cliente).
- `client_id`: obligatorio si `kind = contact`, nulo si `kind = staff`. Lo
  garantiza una restricción `CHECK`.

**Un contacto nunca entra a la consola.** La dependencia que admite peticiones
de la consola (`auth_context`, `backend/app/api/deps.py:75`) rechaza a cualquier
usuario con `kind = contact` con 403, antes de mirar sus permisos. El portal
(`backend/app/modules/portal/principal.py`) solo admite `kind = contact` o a un
técnico con permiso de portal, como hoy.

**Cómo se autentica un contacto no es parte de este proyecto**: lo define el
proyecto 2. Aquí los contactos se crean, editan y desactivan como datos, desde la
ficha del cliente, pero todavía no pueden iniciar sesión.

### 1.4 Qué pasa con cada módulo

| Grupo | Módulos |
|---|---|
| **Por cliente** | Equipos, Alertas, Parches, Software (despliegues y ejecuciones de scripts), Respaldos, Sesiones remotas, Tickets y SLA, Documentación de TI, Bóveda de credenciales, Licencias, Suscripciones, Direcciones IP, Site servers, Registros de seguridad, Personas, Reportes |
| **Opcional por cliente** (activo para Novieri) | Presupuesto de TI, Pruebas de continuidad (BCP), Gestión de cambios |
| **Global de Novieri** | Técnicos y roles, Configuración, Notificaciones, Correo entrante, Paquetes del agente, Asistente e IA, Auditoría, Estado de la plataforma, Monitor de errores, Historial de trabajos (filtrado por cliente, §2) |
| **Oculto** (el código se queda, sale del menú) | Imaging (`provisioning`): depende de PXE, que no está construido |

"Por cliente" se refiere a los **datos**. Las bibliotecas reutilizables
(scripts, paquetes, perfiles de salud, catálogo de parches) son globales, y lo
que pertenece a un cliente es su ejecución o asignación. El Anexo A lo clasifica
tabla por tabla.

### 1.5 Cambios en entidades existentes

- **`devices`:** `client_id` y `site_id` obligatorios. La sede tiene que ser del
  mismo cliente (trigger). `client_id` **no se puede cambiar** (trigger que
  rechaza el `UPDATE`); `site_id` sí, dentro del mismo cliente.
- **`device_groups`:** `client_id` obligatorio. Un grupo organiza equipos de un
  solo cliente y su padre tiene que ser del mismo cliente.
- **`install_links`:** la restricción `uq_install_links_org`
  (`backend/app/models/core.py:700`) se reemplaza por una por sede. Se añaden
  `client_id` y `site_id` obligatorios, y las redes permitidas pasan a ser las
  `egress_networks` de la sede, en lugar de una lista propia del enlace.
- **`enrollment_tokens`:** `client_id` y `site_id` obligatorios.
- **`user_roles`:** la columna `device_group_id` se reemplaza por `client_id`
  (§2.1).

---

## 2. Control de acceso

### 2.1 Asignaciones

Una fila de `user_roles` es una asignación: **técnico + rol + alcance**, donde el
alcance es `client_id`. `NULL` significa "todos los clientes", incluidos los que
se creen después. Un técnico puede tener varias asignaciones.

La duplicación se controla igual que hoy, con dos índices únicos parciales: uno
sobre `(user_id, role_id)` cuando `client_id IS NULL`, y otro sobre
`(user_id, role_id, client_id)` cuando no lo es. El comentario de
`backend/app/models/core.py:99` explica por qué hacen falta los dos.

**Los grupos de equipos dejan de dar permisos.** Solo sirven para organizar
equipos y aplicar políticas.

**Permisos sobre un registro.** `user_permission_codes`
(`backend/app/core/rbac.py:413`) cambia el parámetro `device_group_id` por
`client_id`: cuentan las asignaciones con `client_id IS NULL` y las del mismo
cliente. Toda comprobación de permiso sobre un registro usa el cliente de ese
registro. Por ejemplo, ejecutar un script en un equipo requiere el permiso en el
cliente del equipo.

**Permisos nuevos:** `clients.read`, `clients.manage` y `sites.manage`.
Crear y archivar clientes exige `clients.manage` **y** alcance "todos los
clientes". Editar un cliente o gestionar sus sedes y contactos exige el permiso
en ese cliente.

### 2.2 Alcance de la sesión en la base de datos

Al abrir cada transacción se fijan dos variables de sesión con `SET LOCAL`, igual
que hoy se fija `app.current_org` (`backend/app/core/tenancy.py:20`):

- `app.client_scope`: `all` o `list`.
- `app.current_clients`: lista de UUID separados por comas. Solo se usa con
  `list`.

**Cómo se calcula para un técnico:**
- Si alguna de sus asignaciones tiene `client_id IS NULL`, el alcance es `all`.
- Si no, el alcance es `list` con los clientes de sus asignaciones.
- El cliente `unassigned` solo es visible con `all`: una asignación no puede
  apuntar a él (422 al guardarla).

**Resolver quién es el técnico.** `auth_context` busca la fila del usuario y
sus asignaciones con `ClientScope.all()`, porque el alcance todavía no se
conoce; justamente se calcula a partir de esas filas. Por eso `deps.py` está en
la lista revisada del §2.3. El resto de la petición usa el alcance calculado.

**El selector de cliente estrecha el alcance (§3.1):**
- La consola envía la cabecera `X-Client-Id`, y el alcance pasa a ser
  `list = [ese cliente] ∩ clientes permitidos`.
- Si el cliente pedido no está permitido, la intersección es vacía y la sesión
  ve cero filas. Nunca se ignora la cabecera ni se amplía el alcance.
- Si el valor de la cabecera no es un UUID, la respuesta es 400.

**Reglas RLS.** Se añaden a las existentes, que siguen exigiendo
`org_id = app.current_org`:

```sql
-- Predicado de visibilidad por cliente: CLIENT_VISIBLE(client_id)
current_setting('app.client_scope', true) = 'all'
OR client_id = ANY (string_to_array(current_setting('app.current_clients', true), ',')::uuid[])
```

- **Tablas C y H** (Anexo A): `USING` y `WITH CHECK` exigen
  `CLIENT_VISIBLE(client_id)`.
- **Tablas O** (Anexo A):
  - Lectura: `client_id IS NULL OR CLIENT_VISIBLE(client_id)`.
  - Escritura (`WITH CHECK`): `client_id IS NULL` requiere `client_scope = 'all'`;
    si no es nulo, `CLIENT_VISIBLE(client_id)`. Un técnico limitado puede ver la
    configuración global, pero no cambiarla.

**Si falta una variable, falla cerrado.** `current_setting(..., true)` devuelve
NULL, el predicado es NULL y ninguna fila coincide. Una sesión sin alcance ve
cero filas y no puede escribir, igual que hoy pasa sin `app.current_org`
(`backend/migrations/versions/0002_rls.py`).

### 2.3 Nadie puede olvidar el alcance

- `org_session` (`backend/app/core/tenancy.py:20`) pasa a exigir un argumento
  de palabra clave `clients: ClientScope` **sin valor por defecto**.
  - `ClientScope` es un tipo inmutable con dos constructores: `ClientScope.all()`
    y `ClientScope.only(ids)`.
  - mypy en modo estricto marca cada una de las 625 llamadas actuales hasta que
    se decida su alcance.
- `AuthContext` (`backend/app/api/deps.py:42`) gana el campo `clients`, ya
  estrechado por `X-Client-Id`. Las rutas pasan `clients=ctx.clients`.
- Los procesos internos (worker, gateway, beat, replicador de respaldos) pasan
  `ClientScope.all()` de forma explícita.
  - Una prueba lista los archivos que usan `ClientScope.all()`, y cualquier uso
    nuevo fuera de esa lista falla, para que nadie lo añada sin revisión.
  - Los archivos de rutas HTTP (`api.py`, `api/v1/*`) no pueden usarlo, salvo la
    creación y el archivado de clientes, que ya exigen alcance `all` (§2.1).
    `app/api/deps.py` y `app/modules/portal/principal.py` sí están en la lista,
    solo para resolver la identidad (§2.2).
- `portal_session` (`backend/app/core/tenancy.py:44`) fija además
  `client_scope = list` con el `client_id` del contacto.
- `admin_session` no cambia: se salta RLS y ya exige escribir un evento de
  auditoría.

### 2.4 Tablas que heredan el cliente

Las tablas H toman `client_id` de su padre mediante un trigger
`BEFORE INSERT OR UPDATE OF <fk>`. Por ejemplo, `health_alert` lo toma de
`devices` a través de `device_id`.

- La función es **SECURITY INVOKER**: lee el padre con la RLS de la sesión. Si el
  padre no es visible, el resultado es NULL y el `NOT NULL` de `client_id` hace
  fallar la escritura, así que nunca se escribe una fila en un cliente ajeno.
- Si la aplicación envía un `client_id` distinto del del padre, el trigger
  lanza un error.
- `jobs` usa `target_type` y `target_id` (`backend/app/models/core.py:575`): toma
  el cliente del equipo o del grupo destino.

**Los tickets sí pueden cambiar de cliente**: es la salida normal de "Sin
asignar".
- Un trigger `AFTER UPDATE OF client_id` en `ticket` propaga el cambio a sus
  tablas hijas en la misma transacción: comentarios, adjuntos, aprobaciones,
  historial de estados y SLA.
- Si el ticket tiene equipo, su cliente tiene que ser el del equipo, al crearlo
  y al moverlo. Lo comprueba un trigger.
- El cambio exige el permiso de editar tickets en los dos clientes, el de origen
  y el de destino, y se audita.

### 2.5 Lo que queda global

`organizations`, `roles`, `permissions`, `role_permissions`, `user_roles` (es la
fuente del alcance, así que solo se lee con `admin_session` o la ve quien tiene
`users.read`), configuración, notificaciones, paquetes del agente, IA, auditoría
y estado de la plataforma.

`audit_events` gana un `client_id` **nulo e informativo**, sin regla RLS por
cliente. Sirve para filtrar el registro, que solo ve quien tiene `audit.read`.
Ese permiso se asigna solo a roles de administración.

### 2.6 Canal en vivo de la consola

Hoy el WebSocket de la consola se suscribe a un canal Redis por organización
(`backend/app/api/v1/ws.py:130`, `ui_channel(org_id)`).

- El canal pasa a ser **por cliente**: `ui_channel(org_id, client_id)`.
- El gateway y el worker publican en el canal del cliente del equipo o del
  trabajo.
- El WebSocket se suscribe a los canales de los clientes de su alcance, ya
  estrechado por el cliente elegido en el selector, que viaja como parámetro de
  la conexión.
- Con alcance `all` se suscribe con patrón a todos los canales de la
  organización.

El túnel de control remoto (`backend/app/modules/remote/tunnel.py`) ya valida la
sesión remota contra la base de datos, así que hereda la RLS sin cambios propios.

### 2.7 Enmienda a `CLAUDE.md`

Se añade al invariante 5, sin cambiar su texto actual:

> **5a. Dentro de una organización, todo dato de un cliente está limitado a los
> clientes del alcance de la sesión, y eso lo impone la base de datos mediante
> RLS, no solo el código.** Un técnico limitado a ciertos clientes no puede leer
> ni escribir filas de otro. Una sesión sin alcance ve cero filas. Solo los
> procesos internos usan el alcance "todos", y cada uso está en una lista
> revisada.

---

## 3. API y consola

### 3.1 Selector de cliente

- En la barra superior (`frontend/src/shell/AppShell.tsx`) se muestra un selector
  con "Todos mis clientes" y la lista de clientes permitidos, sin los archivados.
- La elección se guarda en `localStorage` y se manda en cada petición como
  `X-Client-Id` (`frontend/src/lib/ApiContext.tsx`).
- Cambiar de cliente invalida la caché de TanStack Query, para que ninguna
  pantalla siga mostrando datos del cliente anterior.
- Si el cliente guardado ya no está permitido o fue archivado, se vuelve a
  "Todos mis clientes".

### 3.2 API nueva

Todas las rutas van bajo `/api/v1` y llevan `require_permission(...)`, como
exige la prueba de completitud de RBAC.

| Método y ruta | Permiso | Notas |
|---|---|---|
| `GET /clients` | `clients.read` | Clientes del alcance. `?include_archived=true` |
| `POST /clients` | `clients.manage` + alcance `all` | Crea la sede "Principal" en la misma transacción |
| `GET /clients/{id}` | `clients.read` | |
| `PATCH /clients/{id}` | `clients.manage` en ese cliente | Nombre, contacto, dominios, módulos opcionales |
| `POST /clients/{id}/archive` | `clients.manage` + alcance `all` | 409 si tiene equipos activos. 409 si es `internal` o `unassigned` |
| `GET/POST /clients/{id}/sites` | `clients.read` / `sites.manage` | |
| `PATCH/DELETE /sites/{id}` | `sites.manage` | `DELETE` da 409 si la sede tiene equipos |
| `GET/POST /clients/{id}/contacts` | `clients.read` / `clients.manage` | Crea usuarios `kind = contact` |
| `PATCH /contacts/{id}` | `clients.manage` | Editar y desactivar |

**Cambios en rutas existentes:**
- **Inscripción** (`backend/app/api/v1/enrollment.py`): crear un token exige
  `client_id` y `site_id`. El enlace de instalación se gestiona por sede:
  `/sites/{id}/install-link`.
- **Usuarios** (`backend/app/modules/user_admin/api.py`): las asignaciones se
  envían como `[{role_id, client_id | null}]`.
- **Tickets** (`backend/app/modules/service_desk/api.py`): `client_id` es
  obligatorio al crear un ticket. Se añade `POST /tickets/{id}/move` para cambiar
  de cliente (§2.4).
- **Módulos opcionales:**
  - Una dependencia de FastAPI rechaza con 404 cualquier escritura en
    `budget`, `bcp` o `change_mgmt` para un cliente que no tiene el módulo
    activo.
  - En las lecturas no hace falta nada: ese cliente simplemente no tiene filas.

**Errores:**
- Un ID de un cliente fuera del alcance da **404, no 403**, por la convención
  actual (`backend/app/modules/ipam/api.py`, encabezado: un 403 sería un oráculo
  de existencia). Con RLS ese 404 sale solo.

### 3.3 Tickets por correo

Al procesar un correo entrante (`backend/app/modules/email_intake/`), el cliente
se decide en este orden:

1. Si el remitente es un contacto activo, va al cliente de ese contacto.
2. Si no, se busca un cliente cuyo `email_domains` contenga el dominio del
   remitente (comparación exacta, sin mayúsculas).
3. Si no hay coincidencia, va al cliente **Sin asignar**.

Como los correos se procesan en el worker con alcance `all`, los tres pasos ven
todos los clientes.

### 3.4 Consola mínima, sobre el diseño actual

Este proyecto **no rediseña nada**: usa los componentes que ya existen
(`frontend/src/shell/components.tsx`, `frontend/src/components/ui.tsx`), y el
proyecto 3 los reestiliza después.

- **Selector de cliente** en la barra superior (§3.1).
- **Página Clientes** (`/clients`), en la sección de gestión del menú:
  - Una lista con nombre, número de equipos, sedes y estado.
  - Una ficha por cliente con pestañas Resumen, Sedes, Contactos y Módulos.
- **Usuarios**: editor de asignaciones con rol + "Todos los clientes" o cliente.
- **Equipos**: columnas Cliente y Sede, y filtro por sede.
- **Agregar un equipo** (`/devices/onboard`): pide primero cliente y sede.
- **Tickets**: campo Cliente al crear y acción "Mover a otro cliente".
- **Menú**: Presupuesto, Continuidad y Cambios aparecen solo si el cliente
  elegido tiene el módulo activo. Con "Todos mis clientes" aparecen si algún
  cliente visible lo tiene. Imaging sale del menú (`frontend/src/shell/nav.ts`).
- Todo texto nuevo pasa por i18n en inglés y en español: lo exige
  `frontend/src/i18n/coverage.test.ts`.

---

## 4. Errores y casos límite

| Caso | Comportamiento |
|---|---|
| ID de un cliente fuera del alcance | 404 |
| `X-Client-Id` de un cliente no permitido | La sesión ve cero filas. La consola nunca lo ofrece |
| `X-Client-Id` que no es un UUID | 400 |
| Eliminar una sede con equipos | 409 |
| Archivar un cliente con equipos activos | 409. Primero se retiran los equipos |
| Archivar el cliente `internal` o `unassigned` | 409 |
| Mover un equipo a otro cliente | No permitido: el trigger rechaza el `UPDATE`. Se retira y se reinscribe con el enlace correcto |
| Escribir en un módulo opcional apagado para ese cliente | 404 |
| Fila hija cuyo padre no es visible | Falla por `NOT NULL` en `client_id`: nunca se escribe en un cliente ajeno |
| Dos clientes con el mismo dominio de correo | 409 al guardar |
| Inscripción desde una IP fuera de las redes de la sede | Rechazada, igual que hoy con las redes del enlace (`backend/app/services/install_link.py:293`) |

---

## 5. Pruebas

Cada punto es una prueba que tiene que fallar si se quita lo que protege. Es la
disciplina de `HANDOFF.md` §4 contra el "fixture blindness".

1. **Catálogo obligatorio.** Una prueba lee los metadatos de SQLAlchemy y exige
   que cada tabla con `org_id` esté en el catálogo de clasificación
   (`backend/app/core/client_scope_catalog.py`) como C, H, O o G. Una tabla nueva
   sin clasificar hace fallar la suite.
2. **El catálogo coincide con la base de datos.** Por cada tabla C, H u O, la
   prueba consulta `pg_policies` y exige la regla del §2.2 correspondiente.
3. **Aislamiento recorriendo todo el catálogo.** Con dos clientes X e Y y un
   técnico limitado a X, por cada tabla C y H: no lee filas de Y, no las
   modifica y no puede insertar en Y. Por cada tabla O: lee las filas globales
   pero no las modifica.
4. **Las pruebas entre clientes se hacen también con el motor de administración**,
   que se salta RLS: así se comprueba el filtro del código, que la RLS taparía
   (`HANDOFF.md` §4, "RLS masks missing application predicates"). El fixture
   tiene que hacer del cliente el **único** discriminante, para que ningún otro
   campo, como el `target_id` de un trabajo, separe los datos por casualidad.
5. **Una sesión sin alcance ve cero filas** y no puede escribir.
6. **`X-Client-Id` no puede ampliar el alcance**: con un cliente no permitido,
   la API devuelve una lista vacía y un 404 en el detalle.
7. **Herencia.** La alerta de un equipo de X queda en X. Una inserción con
   `client_id` de Y se rechaza. Mover un ticket propaga el cambio a todas sus
   tablas hijas.
8. **Inmutabilidad.** Cambiar el `client_id` de un equipo falla. Cambiar su sede
   dentro del mismo cliente funciona; cambiarla a una sede de otro cliente falla.
9. **Correo entrante.** Hay un caso para el contacto conocido, otro para el
   dominio y otro para "Sin asignar". El fixture usa remitentes distintos para
   que cada paso sea el único que puede acertar.
10. **Inscripción.** Un equipo inscrito por el enlace de una sede queda en su
    cliente y su sede. Desde una IP fuera de las redes de la sede se rechaza.
11. **`ClientScope.all()`** solo aparece en los archivos de la lista revisada
    (§2.3).
12. **WebSocket.** Un técnico limitado a X no recibe eventos de equipos de Y.
13. **Contactos.** Un usuario `contact` recibe 403 en cualquier ruta de la
    consola.
14. **Frontend:**
    - el selector manda la cabecera;
    - cambiar de cliente vacía la caché;
    - página Clientes;
    - editor de asignaciones;
    - menú según los módulos activos;
    - textos en inglés y español.

**Las pruebas existentes se ajustan, no se debilitan.** Los fixtures crean un
cliente y una sede, y las llamadas a `org_session` pasan su alcance. Ninguna
prueba se borra ni se salta para ponerse en verde: lo prohíben `CLAUDE.md` y las
reglas del repositorio.

---

## 6. Fuera de alcance

- **Eliminar Keycloak y el inicio de sesión de los contactos:** proyecto 2.
  Mientras tanto Keycloak sigue haciendo el login de los técnicos, y el modelo de
  clientes no depende de él: el alcance sale de `user_roles`, no del token.
- **El rediseño de la interfaz:** proyecto 3.
- **El despliegue en el VPS y el cambio de marca:** proyecto 4.
- **Mover equipos entre clientes.**
- **Que un contacto vea todos los tickets de su empresa:** hoy un contacto ve
  solo los suyos.
- **La marca propia en el portal de cada cliente.**
- **La co-administración,** es decir, TI del cliente en la consola.

---

## Anexo A — Clasificación de las tablas

- **C** — Cliente obligatorio. La aplicación pone `client_id`.
- **H** — Heredada. `client_id` obligatorio, lo pone un trigger desde el padre
  indicado.
- **O** — Opcional. `client_id` nulo significa "todos los clientes"; si se
  apunta a un grupo o a un equipo, se hereda de él.
- **G** — Global. Sin `client_id`.

La prueba 1 del §5 exige que esta lista y el catálogo del código coincidan. Si al
implementar una tabla resulta mal clasificada, se corrige aquí y en el catálogo
en el mismo PR, con el motivo.

| Tabla | Clase | Padre o nota |
|---|---|---|
| `clients` | G | La entidad; no lleva `client_id`, tiene política propia: `CLIENT_VISIBLE(id)` |
| `sites` | C | |
| `organizations`, `roles`, `permissions`, `role_permissions` | G | |
| `users` | O | `NULL` = técnico; con valor = contacto |
| `user_roles` | G | Su `client_id` es el alcance, no una regla RLS |
| `device_groups` | C | |
| `device_group_evaluation` | G | Estado del trabajador por organización; no tiene clave a un grupo |
| `devices` | C | Más `site_id`; `client_id` inmutable |
| `suggested_asset_match`, `device_tags`, `agents` | H | `devices` |
| `policies` | O | `device_group_id` |
| `credentials` | O | `NULL` = credencial de Novieri (SMTP, almacenamiento externo) |
| `jobs` | H | Equipo o grupo destino |
| `job_logs` | H | `jobs` |
| `audit_events` | G | `client_id` nulo e informativo (§2.5) |
| `enrollment_tokens`, `install_links` | C | Más `site_id` |
| `keycloak_event_ingest` | G | Se elimina en el proyecto 2 |
| `domain_events` | O | Lo pone quien emite el evento |
| `agent_releases`, `dashboard_layouts` | G | |
| `ai_autonomy_ceiling`, `ai_session`, `ai_message`, `ai_injection_flag`, `ai_settings` | G | Por usuario o de la organización |
| `ai_action` | H | `devices`, por `target_device_id` (obligatorio) |
| `approved_action`, `approved_action_script_exemption` | G | Biblioteca |
| `doc_articles`, `doc_records`, `doc_documents` | O | `NULL` = base de conocimiento de Novieri |
| `doc_links`, `doc_versions` | O | Se hereda de `doc_articles` / `doc_records` cuando hay padre; `NULL` si el padre es de Novieri |
| `doc_document_versions`, `doc_document_links` | O | Se hereda de `doc_documents`; `NULL` si el documento es de Novieri |
| `inventory_snapshots`, `installed_software`, `inventory_changes`, `hardware_components` | H | `devices` |
| `mcp_revocation`, `platform_error_group`, `platform_error_event`, `platform_health_sample`, `platform_probe_notice`, `org_vault_keys` | G | |
| `security_events` | H | `devices` |
| `push_authorization`, `push_run` | C | |
| `push_credential_scope`, `push_credential_realm` | H | `credentials`, que tiene que tener cliente |
| `push_host_key` | O | `devices`; su `device_id` es opcional |
| `push_batch_target` | H | `jobs` |
| `push_run_target` | H | `push_run` |
| `agent_update_rollouts` | G | Operación de la plataforma |
| `agent_update_rings` | O | `device_group_id` |
| `agent_update_targets` | H | `devices` |
| `backup_repositories` | C | |
| `backup_chunks`, `backup_chunk_copies` | H | `backup_repositories` |
| `backup_policies`, `backup_offsite_targets`, `backup_recovery_keys` | O | |
| `backup_runs`, `backup_points`, `restore_jobs` | H | `devices` |
| `backup_point_copies` | H | `backup_points` |
| `bcp_plan` | C | Módulo opcional |
| `bcp_test`, `bcp_test_step`, `bcp_finding` | H | Cadena desde `bcp_plan` |
| `budget_settings`, `cost_center`, `expense_category`, `budget`, `expense` | C | Módulo opcional |
| `budget_line` | H | `budget` |
| `fx_rate` | G | Tipos de cambio |
| `change_request` | C | Módulo opcional |
| `change_approval`, `change_link`, `change_external_ref` | H | `change_request` |
| `change_gates` | O | `device_group_id` |
| `packages`, `scripts` | G | Biblioteca |
| `deployments` | C | |
| `deployment_results`, `script_runs` | H | `devices` |
| `script_policies` | H | `device_groups` (obligatorio) |
| `email_mailbox`, `email_oauth_app`, `email_oauth_authorization` | G | Buzones de Novieri |
| `inbound_message` | O | Lo pone el correo entrante al decidir el cliente |
| `health_profile`, `health_check`, `health_org_settings`, `health_baseline_seed` | G | Biblioteca |
| `health_profile_assignment` | O | Equipo o grupo |
| `health_alert`, `device_health_check_state`, `health_flap_state`, `device_health_policy_ack` | H | `devices` |
| `health_alert_action` | H | `health_alert` |
| `ipam_site` | C (pendiente, fase 2) | Se elimina al fusionarse en `sites` (§1.2) |
| `ipam_vrf`, `ipam_vlan`, `ipam_subnet` | C | La sede de una VLAN o subred es opcional (`ipam/models.py:173,234`); si existe, tiene que ser del mismo cliente |
| `ipam_range`, `ipam_sweep_run` | H | `ipam_subnet` |
| `ipam_scan_authorization`, `ipam_address`, `ipam_observation`, `ipam_drift` | H | `ipam_vrf`: su `vrf_id` es obligatorio; el `subnet_id`, donde existe, es opcional |
| `vendor`, `software_product` | G | Catálogos compartidos |
| `license`, `license_compliance_snapshot` | C | |
| `license_allocation` | H | `license` |
| `software_ignore` | O | |
| `notification_channels`, `notification_rules`, `queue_members`, `notification_deliveries`, `webhook_allowlist_entries` | G | |
| `patches` | G | Catálogo |
| `patch_policies`, `patch_approvals` | O | |
| `device_patch_status`, `patch_reboots`, `patch_runs` | H | `devices` |
| `person` | C | |
| `person_login`, `person_checklist` | H | `person` |
| `person_checklist_step` | H | `person_checklist` |
| `device_person` | H | `devices` |
| `os_image`, `provision_profile` | G | Módulo oculto |
| `provision_run` | O | `devices`; su `device_id` es opcional |
| `remote_sessions` | H | `devices` |
| `remote_policies` | O | `device_group_id` |
| `ticket_queue`, `ticket_category`, `ticket_number_counter`, `ticket_canned_response` | G | |
| `ticket` | C | Puede cambiar de cliente (§2.4) |
| `ticket_comment`, `ticket_attachment`, `ticket_approval`, `ticket_status_history` | H | `ticket` |
| `site_server` | H | `devices`; más `site_id` |
| `sla_calendar`, `sla_calendar_window`, `sla_calendar_holiday`, `sla_org_settings` | G | |
| `sla_policy` | O | `NULL` = SLA para todos |
| `ticket_sla` | H | `ticket` |
| `subscription` | C | |
| `seat_assignment`, `subscription_event`, `subscription_alert_sent` | H | `subscription` |
