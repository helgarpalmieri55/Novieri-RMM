# Clientes MSP — Fase 3: la consola. Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** La consola trabaja con clientes: un selector que estrecha cada petición con `X-Client-Id`, `/me` con los permisos por cliente, altas en el cliente correcto, una página Clientes y los cuatro extras que eligió el operador.

**Architecture:** El servidor ya limita todo por cliente con RLS (fases 1 y 2). Esta fase lleva a la consola el cliente elegido y la información de qué puede hacer la persona en cada uno:
- una cabecera, salvo en las rutas globales, cuya lista se genera desde las pruebas del backend;
- un `/me` con el mapa por cliente;
- una pieza compartida que decide el cliente de cada fila nueva.

Los extras son cambios pequeños del backend, cada uno en su módulo. Solo uno añade migración: la 0244.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, PostgreSQL 16 (RLS), pytest. React + TypeScript estricto, Vite, TanStack Query y vitest.

**Diseño:** `docs/superpowers/specs/2026-10-31-msp-clientes-fase3-consola-design.md` (aprobado por el operador el 2026-10-31). Cada tarea cita la sección que implementa.

## Global Constraints

- **Seguridad.** Las invariantes 1, 1b, 1c, 2, 2a, 3, 4, 5, 5a y 6 de `CLAUDE.md` no se tocan.
  - Ninguna tarea cambia los parámetros firmados de un trabajo, el formato del sobre ni lo que ejecuta un agente.
  - La consola no protege nada: toda comprobación de permiso vive en el servidor.
- **Migraciones.**
  - Solo la **0244** (`backend/migrations/versions/0244_remote_supervise.py`), y solo hacia adelante.
  - 0230 a 0243 están en `main` y no se editan nunca.
  - Su `downgrade()` tiene que funcionar: la CI corre `upgrade head → downgrade base → upgrade head`.
- **Alcance.**
  - `ClientScope.all()` en `app/` solo en `ALL_SCOPE_ALLOWLIST` (`app/core/client_scope.py`).
  - Un registro de otro cliente es un 404, nunca un 403.
  - Las comprobaciones de permiso sobre un registro pasan `client_id=<cliente del registro>`.
  - Toda `audit_service.record(...)` y todo `events.publish(...)` cuyo sujeto tiene cliente pasan `client_id=`.
- **Rutas globales.** Las de `GLOBAL_WRITES` y `GLOBAL_READS` (`backend/tests/test_global_writes.py`) nunca llevan `X-Client-Id` desde la consola. Su lista para la consola vive en `frontend/src/lib/globalRoutes.json` y se genera de esas dos (Task 2).
- **`/me`** (diseño §1.3) devuelve:
  - `clients: [{id, name, kind, enabled_modules, permissions}]`: clientes con algún rol, sin archivados;
  - `permissions`: códigos con al menos un cliente en el alcance de la petición;
  - `permissions_everywhere`: códigos con todos los clientes.
- **Consola.**
  - La elección se guarda en `localStorage["itms.client"]`, con el id del cliente o `all`.
  - La cabecera la añade solo `requestHeaders` (`frontend/src/lib/api.ts`).
  - Al cambiar de cliente: `queryClient.clear()`.
  - El socket abre con `?client=<id>` cuando hay un cliente elegido.
- **Cliente de una fila nueva** (diseño §3.1): (1) el de su padre si lo tiene; (2) el elegido arriba; (3) con «Todos mis clientes», un campo **Cliente** obligatorio con los clientes de `/me.clients` cuyo `permissions` tiene el código de esa escritura. «Sin asignar» nunca se ofrece.
- **Textos.**
  - Backend: todo texto nuevo pasa por `tr()` con su traducción en `app/core/i18n/es.py`.
  - Consola: por `t()`, con su clave en `frontend/src/i18n/en/` y `frontend/src/i18n/es/`. Lo comprueban `i18n/coverage.test.ts`, `catalogue.identical.test.ts` y el test de marcadores de posición.
  - Las acciones de auditoría nuevas tienen su frase en `frontend/src/i18n/{en,es}/auditVocabulary.ts`.
- **MCP.** Una ruta nueva necesita su descripción en `backend/app/mcp/descriptions/` (`tests/test_mcp_descriptions.py`).
- **Pruebas.**
  - Ninguna prueba existente se borra, se salta ni se debilita.
  - Las que fijan un comportamiento que el diseño cambia a propósito se corrigen, y el informe lo dice: el 200 vacío de `execute`, la cabecera única del asistente y el cliente interno de los formularios.
- **Verificación, desde `backend/`** (con `pg_ctlcluster 16 main start; redis-cli ping || redis-server --daemonize yes`):
  - `.venv/bin/pytest -q <archivo>`;
  - `.venv/bin/pytest -q`, la suite completa (más de 30 min), antes de dar la tarea por hecha;
  - `.venv/bin/mypy app`;
  - `.venv/bin/ruff check app tests`.
- **Verificación, desde `frontend/`:** `npx vitest run` completo (la prueba de vocabulario de auditoría lee el backend), `npx tsc -b` y `npm run build`.
- **Pistas en paralelo (worktrees)**, cuando el coordinador lo indique:
  - `cd backend && export PYTHONPATH=$PWD`, y comprobar que `.venv/bin/python -c "import app,sys; print(app.__file__)"` apunta a la worktree. El entorno es editable y apunta al árbol principal.
  - `.venv` y `node_modules` se enlazan desde el árbol principal: `ln -s /home/user/Novieri-RMM/backend/.venv backend/.venv` y `ln -s /home/user/Novieri-RMM/frontend/node_modules frontend/node_modules`.
  - `export ITS_TEST_REDIS_URL=redis://localhost:6379/<n>` con el número de base que asigne el coordinador. Varias pruebas vacían su base.

## Oleadas y paralelismo (diseño §9)

| Oleada | Tareas | En paralelo | Depende de |
|---|---|---|---|
| 1 | 1, 2, 3, 4, 5, 6 | sí, todas, cada una en su worktree | — |
| 2 | 7 → 8 → 9 | no, en serie (comparten `lib/api.ts`, `App.tsx` y el shell) | 1, 2 |
| 3 | 10, 11 | sí (carpetas de módulo distintas) | 9 |
| 4 | 12, 13, 14 | sí | 3, 6, 10, 11 |

Choques que el coordinador resuelve al fusionar: `app/core/i18n/es.py`, `app/mcp/descriptions/`, `frontend/src/lib/api.ts`, los catálogos de i18n compartidos y los fixtures.

## Mapa de archivos

Cada tarea lista sus archivos en su bloque **Files** (Create / Modify / Test), con las líneas exactas. Los archivos compartidos entre tareas están en «Notas de integración del coordinador».

## Tareas

| Task | Qué | Diseño |
|---|---|---|
| 1 | `/me` con el mapa por cliente | §1.3 |
| 2 | `globalRoutes.json` generado y su comparador en la consola | §1.2 |
| 3 | Perfil de salud por defecto de cada cliente (backend) | §5.1 |
| 4 | El asistente reenvía `X-Client-Id` | §5.2 |
| 5 | `remote.supervise`, migración 0244 y el autor del corte | §5.3 |
| 6 | `execute` 404, cliente en la auditoría de respaldos, retirar un repositorio (backend) | §5.4, §5.5 |
| 7 | Consola: tipo `Me`, selección, selector, cabecera, caché y socket | §1.1, §1.2, §1.4, §1.5 |
| 8 | Consola: menú por permisos y módulos, Imaging fuera, pantallas globales y nombre de cliente | §2, §3.4 |
| 9 | Consola: `clientSelection.ts`, `ClientField`, `SiteField` y el piloto en tickets | §3.1, §3.2, §4.5 |
| 10 | Consola, grupo A: ipam, devices, agent-releases, enrollment, credentials, agent-push y backup | §3 |
| 11 | Consola, grupo B: budget, settings, docs, patch, sla, subscriptions, licenses, change y bcp | §3 |
| 12 | Página Clientes, cierre de `internalClient.ts` y prueba anti-`PHASE 3` | §4.1, §3.6 |
| 13 | Equipos (columnas, filtro por sede, cambio de sede), alta, tickets, usuarios y auditoría | §4.2–§4.6 |
| 14 | Consola de los extras: salud por cliente y retirar repositorio | §5.1, §5.5 |

## Notas de integración del coordinador

Las tareas de una misma oleada se escribieron en paralelo. Estas notas recogen lo que cruza de una a otra.

- **Oleada 1, al fusionar:**
  - Si la Task 5 (0244) entra antes que la 1, las pruebas de la Task 1 que listan los códigos de org-admin o super-admin incluyen `remote.supervise`.
  - Si alguna tarea cambia `GLOBAL_WRITES` o `GLOBAL_READS`, regenera `frontend/src/lib/globalRoutes.json` con `cd backend && .venv/bin/python -m tests.export_console_global_routes`. Ninguna lo hace según el plan: la ruta de retirar un repositorio no es global.
- **Choques esperados al fusionar:**
  - `app/core/i18n/es.py`: las Tasks 3, 5 y 6 cambian o añaden entradas.
  - `app/mcp/descriptions/part_b.py`: Tasks 3 y 6. `part_d.py`: Tasks 1 y 5.
  - `tests/test_client_record_checks.py` (Tasks 3 y 5), `app/core/rbac.py` (Task 5) y `app/core/ai/tools/registry.py` (Task 3).
  - Los fixtures de la consola: Tasks 5, 7 y 8.
  - `frontend/src/lib/api.ts`: Tasks 7, 12, 13 y 14.
- **Firmas que una tarea fija y otras consumen:**
  - `useClientScope` es una exportación con nombre de `lib/clientScope.tsx`. `useNewRowClient`, `ClientField` y `SiteField` leen la selección a través de ella, y las pruebas de las Tasks 10 a 14 la sustituyen con `vi.mock`.
  - `ApiClient(getToken, onUnauthorized, base, getClient)`: el cuarto argumento es el cliente elegido. Leer un cliente concreto es `getClientRecord` (Task 12).
- **Decisiones del plan que cambian una línea del diseño**, aceptadas por el coordinador:
  - **Task 1:** `own_profile.py` entra en `UNNARROWED_SCOPE_DOORS`. La lista de clientes del selector no puede seguir a la selección, y ese alcance solo abre la lectura de los clientes de la propia persona. Lo revisa la revisión final.
  - **Task 4:** la firma de `executor_portal.invoke_portal_tool` acepta `requested_client`, pero rechaza todo lo que no sea `None`. Su comportamiento no cambia, y así lo exige el tipado de `PersonaProfile`.
  - **Task 6:** el servidor rechaza con 409 una política que nombre un repositorio retirado. El diseño solo pedía ocultarlo en la consola.
  - **Task 11:** Presupuesto trabaja con un cliente cada vez. Con «Todos mis clientes» pide elegir uno arriba.

---

### Task 1: `/me` con el mapa por cliente

Diseño §1.3. Oleada 1, en paralelo con las Tasks 2 a 6. Solo backend.

Hoy `GET /me` devuelve en `permissions` solo los códigos que la persona tiene en **todos** los clientes
(`backend/app/core/identity/own_profile.py:82-86`, `user_permission_codes` sin cliente), así que un técnico
limitado recibe `[]`. Desde esta tarea devuelve el mapa por cliente, `permissions` como unión de los
clientes del alcance de la petición y `permissions_everywhere` como los códigos de todos los clientes.

**Files:**
- Modify: `backend/app/core/identity/own_profile.py` (archivo completo, 151 líneas hoy: docstring `:1-30`,
  imports `:32-43`, `OwnProfile` `:50-63`, `_profile` `:73-89`, `read_own_profile` `:92-95`,
  `update_own_profile` `:149-151`)
- Modify: `backend/app/api/v1/me.py` (imports `:51-52`, `MeResponse` `:76-93`, `_response` `:122-133`,
  docstring de `me` `:151-153`, `:161`, `:193`)
- Modify: `backend/tests/test_client_catalog.py:136-149` (`UNNARROWED_SCOPE_DOORS`)
- Modify: `backend/app/mcp/descriptions/part_d.py:279-282` (descripción de `GET /api/v1/me`)
- Create: `backend/tests/test_me_clients.py`
- Test: `backend/tests/test_me_clients.py`, `backend/tests/test_client_catalog.py`,
  `backend/tests/test_me_profile.py`, `backend/tests/test_api_routes.py`,
  `backend/tests/test_global_rows_api.py`, `backend/tests/test_mcp_schema.py`,
  `backend/tests/test_mcp_descriptions.py`

**Interfaces:**
- Consumes:
  - `app.core.rbac.clients_holding(session, user_id, None) -> ClientScope` (`backend/app/core/rbac.py:480-509`):
    el alcance propio de la persona, el mismo del que parte `bound_auth_context` antes de `X-Client-Id`
    (`backend/app/api/deps.py:389`).
  - `app.core.rbac.scopes_by_code(session, user_id) -> dict[str, ClientScope]` (`rbac.py:460-477`).
  - `app.core.rbac.user_role_names(session, user_id) -> list[str]` (`rbac.py:512-531`).
  - `ClientScope.allows`, `.is_all` (`backend/app/core/client_scope.py:47-53`); `org_session`
    (`backend/app/core/tenancy.py:24-48`).
  - `CLIENT_ARCHIVED` (`backend/app/services/client_sites.py:63`), `CLIENT_KIND_UNASSIGNED`
    (`backend/app/models/clients.py:35`).
  - `AuthContext.clients` de `bound_auth_context`: el alcance de la persona, estrechado por `X-Client-Id`
    (`deps.py:147-157`).
- Produces (nombres de `01-interfaces.md` y los nuevos que se marcan):
  - `backend/app/api/v1/me.py::MeClient(BaseModel)`: `id: UUID`, `name: str`, `kind: str`,
    `enabled_modules: list[str]` (ordenados), `permissions: list[str]` (ordenados).
  - `MeResponse` gana `permissions_everywhere: list[str]` y `clients: list[MeClient]`; `permissions` pasa a
    ser «códigos con al menos un cliente del alcance de la petición». `PATCH /me` devuelve lo mismo.
  - `backend/app/core/identity/own_profile.py`:
    - `OwnProfile.codes_by_client: dict[UUID, frozenset[str]]` y `OwnProfile.codes_everywhere: frozenset[str]`
      (de `01-interfaces.md`). `OwnProfile.permissions` desaparece.
    - **Nuevos:** `OwnProfile.clients: tuple[OwnClient, ...]`; `OwnProfile.codes_in(scope: ClientScope) -> frozenset[str]`;
      `@dataclass(frozen=True) class OwnClient: id: UUID; name: str; kind: str; enabled_modules: tuple[str, ...]`;
      `@dataclass(frozen=True) class OwnGrants: scope: ClientScope; by_code: dict[str, ClientScope]; roles: list[str]`.
  - Orden de `clients`: por `lower(name)` y `id`, con `unassigned` al final. La consola (Task 7) puede
    pintarlos tal cual.

**Decisión del plan (1): la lista de clientes se lee con el alcance propio de la persona, en una segunda
sesión, y la decide RLS.** El perfil se lee hoy bajo `ClientScope.all()`, y su docstring
(`own_profile.py:12-21`) y la razón de su entrada en `ALL_SCOPE_ALLOWLIST` (`client_scope.py:115-125`)
dicen «tablas G solamente». `clients` tiene su propia política `client_isolation` (migración 0231,
`0231_client_scope_rls.py:664-671`): un alcance de lista ve solo sus clientes y nunca `unassigned`; el
alcance `all`, todos. Leer los nombres bajo `all` y filtrar en el código sería imponer 5a «solo en el
código». Así que `_own_clients` abre `org_session(..., clients=clients_holding(..., None))`: es la misma
regla que decide `unassigned` en `GET /clients` (`backend/app/api/v1/clients.py:248-260`, que la delega en
RLS), y la sesión `all` sigue tocando solo tablas G. La entrada de `ALL_SCOPE_ALLOWLIST` no cambia.

**Decisión del plan (2): `own_profile.py` entra en `UNNARROWED_SCOPE_DOORS`** (`tests/test_client_catalog.py:142-149`).
Esa guarda solo deja a las puertas usar `clients_holding`, porque devuelve un alcance que `X-Client-Id` no
ha estrechado. `/me` lo necesita sin estrechar a propósito: `clients` es la lista del selector y no sigue a
la selección (diseño §1.3: `permissions` se calcula sobre «los clientes de `clients` estrechados por
`X-Client-Id`», luego `clients` no lo está). Ese alcance solo abre la sesión que lee las filas de
`clients` de la propia persona; no llega a ninguna ruta. La razón queda escrita junto a la lista. Es una
decisión de seguridad revisable: el informe la señala.

**Decisión del plan (3): `permissions` se calcula sobre el mapa.** Son los códigos de los clientes de
`clients` que `ctx.clients` permite (`OwnProfile.codes_in`). Un `X-Client-Id` que nombra un cliente
archivado, uno sin rol o un id que no es cliente da `[]`, sin error: nunca amplía. `permissions_everywhere`
son los códigos con asignación de todos los clientes (`scopes_by_code(...)[code].is_all`), lo mismo que
`user_permission_codes(session, user_id)` sin cliente, es decir, el `permissions` de hoy. No depende de la
cabecera.

**Pruebas existentes.** Ninguna fija el significado viejo de forma que cambie:
`tests/test_api_routes.py:25-…` (`set(body["permissions"]) == {...}`), `tests/test_me_profile.py:117` y
`tests/test_mcp_schema.py:38` usan asignaciones de todos los clientes (`ApiEnv.create_user`,
`tests/conftest.py:270-287`), y para ellas `permissions` sale igual que hoy. Se quedan como están. La única
prueba que cambia es la lista de la guarda de `tests/test_client_catalog.py:142-149`: antes
`{"app/api/deps.py", "app/modules/portal/principal.py", "app/core/identity/door.py", "app/core/rbac.py"}`,
después lo mismo más `"app/core/identity/own_profile.py"`, con su razón.

- [ ] **Step 1: Escribir las pruebas que fallan**

Crear `backend/tests/test_me_clients.py`. Los ayudantes copian el patrón de `tests/test_client_rbac.py:35-62`
(`_seed`) y de `tests/client_scope_support.py:48-62` (`everything_on`, un rol propio de la organización):

```python
"""`GET /me` and the per-client map (MSP clients phase 3 design §1.3).

Until phase 3, `/me` answered "what may I do on EVERY client": a technician
whose grants were all per client got `permissions: []`, and the console showed
them the Panel and nothing else. It now answers three questions:

- `clients`: every client the person holds a role in, archived ones left out,
  each with the codes they hold THERE. `unassigned` only for a person with a
  grant on every client, as `GET /clients` lists it. Not narrowed by
  `X-Client-Id`: it is the selector's list, and the selector must still offer
  the other clients while one is chosen.
- `permissions`: the codes held in at least one client of the request's scope
  -- `clients`, narrowed by `X-Client-Id` -- which is `require_permission`'s rule.
- `permissions_everywhere`: the codes held on every client (an all-clients
  grant), `require_permission_on_every_client`'s rule. Never narrowed.

None of it needs `clients.read`: it is the person's own grants, named.

Role names: the seeded `technician` holds `jobs.dispatch` and `clients.read`
among others; `read-only` holds only read codes (`devices.read`,
`clients.read`, ...), so neither `jobs.dispatch` nor `clients.manage`. The
tests read each role's codes from the database rather than restate them.
"""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Client, Permission, Role, RolePermission, User, UserRole

CUSTOMERS = ("Acme", "Bravo", "Charlie")


async def _world(env: Any) -> tuple[UUID, dict[str, UUID]]:
    """An org (`me-…`, so its internal client sorts after the customers) with
    customers Acme, Bravo (modules `change` and `budget`) and Charlie."""
    org = await env.create_org("me")
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        made = {}
        for name in CUSTOMERS:
            modules = ["change", "budget"] if name == "Bravo" else []
            row = Client(org_id=org, name=name, kind="customer", enabled_modules=modules)
            s.add(row)
            await s.flush()
            made[name] = row.id
        await s.commit()
    return org, made


async def _builtin(env: Any, name: str) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        return (
            await s.execute(select(Role.id).where(Role.name == name, Role.org_id.is_(None)))
        ).scalar_one()


async def _custom(env: Any, org: UUID, *codes: str) -> UUID:
    """An organisation's own role holding exactly `codes` -- none is a role too."""
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        role = Role(org_id=org, name=f"custom-{uuid4().hex[:8]}")
        s.add(role)
        await s.flush()
        for permission_id in (
            await s.execute(select(Permission.id).where(Permission.code.in_(codes)))
        ).scalars():
            s.add(RolePermission(role_id=role.id, permission_id=permission_id))
        await s.commit()
        return role.id


async def _codes_of(env: Any, role_name: str) -> set[str]:
    async with AsyncSession(env.admin_engine) as s:
        return set(
            (
                await s.execute(
                    select(Permission.code)
                    .join(RolePermission, RolePermission.permission_id == Permission.id)
                    .join(Role, Role.id == RolePermission.role_id)
                    .where(Role.name == role_name, Role.org_id.is_(None))
                )
            ).scalars()
        )


async def _person(env: Any, org: UUID, grants: list[tuple[UUID, UUID | None]]) -> dict[str, str]:
    """Headers of a new person holding `grants`: [(role id, client id or None)]."""
    sub = await env.create_user(org)
    async with AsyncSession(env.admin_engine) as s:
        user_id = (
            await s.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
        for role_id, client_id in grants:
            s.add(UserRole(org_id=org, user_id=user_id, role_id=role_id, client_id=client_id))
        await s.commit()
    return await env.headers(org, sub)


async def _unassigned(env: Any, org: UUID) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        return (
            await s.execute(
                select(Client.id).where(Client.org_id == org, Client.kind == "unassigned")
            )
        ).scalar_one()


async def _archive(env: Any, client_id: UUID) -> None:
    async with AsyncSession(env.admin_engine) as s:
        await s.execute(update(Client).where(Client.id == client_id).values(status="archived"))
        await s.commit()


async def _me(env: Any, headers: dict[str, str]) -> dict[str, Any]:
    response = await env.client.get("/api/v1/me", headers=headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_a_per_client_technician_gets_each_clients_codes(api_env: Any) -> None:
    """The defect this phase fixes: `permissions` was `[]` for them. And the
    list is exactly their clients -- not Charlie, not the internal client,
    not `unassigned` -- because it is read under THEIR scope, where RLS
    decides it as it does for `GET /clients`."""
    env = api_env
    org, c = await _world(env)
    tech, read_only = await _builtin(env, "technician"), await _builtin(env, "read-only")
    headers = await _person(env, org, [(tech, c["Acme"]), (read_only, c["Bravo"])])
    technician_codes, read_only_codes = (
        await _codes_of(env, "technician"),
        await _codes_of(env, "read-only"),
    )

    me = await _me(env, headers)

    assert me["clients"] == [
        {
            "id": str(c["Acme"]),
            "name": "Acme",
            "kind": "customer",
            "enabled_modules": [],
            "permissions": sorted(technician_codes),
        },
        {
            "id": str(c["Bravo"]),
            "name": "Bravo",
            "kind": "customer",
            "enabled_modules": ["budget", "change"],
            "permissions": sorted(read_only_codes),
        },
    ]
    assert me["permissions"] == sorted(technician_codes | read_only_codes)
    assert me["permissions_everywhere"] == []


async def test_x_client_id_narrows_permissions_and_nothing_else(api_env: Any) -> None:
    env = api_env
    org, c = await _world(env)
    tech, read_only = await _builtin(env, "technician"), await _builtin(env, "read-only")
    headers = await _person(env, org, [(tech, c["Acme"]), (read_only, c["Bravo"])])
    whole = await _me(env, headers)

    narrowed = await _me(env, {**headers, "X-Client-Id": str(c["Bravo"])})

    assert narrowed["permissions"] == sorted(await _codes_of(env, "read-only"))
    assert "jobs.dispatch" not in narrowed["permissions"]
    # The selector's list does not follow the selection.
    assert narrowed["clients"] == whole["clients"]
    assert narrowed["permissions_everywhere"] == []


async def test_a_header_outside_the_map_gives_no_permissions(api_env: Any) -> None:
    """Narrowed, never widened: a client they hold nothing on, the internal
    client, an id that is no client -- each is the empty scope, so no code."""
    env = api_env
    org, c = await _world(env)
    tech = await _builtin(env, "technician")
    headers = await _person(env, org, [(tech, c["Acme"])])

    for outside in (c["Charlie"], org, uuid4()):
        me = await _me(env, {**headers, "X-Client-Id": str(outside)})
        assert me["permissions"] == [], outside
        assert [x["id"] for x in me["clients"]] == [str(c["Acme"])]


async def test_an_every_client_grant_counts_in_every_client(api_env: Any) -> None:
    """An all-clients grant lists every client, `unassigned` last, and counts
    in each; a client grant adds its codes to its own client only. For this
    person `permissions_everywhere` is what `/me` used to call `permissions`."""
    env = api_env
    org, c = await _world(env)
    tech = await _builtin(env, "technician")
    extra = await _custom(env, org, "clients.manage", "sites.manage")
    headers = await _person(env, org, [(tech, None), (extra, c["Bravo"])])
    technician_codes = await _codes_of(env, "technician")
    unassigned = await _unassigned(env, org)

    me = await _me(env, headers)

    assert [(x["id"], x["kind"]) for x in me["clients"]] == [
        (str(c["Acme"]), "customer"),
        (str(c["Bravo"]), "customer"),
        (str(c["Charlie"]), "customer"),
        (str(org), "internal"),
        (str(unassigned), "unassigned"),
    ]
    held = {x["id"]: set(x["permissions"]) for x in me["clients"]}
    assert held[str(c["Bravo"])] == technician_codes | {"clients.manage", "sites.manage"}
    for other in (c["Acme"], c["Charlie"], org, unassigned):
        assert held[str(other)] == technician_codes, other
    assert me["permissions"] == sorted(technician_codes | {"clients.manage", "sites.manage"})
    assert me["permissions_everywhere"] == sorted(technician_codes)

    narrowed = await _me(env, {**headers, "X-Client-Id": str(c["Acme"])})
    assert narrowed["permissions"] == sorted(technician_codes)
    assert narrowed["permissions_everywhere"] == sorted(technician_codes)


async def test_an_archived_client_is_left_out(api_env: Any) -> None:
    env = api_env
    org, c = await _world(env)
    tech, read_only = await _builtin(env, "technician"), await _builtin(env, "read-only")
    limited = await _person(env, org, [(tech, c["Acme"]), (read_only, c["Bravo"])])
    everywhere = await _person(env, org, [(tech, None)])

    await _archive(env, c["Acme"])

    me = await _me(env, limited)
    assert [x["id"] for x in me["clients"]] == [str(c["Bravo"])]
    # Acme's codes went with it.
    assert me["permissions"] == sorted(await _codes_of(env, "read-only"))
    assert str(c["Acme"]) not in {x["id"] for x in (await _me(env, everywhere))["clients"]}


async def test_the_map_needs_no_clients_read(api_env: Any) -> None:
    """The names come from the person's own grants, not from `GET /clients`:
    a role without `clients.read` still names its client, and a role with no
    code at all is still a role in its client."""
    env = api_env
    org, c = await _world(env)
    viewer, nothing = await _custom(env, org, "devices.read"), await _custom(env, org)
    headers = await _person(env, org, [(viewer, c["Acme"]), (nothing, c["Charlie"])])
    assert (await env.client.get("/api/v1/clients", headers=headers)).status_code == 403

    me = await _me(env, headers)

    assert me["clients"] == [
        {
            "id": str(c["Acme"]),
            "name": "Acme",
            "kind": "customer",
            "enabled_modules": [],
            "permissions": ["devices.read"],
        },
        {
            "id": str(c["Charlie"]),
            "name": "Charlie",
            "kind": "customer",
            "enabled_modules": [],
            "permissions": [],
        },
    ]
    assert me["permissions"] == ["devices.read"]


async def test_a_person_with_no_role_has_an_empty_map(api_env: Any) -> None:
    env = api_env
    org, _ = await _world(env)
    me = await _me(env, await _person(env, org, []))
    assert (me["clients"], me["permissions"], me["permissions_everywhere"]) == ([], [], [])


async def test_patch_me_answers_with_the_same_map(api_env: Any) -> None:
    """The PATCH answers with the whole of `/me`, so the console does not
    re-fetch -- narrowed by the same header."""
    env = api_env
    org, c = await _world(env)
    tech, read_only = await _builtin(env, "technician"), await _builtin(env, "read-only")
    headers = await _person(env, org, [(tech, c["Acme"]), (read_only, c["Bravo"])])

    response = await env.client.patch(
        "/api/v1/me", headers={**headers, "X-Client-Id": str(c["Bravo"])}, json={"phone": "0101"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["phone"] == "0101"
    assert body["clients"] == (await _me(env, headers))["clients"]
    assert body["permissions"] == sorted(await _codes_of(env, "read-only"))
    assert body["permissions_everywhere"] == []
```

Qué protege cada prueba (comprobado quitando la protección: cada mutación pone en rojo las pruebas que se
nombran):

| Protección (código del Step 3/4) | Si se quita | Falla |
|---|---|---|
| `_own_clients` abre la sesión con `clients=scope`, el alcance propio | con `ClientScope.all()`, un técnico de Acme ve Charlie, el interno y «Sin asignar» | `test_a_per_client_technician_gets_each_clients_codes`, `test_a_header_outside_the_map_gives_no_permissions`, `test_an_archived_client_is_left_out`, `test_the_map_needs_no_clients_read`, `test_a_person_with_no_role_has_an_empty_map` |
| `.where(..., Client.status != CLIENT_ARCHIVED)` en `_own_clients` | los archivados vuelven a la lista y a `permissions` | `test_an_archived_client_is_left_out` |
| `_response` usa `profile.codes_in(scope)` con `scope=ctx.clients` | con `ClientScope.all()`, la cabecera ya no estrecha | `test_x_client_id_narrows_permissions_and_nothing_else`, `test_a_header_outside_the_map_gives_no_permissions`, `test_an_every_client_grant_counts_in_every_client`, `test_patch_me_answers_with_the_same_map` |
| `codes_by_client` usa `held.allows(client.id)` | con `held.is_all`, Acme pierde los códigos de su asignación | `test_a_per_client_technician_gets_each_clients_codes` |
| la lista sale de `clients_holding(..., None)` (`user_roles`), no de los códigos | un rol sin códigos dejaría fuera a su cliente | `test_the_map_needs_no_clients_read` |

- [ ] **Step 2: Ejecutar y ver que fallan**

Run (desde `backend/`): `.venv/bin/pytest -q tests/test_me_clients.py`
Expected: FAIL, `8 failed`. La primera, `test_a_per_client_technician_gets_each_clients_codes`, con
`KeyError: 'clients'`.

- [ ] **Step 3: `own_profile.py`, el perfil con su mapa**

Sustituir `backend/app/core/identity/own_profile.py` entero por:

```python
"""The caller's own profile, for `GET` and `PATCH /me` (MSP clients spec §2.2).

`/me` IS PERSONAL, SO IT RUNS UNDER EVERY CLIENT. A person's `users` row is a
staff row -- `client_id` NULL, a global row of an O table -- and migration
0231 hides global rows from the EMPTY scope, which is exactly the scope of an
admitted person who holds no grant yet: under the request's own scope `/me`
answered 404 to someone the door had just let in. A per-client technician's
scope reads the row but may not change a global row (`app.core.global_rows`),
so their own timezone would have been a 403. Neither is a statement about
clients: the profile belongs to the person, not to any client.

THAT IS SAFE ONLY BECAUSE OF WHAT THESE SESSIONS TOUCH, and nothing else may
be added to them:

- the caller's OWN `users` row, selected by the `user_id` the validated token
  resolved at the door (`AuthContext.user_id`), never by a request field;
- G tables, which carry no client: the grants (`user_roles`,
  `role_permissions`, `permissions`), `roles`, and the audit chain
  (`audit_events`).

No client table is queried under every client. The write surface is the four
presentation fields, written out below; what a person IS (`kind`,
`external_subject`, `client_id`, `status`) is not reachable -- `ProfileUpdate`
forbids every other field, and the database's `its_user_identity_guard` does
not apply under scope all, so the request schema is the guard here.

That makes this module, not the route file, the reviewed place that holds the
"every client" scope for `/me` (`ALL_SCOPE_ALLOWLIST`, spec §2.3: a route
file never holds it).

THE CLIENT MAP IS READ UNDER THE PERSON'S OWN SCOPE, in a session of its own
(MSP clients phase 3 design §1.3). The names, kinds and modules of the clients
a person holds a role in are client data, so RLS decides which ones they see
(CLAUDE.md invariant 5a): under the scope `bound_auth_context` starts from
(`clients_holding(..., None)`, before `X-Client-Id`), migration 0231's
`client_isolation` policy on `clients` shows exactly the clients they hold a
role in, and `unassigned` only to a grant on every client -- the rule
`GET /clients` answers by. Never every client, and never the request's
narrowed scope: the selector lists every client while one is chosen. That
makes this module the one reader of an un-narrowed scope that is not a door
(`tests/test_client_catalog.py::UNNARROWED_SCOPE_DOORS`), and the scope it
reads is handed to no route.
"""

from collections.abc import Set
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.client_scope import ClientScope
from app.core.rbac import clients_holding, scopes_by_code, user_role_names
from app.core.tenancy import org_session
from app.models import Client, User
from app.models.clients import CLIENT_KIND_UNASSIGNED
from app.services.client_sites import CLIENT_ARCHIVED


class OwnRowMissing(LookupError):
    """The admitted person's own row is gone (admission found or created it)."""


@dataclass(frozen=True)
class OwnClient:
    """A client the person holds a role in, as the selector lists it."""

    id: UUID
    name: str
    kind: str
    enabled_modules: tuple[str, ...]


@dataclass(frozen=True)
class OwnGrants:
    """What the person's grants say, read once under every client."""

    #: Every client they hold any role in: their own scope, before
    #: `X-Client-Id` (`clients_holding(..., None)`).
    scope: ClientScope
    #: Each code they hold, with the clients holding it (`scopes_by_code`).
    by_code: dict[str, ClientScope]
    roles: list[str]


@dataclass(frozen=True)
class OwnProfile:
    user_id: UUID
    org_id: UUID
    email: str
    display_name: str
    phone: str | None
    timezone: str | None
    locale: str | None
    #: The codes held on EVERY client -- an all-clients grant
    #: (`user_roles.client_id` NULL): `require_permission_on_every_client`'s
    #: rule, whatever `X-Client-Id` says.
    codes_everywhere: frozenset[str]
    #: The clients they hold a role in, archived ones left out: by name,
    #: `unassigned` last.
    clients: tuple[OwnClient, ...]
    #: The codes held in each of `clients`. An all-clients grant counts in
    #: every one, a client grant in its own (`user_permission_codes`).
    codes_by_client: dict[UUID, frozenset[str]]
    roles: list[str]
    #: The row's identity at the sign-in provider: where `PATCH /me` mirrors a
    #: changed language. Read from the row `user_id` selected, never a request.
    external_subject: str

    def codes_in(self, scope: ClientScope) -> frozenset[str]:
        """The codes held in at least one of `clients` that `scope` allows:
        `require_permission`'s rule for a request with that scope. A client
        outside `clients` (archived, or no role there) adds nothing."""
        held: set[str] = set()
        for client_id, codes in self.codes_by_client.items():
            if scope.allows(client_id):
                held |= codes
        return frozenset(held)


async def _own_row(session: AsyncSession, user_id: UUID) -> User:
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise OwnRowMissing(str(user_id))  # i18n-exempt: mapped to a translated 404
    return user


async def _own_grants(session: AsyncSession, user_id: UUID) -> OwnGrants:
    """The caller's grants: G tables only (`user_roles`, `role_permissions`,
    `permissions`, `roles`)."""
    return OwnGrants(
        scope=await clients_holding(session, user_id, None),
        by_code=await scopes_by_code(session, user_id),
        roles=await user_role_names(session, user_id),
    )


async def _own_clients(
    org_id: UUID, scope: ClientScope, *, engine: AsyncEngine
) -> tuple[OwnClient, ...]:
    """The clients `scope` -- the person's own -- reaches, archived ones left
    out. RLS decides which (module docstring): this session is never every
    client unless the person holds a grant on every client."""
    async with org_session(org_id, engine=engine, clients=scope) as session:
        rows = (
            await session.execute(
                select(Client.id, Client.name, Client.kind, Client.enabled_modules)
                .where(Client.org_id == org_id, Client.status != CLIENT_ARCHIVED)
                .order_by(
                    case((Client.kind == CLIENT_KIND_UNASSIGNED, 1), else_=0),
                    func.lower(Client.name),
                    Client.id,
                )
            )
        ).all()
    return tuple(
        OwnClient(
            id=row.id,
            name=row.name,
            kind=row.kind,
            enabled_modules=tuple(sorted(row.enabled_modules)),
        )
        for row in rows
    )


async def _profile(user: User, grants: OwnGrants, *, engine: AsyncEngine) -> OwnProfile:
    clients = await _own_clients(user.org_id, grants.scope, engine=engine)
    return OwnProfile(
        user_id=user.id,
        org_id=user.org_id,
        email=user.email,
        display_name=user.display_name,
        phone=user.phone,
        timezone=user.timezone,
        locale=user.locale,
        codes_everywhere=frozenset(code for code, held in grants.by_code.items() if held.is_all),
        clients=clients,
        codes_by_client={
            client.id: frozenset(
                code for code, held in grants.by_code.items() if held.allows(client.id)
            )
            for client in clients
        },
        roles=grants.roles,
        external_subject=user.external_subject,
    )


async def read_own_profile(*, org_id: UUID, user_id: UUID, engine: AsyncEngine) -> OwnProfile:
    """`GET /me`: the caller's own row and grants under every client, then
    the clients those grants reach under the caller's own scope."""
    async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
        user = await _own_row(session, user_id)
        grants = await _own_grants(session, user_id)
    return await _profile(user, grants, engine=engine)


async def read_own_locale(*, org_id: UUID, user_id: UUID, engine: AsyncEngine) -> str | None:
    """The caller's saved language, read again after a mirror (`PATCH /me`)."""
    async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
        return (await _own_row(session, user_id)).locale


async def update_own_profile(
    *,
    org_id: UUID,
    user_id: UUID,
    engine: AsyncEngine,
    supplied: Set[str],
    display_name: str | None,
    phone: str | None,
    timezone: str | None,
    locale: str | None,
) -> tuple[OwnProfile, str | None]:
    """`PATCH /me`: write the SUPPLIED presentation fields of the caller's own
    row, audited (keys only), and return the profile and the locale it had."""
    async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
        user = await _own_row(session, user_id)
        previous_locale = user.locale
        # WRITTEN OUT, not `setattr(user, field, ...)` over `supplied`. The
        # reflective form is one model edit away from assigning to any User
        # attribute whose name a request field happens to share; these four
        # lines are the write surface, readable as such by whoever reviews
        # this next.
        if "display_name" in supplied and display_name is not None:
            user.display_name = display_name
        if "phone" in supplied:
            user.phone = phone
        if "timezone" in supplied:
            user.timezone = timezone
        if "locale" in supplied:
            user.locale = locale
        session.add(user)

        # KEYS ONLY, never values. A phone number is directory data about a
        # person and the audit log is read far more widely than the profile
        # form; recording which fields moved is what makes the event useful
        # without turning the chain into a contact list.
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=user_id,
            action="user.profile_updated",
            target_type="user",
            target_id=str(user_id),
            metadata={"fields": sorted(supplied)},
        )
        grants = await _own_grants(session, user_id)
        await session.flush()
    # AFTER THE COMMIT, so the answer is the row as saved.
    return await _profile(user, grants, engine=engine), previous_locale
```

Notas:
- Las tres funciones de `ALL_SCOPE_ALLOWLIST` (`client_scope.py:116-125`) siguen usando `ClientScope.all()`
  y solo tocan tablas G; `_own_clients`, que lee `clients`, no lo usa. La razón de esa entrada no cambia.
- El comentario de `:82-85` («EVERY-CLIENT GRANTS ONLY, BY DESIGN … phase 3's») desaparece: es el pendiente
  que esta tarea cierra.
- `user` se usa después de cerrar la sesión: `org_session` abre con `expire_on_commit=False`
  (`tenancy.py:33-38`), así que sus columnas siguen cargadas.

- [ ] **Step 4: `me.py`, la respuesta**

En `backend/app/api/v1/me.py`:

1. Imports (`:51-52`): añadir debajo de `from app.api.deps import AuthContext, bound_auth_context`:

```python
from app.core.client_scope import ClientScope
```

2. Sustituir `class MeResponse` (`:76-93`) por:

```python
class MeClient(BaseModel):
    """A client the caller holds a role in (MSP clients phase 3 design §1.3)."""

    id: UUID
    name: str
    kind: str = Field(description="customer, internal (Novieri's own IT) or unassigned")
    enabled_modules: list[str] = Field(description="Optional modules on: budget, bcp, change")
    permissions: list[str] = Field(description="The codes the caller holds in this client")


class MeResponse(BaseModel):
    user_id: UUID
    org_id: UUID
    email: str
    display_name: str
    phone: str | None
    timezone: str | None
    locale: str | None
    #: The codes the caller holds in AT LEAST ONE client of this request's
    #: scope: `clients`, narrowed by `X-Client-Id` when it is sent. That is
    #: `require_permission`'s rule, so it is what the console may offer for
    #: the client chosen in its selector.
    permissions: list[str]
    #: The codes the caller holds on EVERY client (an all-clients grant):
    #: `require_permission_on_every_client`'s rule, for global configuration.
    #: The header does not narrow it; the console never sends one there.
    permissions_everywhere: list[str]
    #: Every client the caller holds a role in, archived ones left out, by
    #: name with `unassigned` last, and what they hold in each. Not narrowed
    #: by `X-Client-Id`: it is the selector's list. Needs no `clients.read`:
    #: these are the caller's own grants.
    clients: list[MeClient]
    #: The names of the roles the caller holds. The console renders these in
    #: the account chip, which used to show the constant "Operator" for every
    #: user on every deployment.
    roles: list[str]
    #: Set only by `PATCH /me` when `locale` was supplied: `True` when the
    #: language was also written to the sign-in provider, `False` when that
    #: write failed (the console setting is saved either way), `None` when no
    #: write was attempted -- locale not supplied, or no provider configured.
    identity_locale_synced: bool | None = None
```

3. Sustituir `_response` (`:122-133`) por:

```python
def _response(profile: OwnProfile, scope: ClientScope) -> MeResponse:
    """`scope` is the request's (`ctx.clients`): the person's own, narrowed
    by `X-Client-Id`. It narrows `permissions` and nothing else."""
    return MeResponse(
        user_id=profile.user_id,
        org_id=profile.org_id,
        email=profile.email,
        display_name=profile.display_name,
        phone=profile.phone,
        timezone=profile.timezone,
        locale=profile.locale,
        permissions=sorted(profile.codes_in(scope)),
        permissions_everywhere=sorted(profile.codes_everywhere),
        clients=[
            MeClient(
                id=client.id,
                name=client.name,
                kind=client.kind,
                enabled_modules=list(client.enabled_modules),
                permissions=sorted(profile.codes_by_client[client.id]),
            )
            for client in profile.clients
        ],
        roles=profile.roles,
    )
```

4. En el docstring de `me` (`:151-153`), el párrafo final queda:

```python
    Under EVERY client, not `ctx.clients`: the profile is the person's, and a
    person with no grant has the empty scope, which sees no staff row
    (`app.core.identity.own_profile`). Its client map is read under the
    person's own scope there; `ctx.clients` narrows only `permissions`.
```

5. `:161`: `return _response(profile)` pasa a `return _response(profile, ctx.clients)`.
6. `:193`: `response = _response(profile)` pasa a `response = _response(profile, ctx.clients)`.

- [ ] **Step 5: Ver que pasan, y que la guarda del alcance sin estrechar salta**

Run: `.venv/bin/pytest -q tests/test_me_clients.py tests/test_client_catalog.py`
Expected: `tests/test_me_clients.py` PASS (8). `tests/test_client_catalog.py::test_only_the_doors_read_an_unnarrowed_scope`
FAIL:

```
AssertionError: person_scope / clients_holding return a scope X-Client-Id has not narrowed; only the doors may use them (intersect with the request's own scope instead): ['app/core/identity/own_profile.py:54:clients_holding', 'app/core/identity/own_profile.py:133:clients_holding']
```

Es la guarda haciendo su trabajo: el uso es deliberado (Decisión del plan 2) y se registra en el paso
siguiente, con su razón.

- [ ] **Step 6: Registrar `own_profile.py` en la guarda, con su razón**

En `backend/tests/test_client_catalog.py`, sustituir `:136-149` por:

```python
# The functions that answer with a person's UN-NARROWED scope: X-Client-Id has
# not been applied to what they return. A door combines them with the
# request's own scope (`deps.admit`, `require_permission`, the portal's
# principal and gate, `read_door_identity`); anything else calling them could
# hand a route a scope wider than the one its request asked for.
#
# `own_profile.py` is the one reader that is not a door (MSP clients phase 3
# design §1.3): `/me`'s client map is the selector's list, which must not
# follow the selection, so it reads the person's own `clients` rows under
# their un-narrowed scope -- RLS deciding which -- and hands that scope to no
# route. `permissions` is still narrowed, by `ctx.clients`.
UNNARROWED_SCOPE_READERS = frozenset({"person_scope", "clients_holding"})
UNNARROWED_SCOPE_DOORS = frozenset(
    {
        "app/api/deps.py",
        "app/modules/portal/principal.py",
        "app/core/identity/door.py",
        "app/core/rbac.py",
        "app/core/identity/own_profile.py",
    }
)
```

`test_the_unnarrowed_scope_guard_is_not_vacuous` (`:186-202`) no cambia: sigue exigiendo que las tres
puertas aparezcan y que la guarda detecte un uso ajeno.

- [ ] **Step 7: La descripción MCP de `GET /me`**

En `backend/app/mcp/descriptions/part_d.py`, sustituir `:279-282` por (232 caracteres; el límite de
`tests/test_mcp_descriptions.py:62-64` es 300):

```python
    "GET /api/v1/me": (
        "Returns the caller's own profile and role names; each client they hold a role in, with "
        "the permission codes they hold there; the codes they hold in at least one client of the "
        "request's scope; and the codes they hold on every client."
    ),
```

No hay ruta nueva ni textos nuevos para el usuario: ningún `tr()` ni `es.py` en esta tarea.

- [ ] **Step 8: Ver que todo lo de `/me` pasa**

Run: `.venv/bin/pytest -q tests/test_me_clients.py tests/test_client_catalog.py tests/test_me_profile.py tests/test_me_locale.py tests/test_api_routes.py tests/test_global_rows_api.py tests/test_client_rbac.py tests/test_mcp_schema.py tests/test_mcp_descriptions.py tests/test_mcp_tools.py tests/test_mcp_channel_api.py tests/test_route_gating.py tests/test_logging_discipline.py`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add backend/app/core/identity/own_profile.py backend/app/api/v1/me.py backend/app/mcp/descriptions/part_d.py backend/tests/test_me_clients.py backend/tests/test_client_catalog.py
git commit -m "feat(clients): /me lists each client the person holds a role in, with its codes (phase 3 Task 1)"
```

- [ ] **Step 10: Verificación final**

Desde `backend/` (con `pg_ctlcluster 16 main start; redis-cli ping || redis-server --daemonize yes`):
- `.venv/bin/pytest -q` (la suite completa, más de 30 min). Expected: PASS.
- `.venv/bin/mypy app`. Expected: `Success`.
- `.venv/bin/ruff check app tests`. Expected: `All checks passed!`.

La consola no se toca en esta tarea: el tipo `Me` gana `clients` y `permissions_everywhere` en la Task 7.

---

### Task 2: `globalRoutes.json` generado y su comparador en la consola

Diseño §1.2. Oleada 1, en paralelo con las Tasks 1 y 3 a 6. Lo de la consola son solo archivos nuevos bajo
`frontend/src/lib/`; `lib/api.ts` no se toca (lo cablea la Task 7).

Las rutas que exigen el alcance de todos los clientes están declaradas una vez, en `GLOBAL_WRITES`
(`backend/tests/test_global_writes.py:57-180`, 95 rutas) y `GLOBAL_READS` (`:184-192`, 4 rutas), con el
prefijo `/api/v1`. La consola no las copia a mano: un módulo las exporta a `frontend/src/lib/globalRoutes.json`
y una prueba del backend falla si el archivo no es exactamente su unión.

**Files:**
- Create: `backend/tests/export_console_global_routes.py` (el exportador; no lleva prefijo `test_`, pytest no
  lo colecciona)
- Create: `backend/tests/test_console_global_routes.py`
- Create: `frontend/src/lib/globalRoutes.json` (generado, nunca a mano)
- Create: `frontend/src/lib/globalRoutes.ts`
- Create: `frontend/src/lib/globalRoutes.test.ts`
- Test: `backend/tests/test_console_global_routes.py`, `frontend/src/lib/globalRoutes.test.ts`

**Interfaces:**
- Consumes: `GLOBAL_WRITES`, `GLOBAL_READS`, `WRITE_METHODS` (`backend/tests/test_global_writes.py:53,57,184`).
  Importar ese módulo fuera de pytest no necesita base de datos (comprobado:
  `.venv/bin/python -c "import tests.test_global_writes"`).
- Produces:
  - `frontend/src/lib/globalRoutes.json`: `[{"method": "POST", "path": "/agent/releases"}, …]`, rutas sin el
    prefijo `/api/v1`, ordenadas por (path, method), una ruta por línea.
  - `backend/tests/test_console_global_routes.py`: su mensaje de fallo dice
    `Regenerate it, from backend/: .venv/bin/python -m tests.export_console_global_routes`.
  - `backend/tests/export_console_global_routes.py`: **nuevos** `API_PREFIX = "/api/v1"`,
    `CONSOLE_ROUTES_JSON: Path`, `REGENERATE: str`, `console_global_routes() -> list[dict[str, str]]`,
    `render(routes: list[dict[str, str]]) -> str`, `main() -> None`.
  - `frontend/src/lib/globalRoutes.ts`: `export function isGlobalRoute(method: string, path: string): boolean`
    (de `01-interfaces.md`); **nuevos** `export interface GlobalRoute { method: string; path: string }` y
    `export const GLOBAL_ROUTES: readonly GlobalRoute[]` (la lista, para la Task 8: cada pantalla que llama
    a una ruta global condiciona sus escrituras por `permissions_everywhere`).

**Decisión del plan (4): formato del archivo.** Un array JSON con una ruta por línea
(`  {"method": "POST", "path": "/clients"},`), que es JSON válido y hace que añadir o quitar una ruta sea
un diff de una línea. La prueba compara también los bytes: el archivo editado a mano o reordenado falla y
pide regenerarlo.

**Decisión del plan (5): `isGlobalRoute` es tolerante en dos cosas que la interfaz no prohíbe.** Quita la
query string y el fragmento antes de comparar (`GET /audit?limit=50` es global), y compara el método sin
mayúsculas. La interfaz dice que `path` llega sin query string; esto evita que un descuido de la Task 7
convierta `GET /audit?…` en un 403. Una ruta con el prefijo `/api/v1` **no** casa: el contrato es la ruta
relativa, como la pide `ApiClient` (`frontend/src/lib/api.ts:1502-1512`, `fetch(\`${this.base}${path}\`)`).

- [ ] **Step 1: Escribir la prueba del backend que falla**

Crear `backend/tests/test_console_global_routes.py`. Sigue el patrón de
`backend/tests/test_patch_type_parity.py:40` (leer un archivo de `frontend/` con
`Path(__file__).resolve().parents[2]`; la CI hace checkout del repositorio entero, `.github/workflows/ci.yml:78`):

```python
"""The console's list of global routes is the server's (phase 3 design §1.2).

`frontend/src/lib/globalRoutes.json` names the routes the console never sends
`X-Client-Id` on: with the header, a scope of every client is impossible, so
`require_permission_on_every_client` answers 403 to the very person allowed
to make the change. Which routes those are is declared once, in
`GLOBAL_WRITES` and `GLOBAL_READS` (`tests/test_global_writes.py`), and
checked there against the router in both directions. The console's file is
GENERATED from them (`tests/export_console_global_routes.py`), never typed,
and this file fails CI when the two disagree -- so a new global route that
nobody regenerated for stops CI, not a technician in production.

Two checks, because they fail for different reasons: the routes themselves
(a route missing or extra: the message names it), then the bytes (the file
was edited by hand, or reordered).
"""

import json

from tests.export_console_global_routes import (
    API_PREFIX,
    CONSOLE_ROUTES_JSON,
    REGENERATE,
    console_global_routes,
    render,
)
from tests.test_global_writes import GLOBAL_READS, GLOBAL_WRITES, WRITE_METHODS


def _key(route: dict[str, str]) -> tuple[str, str]:
    return route["method"], route["path"]


def test_the_console_list_is_exactly_the_global_routes() -> None:
    on_disk = json.loads(CONSOLE_ROUTES_JSON.read_text(encoding="utf-8"))
    expected = console_global_routes()
    missing = sorted(set(map(_key, expected)) - set(map(_key, on_disk)))
    extra = sorted(set(map(_key, on_disk)) - set(map(_key, expected)))
    assert not missing and not extra, (
        f"{CONSOLE_ROUTES_JSON.name} is not GLOBAL_WRITES | GLOBAL_READS "
        f"(tests/test_global_writes.py). Missing: {missing}. Extra: {extra}. {REGENERATE}"
    )


def test_the_console_list_is_the_generated_file() -> None:
    assert CONSOLE_ROUTES_JSON.read_text(encoding="utf-8") == render(console_global_routes()), (
        f"{CONSOLE_ROUTES_JSON.name} differs from what the export writes "
        f"(order or format). {REGENERATE}"
    )


def test_the_export_is_every_declared_route_relative_to_the_api() -> None:
    """Non-vacuity: one entry per declared route, none lost to a collision,
    each under `/api/v1` and named without it -- the path `ApiClient`
    requests, which is what the console compares."""
    routes = console_global_routes()
    declared = set(GLOBAL_WRITES) | set(GLOBAL_READS)
    assert len(routes) == len(declared) == len(GLOBAL_WRITES) + len(GLOBAL_READS)
    assert {(r["method"], API_PREFIX + r["path"]) for r in routes} == declared
    assert all(r["path"].startswith("/") and not r["path"].startswith(API_PREFIX) for r in routes)
    assert {r["method"] for r in routes} <= WRITE_METHODS | {"GET"}
    assert routes == sorted(routes, key=lambda r: (r["path"], r["method"]))
```

Qué protege: `test_the_console_list_is_exactly_the_global_routes` falla si una ruta de `GLOBAL_WRITES` o
`GLOBAL_READS` falta en el JSON (comprobado quitando la línea de `GET /audit/verify`: falla con
`Missing: [('GET', '/audit/verify')]`), que en producción sería un 403 para un administrador con un cliente
elegido. No es una protección de seguridad (la consola no protege nada, invariante 6); es la que impide
ese 403.

- [ ] **Step 2: Ejecutar y ver que falla**

Run (desde `backend/`): `.venv/bin/pytest -q tests/test_console_global_routes.py`
Expected: FAIL en la colección, `ModuleNotFoundError: No module named 'tests.export_console_global_routes'`.

- [ ] **Step 3: El exportador**

Crear `backend/tests/export_console_global_routes.py`:

```python
"""Write the console's list of global routes (phase 3 design §1.2).

`GLOBAL_WRITES` and `GLOBAL_READS` (`tests/test_global_writes.py`) are the one
declaration of the routes that need a scope of every client. The console must
never send `X-Client-Id` on them, and reads them from
`frontend/src/lib/globalRoutes.json`, which this module writes:

    cd backend && .venv/bin/python -m tests.export_console_global_routes

`tests/test_console_global_routes.py` fails CI when the file and the lists
disagree. Not a pytest file (no `test_` prefix): run it, do not collect it.
"""

import json
from pathlib import Path

from tests.test_global_writes import GLOBAL_READS, GLOBAL_WRITES

API_PREFIX = "/api/v1"
CONSOLE_ROUTES_JSON = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "globalRoutes.json"
)
REGENERATE = "Regenerate it, from backend/: .venv/bin/python -m tests.export_console_global_routes"


def console_global_routes() -> list[dict[str, str]]:
    """`GLOBAL_WRITES | GLOBAL_READS` as the console names them: the path
    relative to `/api/v1` (what `ApiClient` requests), sorted by
    (path, method)."""
    routes: list[dict[str, str]] = []
    for method, path in set(GLOBAL_WRITES) | set(GLOBAL_READS):
        if not path.startswith(API_PREFIX + "/"):
            raise ValueError(f"{method} {path} is not under {API_PREFIX}")
        routes.append({"method": method, "path": path.removeprefix(API_PREFIX)})
    return sorted(routes, key=lambda route: (route["path"], route["method"]))


def render(routes: list[dict[str, str]]) -> str:
    """The file's exact bytes: a JSON array with one route per line, so a
    route added or removed is a one-line diff."""
    return "[\n" + ",\n".join(f"  {json.dumps(route)}" for route in routes) + "\n]\n"


def main() -> None:
    routes = console_global_routes()
    CONSOLE_ROUTES_JSON.write_text(render(routes), encoding="utf-8")
    print(f"wrote {len(routes)} routes to {CONSOLE_ROUTES_JSON}")


if __name__ == "__main__":
    main()
```

(`tests/` no entra en `mypy app` ni en el catálogo i18n, que solo recorre `app/`
(`tests/test_i18n_catalog.py:20,45`); el `ValueError` es un error de programación del exportador.)

- [ ] **Step 4: Ejecutar: falta el archivo**

Run: `.venv/bin/pytest -q tests/test_console_global_routes.py`
Expected: `2 failed, 1 passed`, las dos con
`FileNotFoundError: [Errno 2] No such file or directory: '…/frontend/src/lib/globalRoutes.json'`.

- [ ] **Step 5: Generar `globalRoutes.json`**

Run (desde `backend/`): `.venv/bin/python -m tests.export_console_global_routes`
Expected: `wrote 99 routes to /…/frontend/src/lib/globalRoutes.json`.

El archivo generado (no se edita a mano; si alguna tarea de la oleada 1 cambia `GLOBAL_WRITES` o
`GLOBAL_READS`, se regenera con el mismo comando) es:

```json
[
  {"method": "POST", "path": "/agent/releases"},
  {"method": "DELETE", "path": "/agent/releases/versions/{version}"},
  {"method": "POST", "path": "/agent/releases/versions/{version}/promote"},
  {"method": "DELETE", "path": "/agent/releases/{release_id}"},
  {"method": "POST", "path": "/agent/releases/{release_id}/artifact"},
  {"method": "PUT", "path": "/agent/releases/{release_id}/release-signature"},
  {"method": "POST", "path": "/agent/update-rollouts"},
  {"method": "POST", "path": "/agent/update-rollouts/preview"},
  {"method": "POST", "path": "/agent/update-rollouts/{rollout_id}/advance"},
  {"method": "POST", "path": "/agent/update-rollouts/{rollout_id}/cancel"},
  {"method": "POST", "path": "/agent/update-rollouts/{rollout_id}/pause"},
  {"method": "POST", "path": "/agent/update-rollouts/{rollout_id}/resume"},
  {"method": "POST", "path": "/agent/update-rollouts/{rollout_id}/rings/{ring_id}/close"},
  {"method": "POST", "path": "/agent/update-rollouts/{rollout_id}/start"},
  {"method": "PUT", "path": "/ai/autonomy"},
  {"method": "PUT", "path": "/ai/settings"},
  {"method": "POST", "path": "/approved-actions"},
  {"method": "DELETE", "path": "/approved-actions/{action_id}"},
  {"method": "PATCH", "path": "/approved-actions/{action_id}"},
  {"method": "DELETE", "path": "/approved-actions/{action_id}/exempt-script"},
  {"method": "POST", "path": "/approved-actions/{action_id}/exempt-script"},
  {"method": "GET", "path": "/audit"},
  {"method": "GET", "path": "/audit/verify"},
  {"method": "POST", "path": "/budget/fx-rates"},
  {"method": "PATCH", "path": "/budget/fx-rates/{fx_rate_id}"},
  {"method": "POST", "path": "/clients"},
  {"method": "POST", "path": "/clients/{client_id}/archive"},
  {"method": "POST", "path": "/deploy/packages"},
  {"method": "DELETE", "path": "/deploy/packages/{package_id}"},
  {"method": "POST", "path": "/deploy/packages/{package_id}/artifact"},
  {"method": "POST", "path": "/deploy/scripts"},
  {"method": "PUT", "path": "/deploy/scripts/{script_id}"},
  {"method": "POST", "path": "/email-intake/mailboxes"},
  {"method": "DELETE", "path": "/email-intake/mailboxes/{mailbox_id}"},
  {"method": "PATCH", "path": "/email-intake/mailboxes/{mailbox_id}"},
  {"method": "POST", "path": "/email-intake/mailboxes/{mailbox_id}/oauth/disconnect"},
  {"method": "POST", "path": "/email-intake/mailboxes/{mailbox_id}/oauth/start"},
  {"method": "POST", "path": "/email-intake/mailboxes/{mailbox_id}/test"},
  {"method": "GET", "path": "/email-intake/messages"},
  {"method": "PUT", "path": "/email-intake/oauth-app"},
  {"method": "POST", "path": "/email-intake/oauth/abandon"},
  {"method": "POST", "path": "/email-intake/oauth/complete"},
  {"method": "POST", "path": "/health/profiles"},
  {"method": "DELETE", "path": "/health/profiles/{profile_id}"},
  {"method": "PATCH", "path": "/health/profiles/{profile_id}"},
  {"method": "PUT", "path": "/health/profiles/{profile_id}/checks"},
  {"method": "POST", "path": "/health/profiles/{profile_id}/clone"},
  {"method": "PUT", "path": "/health/settings"},
  {"method": "POST", "path": "/licenses/products"},
  {"method": "PATCH", "path": "/licenses/products/{product_id}"},
  {"method": "POST", "path": "/licenses/review-queue/map"},
  {"method": "POST", "path": "/notifications/allowlist"},
  {"method": "DELETE", "path": "/notifications/allowlist/{entry_id}"},
  {"method": "POST", "path": "/notifications/channels"},
  {"method": "DELETE", "path": "/notifications/channels/{channel_id}"},
  {"method": "PATCH", "path": "/notifications/channels/{channel_id}"},
  {"method": "POST", "path": "/notifications/channels/{channel_id}/test"},
  {"method": "GET", "path": "/notifications/deliveries"},
  {"method": "DELETE", "path": "/notifications/queues/{queue_id}/members/{user_id}"},
  {"method": "PUT", "path": "/notifications/queues/{queue_id}/members/{user_id}"},
  {"method": "POST", "path": "/notifications/rules"},
  {"method": "DELETE", "path": "/notifications/rules/{rule_id}"},
  {"method": "PATCH", "path": "/notifications/rules/{rule_id}"},
  {"method": "PATCH", "path": "/orgs/current"},
  {"method": "POST", "path": "/platform/errors/{group_id}/resolve"},
  {"method": "POST", "path": "/platform/errors/{group_id}/unresolve"},
  {"method": "POST", "path": "/provisioning/images"},
  {"method": "POST", "path": "/provisioning/profiles"},
  {"method": "PUT", "path": "/provisioning/profiles/{profile_id}"},
  {"method": "POST", "path": "/roles"},
  {"method": "DELETE", "path": "/roles/{role_id}"},
  {"method": "PATCH", "path": "/roles/{role_id}"},
  {"method": "PUT", "path": "/roles/{role_id}/permissions"},
  {"method": "POST", "path": "/service-desk/canned-responses"},
  {"method": "DELETE", "path": "/service-desk/canned-responses/{response_id}"},
  {"method": "PATCH", "path": "/service-desk/canned-responses/{response_id}"},
  {"method": "POST", "path": "/service-desk/categories"},
  {"method": "PATCH", "path": "/service-desk/categories/{category_id}"},
  {"method": "POST", "path": "/service-desk/queues"},
  {"method": "PATCH", "path": "/service-desk/queues/{queue_id}"},
  {"method": "PUT", "path": "/settings/email"},
  {"method": "POST", "path": "/settings/email/test"},
  {"method": "POST", "path": "/sla/calendars"},
  {"method": "DELETE", "path": "/sla/calendars/{calendar_id}"},
  {"method": "PATCH", "path": "/sla/calendars/{calendar_id}"},
  {"method": "POST", "path": "/sla/calendars/{calendar_id}/holidays"},
  {"method": "DELETE", "path": "/sla/calendars/{calendar_id}/holidays/{holiday_id}"},
  {"method": "PUT", "path": "/sla/calendars/{calendar_id}/windows"},
  {"method": "PUT", "path": "/sla/settings"},
  {"method": "POST", "path": "/users"},
  {"method": "DELETE", "path": "/users/{user_id}"},
  {"method": "POST", "path": "/users/{user_id}/disable"},
  {"method": "POST", "path": "/users/{user_id}/enable"},
  {"method": "POST", "path": "/users/{user_id}/resend-enrolment"},
  {"method": "POST", "path": "/users/{user_id}/reset-password"},
  {"method": "POST", "path": "/users/{user_id}/roles"},
  {"method": "DELETE", "path": "/users/{user_id}/roles/{assignment_id}"},
  {"method": "POST", "path": "/vendors"},
  {"method": "PATCH", "path": "/vendors/{vendor_id}"}
]
```

- [ ] **Step 6: Ejecutar la prueba del backend**

Run: `.venv/bin/pytest -q tests/test_console_global_routes.py tests/test_global_writes.py`
Expected: PASS (`test_console_global_routes.py`: 3 passed).

- [ ] **Step 7: Escribir la prueba de la consola que falla**

Crear `frontend/src/lib/globalRoutes.test.ts` (estilo de `frontend/src/lib/permissions.test.ts` y
`internalClient.test.ts`):

```ts
/**
 * Which requests never carry `X-Client-Id` (phase 3 design §1.2).
 *
 * WHAT IS PINNED. The comparison, not the list: the list is the server's,
 * and `backend/tests/test_console_global_routes.py` holds `globalRoutes.json`
 * equal to it. Here, that a request is matched by METHOD and PATH together, a
 * `{param}` standing for exactly one non-empty segment -- so `PATCH
 * /clients/{id}` (one client's own row) keeps the header while `POST
 * /clients/{id}/archive` (every client) drops it.
 */

import { describe, expect, it } from "vitest";

import { GLOBAL_ROUTES, isGlobalRoute } from "./globalRoutes";
import routes from "./globalRoutes.json";

const ID = "0192f3a4-5b6c-7d8e-9f01-23456789abcd";

describe("isGlobalRoute", () => {
  it("matches a template with its parameter filled in", () => {
    expect(isGlobalRoute("POST", `/clients/${ID}/archive`)).toBe(true);
    expect(isGlobalRoute("DELETE", `/users/${ID}/roles/${ID}`)).toBe(true);
    expect(isGlobalRoute("DELETE", "/agent/releases/versions/1.4.2")).toBe(true);
  });

  it("needs the method too", () => {
    expect(isGlobalRoute("POST", "/clients")).toBe(true);
    expect(isGlobalRoute("GET", "/clients")).toBe(false);
    expect(isGlobalRoute("PATCH", "/orgs/current")).toBe(true);
    expect(isGlobalRoute("GET", "/orgs/current")).toBe(false);
    expect(isGlobalRoute("GET", "/audit")).toBe(true);
    expect(isGlobalRoute("post", "/clients")).toBe(true);
  });

  it("keeps the header on a route that is one client's", () => {
    expect(isGlobalRoute("PATCH", `/clients/${ID}`)).toBe(false);
    expect(isGlobalRoute("POST", `/clients/${ID}/contacts`)).toBe(false);
    expect(isGlobalRoute("POST", "/health/assignments")).toBe(false);
    expect(isGlobalRoute("GET", "/devices")).toBe(false);
  });

  it("lets a parameter stand for one non-empty segment, never more or none", () => {
    expect(isGlobalRoute("POST", `/clients/${ID}/x/archive`)).toBe(false);
    expect(isGlobalRoute("POST", "/clients//archive")).toBe(false);
    expect(isGlobalRoute("POST", `/clients/${ID}/archive/`)).toBe(false);
    expect(isGlobalRoute("POST", "/clients/archive")).toBe(false);
  });

  it("compares the path without its query string", () => {
    expect(isGlobalRoute("GET", "/audit?limit=50&offset=100")).toBe(true);
    expect(isGlobalRoute("GET", "/notifications/deliveries?status=failed")).toBe(true);
  });

  it("takes paths relative to /api/v1, as ApiClient requests them", () => {
    expect(isGlobalRoute("GET", "/api/v1/audit")).toBe(false);
  });

  it("finds every route of the generated file", () => {
    expect(GLOBAL_ROUTES.length).toBe(routes.length);
    expect(GLOBAL_ROUTES.length).toBeGreaterThan(0);
    for (const route of GLOBAL_ROUTES) {
      const filled = route.path.replace(/\{[^/{}]+\}/g, ID);
      expect(isGlobalRoute(route.method, filled), `${route.method} ${route.path}`).toBe(true);
      expect(route.path.startsWith("/api/")).toBe(false);
    }
  });
});
```

Qué protege: «needs the method too» falla si `isGlobalRoute` deja de comparar el método (comprobado
quitando `matcher.method === wanted &&`); «lets a parameter stand for one non-empty segment» falla si un
`{param}` acepta un segmento vacío (comprobado). Sin el método, `GET /clients` casaría con `POST /clients`,
perdería la cabecera y la lista dejaría de seguir al selector. El servidor tampoco ampliaría nada (estrecha
por permiso), pero no sería el cliente elegido.

- [ ] **Step 8: Ejecutar y ver que falla**

Run (desde `frontend/`): `npx vitest run src/lib/globalRoutes.test.ts`
Expected: FAIL, `7 failed`, con `TypeError: isGlobalRoute is not a function` (sin `globalRoutes.ts`, Vite
resuelve `./globalRoutes` al `globalRoutes.json` del Step 5).

- [ ] **Step 9: El comparador**

Crear `frontend/src/lib/globalRoutes.ts`:

```ts
/**
 * The routes of configuration shared by every client, which never carry
 * `X-Client-Id` (phase 3 design §1.2).
 *
 * WHY. The server lets only a scope of EVERY client change global
 * configuration (`require_permission_on_every_client`), and the header
 * narrows the scope to one client -- so a global route sent with it is a 403
 * for the very person allowed to make the change, whichever client the
 * selector shows.
 *
 * THE LIST IS NOT WRITTEN HERE. `globalRoutes.json` is generated from
 * `GLOBAL_WRITES` and `GLOBAL_READS` in `backend/tests/test_global_writes.py`
 * (`cd backend && .venv/bin/python -m tests.export_console_global_routes`),
 * and `backend/tests/test_console_global_routes.py` fails CI when they
 * disagree.
 *
 * Paths are relative to `/api/v1`, as `ApiClient` requests them. A `{param}`
 * stands for exactly one non-empty segment; a query string is ignored.
 */

import routes from "./globalRoutes.json";

export interface GlobalRoute {
  method: string;
  path: string;
}

export const GLOBAL_ROUTES: readonly GlobalRoute[] = routes;

interface Matcher {
  method: string;
  /** The template's segments; `null` is a `{param}`, which one non-empty segment fills. */
  segments: readonly (string | null)[];
}

const MATCHERS: readonly Matcher[] = GLOBAL_ROUTES.map((route) => ({
  method: route.method.toUpperCase(),
  segments: route.path.split("/").map((part) => (/^\{[^/{}]+\}$/.test(part) ? null : part)),
}));

export function isGlobalRoute(method: string, path: string): boolean {
  const wanted = method.toUpperCase();
  const parts = (path.split(/[?#]/, 1)[0] ?? "").split("/");
  return MATCHERS.some(
    (matcher) =>
      matcher.method === wanted &&
      matcher.segments.length === parts.length &&
      matcher.segments.every((segment, index) => {
        const part = parts[index] ?? "";
        return segment === null ? part !== "" : segment === part;
      }),
  );
}
```

(`resolveJsonModule` ya está en `frontend/tsconfig.json:16`. `noUncheckedIndexedAccess` (`:9`) es la razón
de los `?? ""`.)

- [ ] **Step 10: Ejecutar y ver que pasa**

Run (desde `frontend/`): `npx vitest run src/lib/globalRoutes.test.ts` y `npx tsc -b`
Expected: `7 passed`; `tsc` sin errores.

- [ ] **Step 11: Commit**

```bash
git add backend/tests/export_console_global_routes.py backend/tests/test_console_global_routes.py frontend/src/lib/globalRoutes.json frontend/src/lib/globalRoutes.ts frontend/src/lib/globalRoutes.test.ts
git commit -m "feat(console): global routes generated from the server's lists, and their matcher (phase 3 Task 2)"
```

- [ ] **Step 12: Verificación final**

Desde `backend/` (con `pg_ctlcluster 16 main start; redis-cli ping || redis-server --daemonize yes`):
- `.venv/bin/pytest -q` (la suite completa, más de 30 min). Expected: PASS.
- `.venv/bin/mypy app`. Expected: `Success` (esta tarea no toca `app/`).
- `.venv/bin/ruff check app tests`. Expected: `All checks passed!`.

Desde `frontend/`:
- `npx vitest run` completo. Expected: PASS.
- `npx tsc -b`. Expected: sin errores.
- `npm run build`. Expected: build correcto.


### Task 3: Perfil de salud por defecto de cada cliente (backend)

Diseño: §5.1. Inventario: `inventory-extras.md` §C. La parte de consola es la Task 14.

Hoy `POST /health/assignments` escribe la asignación de un equipo, de un grupo o la de la organización, nunca
el defecto de un cliente. El modelo, el índice (`uq_health_assignment_client_org`, `NULLS NOT DISTINCT`) y el
resolvedor (`health/resolution.py:253-310`, `_default_assignments` en `:164-190`) ya lo contemplan desde la
fase 2. Falta la API: el campo, la comprobación sobre el cliente, las dos negativas y el nivel `client` en la
respuesta. No hay migración: la columna `client_id` existe desde la 0231.

**Files:**
- Create: `backend/tests/test_health_client_default.py`
- Modify: `backend/app/modules/health/schema.py:119` (`ResolutionLevel`), `:348-372` (`AssignmentCreate`, `AssignmentOut`)
- Modify: `backend/app/modules/health/resolution.py:64` (`__all__`), `:73` (`ResolutionLevel`), `:102-108` (`ResolvedProfile.level`)
- Modify: `backend/app/modules/health/errors.py` (al final, tras `:42`)
- Modify: `backend/app/modules/health/profiles.py:79`, `:81-85`, `:103`, `:158-165` (`_ASSIGNMENT_INDEX_BY_LEVEL`), `:1351-1467` (`create_assignment`, `_assignment_at_level`), `:1470-1524` (`delete_assignment`), `:1548-1567` (`_audit`)
- Modify: `backend/app/modules/health/api.py:179-184`, `:212`, `:252-276` (`_http_error`), `:381-392` (`_assignment_out`), `:842-873` (`create_assignment_route`)
- Modify: `backend/app/core/i18n/es.py:1880-1883`
- Modify: `backend/app/mcp/descriptions/part_b.py:353-360`
- Modify: `backend/app/core/ai/tools/registry.py:121-140` (`response_fields` de `devices.health`)
- Modify: `docs/modules/endpoint-health-alerting/spec.md`, después de `:108`
- Test: `backend/tests/test_health_client_default.py`. Siguen pasando sin cambios `tests/test_health_api.py`, `tests/test_global_rows_api.py:365-377` (el defecto global sigue exigiendo todos los clientes), `tests/test_client_record_checks.py:304-392` (la resolución) y `tests/test_global_writes.py:198-199` (las dos rutas ya están en `NOT_GLOBAL` como `O`, así que no se tocan).

**Interfaces:**
- Consumes:
  - `app.services.client_sites.named_client_for_new_rows(session, *, org_id, client_id, unassigned, not_found=NO_SUCH_CLIENT) -> Client` y `ClientRefusesNewRow(status_code, detail)` (`client_sites.py:179-216`): 404 «no such client», 409 «that client is archived», 422 con la frase del módulo.
  - El estrechamiento de `require_permission(code)` (`app/api/deps.py:503-515`): la petición ve solo los clientes donde la persona tiene `code`, intersectados con `X-Client-Id`.
- Produces:
  - `AssignmentCreate.client_id: UUID | None = None` y `AssignmentOut.client_id: UUID | None` (`health/schema.py`).
  - `ResolutionLevel = Literal["device", "group", "client", "org"]`, en `health/schema.py` y en `health/resolution.py`.
  - `health/resolution.py::assignment_level(assignment: HealthProfileAssignment) -> ResolutionLevel`.
  - `profiles.create_assignment(session, org_id, actor_id, *, profile_id, device_id=None, device_group_id=None, client_id: UUID | None = None)`.
  - `health/errors.py::HealthUnprocessableError(HealthValidationError)`, que `api._http_error` responde con 422.
  - `health.assignment_created` y `health.assignment_deleted` llevan `client_id=` (el de la fila) y la clave `client_id` en sus metadatos. No hay acción de auditoría nueva ni ruta nueva.
  - La herramienta de lectura `devices.health` del asistente declara `client_id` entre sus `response_fields`.

**Decisión del plan (1): la comprobación sobre el registro es el alcance de la ruta, sin un `has_permission` más.**
`require_permission("health.admin")` ya estrecha `ctx.clients` a los clientes donde la persona tiene
`health.admin` (`deps.py:503-515`), y `create_assignment` lee el cliente con `named_client_for_new_rows` en
esa sesión. Un cliente donde no lo tiene no existe para la petición (RLS de `clients`, política
`its_client_visible(id)`), así que responde el 404 de un cliente desconocido. Es el refinamiento 1 del §7
del diseño del proyecto (`docs/superpowers/specs/2026-10-23-msp-clientes-design.md`): «los registros de Z dan
404 por RLS, sin una comprobación por registro que alguien pueda olvidar». Un `has_permission(..., client_id=...)` añadido detrás sería código que ninguna petición alcanza y
que ninguna prueba puede hacer fallar. La prueba del 404 protege la lectura del cliente: sin ella, el `INSERT`
choca con RLS y la ruta responde 500.

**Decisión del plan (2): el 422 de «Sin asignar» es una clase de error nueva, `HealthUnprocessableError`.**
La salud traduce sus errores a HTTP por tipo (`api.py:252-276`) y no tenía ninguno para 422.

**Decisión del plan (3): un cliente junto a un equipo o un grupo es un 400.** Es la negativa que ya existía
para equipo y grupo juntos, con una frase nueva que nombra las cuatro formas. La frase vieja sale de `es.py`.

**Decisión del plan (4): las auditorías de asignación llevan `client_id` en los cuatro niveles.** La
restricción global lo pide para todo sujeto con cliente. La fila de un equipo o de un grupo tiene el cliente
de su padre (los disparadores de la 0231), y la del defecto global, `None`.

**Por qué se toca el registro del asistente.** `DeviceHealthOut.assignment` es un `AssignmentOut`, y la
herramienta `devices.health` (`app/core/ai/tools/registry.py:95-140`) declara, campo por campo, todo lo que su
respuesta puede traer. `scrub` bloquea el resultado entero ante un campo no declarado. Sin añadir `client_id`,
la herramienta deja de responder: la ficha de salud y la sonda de visibilidad de `act.py`, que no lee el
cuerpo, no se ven afectadas, pero la consulta del modelo sí. Lo detectan
`tests/test_ai_read_tools.py::test_declared_response_fields_match_the_live_model` (`:300-327`) y
`tests/test_ai_admin_executor.py::test_a_single_object_response_is_not_wrapped_in_items` (`:307-321`).
`client_id` no tiene forma de secreto (`app.core.secrets.looks_like_secret_key("client_id")` es `False`).

- [ ] **Step 1: Escribir las pruebas que fallan**

```python
# backend/tests/test_health_client_default.py
"""A client's default health profile through the API (phase 3 design §5.1,
plan 2026-10-31 Task 3).

`POST /health/assignments` names a device, a group, a client or nothing. A
client's default needs `health.admin` ON THAT CLIENT: the route's gate
narrows the request to the clients where the person holds it, and the client
is read in that session, so any other client does not exist for the request
(404). Unassigned is refused (422) and an archived client too (409). The
resolver already reads the client's default before the global one; these
tests reach it through the API, and the device view names the level.
"""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, Client, HealthProfileAssignment
from tests.client_scope_support import client_with_site
from tests.conftest import ApiEnv
from tests.health_fixtures import seed_profile
from tests.test_client_record_checks import _admin, _device, _holder

ASSIGNMENTS = "/api/v1/health/assignments"


async def _unassigned(env: ApiEnv, org: UUID) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        return (
            await s.execute(
                select(Client.id).where(Client.org_id == org, Client.kind == "unassigned")
            )
        ).scalar_one()


async def _defaults(env: ApiEnv, org: UUID) -> dict[UUID | None, UUID]:
    """Every default row of `org` (no device, no group), by client -> profile,
    read on the admin engine."""
    async with AsyncSession(env.admin_engine) as s:
        rows = (
            await s.execute(
                select(HealthProfileAssignment.client_id, HealthProfileAssignment.profile_id)
                .where(
                    HealthProfileAssignment.org_id == org,
                    HealthProfileAssignment.device_id.is_(None),
                    HealthProfileAssignment.device_group_id.is_(None),
                )
            )
        ).all()
    return {client: profile for client, profile in rows}


async def test_a_technician_sets_the_default_of_a_client_they_administer_and_not_another_s(
    api_env: ApiEnv,
) -> None:
    """`health.admin` on X and only `health.read` on Y: X's default is
    written (level `client`, audited with X), Y's is a 404 -- Y is outside
    the clients the route's gate narrowed the request to -- and nothing is
    written for Y."""
    org = await api_env.create_org("hcd-tech")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    profile = await seed_profile(api_env, org)
    _, tech = await _holder(
        api_env, org, {x: ("health.admin", "health.read"), y: ("health.read",)}
    )

    mine = await api_env.client.post(
        ASSIGNMENTS, headers=tech, json={"profile_id": str(profile), "client_id": str(x)}
    )
    assert mine.status_code == 201, mine.text
    body = mine.json()
    assert (body["level"], body["client_id"], body["device_id"], body["device_group_id"]) == (
        "client", str(x), None, None
    )

    theirs = await api_env.client.post(
        ASSIGNMENTS, headers=tech, json={"profile_id": str(profile), "client_id": str(y)}
    )
    assert (theirs.status_code, theirs.json()) == (404, {"detail": "no such client"})
    assert await _defaults(api_env, org) == {x: profile}

    async with AsyncSession(api_env.admin_engine) as s:
        audited = (
            await s.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org, AuditEvent.action == "health.assignment_created"
                )
            )
        ).scalar_one()
    assert audited.client_id == x
    assert audited.meta["level"] == "client"
    assert audited.meta["client_id"] == str(x)


async def test_the_selector_narrows_a_client_default_too(api_env: ApiEnv) -> None:
    """An org-admin narrowed to X by `X-Client-Id` cannot write Y's default:
    the header narrows, never widens."""
    org = await api_env.create_org("hcd-narrow")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    profile = await seed_profile(api_env, org)
    narrowed = {**(await _admin(api_env, org)), "X-Client-Id": str(x)}

    other = await api_env.client.post(
        ASSIGNMENTS, headers=narrowed, json={"profile_id": str(profile), "client_id": str(y)}
    )
    assert (other.status_code, other.json()) == (404, {"detail": "no such client"})
    own = await api_env.client.post(
        ASSIGNMENTS, headers=narrowed, json={"profile_id": str(profile), "client_id": str(x)}
    )
    assert own.status_code == 201, own.text
    assert await _defaults(api_env, org) == {x: profile}


async def test_unassigned_and_archived_clients_take_no_default(api_env: ApiEnv) -> None:
    """404 for a client nobody can see, 409 archived, 422 Unassigned (plan
    2026-10-26 C10), and nothing written for any of them -- asked by an
    org-admin, who can see all three."""
    org = await api_env.create_org("hcd-refused")
    archived, _ = await client_with_site(api_env, org, client_archived=True)
    unassigned = await _unassigned(api_env, org)
    profile = await seed_profile(api_env, org)
    admin = await _admin(api_env, org)
    for client, expected in (
        (uuid4(), (404, "no such client")),
        (archived, (409, "that client is archived")),
        (unassigned, (422, "a health profile cannot be assigned to Unassigned")),
    ):
        r = await api_env.client.post(
            ASSIGNMENTS, headers=admin, json={"profile_id": str(profile), "client_id": str(client)}
        )
        assert (r.status_code, r.json()["detail"]) == expected, r.text
    assert await _defaults(api_env, org) == {}


async def test_a_client_default_names_nothing_else(api_env: ApiEnv) -> None:
    """A client with a device or a group is two targets: refused (400) with
    the sentence that names the four shapes, before anything is read."""
    org = await api_env.create_org("hcd-both")
    x, x_site = await client_with_site(api_env, org)
    device = await _device(api_env, org, x, x_site)
    profile = await seed_profile(api_env, org)
    admin = await _admin(api_env, org)
    for extra in ({"device_id": str(device)}, {"device_group_id": str(uuid4())}):
        r = await api_env.client.post(
            ASSIGNMENTS,
            headers=admin,
            json={"profile_id": str(profile), "client_id": str(x), **extra},
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"] == (
            "an assignment names a device, a group, a client or none of them (the org "
            "default), never more than one"
        )
    async with AsyncSession(api_env.admin_engine) as s:
        assert (
            await s.execute(
                select(HealthProfileAssignment.id).where(HealthProfileAssignment.org_id == org)
            )
        ).all() == []


async def test_one_default_per_client_beside_the_global_one(api_env: ApiEnv) -> None:
    """`uq_health_assignment_client_org` (NULLS NOT DISTINCT): X's second
    default is the 400 that names the first; the global default and X's live
    side by side."""
    org = await api_env.create_org("hcd-dup")
    x, _ = await client_with_site(api_env, org)
    shared = await seed_profile(api_env, org)
    mine = await seed_profile(api_env, org)
    admin = await _admin(api_env, org)

    first = await api_env.client.post(
        ASSIGNMENTS, headers=admin, json={"profile_id": str(mine), "client_id": str(x)}
    )
    assert first.status_code == 201, first.text
    second = await api_env.client.post(
        ASSIGNMENTS, headers=admin, json={"profile_id": str(shared), "client_id": str(x)}
    )
    assert second.status_code == 400, second.text
    assert "already exists" in second.json()["detail"]
    assert first.json()["id"] in second.json()["detail"]
    global_default = await api_env.client.post(
        ASSIGNMENTS, headers=admin, json={"profile_id": str(shared)}
    )
    assert global_default.status_code == 201, global_default.text
    assert global_default.json()["level"] == "org"
    assert await _defaults(api_env, org) == {x: mine, None: shared}


async def test_a_device_resolves_to_its_client_s_default_through_the_api(
    api_env: ApiEnv,
) -> None:
    """Device -> group -> its client's default -> the global one, written
    through the API and read back on the device view, which names the level
    `client` and the client. A machine of another client takes the global
    default."""
    org = await api_env.create_org("hcd-resolve")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    x_device = await _device(api_env, org, x, x_site)
    y_device = await _device(api_env, org, y, y_site)
    shared = await seed_profile(api_env, org)
    mine = await seed_profile(api_env, org)
    admin = await _admin(api_env, org)
    for body in ({"profile_id": str(shared)}, {"profile_id": str(mine), "client_id": str(x)}):
        r = await api_env.client.post(ASSIGNMENTS, headers=admin, json=body)
        assert r.status_code == 201, r.text

    seen_x = await api_env.client.get(f"/api/v1/devices/{x_device}/health", headers=admin)
    assert seen_x.status_code == 200, seen_x.text
    assert (
        seen_x.json()["resolution_level"],
        seen_x.json()["profile"]["id"],
        seen_x.json()["assignment"]["client_id"],
    ) == ("client", str(mine), str(x))
    seen_y = await api_env.client.get(f"/api/v1/devices/{y_device}/health", headers=admin)
    assert (seen_y.json()["resolution_level"], seen_y.json()["profile"]["id"]) == (
        "org", str(shared)
    )

    listed = await api_env.client.get(ASSIGNMENTS, headers=admin)
    assert sorted((row["level"], row["client_id"]) for row in listed.json()) == sorted(
        [("org", None), ("client", str(x))]
    )


async def test_a_technician_removes_their_client_s_default_and_not_another_s(
    api_env: ApiEnv,
) -> None:
    """DELETE needs nothing new: a technician of X removes X's default; Y's
    is not theirs to see (404) and stays."""
    org = await api_env.create_org("hcd-delete")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    profile = await seed_profile(api_env, org)
    admin = await _admin(api_env, org)
    ids: dict[UUID, Any] = {}
    for client in (x, y):
        r = await api_env.client.post(
            ASSIGNMENTS, headers=admin, json={"profile_id": str(profile), "client_id": str(client)}
        )
        assert r.status_code == 201, r.text
        ids[client] = r.json()["id"]
    _, tech = await _holder(api_env, org, {x: ("health.admin", "health.read")})

    hidden = await api_env.client.delete(f"{ASSIGNMENTS}/{ids[y]}", headers=tech)
    assert hidden.status_code == 404, hidden.text
    removed = await api_env.client.delete(f"{ASSIGNMENTS}/{ids[x]}", headers=tech)
    assert removed.status_code == 204, removed.text
    assert await _defaults(api_env, org) == {y: profile}
```

Ayudantes reutilizados: `_holder` (`tests/test_client_record_checks.py:51-72`, un rol por cliente con
exactamente esos códigos), `_device` (`:75-83`), `_admin` (`:86-87`), `client_with_site`
(`tests/client_scope_support.py:19-45`) y `seed_profile` (`tests/health_fixtures.py:129-139`). `_unassigned`
copia el de `tests/test_client_scope_push.py:126-132`.

Qué protege cada una:
- La primera y la segunda fallan si `create_assignment` deja de leer el cliente con `_client_for_default`
  (Step 5): el `INSERT` con el cliente de Y choca con RLS y la ruta responde 500, no 404. Se comprobó así,
  quitando la llamada.
- La tercera falla si se quita `_client_for_default` o su traducción a 404/409/422.
- La cuarta, si desaparece la negativa de `sum(...) > 1`.
- La sexta fija `assignment_level` en el orden equipo → grupo → cliente → organización. Si
  `_assignment_out` volviera a calcularlo sin mirar `client_id`, el defecto de X saldría como `org`.

- [ ] **Step 2: Comprobar que fallan**

Run: `.venv/bin/pytest -q tests/test_health_client_default.py`
Expected: FAIL, las siete. El cuerpo todavía no admite `client_id`: `422 {"detail":[{"type":"extra_forbidden","loc":["body","client_id"],...}]}`.

- [ ] **Step 3: El esquema y el nivel**

En `backend/app/modules/health/schema.py:119`:

```python
ResolutionLevel = Literal["device", "group", "client", "org"]
```

En `backend/app/modules/health/schema.py:348-372`, `AssignmentCreate` y `AssignmentOut` quedan así:

```python
class AssignmentCreate(_StrictIn):
    """A device, a group, a client (that client's default) or none of them
    (the org default) -- never more than one.

    Typed as independent optionals rather than as a discriminated union
    because `profiles.create_assignment` owns the refusal and its message names
    all four shapes; a Pydantic union would produce a 422 that says only that
    the body matched no branch.

    `client_id` is a client's DEFAULT (phase 3 design §5.1). A device's and a
    group's assignment take their parent's client from the database trigger,
    so it is never sent beside either.
    """

    profile_id: UUID
    device_id: UUID | None = None
    device_group_id: UUID | None = None
    client_id: UUID | None = None


class AssignmentOut(BaseModel):
    id: UUID
    profile_id: UUID
    profile_name: str
    device_id: UUID | None
    device_group_id: UUID | None
    # The row's client: a client default's own, a device's or a group's
    # parent's (trigger-filled), or None for the org default.
    client_id: UUID | None
    # `device`/`group`/`client`/`org` -- §0.2's resolution level, named
    # server-side so the console never has to infer it from which id is NULL.
    level: ResolutionLevel
    created_by: UUID | None
    created_at: datetime
```

En `backend/app/modules/health/resolution.py`, `__all__` (`:64`):

```python
__all__ = [
    "MAX_GROUP_DEPTH",
    "ResolvedProfile",
    "assignment_level",
    "resolve_profile_for_device",
]
```

`:73` pasa a ser el `Literal` y la función que decide el nivel en un solo sitio:

```python
ResolutionLevel = Literal["device", "group", "client", "org"]


def assignment_level(assignment: HealthProfileAssignment) -> ResolutionLevel:
    """§0.2's level of one assignment row, decided in ONE place for the
    resolver and the API (`api._assignment_out`).

    THE ORDER IS LOAD-BEARING. A device's and a group's row carry their
    parent's `client_id` too (the 0231 triggers), so `client_id IS NOT NULL`
    alone does not make a client default: only a row with no device and no
    group does (phase 3 design §5.1)."""
    if assignment.device_id is not None:
        return "device"
    if assignment.device_group_id is not None:
        return "group"
    if assignment.client_id is not None:
        return "client"
    return "org"
```

Y `ResolvedProfile.level` (`:102-108`) la usa:

```python
    @property
    def level(self) -> ResolutionLevel:
        return assignment_level(self.assignment)
```

- [ ] **Step 4: El error 422 y la API**

Al final de `backend/app/modules/health/errors.py`:

```python
class HealthUnprocessableError(HealthValidationError):
    """The request names something that exists and is visible, but that cannot
    take this write (maps to HTTP 422).

    One case today: a default profile for the Unassigned client, the inbox
    for what has no owner (MSP clients plan 2026-10-26 C10), which is not a
    client to configure -- `client_sites.named_client_for_new_rows` answers
    it with 422 for every module, and this keeps health's answer the same.
    """
```

En `backend/app/modules/health/api.py`:

1. La importación de errores (`:179-184`) añade `HealthUnprocessableError` entre `HealthNotFoundError` y
   `HealthValidationError`.
2. `:212` pasa a ser `from app.modules.health.resolution import assignment_level, resolve_profile_for_device`.
3. En `_http_error` (`:252-276`), el docstring gana este párrafo, antes del de `HealthForbiddenError`:

```python
    `HealthUnprocessableError` is 422: the request names a client that exists
    and is visible but takes no default (Unassigned), the answer every module
    gives through `client_sites.named_client_for_new_rows`.
```

   y el cuerpo, antes del 400 final:

```python
    if isinstance(exc, HealthUnprocessableError):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
```

4. `_assignment_out` (`:381-392`):

```python
def _assignment_out(row: HealthProfileAssignment, profile_name: str) -> AssignmentOut:
    return AssignmentOut(
        id=row.id,
        profile_id=row.profile_id,
        profile_name=profile_name,
        device_id=row.device_id,
        device_group_id=row.device_group_id,
        client_id=row.client_id,
        level=assignment_level(row),
        created_by=row.created_by,
        created_at=row.created_at,
    )
```

5. `create_assignment_route` (`:842-873`) pasa el campo nuevo y lo explica. Su docstring queda así:

```python
    """One assignment per resolution level, enforced by three PARTIAL unique
    indexes (§0.2) -- NULLs do not collide in a plain unique constraint, so
    without them "which of these two applies" would be a tiebreak nobody
    chose.

    A CLIENT'S DEFAULT (`client_id`, phase 3 design §5.1) needs `health.admin`
    IN THAT CLIENT. The gate above already narrowed `ctx.clients` to the
    clients where the person holds it (and to X-Client-Id), and
    `create_assignment` reads the client in that session: any other client is
    the 404 an unknown one gets."""
```

   y la llamada:

```python
            row = await create_assignment(
                session,
                ctx.org_id,
                ctx.user_id,
                profile_id=body.profile_id,
                device_id=body.device_id,
                device_group_id=body.device_group_id,
                client_id=body.client_id,
            )
```

La ruta no cambia de permiso: sigue en `require_permission("health.admin")`, y `test_health_api.py`
(`EXPECTED_ROUTES`, `:113-115`) lo fija.

- [ ] **Step 5: El servicio**

En `backend/app/modules/health/profiles.py`:

1. `:79`: `from app.core.i18n import N_, tr`.
2. La importación de errores (`:81-85`) añade `HealthUnprocessableError`.
3. Después de `:103` (`from app.modules.health.settings import get_settings`):

```python
from app.services.client_sites import ClientRefusesNewRow, named_client_for_new_rows
```

4. En `_ASSIGNMENT_INDEX_BY_LEVEL` (`:158-165`), el defecto de un cliente usa el mismo índice que el global:

```python
    # The default: global (the org's), and per client under NULLS NOT DISTINCT
    # (migration 0231, Task 8b) -- the org's default conflicts with the org's,
    # a client's with that client's. ONE index serves both levels.
    "client": (
        "uq_health_assignment_client_org",
        ["org_id", "client_id"],
        "device_id IS NULL AND device_group_id IS NULL",
    ),
    "org": (
        "uq_health_assignment_client_org",
        ["org_id", "client_id"],
        "device_id IS NULL AND device_group_id IS NULL",
    ),
}
```

5. `:1351-1467` (`create_assignment` y `_assignment_at_level`) se sustituye entero por esto:

```python
async def _client_for_default(session: AsyncSession, org_id: UUID, client_id: UUID) -> None:
    """The client a default is filed under, read FOR SHARE as the write's
    FIRST lock, before the audit chain's (`app.services.client_sites`).

    THIS IS THE RECORD CHECK (phase 3 design §5.1): the session is the one
    `require_permission("health.admin")` narrowed to the clients where the
    person holds that code, intersected with X-Client-Id -- so a client where
    they do not hold it is not visible here, and is the 404 an unknown one
    gets. Archived is a 409 and Unassigned a 422, as for every module."""
    try:
        await named_client_for_new_rows(
            session,
            org_id=org_id,
            client_id=client_id,
            unassigned=N_("a health profile cannot be assigned to Unassigned"),
        )
    except ClientRefusesNewRow as exc:
        if exc.status_code == 404:
            raise HealthNotFoundError(exc.detail) from None
        if exc.status_code == 409:
            raise HealthConflictError(exc.detail) from None
        raise HealthUnprocessableError(exc.detail) from None


async def create_assignment(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    *,
    profile_id: UUID,
    device_id: UUID | None = None,
    device_group_id: UUID | None = None,
    client_id: UUID | None = None,
) -> HealthProfileAssignment:
    """`ON CONFLICT DO NOTHING` + `RETURNING id` against the ONE partial index
    matching this assignment's resolution level -- not check-then-insert. The
    race loser gets the same typed error a pre-check would raise, and never the
    winner's row.

    `index_elements=` + `index_where=`, never `ON CONFLICT ON CONSTRAINT`: these
    are plain partial unique INDEXES with no `pg_constraint` row, so naming them
    as a constraint fails at runtime with `InvalidObjectDefinition`.

    `client_id` alone is THAT CLIENT'S DEFAULT (phase 3 design §5.1): served to
    its machines before the org's, after their device's and group's. It names
    nothing else -- a device's or a group's row takes its parent's client from
    the 0231 triggers.
    """
    if sum(target is not None for target in (device_id, device_group_id, client_id)) > 1:
        raise HealthValidationError(
            tr(
                "an assignment names a device, a group, a client or none of them (the org "
                "default), never more than one"
            )
        )
    if client_id is not None:
        await _client_for_default(session, org_id, client_id)
    elif device_id is None and device_group_id is None:
        # The org default has no parent, so no client: a global row, written by
        # the bulk INSERT below that `app.core.global_rows`'s flush guard never
        # sees. Refused before anything is read, as `require_all_clients` is.
        refuse_unless_every_client(session, "health_profile_assignment")
    await get_profile(session, org_id, profile_id)
    await _reject_cross_org_target(
        session, org_id, device_id=device_id, device_group_id=device_group_id
    )

    if device_id is not None:
        level = "device"
    elif device_group_id is not None:
        level = "group"
    else:
        level = "client" if client_id is not None else "org"
    _, index_elements, index_where = _ASSIGNMENT_INDEX_BY_LEVEL[level]

    inserted_id = (
        await session.execute(
            pg_insert(HealthProfileAssignment)
            .values(
                org_id=org_id,
                profile_id=profile_id,
                device_id=device_id,
                device_group_id=device_group_id,
                client_id=client_id,
                created_by=actor_id,
                created_at=_utcnow(),
            )
            .on_conflict_do_nothing(
                index_elements=index_elements, index_where=text(index_where)
            )
            .returning(HealthProfileAssignment.id)
        )
    ).scalar_one_or_none()

    if inserted_id is None:
        existing = await _assignment_at_level(
            session,
            org_id,
            device_id=device_id,
            device_group_id=device_group_id,
            client_id=client_id,
        )
        raise HealthValidationError(
            tr(
                "a {level}-level health profile assignment already exists here: assignment "
                "{existing_id} points at profile {existing_profile_id}",
                level=level,
                existing_id=existing.id if existing else "?",
                existing_profile_id=existing.profile_id if existing else "?",
            )
        )

    row = (
        await session.execute(
            select(HealthProfileAssignment).where(
                HealthProfileAssignment.org_id == org_id,
                HealthProfileAssignment.id == inserted_id,
            )
        )
    ).scalar_one()
    # §0.8 clause 4's third limb, and the one that actually reads "the DEVICE'S
    # ASSIGNMENT changes". A new device-level or group-level assignment
    # OVERRIDES a less specific one (§0.2's first-match-wins), so checks from
    # the profile the device used to resolve to stop applying to it -- and their
    # alerts, whose check ids the device will now never report again, would
    # otherwise be permanent. Org-wide scope because a group or org-default
    # assignment moves a set this function has no cheaper way to enumerate; it
    # is bounded by open alerts, not by fleet size.
    await clear_alerts_for_inapplicable_checks(session, org_id, actor_id)
    await _audit(
        session,
        org_id,
        actor_id,
        "health.assignment_created",
        "health_profile_assignment",
        row.id,
        {
            "profile_id": str(profile_id),
            "device_id": str(device_id) if device_id else None,
            "device_group_id": str(device_group_id) if device_group_id else None,
            "client_id": str(row.client_id) if row.client_id else None,
            "level": level,
        },
        client_id=row.client_id,
    )
    return row


async def _assignment_at_level(
    session: AsyncSession,
    org_id: UUID,
    *,
    device_id: UUID | None,
    device_group_id: UUID | None,
    client_id: UUID | None = None,
) -> HealthProfileAssignment | None:
    query = select(HealthProfileAssignment).where(HealthProfileAssignment.org_id == org_id)
    if device_id is not None:
        query = query.where(HealthProfileAssignment.device_id == device_id)
    elif device_group_id is not None:
        query = query.where(HealthProfileAssignment.device_group_id == device_group_id)
    else:
        # A default: the client's own (`client_id`), or the org's -- the
        # global row, with no client.
        same_client = (
            HealthProfileAssignment.client_id.is_(None)
            if client_id is None
            else HealthProfileAssignment.client_id == client_id
        )
        query = query.where(
            HealthProfileAssignment.device_id.is_(None),
            HealthProfileAssignment.device_group_id.is_(None),
            same_client,
        )
    return (await session.execute(query)).scalar_one_or_none()
```

`client_id=client_id` en el `INSERT` no cambia nada para un equipo o un grupo: con `None` el disparador
`its_client_from_parent` (`migrations/versions/0231_client_scope_rls.py:388-414`) pone el cliente del padre,
como hoy. La columna no tiene `DEFAULT`, así que pasar `None` en el defecto global es lo mismo que omitirla.
`row.client_id` se lee de la fila que se vuelve a seleccionar justo después del `INSERT`.

6. `delete_assignment` (`:1470-1524`): el `RETURNING` (`:1483-1487`) añade `HealthProfileAssignment.client_id`,
   y la llamada a `_audit` (`:1510-1524`) queda así:

```python
    await _audit(
        session,
        org_id,
        actor_id,
        "health.assignment_deleted",
        "health_profile_assignment",
        assignment_id,
        {
            "profile_id": str(deleted.profile_id),
            "device_id": str(deleted.device_id) if deleted.device_id else None,
            "device_group_id": (
                str(deleted.device_group_id) if deleted.device_group_id else None
            ),
            "client_id": str(deleted.client_id) if deleted.client_id else None,
        },
        client_id=deleted.client_id,
    )
```

7. `_audit` (`:1548-1567`) acepta el cliente:

```python
async def _audit(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    action: str,
    target_type: str,
    target_id: UUID,
    metadata: dict[str, Any],
    *,
    client_id: UUID | None = None,
) -> None:
    """`client_id` is the row's client when it has one -- an assignment's --
    and None for a profile or a check, which belong to every client."""
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        metadata=metadata,
        client_id=client_id,
    )
    await session.flush()
```

`baseline.py:183` llama `create_assignment(session, org_id, None, profile_id=profile.id)`, sin cliente: sigue
creando el defecto global, como hoy.

- [ ] **Step 6: Los textos**

En `backend/app/core/i18n/es.py`, la entrada de `:1880-1883` se sustituye por estas dos. La frase vieja ya no
la usa nadie, y `tests/test_i18n_catalog.py::test_no_stale_catalogue_entries` exige quitarla:

```python
    "an assignment names a device, a group, a client or none of them (the org default), "
    "never more than one": (
        "una asignación nombra un dispositivo, un grupo, un cliente o ninguno (el valor "
        "predeterminado de la organización), nunca más de uno"
    ),
    "a health profile cannot be assigned to Unassigned": (
        "un perfil de estado no se puede asignar a «Sin asignar»"
    ),
```

«no such client» y «that client is archived» ya tienen su línea (`es.py:1045`, `:1047`).

- [ ] **Step 7: Comprobar**

Run: `.venv/bin/pytest -q tests/test_health_client_default.py tests/test_health_api.py tests/test_health_profiles.py tests/test_health_resolution.py tests/test_global_rows_api.py tests/test_global_writes.py tests/test_client_record_checks.py tests/test_i18n_catalog.py`
Expected: PASS.

Run: `.venv/bin/pytest -q tests/test_ai_read_tools.py tests/test_ai_admin_executor.py`
Expected: FAIL. `test_declared_response_fields_match_the_live_model[devices.health]` dice
`undeclared ['client_id']`, y las pruebas de `devices.health` reciben `ToolError(kind='blocked')`. Lo arregla el
paso siguiente.

- [ ] **Step 8: El registro del asistente**

En `backend/app/core/ai/tools/registry.py`, dentro de `response_fields` de `devices.health` (`:121-140`), la
línea `:126` pasa a ser:

```python
                "cleared_at", "cleared_by", "cleared_reason", "client_id", "comparator",
```

Run: `.venv/bin/pytest -q tests/test_ai_read_tools.py tests/test_ai_admin_executor.py`
Expected: PASS.

- [ ] **Step 9: La descripción MCP**

En `backend/app/mcp/descriptions/part_b.py:353-360`, las dos entradas quedan así (243 y 129 caracteres; el
límite de `tests/test_mcp_descriptions.py:62-66` es 300):

```python
    "GET /api/v1/health/assignments": (
        "Lists which health profile applies to the organization, each client, device groups "
        "or single devices, optionally for one profile."
    ),
    "POST /api/v1/health/assignments": (
        "Assigns a health profile to a device, a group, a client (that client's default, which "
        "needs health.admin in that client) or the whole organization, then pushes the changed "
        "policy as signed jobs to the affected agents. One assignment per level."
    ),
```

Run: `.venv/bin/pytest -q tests/test_mcp_descriptions.py`
Expected: PASS.

- [ ] **Step 10: La enmienda de la especificación de salud**

En `docs/modules/endpoint-health-alerting/spec.md`, después del párrafo que termina en `:108`
(«…`docs/superpowers/plans/2026-10-26-msp-clientes-fase2-modulos.md`.») y antes de «Rejected: **union of every
matching profile**», se añade este párrafo. El de 2026-10-29 no se toca: es historia.

```markdown
**Amended 2026-10-31 (MSP clients phase 3, design
`docs/superpowers/specs/2026-10-31-msp-clientes-fase3-consola-design.md` §5.1):
the API writes a client's default.** The paragraph above said none did; from
this phase `POST /health/assignments` takes `client_id`, and an assignment names
a device, a group, a client or nothing — never more than one (400). A client's
default needs `health.admin` **in that client**: the route's gate narrows the
request to the clients where the person holds it, and the client is read in
that session, so any other client is a 404, as an unknown one is. An archived
client is a 409 and Unassigned a 422: Unassigned is the inbox for what has no
owner, not a client to configure. "Nothing" is still the organisation's
default, and still needs a scope of every client. `AssignmentOut` carries the
row's `client_id` and the level `client`, and the device view
(`GET /devices/{id}/health`) reports `resolution_level: "client"` when the
client's default produced the profile. The key is
`uq_health_assignment_client_org`, unchanged: no migration. Creating or
deleting an assignment is audited with the row's client.
```

- [ ] **Step 11: Verificación completa**

Desde `backend/`:
- `.venv/bin/pytest -q` (la suite completa, más de 30 min). Expected: PASS.
- `.venv/bin/mypy app`. Expected: `Success`.
- `.venv/bin/ruff check app tests`. Expected: `All checks passed!`.

La consola no se toca en esta tarea: sus tipos (`frontend/src/modules/health/types.ts:63`, `:203`) son de la
Task 14.

- [ ] **Step 12: Commit**

```bash
git add backend/app/modules/health/schema.py backend/app/modules/health/resolution.py \
  backend/app/modules/health/errors.py backend/app/modules/health/profiles.py \
  backend/app/modules/health/api.py backend/app/core/i18n/es.py \
  backend/app/mcp/descriptions/part_b.py backend/app/core/ai/tools/registry.py \
  backend/tests/test_health_client_default.py docs/modules/endpoint-health-alerting/spec.md
git commit -m "feat(health): a client's default health profile through the API (phase 3 Task 3)"
```

---

### Task 4: El asistente reenvía `X-Client-Id`

Diseño: §5.2. Inventario: `inventory-extras.md` §B.

Las llamadas internas del asistente para técnicos (`executor_staff.py:301-304`) llevan hoy solo
`Authorization`, así que la ruta interna vuelve a admitir a la persona sin estrechar: nunca más allá de sus
permisos, pero sí más allá del cliente que el turno tenía elegido. El MCP ya reenvía la cabecera
(`app/mcp/invoke.py:242-250`, con el valor de `requested_client(request)` en `app/mcp/server.py:388-396`).
Esta tarea hace lo mismo en el asistente: `TurnContext` lleva el cliente pedido, las rutas de `ai_copilot` y
`ai_admin` lo rellenan, y cada llamada interna del turno lo reenvía, también la sonda de visibilidad de
`act.py`. Nunca amplía: la ruta interna pasa por la misma puerta con el mismo token, y la cabecera solo
estrecha.

**Files:**
- Create: `backend/tests/test_ai_client_narrowing.py`
- Modify: `backend/app/core/ai/types.py:76-86` (`TurnContext`)
- Modify: `backend/app/core/ai/personas.py:28`, `:54-75` (`LoopbackExecutor`)
- Modify: `backend/app/core/ai/executor_staff.py:48`, `:222-240` (`invoke_tool`, firma y docstring), `:280-304` (paso 5)
- Modify: `backend/app/core/ai/executor_portal.py:49`, `:123-144` (`invoke_portal_tool`, firma, docstring y una negativa)
- Modify: `backend/app/core/ai/orchestrator.py:48`, `:500-523` (`run_turn`, firma y docstring), `:552`, `:634-641`
- Modify: `backend/app/core/ai/act.py:445-452` (`_device_is_visible`)
- Modify: `backend/app/modules/ai_copilot/api.py:59`, `:330-339`
- Modify: `backend/app/modules/ai_admin/api.py:85-90`, `:740-750`, `:1073-1086` (`_turn_context`)
- Modify: `backend/tests/test_ai_admin_executor.py:65-92` (se corrige) y `:93-105` (un caso más)
- Modify: `backend/tests/test_ai_act_api.py:20-22` (importación) y `:597-610` (su petición falsa necesita `Headers`)
- Modify: `docs/modules/ai-admin/spec.md:907-908` (§4.4)
- Test: `backend/tests/test_ai_client_narrowing.py`, `backend/tests/test_ai_admin_executor.py` y `backend/tests/test_ai_act_api.py`

**Interfaces:**
- Consumes: `app.api.deps.requested_client(request: Request) -> UUID | None` (`deps.py:129-145`). Lo usa
  `app/mcp/server.py:395` y ya lo validó `auth_context` en la misma petición: un valor malformado fue un 400.
- Produces:
  - `TurnContext.requested_client: UUID | None = None` (`app/core/ai/types.py`). Sale en el `repr`; el token,
    no.
  - `LoopbackExecutor.__call__(..., requested_client: UUID | None = None)` (`app/core/ai/personas.py`).
  - `executor_staff.invoke_tool(..., requested_client: UUID | None = None)`: añade `X-Client-Id` cuando no es
    `None`.
  - `executor_portal.invoke_portal_tool(..., requested_client: UUID | None = None)`: con un valor distinto de
    `None` devuelve `ToolError(kind="forbidden")` y no hace ninguna petición.
  - `orchestrator.run_turn(..., requested_client: UUID | None = None)`.

**Decisión del plan (5): el portal gana el parámetro en su firma, pero no cambia su comportamiento.** El diseño
dice que `executor_portal.py` no cambia. Ahora bien, los dos ejecutores satisfacen el mismo protocolo
(`LoopbackExecutor`, `personas.py:54-75`), y el orquestador llama a los dos por ese tipo (`orchestrator.py:634`).
Si el protocolo nombra `requested_client` y el portal no lo acepta, `mypy` rechaza
`PersonaProfile(executor=invoke_portal_tool)` (`ai_portal/profile.py:36`, comprobado). La alternativa sería que
el orquestador pasara el argumento solo a veces, con un `**kwargs` que `mypy` no ve, o con una rama por tipo de
persona, que es justo lo que el §0.2 de la especificación del asistente rechaza.

Así que el portal acepta el parámetro y lo rechaza si no es `None`, antes de construir nada. La ruta del
portal no pasa ninguno, así que para toda petición real el portal se comporta igual que hoy: una sola
cabecera, y su prueba no cambia. Descartarlo en silencio tampoco sirve: respondería más ancho de lo pedido.

**Decisión del plan (6): la sonda de confirmar también reenvía la cabecera.** `_turn_context(request)`
(`ai_admin/api.py:1073-1086`) es el `TurnContext` de la sonda de `confirm_action`, y lo rellena con el
`X-Client-Id` de la petición de confirmar. Es «la sonda de visibilidad de `act.py`» que nombra el diseño: la
de proponer recibe el `TurnContext` del turno, y la de confirmar, este.

**Prueba que se corrige, no se borra** (diseño §5.2, restricción global «Pruebas»):
`tests/test_ai_admin_executor.py:65-92`, `test_the_request_is_a_get_to_the_tools_own_path_with_one_header`.
- Antes: `assert added == {"authorization"}`.
- Después: la prueba se parametriza (`narrowed` sí o no), se renombra a
  `test_the_request_is_a_get_to_the_tools_own_path_with_only_the_turns_headers` y exige
  `{name: request.headers[name] for name in added} == {"authorization": bearer}`, más
  `"x-client-id": str(client)` cuando el turno venía estrechado. Es igualdad exacta, nombres y valores: una
  cabecera de más o un valor cambiado la ponen en rojo.

**Otra prueba que se adapta** (no fija nada que el diseño cambie, pero su petición falsa ya no basta):
`tests/test_ai_act_api.py:597-610`, `test_the_turn_context_built_for_a_confirm_redacts_its_bearer`. Su `_Request`
tiene `headers = {"Authorization": …}`, un `dict`, y `_turn_context` lee ahora `X-Client-Id` con
`requested_client(request)`, que llama `request.headers.getlist(...)` (`deps.py:136`). Con el `dict` falla con
`AttributeError`.
- Antes: `headers = {"Authorization": "Bearer secret-token-value"}`, sin parámetros.
- Después: `headers = Headers(raw)` (`starlette.datastructures.Headers`), parametrizada `narrowed` sí o no, con
  las tres afirmaciones de antes intactas y una más: `ctx.requested_client == client`.

- [ ] **Step 1: Corregir la prueba de la cabecera única, añadir un caso hostil y adaptar la petición falsa**

En `backend/tests/test_ai_admin_executor.py`, `:65-92` se sustituye por:

```python
@pytest.mark.parametrize("narrowed", [False, True], ids=["unnarrowed", "narrowed"])
async def test_the_request_is_a_get_to_the_tools_own_path_with_only_the_turns_headers(
    api_env: Any, monkeypatch: Any, narrowed: bool
) -> None:
    """The whole of §4.4 in one recorded request: the method, the path built
    from the tool's template, and the COMPLETE header set the executor added
    -- the caller's token and, when the turn's own request carried one, its
    `X-Client-Id` (phase 3 design §5.2). Nothing else."""
    org, bearer = await _estate(api_env)
    spy = RecordingTransport(api_env.app)
    install_spy(monkeypatch, spy)
    # The device's own client -- the internal one, whose id is the org's.
    client = org.org_id if narrowed else None

    outcome = await invoke_tool(
        registry=REGISTRY,
        tool_name="devices.health",
        raw_args={"device_id": str(org.device_id)},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
        requested_client=client,
    )

    request = spy.only
    assert request.method == "GET"
    assert urlparse(str(request.url)).path == f"/api/v1/devices/{org.device_id}/health"
    # Only httpx's own transport headers survive beside them -- nothing else
    # this module or the model contributed.
    added = set(request.headers) - {"host", "accept", "accept-encoding", "connection", "user-agent"}
    expected = {"authorization": bearer} | (
        {} if client is None else {"x-client-id": str(client)}
    )
    assert {name: request.headers[name] for name in added} == expected
    # Narrowed to the device's own client, the route still answers.
    assert isinstance(outcome, ToolResult), outcome
```

Y en la lista de `test_no_model_output_can_change_the_request` (`:93-105`), después del caso
`header-injection-lowercase`, un caso más. El modelo tampoco puede poner esta cabecera:

```python
        pytest.param({"X-Client-Id": str(uuid4())}, id="client-header-injection"),
```

(`uuid4`, `pytest` y `ToolResult` ya se importan en `:16`, `:19` y `:22`.)

En `backend/tests/test_ai_act_api.py`, después de `:22` (`from sqlalchemy.ext.asyncio import AsyncSession`):

```python
from starlette.datastructures import Headers
```

y `:597-610` se sustituye por:

```python
@pytest.mark.parametrize("narrowed", [False, True], ids=["unnarrowed", "narrowed"])
def test_the_turn_context_built_for_a_confirm_redacts_its_bearer(narrowed: bool) -> None:
    """The confirm path builds a `TurnContext` for its one loopback read. Its
    repr must not carry the token: `TurnContext` is per-turn and module-private,
    but the accident §0.1 refuses -- a token in a log file forever -- is one
    `logger.debug(ctx)` away. It carries the request's `X-Client-Id`, which is
    no secret (phase 3 design §5.2)."""
    client = uuid4() if narrowed else None
    raw = {"Authorization": "Bearer secret-token-value"} | (
        {} if client is None else {"X-Client-Id": str(client)}
    )

    class _Request:
        app = None
        # A real `Headers`: `requested_client` reads it with `getlist`.
        headers = Headers(raw)

    ctx = api_module._turn_context(_Request())  # type: ignore[arg-type]
    assert ctx.bearer == "Bearer secret-token-value"
    assert "secret-token-value" not in repr(ctx)
    assert ctx.deadline > time.monotonic()
    assert ctx.requested_client == client
```

(`pytest`, `uuid4` y `time` ya se importan en `test_ai_act_api.py:16-20`.)

- [ ] **Step 2: Escribir las pruebas nuevas**

```python
# backend/tests/test_ai_client_narrowing.py
"""The staff assistant follows the console's client selector (phase 3 design
§5.2, plan 2026-10-31 Task 4).

A turn started with `X-Client-Id` runs every loopback call -- the read tools
and the act tool's device probe -- with the same header, so the inner route
runs under the turn's own scope: never wider, because the inner route passes
the same door with the same token and the header only narrows. Without it the
inner route re-admitted the person un-narrowed (bounded by their grants, but
wider than the scope the turn ran under). The portal executor takes no
selection and refuses one.

EVERY FORWARDING ASSERTION IS ON THE RECORDED REQUEST (`RecordingTransport`)
as well as on what came back, so "the header was sent" and "the route
narrowed by it" are both seen.
"""

import time
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai.executor_portal import invoke_portal_tool
from app.core.ai.executor_staff import ToolError, invoke_tool
from app.core.ai.providers import TextBlock, ToolCallBlock
from app.core.ai.tools import REGISTRY, ToolResult
from app.core.ai.types import TurnContext
from app.models import AiSettings, Device
from app.modules.ai_admin import api as admin_api
from app.modules.ai_copilot import api as copilot_api
from app.modules.ai_portal.profile import RESOLVER_PROFILE
from tests.ai_admin_fakes import (
    RecordingTransport,
    ScriptedProvider,
    ai_user,
    install_portal_spy,
    install_spy,
)
from tests.client_scope_support import client_with_site
from tests.health_fixtures import HealthOrg, seed_alert, seed_check, seed_org, seed_profile
from tests.test_ai_copilot import BASE, _world

AI = "/api/v1/ai"


def _deadline() -> float:
    return time.monotonic() + 30.0


async def _two_clients(api_env: Any, tag: str) -> tuple[HealthOrg, UUID, UUID, str]:
    """An org whose internal client and a client X each hold one machine with
    one open alert, and a person holding `ai.use` and `health.read` on every
    client. Returns `(org, x, x_device, bearer)`."""
    org = await seed_org(api_env, tag)
    x, x_site = await client_with_site(api_env, org.org_id)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        device = Device(
            org_id=org.org_id, client_id=x, site_id=x_site, hostname=f"{tag}-x",
            os_family="windows",
        )
        s.add(device)
        if await s.get(AiSettings, org.org_id) is None:
            s.add(AiSettings(org_id=org.org_id))
        await s.commit()
    profile_id = await seed_profile(api_env, org.org_id)
    check_id = await seed_check(api_env, org.org_id, profile_id)
    for device_id in (org.device_id, device.id):
        await seed_alert(
            api_env, org, profile_id=profile_id, check_id=check_id, device_id=device_id
        )
    _, sub = await ai_user(api_env, org.org_id, "ai.use", "health.read")
    bearer = (await api_env.headers(org.org_id, sub))["Authorization"]
    return org, x, device.id, bearer


def _alerted_devices(outcome: Any) -> set[str]:
    assert isinstance(outcome, ToolResult), outcome
    return {item["device_id"] for item in outcome.payload["items"]}


async def test_the_executor_forwards_the_turn_s_client_and_the_route_sees_only_it(
    api_env: Any, monkeypatch: Any
) -> None:
    """Narrowed to X: the request carries `X-Client-Id: X` and the alerts
    route answers with X's machine only. Not narrowed: no such header, and
    both machines."""
    org, x, x_device, bearer = await _two_clients(api_env, "ai-narrow-exec")
    spy = RecordingTransport(api_env.app)
    install_spy(monkeypatch, spy)

    narrowed = await invoke_tool(
        registry=REGISTRY, tool_name="health.alerts", raw_args={}, app=api_env.app,
        bearer=bearer, deadline=_deadline(), requested_client=x,
    )
    everything = await invoke_tool(
        registry=REGISTRY, tool_name="health.alerts", raw_args={}, app=api_env.app,
        bearer=bearer, deadline=_deadline(),
    )

    assert [request.headers.get("x-client-id") for request in spy.requests] == [str(x), None]
    assert _alerted_devices(narrowed) == {str(x_device)}
    assert _alerted_devices(everything) == {str(x_device), str(org.device_id)}


async def test_an_admin_turn_narrowed_by_the_selector_reads_only_that_client(
    api_env: Any, monkeypatch: Any
) -> None:
    """End to end through `POST /ai/sessions/{id}/turns`: the route puts the
    request's `X-Client-Id` on the turn, the read tool's loopback carries it,
    and the turn read one alert -- X's -- where the same turn un-narrowed
    reads two."""
    org, x, _, bearer = await _two_clients(api_env, "ai-narrow-turn")
    spy = RecordingTransport(api_env.app)
    install_spy(monkeypatch, spy)

    async def turn(headers: dict[str, str]) -> Any:
        monkeypatch.setattr(
            admin_api,
            "_provider_for",
            lambda: ScriptedProvider(
                [
                    [ToolCallBlock(id="c0", name="health.alerts", arguments={})],
                    [TextBlock(text="Two machines are alerting.")],
                ]
            ),
        )
        created = await api_env.client.post(f"{AI}/sessions", headers=headers)
        assert created.status_code == 201, created.text
        r = await api_env.client.post(
            f"{AI}/sessions/{created.json()['id']}/turns",
            headers=headers,
            json={"question": "what is alerting?"},
        )
        assert r.status_code == 200, r.text
        return r.json()["tool_calls"][0]

    narrowed = await turn({"Authorization": bearer, "X-Client-Id": str(x)})
    assert [request.headers.get("x-client-id") for request in spy.requests] == [str(x)]
    assert (narrowed["status"], narrowed["result_count"]) == ("ok", 1)

    everything = await turn({"Authorization": bearer})
    assert spy.requests[-1].headers.get("x-client-id") is None
    assert (everything["status"], everything["result_count"]) == ("ok", 2)


async def test_a_copilot_turn_forwards_the_selector_s_client(
    api_env: Any, monkeypatch: Any
) -> None:
    """The copilot's route puts the request's `X-Client-Id` on the turn too:
    a ticket of the internal client, worked with the internal client chosen
    in the selector, reads its alerts under that client."""
    world = await _world(api_env, "ai-narrow-copilot")
    spy = RecordingTransport(api_env.app)
    install_spy(monkeypatch, spy)
    headers = {**world.headers, "X-Client-Id": str(world.org_id)}
    monkeypatch.setattr(
        copilot_api,
        "_provider_for",
        lambda: ScriptedProvider(
            [
                [ToolCallBlock(id="c0", name="health.alerts", arguments={})],
                [TextBlock(text="Nothing is alerting.")],
            ]
        ),
    )
    created = await api_env.client.post(
        f"{BASE}/{world.ticket_id}/ai/sessions", headers=headers, json={"mode": "read"}
    )
    assert created.status_code == 201, created.text
    r = await api_env.client.post(
        f"{BASE}/{world.ticket_id}/ai/sessions/{created.json()['id']}/turns",
        headers=headers,
        json={"question": "why is it slow?"},
    )
    assert r.status_code == 200, r.text
    assert [request.headers.get("x-client-id") for request in spy.requests] == [
        str(world.org_id)
    ]


async def test_the_device_probe_carries_the_turn_s_client(api_env: Any) -> None:
    """`act._device_is_visible` passes the turn's selection to the executor,
    as every loopback call of the turn does."""
    from app.core.ai.act import _device_is_visible

    seen: list[dict[str, Any]] = []

    async def recording_executor(**kwargs: Any) -> ToolError:
        seen.append(kwargs)
        return ToolError(tool="devices.health", kind="blocked", message="scripted")

    x = uuid4()
    ctx = TurnContext(app=api_env.app, bearer="Bearer t", deadline=_deadline(),
                      requested_client=x)
    assert await _device_is_visible(ctx=ctx, executor=recording_executor, device_id=uuid4())
    assert [call["requested_client"] for call in seen] == [x]


async def test_the_confirm_probe_runs_under_the_confirm_s_client(
    api_env: Any, monkeypatch: Any
) -> None:
    """`POST /ai/actions/{id}/confirm` narrowed by the selector: its device
    probe carries the same `X-Client-Id` (`ai_admin.api._turn_context`)."""
    from app.core.ai.act import propose
    from app.core.client_scope import ClientScope
    from app.core.tenancy import org_session
    from tests.ai_act_fixtures import build_world

    world = await build_world(
        api_env, tag="ai-narrow-confirm",
        codes=("ai.use", "ai.act.propose", "ai.act.confirm", "devices.read", "jobs.dispatch"),
    )
    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as s:
        preview = await propose(
            s, org_id=world.org_id, binding=world.binding, ctx=world.ctx,
            executor=invoke_tool, action_key=world.action_key, device_id=world.device_id,
        )
    spy = RecordingTransport(api_env.app)
    install_spy(monkeypatch, spy)
    r = await api_env.client.post(
        f"{AI}/actions/{preview.ai_action_id}/confirm",
        json={"digest": preview.preview_digest},
        headers={**world.headers, "X-Client-Id": str(world.org_id)},
    )
    assert r.status_code == 200, r.text
    assert [request.headers.get("x-client-id") for request in spy.requests] == [
        str(world.org_id)
    ]


async def test_the_portal_executor_refuses_a_client_selection_and_sends_nothing(
    api_env: Any, monkeypatch: Any
) -> None:
    """A portal turn is never narrowed (the design leaves the portal as it
    is), so a selection reaching its executor is refused before a URL exists
    -- dropping it silently would answer wider than asked."""
    spy = RecordingTransport(api_env.app)
    install_portal_spy(monkeypatch, spy)
    outcome = await invoke_portal_tool(
        tool_name="portal.categories",
        raw_args={},
        app=api_env.app,
        bearer="Bearer t",
        deadline=_deadline(),
        registry=RESOLVER_PROFILE.tools,
        requested_client=uuid4(),
    )
    assert isinstance(outcome, ToolError)
    assert (outcome.kind, spy.requests) == ("forbidden", [])
```

Ayudantes reutilizados:
- `RecordingTransport`, `ScriptedProvider`, `ai_user`, `install_spy` e `install_portal_spy`
  (`tests/ai_admin_fakes.py:179`, `:71-92`, `:45-68`, `:309-313`, `:316-327`);
- `seed_org`, `seed_profile`, `seed_check` y `seed_alert` (`tests/health_fixtures.py`);
- `client_with_site` (`tests/client_scope_support.py:19-45`);
- `_world` y `BASE` (`tests/test_ai_copilot.py:104-163`, `:39`);
- `build_world` (`tests/ai_act_fixtures.py:49-131`).

La confirmación copia el recorrido de `tests/test_client_record_checks.py:413-424` (`_propose`) y `:458-494`.

Qué protege cada una:
- `test_the_executor_forwards…`: las líneas de `X-Client-Id` en `invoke_tool` (Step 4). Sin ellas, la
  llamada estrechada devuelve las dos alertas.
- `test_an_admin_turn…`: `requested_client=requested_client(request)` en la llamada a `run_turn` de
  `ai_admin/api.py` y la línea del orquestador que lo pasa al ejecutor (Step 5). Sin ellas, `result_count` es 2.
- `test_a_copilot_turn…`: la misma línea en `ai_copilot/api.py`.
- `test_the_device_probe…`: el argumento en `act._device_is_visible`.
- `test_the_confirm_probe…`: `_turn_context` en `ai_admin/api.py`.
- `test_the_portal_executor…`: la negativa del portal (Step 4).
- La prueba corregida del Step 1 fija el conjunto exacto de cabeceras en los dos casos, y la adaptada de
  `test_ai_act_api.py`, que `_turn_context` lee la cabecera.

Se comprobó quitando el argumento del orquestador y el de `_turn_context`: fallan las tres de extremo a extremo
y el caso `narrowed` de `test_ai_act_api.py`.

- [ ] **Step 3: Comprobar que fallan**

Run: `.venv/bin/pytest -q tests/test_ai_client_narrowing.py tests/test_ai_admin_executor.py tests/test_ai_act_api.py`
Expected: FAIL.
- `TypeError: invoke_tool() got an unexpected keyword argument 'requested_client'` en la prueba del ejecutor
  y en los dos casos de la prueba corregida, que pasa `requested_client=None` explícitamente en el caso
  `unnarrowed`. En la del portal, lo mismo con `invoke_portal_tool()`.
- `TypeError: TurnContext.__init__() got an unexpected keyword argument 'requested_client'` en la de la sonda.
- `[None] == ['…']` en las tres de extremo a extremo (turno de administración, copiloto y confirmación).
- `AttributeError: 'TurnContext' object has no attribute 'requested_client'` en los dos casos de
  `test_the_turn_context_built_for_a_confirm_redacts_its_bearer`.

El caso hostil nuevo (`client-header-injection`) ya pasa: el modelo de argumentos es cerrado.

- [ ] **Step 4: El contexto del turno, el protocolo y los dos ejecutores**

`backend/app/core/ai/types.py`, en `TurnContext` (`:76-86`):

```python
    app: Any
    bearer: str = field(repr=False)
    deadline: float = 0.0
    org_id: UUID | None = None
    user_id: UUID | None = None
    #: The client the turn's own request named in `X-Client-Id` (the console's
    #: selector), or None. A staff executor forwards it on every loopback
    #: call, so the inner route runs under the scope the turn ran under --
    #: never wider (phase 3 design §5.2, as `app.mcp.invoke` does). Filled by
    #: the staff routes only; a portal turn never carries one.
    requested_client: UUID | None = None

    def __repr__(self) -> str:
        return (
            f"TurnContext(org_id={self.org_id!r}, user_id={self.user_id!r}, "
            f"requested_client={self.requested_client!r}, "
            f"deadline={self.deadline!r}, bearer=<redacted>)"
        )
```

`backend/app/core/ai/personas.py`: después de `:28` (`from typing import Any, Literal, Protocol`), añadir
`from uuid import UUID`. `LoopbackExecutor` (`:54-75`) queda así:

```python
class LoopbackExecutor(Protocol):
    """The signature both loopback executors satisfy (§0.2).

    There are two, and there is deliberately NO "principal kind" branch inside
    one: the staff executor carries the caller's own bearer token back through
    the whole ASGI stack so `require_permission` re-runs verbatim, and a portal
    principal has no `AuthContext` to build such a request from. A branch is how
    the staff path and the portal path end up sharing a code path only one of
    them was audited for.

    `requested_client` is the turn's `TurnContext.requested_client`: the staff
    executor forwards it as `X-Client-Id`, and the portal executor, whose
    turns never carry one, refuses any other value than None (phase 3 design
    §5.2). It never comes from the model.
    """

    async def __call__(
        self,
        *,
        tool_name: str,
        raw_args: Mapping[str, Any],
        app: Any,
        bearer: str,
        deadline: float,
        registry: Mapping[str, ReadTool],
        requested_client: UUID | None = None,
    ) -> ToolResult | Any: ...
```

`backend/app/core/ai/executor_staff.py`:

1. Después de `:48` (`from urllib.parse import quote`): `from uuid import UUID`.
2. La firma de `invoke_tool` (`:222-230`) gana `requested_client: UUID | None = None` después de `registry`, y
   su docstring, tras la primera línea («Resolve -> validate -> build -> clamp -> GET -> map. In that order.»),
   este párrafo:

```python
    `requested_client` is the client the turn's own request named in
    `X-Client-Id`, from `TurnContext` -- never from the model -- and is
    forwarded unchanged (step 5).
```

3. Después de `:280` (`timeout = min(MAX_TOOL_CALL_SECONDS, remaining)`):

```python
    headers = {"Authorization": bearer}
    if requested_client is not None:
        headers["X-Client-Id"] = str(requested_client)
```

4. El comentario del paso 5 (`:282-284`, «EXACTLY ONE header is set by this module…») pasa a ser:

```python
    # 5. Issue it. The caller's own token, unmodified, so `auth_context` and
    #    `require_permission` re-run exactly as they would for the human --
    #    and, when the turn's request was narrowed by the console's selector,
    #    its `X-Client-Id`, so the route sees the clients the turn saw and no
    #    other (phase 3 design §5.2; `app.mcp.invoke` forwards it the same
    #    way). Never wider: the route narrows by it, it does not grant. Those
    #    two headers and nothing else; the model sets neither.
    #
```

   El resto del comentario («THE BROAD `except Exception` IS LOAD-BEARING…») se queda igual.
5. `:304` pasa a ser `response = await client.get(url, params=query, headers=headers)`.

`backend/app/core/ai/executor_portal.py`:

1. Después de `:49` (`from typing import Any`): `from uuid import UUID`.
2. La firma de `invoke_portal_tool` (`:123-131`) gana `requested_client: UUID | None = None` después de
   `registry`, y su docstring, tras la primera línea, este párrafo:

```python
    `requested_client` exists because `LoopbackExecutor` names it, and it is
    never forwarded: a portal turn is not narrowed by the console's selector
    (phase 3 design §5.2 -- the portal does not change), and the portal route
    passes none. Any other value than None is refused before anything else,
    so nothing can quietly drop a narrowing here.
```

3. Justo antes de `:144` (`tool = registry.get(tool_name)`):

```python
    if requested_client is not None:
        return ToolError(  # i18n-exempt: model-facing, tool result read by the model
            tool=tool_name,
            kind="forbidden",
            message="This assistant does not take a client selection.",
        )
```

   La petición del portal (`:184-189`, `headers={"Authorization": bearer}`) y su comentario no cambian.

- [ ] **Step 5: El orquestador, la sonda y las dos rutas**

`backend/app/core/ai/orchestrator.py`:

1. Después de `:48` (`from typing import Any, Literal, Protocol`): `from uuid import UUID`.
2. `run_turn` (`:500-513`) gana el último parámetro, después de `act_tools`:

```python
    requested_client: UUID | None = None,
```

   y su docstring, tras la primera línea («Spec §5 steps 4-5. Loading and persisting belong to the route.»),
   este párrafo:

```python
    `requested_client` is the client the route's own request named in
    `X-Client-Id`, or None. It goes on the turn's `TurnContext`, and every
    loopback call of the turn carries it -- the read tools' here, and the act
    tool's device probe (phase 3 design §5.2). The staff routes pass it; the
    portal route does not.
```

3. `:552`:

```python
    ctx = TurnContext(
        app=app, bearer=bearer, deadline=deadline, requested_client=requested_client
    )
```

4. La llamada al ejecutor (`:634-641`) gana el argumento:

```python
                invoked = await profile.executor(
                    tool_name=call.name,
                    raw_args=call.arguments,
                    app=app,
                    bearer=bearer,
                    deadline=deadline,
                    registry=profile.tools,
                    requested_client=ctx.requested_client,
                )
```

`backend/app/core/ai/act.py`, en `_device_is_visible` (`:445-452`):

```python
    outcome = await executor(
        tool_name=_DEVICE_PROBE.name,
        raw_args={"device_id": str(device_id)},
        app=ctx.app,
        bearer=ctx.bearer,
        deadline=ctx.deadline,
        registry=_DEVICE_PROBE_REGISTRY,
        # The turn's client selection, as every loopback call of the turn
        # carries it (phase 3 design §5.2): the probe asks the route the
        # turn's own question, under the turn's own scope.
        requested_client=ctx.requested_client,
    )
```

`backend/app/modules/ai_copilot/api.py`:

1. `:59` pasa a ser `from app.api.deps import AuthContext, requested_client, require_permission` (el orden es
   el de `ruff`).
2. La llamada a `run_turn` (`:330-339`) gana, después de `act_tools=act_tools,`:

```python
            # THE SAME CLIENT NARROWING THIS REQUEST CARRIED (phase 3 design
            # §5.2), forwarded on every loopback call of the turn. Already
            # validated by `auth_context` (a malformed one was a 400).
            requested_client=requested_client(request),
```

`backend/app/modules/ai_admin/api.py`:

1. La importación de `app.api.deps` (`:85-90`) queda así:

```python
from app.api.deps import (
    AuthContext,
    narrowed_to_every,
    requested_client,
    require_permission,
    require_permission_on_every_client,
)
```

2. La llamada a `run_turn` (`:740-750`) gana el mismo argumento y comentario que la del copiloto, después de
   `act_tools=act_tools,`.
3. `_turn_context` (`:1073-1086`):

```python
def _turn_context(request: Request) -> TurnContext:
    """A `TurnContext` for the ONE loopback read the confirm path makes: "may
    this principal see the target device" (§9.2's third part).

    The bearer comes from THIS request's own headers and is passed as an
    argument, never put on `AuthContext` -- that dataclass reaches every module,
    and a token on it is one careless `logger.debug(ctx)` from living in a log
    file forever (§0.1).

    The request's `X-Client-Id` goes with it, so the probe runs under the
    confirm's own client scope (phase 3 design §5.2).
    """
    return TurnContext(
        app=request.app,
        bearer=request.headers.get("Authorization", ""),
        deadline=time.monotonic() + DEFAULTS.wall_clock_s,
        requested_client=requested_client(request),
    )
```

`ai_portal/api.py:197-209` no cambia: no pasa `requested_client`, así que un turno del portal llega a su
ejecutor con `None`, como hoy.

- [ ] **Step 6: Comprobar**

Run: `.venv/bin/pytest -q tests/test_ai_client_narrowing.py tests/test_ai_admin_executor.py tests/test_ai_act_api.py`
Expected: PASS.

Después, las demás pruebas del asistente, que crean `TurnContext`, llaman a los ejecutores o fijan sus
fronteras:

Run: `.venv/bin/pytest -q tests/test_ai_*.py tests/test_client_record_checks.py tests/test_mcp_*.py`
Expected: PASS. `test_ai_boundaries.py:206-216` (tres sitios que construyen un cliente HTTP) sigue en tres:
esta tarea no crea ninguno. `test_ai_act_confirm.py:640-666` (la firma de `confirm_action`) no cambia: la
selección viaja dentro de `ctx`.

- [ ] **Step 7: La enmienda de la especificación del asistente**

En `docs/modules/ai-admin/spec.md`, §4.4, después de `:907-908` («The `Authorization` header is attached by
the executor from the turn's token. No other header is settable.»):

```markdown
**Amended 2026-10-31 (MSP clients phase 3, design
`docs/superpowers/specs/2026-10-31-msp-clientes-fase3-consola-design.md`
§5.2).** When the request that started the turn carried `X-Client-Id` — the
console's client selector — the staff executor sends that header too,
unchanged, on every loopback call of the turn: the read tools, the act tool's
device probe, and the confirm's probe. The inner route then runs under the
scope the turn ran under. The header narrows and never grants, and the route
passes the same door with the same token. The value comes from
`TurnContext.requested_client`, which only the staff routes fill; the model
still sets no header (§13 test 4 has a case for it). The portal executor takes
no selection and refuses one, so a contact's turn still carries exactly one
header. This is what the MCP path already did (`app/mcp/invoke.py`).
```

- [ ] **Step 8: Verificación completa**

Desde `backend/`:
- `.venv/bin/pytest -q` (la suite completa, más de 30 min). Expected: PASS.
- `.venv/bin/mypy app`. Expected: `Success`. Comprueba, entre otras cosas, que `invoke_portal_tool` sigue
  satisfaciendo `LoopbackExecutor` en `ai_portal/profile.py:36`.
- `.venv/bin/ruff check app tests`. Expected: `All checks passed!`.

No hay textos nuevos de `tr()`: el mensaje de la negativa del portal es para el modelo y lleva
`# i18n-exempt`, como los demás `ToolError` del archivo. No hay ruta nueva, ni acción de auditoría, ni cambio en
la consola.

- [ ] **Step 9: Commit**

```bash
git add backend/app/core/ai/types.py backend/app/core/ai/personas.py \
  backend/app/core/ai/executor_staff.py backend/app/core/ai/executor_portal.py \
  backend/app/core/ai/orchestrator.py backend/app/core/ai/act.py \
  backend/app/modules/ai_copilot/api.py backend/app/modules/ai_admin/api.py \
  backend/tests/test_ai_client_narrowing.py backend/tests/test_ai_admin_executor.py \
  backend/tests/test_ai_act_api.py docs/modules/ai-admin/spec.md
git commit -m "feat(ai): the staff assistant forwards the console's X-Client-Id on its loopback calls (phase 3 Task 4)"
```

---


### Task 5: `remote.supervise`, la migración 0244 y el autor del corte

Diseño: §5.3. Inventario: `.git/sdd/phase3/inventory-extras.md` §A (cada `archivo:línea` de abajo se comprobó en `main` @ `635aaa3`).

Hoy, cortar la sesión remota de otro técnico (`POST /remote/sessions/{id}/stop`) o unirse a ella (la puerta del túnel de pantalla, `authorize_tunnel`) pasa por un solo predicado, `owner_or_admin` (`backend/app/modules/remote/tunnel.py:48-64`), que pide `orgs.manage` en el cliente de la sesión. Esta tarea crea el código `remote.supervise`, lo concede con la 0244 a **todo rol que hoy tenga `orgs.manage`** (los globales y los propios de una organización, como el clon de `org-admin` que crea la edición de roles) y hace que el predicado lo pida a él. Además corrige el defecto del autor: al cortar la sesión de otro, `remote.session.ended` se audita con quien la corta como actor y con el `client_id` de la sesión (hoy: el dueño, sin cliente; `remote/api.py:336` y `remote/service.py:681-684`).

**Files:**
- Create: `backend/migrations/versions/0244_remote_supervise.py`
- Create: `backend/tests/test_remote_supervise_migration.py`
- Create: `backend/tests/test_remote_supervise.py`
- Modify: `backend/app/core/rbac.py:60-66` (comentario de `roles.read`), `:101` (código nuevo tras `remote.privacy_blank`)
- Modify: `backend/app/modules/remote/tunnel.py:35-64` (constante y `owner_or_admin`), `:82-90` (docstring de `authorize_tunnel`)
- Modify: `backend/app/modules/remote/api.py:320-338` (`stop_session`)
- Modify: `backend/app/modules/remote/service.py:659-696` (`end_session`), `:725-739` (`end_session_from_result`)
- Modify: `backend/app/core/rbac_admin.py:158-161` (solo comentario)
- Modify: `backend/app/core/i18n/es.py:4276-4278`
- Modify: `backend/app/mcp/descriptions/part_d.py:385-388`
- Modify: `backend/tests/test_rbac.py:110` (registro), `:489-499` (`test_remote_permission_grants`) y una prueba nueva
- Modify: `backend/tests/test_client_record_checks.py:655-700` (dos pruebas corregidas)
- Modify (solo comentarios): `backend/tests/test_remote_api.py:202-204,208,222,242`, `backend/tests/test_remote_tunnel.py:79`, `backend/tests/test_rbac_admin_api.py:950-952`
- Modify: `frontend/src/fixtures/core.ts:70`, `frontend/src/modules/settings/fixtures.ts:118,124-129,170`
- Modify: `docs/modules/remote-control/spec.md:280-293,302-310`, `docs/modules/remote-control/spec-screen-share.md:51,417-424`
- Test: `backend/tests/test_remote_supervise.py`, `backend/tests/test_remote_supervise_migration.py`, `backend/tests/test_rbac.py`, `backend/tests/test_client_record_checks.py`

**Interfaces:**
- Consumes: nada de otras tareas.
- Produces:
  - Código `"remote.supervise"` en `app.core.rbac.PERMISSIONS` (módulo `remote`, acción `supervise`).
  - Migración `0244_remote_supervise.py`: `revision = "0244"`, `down_revision = "0243"`.
  - `app.modules.remote.tunnel.REMOTE_SUPERVISE = "remote.supervise"` (nombre nuevo).
  - `owner_or_admin(session, *, user_id: UUID, session_technician_id: UUID, client_id: UUID) -> bool`: misma firma; pide `remote.supervise` con `client_id`.
  - `RemoteService.end_session(...)`: misma firma; su auditoría lleva `client_id` de la sesión. La ruta de corte pasa `actor_id=ctx.user_id`.
  - Frase del 403 del corte: `tr("requires the session owner or remote.supervise")` (sustituye a `"requires session owner or org-admin"`).

Decisiones del plan de esta tarea:
- **Decisión del plan:** la frase del 403 cambia a «requires the session owner or remote.supervise». La vieja nombra a org-admin, que ya no es lo que se pide. La clave vieja sale de `es.py`, porque `tests/test_i18n_catalog.py::test_no_stale_catalogue_entries` falla con una clave sin uso.
- **Decisión del plan:** `client_id` se pone en la auditoría de `end_session`, así que la llevan todos sus llamadores (la ruta, el túnel, el relé). También en `end_session_from_result`, el otro escritor de `remote.session.ended`, para que una misma acción no salga con cliente unas veces y sin él otras.
- **Decisión del plan:** `owner_or_admin` conserva su nombre: lo importan la ruta, el túnel y las pruebas. El docstring dice que «admin» es ahora el supervisor.
- **Decisión del plan:** en el fixture de Ajustes de la consola, el rol `technician` deja fuera `remote.supervise`, como hace la 0244 (un técnico no tiene `orgs.manage`).

- [ ] **Step 1: Las pruebas del código y de la migración, que fallan**

`backend/tests/test_rbac.py`, en `test_permission_registry_complete`, justo después de `"remote.privacy_blank",` (línea 110):

```python
        # Phase 3 design §5.3 (migration 0244): stopping or joining another
        # technician's session, on the session's client -- the power
        # `orgs.manage` used to carry, granted to every role that held it.
        "remote.supervise",
```

`test_remote_permission_grants` (`:489-499`) queda así (gana una línea en cada bloque):

```python
async def test_remote_permission_grants(admin_engine, app_engine) -> None:
    org_id, tech_id, _ = await _seed_user_with_role(admin_engine, "technician", "rbac-remote-t")
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        assert await has_permission(session, tech_id, "remote.control")
        assert not await has_permission(session, tech_id, "remote.recordings.read")
        # Supervising another person's session is admin tier (migration 0244).
        assert not await has_permission(session, tech_id, "remote.supervise")

    org_id, aud_id, _ = await _seed_user_with_role(admin_engine, "auditor", "rbac-remote-a")
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        assert await has_permission(session, aud_id, "remote.recordings.read")
        assert not await has_permission(session, aud_id, "remote.control")
        assert not await has_permission(session, aud_id, "remote.supervise")
```

Y, justo después, una prueba nueva:

```python
async def test_remote_supervise_is_held_where_orgs_manage_was(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Migration 0244 granted `remote.supervise` to every role holding
    `orgs.manage`: of the seeded roles, `super-admin` and `org-admin`. The
    grant is the role's, so an `org-admin` assigned on one client supervises
    on that client and nowhere else."""
    for role in ("super-admin", "org-admin"):
        org_id, user_id, _ = await _seed_user_with_role(
            admin_engine, role, f"rbac-sup-{role[:5]}"
        )
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            assert await has_permission(session, user_id, "remote.supervise"), role

    org_id, user_id, client_id = await _seed_user_with_role(
        admin_engine, "org-admin", "rbac-sup-c", client=True
    )
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        assert await has_permission(session, user_id, "remote.supervise", client_id=client_id)
        assert not await has_permission(session, user_id, "remote.supervise")
```

`test_org_admin_has_every_permission_except_the_platform_operator_codes` (`:264-290`) no se toca: compara con `set(PERMISSIONS) - PLATFORM_OPERATOR_CODES`, y la 0244 se lo concede a `org-admin` porque tiene `orgs.manage`. Si la migración no concediera, esa prueba se pondría roja; es la que vigila «org-admin».

La prueba de la migración, sobre una base propia parada en 0243 (el patrón de `tests/test_people_migration.py:78-101`; la base de la sesión ya está en la cabeza, donde un rol propio creado *antes* de la concesión no se puede sembrar):

```python
# backend/tests/test_remote_supervise_migration.py
"""Migration 0244 (phase 3 design §5.3): `remote.supervise` goes to every
role that holds `orgs.manage` when it runs -- the global `super-admin` and
`org-admin`, and a role an organisation made itself (the copy-on-write clone
of `org-admin` a role edit makes is one) -- and to no other. Its downgrade
takes the code and every grant of it away and leaves `orgs.manage` as it was;
a second upgrade grants again.

On a throwaway database stopped at 0243 (the `test_people_migration.py`
pattern): the session database is at head, where an organisation's own role
made BEFORE the upgrade can no longer be seeded.
"""

import os
import secrets
import subprocess
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.conftest import BACKEND_DIR, PgCluster, _psql

CODE = "remote.supervise"


def _alembic(url: str, *args: str) -> None:
    subprocess.run(
        [str(BACKEND_DIR / ".venv" / "bin" / "alembic"), *args],
        cwd=BACKEND_DIR,
        env=os.environ | {"ITS_ALEMBIC_URL": url},
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def db_at_0243(pg_cluster: PgCluster) -> Iterator[str]:
    """A database of its own at 0243, owned by `its_admin`; `pg_cluster`
    guarantees the roles the migrations grant to exist."""
    dbname = f"its_supervise_{secrets.token_hex(4)}"
    _psql(f'CREATE DATABASE "{dbname}" OWNER its_admin')
    url = pg_cluster.admin_url.rsplit("/", 1)[0] + f"/{dbname}"
    try:
        _alembic(url, "upgrade", "0243")
        yield url
    finally:
        _psql(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


async def _holders(engine: AsyncEngine, code: str) -> set[tuple[str, UUID | None]]:
    """(role name, the role's organisation) of every role granted `code`."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT r.name, r.org_id FROM roles r "
                "JOIN role_permissions rp ON rp.role_id = r.id "
                "JOIN permissions p ON p.id = rp.permission_id WHERE p.code = :code"
            ),
            {"code": code},
        )
        return {(row.name, row.org_id) for row in rows}


async def test_0244_grants_remote_supervise_wherever_orgs_manage_is_held(
    db_at_0243: str,
) -> None:
    org, leads, desk = uuid4(), uuid4(), uuid4()
    engine = create_async_engine(db_at_0243)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO organizations (id, name, slug, status, settings)"
                    " VALUES (:id, 'supervise', :slug, 'active', '{}')"
                ),
                {"id": org, "slug": f"sup-{org.hex[:8]}"},
            )
            # The organisation's own administrators, and -- the control -- a
            # role of its own that drives sessions but holds no `orgs.manage`.
            for role_id, name, codes in (
                (leads, "service-desk-leads", ["orgs.manage", "remote.control"]),
                (desk, "service-desk", ["remote.control"]),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO roles (id, org_id, name, description)"
                        " VALUES (:id, :org, :name, '')"
                    ),
                    {"id": role_id, "org": org, "name": name},
                )
                await conn.execute(
                    text(
                        "INSERT INTO role_permissions (role_id, permission_id)"
                        " SELECT :role, id FROM permissions"
                        " WHERE code = ANY(CAST(:codes AS text[]))"
                    ),
                    {"role": role_id, "codes": codes},
                )
        before = await _holders(engine, "orgs.manage")
        assert before == {
            ("super-admin", None), ("org-admin", None), ("service-desk-leads", org)
        }

        _alembic(db_at_0243, "upgrade", "0244")
        assert await _holders(engine, CODE) == before
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT module, action FROM permissions WHERE code = :c"), {"c": CODE}
                )
            ).one()
        assert (row.module, row.action) == ("remote", "supervise")

        _alembic(db_at_0243, "downgrade", "0243")
        async with engine.connect() as conn:
            left = (
                await conn.execute(
                    text("SELECT count(*) FROM permissions WHERE code = :c"), {"c": CODE}
                )
            ).scalar_one()
        assert left == 0
        assert await _holders(engine, "orgs.manage") == before

        _alembic(db_at_0243, "upgrade", "0244")
        assert await _holders(engine, CODE) == before
    finally:
        await engine.dispose()
```

El `downgrade` que no borrara primero las concesiones fallaría en el `DELETE FROM permissions` (la clave foránea de `role_permissions.permission_id`), y `_alembic` lanzaría `CalledProcessError`: esa es la prueba de que «su downgrade quita ambas cosas».

- [ ] **Step 2: Comprobar que fallan**

Run (desde `backend/`): `.venv/bin/pytest -q tests/test_rbac.py tests/test_remote_supervise_migration.py`
Expected: FAIL.
- `test_permission_registry_complete`: `AssertionError`, falta `remote.supervise` en `PERMISSIONS`.
- `test_remote_permission_grants` y `test_remote_supervise_is_held_where_orgs_manage_was`: `ValueError: unknown permission code 'remote.supervise'` (`rbac.py::has_permission`).
- La de la migración: `subprocess.CalledProcessError` en `alembic upgrade 0244` («Can't locate revision identified by '0244'»). Tarda un par de minutos: sube 243 migraciones.

- [ ] **Step 3: El código y la migración**

`backend/app/core/rbac.py`, después de `"remote.privacy_blank": ...` (línea 101):

```python
    # Phase 3 design §5.3 (migration 0244). Stopping or joining ANOTHER
    # technician's session, asked on the session's client (its machine's) by
    # `app/modules/remote/tunnel.py::owner_or_admin`. It rode on
    # `orgs.manage` until then, a code that says nothing about remote
    # control; 0244 granted it to every role holding `orgs.manage`, so nobody
    # lost the power unawares. Admin tier: no technician role holds it.
    "remote.supervise": (
        "Stop or join another technician's remote session on a client's devices"
    ),
```

Y el comentario de `roles.read` (`:60-66`), que deja de ser cierto, queda así:

```python
    # Access control administration (spec 2026-08-19 §1). DELIBERATELY NOT
    # `orgs.manage`: that code's description has always promised "settings,
    # users, and roles", but what it gates is the org name/settings PATCH (and
    # gated remote-tunnel authorization until migration 0244 moved it to
    # `remote.supervise`). Attaching identity lifecycle to it would
    # upgrade every current holder from "can rename the org" to "can mint a
    # fleet admin" -- a privilege escalation delivered by a description string,
    # in a migration nobody would look twice at.
```

La migración (el patrón de `0210_mcp.py:34-80` y `0232_client_permissions.py:82-109`, salvo en a quién se concede):

```python
# backend/migrations/versions/0244_remote_supervise.py
"""`remote.supervise`: stopping or joining another technician's remote session
(phase 3 design §5.3, `docs/superpowers/specs/2026-10-31-msp-clientes-fase3-consola-design.md`).

Until now the owner-or-admin rail of the stop route and the screen tunnel
(`app/modules/remote/tunnel.py::owner_or_admin`) asked for `orgs.manage` on the
session's client -- a code whose description is "organization settings, users,
and roles" and says nothing about watching or cutting somebody's remote
session. The rail now asks for a code of its own.

WHO GETS IT: EVERY ROLE THAT HOLDS `orgs.manage` WHEN THIS RUNS -- the two
global ones (`super-admin`, `org-admin`) AND every role an organisation made
for itself, the copy-on-write clone of `org-admin` that a role edit makes
(`app/core/rbac_admin.py`) included. Granting by NAME, the 0210/0232 way,
would reach only the global rows: an organisation whose administrators hold a
local clone of `org-admin` would find, after the upgrade, that nobody can stop
a runaway session any more. Granting by HOLDING is what "nobody loses the
power without being told" means. The grant is the role's, so a person holding
the role on one client holds the new code on that client -- exactly where
`orgs.manage` reached.

`technician`, `auditor`, `read-only` and `requester` hold no `orgs.manage` and
get nothing: supervising another person's session is admin tier. A role made
AFTER this upgrade gets the code only if someone ticks it.

`downgrade()` takes back every grant of the code -- the ones this upgrade made
and any a person added since in the role editor -- and then the code.
`orgs.manage` is never touched, so a downgrade hands the rail back to it with
the roles it had.

The two codes are written here, not imported: a migration that imports
application code runs against whatever that code has become.

Forward-only. `downgrade()` has never run in production (rollback is
restore-from-backup). CI runs upgrade->downgrade->upgrade.

Revision ID: 0244
Revises: 0243
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0244"
down_revision: str | None = "0243"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CODE = "remote.supervise"
#: Whoever holds this when the upgrade runs gets CODE.
HELD_WITH = "orgs.manage"


def upgrade() -> None:
    op.execute(
        "INSERT INTO permissions (id, code, module, action) "
        f"VALUES (gen_random_uuid(), '{CODE}', 'remote', 'supervise')"
    )
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT rp.role_id, granted.id FROM role_permissions rp "
        "JOIN permissions held ON held.id = rp.permission_id "
        f"JOIN permissions granted ON granted.code = '{CODE}' "
        f"WHERE held.code = '{HELD_WITH}'"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code = '{CODE}')"
    )
    op.execute(f"DELETE FROM permissions WHERE code = '{CODE}'")
```

(El SQL se probó en una transacción revertida sobre una base de pruebas en 0243: concede a los 19 roles que allí tenían `orgs.manage` y el `downgrade` los quita todos.)

- [ ] **Step 4: Comprobar que pasan**

Run: `.venv/bin/pytest -q tests/test_rbac.py tests/test_remote_supervise_migration.py tests/test_rbac_admin_api.py`
Expected: PASS. `test_rbac_admin_api.py` sirve el catálogo desde `PERMISSIONS` (`:262`, `:288`), y el código nuevo aparece en la matriz sin más.

- [ ] **Step 5: Las pruebas del supervisor, que fallan**

```python
# backend/tests/test_remote_supervise.py
"""`remote.supervise` (phase 3 design §5.3, migration 0244).

Stopping or joining ANOTHER technician's remote session needs this code on the
session's client -- its machine's. `orgs.manage` no longer gives that power:
it is organisation settings, and says nothing about remote control. A stop is
audited `remote.session.ended` with the person who stopped the session as its
actor, in the session's client; it named the owner, whoever stopped it.

The rail is one predicate (`app/modules/remote/tunnel.py::owner_or_admin`)
behind two doors, the stop route and the screen tunnel's admission
(`authorize_tunnel`). Each test goes through both real doors, not through the
predicate alone.
"""

from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, RemoteSession
from app.modules.remote.service import end_session_from_result
from app.modules.remote.tunnel import TunnelAuthError
from tests.client_scope_support import client_with_site
from tests.conftest import ApiEnv
from tests.test_client_record_checks import REMOTE, _device, _holder, _remote_session, _user
from tests.test_remote_tunnel import _authorize

NOT_OWNER = "requires the session owner or remote.supervise"


def _bearer(headers: dict[str, str]) -> str:
    return headers["Authorization"].removeprefix("Bearer ")


async def _state(env: ApiEnv, session_id: UUID) -> str:
    async with AsyncSession(env.admin_engine) as s:
        row = await s.get(RemoteSession, session_id)
        assert row is not None
        return row.state


async def _ended(env: ApiEnv, session_id: UUID) -> list[AuditEvent]:
    async with AsyncSession(env.admin_engine) as s:
        return list(
            (
                await s.execute(
                    select(AuditEvent)
                    .where(
                        AuditEvent.action == "remote.session.ended",
                        AuditEvent.target_id == str(session_id),
                    )
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        )


async def test_orgs_manage_alone_no_longer_stops_or_joins_another_s_session(
    api_env: ApiEnv,
) -> None:
    """An administrator of X who drives sessions there (`remote.control`)
    and holds `orgs.manage` on X, but not `remote.supervise`: the stop is
    the 403 with its sentence and changes nothing; the tunnel door closes
    4403.
    FALSIFIER: `owner_or_admin` asking `orgs.manage` again (`tunnel.py`)."""
    org = await api_env.create_org("sup-orgs")
    x, x_site = await client_with_site(api_env, org)
    owner = await _user(api_env, org)
    session = await _remote_session(api_env, org, await _device(api_env, org, x, x_site), owner)
    _, admin_of_x = await _holder(api_env, org, {x: ("remote.control", "orgs.manage")})

    r = await api_env.client.post(f"{REMOTE}/{session}/stop", headers=admin_of_x)
    assert (r.status_code, r.json()) == (403, {"detail": NOT_OWNER}), r.text
    assert await _state(api_env, session) == "awaiting_consent"
    assert await _ended(api_env, session) == []
    with pytest.raises(TunnelAuthError) as door:
        await _authorize(api_env, _bearer(admin_of_x), session)
    assert door.value.code == 4403


async def test_remote_supervise_on_x_stops_and_joins_x_s_sessions_and_not_y_s(
    api_env: ApiEnv,
) -> None:
    """The person drives sessions on X and on Y (`remote.control` on both)
    and supervises on X only. On Y's machine the rail refuses: the stop is a
    403 and the session is untouched, the door closes 4403. On X's machine
    the door admits them and the stop ends the session.
    FALSIFIER: drop `client_id=` from the `has_permission` call in
    `owner_or_admin` -- a grant on X alone then counts nowhere, and X's door
    closes too."""
    org = await api_env.create_org("sup-xy")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    owner = await _user(api_env, org)
    on_x = await _remote_session(api_env, org, await _device(api_env, org, x, x_site), owner)
    on_y = await _remote_session(api_env, org, await _device(api_env, org, y, y_site), owner)
    supervisor_id, supervisor = await _holder(
        api_env, org, {x: ("remote.control", "remote.supervise"), y: ("remote.control",)}
    )

    refused = await api_env.client.post(f"{REMOTE}/{on_y}/stop", headers=supervisor)
    assert (refused.status_code, refused.json()) == (403, {"detail": NOT_OWNER}), refused.text
    assert await _state(api_env, on_y) == "awaiting_consent"
    with pytest.raises(TunnelAuthError) as door:
        await _authorize(api_env, _bearer(supervisor), on_y)
    assert door.value.code == 4403

    admitted_org, admitted_user, _ = await _authorize(api_env, _bearer(supervisor), on_x)
    assert (admitted_org, admitted_user) == (org, supervisor_id)
    stopped = await api_env.client.post(f"{REMOTE}/{on_x}/stop", headers=supervisor)
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["state"] == "ended"


async def test_a_stop_is_audited_as_the_act_of_whoever_stopped_it(api_env: ApiEnv) -> None:
    """`remote.session.ended` names the person who stopped the session, in
    the session's client: the supervisor when they stop another's session
    (it named the owner before), the owner when they stop their own.
    FALSIFIER: drop `actor_id=ctx.user_id` from the stop route (`api.py`),
    or `client_id=` from `end_session`'s audit (`service.py`)."""
    org = await api_env.create_org("sup-audit")
    x, x_site = await client_with_site(api_env, org)
    device = await _device(api_env, org, x, x_site)
    owner_id, owner = await _holder(api_env, org, {x: ("remote.control",)})
    supervisor_id, supervisor = await _holder(
        api_env, org, {x: ("remote.control", "remote.supervise")}
    )
    theirs = await _remote_session(api_env, org, device, owner_id)
    own = await _remote_session(api_env, org, device, owner_id)

    r = await api_env.client.post(f"{REMOTE}/{theirs}/stop", headers=supervisor)
    assert r.status_code == 200, r.text
    r = await api_env.client.post(f"{REMOTE}/{own}/stop", headers=owner)
    assert r.status_code == 200, r.text

    (by_supervisor,) = await _ended(api_env, theirs)
    (by_owner,) = await _ended(api_env, own)
    assert (by_supervisor.actor_type, by_supervisor.actor_id, by_supervisor.client_id) == (
        "user", supervisor_id, x
    )
    assert (by_owner.actor_type, by_owner.actor_id, by_owner.client_id) == ("user", owner_id, x)
    assert by_supervisor.meta["reason"] == by_owner.meta["reason"] == "completed"


async def test_the_agent_s_end_of_a_session_is_filed_in_its_client(api_env: ApiEnv) -> None:
    """The other writer of `remote.session.ended`, the job result's
    (`end_session_from_result`): the agent is its actor, and the session's
    client its client.
    FALSIFIER: drop `client_id=` from that function's audit."""
    org = await api_env.create_org("sup-agent-end")
    x, x_site = await client_with_site(api_env, org)
    owner = await _user(api_env, org)
    device = await _device(api_env, org, x, x_site)
    session = await _remote_session(api_env, org, device, owner)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        await end_session_from_result(
            s, session_id=str(session), device_id=device, reason="helper_exited"
        )
    (ended,) = await _ended(api_env, session)
    assert (ended.actor_type, ended.actor_id, ended.client_id) == ("agent", None, x)
```

Las pruebas que fijaban `orgs.manage` se **corrigen** (no se borran), en `backend/tests/test_client_record_checks.py`:

- `:655-677`, `test_stopping_someone_else_s_session_needs_orgs_manage_on_its_client` pasa a llamarse `test_stopping_someone_else_s_session_needs_remote_supervise_on_its_client`.
  - Antes: `{x: ("remote.control",), y: ("orgs.manage",)}` daba 403 y `{x: ("remote.control", "orgs.manage")}` daba 200.
  - Ahora: `{x: ("remote.control",), y: ("remote.supervise",)}` da 403 y `{x: ("remote.control", "remote.supervise")}` da 200.
- `:680-700`, `test_the_screen_tunnel_s_rail_asks_the_session_s_client`.
  - Antes: `on_x`/`on_y` tenían `orgs.manage`.
  - Ahora tienen `remote.supervise`, y gana la aserción `admin_of_x` (`orgs.manage` en X) → `False`.

El bloque `:655-700` queda así:

```python
async def test_stopping_someone_else_s_session_needs_remote_supervise_on_its_client(
    api_env: ApiEnv,
) -> None:
    """The owner-or-supervisor rail (phase 3 design §5.3; it asked for
    `orgs.manage` until migration 0244): `remote.supervise` held on X stops
    another technician's session on X's machine; held on Y only, the old
    403, and the session is unchanged."""
    org = await api_env.create_org("cfg-remote-stop")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    owner = await _user(api_env, org)
    device = await _device(api_env, org, x, x_site)
    first = await _remote_session(api_env, org, device, owner)
    second = await _remote_session(api_env, org, device, owner)

    _, elsewhere = await _holder(
        api_env, org, {x: ("remote.control",), y: ("remote.supervise",)}
    )
    r = await api_env.client.post(f"{REMOTE}/{first}/stop", headers=elsewhere)
    assert r.status_code == 403, r.text
    async with AsyncSession(api_env.admin_engine) as s:
        assert (await s.get(RemoteSession, first)).state == "awaiting_consent"  # type: ignore[union-attr]

    _, x_supervisor = await _holder(api_env, org, {x: ("remote.control", "remote.supervise")})
    r = await api_env.client.post(f"{REMOTE}/{second}/stop", headers=x_supervisor)
    assert r.status_code == 200, r.text


async def test_the_screen_tunnel_s_rail_asks_the_session_s_client(api_env: ApiEnv) -> None:
    """`owner_or_admin`, shared by the stop route and the tunnel door: the
    person's `remote.supervise` on the session's client. `orgs.manage` there
    no longer counts (phase 3 design §5.3)."""
    from app.modules.remote.tunnel import owner_or_admin

    org = await api_env.create_org("cfg-remote-rail")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    owner = await _user(api_env, org)
    device = await _device(api_env, org, x, x_site)
    on_x, _ = await _holder(api_env, org, {x: ("remote.supervise",)})
    on_y, _ = await _holder(api_env, org, {y: ("remote.supervise",)})
    admin_of_x, _ = await _holder(api_env, org, {x: ("orgs.manage",)})
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        assert await owner_or_admin(
            s, user_id=on_x, session_technician_id=owner, client_id=x
        )
        assert not await owner_or_admin(
            s, user_id=on_y, session_technician_id=owner, client_id=x
        )
        assert not await owner_or_admin(
            s, user_id=admin_of_x, session_technician_id=owner, client_id=x
        )
        assert await owner_or_admin(s, user_id=owner, session_technician_id=owner, client_id=x)
    del device
```

Comentarios que dejan de ser ciertos (sin tocar ninguna aserción):
- `backend/tests/test_remote_api.py`:
  - `:202-204`, el docstring de `test_stop_session_requires_owner_or_admin`: «Spec §11 (phase 3 design §5.3): stop is allowed only for the session OWNER or a holder of ``remote.supervise`` on its client. A second technician who holds ``remote.control`` but neither owns the session nor holds ``remote.supervise`` gets 403; the owner and an org-admin (``remote.supervise`` by migration 0244) succeed.»
  - `:208`: `# orgs.manage` → `# remote.supervise (migration 0244)`.
  - `:222`: `(no orgs.manage)` → `(no remote.supervise)`.
  - `:242`: `(orgs.manage)` → `(remote.supervise, by migration 0244)`.
  - Su org-admin sigue cortando: tiene el código por la 0244.
- `backend/tests/test_remote_tunnel.py:79`: `# org-admin holds orgs.manage, so may drive a session it does not own` → `# org-admin holds remote.supervise (migration 0244), so may drive a session it does not own`.
- `backend/tests/test_rbac_admin_api.py:950-952`, el docstring de `test_a_caller_cannot_remove_their_own_orgs_manage`: «The second guarded code. `orgs.manage` is what gates the org-settings write (and gated remote-tunnel authorization until migration 0244); losing it is as unrecoverable as losing `roles.manage`.»

- [ ] **Step 6: Comprobar que fallan**

Run: `.venv/bin/pytest -q tests/test_remote_supervise.py tests/test_client_record_checks.py -k "remote or supervise or stop or rail"`
Expected: FAIL.
- `test_orgs_manage_alone_no_longer_stops_or_joins_another_s_session`: 200 en vez de 403.
- `test_remote_supervise_on_x_stops_and_joins_x_s_sessions_and_not_y_s`: el 403 de Y lleva la frase vieja, y X da 403.
- `test_a_stop_is_audited_as_the_act_of_whoever_stopped_it`: el supervisor no corta (403).
- `test_the_agent_s_end_of_a_session_is_filed_in_its_client`: `client_id` es `None`.
- Las dos corregidas de `test_client_record_checks.py`: 403 en vez de 200, y `owner_or_admin` da `False` para `on_x`.

- [ ] **Step 7: El predicado, la ruta y la auditoría**

`backend/app/modules/remote/tunnel.py`, después de `REMOTE_CONTROL = "remote.control"` (`:35-36`):

```python
#: The code that lets a person stop or join ANOTHER technician's session, on
#: the session's client (phase 3 design §5.3, migration 0244).
REMOTE_SUPERVISE = "remote.supervise"
```

`owner_or_admin` (`:48-64`) queda así:

```python
async def owner_or_admin(
    session: AsyncSession, *, user_id: UUID, session_technician_id: UUID, client_id: UUID
) -> bool:
    """Spec §11 ownership predicate, shared by the tunnel WS gate and the REST
    stop route so the check is defined ONCE.

    True iff ``user_id`` owns the session (is its technician) OR holds
    ``remote.supervise`` ON THE SESSION'S CLIENT, ``client_id`` -- its
    machine's (MSP clients plan 2026-10-26 Task 14, T4-C2 item 13): a
    supervisor of another client is not one here.

    It asked ``orgs.manage`` until migration 0244 (phase 3 design §5.3): a
    code whose description -- organisation settings, users and roles --
    says nothing about watching or cutting a person's remote session. 0244
    granted ``remote.supervise`` to every role that held ``orgs.manage``, so
    the people who had the power kept it; ``orgs.manage`` alone no longer
    gives it. The name stays: the "admin" here is the supervisor.

    The caller is responsible for having already established the
    ``remote.control`` permission — this predicate only decides
    owner-or-supervisor, not the base capability.
    """
    if session_technician_id == user_id:
        return True
    return await has_permission(session, user_id, REMOTE_SUPERVISE, client_id=client_id)
```

En el docstring de `authorize_tunnel` (`:82-90`), estas líneas:

```
    Requires the ``remote.control`` permission on at least one client -- the
    session is then looked up under the clients where it is held -- AND that
    the caller either owns the session (is its technician) or holds
    ``orgs.manage`` (org-admin). Raises ``TunnelAuthError`` otherwise:
      * 4401 — bad/absent token, unknown org, or unknown user;
      * 4403 — not admitted (email domain, a non-active account, or a
        contact), lacks remote.control everywhere, or not the owner and not an
        org-admin;
```

pasan a:

```
    Requires the ``remote.control`` permission on at least one client -- the
    session is then looked up under the clients where it is held -- AND that
    the caller either owns the session (is its technician) or holds
    ``remote.supervise`` on the session's client (``owner_or_admin``).
    Raises ``TunnelAuthError`` otherwise:
      * 4401 — bad/absent token, unknown org, or unknown user;
      * 4403 — not admitted (email domain, a non-active account, or a
        contact), lacks remote.control everywhere, or not the owner and
        without ``remote.supervise`` on the session's client;
```

`backend/app/modules/remote/api.py`, en `stop_session`, de `:320` a `:338` (desde el comentario «Spec §11» hasta el `except InvalidSessionState`):

```python
        # Spec §11, phase 3 design §5.3: stop is allowed only for the session
        # OWNER or someone holding ``remote.supervise`` on its client — the
        # ``remote.control`` permission alone (enforced above) is not enough.
        # Reuse the exact predicate the tunnel WS gate uses so the two paths
        # cannot diverge.
        if not await owner_or_admin(
            session,
            user_id=ctx.user_id,
            session_technician_id=row.technician_id,
            # Its machine's client (plan 2026-10-26 Task 14), loaded with the
            # row just SELECTed.
            client_id=row.client_id,
        ):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, tr("requires the session owner or remote.supervise")
            )
        try:
            # The person who stopped it is the actor (phase 3 design §5.3).
            # Left to its default, `end_session` names the session's owner,
            # which is right only when the owner is the one stopping it.
            await service.end_session(
                session, session_id=session_id, reason="completed", actor_id=ctx.user_id
            )
        except InvalidSessionState as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
```

`backend/app/modules/remote/service.py`, `end_session` (`:659-696`):

```python
    async def end_session(
        self,
        session: AsyncSession,
        *,
        session_id: UUID,
        reason: str,
        actor_type: ActorType = "user",
        actor_id: UUID | None = None,
    ) -> None:
        """End a live session. The actor defaults to the session's technician;
        the stop route names whoever stopped it (phase 3 design §5.3), and the
        gateway names the agent when the endpoint side ended it. Audited in
        the session's client."""
        row = await self._load(session, session_id)
        # Read before anything is written: a flush expires it
        # (`server_onupdate`, written by trigger), and an async session cannot
        # lazy-load it.
        client_id = row.client_id
        self._transition(row, "ended")
        ended_at = datetime.now(UTC)
        row.ended_at = ended_at
        row.end_reason = reason
        duration_s: float | None = None
        if row.started_at is not None:
            started = row.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            duration_s = (ended_at - started).total_seconds()
        if actor_type == "user" and actor_id is None:
            actor_id = row.technician_id
        await audit_service.record(
            session,
            org_id=row.org_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action="remote.session.ended",
            target_type="session",
            target_id=str(row.id),
            metadata={
                "device_id": str(row.device_id),
                "reason": reason,
                "duration_s": duration_s,
            },
            client_id=client_id,
        )
        await session.flush()
        await self._terminate(row)
```

`end_session_from_result` (`:725-739`): después de `if row is None or row.state == "ended": return`, lee el cliente y lo pasa a la auditoría:

```python
    if row is None or row.state == "ended":
        return
    # Read before the writes below (phase 3 design §5.3: the same action, the
    # same client, whichever writer records it).
    client_id = row.client_id
    row.state = "ended"
    row.ended_at = datetime.now(UTC)
    row.end_reason = reason
    await audit_service.record(
        session,
        org_id=row.org_id,
        actor_type="agent",
        actor_id=None,
        action="remote.session.ended",
        target_type="session",
        target_id=str(row.id),
        metadata={"device_id": str(row.device_id), "reason": reason, "source": "job_result"},
        client_id=client_id,
    )
    await session.flush()
```

`backend/app/core/i18n/es.py:4276-4278`: la entrada

```python
    "requires session owner or org-admin": (
        "requiere ser el propietario de la sesión o administrador de la organización"
    ),
```

se sustituye por:

```python
    "requires the session owner or remote.supervise": (
        "requiere ser el propietario de la sesión o tener remote.supervise"
    ),
```

`backend/app/mcp/descriptions/part_d.py:385-388`:

```python
    "POST /api/v1/remote/sessions/{session_id}/stop": (
        "Ends a remote session. Only the session's owner, or someone holding remote.supervise "
        "on its client, may do so; refused with 409 if it has already ended."
    ),
```

`backend/app/core/rbac_admin.py:158-161` (solo comentario):

```python
# Removing one of these from a role the CALLER holds strands them: `roles.manage`
# gates this surface, `orgs.manage` gates the org-settings write. Neither is
# recoverable from the console once lost, only by SQL against production.
# (`orgs.manage` also gated remote-tunnel authorization until migration 0244
# moved that to `remote.supervise`, which is not guarded: whoever holds
# `roles.manage` can always grant it back.)
```

- [ ] **Step 8: Comprobar que pasan**

Run: `.venv/bin/pytest -q tests/test_remote_supervise.py tests/test_client_record_checks.py tests/test_remote_api.py tests/test_remote_tunnel.py tests/test_remote_service.py tests/test_remote_screen_api.py tests/test_remote_screen_relay.py tests/test_rbac.py tests/test_i18n_catalog.py tests/test_mcp_descriptions.py`
Expected: PASS.

- [ ] **Step 9: Consola (fixtures) y especificaciones**

`frontend/src/fixtures/core.ts:70`:

```ts
  "remote.control", "remote.policy.manage", "remote.recordings.read", "remote.supervise", "reporting.read",
```

`frontend/src/modules/settings/fixtures.ts`:
- `:118`, la misma línea que en `core.ts`.
- `VERB` (`:124-129`): la última línea `wake: "Wake", review: "Review",` pasa a `wake: "Wake", review: "Review", supervise: "Supervise",`.
- El rol `technician` (`:170`) deja fuera el código nuevo, como la 0244:

```ts
    // Supervising another person's session is admin tier (migration 0244).
    codes: [
      ...byModule("devices", "patch", "deploy", "jobs", "health", "remote", "backup").filter(
        (code) => code !== "remote.supervise",
      ),
      "ticket.read", "ticket.write", "ai.use",
    ],
```

`docs/modules/remote-control/spec.md`:
- Tras la última línea del §10 («scope is enforced by the same device-group scoping used by `jobs.dispatch`.», `:280`), un bloque nuevo:

```
> **Amended 2026-10-31 (phase 3 design §5.3, migration 0244):
> `remote.supervise`** — stop or join ANOTHER technician's session, asked on
> the session's client (its machine's). The owner-or-admin rail
> (`tunnel.py::owner_or_admin`, shared by `POST /sessions/{id}/stop` and the
> screen tunnel) asked `orgs.manage` until then; it now asks this code, and
> `orgs.manage` no longer carries the power. 0244 granted it to every role that
> held `orgs.manage` when it ran — `super-admin`, `org-admin` and any role an
> organisation made itself — so nobody lost it unawares. `technician`,
> `auditor` and `read-only` do not hold it.
```

- En el §11, la viñeta de `:293-294` pasa a: «- `POST /sessions/{id}/stop` (`remote.control`, own session or `remote.supervise` on the session's client) — transitions to `ended (completed)`; drops the relay tunnel. Audited `remote.session.ended` with whoever stopped it as the actor.»
- Al final del párrafo del §12 (después de «…these records are tamper- **and** forge-evident.», `:310`): «Amended 2026-10-31 (phase 3 design §5.3): `remote.session.ended` carries the session's client, and its actor is the person who stopped the session (it named the owner even when another person stopped it).»

`docs/modules/remote-control/spec-screen-share.md`:
- `:51`: `(user JWT, RBAC, owner check)` → `(user JWT, RBAC, owner or supervisor check)`.
- Tabla del §3.10 (`:417-423`): una fila nueva tras la de `remote.policy.manage`:

```
| `remote.supervise` | **new** (2026-10-31, migration 0244) | stopping or joining another technician's session, on the session's client | super-admin, org-admin, and every role that held `orgs.manage` |
```

- Y justo después de la tabla: «The viewer WebSocket and `POST /sessions/{id}/stop` admit the session's owner or a holder of `remote.supervise` on the session's client (phase 3 design §5.3); `orgs.manage` gave that until migration 0244.»

- [ ] **Step 10: Verificación completa**

Desde `backend/` (con `pg_ctlcluster 16 main start; redis-cli ping || redis-server --daemonize yes`, y en la worktree `export PYTHONPATH=$PWD` y el `ITS_TEST_REDIS_URL` que asigne el coordinador):
- `.venv/bin/pytest -q tests/test_remote_supervise.py tests/test_remote_supervise_migration.py tests/test_rbac.py tests/test_rbac_admin_api.py tests/test_client_record_checks.py tests/test_remote_api.py tests/test_remote_tunnel.py tests/test_i18n_catalog.py tests/test_mcp_descriptions.py tests/test_mcp_catalog.py` → PASS.
- `.venv/bin/pytest -q` (la suite completa, más de 30 min) → PASS.
- `.venv/bin/mypy app` → sin errores.
- `.venv/bin/ruff check app tests` → limpio.

Si ruff ordena distinto algún import, `ruff check --fix` sobre ese archivo.

Desde `frontend/`: `npx vitest run`, `npx tsc -b` y `npm run build` → PASS.

- [ ] **Step 11: Commit**

```bash
git add backend/app/core/rbac.py backend/app/core/rbac_admin.py backend/app/core/i18n/es.py \
  backend/app/mcp/descriptions/part_d.py backend/app/modules/remote/tunnel.py \
  backend/app/modules/remote/api.py backend/app/modules/remote/service.py \
  backend/migrations/versions/0244_remote_supervise.py \
  backend/tests/test_rbac.py backend/tests/test_rbac_admin_api.py \
  backend/tests/test_client_record_checks.py backend/tests/test_remote_api.py \
  backend/tests/test_remote_tunnel.py backend/tests/test_remote_supervise.py \
  backend/tests/test_remote_supervise_migration.py \
  frontend/src/fixtures/core.ts frontend/src/modules/settings/fixtures.ts \
  docs/modules/remote-control/spec.md docs/modules/remote-control/spec-screen-share.md
git commit -m "feat(remote): remote.supervise gates stopping and joining another's session; 0244 grants it wherever orgs.manage was (phase 3 Task 5)"
```

Pruebas corregidas a propósito (para el informe):
- `tests/test_client_record_checks.py:655` (renombrada) y `:680`: de `orgs.manage` a `remote.supervise`, más la aserción de que `orgs.manage` solo ya no basta.
- `tests/test_rbac.py:110`: el registro gana el código.
- Comentarios en `tests/test_remote_api.py`, `tests/test_remote_tunnel.py` y `tests/test_rbac_admin_api.py`.

---

### Task 6: Despliegues (`execute` 404) y respaldos (cliente en la auditoría, retirar un repositorio). Solo backend

Diseño: §5.4 y §5.5 (la consola de «Retirar» es la Task 14). Inventario: `.git/sdd/phase3/inventory-extras.md` §D (cada `archivo:línea` de abajo se comprobó en `main` @ `635aaa3`).

Tres cosas:
1. `POST /deploy/deployments/{id}/execute` responde 404 «no such deployment» cuando el despliegue no existe o no se ve, como el GET. Hoy da 200 `{"job_ids": []}`: `deploy/api.py:746-749` y `dispatch.py:182-183`.
2. Diez auditorías de respaldo llevan el cliente que ya tienen a mano. `backup.offsite_gc` queda sin cliente, documentado.
3. Una ruta para retirar un repositorio. Un repositorio retirado:
   - no recibe respaldos nuevos (el despacho lo rechaza y lo audita);
   - no recibe permisos de deduplicación;
   - no lo nombra ninguna política;
   - sigue pudiéndose restaurar.

**Files:**
- Create: `backend/tests/test_backup_audit_clients.py`
- Create: `backend/tests/test_backup_repository_retire.py`
- Modify: `backend/app/modules/deploy/api.py:746-749` (`execute_deployment`)
- Modify: `backend/app/modules/backup/models.py:34-35` (constantes de estado) y `:62` (comentario de `state`)
- Modify: `backend/app/modules/backup/repository.py:1-28` (docstring e imports) y final del archivo (`RetireRefused`, `retire_repository`)
- Modify: `backend/app/modules/backup/api.py:61` (import), después de `:318` (ruta nueva)
- Modify: `backend/app/modules/backup/clients.py:24-28` (orden de bloqueos), `:116-132` (`policy_repository`)
- Modify: `backend/app/modules/backup/dispatch.py:15-40` (docstring), `:253`, `:289-290`, `:334-376`, `:457-463`
- Modify: `backend/app/modules/backup/read.py:54`, `:81-107`, `:197-198`
- Modify: `backend/app/modules/backup/dedup.py:128-148` (`refusal_client` y `_audit_refusal`)
- Modify: `backend/app/modules/backup/restore.py:196-212`, `:272-273`, `:313-327`
- Modify: `backend/app/modules/backup/test_restore.py:156-170`
- Modify: `backend/app/modules/backup/gc.py:205-212`, `:245-255`
- Modify: `backend/app/modules/backup/offsite.py:801-815`, `:820-848`, `:1031-1043`
- Modify: `backend/app/modules/backup/reconcile.py:64-99`
- Modify: `backend/app/core/i18n/es.py:4173` (tres claves nuevas)
- Modify: `backend/app/mcp/descriptions/part_b.py:212-216`, `:226-229`, `:238`, `:253-256`
- Modify: `backend/tests/test_client_scope_deployments.py:311-312` (prueba corregida) y una prueba nueva
- Modify: `frontend/src/i18n/en/auditVocabulary.ts:92`, `frontend/src/i18n/es/auditVocabulary.ts:89`
- Modify: `docs/modules/backup/spec.md:177` y `:757-758`, `docs/modules/app-deploy/spec.md:389-390`
- Test: las dos nuevas, más `test_client_scope_deployments.py`, `test_client_scope_backup.py`, `test_backup_dedup.py`, `test_backup_read_query.py`, `test_backup_offsite.py`, `test_backup_reconcile.py`, `test_backup_test_restore.py`, `test_backup_gc.py`, `test_backup_restore_result.py` y `test_backup_api.py`.

**Interfaces:**
- Consumes: nada de otras tareas.
- Produces:
  - `POST /api/v1/deploy/deployments/{deployment_id}/execute`: 404 `tr("no such deployment")` si no existe o no se ve; nada se escribe.
  - `POST /api/v1/backup/repositories/{repository_id}/retire` → `RepositoryOut` con `state == "retired"`.
    - Permiso: `require_permission("backup.policy.manage")`.
    - 404 `tr("no such repository")`.
    - 409 `tr("an enabled backup policy still uses this repository; switch it off or point it at another repository first")`.
    - 409 `tr("this repository is already retired")`.
  - `PUT /api/v1/backup/policies` gana un 409 `tr("that repository is retired")`.
  - Auditoría `backup.repository_retired`: `target_type="backup_repository"`, `metadata={"name": …}`, `client_id` del repositorio.
  - Motivo de rechazo del despacho `RETIRED_REPOSITORY = "repository_retired"` (`app.modules.backup.dispatch`).
  - `app.modules.backup.models.REPOSITORY_ACTIVE = "active"` y `REPOSITORY_RETIRED = "retired"`.
  - `app.modules.backup.repository.RetireRefused(status_code: int, detail: str)` y `async def retire_repository(session, *, org_id: UUID, actor_id: UUID, repository_id: UUID) -> BackupRepository`.
  - `app.modules.backup.dedup.refusal_client(session, *, org_id: UUID, device_id: UUID, job_id: str) -> UUID | None`.
  - `app.modules.backup.read.READABLE_STATES: frozenset[str]`.
  - `app.modules.backup.dispatch._refuse_repository` (renombrada desde `_refuse_foreign_repository`, gana `reason`).
  - Ayudas de prueba `tests/test_backup_audit_clients.py::_chunk_query` y `::_read_query`.

Decisiones del plan de esta tarea:
- **Decisión del plan:** ninguna política puede nombrar un repositorio retirado. `policy_repository` responde 409 «that repository is retired». Queda la excepción que `upsert_policy` ya hace: apagar una política guardada tal como está. Así el «409 mientras una política activa lo nombre» no se esquiva volviendo a apuntar la política después. Para que no se crucen un retiro y una escritura de política:
  - `policy_repository` lee el repositorio `FOR SHARE`;
  - `retire_repository` lo tiene `FOR NO KEY UPDATE`.
  - El diseño solo pedía que el editor de la consola no ofreciera los retirados. La consola no protege nada, y el MCP escribe políticas sin consola.
- **Decisión del plan:** retirar un repositorio ya retirado es 409 «this repository is already retired», sin segundo evento. Es el patrón de parar una sesión ya terminada.
- **Decisión del plan:** «`backup.policy.manage` en el cliente del repositorio» lo impone el alcance de la ruta (`require_permission("backup.policy.manage")`, RLS), sin otra llamada a `has_permission`. Por eso el repositorio de otro cliente es 404 y no 403.
- **Decisión del plan:** el 409 por política activa mira las políticas que la petición ve. Una política de otro cliente que nombre el repositorio solo puede ser una que dejó la 0242, y el despacho ya la rechaza como de otro cliente.
- **Decisión del plan:** sobre `backup.read_refused` y `dedup_refused`, el diseño dice «cuando el trabajo se resolvió». Este plan lo lee como: el agente nombró un trabajo de esta organización cuyo objetivo es **este** equipo. Ese trabajo tiene el cliente del equipo (0231).
  - Un id mal formado, un trabajo inexistente o el trabajo de otro equipo quedan sin cliente.
  - Lo resuelve `refusal_client`, llamada dentro de los dos `_audit_refusal`: una lectura más, solo en los rechazos.
- **Decisión del plan:** el despacho rechaza solo `state == "retired"`. Otros valores que nadie escribe siguen como hoy.
- **Decisión del plan:** `_record_missed` no cambia. Un disparo vencido sobre un repositorio retirado sigue escribiendo su fila `missed`. Registra que el respaldo no ocurrió, y el rechazo auditado dice por qué.

- [ ] **Step 1: `execute`, las pruebas que fallan**

`backend/tests/test_client_scope_deployments.py:311-312` se **corrige**:
- Antes: `assert (run.status_code, run.json()) == (200, {"job_ids": []}), run.text`
- Ahora: `assert (run.status_code, run.json()) == (404, {"detail": "no such deployment"}), run.text`

El resto de la prueba no cambia: el despliegue sigue `pending` y no hay trabajos.

Y, justo después de esa prueba, una nueva:

```python
async def test_executing_a_deployment_that_does_not_exist_is_a_404(api_env: ApiEnv) -> None:
    """Phase 3 design §5.4: the answer the GET gives, and no job -- not a
    200 with an empty list, which read as "it ran on nothing".
    FALSIFIER: the `deployment is None` raise in `execute_deployment`."""
    org = await api_env.create_org("d-exec-missing")
    admin = await _admin(api_env, org)
    run = await api_env.client.post(f"{DEPLOYMENTS}/{uuid4()}/execute", headers=admin)
    assert (run.status_code, run.json()) == (404, {"detail": "no such deployment"}), run.text
    async with AsyncSession(api_env.admin_engine) as s:
        assert (
            await s.execute(select(func.count()).select_from(Job).where(Job.org_id == org))
        ).scalar_one() == 0
```

Run: `.venv/bin/pytest -q tests/test_client_scope_deployments.py -k "runs_only or does_not_exist"`
Expected: FAIL, dos veces `(200, {'job_ids': []}) != (404, …)`.

- [ ] **Step 2: `execute`, el código**

`backend/app/modules/deploy/api.py`, en `execute_deployment`, el bloque `:746-749`:

```python
        if deployment is not None:
            refusal = await deployment_refusal(
                session, ctx.org_id, deployment, actor_id=ctx.user_id
            )
```

pasa a:

```python
        if deployment is None:
            # Phase 3 design §5.4: a deployment this request cannot see -- of
            # another client, or none at all -- is the GET's 404, not a 200
            # with no jobs. Nothing has been read or written past this point.
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such deployment"))
        refusal = await deployment_refusal(
            session, ctx.org_id, deployment, actor_id=ctx.user_id
        )
```

La clave ya existe en `es.py:4240`.

`backend/app/mcp/descriptions/part_b.py:253-256`:

```python
    "POST /api/v1/deploy/deployments/{deployment_id}/execute": (
        "Dispatches signed install, update or uninstall jobs to every device the deployment "
        "targets. Changes software on those endpoints. Returns the job ids. A deployment that "
        "does not exist or is not visible is a 404."
    ),
```

`docs/modules/app-deploy/spec.md:389-390`: `POST /deployments/{id}/execute` (`deploy.execute`, computes `now=datetime.now(UTC)` in the handler) pasa a: `POST /deployments/{id}/execute` (`deploy.execute`, computes `now=datetime.now(UTC)` in the handler; a deployment that does not exist or that the caller's clients cannot see is a 404 «no such deployment», like the GET — amended 2026-10-31, phase 3 design §5.4).

Run: `.venv/bin/pytest -q tests/test_client_scope_deployments.py tests/test_deploy_api.py tests/test_deploy_window.py tests/test_change_gates.py`
Expected: PASS.

- [ ] **Step 3: Auditorías de respaldo con cliente, las pruebas que fallan**

```python
# backend/tests/test_backup_audit_clients.py
"""Every backup audit that has its record's client at hand names it (phase 3
design §5.5).

Phase 2 filed the repository, policy and dispatch events under their client
(`test_client_scope_backup.py`, the exact sets near its end). These are the ones
it left without, each with the client of the row it already holds: the restore
and its result, the automatic test restore, the verification, the off-site copy
and its failure, and the two reconciliations.

The refused dedup and read exchanges name the client when the agent named a
job of its own device -- a job's client is its target's (migration 0231) --
and none otherwise: there is nothing to take it from. `backup.offsite_gc`
stays without one: it sums up every target, repository and client of the
organisation.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.gateway.protocol import BackupChunkQueryMsg, BackupReadQueryMsg
from app.models import BackupPoint, BackupRun, Job, RestoreJob
from app.models.core import JobStatus
from app.modules.backup.dedup import answer_chunk_query
from app.modules.backup.gc import verify_point
from app.modules.backup.offsite import record_copy_failure
from app.modules.backup.read import answer_read_query
from app.modules.backup.reconcile import reconcile_stuck
from app.modules.backup.repository import open_repo_key
from app.modules.backup.restore import ingest_restore_result
from app.modules.backup.schema import BackupRestoreResult
from app.modules.backup.test_restore import dispatch_due_test_restores
from tests.backup_dedup_fixtures import SEED_NOW
from tests.backup_offsite_fixtures import OffsiteOrg, add_point, add_proven_key, add_target
from tests.client_scope_support import client_with_site
from tests.conftest import ApiEnv
from tests.test_backup_dispatch import _FakeJobEngine
from tests.test_backup_offsite import _replicate_all, aws, vault  # noqa: F401  (fixtures)
from tests.test_backup_read_query import seed_restore
from tests.test_client_scope_backup import (
    _device,
    _events,
    _group,
    _policy,
    _sealed_point,
    _service_repo,
    _user_id,
)


def _chunk_query(job_id: UUID | str) -> BackupChunkQueryMsg:
    """A dedup query naming `job_id` and nothing to partition."""
    return BackupChunkQueryMsg(
        v=1, kind="backup_chunk_query", job_id=str(job_id), query_id="q1", check=[], stored=[]
    )


def _read_query(job_id: UUID | str) -> BackupReadQueryMsg:
    """A read query naming `job_id`, for one object of the repository."""
    return BackupReadQueryMsg(
        v=1, kind="backup_read_query", job_id=str(job_id), query_id="q1",
        suffixes=["chunks/ab/abcd"],
    )


async def test_a_restore_its_result_and_a_verification_name_the_point_s_client(
    api_env: ApiEnv, aws: Any
) -> None:
    """The automatic test restore of a point of X's device -- its dispatch
    (`backup.restore_dispatched`, `backup.test_restore_dispatched`), its
    result -- and a verification of the same point: all four in X.
    FALSIFIER: drop `client_id=` from any one of the four audits
    (`restore.py` twice, `test_restore.py`, `gc.py`)."""
    store, _s3 = aws
    org = await api_env.create_org("bk-aud-restore")
    x, x_site = await client_with_site(api_env, org)
    actor = await _user_id(api_env, org)
    vault = api_env.app.state.vault
    x_repo = await _service_repo(api_env, org, x, actor)
    group = await _group(api_env, org, x)
    await _policy(api_env, org, x_repo, group=group)
    device = await _device(api_env, org, x, x_site, group)
    point = await _sealed_point(api_env, store, vault, org, device, x_repo,
                                taken_at=datetime.now(UTC) - timedelta(hours=1), legacy=False)
    seeded = {e.id for e in await _events(api_env, org)}
    now = datetime.now(UTC)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        ((job_id, _device_id, _envelope),) = await dispatch_due_test_restores(
            s, _FakeJobEngine(), vault, store, org_id=org, now=now
        )
        job = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()
        await ingest_restore_result(
            s, job,
            BackupRestoreResult(state="success", restored_bytes=1, verified=True, mismatch=[]),
        )
        assert await verify_point(s, store, vault, org_id=org, backup_point_id=point, now=now)
    events = [e for e in await _events(api_env, org) if e.id not in seeded]
    assert sorted(((e.action, e.client_id) for e in events), key=repr) == sorted([
        ("backup.restore_dispatched", x),
        ("backup.test_restore_dispatched", x),
        ("backup.restore_result", x),
        ("backup.verify", x),
    ], key=repr)


async def test_the_reconciliations_name_the_device_s_client(api_env: ApiEnv) -> None:
    """A run and a restore of X's device left `running` by a failed job:
    the sweep fails both and files each in X.
    FALSIFIER: drop `client_id=` from either audit in `reconcile.py`."""
    org = await api_env.create_org("bk-aud-reconcile")
    x, x_site = await client_with_site(api_env, org)
    actor = await _user_id(api_env, org)
    x_repo = await _service_repo(api_env, org, x, actor)
    device = await _device(api_env, org, x, x_site)
    now = datetime.now(UTC)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        jobs = [
            Job(org_id=org, type=kind, target_type="device", target_id=device, params={},
                nonce=uuid4().hex, issued_at=now - timedelta(hours=2),
                expires_at=now + timedelta(hours=1), status=JobStatus.failed,
                result={"state": "failed", "error": "disk"})
            for kind in ("backup.run", "backup.restore")
        ]
        s.add_all(jobs)
        await s.flush()
        done = BackupRun(org_id=org, device_id=device, repository_id=x_repo, state="success",
                         actor_type="system", started_at=now - timedelta(days=1))
        s.add(done)
        await s.flush()
        point = BackupPoint(org_id=org, device_id=device, repository_id=x_repo,
                            backup_run_id=done.id, taken_at=now - timedelta(days=1),
                            manifest_ref="m", size=1, state="complete")
        s.add(point)
        await s.flush()
        s.add_all([
            BackupRun(org_id=org, device_id=device, repository_id=x_repo, job_id=jobs[0].id,
                      state="running", actor_type="system", started_at=now - timedelta(hours=2)),
            RestoreJob(org_id=org, device_id=device, backup_point_id=point.id,
                       job_id=jobs[1].id, selection={"paths": []}, target={}, mode="restore",
                       state="running", actor_type="system"),
        ])
        await s.commit()
    seeded = {e.id for e in await _events(api_env, org)}
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        assert await reconcile_stuck(s, org, now=now) == {"runs": 1, "restores": 1}
    events = [e for e in await _events(api_env, org) if e.id not in seeded]
    assert sorted(((e.action, e.client_id) for e in events), key=repr) == sorted([
        ("backup.run_reconciled", x),
        ("backup.restore_reconciled", x),
    ], key=repr)


async def test_an_off_site_copy_and_its_failure_name_the_point_s_client(
    api_env: ApiEnv, aws: Any
) -> None:
    """A point of X's device copied off site (`backup.replicated`) and a
    failed copy of it (`backup.replication_failed`, which reads the point's
    client: a copy written there for the first time has none yet): both in X.
    FALSIFIER: drop `client_id=` from either audit in `offsite.py`."""
    store, _s3 = aws
    org = await api_env.create_org("bk-aud-offsite")
    x, x_site = await client_with_site(api_env, org)
    actor = await _user_id(api_env, org)
    vault = api_env.app.state.vault
    x_repo = await _service_repo(api_env, org, x, actor)
    device = await _device(api_env, org, x, x_site)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        key = await open_repo_key(s, vault, org_id=org, repository_id=x_repo)
    world = OffsiteOrg(org, actor, device, x_repo, key, b"")
    await add_proven_key(api_env.app_engine, world)
    target = await add_target(api_env.app_engine, vault, world)
    point, _ = await add_point(api_env.app_engine, store, world, {"/srv/a.txt": [b"alpha" * 10]})

    assert await _replicate_all(api_env.app_engine, vault, store, world, datetime.now(UTC)) == [
        True
    ]
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        await record_copy_failure(s, org_id=org, point_id=point, target_id=target,
                                  error=RuntimeError("provider unreachable"))
    events = await _events(api_env, org, "backup.replicated", "backup.replication_failed")
    assert [(e.action, e.target_id, e.client_id) for e in events] == [
        ("backup.replicated", str(point), x),
        ("backup.replication_failed", str(point), x),
    ]


async def test_a_refused_exchange_names_the_client_of_its_own_device_s_job(
    api_env: ApiEnv, aws: Any, vault: LocalVaultService
) -> None:
    """Both exchanges, on the admin engine like their own suites, refused
    three ways. A job that is no longer live, named by its own device: the
    job's client (the device's). A malformed id, and a job of ANOTHER device:
    no client -- the refusal does not take one from a job the asking device
    has no claim on.
    FALSIFIER: drop `client_id=` from either `_audit_refusal`, or the
    `Job.target_id == device_id` predicate from `dedup.py::refusal_client`."""
    store, _s3 = aws
    base, restore, devices = await seed_restore(
        api_env.admin_engine, api_env.app_engine, vault, "bk-aud-refused", device_count=2
    )
    other = devices[1]
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(
            update(Job)
            .where(Job.id.in_([base.job_id, restore.job_id]))
            .values(status=JobStatus.succeeded)
        )
        await s.commit()
    async with AsyncSession(api_env.admin_engine) as s:
        for device, job in ((base.device_id, str(base.job_id)), (base.device_id, "not-a-job"),
                            (other, str(base.job_id))):
            assert await answer_chunk_query(
                s, store, _chunk_query(job), org_id=base.org_id, device_id=device, now=SEED_NOW
            ) is None
        for device, job in ((base.device_id, str(restore.job_id)),
                            (base.device_id, "not-a-job"), (other, str(restore.job_id))):
            assert await answer_read_query(
                s, store, _read_query(job), org_id=base.org_id, device_id=device, now=SEED_NOW
            ) is None
        await s.commit()
    refusals = await _events(api_env, base.org_id, "backup.dedup_refused", "backup.read_refused")
    assert [(e.action, e.meta["reason"], e.client_id) for e in refusals] == [
        ("backup.dedup_refused", "job_not_live", base.org_id),
        ("backup.dedup_refused", "malformed_job_id", None),
        ("backup.dedup_refused", "wrong_device", None),
        ("backup.read_refused", "job_not_live", base.org_id),
        ("backup.read_refused", "malformed_job_id", None),
        ("backup.read_refused", "wrong_device", None),
    ]
```

`seed_restore` (`tests/test_backup_read_query.py:86-155`, sobre `backup_dedup_fixtures.seed_dedup_org`) pone sus equipos en el cliente interno, cuyo id es el de la organización (la convención de id de la fase 2). Por eso el cliente esperado es `base.org_id`. Los dos `None` impiden que una implementación pase usando el `org_id` como cliente.

Run: `.venv/bin/pytest -q tests/test_backup_audit_clients.py`
Expected: FAIL. En las cuatro, `client_id` es `None` donde se espera `x` (o `base.org_id`).

- [ ] **Step 4: Auditorías de respaldo con cliente, el código**

`backend/app/modules/backup/restore.py`:
- En `dispatch_restore`, la auditoría de `:196-212` gana `client_id=point.client_id,` tras `metadata={...},`. El punto no se escribe en esa función, así que su `client_id` no caduca. Es el cliente del equipo: H desde `device_id`, incluso para un punto de antes de la 0242 en el repositorio interno.
- En `ingest_restore_result`, justo después de `if restore_job is None: return  # …` (`:272-273`):

```python
    # Read now: the state update below expires it (`server_onupdate`, written
    # by trigger), and an async session cannot lazy-load it.
    client_id = restore_job.client_id
```

  y la auditoría de `:313-327` gana `client_id=client_id,` tras `metadata={...},`.

`backend/app/modules/backup/test_restore.py:156-170`: la auditoría `backup.test_restore_dispatched` gana `client_id=client_id,` (la variable del bucle de `:86`, `for device_id, client_id in devices:`) tras `metadata={...},`.

`backend/app/modules/backup/gc.py`, en `verify_point`, justo después del `select(BackupPoint)…scalar_one()` (`:205-211`):

```python
    # Read now: stamping `verified_at` below expires it (`server_onupdate`,
    # written by trigger), and an async session cannot lazy-load it.
    client_id = point.client_id
```

y la auditoría `backup.verify` (`:245-255`) gana `client_id=client_id,` tras `metadata={"ok": ok, "checked": checked},`.

`backend/app/modules/backup/offsite.py`:
- `replicate_step`: la auditoría `backup.replicated` (`:801-815`) gana `client_id=point.client_id,` tras su `metadata={...},`. El punto no se escribe en esta función.
- `record_copy_failure` (`:820-848`) queda así:

```python
async def record_copy_failure(
    session: AsyncSession, *, org_id: UUID, point_id: UUID, target_id: UUID, error: BaseException
) -> None:
    # The point's client (phase 3 design §5.5): one read, because a copy
    # written here for the first time has not had its client from the trigger
    # yet, and a point that is gone has none to give.
    client_id = (
        await session.execute(
            select(BackupPoint.client_id).where(
                BackupPoint.org_id == org_id, BackupPoint.id == point_id
            )
        )
    ).scalar_one_or_none()
    copy = (
        await session.execute(
            select(BackupPointCopy).where(
                BackupPointCopy.org_id == org_id,
                BackupPointCopy.point_id == point_id,
                BackupPointCopy.target_id == target_id,
            )
        )
    ).scalar_one_or_none()
    if copy is None:
        copy = BackupPointCopy(org_id=org_id, point_id=point_id, target_id=target_id)
        session.add(copy)
    copy.state = "failed"
    copy.last_error = _safe_error(error)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="system",
        actor_id=None,
        action="backup.replication_failed",
        target_type="backup_point",
        target_id=str(point_id),
        metadata={"target_id": str(target_id), "error": copy.last_error},
        client_id=client_id,
    )
    await session.flush()
```

  (`BackupPoint` ya está importado en `offsite.py`: lo usa `replicate_point`.)
- `offsite_gc`: encima de `if expired or deleted:` (`:1031`):

```python
    # NO CLIENT, deliberately (phase 3 design §5.5): one summary of every
    # target, repository and client of the organisation. Whoever reads the
    # audit log holds every client (plan 2026-10-26 C6).
```

`backend/app/modules/backup/reconcile.py:64-99`: en cada bucle, lo primero es leer el cliente, y la auditoría lo pasa:

```python
    for run, job in runs:
        # Read before the update: a flush expires it (`server_onupdate`,
        # written by trigger). Phase 3 design §5.5.
        client_id = run.client_id
        run.state = "failed"
        run.error = _reason(job)
        run.finished_at = now
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="system",
            actor_id=None,
            action="backup.run_reconciled",
            target_type="device",
            target_id=str(run.device_id),
            metadata={"run_id": str(run.id), "job_id": str(job.id)},
            client_id=client_id,
        )
```

y lo mismo en `for restore, job in restores:`, con `client_id = restore.client_id` y `client_id=client_id,` en `backup.restore_reconciled`.

`backend/app/modules/backup/dedup.py`: antes de `_audit_refusal` (`:128`), la función nueva, y `_audit_refusal` la usa:

```python
async def refusal_client(
    session: AsyncSession, *, org_id: UUID, device_id: UUID, job_id: str
) -> UUID | None:
    """The client a refused exchange is filed under (phase 3 design §5.5).

    The job's, when the agent named a job of this organisation whose target is
    this very device -- a job's client is its target's (migration 0231), so it
    is the device's own. Otherwise none: a malformed id, an unknown job, or
    another device's job names no client this refusal can honestly carry.
    Shared with `read.py`, whose refusals follow the same rule. Runs only on
    a refusal, never on a granted exchange."""
    try:
        parsed = UUID(job_id)
    except ValueError:
        return None
    return (
        await session.execute(
            select(Job.client_id).where(
                Job.org_id == org_id,
                Job.id == parsed,
                Job.target_type == "device",
                Job.target_id == device_id,
            )
        )
    ).scalar_one_or_none()


async def _audit_refusal(
    session: AsyncSession,
    *,
    org_id: UUID,
    device_id: UUID,
    job_id: str,
    reason: str,
) -> None:
    client_id = await refusal_client(
        session, org_id=org_id, device_id=device_id, job_id=job_id
    )
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="agent",
        actor_id=None,
        action="backup.dedup_refused",
        target_type="device",
        target_id=str(device_id),
        # `job_id` is agent-supplied and may be anything the wire model allowed;
        # it is bounded here so a hostile value cannot bloat an audit row.
        metadata={"reason": reason, "job_id": job_id[:64]},
        client_id=client_id,
    )
```

`backend/app/modules/backup/read.py`:
- `:54`: `from app.modules.backup.dedup import _LIVE_JOB_STATUSES` pasa a `from app.modules.backup.dedup import _LIVE_JOB_STATUSES, refusal_client`.
- `_audit_refusal` (`:81-107`) queda así:

```python
async def _audit_refusal(
    session: AsyncSession,
    *,
    org_id: UUID,
    device_id: UUID,
    job_id: str,
    reason: str,
) -> None:
    """The twin of `dedup.py::_audit_refusal`, and it must stay in step with it.

    `reason` is a word from the fixed vocabulary used below -- never a suffix,
    never a key, never an exception message. This row is hash-chained and
    permanent. Its client is `dedup.refusal_client`'s (phase 3 design §5.5).
    """
    client_id = await refusal_client(
        session, org_id=org_id, device_id=device_id, job_id=job_id
    )
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="agent",
        actor_id=None,
        action="backup.read_refused",
        target_type="device",
        target_id=str(device_id),
        # `job_id` is agent-supplied and may be anything the wire model allowed;
        # bounded here so a hostile value cannot bloat an audit row.
        metadata={"reason": reason, "job_id": job_id[:64]},
        client_id=client_id,
    )
```

Run: `.venv/bin/pytest -q tests/test_backup_audit_clients.py tests/test_backup_dedup.py tests/test_backup_read_query.py tests/test_backup_lifetime.py tests/test_backup_reconcile.py tests/test_backup_restore.py tests/test_backup_restore_result.py tests/test_backup_test_restore.py tests/test_backup_gc.py tests/test_backup_offsite.py tests/test_client_scope_backup.py`
Expected: PASS. `test_a_malformed_job_id_is_refused_without_a_database_lookup` (`test_backup_read_query.py:802`) sigue verde: `refusal_client` sale en el `ValueError` sin consultar nada.

- [ ] **Step 5: Retirar un repositorio, las pruebas que fallan**

```python
# backend/tests/test_backup_repository_retire.py
"""Retiring a backup repository (phase 3 design §5.5, operator decision Q5).

`POST /backup/repositories/{id}/retire` needs `backup.policy.manage` on the
repository's client -- the route's scope is where the person holds it, so
another client's repository is a 404 like a missing one -- and is refused with
a 409 while an enabled policy names it, or once it is retired.

A retired repository takes no new backup:
- the dispatch refuses it and records why, reason `repository_retired`, in
  the `backup.dispatch_refused` shape it already uses -- once per device and
  policy version for the window, every time for a person;
- the dedup exchange grants it no writes;
- no policy may name it again.
Its points stay, and stay restorable: the read exchange grants reads on
`active` and `retired` repositories. There is no way back in this phase.
"""

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import BackupRepository, BackupRun
from app.modules.backup.dedup import answer_chunk_query
from app.modules.backup.dispatch import dispatch_due_backup
from app.modules.backup.read import answer_read_query
from tests.backup_dedup_fixtures import SEED_NOW
from tests.client_scope_support import client_with_site
from tests.conftest import ApiEnv
from tests.test_backup_audit_clients import _chunk_query, _read_query
from tests.test_backup_dispatch import _FakeJobEngine
from tests.test_backup_offsite import aws, vault  # noqa: F401  (fixtures)
from tests.test_backup_read_query import seed_restore
from tests.test_client_record_checks import _holder
from tests.test_client_scope_backup import (
    _LONG_AGO,
    BK,
    _admin,
    _device,
    _events,
    _group,
    _policy,
    _repo,
    _tomorrow,
    _written,
)

IN_USE = (
    "an enabled backup policy still uses this repository; switch it off or point it at "
    "another repository first"
)
ALREADY = "this repository is already retired"
RETIRED = "that repository is retired"

#: Some session other than the holder's (`:me`) waits for a
#: `backup_repositories` row the holder locked:
#: `client_scope_support.WAITS_ON_A_CLIENT_ROW` on this table (that comment
#: explains the two shapes the wait takes in `pg_locks`).
WAITS_ON_A_REPOSITORY_ROW = text(
    "SELECT count(*) FROM pg_locks t "
    "WHERE t.locktype = 'tuple' AND t.relation = 'backup_repositories'::regclass "
    "AND t.pid <> :me AND (NOT t.granted OR EXISTS ("
    "  SELECT 1 FROM pg_locks w JOIN pg_locks h ON h.transactionid = w.transactionid "
    "  WHERE w.pid = t.pid AND w.locktype = 'transactionid' AND NOT w.granted "
    "  AND h.pid = :me AND h.locktype = 'transactionid' AND h.granted))"
)


async def _retire(env: ApiEnv, headers: dict[str, str], repository: str | UUID) -> Any:
    return await env.client.post(f"{BK}/repositories/{repository}/retire", headers=headers)


async def _state(env: ApiEnv, repository: str | UUID) -> str:
    async with AsyncSession(env.admin_engine) as s:
        return (
            await s.execute(
                select(BackupRepository.state).where(
                    BackupRepository.id == UUID(str(repository))
                )
            )
        ).scalar_one()


async def _wait_until_blocked_on_a_repository(observer: AsyncSession, holder_pid: int) -> bool:
    for _ in range(400):
        await asyncio.sleep(0.025)
        waiting = (
            await observer.execute(WAITS_ON_A_REPOSITORY_ROW, {"me": holder_pid})
        ).scalar_one()
        await observer.rollback()
        if waiting:
            return True
    return False


async def test_a_repository_is_retired_in_its_client_and_audited_there(
    api_env: ApiEnv,
) -> None:
    """`backup.policy.manage` on X retires X's repository: 200, `retired`,
    and one `backup.repository_retired` in X naming the person. Y's
    repository, where the person only reads, is the 404 a missing one gets
    and stays active. Without the code anywhere, the old 403.
    FALSIFIER: gate the route on `backup.read` (Y's repository is retired)."""
    org = await api_env.create_org("bk-retire")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    x_repo = await _repo(api_env, admin, x, "x")
    y_repo = await _repo(api_env, admin, y, "y")
    tech_id, tech = await _holder(
        api_env, org, {x: ("backup.read", "backup.policy.manage"), y: ("backup.read",)}
    )
    _, reader = await _holder(api_env, org, {None: ("backup.read",)})

    hidden = await _retire(api_env, tech, y_repo)
    missing = await _retire(api_env, tech, uuid4())
    assert (hidden.status_code, hidden.json()) == (missing.status_code, missing.json()) == (
        404, {"detail": "no such repository"}
    )
    assert await _state(api_env, y_repo) == "active"
    assert (await _retire(api_env, reader, x_repo)).status_code == 403

    r = await _retire(api_env, tech, x_repo)
    assert r.status_code == 200, r.text
    assert (r.json()["id"], r.json()["client_id"], r.json()["state"]) == (
        x_repo, str(x), "retired"
    )
    assert await _state(api_env, x_repo) == "retired"
    (event,) = await _events(api_env, org, "backup.repository_retired")
    assert (event.target_type, event.target_id, event.client_id, event.actor_id) == (
        "backup_repository", x_repo, x, tech_id
    )
    assert event.meta["name"] == "x"


async def test_a_repository_an_enabled_policy_uses_cannot_be_retired(api_env: ApiEnv) -> None:
    """A 409 while an enabled policy names it: the repository unchanged and
    nothing written or audited. Switched off -- same repository, as it
    stands -- the policy no longer holds it. A second retire is a 409 too
    and writes no second event.
    FALSIFIER: drop the enabled-policy check in
    `repository.py::retire_repository`."""
    org = await api_env.create_org("bk-retire-used")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    x_repo = await _repo(api_env, admin, x, "x")
    group = await _group(api_env, org, x)
    policy = {"device_group_id": str(group), "repository_id": x_repo, "window_cron": "0 2 * * *"}
    r = await api_env.client.put(f"{BK}/policies", json=policy, headers=admin)
    assert r.status_code == 200, r.text

    before = await _written(api_env, org)
    used = await _retire(api_env, admin, x_repo)
    assert (used.status_code, used.json()) == (409, {"detail": IN_USE}), used.text
    assert await _written(api_env, org) == before
    assert await _state(api_env, x_repo) == "active"

    off = await api_env.client.put(
        f"{BK}/policies", json={**policy, "enabled": False}, headers=admin
    )
    assert off.status_code == 200, off.text
    assert (await _retire(api_env, admin, x_repo)).status_code == 200
    again = await _retire(api_env, admin, x_repo)
    assert (again.status_code, again.json()) == (409, {"detail": ALREADY}), again.text
    assert len(await _events(api_env, org, "backup.repository_retired")) == 1


async def test_no_policy_may_name_a_retired_repository(api_env: ApiEnv) -> None:
    """Plan decision (Task 6): the dispatch would refuse every window of such
    a policy, so the write says so first -- a 409, nothing written. A stored
    policy that names it, switched off before the retire, may still be saved
    switched off as it stands (the one exception `upsert_policy` already
    makes); switching it on is the 409.
    FALSIFIER: drop the state check in `clients.py::policy_repository`."""
    org = await api_env.create_org("bk-retire-policy")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    x_repo = await _repo(api_env, admin, x, "x")
    kept = await _group(api_env, org, x)
    fresh = await _group(api_env, org, x)
    stored = {"device_group_id": str(kept), "repository_id": x_repo, "enabled": False}
    r = await api_env.client.put(f"{BK}/policies", json=stored, headers=admin)
    assert r.status_code == 200, r.text
    assert (await _retire(api_env, admin, x_repo)).status_code == 200

    before = await _written(api_env, org)
    new = await api_env.client.put(
        f"{BK}/policies", json={"device_group_id": str(fresh), "repository_id": x_repo},
        headers=admin,
    )
    assert (new.status_code, new.json()) == (409, {"detail": RETIRED}), new.text
    on = await api_env.client.put(f"{BK}/policies", json={**stored, "enabled": True},
                                  headers=admin)
    assert (on.status_code, on.json()) == (409, {"detail": RETIRED}), on.text
    assert await _written(api_env, org) == before

    kept_off = await api_env.client.put(
        f"{BK}/policies", json={**stored, "window_cron": "0 3 * * *"}, headers=admin
    )
    assert kept_off.status_code == 200, kept_off.text


async def test_a_policy_write_waits_for_a_retire_in_flight(api_env: ApiEnv) -> None:
    """A retire and a policy write are a check and an act each: the retire
    looks for an enabled policy and writes the state; the write looks at the
    state and writes the policy. Unserialised, both pass and an enabled
    policy lands on a retired repository. So the write reads the repository
    FOR SHARE (`policy_repository`) and the retire holds it FOR NO KEY
    UPDATE. Here the holder is a retire in flight -- the state written, not
    committed: the write waits on the row, and once the retire commits it
    answers the 409 a retired repository gets.
    FALSIFIER: drop `.with_for_update(read=True)` from `policy_repository`:
    the write reads the row as it was, never waits, and answers 200."""
    org = await api_env.create_org("bk-retire-lock")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    x_repo = await _repo(api_env, admin, x, "x")
    group = await _group(api_env, org, x)
    body = {"device_group_id": str(group), "repository_id": x_repo}
    async with AsyncSession(api_env.admin_engine) as holder, AsyncSession(
        api_env.admin_engine
    ) as observer:
        await holder.execute(
            text("UPDATE backup_repositories SET state = 'retired' WHERE id = :r"),
            {"r": UUID(x_repo)},
        )
        me = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        put = asyncio.create_task(
            api_env.client.put(f"{BK}/policies", json=body, headers=admin)
        )
        if not await _wait_until_blocked_on_a_repository(observer, me):
            put.cancel()
            pytest.fail("the policy write never waited on the repository row")
        await holder.commit()
    r = await put
    assert (r.status_code, r.json()) == (409, {"detail": RETIRED}), r.text


async def test_the_window_refuses_a_retired_repository_once_per_policy_version(
    api_env: ApiEnv,
) -> None:
    """No job and no key, and the refusal recorded in the device's client,
    reason `repository_retired`, once however many sweeps the fire is due
    for. The policy is planted after the retire (`_policy` writes the row
    directly): the API refuses to write it, and that is the case the
    dispatch is the net for.
    FALSIFIER: drop the state check in `dispatch.py::_dispatch_for_policy`
    (a job is created, with the repository key in its params)."""
    org = await api_env.create_org("bk-retire-window")
    x, x_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    x_repo = await _repo(api_env, admin, x, "x")
    assert (await _retire(api_env, admin, x_repo)).status_code == 200
    group = await _group(api_env, org, x)
    policy = await _policy(api_env, org, UUID(x_repo), group=group, window_cron="0 2 * * *",
                           created_at=_LONG_AGO)
    device = await _device(api_env, org, x, x_site, group, created_at=_LONG_AGO)
    engine = _FakeJobEngine()
    for minute in (5, 20):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            assert await dispatch_due_backup(
                s, engine, api_env.app.state.vault, org_id=org, device_id=device,
                now=_tomorrow(2, minute),
            ) == []
    assert engine.created == []
    async with AsyncSession(api_env.admin_engine) as s:
        runs = (
            await s.execute(
                select(func.count()).select_from(BackupRun).where(BackupRun.device_id == device)
            )
        ).scalar_one()
    assert runs == 0
    (refusal,) = await _events(api_env, org, "backup.dispatch_refused")
    assert (refusal.actor_type, refusal.target_id, refusal.client_id) == (
        "system", str(device), x
    )
    assert (refusal.meta["reason"], refusal.meta["repository_id"], refusal.meta["policy_id"]) == (
        "repository_retired", x_repo, str(policy)
    )


async def test_run_now_on_a_retired_repository_is_refused_every_time(api_env: ApiEnv) -> None:
    """A person's Run now is their act, recorded each time it is refused
    (invariant 4), and dispatches nothing."""
    org = await api_env.create_org("bk-retire-runnow")
    x, x_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    x_repo = await _repo(api_env, admin, x, "x")
    assert (await _retire(api_env, admin, x_repo)).status_code == 200
    group = await _group(api_env, org, x)
    await _policy(api_env, org, UUID(x_repo), group=group)
    device = await _device(api_env, org, x, x_site, group)
    for _ in range(2):
        r = await api_env.client.post(f"{BK}/devices/{device}/run", headers=admin)
        assert (r.status_code, r.json()) == (200, {"job_ids": []}), r.text
    refusals = await _events(api_env, org, "backup.dispatch_refused")
    assert [(e.actor_type, e.client_id, e.meta["reason"]) for e in refusals] == [
        ("user", x, "repository_retired")
    ] * 2


async def test_a_retired_repository_takes_no_writes_and_its_points_stay_readable(
    api_env: ApiEnv, aws: Any, vault: LocalVaultService
) -> None:
    """The exchanges, on the admin engine like their own suites. A retired
    repository gets no write grant (`dedup.py`, unchanged: only `active`
    writes) and does get the read grants of a restore (`read.py`: `active`
    and `retired`); any other state, none.
    FALSIFIER: `read.py` back to `repo.state != "active"` (the restore is
    refused)."""
    store, _s3 = aws
    base, restore, _ = await seed_restore(
        api_env.admin_engine, api_env.app_engine, vault, "bk-retire-x"
    )

    async def set_state(state: str) -> None:
        async with AsyncSession(api_env.admin_engine) as s:
            await s.execute(
                text("UPDATE backup_repositories SET state = :state WHERE id = :r"),
                {"state": state, "r": base.repository_id},
            )
            await s.commit()

    await set_state("retired")
    async with AsyncSession(api_env.admin_engine) as s:
        write = await answer_chunk_query(s, store, _chunk_query(base.job_id),
                                         org_id=base.org_id, device_id=base.device_id,
                                         now=SEED_NOW)
        read = await answer_read_query(s, store, _read_query(restore.job_id),
                                       org_id=base.org_id, device_id=base.device_id,
                                       now=SEED_NOW)
        await s.commit()
    assert write is None
    assert read is not None
    assert [g.suffix for g in read.grants] == ["chunks/ab/abcd"]

    await set_state("archived")
    async with AsyncSession(api_env.admin_engine) as s:
        assert await answer_read_query(s, store, _read_query(restore.job_id),
                                       org_id=base.org_id, device_id=base.device_id,
                                       now=SEED_NOW) is None
        await s.commit()
    refusals = await _events(api_env, base.org_id, "backup.dedup_refused",
                             "backup.read_refused")
    assert [(e.action, e.meta["reason"]) for e in refusals] == [
        ("backup.dedup_refused", "repository_not_active"),
        ("backup.read_refused", "repository_not_active"),
    ]
```

Run: `.venv/bin/pytest -q tests/test_backup_repository_retire.py`
Expected: FAIL.
- La ruta no existe: `POST …/retire` responde 404 `{"detail": "Not Found"}` y fallan las aserciones de 200, 409 y 403.
- `test_a_policy_write_waits_for_a_retire_in_flight` termina en `pytest.fail("the policy write never waited …")`.
- La lectura sobre un repositorio `retired` devuelve `None` (`repository_not_active`).

- [ ] **Step 6: Retirar un repositorio, el código**

`backend/app/modules/backup/models.py`, después de los imports (`:34`) y antes de `class BackupRepository`:

```python
#: `backup_repositories.state`, which no CHECK constraint holds (0014). A new
#: repository is `active`. A `retired` one (phase 3 design §5.5) takes no new
#: backup -- the dispatch refuses it, `dedup.py` grants it no writes, no policy
#: may name it -- and its points stay restorable (`read.py`). There is no way
#: back to `active` in this phase.
REPOSITORY_ACTIVE = "active"
REPOSITORY_RETIRED = "retired"
```

Y la línea `state: Mapped[str] = mapped_column(String(16), default="active")` (`:62`) gana encima el comentario `# REPOSITORY_ACTIVE or REPOSITORY_RETIRED (above).`

`backend/app/modules/backup/repository.py`:
- Imports: `from app.core.i18n import tr`, y `from app.modules.backup.models import BackupRepository` pasa a `from app.modules.backup.models import REPOSITORY_RETIRED, BackupPolicy, BackupRepository`.
- Al final del docstring del módulo (`:1-14`), el párrafo «A repository can be RETIRED (phase 3 design §5.5, `retire_repository`): it takes no new backup and keeps its points, restorable.»
- Al final del archivo:

```python
class RetireRefused(Exception):
    """A refused retire, with the status the API answers and its sentence:
    404 for a repository this request cannot see, 409 for one an enabled
    policy uses or one already retired."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def retire_repository(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, repository_id: UUID
) -> BackupRepository:
    """Retire one repository (phase 3 design §5.5, operator decision Q5).

    It takes no new backup: the dispatch refuses it (`dispatch.py`), the
    dedup exchange grants it no writes (`dedup.py`) and no policy may name it
    (`clients.policy_repository`). Its points stay, still occupying their
    space, and stay restorable (`read.py` grants reads on `retired`);
    verification, GC and the off-site copy of its points go on. There is no
    way back to `active` in this phase.

    `backup.policy.manage` on the repository's client is the ROUTE's gate: it
    runs under the clients where the person holds that code, so another
    client's repository is not found here -- the 404 a missing one gets (MSP
    clients spec §3.2's existence oracle), never a 403.

    A 409 while an ENABLED policy names it: retiring it would silently stop
    those devices' backups. The check sees the policies this request can
    see; a policy of another client naming it can only be one 0242 left
    behind, and the dispatch already refuses that one as another client's. A
    second retire is a 409 too, and writes no second event.

    LOCKS. The repository row, FOR NO KEY UPDATE, is the first lock; the audit
    chain's is the last. A policy write reads the repository FOR SHARE
    (`policy_repository`), so a write naming it and this retire serialise:
    neither passes its check while the other is between its check and its
    write. NO KEY, because the foreign-key checks of rows that only reference
    the repository (a run, a point) take FOR KEY SHARE and need not wait. No
    client lock: a retire creates no row of the client, and a repository of
    an archived client can still be retired."""
    repo = (
        await session.execute(
            select(BackupRepository)
            .where(BackupRepository.org_id == org_id, BackupRepository.id == repository_id)
            .with_for_update(key_share=True)
        )
    ).scalar_one_or_none()
    if repo is None:
        raise RetireRefused(404, tr("no such repository"))
    if repo.state == REPOSITORY_RETIRED:
        raise RetireRefused(409, tr("this repository is already retired"))
    in_use = (
        await session.execute(
            select(BackupPolicy.id)
            .where(
                BackupPolicy.org_id == org_id,
                BackupPolicy.repository_id == repo.id,
                BackupPolicy.enabled.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if in_use is not None:
        raise RetireRefused(
            409,
            tr(
                "an enabled backup policy still uses this repository; switch it off or "
                "point it at another repository first"
            ),
        )
    repo.state = REPOSITORY_RETIRED
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="backup.repository_retired",
        target_type="backup_repository",
        target_id=str(repo.id),
        metadata={"name": repo.name},
        client_id=repo.client_id,
    )
    return repo
```

(`BackupRepository.client_id` y `name` no tienen `server_onupdate`, así que el `flush` no los caduca. Solo caduca `updated_at`, que `_repository_out` no lee.)

`backend/app/modules/backup/api.py`:
- `:61`: el import pasa a `from app.modules.backup.repository import RetireRefused, create_repository, retire_repository`.
- Después de `create_repository_route` (`:284-318`):

```python
@router.post("/repositories/{repository_id}/retire", response_model=RepositoryOut)
async def retire_repository_route(
    repository_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("backup.policy.manage")),
) -> RepositoryOut:
    """Phase 3 design §5.5 (`repository.retire_repository`). The scope is
    where the person holds `backup.policy.manage`, so another client's
    repository is the 404 a missing one gets."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            repo = await retire_repository(
                session, org_id=ctx.org_id, actor_id=ctx.user_id, repository_id=repository_id
            )
        except RetireRefused as exc:
            raise HTTPException(exc.status_code, exc.detail) from None
        return _repository_out(repo)
```

`backend/app/modules/backup/clients.py`:
- Import: `from app.modules.backup.models import BackupRepository` pasa a `from app.modules.backup.models import REPOSITORY_RETIRED, BackupRepository`.
- Las líneas `:26-28` del docstring del módulo («The group is read before it; the stored policy and the repository after it; none of those reads takes a lock.») pasan a: «The group is read before it; the stored policy after it, without a lock; the repository last, FOR SHARE (`policy_repository`: a retire holds it, phase 3 design §5.5).»
- `policy_repository` (`:116-132`) queda así:

```python
async def policy_repository(
    session: AsyncSession, *, org_id: UUID, client_id: UUID, repository_id: UUID
) -> None:
    """The repository a policy of `client_id` names is that client's, and is
    not retired: a 404 when this organisation has no such repository (or the
    session cannot see it), a 422 when it is another client's, a 409 when it
    is retired (phase 3 design §5.5: the dispatch would refuse every window
    of such a policy, so the write says so first).

    Read FOR SHARE, the policy write's last lock before the audit chain's. A
    retire holds the row FOR NO KEY UPDATE while it checks that no enabled
    policy names it and writes the state (`repository.retire_repository`), so
    the two serialise, and an enabled policy never lands on a repository that
    a concurrent retire found unused."""
    repository = (
        await session.execute(
            select(BackupRepository.client_id, BackupRepository.state)
            .where(BackupRepository.org_id == org_id, BackupRepository.id == repository_id)
            .with_for_update(read=True)
        )
    ).one_or_none()
    if repository is None:
        raise BackupClientError(404, tr("no such repository"))
    if repository.client_id != client_id:
        raise BackupClientError(422, tr("that repository belongs to another client"))
    if repository.state == REPOSITORY_RETIRED:
        raise BackupClientError(409, tr("that repository is retired"))
```

(La API ya traduce `BackupClientError` con su `status_code`: `api.py`, en `upsert_policy`.)

`backend/app/modules/backup/dispatch.py`:
- Import: `from app.modules.backup.models import REPOSITORY_RETIRED`, entre `from app.models import …` (`:56`) y `from app.modules.backup.policy import …` (`:57`).
- Al final del primer párrafo del docstring del módulo («The repository is the DEVICE'S CLIENT'S…», `:15-22`), la frase: «A RETIRED repository (phase 3 design §5.5) takes no new backup either: `_dispatch_for_policy` refuses it after the client check, recorded the same way, reason `repository_retired`. Its points stay restorable (`read.py`).»
- `:253`: `(`_refuse_foreign_repository`)` → `(`_refuse_repository`)`.
- `:289-290` pasan a:

```python
FOREIGN_REPOSITORY = "repository_of_another_client"
#: Phase 3 design §5.5: a retired repository takes no new backup.
RETIRED_REPOSITORY = "repository_retired"
UNREADABLE_WINDOW = "unreadable_window"
```

- `_refuse_foreign_repository` (`:340-376`) pasa a llamarse `_refuse_repository` y gana `reason`:

```python
async def _refuse_repository(
    session: AsyncSession,
    *,
    org_id: UUID,
    device: Device,
    policy: ResolvedBackupPolicy,
    repository_id: UUID,
    reason: str,
    actor_id: UUID | None,
    actor_type: Literal["user", "agent", "system"],
) -> None:
    """The dispatch's refusals of a policy's repository -- another client's
    (the module docstring's net, `FOREIGN_REPOSITORY`) or a retired one
    (phase 3 design §5.5, `RETIRED_REPOSITORY`) -- audited like every
    refusal of the module (`backup.dedup_refused`, `backup.read_refused`),
    with a reason from a fixed vocabulary and no key material (invariant 3):
    once per device and policy version when the window asks
    (`refusal_lookup`), every time a person does."""
    version = _policy_version(policy)
    if actor_type == "system" and (
        await session.execute(
            refusal_lookup(
                org_id=org_id, target_type="device", target_id=device.id,
                client_id=device.client_id, reason=reason,
                repository_id=repository_id, policy_id=policy.id, policy_version=version,
            )
        )
    ).first() is not None:
        return
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action="backup.dispatch_refused",
        target_type="device",
        target_id=str(device.id),
        metadata={
            "reason": reason,
            "device_id": str(device.id),
            "repository_id": str(repository_id),
            "policy_id": str(policy.id),
            "policy_updated_at": version,
        },
        client_id=device.client_id,
    )
    await session.flush()
```

- En `_dispatch_for_policy`, el bloque `:457-463` (`if repo.client_id != device.client_id: …`) pasa a:

```python
    if repo.client_id != device.client_id:
        await _refuse_repository(
            session, org_id=org_id, device=device, policy=policy, repository_id=repo.id,
            reason=FOREIGN_REPOSITORY, actor_id=actor_id, actor_type=actor_type,
        )
        return []
    if repo.state == REPOSITORY_RETIRED:
        # Phase 3 design §5.5: no new backup into a retired repository -- no
        # job and no key -- recorded like the refusal above. Its points stay.
        await _refuse_repository(
            session, org_id=org_id, device=device, policy=policy, repository_id=repo.id,
            reason=RETIRED_REPOSITORY, actor_id=actor_id, actor_type=actor_type,
        )
        return []
```

`backend/app/modules/backup/read.py`:
- Import: `from app.modules.backup.models import REPOSITORY_ACTIVE, REPOSITORY_RETIRED`.
- Después de `READ_GRANT_TTL = 900`:

```python
#: The repository states a restore may read (phase 3 design §5.5): a retired
#: repository takes no new backup, and its points stay restorable. `dedup.py`
#: still grants writes on `active` only.
READABLE_STATES = frozenset({REPOSITORY_ACTIVE, REPOSITORY_RETIRED})
```

- `:197-198`: `if repo.state != "active":` pasa a `if repo.state not in READABLE_STATES:`. El motivo `repository_not_active` no cambia: es vocabulario de filas permanentes.

`backend/app/core/i18n/es.py`, después de `"that repository belongs to another client": "ese repositorio es de otro cliente",` (`:4173`):

```python
    # A retired backup repository (phase 3 design §5.5).
    "that repository is retired": "ese repositorio está retirado",
    "this repository is already retired": "este repositorio ya está retirado",
    "an enabled backup policy still uses this repository; switch it off or point it at "
    "another repository first": (
        "una política de copias activa todavía usa este repositorio; desactívala o apúntala "
        "a otro repositorio antes"
    ),
```

`backend/app/mcp/descriptions/part_b.py`:
- `:212-216`, `PUT /api/v1/backup/policies` (280 caracteres, por debajo del límite de 300 de `test_descriptions_are_short_and_plain`):

```python
    "PUT /api/v1/backup/policies": (
        "Creates or replaces one device group's backup policy, or a client's default "
        "(client_id, no group). The repository must be that client's and not retired, except "
        "to switch a stored policy off naming the repository it already names. Governs "
        "scheduled backups on its devices. Audited."
    ),
```

- `:226-229`, `GET /api/v1/backup/repositories`: «Lists the backup repositories the caller's clients can see (client, name, state, dedup parameters, off-site target link). Keys are never returned.»
- Después de la entrada de `…/offsite` (`:235-238`):

```python
    "POST /api/v1/backup/repositories/{repository_id}/retire": (
        "Retires one backup repository: it takes no new backups and no policy may name it "
        "again, while its recovery points stay and can still be restored. Refused with 409 "
        "while an enabled policy uses it. There is no way back. Audited."
    ),
```

`frontend/src/i18n/en/auditVocabulary.ts`, después de `backup_repository_created: "Backup repository created",` (`:92`):

```ts
  backup_repository_retired: "Backup repository retired",
```

`frontend/src/i18n/es/auditVocabulary.ts`, después de `backup_repository_created: …` (`:89`):

```ts
  backup_repository_retired: "Repositorio de copias de seguridad retirado",
```

(`auditVocabulary.coverage.test.ts` lee el backend y fallaría sin estas dos líneas. El inglés es la derivación de `humaniseEnum`, como exige `auditVocabulary.english.test.ts`.)

- [ ] **Step 7: Comprobar que pasan**

Run: `.venv/bin/pytest -q tests/test_backup_repository_retire.py tests/test_backup_audit_clients.py tests/test_client_scope_backup.py tests/test_backup_api.py tests/test_backup_policy.py tests/test_backup_dispatch.py tests/test_backup_dedup.py tests/test_backup_read_query.py tests/test_i18n_catalog.py tests/test_mcp_descriptions.py tests/test_mcp_catalog.py tests/test_route_gating.py tests/test_global_writes.py`
Expected: PASS.
- `test_global_writes.py` no cambia: `backup.policy.manage` no protege ninguna escritura global, así que la ruta nueva no se clasifica.
- `test_backup_read_query.py` y `test_backup_dedup.py` siguen rechazando `archived` con `repository_not_active` (`:355`, `:671`).

Desde `frontend/`: `npx vitest run src/modules/audit` → PASS.

- [ ] **Step 8: La especificación de respaldos**

`docs/modules/backup/spec.md`. Después del bloque «Amended 2026-10-29…» del §4, que termina en `:177` con «…`docs/superpowers/plans/2026-10-26-msp-clientes-fase2-modulos.md`.», añade:

```
> **Amended 2026-10-31 (phase 3 design §5.5, operator decision Q5): a
> repository can be retired.** `POST /backup/repositories/{id}/retire`
> (`backup.policy.manage` on the repository's client; another client's
> repository is a 404) sets `state = 'retired'` and is audited
> `backup.repository_retired` in that client. It is a 409 while an enabled
> policy names the repository, and a 409 on one already retired; there is no
> way back to `active`. A retired repository takes no new backup: the dispatch
> refuses it — no job, no key — and records `backup.dispatch_refused`, reason
> `repository_retired` (once per device and policy version for the window,
> every time for a person's Run now); the dedup exchange grants it no writes
> (`repository_not_active`); and no policy write may name it (`PUT /policies`,
> 409), except to switch a stored policy off as it stands. Its points stay
> where they are, still occupying their space, and stay restorable: the read
> exchange grants reads on `active` and `retired` repositories, and
> verification, GC and the off-site copy go on as before. The `state`
> vocabulary is therefore `active|retired` (no CHECK constraint holds it,
> 0014).
>
> **Every backup audit that has its record's client at hand names it** (same
> date, §5.5): the restore and its result, the automatic test restore, the
> verification, the off-site copy and its failure, and the two
> reconciliations, each in its device's client. `backup.dedup_refused` and
> `backup.read_refused` name the client when the agent named a job of its own
> device, and none otherwise. `backup.offsite_gc` names none, deliberately: it
> is one summary of every target, repository and client of the organisation.
```

En el §12, tras la viñeta «`GET/POST /repositories` (`read` / `policy.manage`), `GET/PUT /policies` (`read` / `policy.manage`).» (`:757-758`), una viñeta nueva: «- `POST /repositories/{id}/retire` (`policy.manage` on the repository's client; 409 while an enabled policy names it; see the 2026-10-31 amendment in §4).»

- [ ] **Step 9: Verificación completa**

Desde `backend/` (con `pg_ctlcluster 16 main start; redis-cli ping || redis-server --daemonize yes`, y en la worktree `export PYTHONPATH=$PWD` y el `ITS_TEST_REDIS_URL` que asigne el coordinador):
- `.venv/bin/pytest -q tests/test_client_scope_deployments.py tests/test_deploy_api.py tests/test_backup_audit_clients.py tests/test_backup_repository_retire.py tests/test_client_scope_backup.py tests/test_backup_dedup.py tests/test_backup_read_query.py tests/test_backup_offsite.py tests/test_backup_reconcile.py tests/test_backup_test_restore.py tests/test_backup_gc.py tests/test_backup_restore_result.py tests/test_backup_api.py tests/test_i18n_catalog.py tests/test_mcp_descriptions.py tests/test_mcp_catalog.py` → PASS.
- `.venv/bin/pytest -q` (la suite completa, más de 30 min) → PASS.
- `.venv/bin/mypy app` → sin errores.
- `.venv/bin/ruff check app tests` → limpio.

Si ruff ordena distinto algún import de las pruebas nuevas, `ruff check --fix` sobre ese archivo.

Desde `frontend/`: `npx vitest run` completo (la prueba de vocabulario de auditoría lee el backend), `npx tsc -b` y `npm run build` → PASS.

- [ ] **Step 10: Commit**

```bash
git add backend/app/modules/deploy/api.py backend/app/modules/backup/models.py \
  backend/app/modules/backup/repository.py backend/app/modules/backup/api.py \
  backend/app/modules/backup/clients.py backend/app/modules/backup/dispatch.py \
  backend/app/modules/backup/read.py backend/app/modules/backup/dedup.py \
  backend/app/modules/backup/restore.py backend/app/modules/backup/test_restore.py \
  backend/app/modules/backup/gc.py backend/app/modules/backup/offsite.py \
  backend/app/modules/backup/reconcile.py backend/app/core/i18n/es.py \
  backend/app/mcp/descriptions/part_b.py \
  backend/tests/test_client_scope_deployments.py backend/tests/test_backup_audit_clients.py \
  backend/tests/test_backup_repository_retire.py \
  frontend/src/i18n/en/auditVocabulary.ts frontend/src/i18n/es/auditVocabulary.ts \
  docs/modules/backup/spec.md docs/modules/app-deploy/spec.md
git commit -m "feat(backup,deploy): retire a backup repository; backup audits name their client; executing an unseen deployment is a 404 (phase 3 Task 6)"
```

Pruebas corregidas a propósito (para el informe):
- `tests/test_client_scope_deployments.py:312`: el 200 `{"job_ids": []}` de un `execute` sobre un despliegue invisible pasa a 404 «no such deployment». Lo demás de la prueba no cambia.


### Task 7: Consola — tipo `Me`, selección de cliente, selector, cabecera, caché y canal en vivo

Diseño: §1.1, §1.2, §1.4, §1.5. Inventario: §2 y §3.

Depende de la Task 1 (`/me` con `clients`, `permissions` y `permissions_everywhere`) y de la Task 2
(`frontend/src/lib/globalRoutes.ts` exporta `isGlobalRoute(method, path)`).

**Files:**
- Create: `frontend/src/lib/clientChoice.ts` (la elección, fuera de React)
- Create: `frontend/src/lib/clientScope.tsx` (`ClientScopeProvider`, `useClientScope`, `CLIENT_STORAGE_KEY`)
- Create: `frontend/src/shell/ClientSelector.tsx`
- Modify: `frontend/src/lib/api.ts` — antes de `export interface Me` (`:767`) el tipo `MeClient`; `Me` (`:767-809`) gana `clients` y `permissions_everywhere`; `requestHeaders` (`:1487-1500`); el constructor de `ApiClient` y `request()` (`:1502-1513`); las 17 llamadas crudas a `requestHeaders(this.getToken()…)` (`:2058, :2247, :2277, :2499, :2752, :2781, :3169, :3329, :3994, :4014, :4211, :4232, :4500, :4524, :5144, :5185, :5370`)
- Modify: `frontend/src/lib/ApiContext.tsx:38-46` (cuarto argumento de `ApiClient`)
- Modify: `frontend/src/lib/ws.ts` (archivo entero, abajo)
- Modify: `frontend/src/shell/AppShell.tsx` — `AppShellProps` (`:37-77`), la desestructuración (`:197-214`), `.top-actions` (`:485`)
- Modify: `frontend/src/shell/shell.css` — tras `.top-actions` (`:284`) y en el bloque `@media (max-width: 640px)` (`:724`)
- Modify: `frontend/src/App.tsx` — imports (`:11-17`), comentario de `queryClient` (`:130-136`), `<AppShell` en `Shell` (`:321-360`), `RequireAuth` (`:425`)
- Modify: `frontend/src/fixtures/core.ts` — import (`:23`), `me` (`:91-104`)
- Modify: `frontend/src/test/setup.ts:65-68` (cada prueba empieza con «Todos mis clientes»)
- Modify: `frontend/src/i18n/en/shell.ts`, `frontend/src/i18n/es/shell.ts` (sección `clientSelector`), `frontend/src/i18n/en/lib.ts`, `frontend/src/i18n/es/lib.ts` (secciones `clientScope` y `clients`)
- Test (nuevos): `frontend/src/lib/clientChoice.test.ts`, `frontend/src/lib/api.clientScope.test.ts`, `frontend/src/lib/clientScope.test.tsx`, `frontend/src/shell/ClientSelector.test.tsx`
- Test (añadidos): `frontend/src/lib/ws.test.ts`, `frontend/src/shell/AppShell.test.tsx`, `frontend/src/App.shell.test.tsx`, `frontend/src/App.auth.test.tsx`, `frontend/src/fixtures/client.test.ts`

**Interfaces:**
- Consumes: `GET /me` → `clients: [{id, name, kind, enabled_modules, permissions}]`, `permissions`, `permissions_everywhere` (Task 1). `isGlobalRoute(method: string, path: string): boolean` de `frontend/src/lib/globalRoutes.ts` (Task 2): `path` relativo a `/api/v1`, sin query string.
- Produces (01-interfaces):
  - `frontend/src/lib/api.ts`: `interface MeClient { id: string; name: string; kind: "customer" | "internal" | "unassigned"; enabled_modules: string[]; permissions: string[] }`; `Me` gana `clients?: MeClient[]` y `permissions_everywhere?: string[]`.
  - `new ApiClient(getToken, onUnauthorized, base = "/api/v1", getClient: () => string | null = () => null)`.
  - `requestHeaders(token, extra?, route?)` añade `X-Client-Id` salvo cuando `isGlobalRoute(route.method, route.path)`.
  - `frontend/src/lib/clientScope.tsx`: `ClientScopeProvider`; `useClientScope(): ClientScope` con `{ selected: string | null; select(id: string | null): void; clients: MeClient[]; ready: boolean }`; `CLIENT_STORAGE_KEY = "itms.client"` (reexportado).
  - `frontend/src/shell/ClientSelector.tsx`: `ClientSelector()`.
  - `frontend/src/lib/ws.ts`: `liveUrl(client: string | null): string`.
- Produces (nombres nuevos, fuera de 01-interfaces):
  - `frontend/src/lib/clientChoice.ts`: `CLIENT_STORAGE_KEY`, `readStoredClient(): string | null`, `selectedClient(): string | null`, `subscribeClient(listener: () => void): () => void`, `setSelectedClient(id: string | null): void`, `adoptStoredClient(): void`.
  - `frontend/src/lib/api.ts`: `interface RequestRoute { method: string; path: string; client: string | null }`; método privado `ApiClient.route(method, path)`.
  - `frontend/src/lib/clientScope.tsx`: `interface ClientScope`, `orderClients(clients): MeClient[]`.
  - `frontend/src/lib/ws.ts`: `CLOSE_SCOPE_REFUSED = 4403`.
  - `AppShellProps.clientSelector?: ReactNode`.

**Decisiones del plan (Task 7):**
- *Decisión del plan: `Me.clients` y `Me.permissions_everywhere` son opcionales (`?`).* `api.ts:780-791` ya razona por qué los campos nuevos de `Me` son opcionales: hay 90 literales `const X: Me = {…}` en las pruebas (`grep -rn ": Me = {" src | grep -v " as "`), y hacerlos obligatorios rompe `tsc -b` en todos. Ausente, `clients` se lee como `[]` y `permissions_everywhere` como `permissions` (antes de la fase 3, `/me` devolvía justo los códigos de todos los clientes: `own_profile.py:85`). Así las pruebas que no lo dicen siguen describiendo a un administrador de todos los clientes, que es lo que describían.
- *Decisión del plan: la elección vive fuera de React, en `lib/clientChoice.ts`.* `ApiClient` (cabecera) y el socket (`?client=`) no son componentes, y `ApiContext.tsx` no puede importar `clientScope.tsx` (que importa `useApi` de `ApiContext.tsx`: ciclo). Mismo patrón que `getLanguage()`/`setActiveLanguage()` de `i18n/core.ts:7-23`, que `requestHeaders` ya lee.
- *Decisión del plan: solo se lee de `localStorage` un valor con forma de UUID.* `deps.py:129-145` responde 400 a un `X-Client-Id` malformado en toda ruta, `/me` incluida; un valor ilegible dejaría a la consola sin forma de saber que tiene que soltarlo. Cualquier otro valor se lee como «Todos mis clientes». (Los ids de los fixtures no son UUID: en modo fixtures la elección vale para la pestaña y no sobrevive a una recarga.)
- *Decisión del plan: el proveedor envuelve el `Shell` dentro de `RequireAuth` y, además de `queryClient.clear()`, vuelve a montar lo que envuelve (`key`).* `clear()` (`query-core/src/queryClient.ts:632`) quita las consultas sin avisar a los observadores montados, que siguen pintando su último resultado hasta que algo los vuelve a renderizar. Con la `key`, el shell y la página se montan de nuevo y piden todo con la cabecera nueva; un formulario a medias del cliente anterior se va con ellos, que es lo correcto.
- *Decisión del plan: el selector es una ranura (`clientSelector`) de `AppShell`, el primer elemento de `.top-actions`.* `AppShell` se renderiza en sus pruebas y en `/__preview` sin `QueryClientProvider`, y el selector lee `/me`. Va primero porque es el contexto de todo lo demás del grupo, «Añadir un equipo» incluido; «antes de los glifos» se cumple. No se dibuja si `/me` no lista ningún cliente.
- *Decisión del plan: un 4403 en un socket que el servidor había aceptado pide `/me` y reconecta enseguida; uno en el saludo (el socket nunca abrió) pide `/me` y espera como cualquier otro cierre.* Reconectar enseguida a un alcance que el servidor acaba de rechazar sería un bucle (un técnico sin ningún cliente, con «Todos mis clientes», lo provoca).
- *Decisión del plan: el aviso único es un toast informativo que pinta el propio proveedor.* El toaster de `LanguageBridge` (`App.tsx:508-565`) es estado local suyo; el proveedor es el único sitio que existe también cuando ya no hay ningún cliente que mostrar en el selector.
- *Decisión del plan: `requestHeaders` borra cualquier `X-Client-Id` que traiga el llamador antes de decidir.* «El único sitio que arma las cabeceras» (§1.2) vale también contra una copia del llamador: dos cabeceras son un 400 (`deps.py:136-139`) y una en una ruta global, un 403.

- [ ] **Step 1: Escribir las pruebas que fallan de la elección y de la cabecera**

```ts
// frontend/src/lib/clientChoice.test.ts
/**
 * The client chosen in the top bar, as a value outside React (phase 3 design
 * §1.1). Kept in `localStorage["itms.client"]` as the id or `all`; read back
 * only when it is one: the server answers a malformed `X-Client-Id` with 400
 * on every route, `/me` included, so a stored value it cannot read would lock
 * the console out of learning that it should drop it.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CLIENT_STORAGE_KEY,
  adoptStoredClient,
  readStoredClient,
  selectedClient,
  setSelectedClient,
  subscribeClient,
} from "./clientChoice";

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";

afterEach(() => vi.restoreAllMocks());

describe("the stored choice", () => {
  it("reads a client id back, and anything else as every client", () => {
    window.localStorage.setItem(CLIENT_STORAGE_KEY, ACME);
    expect(readStoredClient()).toBe(ACME);
    window.localStorage.setItem(CLIENT_STORAGE_KEY, "all");
    expect(readStoredClient()).toBeNull();
    // Not a client id: sent, it would be a 400 on every request.
    window.localStorage.setItem(CLIENT_STORAGE_KEY, "acme");
    expect(readStoredClient()).toBeNull();
    window.localStorage.removeItem(CLIENT_STORAGE_KEY);
    expect(readStoredClient()).toBeNull();
  });

  it("reads as every client when the browser refuses storage", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("storage is blocked");
    });
    expect(readStoredClient()).toBeNull();
  });

  it("is written as the id or `all`, and told to its listeners once per change", () => {
    const heard = vi.fn();
    const stop = subscribeClient(heard);
    setSelectedClient(ACME);
    expect(selectedClient()).toBe(ACME);
    expect(window.localStorage.getItem(CLIENT_STORAGE_KEY)).toBe(ACME);
    setSelectedClient(ACME);
    expect(heard).toHaveBeenCalledTimes(1);
    setSelectedClient(null);
    expect(selectedClient()).toBeNull();
    expect(window.localStorage.getItem(CLIENT_STORAGE_KEY)).toBe("all");
    expect(heard).toHaveBeenCalledTimes(2);
    stop();
  });

  it("adopts what another tab stored", () => {
    window.localStorage.setItem(CLIENT_STORAGE_KEY, ACME);
    adoptStoredClient();
    expect(selectedClient()).toBe(ACME);
  });
});
```

```ts
// frontend/src/lib/api.clientScope.test.ts
/**
 * `X-Client-Id` (phase 3 design §1.2). The client chosen in the top bar
 * narrows every request this console sends -- the JSON ones through
 * `request()` and the raw file ones alike -- and a route of global
 * configuration never carries it: with it, the server answers 403
 * (`backend/tests/test_global_writes.py`). The list of those routes is
 * `globalRoutes.json`, generated from the backend's (Task 2), and matched by
 * method as well as path: `PATCH /orgs/current` is global, `GET /orgs/current`
 * is not.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient, requestHeaders } from "./api";

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";

function stubFetch(): ReturnType<typeof vi.fn> {
  const fetchMock = vi
    .fn()
    .mockImplementation(() => Promise.resolve(new Response("{}", { status: 200 })));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** The headers of the nth `fetch` call. */
function sent(fetchMock: ReturnType<typeof vi.fn>, call = 0): Headers {
  return new Headers((fetchMock.mock.calls[call] as [string, RequestInit])[1].headers);
}

function narrowedTo(client: string | null): ApiClient {
  return new ApiClient(() => "t", vi.fn(), "/api/v1", () => client);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("X-Client-Id", () => {
  it("names the client chosen in the top bar", async () => {
    const fetchMock = stubFetch();
    await narrowedTo(ACME).listDevices();
    expect(sent(fetchMock).get("X-Client-Id")).toBe(ACME);
  });

  it("is not sent with every client", async () => {
    const fetchMock = stubFetch();
    await narrowedTo(null).listDevices();
    expect(sent(fetchMock).has("X-Client-Id")).toBe(false);
  });

  it("is read on every request, not when the client was built", async () => {
    const fetchMock = stubFetch();
    let chosen: string | null = null;
    const api = new ApiClient(() => "t", vi.fn(), "/api/v1", () => chosen);
    await api.me();
    chosen = ACME;
    await api.me();
    expect(sent(fetchMock, 0).has("X-Client-Id")).toBe(false);
    expect(sent(fetchMock, 1).get("X-Client-Id")).toBe(ACME);
  });

  it("never travels on a route of global configuration, by method and path", async () => {
    const fetchMock = stubFetch();
    const api = narrowedTo(ACME);
    await api.listAuditEvents({ limit: 10 }); // GET /audit: GLOBAL_READS
    await api.updateCurrentOrg({ name: "Toeshee" }); // PATCH /orgs/current: GLOBAL_WRITES
    await api.getCurrentOrg(); // GET /orgs/current: not global
    expect(sent(fetchMock, 0).has("X-Client-Id")).toBe(false);
    expect(sent(fetchMock, 1).has("X-Client-Id")).toBe(false);
    expect(sent(fetchMock, 2).get("X-Client-Id")).toBe(ACME);
  });

  it("travels on the raw file routes too, and not on a global upload", async () => {
    const fetchMock = stubFetch();
    const api = narrowedTo(ACME);
    await api.exportPatchReport();
    await api.uploadPackageArtifact("p1", new File(["x"], "agent.msi"));
    expect(sent(fetchMock, 0).get("X-Client-Id")).toBe(ACME);
    // POST /deploy/packages/{package_id}/artifact is in GLOBAL_WRITES.
    expect(sent(fetchMock, 1).has("X-Client-Id")).toBe(false);
  });

  it("is decided by the builder alone, whatever a caller put in", () => {
    const own = { "X-Client-Id": "a-caller's-own" };
    expect(
      requestHeaders("t", own, { method: "GET", path: "/audit?limit=5", client: ACME }).has("X-Client-Id"),
    ).toBe(false);
    expect(
      requestHeaders("t", own, { method: "GET", path: "/devices", client: ACME }).get("X-Client-Id"),
    ).toBe(ACME);
    expect(requestHeaders("t", own).has("X-Client-Id")).toBe(false);
  });

  it("is decided on every request ApiClient builds headers for", () => {
    // A raw fetch path whose `requestHeaders(` call forgets the route sends no
    // header at all: the request runs with the person's whole scope, and the
    // page shows another client's rows under the one chosen.
    const source = readFileSync(resolve(import.meta.dirname, "api.ts"), "utf8");
    const calls = source
      .split("\n")
      .filter((line) => line.includes("requestHeaders(") && !line.includes("export function requestHeaders("));
    expect(calls.length).toBeGreaterThanOrEqual(18);
    expect(calls.filter((line) => !line.includes("this.route("))).toEqual([]);
  });
});
```

- [ ] **Step 2: Comprobar que fallan**

Run (desde `frontend/`): `npx vitest run src/lib/clientChoice.test.ts src/lib/api.clientScope.test.ts`
Expected: FAIL — `Failed to resolve import "./clientChoice"`, y en `api.clientScope.test.ts` `expected null to be '0b6a2c1e-…'` (hoy ningún `fetch` lleva la cabecera) y la prueba del código fuente lista las 18 líneas sin `this.route(`.

- [ ] **Step 3: La elección, la cabecera y `ApiClient`**

```ts
// frontend/src/lib/clientChoice.ts
/**
 * THE CLIENT CHOSEN IN THE TOP BAR, as a value outside React (phase 3 design
 * §1.1, §1.2, §1.5). The two things that send it are not components:
 * `ApiClient` reads it on every request for `X-Client-Id`, and the live
 * socket for `?client=`. Components read and change it through
 * `lib/clientScope.tsx`, which also empties the query cache on every change.
 * The same shape as `i18n/core.ts`'s active language, which `requestHeaders`
 * already reads.
 *
 * `null` is "All my clients": no header, no `?client=`, the person's whole
 * scope. Kept in `localStorage["itms.client"]` as the id or `all`; project 4's
 * rename of the brand renames it with `itms.sidebar.collapsed`.
 *
 * ONLY AN ID IS READ BACK. The server answers a malformed `X-Client-Id` with
 * 400 on every route, `/me` included (`backend/app/api/deps.py`), so a stored
 * value it cannot read would leave the console unable even to learn that it
 * should drop it. Anything else stored there reads as every client.
 *
 * INVARIANT 6: THIS GATES NOTHING. The header narrows the person's scope and
 * never widens it: a client outside it answers empty lists and 404s.
 */

export const CLIENT_STORAGE_KEY = "itms.client";

const EVERY_CLIENT = "all";
const CLIENT_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The stored choice: a client id, or `null` for every client (and for anything unreadable). */
export function readStoredClient(): string | null {
  // Storage throws in Safari private browsing and in a webview with site
  // data blocked (`shell/AppShell.tsx`'s `readCollapsed`): every client, then.
  try {
    const stored = window.localStorage.getItem(CLIENT_STORAGE_KEY);
    return stored !== null && CLIENT_ID.test(stored) ? stored : null;
  } catch {
    return null;
  }
}

function writeStoredClient(id: string | null): void {
  try {
    window.localStorage.setItem(CLIENT_STORAGE_KEY, id ?? EVERY_CLIENT);
  } catch {
    // Storage blocked: the choice lasts as long as this tab.
  }
}

let active: string | null = readStoredClient();
const listeners = new Set<() => void>();

/** The client chosen now, or `null` for every client. */
export function selectedClient(): string | null {
  return active;
}

/** Told of every change, synchronously, before React renders anything for it. */
export function subscribeClient(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function apply(id: string | null): void {
  if (id === active) return;
  active = id;
  listeners.forEach((listener) => listener());
}

/** Choose a client, or `null` for every client: stored, and sent from the next request on. */
export function setSelectedClient(id: string | null): void {
  writeStoredClient(id);
  apply(id);
}

/** Take what another tab stored (the browser's `storage` event), without writing it back. */
export function adoptStoredClient(): void {
  apply(readStoredClient());
}
```

En `frontend/src/lib/api.ts`:

1. Junto al import de `getLanguage` (`:3`):

```ts
import { getLanguage } from "../i18n/core";
import { isGlobalRoute } from "./globalRoutes";
```

2. Inmediatamente antes de `export interface Me {` (`:767`):

```ts
/**
 * One client the signed-in person holds any role in (`GET /me`'s `clients`,
 * phase 3 design §1.3), archived ones left out. Mirrors
 * `backend/app/api/v1/me.py::MeClient`. Not narrowed by `X-Client-Id`: it is
 * what the top bar's selector offers and what a new row's Client field
 * filters (`lib/clientSelection.ts`). It is the person's own data, so it needs
 * no `clients.read`.
 */
export interface MeClient {
  id: string;
  name: string;
  kind: "customer" | "internal" | "unassigned";
  /** Optional modules on for this client: `budget`, `bcp`, `change`. */
  enabled_modules: string[];
  /** The codes the person holds IN THIS CLIENT, sorted. */
  permissions: string[];
}
```

3. En `Me` (`:767-772`), sustituir

```ts
export interface Me {
  user_id: string;
  org_id: string;
  email: string;
  display_name: string;
  permissions: string[];
```

por

```ts
export interface Me {
  user_id: string;
  org_id: string;
  email: string;
  display_name: string;
  /**
   * The codes the person holds in AT LEAST ONE client of the request's scope
   * -- their clients, narrowed by `X-Client-Id` when one is chosen (phase 3
   * design §1.3, `require_permission`'s rule). What the menu and the pages
   * render from.
   */
  permissions: string[];
  /**
   * The person's clients, archived ones left out, each with the codes held
   * there (phase 3 design §1.3). Not narrowed by `X-Client-Id`.
   *
   * OPTIONAL for the reason the profile fields below are: an API older than
   * phase 3 does not send it, and ~90 `Me` literals in the tests would
   * otherwise need it. Read as `?? []`.
   */
  clients?: MeClient[];
  /**
   * The codes held on EVERY client (`require_permission_on_every_client`'s
   * rule), not narrowed by `X-Client-Id`: what a screen of configuration every
   * client shares offers its writes on (phase 3 design §2).
   *
   * OPTIONAL like `clients`. Absent, `permissions` IS this set -- before phase
   * 3, `/me` returned only the codes held on every client -- and
   * `useEverywherePermissions` reads it so.
   */
  permissions_everywhere?: string[];
```

4. Sustituir `requestHeaders` y su comentario (`:1487-1500`):

```ts
/**
 * Where a request goes, for the one thing `requestHeaders` decides beyond the
 * bearer and the language: whether it carries `X-Client-Id`.
 */
export interface RequestRoute {
  /** `GET`, `POST`, … */
  method: string;
  /** Relative to `/api/v1`. A query string is ignored. */
  path: string;
  /** The client chosen in the top bar, or `null` for every client. */
  client: string | null;
}

/**
 * The one place request headers are built: bearer, language and client.
 *
 * EVERY fetch in the console goes through this, including the raw ones that
 * bypass `request()` for binary bodies. A path that builds its own `Headers`
 * sends no `Accept-Language`, so the server answers it in English whatever the
 * person chose; `api.language.test.ts` fails the build on such a path.
 *
 * `X-Client-Id` (phase 3 design §1.2) names the client chosen in the top bar,
 * EXCEPT on a route of global configuration (`globalRoutes.json`, generated
 * from `backend/tests/test_global_writes.py`): narrowed, those answer 403.
 * Decided here or nowhere: whatever a caller put in `extra` is dropped first,
 * because a second copy is a 400 and one on a global route a 403.
 * `api.clientScope.test.ts` fails the build on an `ApiClient` call that does
 * not say where it goes.
 */
export function requestHeaders(
  token: string | null | undefined,
  extra?: HeadersInit,
  route?: RequestRoute,
): Headers {
  const headers = new Headers(extra);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  headers.set("Accept-Language", getLanguage());
  headers.delete("X-Client-Id");
  if (route?.client) {
    const path = route.path.split("?")[0] ?? route.path;
    if (!isGlobalRoute(route.method, path)) headers.set("X-Client-Id", route.client);
  }
  return headers;
}
```

5. Sustituir el constructor y la cabecera de `request()` (`:1502-1510`):

```ts
export class ApiClient {
  constructor(
    private readonly getToken: () => string | null,
    private readonly onUnauthorized: () => void,
    private readonly base: string = "/api/v1",
    /**
     * The client chosen in the top bar (`lib/clientChoice.ts`), read on EVERY
     * request: a choice made after this client was built still reaches the
     * next request. `null` is every client. The default sends no header,
     * which is what a test or a caller with no selector wants.
     */
    private readonly getClient: () => string | null = () => null,
  ) {}

  /** What `requestHeaders` needs to know about one request to this API. */
  private route(method: string | undefined, path: string): RequestRoute {
    return { method: (method ?? "GET").toUpperCase(), path, client: this.getClient() };
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = requestHeaders(this.getToken(), init.headers, this.route(init.method, path));
```

6. Las 17 llamadas crudas. Cada línea `const headers = requestHeaders(this.getToken());` (o con `extra`) pasa a nombrar su ruta; nada más cambia en esos métodos:

| Línea | Método | Nueva línea |
|---|---|---|
| `:2058` | `fetchFile(path)` | `const headers = requestHeaders(this.getToken(), undefined, this.route("GET", path));` |
| `:2247` | `uploadDocumentVersion` | `const headers = requestHeaders(this.getToken(), undefined, this.route("POST", `/docs/documents/${id}/versions`));` |
| `:2277` | `downloadDocumentVersion` | `const headers = requestHeaders(this.getToken(), undefined, this.route("GET", `/docs/documents/${id}/versions/${version}/content`));` |
| `:2499` | `getRemoteRecording` | `const headers = requestHeaders(this.getToken(), undefined, this.route("GET", `/remote/sessions/${id}/recording`));` |
| `:2752` | `exportPatchReport` | `const headers = requestHeaders(this.getToken(), undefined, this.route("GET", "/patch/report/export"));` |
| `:2781` | `uploadPackageArtifact` | `const headers = requestHeaders(this.getToken(), undefined, this.route("POST", `/deploy/packages/${packageId}/artifact`));` |
| `:3169` | `uploadAgentReleaseArtifact` | `const headers = requestHeaders(this.getToken(), undefined, this.route("POST", `/agent/releases/${releaseId}/artifact`));` |
| `:3329` | `downloadInstallLinkMsi` | `const headers = requestHeaders(this.getToken(), { "Content-Type": "application/json" }, this.route("POST", `/sites/${siteId}/install-link/msi`));` |
| `:3994` | `uploadExpenseAttachment` | `const headers = requestHeaders(this.getToken(), undefined, this.route("POST", `/budget/expenses/${id}/attachment`));` |
| `:4014` | `downloadExpenseAttachment` | `const headers = requestHeaders(this.getToken(), undefined, this.route("GET", `/budget/expenses/${id}/attachment`));` |
| `:4211` | `uploadTicketAttachment` | `const headers = requestHeaders(this.getToken(), undefined, this.route("POST", `/service-desk/tickets/${id}/attachments`));` |
| `:4232` | `downloadTicketAttachment` | `const headers = requestHeaders(this.getToken(), undefined, this.route("GET", `/service-desk/attachments/${attachmentId}`));` |
| `:4500` | `uploadPortalAttachment` | `const headers = requestHeaders(this.getToken(), undefined, this.route("POST", `/portal/tickets/${id}/attachments`));` |
| `:4524` | `downloadPortalAttachment` | `const headers = requestHeaders(this.getToken(), undefined, this.route("GET", `/portal/tickets/${ticketId}/attachments/${attachmentId}`));` |
| `:5144` | `ipamImportRequest(path, file)` | `const headers = requestHeaders(this.getToken(), undefined, this.route("POST", path));` |
| `:5185` | `ipamCsv(path)` | `const headers = requestHeaders(this.getToken(), undefined, this.route("GET", path));` |
| `:5370` | `exportReport` | `const headers = requestHeaders(this.getToken(), undefined, this.route("GET", `/reports/${key}/export.csv`));` |

Cada llamada queda en una sola línea: la prueba del código fuente de `api.clientScope.test.ts` lee línea a línea, y `api.language.test.ts` sigue encontrando `requestHeaders(` entre el inicio del método y su `fetch(`.

`frontend/src/lib/ApiContext.tsx`: import y construcción (`:38-46`):

```tsx
import { ApiClient } from "./api";
import { accessToken, signIn } from "./auth";
import { selectedClient } from "./clientChoice";
```

```tsx
  const client = useMemo(
    () =>
      FIXTURE_MODE
        ? fixture
        : new ApiClient(
            accessToken,
            () => {
              void signIn();
            },
            "/api/v1",
            // The top bar's choice, read on every request (`X-Client-Id`).
            selectedClient,
          ),
    [fixture],
  );
```

`frontend/src/test/setup.ts`: tras `import { setActiveLanguage } from "../i18n/core";` (`:60`) añadir `import { setSelectedClient } from "../lib/clientChoice";`, y en el `beforeEach` (`:65-68`), tras `setActiveLanguage("en");`:

```ts
  // ...and with every client chosen, so one test's choice in the top bar can
  // never narrow the next test's requests (`lib/clientChoice.ts`).
  setSelectedClient(null);
```

- [ ] **Step 4: Comprobar que pasan**

Run: `npx vitest run src/lib/clientChoice.test.ts src/lib/api.clientScope.test.ts src/lib/api.language.test.ts src/lib/api.test.ts`
Expected: PASS. `api.language.test.ts` sigue verde sin tocarlo.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/clientChoice.ts frontend/src/lib/clientChoice.test.ts frontend/src/lib/api.ts frontend/src/lib/api.clientScope.test.ts frontend/src/lib/ApiContext.tsx frontend/src/test/setup.ts
git commit -m "feat(console): every request names the chosen client in X-Client-Id, except global configuration (phase 3 Task 7)"
```

- [ ] **Step 6: Escribir las pruebas que fallan del proveedor, el selector y su sitio**

```tsx
// frontend/src/lib/clientScope.test.tsx
/**
 * The client selection, for components (phase 3 design §1.1, §1.4).
 *
 * WHAT A CHANGE OF CLIENT OWES THE PAGE. Nothing read for one client may stay
 * on screen under another: the query cache is emptied and the page under the
 * provider starts again, asking everything with the new header. A choice /me
 * does not list -- archived, or the person lost access -- falls back to every
 * client and says so once. A choice made in another tab is taken the same way.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import { createElement, useEffect, type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient, Me, MeClient } from "./api";
import { CLIENT_STORAGE_KEY, selectedClient, setSelectedClient } from "./clientChoice";
import { ClientScopeProvider, useClientScope } from "./clientScope";

const mockUseApi = vi.fn();
vi.mock("./ApiContext", () => ({ useApi: () => mockUseApi() }));

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";
const ZETA = "5c9e1d2f-3a4b-4c5d-8e6f-7a8b9c0d1e02";
const INTERNAL = "9f8e7d6c-5b4a-4321-8fed-cba987654303";
const UNASSIGNED = "1a2b3c4d-5e6f-4789-8abc-def012345604";
const STALE = "7e6d5c4b-3a29-4180-9fed-cba987654305";

function client(id: string, name: string, kind: MeClient["kind"] = "customer"): MeClient {
  return { id, name, kind, enabled_modules: [], permissions: ["devices.read"] };
}

const ME: Me = {
  user_id: "u1",
  org_id: INTERNAL,
  email: "tech@example.com",
  display_name: "Tech",
  permissions: ["devices.read"],
  clients: [
    client(ZETA, "Zeta Foods"),
    client(UNASSIGNED, "Unassigned", "unassigned"),
    client(INTERNAL, "Toeshee", "internal"),
    client(ACME, "Acme Dental"),
  ],
};

function newQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

let mounts = 0;

/** A page under the provider: counts its mounts and says what it sees. */
function Page() {
  useEffect(() => {
    mounts += 1;
  }, []);
  const scope = useClientScope();
  return <p data-testid="page">{scope.ready ? `ready:${scope.selected ?? "all"}` : "waiting"}</p>;
}

function renderScoped(me: Me = ME, queryClient: QueryClient = newQueryClient()) {
  const api = { me: vi.fn().mockResolvedValue(me) };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  render(
    <QueryClientProvider client={queryClient}>
      <ClientScopeProvider>
        <Page />
      </ClientScopeProvider>
    </QueryClientProvider>,
  );
  return { api, queryClient };
}

beforeEach(() => {
  mounts = 0;
});

describe("ClientScopeProvider", () => {
  it("empties the query cache and starts the page again when the client changes", async () => {
    const { api, queryClient } = renderScoped();
    expect(await screen.findByText("ready:all")).toBeInTheDocument();
    queryClient.setQueryData(["devices"], ["a device of every client"]);

    act(() => setSelectedClient(ACME));

    expect(queryClient.getQueryData(["devices"])).toBeUndefined();
    expect(await screen.findByText(`ready:${ACME}`)).toBeInTheDocument();
    expect(mounts).toBe(2);
    // `/me` itself is asked again, under the new narrowing.
    expect(api.me.mock.calls.length).toBeGreaterThan(1);
  });

  it("falls back to every client, and says so once, when /me does not list the choice", async () => {
    setSelectedClient(STALE);
    renderScoped();

    expect(
      await screen.findByText(
        "The client you had chosen is no longer available to you, so the console is showing all your clients.",
      ),
    ).toBeInTheDocument();
    expect(await screen.findByText("ready:all")).toBeInTheDocument();
    expect(selectedClient()).toBeNull();
    expect(window.localStorage.getItem(CLIENT_STORAGE_KEY)).toBe("all");
    expect(screen.getAllByRole("status")).toHaveLength(1);
  });

  it("takes a choice made in another tab like one made here", async () => {
    const { queryClient } = renderScoped();
    await screen.findByText("ready:all");
    queryClient.setQueryData(["devices"], ["a device of every client"]);

    window.localStorage.setItem(CLIENT_STORAGE_KEY, ACME);
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: CLIENT_STORAGE_KEY, newValue: ACME }));
    });

    expect(selectedClient()).toBe(ACME);
    expect(queryClient.getQueryData(["devices"])).toBeUndefined();
    expect(await screen.findByText(`ready:${ACME}`)).toBeInTheDocument();
  });
});

describe("useClientScope", () => {
  function hookScope(me: Me = ME) {
    mockUseApi.mockReturnValue({ me: vi.fn().mockResolvedValue(me) } as unknown as ApiClient);
    const queryClient = newQueryClient();
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    return renderHook(() => useClientScope(), { wrapper });
  }

  it("lists the person's clients by name, Unassigned last, once /me answers", async () => {
    const { result } = hookScope();
    expect(result.current.ready).toBe(false);
    expect(result.current.clients).toEqual([]);
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.clients.map((c) => c.name)).toEqual([
      "Acme Dental",
      "Toeshee",
      "Zeta Foods",
      "Unassigned",
    ]);
  });

  it("is not ready while the choice is not one of /me's clients", async () => {
    setSelectedClient(STALE);
    const { result } = hookScope();
    await waitFor(() => expect(result.current.clients).toHaveLength(4));
    expect(result.current.ready).toBe(false);
  });
});
```

```tsx
// frontend/src/shell/ClientSelector.test.tsx
/**
 * The top bar's client selector (phase 3 design §1.1): "All my clients", then
 * the person's clients from /me by name, Unassigned last. A choice is stored
 * (`itms.client`) and narrows every request from then on.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../i18n/core";
import type { ApiClient, Me, MeClient } from "../lib/api";
import { CLIENT_STORAGE_KEY, selectedClient } from "../lib/clientChoice";
import { ClientSelector } from "./ClientSelector";

const mockUseApi = vi.fn();
vi.mock("../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";
const ZETA = "5c9e1d2f-3a4b-4c5d-8e6f-7a8b9c0d1e02";
const INTERNAL = "9f8e7d6c-5b4a-4321-8fed-cba987654303";
const UNASSIGNED = "1a2b3c4d-5e6f-4789-8abc-def012345604";

function client(id: string, name: string, kind: MeClient["kind"] = "customer"): MeClient {
  return { id, name, kind, enabled_modules: [], permissions: ["devices.read"] };
}

const ME: Me = {
  user_id: "u1",
  org_id: INTERNAL,
  email: "tech@example.com",
  display_name: "Tech",
  permissions: ["devices.read"],
  clients: [
    client(ZETA, "Zeta Foods"),
    client(UNASSIGNED, "Unassigned", "unassigned"),
    client(INTERNAL, "Toeshee", "internal"),
    client(ACME, "Acme Dental"),
  ],
};

function renderSelector(me: Me = ME) {
  const meFn = vi.fn().mockResolvedValue(me);
  mockUseApi.mockReturnValue({ me: meFn } as unknown as ApiClient);
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ClientSelector />
    </QueryClientProvider>,
  );
  return meFn;
}

describe("ClientSelector", () => {
  it("offers All my clients, then the person's clients by name, Unassigned last", async () => {
    renderSelector();
    const box = await screen.findByRole("combobox", { name: "Client" });
    expect(within(box).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "All my clients",
      "Acme Dental",
      "Toeshee",
      "Zeta Foods",
      "Unassigned",
    ]);
    expect(box).toHaveValue("all");
  });

  it("stores the choice, and every client as `all`", async () => {
    const user = userEvent.setup();
    renderSelector();
    const box = await screen.findByRole("combobox", { name: "Client" });

    await user.selectOptions(box, ACME);
    expect(selectedClient()).toBe(ACME);
    expect(window.localStorage.getItem(CLIENT_STORAGE_KEY)).toBe(ACME);
    expect(box).toHaveValue(ACME);

    await user.selectOptions(box, "all");
    expect(selectedClient()).toBeNull();
    expect(window.localStorage.getItem(CLIENT_STORAGE_KEY)).toBe("all");
  });

  it("is not drawn for someone /me lists no client for", async () => {
    const me = renderSelector({ ...ME, clients: [] });
    await waitFor(() => expect(me).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("speaks the person's language", async () => {
    setActiveLanguage("es");
    renderSelector();
    const box = await screen.findByRole("combobox", { name: "Cliente" });
    expect(within(box).getByRole("option", { name: "Todos mis clientes" })).toBeInTheDocument();
    expect(within(box).getByRole("option", { name: "Sin asignar" })).toBeInTheDocument();
  });
});
```

En `frontend/src/shell/AppShell.test.tsx`, dentro de `describe("AppShell", …)`, tras «renders the page inside the chrome»:

```tsx
  it("draws the client selector first in the top bar's cluster (phase 3 design §1.1)", () => {
    render(
      <MemoryRouter>
        <AppShell
          permissions={["devices.read", "health.read"]}
          orgName="Operations"
          title="Devices"
          user={{ name: "A B", role: "Operator" }}
          onSignOut={vi.fn()}
          clientSelector={<span data-testid="client-selector" />}
        >
          <p>x</p>
        </AppShell>
      </MemoryRouter>,
    );
    expect(document.querySelector(".top-actions")?.firstElementChild).toBe(
      screen.getByTestId("client-selector"),
    );
  });
```

En `frontend/src/App.shell.test.tsx`, al final del archivo:

```tsx
describe("the top bar's client selector (phase 3 design §1.1)", () => {
  it("is drawn from /me's clients", async () => {
    const api = fakeApi(OPERATOR);
    api.me = vi.fn().mockResolvedValue({
      user_id: "u1",
      display_name: "Ada",
      email: "ada@example.test",
      org_id: "org-1",
      roles: [],
      permissions: OPERATOR,
      clients: [
        { id: "org-1", name: "Toeshee", kind: "internal", enabled_modules: [], permissions: OPERATOR },
      ],
    });
    apiRef.current = api;
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/devices"]}>
          <Shell>
            <Where />
          </Shell>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const selector = await screen.findByRole("combobox", { name: "Client" });
    expect(within(selector).getByRole("option", { name: "Toeshee" })).toBeInTheDocument();
  });
});
```

En `frontend/src/App.auth.test.tsx`: el import de testing-library pasa a `import { act, render, screen, waitFor } from "@testing-library/react";`, se añaden `import { useEffect } from "react";` y `import { setSelectedClient } from "./lib/clientChoice";` junto a los demás imports estáticos, y dentro de `describe("an unauthenticated visitor", …)`, tras «renders the page once authenticated, with no redirect»:

```tsx
  it("starts the page again, on an empty cache, when the client changes (phase 3 design §1.4)", async () => {
    isAuthenticated.mockReturnValue(true);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let mounts = 0;
    function Secret() {
      useEffect(() => {
        mounts += 1;
      }, []);
      return <p>secret</p>;
    }
    render(
      <QueryClientProvider client={queryClient}>
        <ApiProvider>
          <MemoryRouter>
            <RequireAuth>
              <Secret />
            </RequireAuth>
          </MemoryRouter>
        </ApiProvider>
      </QueryClientProvider>,
    );
    expect(await screen.findByText("secret")).toBeInTheDocument();
    queryClient.setQueryData(["devices"], ["a device of every client"]);

    act(() => setSelectedClient("6b1f2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"));

    await waitFor(() => expect(mounts).toBe(2));
    expect(queryClient.getQueryData(["devices"])).toBeUndefined();
  });
```

En `frontend/src/fixtures/client.test.ts`, dentro de `describe("fixtureRegistry", …)`, tras «signs the fixture session in with every permission the nav gates on»:

```ts
  it("signs the fixture session in on every client, listing the clients the settings fixtures list", async () => {
    const me = await fixtureClient().me();
    expect(me.permissions_everywhere).toEqual(me.permissions);
    expect((me.clients ?? []).map((client) => [client.id, client.kind])).toEqual([
      ["org-toeshee", "internal"],
      ["cli-logistics", "customer"],
      ["cli-unassigned", "unassigned"],
    ]);
  });
```

- [ ] **Step 7: Comprobar que fallan**

Run: `npx vitest run src/lib/clientScope.test.tsx src/shell/ClientSelector.test.tsx src/shell/AppShell.test.tsx src/App.shell.test.tsx src/App.auth.test.tsx src/fixtures/client.test.ts`
Expected: FAIL — `Failed to resolve import "./clientScope"` / `"./ClientSelector"`; en `AppShell.test.tsx`, `clientSelector` no es una prop (`tsc`) y `.top-actions` empieza por «Add a computer»; en `App.shell.test.tsx` no hay combobox «Client»; en `App.auth.test.tsx` `mounts` se queda en 1; en `client.test.ts` `permissions_everywhere` es `undefined`.

- [ ] **Step 8: El proveedor, el selector, el shell y los textos**

```tsx
// frontend/src/lib/clientScope.tsx
/**
 * THE CLIENT CHOSEN IN THE TOP BAR, FOR COMPONENTS (phase 3 design §1.1, §1.4).
 *
 * The choice itself lives outside React (`./clientChoice`), because the two
 * things that send it -- `ApiClient` (`X-Client-Id`) and the live socket
 * (`?client=`) -- are not components. This module is what components read:
 * the choice, the person's clients from `/me`, and whether the two agree.
 *
 * `ClientScopeProvider` is mounted ONCE, around the signed-in shell
 * (`App.tsx`'s `RequireAuth`), and does what a change of client owes the page:
 *
 * 1. IT EMPTIES THE QUERY CACHE before anything renders again (§1.4). Every
 *    cached answer was read under the previous choice; none may be shown
 *    under the next one.
 * 2. IT STARTS THE PAGE AGAIN (a `key` on its children). `clear()` removes the
 *    queries, but a mounted component keeps the result it last rendered until
 *    something renders it again -- so the shell and the page under it are
 *    mounted afresh, and every query they hold is asked again with the new
 *    header. Their own state (a half-filled form for the previous client)
 *    goes with them, which is the point.
 * 3. IT FALLS BACK TO EVERY CLIENT, and says so once, when the choice is not
 *    one of `/me`'s clients -- archived, or the person lost access (§1.1, §6).
 *
 * A choice made in another tab arrives by the browser's `storage` event and
 * is handled exactly like one made here.
 *
 * INVARIANT 6: THIS GATES NOTHING. The server narrows by the header and never
 * widens: a client outside the person's scope answers empty lists and 404s.
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";

import { translate } from "../i18n/core";
import { Toaster, type ToastItem } from "../shell/Toaster";
import type { ApiError, Me, MeClient } from "./api";
import { useApi } from "./ApiContext";
import {
  CLIENT_STORAGE_KEY,
  adoptStoredClient,
  selectedClient,
  setSelectedClient,
  subscribeClient,
} from "./clientChoice";

export { CLIENT_STORAGE_KEY };

export interface ClientScope {
  /** The client chosen in the top bar, or `null` for "All my clients". */
  selected: string | null;
  /** Choose a client, or `null` for every client: stored, and sent from the next request on. */
  select(id: string | null): void;
  /** The person's clients (`/me`), by name, Unassigned last. Empty until `/me` answers. */
  clients: MeClient[];
  /** `/me` has answered, and the choice is one of its clients (or every client). */
  ready: boolean;
}

/** By name, with the Unassigned client last (design §1.1). */
export function orderClients(clients: readonly MeClient[]): MeClient[] {
  return [...clients].sort(
    (a, b) =>
      Number(a.kind === "unassigned") - Number(b.kind === "unassigned") ||
      a.name.localeCompare(b.name),
  );
}

function useMeQuery() {
  const api = useApi();
  return useQuery<Me, ApiError>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
}

function useSelected(): string | null {
  return useSyncExternalStore(subscribeClient, selectedClient, selectedClient);
}

export function useClientScope(): ClientScope {
  const me = useMeQuery();
  const selected = useSelected();
  const clients = useMemo(() => orderClients(me.data?.clients ?? []), [me.data]);
  const ready =
    me.data !== undefined && (selected === null || clients.some((client) => client.id === selected));
  return { selected, select: setSelectedClient, clients, ready };
}

let noticeSeq = 0;

export function ClientScopeProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const me = useMeQuery();
  const selected = useSelected();
  const [notices, setNotices] = useState<ToastItem[]>([]);

  // 1. Inside the change itself: listeners run synchronously when the choice
  //    moves, and React only renders after the change has returned.
  useEffect(() => subscribeClient(() => queryClient.clear()), [queryClient]);

  // A choice made in another tab. `key === null` is that tab clearing storage.
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key === CLIENT_STORAGE_KEY || event.key === null) adoptStoredClient();
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  // 3. A choice /me does not list. `undefined` is an API older than phase 3,
  //    which names no clients: there is nothing to check it against.
  const listed = me.data?.clients;
  useEffect(() => {
    if (selected === null || listed === undefined) return;
    if (listed.some((client) => client.id === selected)) return;
    noticeSeq += 1;
    setNotices([{ id: `client-gone-${noticeSeq}`, title: translate("lib.clientScope.gone"), tone: "info" }]);
    setSelectedClient(null);
  }, [selected, listed]);

  const dismiss = useCallback(
    (id: string) => setNotices((current) => current.filter((notice) => notice.id !== id)),
    [],
  );

  return (
    <>
      {/* 2. A new key is a new page: every query asked again, every form empty. */}
      <Fragment key={selected ?? "all"}>{children}</Fragment>
      <Toaster toasts={notices} onDismiss={dismiss} />
    </>
  );
}
```

```tsx
// frontend/src/shell/ClientSelector.tsx
/**
 * THE TOP BAR'S CLIENT SELECTOR (phase 3 design §1.1).
 *
 * "All my clients" first, then the person's clients from `/me` by name, the
 * Unassigned client last -- `/me` lists it only for someone holding a role on
 * every client, the same rule as `GET /clients`. Archived clients are not in
 * `/me`, so they are not here. A choice is stored (`itms.client`) and narrows
 * every request from then on (`X-Client-Id`); the provider empties the cache
 * and starts the page again (`lib/clientScope.tsx`).
 *
 * NOT DRAWN when `/me` lists no client: there is nothing to choose between,
 * and a select with one option is furniture.
 *
 * A NATIVE `<select>` in the kit's `.select-field`, the control the filter
 * bars already use: this phase adds the selector, it does not restyle the
 * chrome (project 3 does).
 */

import { useT } from "../i18n/context";
import { useClientScope } from "../lib/clientScope";

/** The option standing for every client, the choice `null`. Never a client id. */
const EVERY_CLIENT = "all";

export function ClientSelector() {
  const t = useT();
  const { selected, select, clients } = useClientScope();
  if (clients.length === 0) return null;
  const value =
    selected !== null && clients.some((client) => client.id === selected) ? selected : EVERY_CLIENT;
  return (
    <select
      className="select-field top-client"
      aria-label={t("shell.clientSelector.label")}
      value={value}
      onChange={(event) => select(event.target.value === EVERY_CLIENT ? null : event.target.value)}
    >
      <option value={EVERY_CLIENT}>{t("shell.clientSelector.all")}</option>
      {clients.map((client) => (
        <option key={client.id} value={client.id}>
          {client.kind === "unassigned" ? t("lib.clients.unassigned") : client.name}
        </option>
      ))}
    </select>
  );
}
```

`frontend/src/shell/AppShell.tsx`:

1. En `AppShellProps`, tras `onAsk?: (question: string) => void;` (`:73`):

```tsx
  /**
   * The top bar's client selector (phase 3 design §1.1), drawn first in the
   * cluster. A SLOT, NOT A COMPONENT THE SHELL RENDERS ITSELF: the selector
   * reads `/me` through the query client, and this shell also renders where
   * there is none -- its own tests and `/__preview`.
   */
  clientSelector?: ReactNode;
```

2. En la desestructuración, tras `onAsk,` (`:212`): `clientSelector,`.

3. Primer hijo de `<div className="top-actions">` (`:485`):

```tsx
          <div className="top-actions">
            {/* THE CLIENT EVERYTHING IN THIS CLUSTER, AND ON THE PAGE, IS FOR
                (phase 3 design §1.1): first, because it is the context of the
                rest -- "Add a computer" included. */}
            {clientSelector}
```

`frontend/src/shell/shell.css`, tras `.top-actions { display: flex; align-items: center; gap: 10px; }` (`:284`):

```css
/* THE CLIENT SELECTOR (phase 3 design §1.1). A select in a toolbar row, not a
 * form field: `.select-field`'s `width: 100%` would claim the whole cluster,
 * so it takes the geometry `.tb .select-field` already gives one in
 * `components.css`, capped so a long client name cannot push the account menu
 * off a narrow bar. */
.top-client { width: auto; flex: 0 1 auto; min-width: 0; max-width: 220px; }
```

y en el bloque `@media (max-width: 640px)` (`:724`), tras `.topbar { gap: 8px; padding: 0 10px; }`:

```css
  /* On a handset the selector gives way first: the name truncates inside the
   * box rather than the bar scrolling sideways. */
  .top-client { max-width: 120px; }
```

`frontend/src/App.tsx`:

1. Imports (tras `:17`):

```tsx
import { ClientScopeProvider } from "./lib/clientScope";
import { ClientSelector } from "./shell/ClientSelector";
```

2. Al comentario de `queryClient` (`:130-135`), antes de `export const queryClient`, añadir el párrafo:

```ts
// A change of the top bar's client empties it (`queryClient.clear()`, in
// `lib/clientScope.tsx`'s provider, which reaches this same client through
// `useQueryClient`): nothing read for one client is shown under another.
```

3. En `<AppShell` de `Shell` (`:321`), tras `permissions={permissions}`:

```tsx
      clientSelector={<ClientSelector />}
```

4. `RequireAuth` (`:425`): sustituir `if (authed) return <Shell>{children}</Shell>;` por

```tsx
  // INSIDE THE GUARD, AROUND THE SHELL: the client selection needs `/me`, which
  // only a signed-in session can ask, and a change of client starts the shell
  // and the page again (`lib/clientScope.tsx`).
  if (authed) {
    return (
      <ClientScopeProvider>
        <Shell>{children}</Shell>
      </ClientScopeProvider>
    );
  }
```

`frontend/src/fixtures/core.ts`: el import de `:23` pasa a `import type { CurrentOrg, Device, DeviceInventory, DeviceState, Me, MeClient } from "../lib/api";`, y `me` (`:91-104`) queda:

```ts
/**
 * The fixture session's clients (`/me`'s `clients`, phase 3 design §1.3): the
 * three `modules/settings/fixtures.ts` lists for `GET /clients`, with every
 * code in each -- this session holds every code on every client.
 */
const myClients = (codes: string[]): MeClient[] => [
  { id: ORG_ID, name: "Toeshee", kind: "internal", enabled_modules: ["budget", "bcp", "change"], permissions: codes },
  { id: "cli-logistics", name: "Andes Logistics", kind: "customer", enabled_modules: [], permissions: codes },
  { id: "cli-unassigned", name: "Unassigned", kind: "unassigned", enabled_modules: [], permissions: codes },
];

const me = (): Me => {
  const codes = allPermissions();
  return {
    user_id: "usr-helgar",
    org_id: ORG_ID,
    email: "helgar.palmieri@toeshee.example",
    display_name: "Helgar Palmieri",
    permissions: codes,
    permissions_everywhere: codes,
    clients: myClients(codes),
    roles: ["org-admin"],
    phone: "+351 21 000 0000",
    timezone: "Europe/Lisbon",
    // No saved language, so the console resolves it from this browser's choice
    // (`itms.lang`), which is how the harmony gate walks every route in each
    // language (`scripts/harmony.mjs --lang`).
    locale: null,
  };
};
```

Textos. `frontend/src/i18n/en/shell.ts`, tras `toaster: { dismiss: "Dismiss" },`:

```ts
  // The top bar's client selector (shell/ClientSelector.tsx, phase 3 design §1.1).
  clientSelector: {
    label: "Client",
    all: "All my clients",
  },
```

`frontend/src/i18n/es/shell.ts`, tras `toaster: { dismiss: "Descartar" },`:

```ts
  clientSelector: {
    label: "Cliente",
    all: "Todos mis clientes",
  },
```

`frontend/src/i18n/en/lib.ts`, tras la sección `internalClient` (`:45-47`):

```ts
  // The client selection (lib/clientScope.tsx, phase 3 design §1.1).
  clientScope: {
    gone: "The client you had chosen is no longer available to you, so the console is showing all your clients.",
  },
  // Clients as the console names them (shell/ClientSelector.tsx, phase 3 design §1.1).
  clients: {
    unassigned: "Unassigned",
  },
```

`frontend/src/i18n/es/lib.ts`, tras la sección `internalClient` (`:44-46`):

```ts
  clientScope: {
    gone: "El cliente que habías elegido ya no está disponible para ti, así que la consola muestra todos tus clientes.",
  },
  clients: {
    unassigned: "Sin asignar",
  },
```

- [ ] **Step 9: Comprobar que pasan**

Run: `npx vitest run src/lib/clientScope.test.tsx src/shell/ClientSelector.test.tsx src/shell/AppShell.test.tsx src/App.shell.test.tsx src/App.auth.test.tsx src/App.crumb.test.tsx src/App.roleChip.test.tsx src/App.language.test.tsx src/fixtures/client.test.ts src/i18n`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add frontend/src/lib/clientScope.tsx frontend/src/lib/clientScope.test.tsx frontend/src/shell/ClientSelector.tsx frontend/src/shell/ClientSelector.test.tsx frontend/src/shell/AppShell.tsx frontend/src/shell/AppShell.test.tsx frontend/src/shell/shell.css frontend/src/App.tsx frontend/src/App.shell.test.tsx frontend/src/App.auth.test.tsx frontend/src/fixtures/core.ts frontend/src/fixtures/client.test.ts frontend/src/i18n/en/shell.ts frontend/src/i18n/es/shell.ts frontend/src/i18n/en/lib.ts frontend/src/i18n/es/lib.ts
git commit -m "feat(console): the client selector; a change empties the cache and starts the page again (phase 3 Task 7)"
```

- [ ] **Step 11: Escribir las pruebas que fallan del canal en vivo**

`useLiveEvents` pasa a leer el cliente de React Query (`useQueryClient`), así que las siete llamadas `renderHook(() => useLiveEvents(…))` de `frontend/src/lib/ws.test.ts` (`:100, :132, :147, :160, :173, :190, :204`) ganan `{ wrapper }`. Es el único cambio en esas pruebas; lo que afirman no cambia. Ejemplo, `:100`:

```ts
    const { unmount } = renderHook(() => useLiveEvents(handler), { wrapper });
```

Imports y montaje, en la cabecera del archivo:

```ts
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setSelectedClient } from "./clientChoice";
import { RESYNC_KIND } from "./resync";
import { SWAP_GRACE_MS, liveUrl, useLiveEvents } from "./ws";
```

```ts
let queryClient: QueryClient;

/** `useLiveEvents` asks `/me` again on a 4403, through the query client. */
function wrapper({ children }: { children: ReactNode }) {
  return createElement(QueryClientProvider, { client: queryClient }, children);
}

/** The `client` parameters a socket was opened with. */
function clientOf(socket: FakeSocket): string[] {
  return new URL(socket.url).searchParams.getAll("client");
}
```

y en el `beforeEach` existente, tras `auth.renew.mockReset();`: `queryClient = new QueryClient();`.

Al final del archivo:

```ts
const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";
const ZETA = "5c9e1d2f-3a4b-4c5d-8e6f-7a8b9c0d1e02";

describe("useLiveEvents and the chosen client (phase 3 design §1.5)", () => {
  it("names the chosen client in ?client=, and none for every client", () => {
    expect(new URL(liveUrl(null)).pathname).toBe("/api/v1/ws");
    expect(new URL(liveUrl(null)).search).toBe("");
    expect(new URL(liveUrl(ACME)).searchParams.getAll("client")).toEqual([ACME]);

    const { unmount } = renderHook(() => useLiveEvents(vi.fn()), { wrapper });
    expect(clientOf(sockets()[0]!)).toEqual([]);
    unmount();

    setSelectedClient(ACME);
    renderHook(() => useLiveEvents(vi.fn()), { wrapper });
    expect(clientOf(sockets()[1]!)).toEqual([ACME]);
  });

  it("opens again for a new choice, and the old socket's close is planned", () => {
    const handler = vi.fn();
    renderHook(() => useLiveEvents(handler), { wrapper });
    const [first] = sockets();
    act(() => first!.open());

    act(() => setSelectedClient(ZETA));
    expect(sockets()).toHaveLength(2);
    expect(first!.closed).toBe(true);
    expect(clientOf(sockets()[1]!)).toEqual([ZETA]);

    // The old socket's own close schedules nothing and is no gap.
    act(() => first!.ended(1000));
    act(() => vi.advanceTimersByTime(60_000));
    expect(sockets()).toHaveLength(2);
    expect(handler).not.toHaveBeenCalledWith({ kind: RESYNC_KIND });
  });

  it("on a 4403 after the socket was open, asks /me again and reconnects at once", async () => {
    const invalidate = vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue(undefined);
    renderHook(() => useLiveEvents(vi.fn()), { wrapper });
    act(() => sockets()[0]!.open());

    await act(async () => {
      sockets()[0]!.ended(4403);
      await Promise.resolve();
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["me"] });
    // No backoff waited: the second socket is already there.
    expect(sockets()).toHaveLength(2);
  });

  it("on a 4403 at the handshake, asks /me again and backs off, so a refused scope cannot spin", async () => {
    const invalidate = vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue(undefined);
    renderHook(() => useLiveEvents(vi.fn()), { wrapper });

    await act(async () => {
      sockets()[0]!.ended(4403);
      await Promise.resolve();
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["me"] });
    expect(sockets()).toHaveLength(1);
    act(() => vi.advanceTimersByTime(2_000));
    expect(sockets()).toHaveLength(2);
  });
});
```

- [ ] **Step 12: Comprobar que fallan**

Run: `npx vitest run src/lib/ws.test.ts`
Expected: FAIL — `liveUrl` no se exporta; las cuatro pruebas nuevas fallan (sin `?client=`, sin reconexión al cambiar, el 4403 entra en la espera). Las siete existentes siguen en verde con el `wrapper`.

- [ ] **Step 13: `ws.ts`**

Archivo entero:

```ts
// Live status: WebSocket to /api/v1/ws, events published by gateway/API.
//
// THE SOCKET OUTLIVES ITS TOKEN, AND MUST NOT LOSE EVENTS WHEN IT DOES (final
// review W1). The server re-checks an open socket and closes it 4401 once the
// token it was opened with has expired; Keycloak's access tokens live five
// minutes. A socket that kept its first token therefore closed every five
// minutes, waited out a backoff, and every event published in that gap was
// gone -- and the pages that refresh only on live events (jobs, a device's
// page) kept showing a finished job as running. So:
//
// 1. ON A RENEWED TOKEN (`onTokenRenewed`) a NEW socket opens with it, and
//    the old one is closed only once the new one is open -- after a short
//    grace (`SWAP_GRACE_MS`) during which both deliver, because the server
//    subscribes to its channels just after it accepts the handshake. The old
//    socket is RETIRED: its close schedules no reconnect and is no gap.
//    Every consumer's handler is an idempotent invalidation, so an event
//    heard twice in that second costs a refetch, never a wrong screen.
// 2. AFTER ANY UNPLANNED CLOSE the next open delivers `{kind: "resync"}`
//    (`RESYNC_KIND`): events may have been missed, and the page re-reads
//    what it keeps fresh from them.
// 3. A 4401 RECONNECTS AT ONCE with a newer token -- the one the renewal
//    already fetched, or one renewed now -- instead of waiting out a backoff.
//    With no newer token to be had it backs off like any other close, so an
//    ended session cannot spin.
// 4. Any other unplanned close keeps the doubling backoff (2 s, 4 s ... 30 s).
// 5. THE CHOSEN CLIENT (phase 3 design §1.5) travels as `?client=<id>` -- a
//    UUID, not a credential, so the query string is the right place for it --
//    and the socket opens again when the choice changes. A 4403 says the
//    scope is empty or is no longer the one subscribed: access to that client
//    went, or a grant changed. The console asks `/me` again, which moves the
//    top bar back to every client when the client is gone
//    (`lib/clientScope.tsx`), and reconnects AT ONCE when the refused socket
//    had been open. One refused at the handshake backs off instead:
//    reconnecting at once to a scope the server has just refused would loop.

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useSyncExternalStore } from "react";
import { accessToken, onTokenRenewed, renewAccessToken } from "./auth";
import { selectedClient, subscribeClient } from "./clientChoice";
import { RESYNC_KIND } from "./resync";

export interface UiEvent {
  kind: string;
  job_id?: string;
  state?: string;
  [key: string]: unknown;
}

/** How long a replaced socket keeps delivering after its successor opened. */
export const SWAP_GRACE_MS = 1000;
/** The server's close code for a token that is not (or no longer) valid. */
export const CLOSE_TOKEN_EXPIRED = 4401;
/** The server's close code for a scope that is empty, or no longer the one subscribed. */
export const CLOSE_SCOPE_REFUSED = 4403;
const INITIAL_RETRY_MS = 1000;
const MAX_RETRY_MS = 30_000;

/** The socket's address: `?client=` names the chosen client, and is absent for every client. */
export function liveUrl(client: string | null): string {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const query = client === null ? "" : `?client=${encodeURIComponent(client)}`;
  return `${scheme}://${window.location.host}/api/v1/ws${query}`;
}

export function useLiveEvents(onEvent: (event: UiEvent) => void): void {
  const handler = useRef(onEvent);
  handler.current = onEvent;
  const queryClient = useQueryClient();
  const client = useSyncExternalStore(subscribeClient, selectedClient, selectedClient);

  useEffect(() => {
    let disposed = false;
    // The socket whose loss is a gap.
    let current: WebSocket | null = null;
    // A replacement still opening, after a token renewal.
    let pending: WebSocket | null = null;
    // Replaced or abandoned on purpose: their close is planned.
    const retired = new Set<WebSocket>();
    // Sockets the server accepted: a 4403 on one of these is a scope that
    // changed under it, not one refused at the handshake.
    const accepted = new WeakSet<WebSocket>();
    const graceTimers = new Set<ReturnType<typeof setTimeout>>();
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let retryMs = INITIAL_RETRY_MS;
    // Events may have been missed: the next open says so.
    let gap = false;

    const deliver = (event: UiEvent) => handler.current(event);

    const opened = () => {
      retryMs = INITIAL_RETRY_MS;
      if (gap) {
        gap = false;
        deliver({ kind: RESYNC_KIND });
      }
    };

    // The token goes in the subprotocol list, NEVER in the URL: a query
    // string is written verbatim to the server's access log, and no
    // logging-side redaction can reach it. The server requires exactly
    // ["bearer", token] and echoes back "bearer".
    const socketWith = (token: string): WebSocket => {
      const socket = new WebSocket(liveUrl(client), ["bearer", token]);
      socket.onmessage = (message) => {
        if (socket !== current && !retired.has(socket)) return;
        try {
          deliver(JSON.parse(message.data as string) as UiEvent);
        } catch {
          // ignore malformed frames
        }
      };
      return socket;
    };

    const backoff = () => {
      retryMs = Math.min(retryMs * 2, MAX_RETRY_MS);
      retryTimer = setTimeout(() => connect(), retryMs);
    };

    const reauthenticate = async (stale: string) => {
      let token = accessToken();
      if (!token || token === stale) token = await renewAccessToken();
      if (disposed) return;
      if (token && token !== stale) connect(token);
      else backoff();
    };

    // A 4403 (point 5): `/me` first. When the chosen client is gone the top
    // bar falls back to every client and this effect starts over with that
    // choice, so `disposed` is set by the time the refetch has answered.
    const rescope = async (wasOpen: boolean) => {
      await queryClient.invalidateQueries({ queryKey: ["me"] });
      if (disposed) return;
      if (wasOpen) connect();
      else backoff();
    };

    const lost = (socket: WebSocket, token: string, code: number) => {
      // A retired socket's close was planned: forget it, schedule nothing.
      if (retired.delete(socket)) return;
      if (disposed || socket !== current) return;
      current = null;
      gap = true;
      if (code === CLOSE_TOKEN_EXPIRED) void reauthenticate(token);
      else if (code === CLOSE_SCOPE_REFUSED) void rescope(accepted.has(socket));
      else backoff();
    };

    const connect = (given?: string) => {
      retryTimer = null;
      const token = given ?? accessToken();
      if (!token || disposed) return;
      const socket = socketWith(token);
      current = socket;
      socket.onopen = () => {
        accepted.add(socket);
        opened();
      };
      socket.onclose = (event) => lost(socket, token, event.code);
    };

    const renewed = (token: string) => {
      // No live socket: the reconnect already under way reads the new token.
      if (disposed || current === null) return;
      if (pending) {
        retired.add(pending);
        pending.close();
      }
      const next = socketWith(token);
      pending = next;
      next.onopen = () => {
        if (disposed || pending !== next) return;
        accepted.add(next);
        pending = null;
        const old = current;
        current = next;
        if (retryTimer) {
          clearTimeout(retryTimer);
          retryTimer = null;
        }
        if (old) {
          retired.add(old);
          const timer = setTimeout(() => {
            graceTimers.delete(timer);
            old.close();
          }, SWAP_GRACE_MS);
          graceTimers.add(timer);
        }
        opened();
      };
      next.onclose = (event) => {
        if (pending === next) {
          // It never opened: the old socket carries on, and the next
          // renewal (or its own 4401) tries again.
          pending = null;
          return;
        }
        lost(next, token, event.code);
      };
    };

    const unsubscribe = onTokenRenewed(renewed);
    connect();
    return () => {
      disposed = true;
      unsubscribe();
      if (retryTimer) clearTimeout(retryTimer);
      for (const timer of graceTimers) clearTimeout(timer);
      current?.close();
      pending?.close();
      for (const socket of retired) socket.close();
    };
    // A new choice is a new socket: the old one is closed by this cleanup,
    // with `disposed` set, so its close schedules nothing.
  }, [client, queryClient]);
}
```

- [ ] **Step 14: Comprobar que pasan, y commit**

Run: `npx vitest run src/lib/ws.test.ts src/modules/jobs src/modules/devices/DevicesPage.test.tsx src/modules/inventory/DeviceDetailPage.test.tsx src/modules/security`
Expected: PASS (las cuatro páginas que abren el socket tienen ya `QueryClientProvider` en sus pruebas).

```bash
git add frontend/src/lib/ws.ts frontend/src/lib/ws.test.ts
git commit -m "feat(console): the live socket follows the chosen client, and a 4403 re-reads /me (phase 3 Task 7)"
```

- [ ] **Step 15: Verificación**

Desde `frontend/`: `npx vitest run` (completo; incluye `i18n/coverage.test.ts`, `catalogue.identical.test.ts` y la prueba de marcadores), `npx tsc -b` y `npm run build`.
Expected: todo en verde. No se tocó el backend.

---

### Task 8: Consola — menú por permisos y módulos, Imaging fuera, pantallas globales y nombre de cliente

Diseño: §2 y §3.4 (el nombre de cliente). Inventario: §2, §3, §11.

Depende de la Task 7 (`MeClient`, `Me.clients`, `Me.permissions_everywhere`, `useClientScope`).

**Files:**
- Modify: `frontend/src/lib/mePermissions.ts` (`useEverywherePermissions`)
- Modify: `frontend/src/lib/clientScope.tsx` (`enabledModules`, `useEnabledModules`)
- Modify: `frontend/src/shell/nav.ts` — `page()` (`:56-66`), `NavItem` (`:76-91`), `NAV_GROUPS` (`:139-140`, `:146`, `:169`), `NAV_UNLISTED` (`:245-259`), `visibleGroups` (`:315-335`), `searchDestinations` (`:346-363`)
- Modify: `frontend/src/shell/Sidebar.tsx:26-43`, `frontend/src/shell/AppShell.tsx` (prop `modules`, `:406`), `frontend/src/shell/AreaPage.tsx:19-28`, `frontend/src/App.tsx` (`Shell`: `:14`, `:208`, `:297`, `:306`, `:321-322`)
- Modify (pantallas globales, lista abajo): `frontend/src/modules/settings/SettingsPage.tsx`, `OrganizationTab.tsx`, `EmailTab.tsx`, `RolesTab.tsx`, `UsersTab.tsx`, `AssistantTab.tsx`, `AiConnectionsTab.tsx`, `NotificationsTab.tsx`, `ServiceLevelsTab.tsx`, `HealthTab.tsx`, `settingsState.ts`, `api.ts`; `frontend/src/modules/sla/SlaAdminPage.tsx`; `notification/NotificationAdminPage.tsx`; `health/HealthPage.tsx`; `agent-releases/AgentReleasesPage.tsx`, `AgentUpdatesSection.tsx`, `AgentUpdatePrerequisites.tsx`, `api.ts`; `service-desk/ServiceDeskAdminPage.tsx`; `deploy/PackagesPage.tsx`, `ScriptsPage.tsx`; `licenses/VendorsPage.tsx`, `ProductsPage.tsx`, `ReviewQueue.tsx`; `budget/BudgetAdminPage.tsx`; `provisioning/ImagesPage.tsx`, `ProfilesPage.tsx`; `email-intake/EmailIntakeAdminPage.tsx`; `error-monitor/api.ts`; `ai-automation/AutonomyDialPage.tsx`; `people/PersonPage.tsx`; `frontend/src/components/forEveryClient.tsx`
- Modify: `frontend/src/modules/settings/api.ts:615-629` (`useClientName`)
- Modify: `frontend/src/i18n/{en,es}/lib.ts`, `{en,es}/settings.ts`, `{en,es}/aiAutomation.ts`
- Test (nuevos): `frontend/src/lib/mePermissions.test.tsx`, `frontend/src/test/globalWrites.test.tsx`, `frontend/src/modules/settings/__tests__/everyClient.test.tsx`, `frontend/src/modules/settings/__tests__/clientName.test.tsx`
- Test (añadidos o corregidos): `frontend/src/shell/nav.test.ts`, `frontend/src/shell/shell.es.test.tsx:23,63`, `frontend/src/lib/clientScope.test.tsx`, `frontend/src/shell/AppShell.test.tsx`, `frontend/src/App.shell.test.tsx`, `frontend/src/modules/error-monitor/ErrorMonitorPage.test.tsx`, `frontend/src/components/forEveryClient.test.tsx`, `frontend/src/modules/people/__tests__/people.test.tsx`, `frontend/src/modules/licenses/__tests__/licenses.test.tsx`, `frontend/src/modules/ipam/__tests__/clients.test.tsx`, `frontend/src/modules/patch/__tests__/patch.test.tsx`, `frontend/src/modules/devices/CandidatesPage.test.tsx`, `frontend/src/modules/subscriptions/__tests__/subscriptions.test.tsx`

**Interfaces:**
- Consumes (Task 7): `MeClient`, `Me.clients?`, `Me.permissions_everywhere?`, `useClientScope`.
- Produces (01-interfaces):
  - `NavItem.module?: "budget" | "bcp" | "change"`.
  - `visibleGroups(permissions: readonly string[], modules: ReadonlySet<string>, groups?: readonly NavGroup[]): NavGroup[]` (el tercer parámetro es el `groups` que ya existía).
  - `frontend/src/lib/mePermissions.ts`: `useEverywherePermissions(): MePermissions`.
  - `useClientName(): (clientId: string | null) => string` en `modules/settings/api.ts`, que lee `me.clients`. Un id desconocido, o `null`, es `t("lib.clients.everyClient")`.
- Produces (nombres nuevos, fuera de 01-interfaces):
  - `frontend/src/shell/nav.ts`: `type NavModule = "budget" | "bcp" | "change"`, `OPTIONAL_MODULES: ReadonlySet<string>`; `searchDestinations(permissions, modules)`.
  - `frontend/src/lib/clientScope.tsx`: `enabledModules(clients: readonly MeClient[] | undefined, selected: string | null): ReadonlySet<string>`, `useEnabledModules(): ReadonlySet<string>`.
  - `AppShellProps.modules?: ReadonlySet<string>`, `SidebarProps.modules?: ReadonlySet<string>`.
  - `frontend/src/modules/settings/settingsState.ts`: `everyClientReason(code)`; `frontend/src/modules/settings/api.ts`: `useGlobalWriteReason(code): string`.
  - Props opcionales: `EmailTab({ canManage, canEdit? })`, `AssistantTab({ canAdmin, canEdit? })`, `AiConnectionsTab({ canUse, canManageOrg, canSwitch? })`.

**Las pantallas de configuración global (diseño §2, derivado de §1.2).** Cada fila es una pantalla que llama a una ruta de `globalRoutes.json`; la ruta se encontró buscando su plantilla en `frontend/src/lib/api.ts` y el método en las pantallas. La columna «Escrituras» cita las líneas de hoy de los controles; la columna «Cambio» dice qué pasa a leer `permissions_everywhere`.

| Pantalla | Puerta hoy | Escrituras (controles) → ruta global | Cambio |
|---|---|---|---|
| Ajustes → Organización | `SettingsPage.tsx:263` `canManageOrg` → `:389` | `OrganizationTab.tsx:153` `editable` (campos `:258, :324, :358, :385`; guardar `:436`) → `PATCH /orgs/current` | `canManage={everywhere.has(ORGS_MANAGE)}` |
| Ajustes → Correo | `:263` → `:390` | `EmailTab.tsx:220` `editable` (`:283-:471`), guardar `:507-:516`, prueba `:531, :543` → `PUT /settings/email`, `POST /settings/email/test` | prop nueva `canEdit` (la lectura sigue con `canManage`) |
| Ajustes → Roles | `:267` → `:391-393` | `RolesTab.tsx:219, :263, :280, :300, :308, :318` → `/roles…` | `canManage={everywhere.has(ROLES_MANAGE)}` |
| Ajustes → Usuarios | `:269` → `:394-400` | `UsersTab.tsx:481-:528, :621-:718, :790` → `/users…` | `canManage={everywhere.has(USERS_MANAGE)}` |
| Ajustes → Asistente | `:270` → `:401` | `AssistantTab.tsx:116` (`:255-:304`), guardar `:321-:324` → `PUT /ai/settings` | prop nueva `canEdit` |
| Ajustes → Conexiones IA | `:263` → `:402-404` | `AiConnectionsTab.tsx:93` interruptor `:106-:111` → `PATCH /orgs/current` (revocar para todos `:193` NO es global) | prop nueva `canSwitch` |
| Ajustes → Notificaciones | `:273` → `:405-407` | `NotificationsTab.tsx:355-:772` → canales y reglas | `canAdmin={everywhere.has(NOTIFICATION_ADMIN)}` |
| Ajustes → Niveles de servicio | `:275` → `:408-410` | `ServiceLevelsTab.tsx:90, :103-:105` → `PUT /sla/settings` | `canAdmin={everywhere.has(SLA_ADMIN)}` |
| Ajustes → Salud | `:277` → `:411-413` | `HealthTab.tsx:117, :457-:461` → `PUT /health/settings` | `canAdmin={everywhere.has(HEALTH_ADMIN)}` |
| Ajustes → Presupuesto | — | `PUT /budget/settings` **no** es global | sin cambio |
| `/sla` | `SlaAdminPage.tsx:40` | `SlaSettingsPanel.tsx:121, :137`; `CalendarsPanel.tsx:222, :287`; `HolidaysPanel.tsx:84-:154`; `WorkingWindowsEditor.tsx:169-:247`; atajo «Add a business calendar» `PoliciesPanel.tsx:375` → `/sla/calendars…`, `PUT /sla/settings` | `canConfigure` para ajustes, calendarios y el atajo; las políticas (O) siguen con `canAdmin` |
| `/notifications` | `NotificationAdminPage.tsx:93` | `RulesPanel.tsx:151, :173, :203, :214`; `ChannelsPanel.tsx:358, :382, :416, :427`; `QueueMembersPanel.tsx:140, :155, :179`; `AllowlistPanel.tsx:54, :79, :93` | `canAdmin` desde `everywhere` |
| `/health` | `HealthPage.tsx:73` (`canAdmin`), `:76` (`canActionAdmin`) | `ProfileList.tsx:252` (clonar, borrar), `:301` (crear); `ProfileEditor.tsx:195, :217`; `HealthSettingsPanel.tsx:264, :285, :309`; `ApprovedActionList.tsx:237, :302, :321` → `/health/profiles…`, `PUT /health/settings`, `/approved-actions…` | ambos desde `everywhere` |
| `/agent-packages` | `AgentReleasesPage.tsx:191`; `AgentUpdatesSection.tsx:86`; `AgentUpdatePrerequisites.tsx:88` | publicar `AgentReleasesPage.tsx:250`, texto del catálogo vacío `:352`, canales `:506` (`ReleaseChannels`); rollouts `AgentUpdatesSection.tsx:171, :285`; CTA «marcar estable» `AgentUpdatePrerequisites.tsx:100` → `/agent/releases…`, `/agent/update-rollouts…` | los tres desde `everywhere`; también `usePublishPermission` (`agent-releases/api.ts:20-24`, hoy sin consumidores) |
| `/service-desk/admin` | `ServiceDeskAdminPage.tsx:57` | `:152, :169, :206` (colas); `:360, :379, :428` (categorías); `SavedRepliesPanel.tsx:45, :61, :73` → `/service-desk/queues…`, `/categories…`, `/canned-responses…` | paneles con `canConfigure`; la pestaña «Admin» de `ServiceDeskNav` (`:65`) sigue con `canAdmin` (es navegación) |
| `/deploy/packages` | `PackagesPage.tsx:57` | `:76` (nuevo), `:165` (fila) → `/deploy/packages…` | `canManage` desde `everywhere` |
| `/deploy/scripts` | `ScriptsPage.tsx:70` | `:82` (nuevo), `:140` (editar) → `/deploy/scripts…` | `canManage` desde `everywhere` |
| `/licenses/vendors` | `VendorsPage.tsx:25` | `:52` → `POST /vendors` | `canManage` desde `everywhere` |
| `/licenses/products` | `ProductsPage.tsx:45` | `:72, :136` → `/licenses/products…` | `canManage` desde `everywhere` |
| `/licenses/review` | `ReviewQueue.tsx:32` | «Map» `:106-:108` → `POST /licenses/review-queue/map`; «Ignore» `:109-:115` (`/review-queue/ignore`) **no** es global | `canMap` para «Map»; «Ignore» sigue con `canManage` |
| `/budget/admin` | `BudgetAdminPage.tsx:110` | `FxRatesPanel` `:667, :682, :715` → `/budget/fx-rates…` (categorías `:363` y centros de coste `:514` no son globales) | `canManageRates` solo para `FxRatesPanel` |
| `/provisioning/images` | `ImagesPage.tsx:57` | `:83` → `POST /provisioning/images` | `canManage` desde `everywhere` |
| `/provisioning/profiles` | `ProfilesPage.tsx:49` | `:76, :211` → `/provisioning/profiles…` | `canManage` desde `everywhere` |
| `/email-intake` | `EmailIntakeAdminPage.tsx:88` | `MailboxList.tsx:128, :166, :214, :250`; `MicrosoftConnection.tsx:97, :107, :122, :138` → `/email-intake/mailboxes…`, `/oauth-app`, `/oauth/…` | `canAdmin` desde `everywhere` |
| `/error-monitor` | `error-monitor/api.ts:69-73` `useErrorTriagePermission` | `ErrorMonitorPage.tsx:453, :609` → `/platform/errors/{id}/resolve`, `/unresolve` | el hook lee `everywhere` |
| `/assistant/autonomy` | ninguna | celdas `AutonomyDialPage.tsx:384-:391`, guardar `:410-:417` → `PUT /ai/autonomy` | `canSet`: celdas `disabled`, guardar solo con `canSet` |
| `/people/:id` | `PersonPage.tsx:190` | «Turn off sign-in» `:210` → `POST /users/{id}/disable` para el personal; un contacto usa `PATCH /contacts/{id}`, que no es global | personal desde `everywhere`; contacto sigue con `clients.manage` |
| «Para todos los clientes» | `components/forEveryClient.tsx:36` | la casilla crea una fila global | `useEverywherePermissions` (hasta hoy `/me` *era* ese conjunto) |

`/assistant` (`AiSettingsPage.tsx`) es de solo lectura y remite a Ajustes; el enlace de `ChatPanel.tsx:200` es navegación; `/email-intake/oauth/callback` completa sin control. Ninguno cambia.

**Decisiones del plan (Task 8):**
- *Decisión del plan: una pantalla global se sigue mostrando con el código en `permissions`; solo sus escrituras miran `permissions_everywhere`.* Es la letra de §2. Donde un mismo `canX` decidía lectura y escritura (`EmailTab`, `AssistantTab`), se separa con una prop opcional que por defecto vale lo de antes, para que las pruebas que renderizan la pestaña sola sigan compilando.
- *Decisión del plan: en las pestañas de Ajustes que son globales, el motivo de solo lectura dice «en todos los clientes» cuando la persona tiene el código en algún cliente* (`useGlobalWriteReason`). Sin el código en ningún cliente, la frase es la de siempre (`settings.readOnly`), así que `settings.es.test.tsx:264, :645, :783, :1128` no cambian.
- *Decisión del plan: `visibleGroups` exige `modules`; `Sidebar` y `AppShell` lo reciben opcional y, sin él, aplican todos los módulos (`OPTIONAL_MODULES`).* El `Shell` siempre lo pasa; las pruebas del shell y `/__preview` no saben de clientes. `useEnabledModules` devuelve también todos cuando `/me` no trae `clients` (API anterior a la fase 3, que no condicionaba módulos en el menú).
- *Decisión del plan: Imaging sale de `NAV_GROUPS` y entra en `NAV_UNLISTED` bajo `/admin`.* Sus rutas se quedan (§2); sin la entrada no listada, `/provisioning` se quedaría sin miga de pan («ITMS» a secas).
- *Decisión del plan: `useClientName` acepta `string | null` y traduce el cliente Sin asignar.* Un id desconocido o `null` es «Todos los clientes» (§3.4 y 01-interfaces). Ojo: `/me.clients` deja fuera los archivados, así que una fila de un cliente archivado también se nombra «Todos los clientes»; el diseño no lo distingue y esta tarea no lo inventa.
- *Decisión del plan: ocultar la columna Cliente con un cliente elegido (§3.4, segunda mitad) no está en esta tarea.* Ver la nota final para el coordinador.

- [ ] **Step 1: Escribir las pruebas que fallan del menú, los módulos y `useEverywherePermissions`**

```tsx
// frontend/src/lib/mePermissions.test.tsx
/**
 * The two permission lists /me returns (phase 3 design §1.3): `permissions`,
 * the codes held in at least one client of the request's scope, and
 * `permissions_everywhere`, the codes held on every client -- what a screen
 * of configuration every client shares offers its writes on (§2).
 *
 * THREE STATES, NOT TWO, for both: neither `has` nor `lacks` is true until /me
 * has answered. And an API older than phase 3 sends no
 * `permissions_everywhere`: its `permissions` WAS that set.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient, Me } from "./api";
import { useEverywherePermissions, useMePermissions } from "./mePermissions";

const mockUseApi = vi.fn();
vi.mock("./ApiContext", () => ({ useApi: () => mockUseApi() }));

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "tech@example.com",
  display_name: "Tech",
  permissions: ["sla.read", "sla.admin"],
  permissions_everywhere: ["sla.read"],
};

function hook<T>(use: () => T, me: Promise<Me>) {
  mockUseApi.mockReturnValue({ me: vi.fn().mockReturnValue(me) } as unknown as ApiClient);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  return renderHook(use, { wrapper });
}

describe("useEverywherePermissions", () => {
  it("reads the codes held on every client, not those held in some", async () => {
    const { result } = hook(() => useEverywherePermissions(), Promise.resolve(ME));
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.has("sla.read")).toBe(true);
    expect(result.current.has("sla.admin")).toBe(false);
    expect(result.current.lacks("sla.admin")).toBe(true);
  });

  it("is neither yes nor no before /me answers", () => {
    const { result } = hook(() => useEverywherePermissions(), new Promise<Me>(() => {}));
    expect(result.current.status).toBe("loading");
    expect(result.current.has("sla.read")).toBe(false);
    expect(result.current.lacks("sla.read")).toBe(false);
  });

  it("reads `permissions` from an API that sends no permissions_everywhere", async () => {
    const older: Me = { ...ME, permissions_everywhere: undefined };
    const { result } = hook(() => useEverywherePermissions(), Promise.resolve(older));
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.has("sla.admin")).toBe(true);
  });

  it("leaves useMePermissions on the codes held in some client", async () => {
    const { result } = hook(() => useMePermissions(), Promise.resolve(ME));
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.has("sla.admin")).toBe(true);
  });
});
```

`frontend/src/shell/nav.test.ts` — **actualización deliberada**. `visibleGroups` exige ahora `modules` y `searchDestinations` también, así que todas sus llamadas (`:36, :197, :221, :227, :236, :244, :252, :288, :393, :407, :426, :432, :433`) pasan `OPTIONAL_MODULES`. Es mecánico y no cambia ninguna afirmación existente:

```bash
cd frontend
sed -i -E 's/visibleGroups\(([^()]*)\)/visibleGroups(\1, OPTIONAL_MODULES)/g; s/searchDestinations\(([^()]*)\)/searchDestinations(\1, OPTIONAL_MODULES)/g' src/shell/nav.test.ts
```

y el import (`:12-26`) gana `OPTIONAL_MODULES,` (tras `NAV_UNLISTED,`). Al final del archivo, dos bloques nuevos:

```ts
/* OPTIONAL MODULES (phase 3 design §2): a page of one needs its permission AND
 * the module on -- in the chosen client, or in at least one of /me's clients
 * with "All my clients". The caller works out which modules; the model only
 * says which page belongs to which. */
describe("optional modules", () => {
  const CODES = ["budget.read", "bcp.read", "change.read"];
  const pages = (modules: readonly string[]) =>
    navItems(visibleGroups(CODES, new Set(modules))).map((item) => item.to);

  it("marks exactly Changes, Continuity and IT budget as optional modules", () => {
    const marked = ALL_ITEMS.filter((item) => item.module !== undefined).map((item) => [item.to, item.module]);
    expect(marked).toEqual([
      ["/changes", "change"],
      ["/bcp", "bcp"],
      ["/budget", "budget"],
    ]);
  });

  it("hides a module's page while no client of the choice uses the module, whatever the permission", () => {
    expect(pages([])).toEqual(["/dashboard"]);
    expect(pages(["budget"])).toEqual(["/dashboard", "/budget"]);
    expect(pages(["budget", "bcp", "change"])).toEqual(["/dashboard", "/changes", "/bcp", "/budget"]);
  });

  it("still needs the permission when the module is on", () => {
    expect(navItems(visibleGroups([], OPTIONAL_MODULES)).map((item) => item.to)).toEqual(["/dashboard"]);
  });

  it("keeps the search to what the rail shows", () => {
    expect(searchDestinations(["budget.read"], new Set()).map((d) => d.to)).not.toContain("/budget");
    expect(searchDestinations(["budget.read"], new Set(["budget"])).map((d) => d.to)).toContain("/budget");
  });
});

/* IMAGING DEPENDS ON PXE, WHICH IS NOT BUILT (project decision P3). It leaves
 * the rail and the search; its routes and its code stay, and a link to one of
 * its pages still finds its place under Admin. */
describe("Imaging", () => {
  it("is in neither the rail nor the search, whoever holds provisioning.read", () => {
    expect(ALL_ITEMS.map((item) => item.to)).not.toContain("/provisioning");
    const found = searchDestinations([...EVERY_PERMISSION, "provisioning.read"], OPTIONAL_MODULES);
    expect(found.map((d) => d.to)).not.toContain("/provisioning");
  });

  it("keeps a crumb for its pages, under Admin", () => {
    expect(crumbFor("/provisioning")).toEqual({ section: "Admin", sectionTo: "/admin", title: "Imaging" });
    expect(crumbFor("/provisioning/images").title).toBe("Imaging");
  });
});
```

`frontend/src/shell/shell.es.test.tsx`: el import de `:23` gana `OPTIONAL_MODULES`, y `:63` pasa a `searchItems={searchDestinations(EVERYTHING, OPTIONAL_MODULES).map((page) => ({`.

En `frontend/src/lib/clientScope.test.tsx` (Task 7), el import pasa a `import { ClientScopeProvider, enabledModules, useClientScope } from "./clientScope";` y se añade:

```tsx
describe("enabledModules (phase 3 design §2)", () => {
  const withModules = (id: string, modules: string[]): MeClient => ({
    ...client(id, id),
    enabled_modules: modules,
  });
  const CLIENTS = [withModules(ACME, ["budget"]), withModules(ZETA, ["change"]), withModules(INTERNAL, [])];

  it("is the chosen client's modules", () => {
    expect([...enabledModules(CLIENTS, ACME)]).toEqual(["budget"]);
    expect([...enabledModules(CLIENTS, INTERNAL)]).toEqual([]);
  });

  it("is every module some client uses, with every client chosen", () => {
    expect([...enabledModules(CLIENTS, null)].sort()).toEqual(["budget", "change"]);
  });

  it("is every optional module from an API that names no clients", () => {
    expect([...enabledModules(undefined, null)].sort()).toEqual(["bcp", "budget", "change"]);
  });
});
```

En `frontend/src/shell/AppShell.test.tsx`, dentro de `describe("AppShell", …)`:

```tsx
  it("hides a module's page when no client of the choice uses the module (phase 3 design §2)", () => {
    const shell = (modules: ReadonlySet<string>) => (
      <MemoryRouter>
        <AppShell
          permissions={["budget.read"]}
          modules={modules}
          orgName="Operations"
          title="Dashboard"
          user={{ name: "A B", role: "Operator" }}
          onSignOut={vi.fn()}
        >
          <p>x</p>
        </AppShell>
      </MemoryRouter>
    );
    const { unmount } = render(shell(new Set()));
    expect(sidebar().queryByRole("link", { name: /IT budget/ })).not.toBeInTheDocument();
    unmount();
    render(shell(new Set(["budget"])));
    expect(sidebar().getByRole("link", { name: /IT budget/ })).toHaveAttribute("href", "/budget");
  });
```

En `frontend/src/App.shell.test.tsx`, dentro del `describe` de la Task 7:

```tsx
  it("shows IT budget only when a client of the choice uses the budget module (phase 3 design §2)", async () => {
    const renderWith = (modules: string[]) => {
      const api = fakeApi(["budget.read"]);
      api.me = vi.fn().mockResolvedValue({
        user_id: "u1",
        display_name: "Ada",
        email: "ada@example.test",
        org_id: "org-1",
        roles: [],
        permissions: ["budget.read"],
        clients: [
          { id: "org-1", name: "Toeshee", kind: "internal", enabled_modules: modules, permissions: ["budget.read"] },
        ],
      });
      apiRef.current = api;
      return render(
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <MemoryRouter initialEntries={["/dashboard"]}>
            <Shell>
              <Where />
            </Shell>
          </MemoryRouter>
        </QueryClientProvider>,
      );
    };
    const rail = () => within(screen.getByRole("navigation", { name: "Main" }));

    const { unmount } = renderWith([]);
    await screen.findByText("Ada");
    expect(rail().queryByRole("link", { name: /IT budget/ })).not.toBeInTheDocument();
    unmount();

    renderWith(["budget"]);
    await screen.findByText("Ada");
    expect(await rail().findByRole("link", { name: /IT budget/ })).toHaveAttribute("href", "/budget");
  });
```

- [ ] **Step 2: Comprobar que fallan**

Run: `npx vitest run src/lib/mePermissions.test.tsx src/shell/nav.test.ts src/shell/shell.es.test.tsx src/lib/clientScope.test.tsx src/shell/AppShell.test.tsx src/App.shell.test.tsx`
Expected: FAIL — `useEverywherePermissions` y `enabledModules` no existen; `OPTIONAL_MODULES` no se exporta; Imaging sigue en el menú; `modules` no es prop de `AppShell`.

- [ ] **Step 3: `useEverywherePermissions`, el modelo del menú y su cableado**

`frontend/src/lib/mePermissions.ts`, del `export function useMePermissions` (`:34`) al final:

```ts
function fromMe(me: UseQueryResult<Me, ApiError>, codes: readonly string[]): MePermissions {
  const status: MePermissions["status"] = me.isSuccess ? "ready" : me.isError ? "error" : "loading";
  const shown = status === "ready" ? codes : [];
  return {
    status,
    codes: shown,
    has: (code) => status === "ready" && shown.includes(code),
    lacks: (code) => status === "ready" && !shown.includes(code),
  };
}

function useMeQuery() {
  const api = useApi();
  return useQuery<Me, ApiError>({
    queryKey: ["me"],
    queryFn: () => api.me(),
    staleTime: 60_000,
    retry: false,
  });
}

/** The codes held in at least one client of the request's scope (`/me`'s `permissions`). */
export function useMePermissions(): MePermissions {
  const me = useMeQuery();
  return fromMe(me, me.data?.permissions ?? []);
}

/**
 * The codes held on EVERY client (`/me`'s `permissions_everywhere`, phase 3
 * design §1.3): what a screen of configuration every client shares offers its
 * writes on (§2). The server refuses those writes to a narrower scope with
 * 403 (`backend/tests/test_global_writes.py`); this saves the person a
 * control that can only answer that.
 *
 * AN API OLDER THAN PHASE 3 SENDS NO `permissions_everywhere`, and then
 * `permissions` IS this set: before phase 3, `/me` returned only the codes
 * held on every client.
 */
export function useEverywherePermissions(): MePermissions {
  const me = useMeQuery();
  return fromMe(me, me.data?.permissions_everywhere ?? me.data?.permissions ?? []);
}
```

y el import de `:19` pasa a `import { useQuery, type UseQueryResult } from "@tanstack/react-query";`. La cabecera del archivo (`:1-16`) gana, tras «THREE STATES, NOT TWO…», el párrafo: «TWO LISTS SINCE PHASE 3. `useMePermissions` is the codes held in at least one client of the request's scope -- what a page draws its controls for; `useEverywherePermissions` the codes held on every client -- what a write to configuration every client shares is offered for.»

`frontend/src/shell/nav.ts`:

1. Tras `section()` (`:72-74`):

```ts
/** An optional module a client may have on (`clients.enabled_modules`, MSP clients spec §1.4). */
export type NavModule = "budget" | "bcp" | "change";

/**
 * Every optional module: what a caller that knows of no client passes -- the
 * shell's tests, `/__preview`. The console's own shell passes the modules of
 * the current choice (`useEnabledModules`, `lib/clientScope.tsx`).
 */
export const OPTIONAL_MODULES: ReadonlySet<string> = new Set<NavModule>(["budget", "bcp", "change"]);
```

2. `page()` (`:56-66`): el tipo de `extra` pasa a `extra: { badgeTone?: "gold" | "red"; hint?: MessageKey; module?: NavModule } = {},` (el `...rest` ya lo lleva al objeto).

3. En `NavItem`, tras `readonly hint?: string;` (`:90`):

```ts
  /**
   * The optional module this page belongs to (phase 3 design §2). It shows
   * only when the module is on -- in the chosen client, or in at least one of
   * the person's clients with "All my clients" -- as well as the permission.
   * The server answers 404 to a write for a client without the module.
   */
  readonly module?: NavModule;
```

4. `NAV_GROUPS`: `:139` pasa a `page("/changes", "shell.nav.item.changes", "changes", "change.read", { hint: "shell.nav.hint.changes", module: "change" }),`; `:140` a `page("/bcp", "shell.nav.item.continuityTests", "bcp", "bcp.read", { hint: "shell.nav.hint.bcp", module: "bcp" }),`; `:146` a `page("/budget", "shell.nav.item.itBudget", "budget", "budget.read", { hint: "shell.nav.hint.budget", module: "budget" }),`; y se borra la línea `:169` (`page("/provisioning", "shell.nav.item.imaging", …)`).

5. `NAV_UNLISTED`, tras `unlisted("/agent-push", …)` (`:253`):

```ts
  // IMAGING LEFT THE RAIL AND THE SEARCH (phase 3 design §2, project decision
  // P3: it depends on PXE, which is not built). Its pages stay reachable by a
  // link -- a device's Imaging tab -- and keep their place under Admin.
  unlisted("/provisioning", "shell.nav.item.imaging", "/admin"),
```

6. `visibleGroups` (`:315-335`):

```ts
/**
 * The groups an operator may actually see. An area keeps only the pages the
 * operator may open; an empty section, area or group is dropped entirely,
 * because a heading over nothing reads as a broken render. A page of an
 * optional module also needs the module in `modules` (phase 3 design §2).
 */
export function visibleGroups(
  permissions: readonly string[],
  modules: ReadonlySet<string>,
  groups: readonly NavGroup[] = NAV_GROUPS,
): NavGroup[] {
  // A requester is offered the portal's rail, never the operator groups.
  const source = groups === NAV_GROUPS && isPortalOnly(permissions) ? PORTAL_GROUPS : groups;
  const held = new Set(permissions);
  const allowed = (item: NavItem) =>
    (item.permission === null || held.has(item.permission)) &&
    (item.module === undefined || modules.has(item.module));
```

(el resto del cuerpo no cambia).

7. `searchDestinations` (`:346`): firma `export function searchDestinations(permissions: readonly string[], modules: ReadonlySet<string>): SearchDestination[] {` y su primera línea `return visibleGroups(permissions, modules).flatMap((group) =>`.

`frontend/src/lib/clientScope.tsx`: import `import { OPTIONAL_MODULES } from "../shell/nav";` y, tras `useClientScope`:

```ts
/**
 * The optional modules the menu may show for the current choice (phase 3
 * design §2): the chosen client's, or, with every client, those on in at
 * least one of the person's clients. An API older than phase 3 names no
 * clients, and the menu then gated no module: every optional module, as
 * before.
 */
export function enabledModules(
  clients: readonly MeClient[] | undefined,
  selected: string | null,
): ReadonlySet<string> {
  if (clients === undefined) return OPTIONAL_MODULES;
  const chosen = selected === null ? clients : clients.filter((client) => client.id === selected);
  return new Set(chosen.flatMap((client) => client.enabled_modules));
}

export function useEnabledModules(): ReadonlySet<string> {
  const me = useMeQuery();
  const selected = useSelected();
  return useMemo(() => enabledModules(me.data?.clients, selected), [me.data, selected]);
}
```

`frontend/src/shell/Sidebar.tsx`: el import de `:24` pasa a `import { activeNavTo, isArea, OPTIONAL_MODULES, visibleGroups } from "./nav";`; `SidebarProps` gana, tras `permissions`:

```ts
  /** Optional modules on for the current choice (phase 3 design §2). Absent: every one. */
  modules?: ReadonlySet<string>;
```

la desestructuración gana `modules,` y `:43` pasa a `const groups = visibleGroups(permissions, modules ?? OPTIONAL_MODULES);`.

`frontend/src/shell/AppShell.tsx`: en `AppShellProps`, tras `permissions: readonly string[];` (`:38`):

```ts
  /**
   * Optional modules on for the current choice (`useEnabledModules`, phase 3
   * design §2). Absent -- the shell's own tests, `/__preview` -- every one.
   */
  modules?: ReadonlySet<string>;
```

la desestructuración gana `modules,` tras `permissions,`, y en `<Sidebar` (`:406`), tras `permissions={permissions}`: `modules={modules}`.

`frontend/src/shell/AreaPage.tsx`: import `import { useEnabledModules } from "../lib/clientScope";` y `:26` pasa a

```tsx
  const modules = useEnabledModules();
  const visible = visibleGroups(me.data?.permissions ?? [], modules)
```

`frontend/src/App.tsx` (`Shell`): import `import { ClientScopeProvider, useEnabledModules } from "./lib/clientScope";` (sustituye al de la Task 7); tras `const permissions = me.data?.permissions ?? [];` (`:208`): `const modules = useEnabledModules();`; `:297` pasa a `...searchDestinations(permissions, modules).map((page) => ({`; `:306` a `[records.hits, permissions, modules, navigate, language, t],`; y en `<AppShell` (`:321-322`), tras `permissions={permissions}`: `modules={modules}`.

- [ ] **Step 4: Comprobar que pasan**

Run: `npx vitest run src/lib/mePermissions.test.tsx src/shell src/lib/clientScope.test.tsx src/App.shell.test.tsx src/App.crumb.test.tsx src/fixtures/client.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/mePermissions.ts frontend/src/lib/mePermissions.test.tsx frontend/src/lib/clientScope.tsx frontend/src/lib/clientScope.test.tsx frontend/src/shell frontend/src/App.tsx frontend/src/App.shell.test.tsx
git commit -m "feat(console): the menu follows the choice's permissions and modules; Imaging leaves the menu (phase 3 Task 8)"
```

- [ ] **Step 6: Escribir las pruebas que fallan de las pantallas globales**

```tsx
// frontend/src/test/globalWrites.test.tsx
/**
 * CONFIGURATION EVERY CLIENT SHARES IS OFFERED FOR WRITING ONLY TO SOMEONE
 * HOLDING THE CODE ON EVERY CLIENT (phase 3 design §2).
 *
 * The routes behind these controls are in `GLOBAL_WRITES`
 * (`backend/tests/test_global_writes.py`): the server refuses them, 403, to a
 * scope that is not every client. So each screen offers the write only with
 * the code in /me's `permissions_everywhere`, and keeps showing the screen --
 * and any write on it that is ONE client's -- with the code in `permissions`.
 *
 * INVARIANT 6: THIS GATES NOTHING; the server does. What is pinned is that the
 * console does not draw a control that can only answer 403.
 *
 * Every read a screen makes beyond /me (and the few named in `answers`) stays
 * in flight, so the only thing that differs between a row's two renders is
 * /me.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient, Me } from "../lib/api";
import { AgentReleasesPage } from "../modules/agent-releases/AgentReleasesPage";
import { AutonomyDialPage } from "../modules/ai-automation/AutonomyDialPage";
import { BudgetAdminPage } from "../modules/budget/BudgetAdminPage";
import { PackagesPage } from "../modules/deploy/PackagesPage";
import { ScriptsPage } from "../modules/deploy/ScriptsPage";
import { EmailIntakeAdminPage } from "../modules/email-intake/EmailIntakeAdminPage";
import { HealthPage } from "../modules/health/HealthPage";
import { ProductsPage } from "../modules/licenses/ProductsPage";
import { ReviewQueue } from "../modules/licenses/ReviewQueue";
import { VendorsPage } from "../modules/licenses/VendorsPage";
import { NotificationAdminPage } from "../modules/notification/NotificationAdminPage";
import { ImagesPage } from "../modules/provisioning/ImagesPage";
import { ProfilesPage } from "../modules/provisioning/ProfilesPage";
import { ServiceDeskAdminPage } from "../modules/service-desk/ServiceDeskAdminPage";
import { SlaAdminPage } from "../modules/sla/SlaAdminPage";

const mockUseApi = vi.fn();
vi.mock("../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

interface Screen {
  route: string;
  page: () => ReactElement;
  /** What the person holds on SOME client: the read, and the write. */
  held: string[];
  /** The code the global write needs on every client. */
  code: string;
  /** The control that writes configuration every client shares. */
  write: string;
  /** A control on the same screen that writes ONE client's data: it stays. */
  stays?: string;
  /** Reads that answer (by `ApiClient` method), for controls drawn per row. */
  answers?: Record<string, unknown>;
}

const SCREENS: Screen[] = [
  { route: "/notifications", page: () => <NotificationAdminPage />, held: ["notification.read", "notification.admin"], code: "notification.admin", write: "Add channel" },
  { route: "/sla", page: () => <SlaAdminPage />, held: ["sla.read", "sla.admin"], code: "sla.admin", write: "Add calendar", stays: "Add policy" },
  { route: "/service-desk/admin", page: () => <ServiceDeskAdminPage />, held: ["ticket.read", "ticket.admin"], code: "ticket.admin", write: "New queue" },
  { route: "/email-intake", page: () => <EmailIntakeAdminPage />, held: ["email_intake.read", "email_intake.admin"], code: "email_intake.admin", write: "Add mailbox" },
  { route: "/deploy/packages", page: () => <PackagesPage />, held: ["deploy.read", "deploy.package.manage"], code: "deploy.package.manage", write: "New package" },
  { route: "/deploy/scripts", page: () => <ScriptsPage />, held: ["deploy.read", "deploy.script.manage"], code: "deploy.script.manage", write: "New script" },
  { route: "/licenses/vendors", page: () => <VendorsPage />, held: ["licenses.read", "licenses.manage"], code: "licenses.manage", write: "New vendor" },
  { route: "/licenses/products", page: () => <ProductsPage />, held: ["licenses.read", "licenses.manage"], code: "licenses.manage", write: "New product" },
  {
    route: "/licenses/review",
    page: () => <ReviewQueue />,
    held: ["licenses.read", "licenses.manage"],
    code: "licenses.manage",
    write: "Map",
    stays: "Ignore",
    answers: { reviewQueue: [{ name: "7-Zip", publisher: "Igor Pavlov", device_count: 4 }] },
  },
  { route: "/budget/admin", page: () => <BudgetAdminPage />, held: ["budget.read", "budget.manage"], code: "budget.manage", write: "New rate", stays: "New category" },
  { route: "/provisioning/images", page: () => <ImagesPage />, held: ["provisioning.read", "provisioning.manage"], code: "provisioning.manage", write: "New image" },
  { route: "/provisioning/profiles", page: () => <ProfilesPage />, held: ["provisioning.read", "provisioning.manage"], code: "provisioning.manage", write: "New profile" },
  { route: "/agent-packages", page: () => <AgentReleasesPage />, held: ["agent.release.read", "agent.release.manage"], code: "agent.release.manage", write: "Publish a release…" },
  { route: "/agent-packages, self-update", page: () => <AgentReleasesPage />, held: ["agent.release.read", "agent.update.read", "agent.update.rollout"], code: "agent.update.rollout", write: "New rollout…" },
  { route: "/assistant/autonomy", page: () => <AutonomyDialPage />, held: ["ai.admin"], code: "ai.admin", write: "Save changes" },
  {
    route: "/health",
    page: () => <HealthPage />,
    held: ["health.read", "health.admin"],
    code: "health.admin",
    write: "Create",
    answers: { listHealthProfiles: [], listHealthAssignments: [], listApprovedActions: [] },
  },
];

function meWith(held: string[], everywhere: string[]): Me {
  return {
    user_id: "u1",
    org_id: "o1",
    email: "op@example.com",
    display_name: "Operator",
    timezone: null,
    permissions: held,
    permissions_everywhere: everywhere,
  };
}

/** `/me` and the named reads answer; every other read stays in flight. */
function stubApi(me: Me, answers: Record<string, unknown> = {}): ReturnType<typeof vi.fn> {
  const meFn = vi.fn().mockResolvedValue(me);
  const known: Record<string, unknown> = { me: meFn };
  for (const [name, value] of Object.entries(answers)) known[name] = vi.fn().mockResolvedValue(value);
  const api = new Proxy(known, {
    get: (target, key) =>
      typeof key !== "string" || key === "then"
        ? undefined
        : (target[key] ?? vi.fn().mockReturnValue(new Promise(() => {}))),
  });
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return meFn;
}

function renderPage(page: ReactElement): void {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{page}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe.each(SCREENS)("$route", ({ page, held, code, write, stays, answers }) => {
  it("offers the write to a holder of the code on every client", async () => {
    stubApi(meWith(held, held), answers);
    renderPage(page());
    expect(await screen.findByRole("button", { name: write })).toBeInTheDocument();
  });

  it("does not offer it to a holder of the code on some clients only", async () => {
    const me = stubApi(meWith(held, held.filter((one) => one !== code)), answers);
    renderPage(page());
    await waitFor(() => expect(me).toHaveBeenCalled());
    if (stays) {
      // The screen is still there, and its one-client writes with it.
      expect(await screen.findByRole("button", { name: stays })).toBeInTheDocument();
    } else {
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    expect(screen.queryByRole("button", { name: write })).toBeNull();
  });
});
```

```tsx
// frontend/src/modules/settings/__tests__/everyClient.test.tsx
/**
 * Settings that every client shares (phase 3 design §2): organization, email,
 * roles, users, the assistant, notifications, service levels and health
 * settings are written through routes in `GLOBAL_WRITES`. A tab is offered
 * with its code; its writes, with the code on every client -- and a person
 * holding the code on some clients only is told that, not that they lack it.
 */

import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ALL_PERMISSIONS, ME_FIXTURE, apiClient, calls, installDefaults, renderSettings } from "./harness";

vi.mock("../../../lib/auth", () => ({
  accessToken: () => "test-token",
  oidcAuthority: "https://id.example.test/realms/it-suite",
}));

// Referenced lazily, inside the closure -- see `organization.test.tsx`.
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

/** Every code the harness knows, held on some clients; on every client, only `everywhere`. */
function holdOnSomeClients(everywhere: readonly string[] = []): void {
  installDefaults();
  calls.me.mockResolvedValue({
    ...ME_FIXTURE,
    permissions: [...ALL_PERMISSIONS],
    permissions_everywhere: [...everywhere],
  });
}

describe("Settings: configuration every client shares (phase 3 design §2)", () => {
  it("shows the organization read-only to orgs.manage on some clients, saying it needs every client", async () => {
    holdOnSomeClients();
    renderSettings();
    await screen.findByLabelText("Default timezone");
    expect(screen.getByLabelText("Organization name")).toBeDisabled();
    expect(
      await screen.findByText(/requires the orgs\.manage permission on every client/),
    ).toBeInTheDocument();
  });

  it("offers the organization's form to orgs.manage on every client", async () => {
    holdOnSomeClients(ALL_PERMISSIONS);
    renderSettings();
    await screen.findByLabelText("Default timezone");
    expect(screen.getByLabelText("Organization name")).toBeEnabled();
  });

  it("lists the users but disables every write for users.manage on some clients", async () => {
    holdOnSomeClients();
    renderSettings("/settings?tab=users");
    expect(await screen.findByRole("row", { name: /casey@example\.com/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create user" })).toBeDisabled();
  });

  it("still offers and reads the Email tab for orgs.manage on some clients, read-only", async () => {
    holdOnSomeClients();
    renderSettings("/settings?tab=email");
    expect(await screen.findByRole("button", { name: "Send a test message" })).toBeDisabled();
    expect(calls.getEmailSettings).toHaveBeenCalled();
  });
});
```

`frontend/src/modules/error-monitor/ErrorMonitorPage.test.tsx`: el import de `:2` pasa a `import { render, screen, waitFor, within } from "@testing-library/react";`, y dentro del `describe` de `:399` (`ErrorMonitorPage reopen`), tras «draws neither triage control for an operator without platform.errors.write» (`:502-516`):

```tsx
  it("draws neither triage control for platform.errors.write on some clients only (phase 3 design §2)", async () => {
    (api.me as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...ME,
      permissions_everywhere: ["platform.errors.read"],
    });
    renderWithProviders(<ErrorMonitorPage />);

    const openRow = (await screen.findByText("ValueError")).closest("tr") as HTMLElement;
    const resolvedRow = (await screen.findByText("TimeoutError")).closest("tr") as HTMLElement;
    await waitFor(() => expect(api.me).toHaveBeenCalled());
    expect(within(openRow).queryByRole("button", { name: /resolve/i })).not.toBeInTheDocument();
    expect(within(resolvedRow).queryByRole("button", { name: /reopen/i })).not.toBeInTheDocument();
  });
```

`frontend/src/components/forEveryClient.test.tsx`, tras «is absent for anyone else»:

```tsx
  it("is absent for a holder of the code on some clients only (phase 3 design §2)", async () => {
    const me = vi.fn().mockResolvedValue({ ...ME, permissions_everywhere: ["docs.read"] });
    mockUseApi.mockReturnValue({ me } as unknown as ApiClient);
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <ForEveryClientToggle code="docs.manage" checked={false} onChange={() => undefined} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(me).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByRole("checkbox")).toBeNull();
  });
```

`frontend/src/modules/people/__tests__/people.test.tsx`, dentro de `describe("PersonPage", …)`, tras «turns a staff sign-in off through Settings' user endpoint» (`:225-245`):

```tsx
  it("offers a staff sign-in's switch only to users.manage on every client (phase 3 design §2)", async () => {
    const client = api(
      {
        getPerson: vi.fn().mockResolvedValue(detail()),
        me: vi.fn().mockResolvedValue({
          ...me("people.read", "users.manage"),
          permissions_everywhere: ["people.read"],
        }),
      },
      ["people.read", "users.manage"],
    );
    mockUseApi.mockReturnValue(client);
    renderAt(<PersonPage />, "/people/:personId", "/people/p1");

    expect(await screen.findByRole("heading", { name: "Maria Garcia" })).toBeInTheDocument();
    await vi.waitFor(() => expect(client.me).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Turn off sign-in" })).toBeNull();
  });
```

- [ ] **Step 7: Comprobar que fallan**

Run: `npx vitest run src/test/globalWrites.test.tsx src/modules/settings/__tests__/everyClient.test.tsx src/modules/error-monitor/ErrorMonitorPage.test.tsx src/components/forEveryClient.test.tsx src/modules/people/__tests__/people.test.tsx`
Expected: FAIL — en cada fila de `globalWrites.test.tsx`, la prueba «does not offer it…» encuentra el botón (hoy todas leen `permissions`); las de «offers…» pasan. En `everyClient.test.tsx` el formulario está habilitado y no hay frase «on every client».

- [ ] **Step 8: Condicionar las escrituras globales**

En cada archivo, `import { useEverywherePermissions } from "<ruta>/lib/mePermissions";` (desde `modules/<m>/`, `"../../lib/mePermissions"`; desde `components/`, `"../lib/mePermissions"`).

`frontend/src/modules/settings/settingsState.ts`, tras `permissionReason` (`:43-45`):

```ts
/**
 * The read-only sentence for a write to configuration EVERY client shares
 * (phase 3 design §2), for a person who holds the code on some clients but
 * not on all: "you lack the permission" would be false for them.
 */
export function everyClientReason(code: string): string {
  return translate("settings.readOnlyEveryClient", { code });
}
```

`frontend/src/modules/settings/api.ts`: import `import { everyClientReason, permissionReason } from "./settingsState";` y, tras `useMe` (`:143-151`):

```ts
/**
 * Why a write to configuration every client shares is not offered (phase 3
 * design §2): the code on every client, for someone who holds it on some;
 * the code itself, for someone who holds it nowhere.
 */
export function useGlobalWriteReason(code: string): string {
  const me = useMe();
  return (me.data?.permissions ?? []).includes(code) ? everyClientReason(code) : permissionReason(code);
}
```

Las nueve pestañas globales de Ajustes. En cada una: `permissionReason` sale del import de `./settingsState`, `useGlobalWriteReason` entra en su import de `./api`, y sus usos de `permissionReason(CODE)` pasan a `reason`:

| Pestaña | Import `./settingsState` queda | Hook (tras `const t = useT();`, o en su sitio) | Usos sustituidos |
|---|---|---|---|
| `OrganizationTab.tsx` | `isDirty, isForbidden, isMissingRoute, refusal` (`:55`) | `const reason = useGlobalWriteReason(ORGS_MANAGE);` tras `:107` | `:162` |
| `EmailTab.tsx` | `isDirty, isForbidden, parseIntegerField` (`:65`) | `const reason = useGlobalWriteReason(EMAIL_MANAGE);` tras `:173` | `:515` |
| `RolesTab.tsx` | `isForbidden` (`:53`) | `:93` pasa a `const reason = useGlobalWriteReason(ROLES_MANAGE);` | — |
| `UsersTab.tsx` | `isForbidden` (`:98`) | `:219` pasa a `const reason = useGlobalWriteReason(USERS_MANAGE);` | — |
| `AssistantTab.tsx` | `isDirty, isForbidden, parseIntegerField` (`:55`) | `const reason = useGlobalWriteReason(AI_ADMIN);` tras `:99` | `:324` |
| `AiConnectionsTab.tsx` | `isForbidden` (`:57`) | `const reason = useGlobalWriteReason(ORGS_MANAGE);` tras `:78` | `:114` |
| `NotificationsTab.tsx` | `isForbidden` (`:59`) | `const reason = useGlobalWriteReason(NOTIFICATION_ADMIN);` tras `:124` | `:297, :576, :772` |
| `ServiceLevelsTab.tsx` | `isForbidden, parseIntegerField` (`:27`) | `const reason = useGlobalWriteReason(SLA_ADMIN);` tras `:33` | `:105` |
| `HealthTab.tsx` | `isDirty, isForbidden, parseIntegerField` (`:38`) | `const reason = useGlobalWriteReason(HEALTH_ADMIN);` tras `:93` | `:461` |

Además:
- `EmailTab.tsx:172` pasa a `export function EmailTab({ canManage, canEdit = canManage }: { canManage: boolean; canEdit?: boolean }) {` con el comentario `/** `canManage` reads the tab (its read is `orgs.manage` too); `canEdit` writes it: `orgs.manage` on every client (phase 3 design §2). */` encima; `:220` pasa a `const editable = canEdit && settings.data != null;`; en `:511` y en `:531, :543`, `canManage` pasa a `canEdit` (`!canManage` → `!canEdit`).
- `AssistantTab.tsx:98` pasa a `export function AssistantTab({ canAdmin, canEdit = canAdmin }: { canAdmin: boolean; canEdit?: boolean }) {` con el mismo tipo de comentario (`ai.admin`), y `:116` a `const editable = canEdit && settings.data != null;`.
- `AiConnectionsTab.tsx:77` pasa a `export function AiConnectionsTab({ canUse, canManageOrg, canSwitch = canManageOrg }: { canUse: boolean; canManageOrg: boolean; canSwitch?: boolean }) {` con el comentario `/** `canSwitch`: the MCP switch is an org setting (`PATCH /orgs/current`), so `orgs.manage` on every client; revoking for everyone is not a global route and keeps `canManageOrg`. */`; `:93` pasa a `const switchEditable = canSwitch && org.data != null && !update.isPending;` y `:112` a `{!canSwitch ? (`.

`frontend/src/modules/settings/SettingsPage.tsx`: import `import { useEverywherePermissions } from "../../lib/mePermissions";`; se borran `:267` (`canManageRoles`), `:269` (`canManageUsers`), `:273` (`canAdminNotifications`), `:275` (`canAdminSla`) y `:277` (`canAdminHealth`), que dejan de usarse; tras `const canManageBudget = held.has(BUDGET_MANAGE);`:

```tsx
  // CONFIGURATION EVERY CLIENT SHARES (phase 3 design §2). Organization,
  // email, roles, users, the assistant, notifications, service levels and
  // health settings are written through routes that need the code on EVERY
  // client (`GLOBAL_WRITES`): a tab is offered with the code (`held`), its
  // writes with the code everywhere. Budget settings are one client's, and
  // keep `held`.
  const everywhere = useEverywherePermissions();
```

y `:389-413` queda:

```tsx
          {current === "organization" ? <OrganizationTab canManage={everywhere.has(ORGS_MANAGE)} /> : null}
          {current === "email" ? (
            <EmailTab canManage={canManageOrg} canEdit={everywhere.has(ORGS_MANAGE)} />
          ) : null}
          {current === "roles" ? (
            <RolesTab canRead={canReadRoles} canManage={everywhere.has(ROLES_MANAGE)} />
          ) : null}
          {current === "users" ? (
            <UsersTab
              canRead={canReadUsers}
              canManage={everywhere.has(USERS_MANAGE)}
              canReadEmail={canManageOrg}
            />
          ) : null}
          {current === "assistant" ? (
            <AssistantTab canAdmin={canAiAdmin} canEdit={everywhere.has(AI_ADMIN)} />
          ) : null}
          {current === "ai-connections" ? (
            <AiConnectionsTab
              canUse={canUseMcp}
              canManageOrg={canManageOrg}
              canSwitch={everywhere.has(ORGS_MANAGE)}
            />
          ) : null}
          {current === "notifications" ? (
            <NotificationsTab canRead={canReadNotifications} canAdmin={everywhere.has(NOTIFICATION_ADMIN)} />
          ) : null}
          {current === "sla" ? (
            <ServiceLevelsTab canRead={canReadSla} canAdmin={everywhere.has(SLA_ADMIN)} />
          ) : null}
          {current === "health" ? (
            <HealthTab canRead={canReadHealth} canAdmin={everywhere.has(HEALTH_ADMIN)} />
          ) : null}
```

El resto de pantallas (la sustitución exacta; en todas, un comentario de una línea encima: `// Configuration every client shares (GLOBAL_WRITES): the code on EVERY client (phase 3 design §2).`):

- `sla/SlaAdminPage.tsx`: tras `:40` `const canConfigure = useEverywherePermissions().has("sla.admin");`; `:184` `<SlaSettingsPanel canAdmin={canConfigure} />`; en `<PoliciesPanel` (`:188-192`) `onCreateCalendar={canConfigure ? () => setCalendarCreateNonce((n) => n + 1) : undefined}`; `:193` `<CalendarsPanel canAdmin={canConfigure} openCreateNonce={calendarCreateNonce} />`. `PoliciesPanel` sigue con `canAdmin`: una política es O, de un cliente o global, y eso lo decide su propio formulario (Task 11).
- `notification/NotificationAdminPage.tsx:92-93`: se borra `const permissions = me.data?.permissions ?? [];` (queda sin uso; `me` sigue usándose en `:255`) y `const canAdmin = useEverywherePermissions().has("notification.admin");`.
- `email-intake/EmailIntakeAdminPage.tsx:87-88`: igual; `const canAdmin = useEverywherePermissions().has("email_intake.admin");` (`me` sigue en `:257, :259`).
- `health/HealthPage.tsx`: tras `:71` `const everywhere = useEverywherePermissions();`; `:73` `const canAdmin = everywhere.has("health.admin");`; `:76` `const canActionAdmin = everywhere.has("health.action.admin");`.
- `agent-releases/AgentReleasesPage.tsx:191`: `const canPublish = useEverywherePermissions().has("agent.release.manage");`.
- `agent-releases/AgentUpdatesSection.tsx:85-86`: `const canRollout = useEverywherePermissions().has("agent.update.rollout");` (`perms` desaparece; `me` sigue en `:214`).
- `agent-releases/AgentUpdatePrerequisites.tsx:88`: `const canMarkStable = useEverywherePermissions().has(AGENT_RELEASE_MANAGE);`.
- `agent-releases/api.ts:20-24`: `export function usePublishPermission(): boolean { return useEverywherePermissions().has(RELEASE_MANAGE); }`; el import de `:9` pasa a `import { useMutation, useQueryClient } from "@tanstack/react-query";` y el de `:11` pierde `Me` (ambos quedan sin uso).
- `service-desk/ServiceDeskAdminPage.tsx`: tras `:57` `const canConfigure = useEverywherePermissions().has("ticket.admin");`; `:68-70` pasan `canAdmin={canConfigure}` a `QueuesPanel`, `CategoriesPanel` y `SavedRepliesPanel`. `ServiceDeskNav` (`:65`) sigue con `canAdmin`.
- `deploy/PackagesPage.tsx:57`: `const canManage = useEverywherePermissions().has("deploy.package.manage");`.
- `deploy/ScriptsPage.tsx:70`: `const canManage = useEverywherePermissions().has("deploy.script.manage");`.
- `licenses/VendorsPage.tsx:25` y `licenses/ProductsPage.tsx:45`: `const canManage = useEverywherePermissions().has("licenses.manage");`.
- `licenses/ReviewQueue.tsx`: tras `:32` `const canMap = useEverywherePermissions().has("licenses.manage");`, y el botón «Map» (`:106-108`) queda `{canMap ? (<Button variant="outline" onClick={() => setMapping(row)}>{t("licenses.review.map")}</Button>) : null}`. «Ignore» no cambia.
- `budget/BudgetAdminPage.tsx`: tras `:110` `const canManageRates = useEverywherePermissions().has("budget.manage");`, y `:129` `<FxRatesPanel canManage={canManageRates} />`.
- `provisioning/ImagesPage.tsx:57` y `provisioning/ProfilesPage.tsx:49`: `const canManage = useEverywherePermissions().has("provisioning.manage");`.
- `error-monitor/api.ts:69-73`: el cuerpo de `useErrorTriagePermission` pasa a `return useEverywherePermissions().has("platform.errors.write");` y su comentario gana «…and since phase 3, on EVERY client: `/platform/errors/{id}/resolve` is in `GLOBAL_WRITES`.» (`Me`, `useQuery` y `useApi` siguen en uso en `:24-25` y en los demás hooks).
- `ai-automation/AutonomyDialPage.tsx`: tras `const setCeiling = useSetAiCeiling();` (`:157`):

```tsx
  // The dial is configuration every client shares (`PUT /ai/autonomy` is in
  // GLOBAL_WRITES): `ai.admin` on EVERY client, or it is read-only (phase 3
  // design §2).
  const canSet = useEverywherePermissions().has("ai.admin");
```

  en `<Chips` (`:383`) se añade `disabled={!canSet}`, y el bloque `<div className="form-actions" …>…</div>` (`:410-438`) queda dentro de `{canSet ? ( … ) : (<p className="note" style={{ margin: 0, padding: "14px 18px 16px" }}>{t("aiAutomation.dial.everyClientOnly")}</p>)}`.
- `people/PersonPage.tsx:190`: se sustituye por

```tsx
  // Staff are switched off in Settings, through a route of configuration every
  // client shares (`POST /users/{id}/disable`): `users.manage` on EVERY client
  // (phase 3 design §2). A client's contact is switched off as a contact.
  const everywhere = useEverywherePermissions();
  const canTurnOffContact = useCan("clients.manage");
  const canTurnOff =
    data.account?.kind === "contact" ? canTurnOffContact : everywhere.has("users.manage");
```

  y `turnOffPermission` sale del import de `./api` (`:15-25`), que queda sin uso.
- `components/forEveryClient.tsx`: el import pasa a `import { useEverywherePermissions } from "../lib/mePermissions";`, `:36-37` a `const permissions = useEverywherePermissions(); if (!permissions.has(code)) return null;`, y en el comentario de `:9-14` «EVERY client -- what `/me`'s permissions list --» pasa a «EVERY client -- `/me`'s `permissions_everywhere` --».

Textos. `frontend/src/i18n/en/settings.ts`, tras `readOnly` (`:38`):

```ts
  readOnlyEveryClient: "Read-only: this change requires the {code} permission on every client, because every client shares this configuration. The server enforces it; this console only reflects it.",
```

`frontend/src/i18n/es/settings.ts`, tras `readOnly` (`:32`):

```ts
  readOnlyEveryClient: "Solo lectura: este cambio requiere el permiso {code} en todos los clientes, porque todos los clientes comparten esta configuración. Lo aplica el servidor; esta consola solo lo refleja.",
```

`frontend/src/i18n/en/aiAutomation.ts`, en `dial`, tras `save: "Save changes",` (`:155`):

```ts
    everyClientOnly: "Read-only: every client shares the autonomy dial, so changing it requires the ai.admin permission on every client.",
```

`frontend/src/i18n/es/aiAutomation.ts`, en `dial`, tras `save: "Guardar cambios",` (`:147`):

```ts
    everyClientOnly: "Solo lectura: todos los clientes comparten el dial de autonomía, así que cambiarlo requiere el permiso ai.admin en todos los clientes.",
```

- [ ] **Step 9: Comprobar que pasan**

Run: `npx vitest run src/test/globalWrites.test.tsx src/modules/settings src/modules/sla src/modules/notification src/modules/health src/modules/agent-releases src/modules/service-desk src/modules/deploy src/modules/licenses src/modules/budget src/modules/provisioning src/modules/email-intake src/modules/error-monitor src/modules/ai-automation src/modules/people src/components src/i18n`
Expected: PASS. Las pruebas existentes de estas pantallas no dicen `permissions_everywhere` y siguen en verde por la lectura de respaldo (`permissions`).

- [ ] **Step 10: Commit**

```bash
git add frontend/src/test/globalWrites.test.tsx frontend/src/modules frontend/src/components frontend/src/i18n
git commit -m "feat(console): writes to configuration every client shares need the code on every client (phase 3 Task 8)"
```

- [ ] **Step 11: Escribir la prueba que falla del nombre de cliente, y corregir las que fijaban el viejo**

```tsx
// frontend/src/modules/settings/__tests__/clientName.test.tsx
/**
 * A row's client by name (phase 3 design §3.4). From /me's `clients` -- the
 * person's own clients, which needs no `clients.read` -- so every technician
 * reads a name, never an id. A row every client shares, or an id /me does not
 * list, is "All clients". The Unassigned client is named in the person's
 * language.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../../i18n/core";
import type { ApiClient, Me } from "../../../lib/api";
import { useClientName } from "../api";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "tech@example.com",
  display_name: "Tech",
  permissions: ["licenses.read"],
  clients: [
    { id: "o1", name: "Novieri", kind: "internal", enabled_modules: [], permissions: ["licenses.read"] },
    { id: "c2", name: "Globex", kind: "customer", enabled_modules: [], permissions: ["licenses.read"] },
    { id: "c9", name: "Unassigned", kind: "unassigned", enabled_modules: [], permissions: ["licenses.read"] },
  ],
};

function nameOf(me: Me = ME) {
  const listClients = vi.fn();
  mockUseApi.mockReturnValue({ me: vi.fn().mockResolvedValue(me), listClients } as unknown as ApiClient);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  return { ...renderHook(() => useClientName(), { wrapper }), listClients };
}

describe("useClientName", () => {
  it("names a client from /me, with no clients.read and no read of the clients list", async () => {
    const { result, listClients } = nameOf();
    await waitFor(() => expect(result.current("c2")).toBe("Globex"));
    expect(result.current("o1")).toBe("Novieri");
    expect(listClients).not.toHaveBeenCalled();
  });

  it("names a row every client shares, and a client /me does not list, as all clients", async () => {
    const { result } = nameOf();
    await waitFor(() => expect(result.current("c2")).toBe("Globex"));
    expect(result.current(null)).toBe("All clients");
    expect(result.current("c-gone")).toBe("All clients");
  });

  it("names the Unassigned client, and every client, in the person's language", async () => {
    const { result } = nameOf();
    await waitFor(() => expect(result.current("c2")).toBe("Globex"));
    expect(result.current("c9")).toBe("Unassigned");
    setActiveLanguage("es");
    expect(result.current("c9")).toBe("Sin asignar");
    expect(result.current(null)).toBe("Todos los clientes");
  });
});
```

**Pruebas que fijan el comportamiento que esta tarea cambia a propósito** (antes: el nombre salía de `GET /clients` con `clients.read`, y sin él se veía el id; ahora sale de `/me.clients` y un id desconocido es «All clients»). Se corrigen, no se borran:

1. `frontend/src/modules/licenses/__tests__/licenses.test.tsx`
   - `:101` `const ME_READS_CLIENTS: Me = { ...ME, permissions: [...ME.permissions, "clients.read"] };` → `const ME_READS_CLIENTS: Me = { ...ME, permissions: [...ME.permissions, "clients.read"], clients: [meClient("o1", "Novieri"), meClient("c2", "Globex")] };`, con, justo antes:

```ts
/** A client of /me (phase 3 design §1.3): where a row's client name comes from now. */
function meClient(id: string, name: string): MeClient {
  return { id, name, kind: id === "o1" ? "internal" : "customer", enabled_modules: [], permissions: ["licenses.read"] };
}
```

     y `MeClient` en el import de tipos de `../../../lib/api`.
   - `:1019` `expect(api.listClients).toHaveBeenCalledWith({ include_archived: true });` → `expect(api.listClients).not.toHaveBeenCalled();` (el nombre ya no lee `GET /clients`).
   - `:1022-1029` «names a row's client by its id for someone who cannot read clients» → título «names a row's client /me does not list as all clients, and never reads the clients list»; `expect(within(row).getByText("o1")).toBeInTheDocument();` → `expect(within(row).getByText("All clients")).toBeInTheDocument();`.
2. `frontend/src/modules/ipam/__tests__/clients.test.tsx`
   - `ME` (`:27-33`) gana, tras `permissions`:

```ts
  clients: [
    { id: "o1", name: "Novieri", kind: "internal", enabled_modules: [], permissions: ["ipam.read", "ipam.assign", "ipam.manage", "ipam.scope.manage"] },
    { id: "c-x", name: "Acme", kind: "customer", enabled_modules: [], permissions: ["ipam.read", "ipam.assign", "ipam.manage", "ipam.scope.manage"] },
    { id: "c-y", name: "Yonder", kind: "customer", enabled_modules: [], permissions: ["ipam.read", "ipam.assign", "ipam.manage", "ipam.scope.manage"] },
  ],
```

   - `:182-184` comentario «`lab · o1`: the VRF names its client (its id here: this `/me` cannot read the clients' names).» → «`lab · Novieri`: the VRF names its client, by the name /me gives it.»; `{ name: "lab · o1" }` → `{ name: "lab · Novieri" }`.
   - `:566` `{ name: "global · c-y" }` → `{ name: "global · Yonder" }`.
3. `frontend/src/modules/patch/__tests__/patch.test.tsx`
   - Antes de `:3244`, `const MY_CLIENTS: MeClient[] = [{ id: "o1", name: "Novieri", kind: "internal", enabled_modules: [], permissions: ME.permissions }, { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: ME.permissions }];` (y `MeClient` en el import de tipos); `:3244` → `const READS_CLIENTS: Me = { ...ME, permissions: [...ME.permissions, "clients.read"], clients: MY_CLIENTS };`.
   - `:3401-3414` «names the customer by its id when the clients list cannot be read» → «names the customer from /me, without reading the clients list»: `api = customerMachine({ listClients });` → `api = customerMachine({ me: vi.fn().mockResolvedValue({ ...ME, clients: MY_CLIENTS }), listClients });`; el texto esperado «…covers the 1 device of c-acme in the test ring — not sentinel alone.» → «…covers the 1 device of Acme in the test ring — not sentinel alone.»; `expect(listClients).not.toHaveBeenCalled();` se queda.
   - `:3482-3487` «names the client by its id in the list when the clients list cannot be read» → «names the client from /me in the list, without clients.read»: `twinApprovals({ me: vi.fn().mockResolvedValue(ME) })` → `twinApprovals({ me: vi.fn().mockResolvedValue({ ...ME, clients: MY_CLIENTS }) })`; `getByText("c-acme")` → `getByText("Acme")`.
4. `frontend/src/modules/devices/CandidatesPage.test.tsx`
   - `:102` `me: vi.fn().mockResolvedValue({ org_id: "o1", permissions: [], ...me } as Me),` → `me: vi.fn().mockResolvedValue({ org_id: "o1", permissions: [], clients: [{ id: "o1", name: "Novieri", kind: "internal", enabled_modules: [], permissions: [] }, { id: "c-y", name: "Yonder", kind: "customer", enabled_modules: [], permissions: [] }], ...me } as Me),`.
   - `:394-397` comentario «the client's name for someone holding `clients.read`, its id otherwise» → «the client's name, from /me»; `"172.22.104.55 of o1"` → `"172.22.104.55 of Novieri"`; `"172.22.104.55 of c-y"` → `"172.22.104.55 of Yonder"`.
5. `frontend/src/modules/subscriptions/__tests__/subscriptions.test.tsx:1017`: `api.me = vi.fn().mockResolvedValue({ ...ME, permissions: [...ME.permissions, "clients.read"] });` → `api.me = vi.fn().mockResolvedValue({ ...ME, clients: [{ id: "o1", name: "Novieri", kind: "internal", enabled_modules: [], permissions: ME.permissions }, { id: "c2", name: "Globex", kind: "customer", enabled_modules: [], permissions: ME.permissions }] });` (el `listClients` de `:1018` se queda; ya no se llama).

- [ ] **Step 12: Comprobar que falla la nueva y, con el código viejo, las corregidas**

Run: `npx vitest run src/modules/settings/__tests__/clientName.test.tsx src/modules/licenses src/modules/ipam/__tests__/clients.test.tsx src/modules/patch src/modules/devices/CandidatesPage.test.tsx src/modules/subscriptions`
Expected: FAIL — `clientName.test.tsx` («o1» en vez de «Novieri» sin `clients.read`); las corregidas, por lo mismo (`lab · o1` donde esperan `lab · Novieri`).

- [ ] **Step 13: `useClientName`**

`frontend/src/modules/settings/api.ts:615-629`, comentario y función:

```ts
/**
 * A client's name by id, for a column or a sentence that names a row's
 * client (phase 3 design §3.4). Read from `/me`'s `clients` -- the person's
 * own clients, which needs no `clients.read` -- so every technician reads a
 * name, never an id, and no `GET /clients` is made for it.
 *
 * An id `/me` does not list, or none at all, is a row every client shares:
 * "All clients". (`/me` leaves archived clients out, so an archived client's
 * row reads the same; the design does not tell the two apart.) The Unassigned
 * client is named in the person's language, as the top bar names it.
 */
export function useClientName(): (clientId: string | null) => string {
  const me = useMe();
  const known = new Map((me.data?.clients ?? []).map((client) => [client.id, client]));
  return (clientId) => {
    const client = clientId === null ? undefined : known.get(clientId);
    if (client === undefined) return translate("lib.clients.everyClient");
    return client.kind === "unassigned" ? translate("lib.clients.unassigned") : client.name;
  };
}
```

y `import { translate } from "../../i18n/core";` junto a los imports (`:23-36`).

Textos. `frontend/src/i18n/en/lib.ts`, en `clients` (Task 7), tras `unassigned`: `everyClient: "All clients",`. `frontend/src/i18n/es/lib.ts`, en `clients`: `everyClient: "Todos los clientes",`.

- [ ] **Step 14: Comprobar que pasan, y commit**

Run: `npx vitest run src/modules/settings src/modules/licenses src/modules/ipam src/modules/patch src/modules/devices src/modules/subscriptions src/i18n`
Expected: PASS.

```bash
git add frontend/src/modules frontend/src/i18n
git commit -m "feat(console): client names come from /me, with no clients.read (phase 3 Task 8)"
```

- [ ] **Step 15: Verificación**

Desde `frontend/`: `npx vitest run` completo, `npx tsc -b` y `npm run build`.
Expected: todo en verde. `tsc` cuida de los imports que quedan sin uso (`noUnusedLocals`); si alguno de los listados arriba no lo estaba, se quita solo ese.

---

### Task 9: Consola — `clientSelection.ts`, `ClientField`, `SiteField` y el piloto en tickets

Diseño: §3.1, §3.2 y §4.5 (crear un ticket sin equipo). Inventario: §1 (`lib/internalClient.ts`), §9.

Depende de las Tasks 7 y 8. La columna Cliente de las listas de tickets (§4.5, tercera viñeta, y §3.4) es de la Task 13. `lib/internalClient.ts` **no se borra aquí**: las Tasks 10 y 11 migran el resto de puntos y la Task 12 lo borra.

**Files:**
- Create: `frontend/src/lib/clientSelection.ts`
- Create: `frontend/src/lib/clientSites.ts`
- Create: `frontend/src/components/ClientField.tsx`, `frontend/src/components/SiteField.tsx`
- Modify: `frontend/src/modules/service-desk/NewTicketDialog.tsx` — imports (`:6-16`), reglas (`:36-40`), estado (`:58-91`), `reset()` (`:98-108`), `submit()` (`:120-155`), formulario (`:162-163`)
- Modify: `frontend/src/i18n/{en,es}/lib.ts` (`clientSelection`), `frontend/src/i18n/{en,es}/components.ts` (`clientField`, `siteField`)
- Test (nuevos): `frontend/src/lib/clientSelection.test.tsx`, `frontend/src/components/ClientField.test.tsx`, `frontend/src/components/SiteField.test.tsx`
- Test (corregido y añadido): `frontend/src/modules/service-desk/__tests__/serviceDesk.test.tsx` (`:59-65`, `:494-501`, `:519-538`, `:561-563` y un `describe` nuevo)

**Interfaces:**
- Consumes (Task 7): `useClientScope()`, `MeClient`, `setSelectedClient` (pruebas). `ApiClient.listClientSites(clientId)` (`api.ts:1645-1647`, existente).
- Produces (01-interfaces):
  - `frontend/src/lib/clientSelection.ts`: `useNewRowClient(code: string, parentClientId?: string | null): { clientId: string | null; needsField: boolean; options: MeClient[] }`; `requireKnown(id: string | null): string`; `loadingUntilClient(query, client)`; `class ClientNotChosen extends Error`.
  - `frontend/src/components/ClientField.tsx`: `ClientField({ code, value, onChange }: { code: string; value: string; onChange: (clientId: string) => void })`.
  - `frontend/src/components/SiteField.tsx`: `SiteField({ clientId, value, onChange }: { clientId: string | null; value: string; onChange: (siteId: string) => void })`.
  - `frontend/src/lib/clientSites.ts`: `useClientSites(clientId: string | null)`.
- Produces (nombres nuevos): `interface NewRowClient`, `clientsHolding(clients: readonly MeClient[], code: string): MeClient[]`.

**Decisiones del plan (Task 9):**
- *Decisión del plan: `parentClientId` distingue tres casos.* `undefined`: la fila no cuelga de nada. `null`: cuelga de algo cuyo cliente aún no se conoce (cargando): no se pregunta y `clientId` es `null`, así que `requireKnown` no deja enviar. Una cadena: ese cliente.
- *Decisión del plan: `options` se calcula siempre* (clientes de `/me` con el código, sin «Sin asignar», en el orden de `useClientScope`); `needsField` dice si se muestra. Con un solo cliente posible **no** se elige por la persona: Q1 dice que el registro va donde ella diga.
- *Decisión del plan: `ClientNotChosen` tiene su propia frase (`lib.clientSelection.notChosen`), y `lib/internalClient.ts` no cambia.* Durante las Tasks 10-11 conviven dos `requireKnown`; la Task 12 borra el viejo y su clave. La frase nueva conserva «Your account has not loaded yet» / «Tu cuenta aún no se ha cargado», que es lo que leen `serviceDesk.test.tsx:575` y el mensaje de hoy.
- *Decisión del plan: `SiteField` suelta una sede que no es del cliente actual y elige la única que hay.* §3.2: «Si hay una sola, viene ya elegida»; al cambiar de cliente en el mismo formulario, la sede del anterior no puede quedarse. Comparte la clave `["client-sites", clientId]` con `useIpamSites` (`ipam/api.ts:213-224`): misma respuesta, una caché.
- *Decisión del plan: en el diálogo de tickets el campo Cliente va el primero.* Con equipo elegido desaparece (el servidor toma el cliente del equipo y rechaza otro: `service_desk/schema.py:245-248`), y no se envía `client_id`, como hoy.
- *Decisión del plan: las pruebas de `TicketListPage wiring` eligen «Acme Dental» (`c-acme`) arriba en su `beforeEach`.* Con `o1` no se distinguiría el cliente elegido del `org_id` que se usaba antes.

- [ ] **Step 1: Escribir las pruebas que fallan de la pieza compartida y los dos campos**

```tsx
// frontend/src/lib/clientSelection.test.tsx
/**
 * THE CLIENT OF A NEW ROW (phase 3 design §3.1): the parent's, if it has one;
 * else the client chosen in the top bar; else, with "All my clients", a
 * required Client field offering only the clients where the person holds the
 * write's code -- never Unassigned, and never the internal client unasked
 * (Q1). Nothing is sent before the client is known (`requireKnown`).
 */

import { QueryClient, QueryClientProvider, type UseQueryResult } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../i18n/core";
import type { ApiClient, Me, MeClient } from "./api";
import { setSelectedClient } from "./clientChoice";
import { ClientNotChosen, loadingUntilClient, requireKnown, useNewRowClient } from "./clientSelection";

const mockUseApi = vi.fn();
vi.mock("./ApiContext", () => ({ useApi: () => mockUseApi() }));

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";
const ZETA = "5c9e1d2f-3a4b-4c5d-8e6f-7a8b9c0d1e02";
const NORTH = "2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f06";
const INTERNAL = "9f8e7d6c-5b4a-4321-8fed-cba987654303";
const UNASSIGNED = "1a2b3c4d-5e6f-4789-8abc-def012345604";
const STALE = "7e6d5c4b-3a29-4180-9fed-cba987654305";

function client(id: string, name: string, permissions: string[], kind: MeClient["kind"] = "customer"): MeClient {
  return { id, name, kind, enabled_modules: [], permissions };
}

const ME: Me = {
  user_id: "u1",
  org_id: INTERNAL,
  email: "tech@example.com",
  display_name: "Tech",
  permissions: ["ticket.read", "ticket.write"],
  clients: [
    client(ZETA, "Zeta Foods", ["ticket.read", "ticket.write"]),
    client(NORTH, "Northwind", ["ticket.read"]),
    client(INTERNAL, "Toeshee", ["ticket.read", "ticket.write"], "internal"),
    client(UNASSIGNED, "Unassigned", ["ticket.read", "ticket.write"], "unassigned"),
    client(ACME, "Acme Dental", ["ticket.read", "ticket.write"]),
  ],
};

function newRow(parent?: string | null, me: Promise<Me> = Promise.resolve(ME)) {
  const meFn = vi.fn().mockReturnValue(me);
  mockUseApi.mockReturnValue({ me: meFn } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
  return { ...renderHook(() => useNewRowClient("ticket.write", parent), { wrapper }), meFn };
}

describe("useNewRowClient", () => {
  it("takes the parent's client and asks nothing, whatever the top bar says", async () => {
    const { result, meFn } = newRow(ACME);
    await waitFor(() => expect(meFn).toHaveBeenCalled());
    expect(result.current.clientId).toBe(ACME);
    expect(result.current.needsField).toBe(false);
  });

  it("asks nothing and names nothing while the parent's client is not known", async () => {
    const { result, meFn } = newRow(null);
    await waitFor(() => expect(meFn).toHaveBeenCalled());
    expect(result.current).toMatchObject({ clientId: null, needsField: false });
  });

  it("takes the client chosen in the top bar", async () => {
    setSelectedClient(ZETA);
    const { result } = newRow();
    await waitFor(() => expect(result.current.clientId).toBe(ZETA));
    expect(result.current.needsField).toBe(false);
  });

  it("asks with every client, offering only the clients holding the code, never Unassigned", async () => {
    const { result } = newRow();
    await waitFor(() => expect(result.current.needsField).toBe(true));
    expect(result.current.clientId).toBeNull();
    expect(result.current.options.map((c) => c.name)).toEqual(["Acme Dental", "Toeshee", "Zeta Foods"]);
  });

  it("asks nothing and names nothing until /me answers", () => {
    const { result } = newRow(undefined, new Promise<Me>(() => {}));
    expect(result.current).toMatchObject({ clientId: null, needsField: false });
  });

  it("names nothing for a top-bar choice /me does not list", async () => {
    setSelectedClient(STALE);
    const { result, meFn } = newRow();
    await waitFor(() => expect(meFn).toHaveBeenCalled());
    await waitFor(() => expect(result.current.options).toHaveLength(3));
    expect(result.current).toMatchObject({ clientId: null, needsField: false });
  });
});

describe("requireKnown", () => {
  it("hands back a known id unchanged", () => {
    expect(requireKnown(ACME)).toBe(ACME);
  });

  it("refuses an unknown one with a sentence, in the person's language", () => {
    expect(() => requireKnown(null)).toThrow(ClientNotChosen);
    expect(() => requireKnown(null)).toThrow(/account has not loaded yet/);
    setActiveLanguage("es");
    expect(() => requireKnown(null)).toThrow(/tu cuenta aún no se ha cargado/i);
  });
});

describe("loadingUntilClient", () => {
  it("reads as loading while the client it waits for is unknown", () => {
    const idle = { isPending: true, isLoading: false } as UseQueryResult<unknown, unknown>;
    expect(loadingUntilClient(idle, null).isLoading).toBe(true);
    expect(loadingUntilClient(idle, ACME).isLoading).toBe(false);
  });
});
```

```tsx
// frontend/src/components/ClientField.test.tsx
/**
 * The Client field (phase 3 design §3.1, Q1): required, and offering only the
 * person's clients where they hold the write's code -- never Unassigned.
 * Nothing is chosen for the person.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../i18n/core";
import type { ApiClient, Me, MeClient } from "../lib/api";
import { ClientField } from "./ClientField";

const mockUseApi = vi.fn();
vi.mock("../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";
const ZETA = "5c9e1d2f-3a4b-4c5d-8e6f-7a8b9c0d1e02";
const NORTH = "2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f06";
const UNASSIGNED = "1a2b3c4d-5e6f-4789-8abc-def012345604";

function client(id: string, name: string, permissions: string[], kind: MeClient["kind"] = "customer"): MeClient {
  return { id, name, kind, enabled_modules: [], permissions };
}

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "tech@example.com",
  display_name: "Tech",
  permissions: ["ticket.read", "ticket.write"],
  clients: [
    client(ZETA, "Zeta Foods", ["ticket.write"]),
    client(NORTH, "Northwind", ["ticket.read"]),
    client(UNASSIGNED, "Unassigned", ["ticket.write"], "unassigned"),
    client(ACME, "Acme Dental", ["ticket.write"]),
  ],
};

function renderField(me: Me = ME, onChange = vi.fn()) {
  mockUseApi.mockReturnValue({ me: vi.fn().mockResolvedValue(me) } as unknown as ApiClient);
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ClientField code="ticket.write" value="" onChange={onChange} />
    </QueryClientProvider>,
  );
  return onChange;
}

describe("ClientField", () => {
  it("offers the clients holding the code, by name, never Unassigned, and chooses none", async () => {
    renderField();
    const field = await screen.findByLabelText("Client");
    await within(field).findByRole("option", { name: "Acme Dental" });
    expect(within(field).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Choose a client",
      "Acme Dental",
      "Zeta Foods",
    ]);
    expect(field).toHaveValue("");
    expect(field).toBeRequired();
  });

  it("reports the client chosen", async () => {
    const user = userEvent.setup();
    const onChange = renderField();
    const field = await screen.findByLabelText("Client");
    await within(field).findByRole("option", { name: "Acme Dental" });
    await user.selectOptions(field, ACME);
    expect(onChange).toHaveBeenCalledWith(ACME);
  });

  it("says so when no client of the person allows the write", async () => {
    renderField({ ...ME, clients: [client(NORTH, "Northwind", ["ticket.read"])] });
    expect(await screen.findByText("None of your clients lets you do this.")).toBeInTheDocument();
  });

  it("speaks the person's language", async () => {
    setActiveLanguage("es");
    renderField();
    const field = await screen.findByLabelText("Cliente");
    expect(within(field).getByRole("option", { name: "Elige un cliente" })).toBeInTheDocument();
  });
});
```

```tsx
// frontend/src/components/SiteField.test.tsx
/**
 * The Site field (phase 3 design §3.2): the client's live sites; the only one
 * already chosen; nothing listed before the client is known; and a site of
 * another client never kept.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient, ClientSiteOut } from "../lib/api";
import { SiteField } from "./SiteField";

const mockUseApi = vi.fn();
vi.mock("../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";

function site(id: string, name: string, archived: string | null = null): ClientSiteOut {
  return {
    id,
    client_id: ACME,
    name,
    address: "",
    description: "",
    egress_networks: [],
    archived_at: archived,
    device_count: 0,
  };
}

function renderSite(clientId: string | null, sites: ClientSiteOut[], value = "") {
  const listClientSites = vi.fn().mockResolvedValue(sites);
  mockUseApi.mockReturnValue({ listClientSites } as unknown as ApiClient);
  const onChange = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <SiteField clientId={clientId} value={value} onChange={onChange} />
    </QueryClientProvider>,
  );
  return { listClientSites, onChange };
}

describe("SiteField", () => {
  it("asks for the client first, and reads nothing without one", () => {
    const { listClientSites } = renderSite(null, [site("s1", "Bogotá")]);
    expect(screen.getByLabelText("Site")).toBeDisabled();
    expect(screen.getByText("Choose the client first.")).toBeInTheDocument();
    expect(listClientSites).not.toHaveBeenCalled();
  });

  it("offers the client's live sites, never an archived one", async () => {
    const { listClientSites } = renderSite(ACME, [
      site("s1", "Bogotá"),
      site("s2", "Medellín"),
      site("s3", "Cali", "2026-09-01T00:00:00Z"),
    ]);
    const field = screen.getByLabelText("Site");
    await within(field).findByRole("option", { name: "Medellín" });
    expect(within(field).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Choose a site",
      "Bogotá",
      "Medellín",
    ]);
    expect(listClientSites).toHaveBeenCalledWith(ACME);
  });

  it("chooses the only site by itself", async () => {
    const { onChange } = renderSite(ACME, [site("s1", "Bogotá")]);
    await waitFor(() => expect(onChange).toHaveBeenCalledWith("s1"));
  });

  it("drops a site that is not the client's", async () => {
    const { onChange } = renderSite(ACME, [site("s1", "Bogotá"), site("s2", "Medellín")], "s-other");
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(""));
  });

  it("says when the client has no site", async () => {
    renderSite(ACME, []);
    expect(
      await screen.findByText("This client has no sites. Add one on the Clients page."),
    ).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Comprobar que fallan**

Run: `npx vitest run src/lib/clientSelection.test.tsx src/components/ClientField.test.tsx src/components/SiteField.test.tsx`
Expected: FAIL — `Failed to resolve import "./clientSelection"` / `"./ClientField"` / `"./SiteField"`.

- [ ] **Step 3: La pieza compartida, las sedes y los dos campos**

```ts
// frontend/src/lib/clientSelection.ts
/**
 * THE CLIENT OF A NEW ROW (phase 3 design §3.1). One rule for every form that
 * creates something a client owns:
 *
 * 1. A row that hangs from something with a client -- a device, a group, a
 *    VRF, a subnet, a group's policy -- goes to that client, and the form
 *    does not ask (`parentClientId`; the operator's rule of 2026-10-28).
 * 2. Otherwise, the client chosen in the top bar.
 * 3. With "All my clients", the form shows a Client field (`ClientField`),
 *    required, offering only the person's clients whose `/me` permissions
 *    hold the code of that write -- never the Unassigned client (C10), and
 *    never the internal one unless the person picks it (Q1).
 *
 * The body is still built INSIDE the mutation (`requireKnown`): nothing is
 * sent before the client is known.
 *
 * Replaces `lib/internalClient.ts`, which filed every new row under the
 * organisation's internal client until this phase. Its callers move here in
 * Tasks 10 and 11, and Task 12 deletes it.
 *
 * INVARIANT 6: THIS GATES NOTHING. The server refuses a client outside the
 * request's scope, or one where the person lacks the code, whatever a form
 * sent.
 */

import type { UseQueryResult } from "@tanstack/react-query";
import { useMemo } from "react";

import { translate } from "../i18n/core";
import type { MeClient } from "./api";
import { useClientScope } from "./clientScope";

export interface NewRowClient {
  /** Where the row goes when the form need not ask; `null` when it must, or while unknown. */
  clientId: string | null;
  /** The form shows a required Client field: "All my clients", and no parent decides. */
  needsField: boolean;
  /** What that field offers: the person's clients holding the code, never Unassigned. */
  options: MeClient[];
}

/** The person's clients where `code` is held, the Unassigned client never (C10). */
export function clientsHolding(clients: readonly MeClient[], code: string): MeClient[] {
  return clients.filter((client) => client.kind !== "unassigned" && client.permissions.includes(code));
}

/**
 * Where a new row goes. `code` is the permission its create route checks.
 * `parentClientId`: absent for a row that hangs from nothing; `null` for one
 * whose parent's client is not known yet (still loading) -- nothing is asked
 * and nothing can be sent until it is; an id, that client.
 */
export function useNewRowClient(code: string, parentClientId?: string | null): NewRowClient {
  const { selected, clients, ready } = useClientScope();
  const options = useMemo(() => clientsHolding(clients, code), [clients, code]);
  if (parentClientId !== undefined) return { clientId: parentClientId, needsField: false, options };
  if (!ready) return { clientId: null, needsField: false, options };
  if (selected !== null) return { clientId: selected, needsField: false, options };
  return { clientId: null, needsField: true, options };
}

/**
 * Thrown by a create that cannot name its client yet: `/me` has not answered,
 * the parent has not loaded, or the Client field is empty. A sentence, in the
 * person's language, so the form's error surface (`describeSubmitError`)
 * shows it as it is; and nothing is sent.
 */
export class ClientNotChosen extends Error {
  constructor() {
    super(translate("lib.clientSelection.notChosen"));
    this.name = "ClientNotChosen";
  }
}

/** `id`, or `ClientNotChosen` when it is not known yet. For a mutation's body. */
export function requireKnown(id: string | null): string {
  if (id === null) throw new ClientNotChosen();
  return id;
}

/**
 * A read that cannot be sent before its client is known is LOADING until
 * then, not empty: a disabled query reports `isLoading: false` with no data,
 * which a panel would draw as "nothing yet" while `/me` is still in flight.
 */
export function loadingUntilClient<R extends UseQueryResult<unknown, unknown>>(
  query: R,
  client: string | null,
): R {
  return client == null && query.isPending ? { ...query, isLoading: true } : query;
}
```

```ts
// frontend/src/lib/clientSites.ts
/**
 * A client's sites (`GET /clients/{id}/sites`, phase 1), archived ones left
 * out by the server, for the Site field of a form that files something under
 * one (phase 3 design §3.2). The key is IPAM's (`useIpamSites`): the same
 * answer, one cache entry.
 *
 * LOADING until the client is known, not empty (`loadingUntilClient`); `null`
 * reads nothing.
 */

import { useQuery } from "@tanstack/react-query";

import type { ApiError, ClientSiteOut } from "./api";
import { useApi } from "./ApiContext";
import { loadingUntilClient, requireKnown } from "./clientSelection";

export function useClientSites(clientId: string | null) {
  const api = useApi();
  return loadingUntilClient(
    useQuery<ClientSiteOut[], ApiError>({
      queryKey: ["client-sites", clientId],
      queryFn: () => api.listClientSites(requireKnown(clientId)),
      enabled: clientId !== null,
      retry: false,
    }),
    clientId,
  );
}
```

```tsx
// frontend/src/components/ClientField.tsx
/**
 * THE CLIENT FIELD (phase 3 design §3.1, Q1): what a form shows when a new
 * row's client is decided neither by its parent nor by the top bar -- "All
 * my clients" is chosen. Required, and it offers only the person's clients
 * whose `/me` permissions hold `code`, the code of that write; the Unassigned
 * client never (C10). Nothing is chosen for the person, not even when one
 * client is the only choice: the row goes where they say.
 *
 * Shown where `useNewRowClient(code).needsField` says so; the form keeps the
 * value and sends it inside its mutation (`requireKnown`).
 *
 * INVARIANT 6: THIS GATES NOTHING. The server refuses a client where the
 * person lacks the code, whatever this offered.
 */

import { useT } from "../i18n/context";
import { useNewRowClient } from "../lib/clientSelection";
import { Field, inputClass } from "./ui";

export function ClientField({
  code,
  value,
  onChange,
}: {
  /** The permission the create route checks: `ticket.write`, `docs.manage`… */
  code: string;
  /** The client chosen, or `""`. */
  value: string;
  onChange: (clientId: string) => void;
}) {
  const t = useT();
  const { options } = useNewRowClient(code);
  return (
    <Field
      label={t("components.clientField.label")}
      help={options.length === 0 ? t("components.clientField.none") : undefined}
    >
      <select
        className={inputClass}
        required
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">{t("components.clientField.choose")}</option>
        {options.map((client) => (
          <option key={client.id} value={client.id}>
            {client.name}
          </option>
        ))}
      </select>
    </Field>
  );
}
```

```tsx
// frontend/src/components/SiteField.tsx
/**
 * THE SITE A NEW ROW GOES TO (phase 3 design §3.2): an enrollment token, an
 * install link or command, a hand-made asset, a managed deployment of the
 * agent. Its client's live sites; with one, it is already chosen. The
 * internal client's "Principal" is no longer the only site there is.
 *
 * Nothing is listed before the client is known -- a form takes it from its
 * parent, from the top bar, or from its Client field
 * (`lib/clientSelection.ts`) -- and a site of another client is never kept:
 * when the client changes, the choice goes.
 *
 * INVARIANT 6: THIS GATES NOTHING. The server refuses a site outside the
 * request's scope whatever this offered.
 */

import { useEffect } from "react";

import { useT } from "../i18n/context";
import { useClientSites } from "../lib/clientSites";
import { Field, inputClass } from "./ui";

export function SiteField({
  clientId,
  value,
  onChange,
}: {
  /** The row's client, or `null` while it is not known. */
  clientId: string | null;
  /** The site chosen, or `""`. */
  value: string;
  onChange: (siteId: string) => void;
}) {
  const t = useT();
  const sites = useClientSites(clientId);
  const live = (sites.data ?? []).filter((site) => site.archived_at === null);
  const only = live.length === 1 ? (live[0]?.id ?? null) : null;
  const known = sites.data !== undefined;
  const kept = value === "" || live.some((site) => site.id === value);

  useEffect(() => {
    if (known && !kept) onChange("");
    else if (value === "" && only !== null) onChange(only);
  }, [known, kept, value, only, onChange]);

  const help =
    clientId === null
      ? t("components.siteField.chooseClientFirst")
      : sites.isLoading
        ? t("components.siteField.loading")
        : sites.isError
          ? t("components.siteField.failed")
          : live.length === 0
            ? t("components.siteField.none")
            : undefined;

  return (
    <Field label={t("components.siteField.label")} help={help}>
      <select
        className={inputClass}
        required
        value={kept ? value : ""}
        disabled={live.length === 0}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">{t("components.siteField.choose")}</option>
        {live.map((site) => (
          <option key={site.id} value={site.id}>
            {site.name}
          </option>
        ))}
      </select>
    </Field>
  );
}
```

Textos. `frontend/src/i18n/en/lib.ts`, tras `clientScope` (Task 7):

```ts
  // A new row's client (lib/clientSelection.ts, phase 3 design §3.1).
  clientSelection: {
    notChosen: "Your account has not loaded yet, or the client this is for has not been chosen, so nothing was sent. Choose the client, or reload the page and try again.",
  },
```

`frontend/src/i18n/es/lib.ts`, tras `clientScope`:

```ts
  clientSelection: {
    notChosen: "Tu cuenta aún no se ha cargado, o aún no se ha elegido el cliente al que pertenece, así que no se envió nada. Elige el cliente, o recarga la página e inténtalo de nuevo.",
  },
```

`frontend/src/i18n/en/components.ts`, tras `forEveryClient` (`:35-38`):

```ts
  // A new row's client, with "All my clients" (components/ClientField.tsx).
  clientField: {
    label: "Client",
    choose: "Choose a client",
    none: "None of your clients lets you do this.",
  },
  // A new row's site (components/SiteField.tsx).
  siteField: {
    label: "Site",
    choose: "Choose a site",
    chooseClientFirst: "Choose the client first.",
    loading: "Loading sites…",
    failed: "The sites could not be loaded.",
    none: "This client has no sites. Add one on the Clients page.",
  },
```

`frontend/src/i18n/es/components.ts`, tras `forEveryClient` (`:37-40`):

```ts
  clientField: {
    label: "Cliente",
    choose: "Elige un cliente",
    none: "Ninguno de tus clientes te permite hacer esto.",
  },
  siteField: {
    label: "Sede",
    choose: "Elige una sede",
    chooseClientFirst: "Elige primero el cliente.",
    loading: "Cargando sedes…",
    failed: "No se pudieron cargar las sedes.",
    none: "Este cliente no tiene sedes. Añade una en la página Clientes.",
  },
```

- [ ] **Step 4: Comprobar que pasan, y commit**

Run: `npx vitest run src/lib/clientSelection.test.tsx src/components/ClientField.test.tsx src/components/SiteField.test.tsx src/lib/internalClient.test.ts src/i18n`
Expected: PASS (`internalClient.test.ts` no cambia).

```bash
git add frontend/src/lib/clientSelection.ts frontend/src/lib/clientSelection.test.tsx frontend/src/lib/clientSites.ts frontend/src/components/ClientField.tsx frontend/src/components/ClientField.test.tsx frontend/src/components/SiteField.tsx frontend/src/components/SiteField.test.tsx frontend/src/i18n
git commit -m "feat(console): one rule for a new row's client, and the Client and Site fields (phase 3 Task 9)"
```

- [ ] **Step 5: Escribir las pruebas que fallan del piloto, y corregir las que fijaban el cliente interno**

En `frontend/src/modules/service-desk/__tests__/serviceDesk.test.tsx`:

1. Import (tras `:7`): `import { setSelectedClient } from "../../../lib/clientChoice";`.

2. **Corregida** `ME_TECHNICIAN` (`:59-65`): gana, tras `permissions`,

```ts
  // The person's clients (phase 3 design §1.3): their organisation's own and
  // one customer's, with the same codes in each.
  clients: [
    { id: "o1", name: "Toeshee", kind: "internal", enabled_modules: [], permissions: ["ticket.read", "ticket.write", "ticket.assign"] },
    { id: "c-acme", name: "Acme Dental", kind: "customer", enabled_modules: [], permissions: ["ticket.read", "ticket.write", "ticket.assign"] },
  ],
```

   (`ME_FULL` la hereda.)

3. **Corregido** el `beforeEach` de `describe("TicketListPage wiring", …)` (`:494-501`): tras `mockUseApi.mockReturnValue(api as ApiClient);`,

```ts
    // A client chosen in the top bar: a ticket without a device goes there,
    // and the form asks nothing (phase 3 design §3.1).
    setSelectedClient("c-acme");
```

4. **Corregida** «submits a new incident ticket with the exact entered fields» (`:519-538`). Antes: `client_id: "o1"`, con el comentario «the internal client, whose id is `/me`'s org_id until the phase-3 selector». Ahora:

```ts
      device_id: null,
      // No device, so the ticket names its client: the one chosen in the top
      // bar -- not the internal client, whose id is `/me`'s org_id (`o1`).
      client_id: "c-acme",
```

5. **Corregido solo el comentario** de «sends nothing and says why when the client cannot be named yet» (`:561-563`): «Without `/me` the console cannot name the internal client: it sends nothing --» → «Without `/me` the console cannot tell whether the chosen client is one of the person's: it sends nothing --». La afirmación (`/has not loaded yet/`, ninguna llamada) no cambia.

6. Un `describe` nuevo, tras `describe("TicketListPage wiring", …)`:

```tsx
// --------------------------------------------------------------------------
// The client of a ticket with no device (phase 3 design §3.1, §4.5; Q1)
// --------------------------------------------------------------------------

describe("NewTicketDialog: the client of a ticket with no device", () => {
  const ME_MSP: Me = {
    ...ME_TECHNICIAN,
    clients: [
      { id: "o1", name: "Toeshee", kind: "internal", enabled_modules: [], permissions: ["ticket.read", "ticket.write"] },
      { id: "c-acme", name: "Acme Dental", kind: "customer", enabled_modules: [], permissions: ["ticket.read", "ticket.write"] },
      { id: "c-north", name: "Northwind", kind: "customer", enabled_modules: [], permissions: ["ticket.read"] },
      { id: "c-unassigned", name: "Unassigned", kind: "unassigned", enabled_modules: [], permissions: ["ticket.read", "ticket.write"] },
    ],
  };
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_MSP),
      listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
      listTicketCategories: vi.fn().mockResolvedValue(CATEGORIES),
      createTicket: vi.fn().mockResolvedValue(TICKET_OPEN),
      listUsers: vi.fn().mockResolvedValue({ users: USERS }),
      listDevices: vi.fn().mockResolvedValue({ items: DEVICES, total: DEVICES.length }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  async function fillTicket(user: ReturnType<typeof userEvent.setup>) {
    await screen.findByRole("option", { name: "Hardware" });
    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "Printer jammed");
    await pickEntity(user, "Requester", /Rachel Requester/);
  }

  function sentBody() {
    return vi.mocked(api.createTicket as ApiClient["createTicket"]).mock.calls[0]?.[0];
  }

  it("asks for the client with every client chosen, offering only where a ticket may be filed", async () => {
    renderWithProviders(<NewTicketDialog open onClose={() => {}} />);
    const field = await screen.findByLabelText("Client");
    await within(field).findByRole("option", { name: "Acme Dental" });
    expect(within(field).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Choose a client",
      "Acme Dental",
      "Toeshee",
    ]);
  });

  it("files the ticket under the client chosen in the form", async () => {
    const user = userEvent.setup();
    renderWithProviders(<NewTicketDialog open onClose={() => {}} />);
    await fillTicket(user);
    await user.selectOptions(await screen.findByLabelText("Client"), "c-acme");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(api.createTicket).toHaveBeenCalledTimes(1));
    expect(sentBody()).toMatchObject({ client_id: "c-acme", device_id: null });
  });

  it("never files it under the internal client unasked: with no choice, nothing is sent", async () => {
    const user = userEvent.setup();
    renderWithProviders(<NewTicketDialog open onClose={() => {}} />);
    await fillTicket(user);
    await screen.findByLabelText("Client");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Client is required");
    expect(api.createTicket).not.toHaveBeenCalled();
  });

  it("asks nothing once a device is chosen, and sends no client: the device's is the ticket's", async () => {
    const user = userEvent.setup();
    renderWithProviders(<NewTicketDialog open onClose={() => {}} />);
    await fillTicket(user);
    await screen.findByLabelText("Client");
    await pickEntity(user, "Device (optional)", /BUILD-01/);
    expect(screen.queryByLabelText("Client")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(api.createTicket).toHaveBeenCalledTimes(1));
    expect(sentBody()).toMatchObject({ device_id: "dev-2" });
    expect(sentBody()).not.toHaveProperty("client_id");
  });

  it("asks nothing with a client chosen in the top bar, and files it there", async () => {
    const user = userEvent.setup();
    setSelectedClient("o1");
    renderWithProviders(<NewTicketDialog open onClose={() => {}} />);
    await fillTicket(user);
    expect(screen.queryByLabelText("Client")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(api.createTicket).toHaveBeenCalledTimes(1));
    expect(sentBody()).toMatchObject({ client_id: "o1" });
  });
});
```

- [ ] **Step 6: Comprobar que fallan**

Run: `npx vitest run src/modules/service-desk/__tests__/serviceDesk.test.tsx`
Expected: FAIL — «submits a new incident ticket…» recibe `client_id: "o1"` (el `org_id`) donde espera `c-acme`; en el `describe` nuevo no hay campo «Client» y el ticket sin elegir se envía con `client_id: "o1"`.

- [ ] **Step 7: Migrar `NewTicketDialog.tsx`**

1. Imports (`:6-16`): se borran `import { useQuery } from "@tanstack/react-query";`, `import type { Me } from "../../lib/api";`, `import { useApi } from "../../lib/ApiContext";` e `import { internalClientId, requireKnown } from "../../lib/internalClient";` (solo servían a `me`), y entran

```tsx
import { ClientField } from "../../components/ClientField";
import { requireKnown, useNewRowClient } from "../../lib/clientSelection";
```

2. Las reglas (`:34-40`):

```tsx
// The words ON SCREEN, so "Requester is required" reads back to the field
// being looked at. `description` is absent: TicketCreate defaults it to "".
// `clientId` only when the form asks for it (`useNewRowClient`).
const ticketRules = (askClient: boolean) => (): readonly FieldRule[] => [
  ...(askClient ? [{ name: "clientId", label: translate("components.clientField.label") }] : []),
  { name: "categoryId", label: translate("serviceDesk.common.category") },
  { name: "subject", label: translate("serviceDesk.common.subject") },
  { name: "requesterId", label: translate("serviceDesk.common.requester") },
];
```

3. En el componente se borran `const api = useApi();` (`:59`) y `const me = useQuery<Me>(…)` (`:60`). Tras `const [deviceId, setDeviceId] = useState(initialDeviceId ?? "");` (`:86`):

```tsx
  // The Client field's value: read only when the form asks for it.
  const [clientId, setClientId] = useState("");
  // THE TICKET'S CLIENT (phase 3 design §3.1, §4.5). With a device it is the
  // device's -- the server takes it from the device and refuses a different
  // one, so nothing is asked and nothing is sent. Without one: the client
  // chosen in the top bar or, with "All my clients", the Client field, which
  // offers only the clients where this person may file a ticket.
  const deviceClient = devices.data?.find((device) => device.id === deviceId)?.client_id ?? null;
  const newRow = useNewRowClient("ticket.write", deviceId ? deviceClient : undefined);
```

4. En `reset()` (`:98-108`), tras `setDeviceId(initialDeviceId ?? "");`: `setClientId("");`.

5. En `submit()`, la primera línea (`:121`) pasa a

```tsx
    const askClient = newRow.needsField;
    const next: Attempt = {
      rules: ticketRules(askClient),
      values: { clientId, categoryId, subject, requesterId },
    };
```

   y el bloque de `:139-147` («A ticket with a device… `PHASE 3`…» hasta `deviceId ? base : …`) pasa a

```tsx
    // A ticket with a device is the device's client's: the server takes it
    // from the device, so nothing is sent. Without one it names its client --
    // the one chosen in the top bar, or in the Client field above -- and
    // nothing is sent while that is not known (`requireKnown`).
    const client = askClient ? clientId || null : newRow.clientId;
    const build = (): TicketCreate =>
      deviceId ? base : { ...base, client_id: requireKnown(client) };
```

6. El primer hijo de `<div className="form-grid">` (`:163`):

```tsx
          {/* With "All my clients" and no device, the person says whose ticket
              this is (Q1): never the internal client by default. */}
          {newRow.needsField ? (
            <ClientField code="ticket.write" value={clientId} onChange={setClientId} />
          ) : null}
```

- [ ] **Step 8: Comprobar que pasan, y commit**

Run: `npx vitest run src/modules/service-desk src/modules/inventory/DeviceDetailPage.hub.test.tsx src/modules/portal src/App.shell.test.tsx src/i18n`
Expected: PASS. `service-desk.es.test.tsx` no cambia: si abre el diálogo con una persona sin `clients`, el campo sale en español («Cliente», «Elige un cliente», «Ninguno de tus clientes…»).

```bash
git add frontend/src/modules/service-desk
git commit -m "feat(console): a ticket with no device asks for its client with every client chosen (phase 3 Task 9)"
```

- [ ] **Step 9: Verificación**

Desde `frontend/`: `npx vitest run` completo, `npx tsc -b` y `npm run build`.
Expected: todo en verde. `grep -n "internalClient" frontend/src/modules/service-desk/NewTicketDialog.tsx` no devuelve nada; `lib/internalClient.ts` sigue existiendo, con sus demás llamadores, hasta la Task 12.

---

#### Notas del redactor de las Tasks 7-9 para el coordinador

**Decisiones del plan tomadas** (detalle en cada tarea):
1. `Me.clients` y `Me.permissions_everywhere` opcionales; sin `permissions_everywhere` se lee `permissions`; sin `clients`, ningún cliente (y todos los módulos en el menú).
2. `requestHeaders(token, extra?, route?: RequestRoute)`; borra cualquier `X-Client-Id` del llamador.
3. La elección vive fuera de React, en el archivo nuevo `lib/clientChoice.ts`; solo se lee un valor con forma de UUID.
4. `ClientScopeProvider` envuelve `<Shell>` dentro de `RequireAuth` y remonta lo que envuelve (`key`) además de `queryClient.clear()`.
5. El selector es la ranura `clientSelector` de `AppShell`, primera de `.top-actions`, y no se dibuja sin clientes; regla CSS nueva `.top-client`.
6. 4403 tras abrir: `/me` y reconexión inmediata; 4403 en el saludo: `/me` y espera.
7. El aviso único es un toast que pinta el proveedor.
8. `visibleGroups(permissions, modules, groups?)`; `Sidebar`/`AppShell` con `modules?` (por defecto, todos).
9. Imaging fuera de `NAV_GROUPS` y dentro de `NAV_UNLISTED` bajo `/admin`.
10. Las pantallas globales se siguen mostrando con `permissions`; sus escrituras miran `permissions_everywhere`. Props opcionales nuevas en `EmailTab`, `AssistantTab` y `AiConnectionsTab`; frase nueva «en todos los clientes» en Ajustes.
11. `useClientName` acepta `string | null` y traduce «Sin asignar»; un cliente archivado se nombra «Todos los clientes».
12. `parentClientId`: `undefined` / `null` / id; `options` siempre calculado; nunca se elige un cliente solo por ser el único.
13. `ClientNotChosen` con su propia frase; `lib/internalClient.ts` intacto (dos `requireKnown` hasta la Task 12).
14. `SiteField` suelta la sede de otro cliente y elige la única; clave compartida con IPAM.
15. En el diálogo de tickets el campo Cliente va primero; las pruebas de `TicketListPage wiring` eligen `c-acme`.

**Nombres que no están en 01-interfaces:** `lib/clientChoice.ts` (`CLIENT_STORAGE_KEY`, `readStoredClient`, `selectedClient`, `subscribeClient`, `setSelectedClient`, `adoptStoredClient`); `RequestRoute` y el privado `ApiClient.route`; `ClientScope`, `orderClients`, `enabledModules`, `useEnabledModules` (`lib/clientScope.tsx`); `CLOSE_SCOPE_REFUSED` (`lib/ws.ts`); `AppShellProps.clientSelector` y `.modules`, `SidebarProps.modules`; `NavModule` y `OPTIONAL_MODULES` (`shell/nav.ts`); `searchDestinations(permissions, modules)`; `everyClientReason` (`settingsState.ts`) y `useGlobalWriteReason` (`settings/api.ts`); props `EmailTab.canEdit`, `AssistantTab.canEdit`, `AiConnectionsTab.canSwitch`; `NewRowClient`, `clientsHolding` (`lib/clientSelection.ts`). Claves i18n: `shell.clientSelector.{label,all}`, `lib.clientScope.gone`, `lib.clients.{unassigned,everyClient}`, `lib.clientSelection.notChosen`, `components.clientField.{label,choose,none}`, `components.siteField.{label,choose,chooseClientFirst,loading,failed,none}`, `settings.readOnlyEveryClient`, `aiAutomation.dial.everyClientOnly`.

**Lo que otras tareas tienen que saber:**
- **Task 1:** el selector asume que `/me.clients` incluye el cliente `unassigned` solo para quien tiene un rol en todos los clientes (lo pone al final). `MeClient.kind` es la unión `customer | internal | unassigned`.
- **Task 2:** `api.ts` importa `isGlobalRoute` de `./globalRoutes`; se le pasa el método en mayúsculas y la ruta sin query string (con los ids ya sustituidos).
- **Tasks 10 y 11:** usar `useNewRowClient(code, parent?)`, `ClientField`, `SiteField`/`useClientSites`, y `requireKnown`/`loadingUntilClient` de `lib/clientSelection.ts`. Una prueba de un formulario migrado necesita `me.clients` y, o bien `setSelectedClient(<id>)` (de `lib/clientChoice.ts`; `src/test/setup.ts` lo vuelve a `null` antes de cada prueba), o bien elegir en el campo «Client». `ForEveryClientToggle` ya lee `permissions_everywhere`; su pista «Unticked, it belongs to your organisation's own client» (`components.forEveryClient.hint`) deja de ser verdad en cuanto un formulario use el cliente elegido: alguien tiene que cambiar ese texto (Task 11 o 12).
- **Task 11:** las políticas de SLA (`PoliciesPanel`), y cualquier fila O que pueda ser global, siguen con `permissions`; una fila global (sin cliente) necesita además el código en todos los clientes, y eso no lo cubre la Task 8.
- **Task 12:** borrar `lib/internalClient.ts`, su prueba y la clave `lib.internalClient.unknown`. «Nuevo cliente» y «Archivar» son rutas globales: condicionar con `useEverywherePermissions().has("clients.manage")`; la cabecera ya no viaja en ellas (`isGlobalRoute`).
- **Task 13:** §3.4 pide además ocultar la columna Cliente con un cliente elegido, «la misma en todos los módulos». Estas tareas no construyen esa pieza; las tres columnas que ya existen (`licenses/ComplianceDashboard.tsx:47,71`, `licenses/RenewalCalendar.tsx:51,67`, `subscriptions/SpendDashboard.tsx:46,61`) siguen siempre visibles. Basta un `useClientScope().selected === null`. La Sede de equipos y el alta pueden usar `SiteField`/`useClientSites`.
- **Task 14:** en `HealthPage.tsx`, `canAdmin` significa ahora `health.admin` en todos los clientes (perfiles y ajustes globales). El «perfil por defecto del cliente» es una escritura de un cliente: debe condicionarse con `permissions` (o con el permiso del cliente en `me.clients`), no con ese `canAdmin`.
- **Coordinador:** el toast del proveedor y el de `LanguageBridge` son dos `.toast-stack`; si coinciden a la vez se solapan (raro). El selector añade un control a la barra superior: la pasada de harmony a 390 px tiene que confirmarlo (regla `.top-client` con `max-width: 120px` en el bloque de 640 px).



#### Addendum del coordinador (Task 9): la pista de «Para todos los clientes»

La Task 8 hace que `ForEveryClientToggle` lea `permissions_everywhere`. Con esta tarea, una fila nueva va al cliente que el formulario elige, así que la pista actual («Sin marcar, es del cliente propio de tu organización») deja de ser verdad. Se corrige aquí, porque esta tarea ya edita `components.ts`.

- [ ] **Step A1: Prueba que falla.** En `frontend/src/components/forEveryClient.test.tsx`, añade:

```tsx
it("says an unticked row belongs to the client the form files it under", () => {
  renderToggle(); // the file's existing helper that renders the toggle for a holder
  expect(screen.getByText("Every client's technicians will see it. Unticked, it belongs to the client this form files it under.")).toBeInTheDocument();
});
```

Si el archivo no tiene un ayudante `renderToggle`, usa el render del caso «renders for a holder» del propio archivo, con el mismo código de permiso.

- [ ] **Step A2:** `npx vitest run src/components/forEveryClient.test.tsx`. Resultado esperado: FAIL, porque el texto todavía dice «your organisation's own client».
- [ ] **Step A3: Textos.**
  - `frontend/src/i18n/en/components.ts`, `forEveryClient.hint`: `"Every client's technicians will see it. Unticked, it belongs to the client this form files it under."`
  - `frontend/src/i18n/es/components.ts`, `forEveryClient.hint`: `"Lo verán los técnicos de todos los clientes. Sin marcar, es del cliente en el que este formulario lo archiva."`
  - Busca el texto viejo en las pruebas (`grep -rn "your organisation's own client\|cliente propio de tu organización" frontend/src --include=*.test.tsx`) y cambia cada aserción por el texto nuevo. Son pruebas que fijaban la pista vieja: el informe las lista.
- [ ] **Step A4:** `npx vitest run src/components src/modules/docs src/modules/patch`. Resultado esperado: PASS.
- [ ] **Step A5: Commit.** `git add frontend/src/components frontend/src/i18n/en/components.ts frontend/src/i18n/es/components.ts frontend/src/modules/docs frontend/src/modules/patch && git commit -m "fix(console): the every-client toggle's hint names the form's client, not the internal one"`

### Task 10: Consola, grupo A — el cliente de cada fila nueva en ipam, devices, agent-releases, enrollment, credentials, agent-push y backup

Diseño: §3.1 (una sola regla), §3.2 (sedes), §3.3 (selectores dentro del cliente de la fila), §3.4 (columna
Cliente). Corre en paralelo con la Task 11, en su propia worktree, después de la Task 9. Toca solo las
carpetas de módulo de abajo, sus catálogos `i18n/{en,es}/<módulo>.ts` y sus pruebas. **No toca
`lib/api.ts`, `lib/internalClient.ts` (lo borra la Task 12) ni ningún archivo compartido.**

**Files:**
- Modify: `frontend/src/modules/ipam/api.ts:31-36` (imports), `:143-172` (`useIpamClient`, `namingClient`),
  `:182-198` (VRF), `:200-221` (`useIpamSites`), `:231-243` (VLAN), `:292-305` (subnet), `:463-474`
  (address), `:578-591` (scan authorization), `:675-723` (import)
- Modify: `frontend/src/modules/ipam/RegistryPage.tsx` (entero: VRF, sedes, VLAN), `SubnetTreePage.tsx:1-30,
  126-160, 292-316, 392-410, 563-727`, `ScanAuthorizationsPage.tsx:1-20, 145-160, 300-470`,
  `ImportPage.tsx:1-30, 80-140, 150-170, 262-300`, `ipamState.ts:1451-1462` (`PreviewedFile`)
- Modify: `frontend/src/modules/devices/GroupsPage.tsx:95, 478-485, 523-546, 634-645, 680-700, 776-800, 931-990`,
  `AddAssetDialog.tsx` (entero), `DevicesPage.tsx:51, 912-920`
- Modify: `frontend/src/modules/agent-releases/AgentReleasesPage.tsx:14, 180-217, 310-318, 610-617`,
  `ManagedDeploymentDialog.tsx:47, 128-136`
- Modify: `frontend/src/modules/enrollment/EnrollmentPage.tsx:124, 271, 402-424, 460-475, 752-795`
- Modify: `frontend/src/modules/credentials/CredentialsPage.tsx:17, 135-136, 310-330, 523-590, 640-700`
- Modify: `frontend/src/modules/agent-push/PushAuthorizationsPage.tsx:11, 136-150, 181-210, 259-400`,
  `PushInstallFlow.tsx:66-80, 160-175`, `api.ts:76-81` (comentario)
- Modify: `frontend/src/modules/backup/BackupPolicyEditor.tsx:35, 205-260, 291-372`, `RepositoriesPage.tsx:29,
  150-240`, `pickerOptions.ts:31-39`, `api.ts:60-66` (comentario)
- Modify: `frontend/src/i18n/en/{ipam,devices,agentReleases,enrollment,credentials,agentPush,backup}.ts` y sus
  pares en `frontend/src/i18n/es/`
- Modify (pruebas corregidas): `ipam/__tests__/clients.test.tsx:15-23, 26-31, 104-111, 122-216, 287-311, 472-517`,
  `ipam/ipam.es.test.tsx:1219`, `devices/GroupsPage.test.tsx:113-140, 389-402, 822-827`,
  `devices/AddAssetDialog.test.tsx:26-52`, `devices/devices.es.test.tsx:267`,
  `agent-releases/AgentReleasesPage.test.tsx:59-92, 414-419, 655-684`, `enrollment/EnrollmentPage.test.tsx:170-180, 395-401`,
  `credentials/CredentialsPage.test.tsx:43-45, 703-723`, `agent-push/__tests__/pushPages.test.tsx:266-295`,
  `backup/__tests__/backup.test.tsx:881-900, 1030-1040, 1301-1320`
- Modify (solo el arnés del selector, Step 1): todo `*.test.tsx` bajo `frontend/src/modules/{ipam,devices,agent-releases,enrollment,credentials,agent-push,backup}/`
- Create: `frontend/src/modules/ipam/__tests__/clientSelection.test.tsx`,
  `frontend/src/modules/devices/clientSelection.test.tsx`,
  `frontend/src/modules/agent-releases/clientSelection.test.tsx`,
  `frontend/src/modules/enrollment/clientSelection.test.tsx`,
  `frontend/src/modules/credentials/clientSelection.test.tsx`,
  `frontend/src/modules/agent-push/__tests__/clientSelection.test.tsx`,
  `frontend/src/modules/backup/__tests__/clientSelection.test.tsx`

**Interfaces:**
- Consumes (Task 7): `useClientScope(): { selected: string | null; select(id: string | null): void; clients: MeClient[]; ready: boolean }`
  de `frontend/src/lib/clientScope.tsx`; `MeClient` de `frontend/src/lib/api.ts`.
- Consumes (Task 8): `useClientName(): (clientId: string) => string` (`modules/settings/api.ts`), que lee
  `/me.clients` y nombra un id desconocido con `t("lib.clients.everyClient")`.
- Consumes (Task 9), de `frontend/src/lib/clientSelection.ts`:
  - `useNewRowClient(code: string, parentClientId?: string | null): { clientId: string | null; needsField: boolean; options: MeClient[] }`.
    Esta tarea lo usa así, y es lo que asume: con `parentClientId` de tipo `string`, `{ clientId: parentClientId, needsField: false }`;
    sin padre y con un cliente elegido arriba, `{ clientId: selected, needsField: false }`; sin padre, con
    «Todos mis clientes» y `/me` respondido, `{ clientId: null, needsField: true }`; mientras `/me` no ha
    respondido (`ready === false`), `{ clientId: null, needsField: false }`. `options` son los clientes de
    `/me.clients` que tienen `code`, sin el `unassigned`. Esta tarea pasa como padre solo un `string` o
    `undefined`, nunca `null`.
  - `requireKnown(id: string | null): string`, que lanza `ClientNotChosen`; `loadingUntilClient(query, client)`.
- Consumes (Task 9): `ClientField({ code, value, onChange })` (`components/ClientField.tsx`), un `<select>` con
  la etiqueta «Client» / «Cliente» cuyas opciones tienen por `value` el id del cliente;
  `SiteField({ clientId, value, onChange })` (`components/SiteField.tsx`), un `<select>` con la etiqueta
  «Site» / «Sede» cuyas opciones son las sedes no archivadas de `clientId` (`GET /clients/{id}/sites`, vía
  `useClientSites`), y que con una sola sede la elige él mismo llamando a `onChange`. Las pruebas de esta
  tarea dependen de esas dos etiquetas y de esa elección automática.
- Produces: `useIpamSiteDirectory(): { sites: ClientSiteOut[]; siteName(siteId: string | null): string; isLoading: boolean; error: unknown }`
  (`ipam/api.ts`). `useIpamSites(of: string | null)` pierde el argumento opcional. `useIpamClient` desaparece.
- Produces: las mutaciones de creación de IPAM reciben el cliente en el cuerpo (`client_id: string | null`); la de
  la importación recibe `clientId: string | null`. `PreviewedFile` gana `clientId: string | null`.
- Produces: `AddAssetDialog({ open, onClose })` pierde la prop `siteId`.

**El arnés de las pruebas.** Las pruebas de módulo simulan `useApi` con `vi.mock("…/lib/ApiContext")`. Esta
tarea simula también el selector, con el mismo patrón, para no depender de cómo monte la Task 7 su
proveedor: cada archivo de prueba de estos siete módulos añade, junto a su `vi.mock` de `ApiContext`:

```tsx
import type { MeClient } from "<ruta>/lib/api";

const mockClientScope = vi.fn();
vi.mock("<ruta>/lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

/** The internal client every test here works in unless it chooses otherwise. */
const SCOPE_INTERNAL: MeClient = {
  id: "<el org_id del ME de ese archivo>",
  name: "Novieri",
  kind: "internal",
  enabled_modules: [],
  permissions: [],
};

/** The selector (Task 7): `selected` is the client chosen at the top, `null` for every client. */
function selectClient(selected: string | null, clients: MeClient[] = [SCOPE_INTERNAL]): void {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients, ready: true });
}

beforeEach(() => selectClient(SCOPE_INTERNAL.id));
```

`<ruta>` es `../..` para un archivo en `modules/<m>/` y `../../..` para uno en `modules/<m>/__tests__/`.
Con el cliente interno elegido arriba, la regla 2 del §3.1 da el mismo cliente que hasta hoy daba
`internalClientId(me)`, así que las pruebas que solo afirman `client_id: "o1"` siguen valiendo sin tocar su
afirmación; lo que se corrige es su nombre y su comentario, que hablan del cliente interno «hasta la fase
3». Las pruebas nuevas eligen otro cliente o «Todos mis clientes» con `selectClient(...)`.

Las pruebas que miran opciones del campo Cliente o la columna Cliente ponen los mismos clientes en
`/me.clients` (lo que lee `useClientName`, Task 8) y en el selector simulado (lo que lee
`useNewRowClient`, Task 9).

**Decisiones del plan de esta tarea** (también en el informe al coordinador):
- **D10.1.** IPAM deja de tener un cliente a nivel de hook (`useIpamClient`): cada formulario decide el
  cliente con `useNewRowClient` y lo pone en el cuerpo de la mutación, que lo descarta cuando un padre (VRF o
  sede) decide. Así un mismo hook sirve a un formulario con el campo Cliente y a otro sin él.
- **D10.2.** Con «Todos mis clientes», las listas de IPAM nombran las sedes leyendo las de cada cliente que
  tiene una VRF o una VLAN a la vista (`useIpamSiteDirectory`, una lectura cacheada por cliente). Sin eso,
  la columna de sede mostraría ids.
- **D10.3.** La importación de IPAM conserva su VRF por defecto (la `global` del cliente elegido, o la primera
  de la lista): la VRF es el padre y se ve en pantalla con su cliente antes de «Previsualizar». El campo
  Cliente solo aparece cuando no hay ninguna VRF listada.
- **D10.4.** En `/agent-packages`, un único bloque «Dónde van estas máquinas» (campo Cliente si hace falta y
  campo Sede) decide la sede de la instrucción de instalación, del enlace de instalación y del despliegue
  gestionado. Su código es `enrollment.manage`, el de las rutas que acuñan tokens.
- **D10.5.** La lista de tokens de inscripción no tiene columna Cliente: `EnrollmentTokenListItem`
  (`enrollment/types.ts:12-31`) no trae ni cliente ni sede, y esta fase no toca esa API.
- **D10.6.** El aviso `repositoryPickerEmptyHintElsewhere` de respaldos cambia de sentido: ya no es «la consola
  no puede elegir cliente», sino «no puedes crear repositorios en el cliente de esta política» (el cliente
  no está en `options` de `backup.policy.manage`). Se conserva la función y su prueba (`backup.test.tsx:1813`).
- **D10.7.** `AddAssetDialog` monta su formulario solo mientras está abierto (`open ? <AddAssetForm/> : null`).
  Así las pruebas de `DevicesPage` que no lo abren no dependen del selector.
- **D10.8.** El flujo de instalación por push (`PushInstallFlow`) solo ofrece credenciales del cliente de las
  máquinas marcadas; si son de varios clientes, no ofrece ninguna y lo dice (§3.3).
- **D10.9.** Las columnas Cliente usan una clave por módulo (`ipam.fields.client`, `devices.groups.colClient`,
  `credentials.list.client`, `agentPush.authorizations.colClient`, `backup.policies.colClient`,
  `backup.repositories.colClient`), todas «Client» / «Cliente», para que las Tasks 10 y 11 no choquen en un
  catálogo compartido. Una fila global (`client_id` nulo) dice `t("lib.clients.everyClient")` (Task 8).
- **D10.10.** No se leen `?client=` ni `?site=` de la URL: es el §4.4, de la Task 13, que construye esos
  enlaces. Los estados que tendría que inicializar son `pickedClient` y `siteId` de `AgentReleasesPage` y
  de los formularios de IPAM, con esos nombres.

---

- [ ] **Step 1: El arnés del selector en las pruebas existentes de los siete módulos**

En cada `*.test.tsx` bajo `frontend/src/modules/{ipam,devices,agent-releases,enrollment,credentials,agent-push,backup}/`,
añadir el bloque del arnés de arriba justo después de su `vi.mock` de `ApiContext`, con `SCOPE_INTERNAL.id`
igual al `org_id` del `/me` de ese archivo: `"o1"` en ipam, credentials, agent-push y backup; `"o"` en
agent-releases y enrollment; `ORG` (`"00000000-0000-4000-8000-00000000000a"`) en
`devices/GroupsPage.test.tsx`; `"o1"` en el resto de devices. Si el archivo no importaba `beforeEach` de
`vitest`, se añade al import.

Comprobar que nada cambia todavía (el módulo `clientScope` existe desde la Task 7 y nadie lo llama aún
desde estas carpetas):

```bash
cd frontend && npx vitest run src/modules/ipam src/modules/devices src/modules/agent-releases src/modules/enrollment src/modules/credentials src/modules/agent-push src/modules/backup
```

Esperado: PASS, el mismo número de pruebas que antes.

#### IPAM

- [ ] **Step 2: Las pruebas que fallan, IPAM**

Crear `frontend/src/modules/ipam/__tests__/clientSelection.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, ClientSiteOut, MeClient } from "../../../lib/api";
import { ImportPage } from "../ImportPage";
import { RegistryPage } from "../RegistryPage";
import { ScanAuthorizationsPage } from "../ScanAuthorizationsPage";
import { SubnetTreePage } from "../SubnetTreePage";
import type { ScopeOut, SubnetOut, VlanOut, VrfOut } from "../types";

// MSP clients phase 3, design §3.1-§3.4: a new IPAM row takes its client from
// its parent (a VRF, or for a VLAN a site), else from the client chosen at the
// top, else from a required Client field; pickers list the row's client's
// things; and with every client chosen the lists say whose each row is.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

const CODES = ["ipam.read", "ipam.assign", "ipam.manage", "ipam.scope.manage"];
const INTERNAL: MeClient = { id: "o1", name: "Novieri", kind: "internal", enabled_modules: [], permissions: CODES };
const ACME: MeClient = { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: CODES };
const BETA: MeClient = { id: "c-beta", name: "Beta", kind: "customer", enabled_modules: [], permissions: ["ipam.read"] };
const CLIENTS = [INTERNAL, ACME, BETA];

function selectClient(selected: string | null): void {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients: CLIENTS, ready: true });
}

const ME = {
  user_id: "u1",
  org_id: "o1",
  email: "a@b.c",
  display_name: "A",
  permissions: CODES,
  permissions_everywhere: ["ipam.read"],
  clients: CLIENTS,
};

function site(id: string, clientId: string, name: string): ClientSiteOut {
  return { id, client_id: clientId, name, address: "", description: "", egress_networks: [], archived_at: null, device_count: 0 };
}
const OWN_SITE = site("site-o", "o1", "Medellín");
const ACME_SITE = site("site-a", "c-acme", "Acme HQ");
const OWN_VRF: VrfOut = { id: "vrf-o", client_id: "o1", name: "lab", rd: null, description: "" };
const ACME_VRF: VrfOut = { id: "vrf-a", client_id: "c-acme", name: "acme-lan", rd: null, description: "" };

function subnet(overrides: Partial<SubnetOut> = {}): SubnetOut {
  return {
    client_id: "o1", id: "s1", vrf_id: "vrf-o", prefix: "10.4.7.0/24", kind: "assignable", name: "",
    description: "", site_id: null, vlan_id: null, gateway: null, dns_servers: [], parent_id: null,
    sweep_enabled: false, sweep_device_id: null, sweep_interval_hours: null, last_swept_at: null,
    ...overrides,
  };
}

function stubApi(overrides: Partial<Record<keyof ApiClient, unknown>> = {}): Record<string, ReturnType<typeof vi.fn>> {
  const api = {
    me: vi.fn().mockResolvedValue(ME),
    listIpamVrfs: vi.fn().mockResolvedValue([OWN_VRF, ACME_VRF]),
    listClientSites: vi.fn((clientId: string) =>
      Promise.resolve(clientId === "c-acme" ? [ACME_SITE] : clientId === "o1" ? [OWN_SITE] : []),
    ),
    listIpamVlans: vi.fn().mockResolvedValue([]),
    listIpamSubnets: vi.fn().mockResolvedValue([]),
    listIpamDrift: vi.fn().mockResolvedValue([]),
    listIpamOrphans: vi.fn().mockResolvedValue([]),
    listIpamScanAuthorizations: vi.fn().mockResolvedValue([]),
    listIpamSweepRuns: vi.fn().mockResolvedValue([]),
    listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    listSiteServers: vi.fn().mockResolvedValue([]),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api);
  return api as Record<string, ReturnType<typeof vi.fn>>;
}

function renderAt(node: React.ReactNode, entry: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function bodyOf(mock: ReturnType<typeof vi.fn>): Record<string, unknown> {
  return mock.mock.calls[0]?.[0] as Record<string, unknown>;
}

beforeEach(() => selectClient("o1"));

describe("a VRF goes to the client chosen at the top, or the one the form names", () => {
  it("files it under the selected client without asking", async () => {
    selectClient("c-acme");
    const createIpamVrf = vi.fn().mockResolvedValue(ACME_VRF);
    stubApi({ createIpamVrf });
    const user = userEvent.setup();
    renderAt(<RegistryPage />, "/ipam/registry");

    await user.type((await screen.findAllByLabelText("Name"))[0]!, "lan");
    expect(screen.queryByLabelText("Client")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Add VRF" }));

    await waitFor(() => expect(createIpamVrf).toHaveBeenCalled());
    expect(bodyOf(createIpamVrf)).toMatchObject({ name: "lan", client_id: "c-acme" });
  });

  it("with every client chosen, asks for the client, offers only those that may, and sends nothing without one", async () => {
    selectClient(null);
    const createIpamVrf = vi.fn().mockResolvedValue(ACME_VRF);
    stubApi({ createIpamVrf });
    const user = userEvent.setup();
    renderAt(<RegistryPage />, "/ipam/registry");

    const field = (await screen.findAllByLabelText("Client"))[0]!;
    // Beta lacks `ipam.manage`; "Unassigned" is never offered (design §3.1, C10).
    expect(within(field).queryByRole("option", { name: "Beta" })).toBeNull();
    await user.type(screen.getAllByLabelText("Name")[0]!, "lan");
    await user.click(screen.getByRole("button", { name: "Add VRF" }));
    expect(createIpamVrf).not.toHaveBeenCalled();

    await user.selectOptions(field, "c-acme");
    await user.click(screen.getByRole("button", { name: "Add VRF" }));
    await waitFor(() => expect(createIpamVrf).toHaveBeenCalledTimes(1));
    expect(bodyOf(createIpamVrf)).toMatchObject({ name: "lan", client_id: "c-acme" });
  });
});

describe("a VLAN: its site decides, and its site picker is its client's", () => {
  it("lists the chosen client's sites and names no client when a site is picked", async () => {
    selectClient(null);
    const createIpamVlan = vi.fn().mockResolvedValue({} as VlanOut);
    const api = stubApi({ createIpamVlan });
    const user = userEvent.setup();
    renderAt(<RegistryPage />, "/ipam/registry");

    const fields = await screen.findAllByLabelText("Client");
    await user.selectOptions(fields[fields.length - 1]!, "c-acme");
    await waitFor(() => expect(api.listClientSites).toHaveBeenCalledWith("c-acme"));
    const picker = screen.getByLabelText("Site");
    await within(picker).findByRole("option", { name: "Acme HQ" });
    expect(within(picker).queryByRole("option", { name: "Medellín" })).toBeNull();

    await user.type(screen.getByLabelText("VID"), "40");
    await user.selectOptions(picker, "site-a");
    await user.click(screen.getByRole("button", { name: "Add VLAN" }));
    await waitFor(() => expect(createIpamVlan).toHaveBeenCalled());
    const body = bodyOf(createIpamVlan);
    expect(body).toMatchObject({ vid: 40, site_id: "site-a" });
    expect(body).not.toHaveProperty("client_id");
  });

  it("files a VLAN with no site under the chosen client", async () => {
    selectClient(null);
    const createIpamVlan = vi.fn().mockResolvedValue({} as VlanOut);
    stubApi({ createIpamVlan });
    const user = userEvent.setup();
    renderAt(<RegistryPage />, "/ipam/registry");

    const fields = await screen.findAllByLabelText("Client");
    await user.selectOptions(fields[fields.length - 1]!, "c-acme");
    await user.type(screen.getByLabelText("VID"), "10");
    await user.click(screen.getByRole("button", { name: "Add VLAN" }));
    await waitFor(() => expect(createIpamVlan).toHaveBeenCalled());
    expect(bodyOf(createIpamVlan)).toMatchObject({ vid: 10, site_id: null, client_id: "c-acme" });
  });
});

describe("with every client chosen, the lists say whose each row is", () => {
  it("shows a Client column on the VLAN list, named from `/me`, and names each VLAN's site", async () => {
    selectClient(null);
    stubApi({
      listIpamVlans: vi.fn().mockResolvedValue([
        { id: "vl-a", client_id: "c-acme", vid: 20, site_id: "site-a", name: "acme", description: "" },
      ]),
    });
    renderAt(<RegistryPage />, "/ipam/registry");

    const table = (await screen.findByText("VLAN 20")).closest("table") as HTMLElement;
    expect(within(table).getByRole("columnheader", { name: "Client" })).toBeInTheDocument();
    const row = (await within(table).findByText("VLAN 20")).closest("tr") as HTMLElement;
    expect(within(row).getByText("Acme")).toBeInTheDocument();
    expect(await within(row).findByText("Acme HQ")).toBeInTheDocument();
  });

  it("hides the Client column once a client is chosen at the top", async () => {
    selectClient("c-acme");
    stubApi({
      listIpamVlans: vi.fn().mockResolvedValue([
        { id: "vl-a", client_id: "c-acme", vid: 20, site_id: null, name: "acme", description: "" },
      ]),
    });
    renderAt(<RegistryPage />, "/ipam/registry");
    const table = (await screen.findByText("VLAN 20")).closest("table") as HTMLElement;
    expect(within(table).queryByRole("columnheader", { name: "Client" })).toBeNull();
  });

  it("shows the Client column on the subnet tree and on the scan authorizations", async () => {
    selectClient(null);
    const scope: ScopeOut = {
      client_id: "c-acme", id: "sc", vrf_id: "vrf-a", prefix: "10.9.0.0/24", note: "", authorized_by: "u1",
      authorized_at: new Date().toISOString(), expires_at: null, revoked_at: null,
    };
    stubApi({
      listIpamSubnets: vi.fn().mockResolvedValue([subnet({ client_id: "c-acme", vrf_id: "vrf-a" })]),
      listIpamScanAuthorizations: vi.fn().mockResolvedValue([scope]),
    });
    const { unmount } = renderAt(<SubnetTreePage />, "/ipam");
    const tree = (await screen.findByText("10.4.7.0/24")).closest("table") as HTMLElement;
    expect(within(tree).getByRole("columnheader", { name: "Client" })).toBeInTheDocument();
    expect(within(tree).getByText("Acme")).toBeInTheDocument();
    unmount();

    renderAt(<ScanAuthorizationsPage />, "/ipam/scan-authorizations");
    const grants = (await screen.findByText("10.9.0.0/24")).closest("table") as HTMLElement;
    expect(within(grants).getByRole("columnheader", { name: "Client" })).toBeInTheDocument();
    expect(within(grants).getByText("Acme")).toBeInTheDocument();
  });
});

describe("a subnet and a scan authorization: the VRF decides, else the client", () => {
  it("asks for the client only while no VRF is chosen", async () => {
    selectClient(null);
    const createIpamSubnet = vi.fn().mockResolvedValue(subnet());
    stubApi({ createIpamSubnet });
    const user = userEvent.setup();
    renderAt(<SubnetTreePage />, "/ipam");
    await user.click(await screen.findByRole("button", { name: "New subnet" }));
    const dialog = await screen.findByRole("dialog");

    await user.type(within(dialog).getByPlaceholderText("10.4.7.0/24"), "10.4.7.0/24");
    await user.selectOptions(within(dialog).getByLabelText("Client"), "c-acme");
    await user.click(within(dialog).getByRole("button", { name: "Register" }));
    await waitFor(() => expect(createIpamSubnet).toHaveBeenCalledTimes(1));
    expect(bodyOf(createIpamSubnet)).toMatchObject({ vrf_id: null, client_id: "c-acme" });

    await user.selectOptions(within(dialog).getByLabelText("VRF"), "vrf-o");
    expect(within(dialog).queryByLabelText("Client")).toBeNull();
  });

  it("files an authorization with no VRF under the client chosen at the top", async () => {
    selectClient("c-acme");
    const createIpamScanAuthorization = vi.fn().mockResolvedValue({});
    stubApi({ createIpamScanAuthorization });
    const user = userEvent.setup();
    renderAt(<ScanAuthorizationsPage />, "/ipam/scan-authorizations");
    await user.click(await screen.findByRole("button", { name: "Authorize a subnet" }));
    await user.type(await screen.findByLabelText("Prefix"), "10.4.7.0/24");
    expect(screen.queryByLabelText("Client")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Authorize" }));
    await waitFor(() => expect(createIpamScanAuthorization).toHaveBeenCalled());
    expect(bodyOf(createIpamScanAuthorization)).toMatchObject({ prefix: "10.4.7.0/24", vrf_id: null, client_id: "c-acme" });
  });
});

describe("an import with no VRF listed names a client", () => {
  const PREVIEW = {
    digest: "a".repeat(64), total_rows: 1, creates: 1, updates: 0, conflicts: 0, errors: 0,
    rows: [{ line: 2, action: "create", address: "10.4.7.9", error: "", detail: {} }],
  };

  it("asks for it with every client chosen, and a new choice clears the preview", async () => {
    selectClient(null);
    const previewIpamImport = vi.fn().mockResolvedValue(PREVIEW);
    const applyIpamImport = vi.fn().mockResolvedValue({ ...PREVIEW, applied: 1 });
    stubApi({ previewIpamImport, applyIpamImport, listIpamVrfs: vi.fn().mockResolvedValue([]) });
    const user = userEvent.setup();
    renderAt(<ImportPage />, "/ipam/import");

    const input = (await screen.findByLabelText("CSV file")) as HTMLInputElement;
    await user.upload(input, new File(["address\n10.4.7.9\n"], "plan.csv", { type: "text/csv" }));
    await user.selectOptions(await screen.findByLabelText("address"), "address");
    await user.selectOptions(screen.getByLabelText("Client"), "c-acme");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Apply" })).not.toBeDisabled());
    expect(previewIpamImport.mock.calls[0]?.[2]).toEqual({ clientId: "c-acme" });

    await user.selectOptions(screen.getByLabelText("Client"), "o1");
    expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
  });
});
```

- [ ] **Step 3: Las pruebas existentes que fijaban el cliente interno, corregidas**

En `frontend/src/modules/ipam/__tests__/clients.test.tsx` (con el arnés del Step 1, `SCOPE_INTERNAL.id = "o1"`):

| Línea | Antes | Después |
|---|---|---|
| 15-23 (comentario) | «Until phase 3's selector that client is the internal one, `/me`'s `org_id` (`lib/internalClient.ts`), and nothing is sent before `/me` has answered.» | «That client is the one chosen at the top, or the one the form's Client field names (`lib/clientSelection.ts`, phase 3), and nothing is sent before it is known.» |
| 122 | `it("files a new VRF under the internal client", …)` | `it("files a new VRF under the client chosen at the top", …)` |
| 132 | `// PHASE 3: the selected client; the internal one, `/me`'s `org_id`, until then.` | `// The selected client (here the internal one, `o1`).` |
| 158 | `it("files a VLAN with no site under the internal client", …)` | `it("files a VLAN with no site under the client chosen at the top", …)` |
| 192 | `it("files a subnet with no VRF under the internal client's global VRF", …)` | `it("files a subnet with no VRF under the selected client's global VRF", …)` |
| 202 | `it("files an authorization with no VRF under the internal client", …)` | `it("files an authorization with no VRF under the client chosen at the top", …)` |
| 287-311 | `stubApi({ createIpamSubnet, me: vi.fn(() => new Promise(() => {})) })` y `expect(await screen.findByText(/Your account has not loaded yet/)).toBeInTheDocument();` | `selectClient(null); stubApi({ createIpamSubnet });` y `expect(await screen.findByText(new ClientNotChosen().message)).toBeInTheDocument();` — el componente `Register` llama a `create.mutate({ prefix: "10.4.7.0/24", kind: "assignable", vrf_id: null, client_id: null })`, el cuerpo de un formulario cuyo campo Cliente sigue vacío. Import: `import { ClientNotChosen } from "../../../lib/clientSelection";`. Nombre: `it("sends nothing while the client is not chosen where it must be named, and says why", …)` |
| 486 | `it("starts on the internal client's global VRF, and another choice clears the preview", …)` con `listIpamVrfs: [X_GLOBAL, OWN_GLOBAL]` y `expect(previewIpamImport.mock.calls[0]?.[2]).toEqual({ vrfId: "vrf-o" })` | `it("starts on the selected client's global VRF, and another choice clears the preview", …)`, con `selectClient("o1")` explícito al principio y la misma afirmación (`vrf-o` es la `global` de `o1`) |
| 509-517 | `it("names the internal client only when no VRF is listed", …)` y su comentario `// PHASE 3: …` | `it("names the selected client only when no VRF is listed", …)` y `// The client chosen at the top: here the internal one, o1.`; la afirmación `toEqual({ clientId: "o1" })` no cambia |

En `frontend/src/modules/ipam/ipam.es.test.tsx:1219`: el comentario `// PHASE 3: the internal client, `/me`'s `org_id`, until the selector exists.` pasa a `// El cliente elegido arriba: aquí el interno, `o1` (`selectClient` del arnés).`

- [ ] **Step 4: Ejecutar y ver que fallan**

```bash
cd frontend && npx vitest run src/modules/ipam
```

Esperado: FAIL. `clientSelection.test.tsx` falla en todos sus casos (no hay campo «Client», no hay columna
«Client», los cuerpos llevan `client_id: "o1"`); en `clients.test.tsx` falla el caso de la línea 287 (el
mensaje que busca es el de `ClientNotChosen` y el formulario sigue leyendo `/me`).

- [ ] **Step 5: Implementar en `ipam/api.ts`**

Imports (líneas 31-36):

```ts
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiError, ClientSiteOut, Device, DevicePage, Me, Page } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useClientScope } from "../../lib/clientScope";
import { loadingUntilClient, requireKnown } from "../../lib/clientSelection";
```

Sustituir las líneas 143-172 (la sección «The client», `useIpamClient` y `namingClient`) por:

```ts
// --------------------------------------------------------------------------
// The client — every IPAM row is one's (MSP clients phase 2)
// --------------------------------------------------------------------------

/**
 * A create body names its client only when nothing above it does -- no VRF,
 * or for a VLAN no site -- because the server takes the client from the
 * parent and refuses a body that contradicts it. `parent` is that VRF or
 * site.
 *
 * The FORM decides the client (`lib/clientSelection.ts`, phase 3): the one
 * chosen at the top, or the one its Client field names, put in `client_id`.
 * Under a parent it is dropped; without one it must be known, and
 * `requireKnown` throws `ClientNotChosen` inside the mutation, so NOTHING is
 * sent.
 */
function namingClient<B extends { client_id?: string | null }>(
  body: B,
  parent: string | null | undefined,
): B {
  const { client_id: client = null, ...rest } = body;
  return (parent ? rest : { ...rest, client_id: requireKnown(client) }) as unknown as B;
}
```

Sustituir `useCreateIpamVrfMutation` (182-198) por:

```ts
export function useCreateIpamVrfMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<VrfOut, ApiError, Omit<VrfCreate, "client_id"> & { client_id: string | null }>({
    // Nothing above a VRF names its client: the form's does.
    mutationFn: ({ client_id: client, ...body }) =>
      api.createIpamVrf({ ...body, client_id: requireKnown(client) }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ipam-vrfs"] });
    },
  });
}
```

Sustituir `useIpamSites` (200-221) por `useIpamSites` y `useIpamSiteDirectory`:

```ts
/**
 * The sites a VLAN or a subnet may name: its client's own (`GET
 * /clients/{id}/sites`, phase 1). IPAM keeps no sites of its own since
 * migration 0240, and creates none -- a site is a client's office. `of` is the
 * row's client, whose sites alone it may name. LOADING until it is known, not
 * empty: `null` waits (the row, the VRF that decides it, or the form's Client
 * field has not said yet).
 */
export function useIpamSites(of: string | null) {
  const api = useApi();
  return loadingUntilClient(
    useQuery<ClientSiteOut[], ApiError>({
      queryKey: ["client-sites", of],
      queryFn: () => api.listClientSites(requireKnown(of)),
      enabled: of != null,
      ...NO_RETRY,
    }),
    of,
  );
}

/**
 * Every site a LIST on these pages may name, and its name (MSP clients phase
 * 3, design §3.4). With a client chosen at the top, that client's sites. With
 * every client, the sites of each client that has a VRF or a VLAN in view:
 * one cached read per client, under the key the pickers above use, never one
 * per row. A site no read has named (yet, or at all) shows its id, as before.
 */
export function useIpamSiteDirectory(): {
  sites: ClientSiteOut[];
  siteName: (siteId: string | null) => string;
  isLoading: boolean;
  error: unknown;
} {
  const api = useApi();
  const { selected } = useClientScope();
  const vrfs = useIpamVrfs();
  const vlans = useIpamVlans();
  const clientIds =
    selected !== null
      ? [selected]
      : [...new Set([...(vrfs.data ?? []), ...(vlans.data ?? [])].map((row) => row.client_id))].sort();
  const reads = useQueries({
    queries: clientIds.map((id) => ({
      queryKey: ["client-sites", id],
      queryFn: () => api.listClientSites(id),
      ...NO_RETRY,
    })),
  });
  const sites = reads.flatMap((read) => read.data ?? []);
  const names = new Map(sites.map((site) => [site.id, site.name]));
  return {
    sites,
    siteName: (siteId) => (siteId === null ? "" : (names.get(siteId) ?? siteId)),
    isLoading: reads.some((read) => read.isLoading),
    error: reads.find((read) => read.error != null)?.error ?? null,
  };
}
```

Sustituir el cuerpo de `useCreateIpamVlanMutation` (231-243) por:

```ts
export function useCreateIpamVlanMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<VlanOut, ApiError, VlanCreate>({
    // With a site, the site's client is the VLAN's; with none, the form's.
    mutationFn: (body) => api.createIpamVlan(namingClient(body, body.site_id)),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ipam-vlans"] });
    },
  });
}
```

En `useCreateIpamSubnetMutation` (292-305), `useCreateIpamAddressMutation` (463-474) y
`useCreateIpamScanAuthorizationMutation` (578-591): borrar `const client = useIpamClient();`, borrar los
comentarios `// PHASE 3: …` y llamar `namingClient(body, body.vrf_id)` (dos argumentos). El comentario
nuevo, igual en las tres:

```ts
    // With a VRF, the VRF's client is the row's; with none, the form's
    // (`client_id`), whose `global` VRF the row goes into.
```

Sustituir 675-723 (desde el comentario «AN IMPORT NAMES WHERE IT GOES» hasta el final de
`useApplyIpamImportMutation`) por:

```ts
//
// AN IMPORT NAMES WHERE IT GOES: the VRF the page chose -- its client is the
// import's, which is how a technician limited to one client imports into it --
// or, with none listed, a client's `global` VRF: the one chosen at the top, or
// the one the page's Client field names. The server refuses neither.

/** The target of an import: the VRF chosen, or with none a client's `global` VRF. */
function importTarget(vrfId: string | null, clientId: string | null): ImportTarget {
  return vrfId ? { vrfId } : { clientId: requireKnown(clientId) };
}

export function usePreviewIpamImportMutation() {
  const api = useApi();
  return useMutation<
    ImportPreviewOut,
    ApiError,
    { file: File; mapping: Record<string, string>; vrfId: string | null; clientId: string | null }
  >({
    mutationFn: ({ file, mapping, vrfId, clientId }) =>
      api.previewIpamImport(file, mapping, importTarget(vrfId, clientId)),
  });
}

export function useApplyIpamImportMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<
    ImportResultOut,
    ApiError,
    {
      file: File;
      mapping: Record<string, string>;
      digest: string;
      vrfId: string | null;
      clientId: string | null;
    }
  >({
    // Into the same target the preview read: the page clears the preview
    // whenever the VRF or the client changes.
    mutationFn: ({ file, mapping, digest, vrfId, clientId }) =>
      api.applyIpamImport(file, mapping, digest, importTarget(vrfId, clientId)),
    onSuccess: () => {
      invalidateAddressEffects(queryClient);
    },
  });
}
```

`Me` sigue importado: `useIpamPermissions` y `useUserTimezone` lo usan.

- [ ] **Step 6: Implementar `RegistryPage.tsx`**

Imports: añadir

```tsx
import { ClientField } from "../../components/ClientField";
import { useClientScope } from "../../lib/clientScope";
import { useNewRowClient } from "../../lib/clientSelection";
```

cambiar el import de `./api` a `useCreateIpamVlanMutation, useCreateIpamVrfMutation, useIpamPermissions,
useIpamSiteDirectory, useIpamSites, useIpamVlans, useIpamVrfs`, y añadir `IPAM_MANAGE` desde `./types`.

Comentario de cabecera, líneas 27-28: «EVERY ROW IS ONE CLIENT'S (MSP clients phase 2): this console files them
under the internal client until phase 3's selector (`api.ts::useIpamClient`).» pasa a «EVERY ROW IS ONE
CLIENT'S (MSP clients phase 2): a new one goes to its parent's client, else to the client chosen at the top,
else to the one its form's Client field names (phase 3, `lib/clientSelection.ts`).»

`VrfPanel`: tras `const create = useCreateIpamVrfMutation();` añadir

```tsx
  // No parent: the client chosen at the top, or the Client field's (§3.1).
  const target = useNewRowClient(IPAM_MANAGE);
  const [pickedClient, setPickedClient] = useState<string | null>(null);
  const vrfClient = target.needsField ? pickedClient : target.clientId;
```

dentro de su `<div className="form-grid">`, antes del campo Nombre:

```tsx
            {target.needsField ? (
              <ClientField code={IPAM_MANAGE} value={pickedClient} onChange={setPickedClient} />
            ) : null}
```

y la llamada pasa a `create.mutate({ name, rd: rd || null, client_id: vrfClient }, …)`.

`SitePanel` entero:

```tsx
/** The sites in view, read-only: IPAM names them and no longer creates them. */
function SitePanel() {
  const t = useT();
  const { selected } = useClientScope();
  const clientName = useClientName();
  const directory = useIpamSiteDirectory();

  return (
    <Panel
      title={t("ipam.registry.sitesTitle")}
      subtitle={t("ipam.registry.sitesSubtitle")}
      bare
    >
      <QueryState
        loading={directory.isLoading}
        error={directory.error}
        empty={directory.sites.length === 0}
        emptyLabel={t("ipam.registry.sitesEmpty")}
        emptyIcon="building"
      >
        <Table>
          <thead>
            <tr>
              <Th>{t("ipam.fields.name")}</Th>
            </tr>
          </thead>
          <tbody>
            {/* One column: in the two-column grid a second column clipped at
                the panel edge at 1440, so the address -- and, with every
                client chosen, whose office it is -- rides in the sub-line. */}
            {directory.sites.map((site) => {
              const sub = [
                selected === null ? clientName(site.client_id) : "",
                site.description || site.address,
              ].filter((part) => part !== "");
              return (
                <tr key={site.id}>
                  <Td lead={<Icon name="building" size={18} />}>
                    <div className="cell-title">{site.name}</div>
                    {sub.length > 0 ? <div className="cell-sub">{sub.join(" · ")}</div> : null}
                  </Td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      </QueryState>
    </Panel>
  );
}
```

`VlanPanel`: sustituir `const sites = useIpamSites();` por

```tsx
  const { selected } = useClientScope();
  const clientName = useClientName();
  const directory = useIpamSiteDirectory();
  // No parent above the form: the client chosen at the top, or the Client
  // field's. Its sites are the only ones a new VLAN may name; a site, once
  // picked, decides the VLAN's client (the body then names none).
  const target = useNewRowClient(IPAM_MANAGE);
  const [pickedClient, setPickedClient] = useState<string | null>(null);
  const vlanClient = target.needsField ? pickedClient : target.clientId;
  const sites = useIpamSites(vlanClient);
  const showClient = selected === null;
```

En su tabla: tras `<Th>{t("ipam.fields.vlan")}</Th>` añadir `{showClient ? <Th>{t("ipam.fields.client")}</Th> : null}`;
tras la primera `</Td>` de cada fila añadir `{showClient ? <Td>{clientName(vlan.client_id)}</Td> : null}`; el
`label` de la pastilla de sede pasa a `directory.siteName(vlan.site_id)`. En el formulario, antes del campo
VID:

```tsx
            {target.needsField ? (
              <ClientField
                code={IPAM_MANAGE}
                value={pickedClient}
                onChange={(id) => {
                  setPickedClient(id);
                  // Another client is another set of sites.
                  setSiteId("");
                }}
              />
            ) : null}
```

y la llamada pasa a `create.mutate({ vid: Number(vid), site_id: siteId || null, client_id: vlanClient, name }, …)`.

- [ ] **Step 7: Implementar `SubnetTreePage.tsx`**

Imports: añadir `ClientField`, `useClientScope`, `useNewRowClient` (mismas rutas que arriba) e `IPAM_MANAGE` de
`./types`; en el import de `./api`, quitar `useIpamClient` y añadir `useIpamSiteDirectory`.

En `SubnetTreePage` (126-160): sustituir `const sites = useIpamSites();` por

```tsx
  const { selected } = useClientScope();
  const clientName = useClientName();
  const directory = useIpamSiteDirectory();
  const showClient = selected === null;
```

y `siteName` (157-158) por `const siteName = directory.siteName;`. El filtro de sede (308-316) itera
`directory.sites` y, con `showClient`, rotula cada opción
`t("ipam.fields.siteOfClient", { site: site.name, client: clientName(site.client_id) })`. En la tabla
(392-410): tras `<Th>{t("ipam.tree.colName")}</Th>` añadir `{showClient ? <Th>{t("ipam.fields.client")}</Th> : null}`,
y tras la primera `</Td>` de cada fila `{showClient ? <Td>{clientName(node.subnet.client_id)}</Td> : null}`.

En `CreateSubnetForm` (563-590): sustituir `const own = useIpamClient();` y la línea `const client = …` por

```tsx
  // THE VRF DECIDES THE SUBNET'S CLIENT; with none, the client chosen at the
  // top or the Client field's, whose `global` VRF it goes into. The sites and
  // VLANs offered are that client's -- the server refuses any other's (final
  // review E). `null` while the Client field is empty.
  const vrfClient = vrfId ? vrfs.data?.find((vrf) => vrf.id === vrfId)?.client_id : undefined;
  const target = useNewRowClient(IPAM_MANAGE, vrfClient);
  const [pickedClient, setPickedClient] = useState<string | null>(null);
  const client = vrfId ? (vrfClient ?? null) : target.needsField ? pickedClient : target.clientId;
```

en `submit`, añadir `client_id: client,` al objeto de `create.mutate`; y tras el `<Field>` de VRF:

```tsx
          {!vrfId && target.needsField ? (
            <ClientField
              code={IPAM_MANAGE}
              value={pickedClient}
              onChange={(id) => {
                setPickedClient(id);
                setSiteId("");
                setVlanId("");
              }}
            />
          ) : null}
```

- [ ] **Step 8: Implementar `ScanAuthorizationsPage.tsx` e `ImportPage.tsx`**

`ScanAuthorizationsPage.tsx`: imports como en el Step 7, con `IPAM_SCOPE_MANAGE`. En la lista (145-160), con
`const { selected } = useClientScope(); const clientName = useClientName(); const showClient = selected === null;`
en el componente de la tabla, cabecera `{showClient ? <Th>{t("ipam.fields.client")}</Th> : null}` tras
`colPrefix`, y `ScopeRow` (200) recibe dos props nuevas, `showClient: boolean` y `clientName: (id: string) => string`
(se las pasa el `scopes.data?.map` de 158-166), y pinta `{showClient ? <Td>{clientName(scope.client_id)}</Td> : null}`
tras su primera celda. En
`GrantForm` (300-470), tras `const [expiresAt, setExpiresAt] = useState("");`:

```tsx
  // The VRF decides the grant's client; with none, the client chosen at the
  // top or the Client field's, whose `global` VRF it is for.
  const vrfClient = vrfId ? vrfs.data?.find((vrf) => vrf.id === vrfId)?.client_id : undefined;
  const target = useNewRowClient(IPAM_SCOPE_MANAGE, vrfClient);
  const [pickedClient, setPickedClient] = useState<string | null>(null);
  const client = vrfId ? (vrfClient ?? null) : target.needsField ? pickedClient : target.clientId;
```

tras el `<Field>` de VRF:

```tsx
        {!vrfId && target.needsField ? (
          <ClientField code={IPAM_SCOPE_MANAGE} value={pickedClient} onChange={setPickedClient} />
        ) : null}
```

y `client_id: client,` en el objeto de `create.mutate`, tras `vrf_id`.

`ImportPage.tsx`: imports — quitar `useIpamClient`; añadir `ClientField`, `useClientScope`, `useNewRowClient`,
`IPAM_MANAGE` (`./types`). Sustituir `defaultImportVrf` (80-91) por:

```tsx
/**
 * The VRF an import goes into until the operator picks another: the `global`
 * VRF of the client chosen at the top when it is listed, else the first
 * listed -- the VRFs listed are exactly those of the clients the operator can
 * see, and each names its client in the picker. `null` with none listed: the
 * import then names a client (`ipam/api.ts`), the one chosen at the top or
 * the one the Client field names.
 */
function defaultImportVrf(vrfs: readonly VrfOut[], selected: string | null): string | null {
  const selectedGlobal = vrfs.find((vrf) => vrf.client_id === selected && vrf.name === "global");
  return selectedGlobal?.id ?? vrfs[0]?.id ?? null;
}
```

En `CsvImportPanel`: sustituir `const client = useIpamClient();` por

```tsx
  const { selected } = useClientScope();
  const target = useNewRowClient(IPAM_MANAGE);
  const [pickedClient, setPickedClient] = useState<string | null>(null);
  const client = target.needsField ? pickedClient : target.clientId;
```

`const vrfId = vrfChoice ?? defaultImportVrf(vrfs.data ?? [], selected);`. El aviso `blockReason` (130-136)
gana una condición: `(previewed !== null && (previewed.vrfId !== vrfId || (vrfId === null && previewed.clientId !== client)) ? t("ipam.importExport.targetMoved") : null)`.
Tras el bloque `{(vrfs.data?.length ?? 0) > 0 && (…)}` del selector de VRF:

```tsx
      {(vrfs.data?.length ?? 0) === 0 && target.needsField ? (
        <ClientField
          code={IPAM_MANAGE}
          value={pickedClient}
          onChange={(id) => {
            setPickedClient(id);
            // Another client is another import: the verdict was not about it.
            setPreviewed(null);
            apply.reset();
            preview.reset();
          }}
        />
      ) : null}
```

En el botón Preview (268-290): `const target = vrfId; const targetClient = client;`, `preview.mutate({ file, mapping, vrfId: target, clientId: targetClient }, …)`
y `setPreviewed({ …, vrfId: target, clientId: targetClient })`. En Apply (298):
`apply.mutate({ file, mapping, digest: previewed.digest, vrfId: previewed.vrfId, clientId: previewed.clientId });`.

`ipamState.ts:1451-1462`, `PreviewedFile`: el comentario de `vrfId` («`null` when it named the internal
client's `global` VRF instead») pasa a «`null` when it named a client's `global` VRF instead: `clientId`
says whose.» y se añade

```ts
  /** The client whose `global` VRF the preview read when `vrfId` is null; ignored otherwise. */
  clientId: string | null;
```

Toda prueba de `ipamState.test.ts` que construye un `PreviewedFile` literal gana `clientId: null`.

- [ ] **Step 9: Textos de IPAM**

`frontend/src/i18n/en/ipam.ts`, dentro de `fields`: `client: "Client",` y `siteOfClient: "{site} · {client}",`.
`frontend/src/i18n/es/ipam.ts`, dentro de `fields`: `client: "Cliente",` y `siteOfClient: "{site} · {client}",`.

- [ ] **Step 10: Ejecutar y ver que pasan**

```bash
cd frontend && npx vitest run src/modules/ipam && npx tsc -b
```

Esperado: PASS, y `tsc` sin errores (ningún `useIpamClient` ni `useIpamSites()` sin argumento queda).

- [ ] **Step 11: Commit**

```bash
git add frontend/src/modules/ipam frontend/src/i18n/en/ipam.ts frontend/src/i18n/es/ipam.ts
git commit -m "feat(console): IPAM files new rows under the parent, selected or chosen client

A VRF, a VLAN without a site, and a subnet, an address, a scan
authorization or an import without a VRF take the client chosen at the
top, or a required Client field with every client chosen. Pickers list
the row's client's sites and VLANs; with every client chosen the VLAN,
subnet and scan-authorization lists show a Client column and name each
site. useIpamClient is gone (design §3.1-§3.4)."
```

#### Devices: grupos y activos a mano

- [ ] **Step 12: Las pruebas que fallan, devices**

Crear `frontend/src/modules/devices/clientSelection.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, ClientSiteOut, DeviceGroup, MeClient } from "../../lib/api";
import { AddAssetDialog } from "./AddAssetDialog";
import { GroupsPage } from "./GroupsPage";

// MSP clients phase 3, design §3.1-§3.4: a new group takes its parent group's
// client, else the client chosen at the top, else a required Client field; its
// parent picker lists that client's groups; the list says whose each group is.
// A hand-made asset asks for its site (§3.2), among its client's.

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

const CODES = ["devices.read", "devices.manage", "assets.manage"];
const INTERNAL: MeClient = { id: "o1", name: "Novieri", kind: "internal", enabled_modules: [], permissions: CODES };
const ACME: MeClient = { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: CODES };
const CLIENTS = [INTERNAL, ACME];

function selectClient(selected: string | null): void {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients: CLIENTS, ready: true });
}

const ME = { user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions: CODES, permissions_everywhere: [], clients: CLIENTS };

function group(overrides: Partial<DeviceGroup>): DeviceGroup {
  return {
    id: "g1", client_id: "o1", name: "Servers", parent_group_id: null, description: "", dynamic: false,
    device_count: 0, precedence: 0, archived_at: null, ...overrides,
  };
}

const NOW = new Date().toISOString();
const GROUPS = {
  items: [group({ id: "g-own", name: "Own servers" }), group({ id: "g-acme", client_id: "c-acme", name: "Acme desks" })],
  total: 2,
  ungrouped_device_count: 0,
  evaluation: {
    last_evaluated_at: NOW, last_full_pass_at: NOW, full_pass: false, groups_evaluated: 2,
    devices_evaluated: 0, devices_moved: 0, devices_without_inventory: 0, interval_seconds: 300,
  },
};

function stubApi(overrides: Partial<Record<keyof ApiClient, unknown>> = {}): Record<string, ReturnType<typeof vi.fn>> {
  const api = {
    me: vi.fn().mockResolvedValue(ME),
    listDeviceGroups: vi.fn().mockResolvedValue(GROUPS),
    listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    createDeviceGroup: vi.fn().mockResolvedValue(group({})),
    previewDeviceGroupRules: vi.fn(),
    listClientSites: vi.fn((clientId: string) =>
      Promise.resolve<ClientSiteOut[]>(
        clientId === "c-acme"
          ? [{ id: "site-a", client_id: "c-acme", name: "Acme HQ", address: "", description: "", egress_networks: [], archived_at: null, device_count: 0 }]
          : [],
      ),
    ),
    createAsset: vi.fn().mockResolvedValue({ id: "d1" }),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api);
  return api as Record<string, ReturnType<typeof vi.fn>>;
}

function renderIt(node: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => selectClient("o1"));

describe("a new device group", () => {
  it("with every client chosen, asks for the client and files the group there", async () => {
    selectClient(null);
    const api = stubApi();
    const user = userEvent.setup();
    renderIt(<GroupsPage />);
    await user.click(await screen.findByRole("button", { name: "New group" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Name"), "desks");
    await user.selectOptions(within(dialog).getByLabelText("Client"), "c-acme");
    await user.click(within(dialog).getByRole("button", { name: "Create group" }));
    await waitFor(() => expect(api.createDeviceGroup).toHaveBeenCalled());
    expect(api.createDeviceGroup.mock.calls[0]?.[0]).toMatchObject({ name: "desks", client_id: "c-acme" });
  });

  it("takes the parent group's client and stops asking once a parent is picked", async () => {
    selectClient(null);
    const api = stubApi();
    const user = userEvent.setup();
    renderIt(<GroupsPage />);
    await user.click(await screen.findByRole("button", { name: "New group" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Name"), "desks floor 2");
    await user.click(within(dialog).getByRole("combobox", { name: /Parent group/ }));
    await user.click(await screen.findByRole("option", { name: /Acme desks/ }));
    expect(within(dialog).queryByLabelText("Client")).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: "Create group" }));
    await waitFor(() => expect(api.createDeviceGroup).toHaveBeenCalled());
    expect(api.createDeviceGroup.mock.calls[0]?.[0]).toMatchObject({ parent_group_id: "g-acme", client_id: "c-acme" });
  });

  it("offers as parents only the groups of the client the new group is for", async () => {
    selectClient("c-acme");
    stubApi();
    const user = userEvent.setup();
    renderIt(<GroupsPage />);
    await user.click(await screen.findByRole("button", { name: "New group" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("combobox", { name: /Parent group/ }));
    expect(await screen.findByRole("option", { name: /Acme desks/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Own servers/ })).toBeNull();
  });

  it("shows a Client column with every client chosen, and hides it with one", async () => {
    selectClient(null);
    stubApi();
    const { unmount } = renderIt(<GroupsPage />);
    const row = (await screen.findByText("Acme desks")).closest("tr") as HTMLElement;
    expect(screen.getByRole("columnheader", { name: "Client" })).toBeInTheDocument();
    expect(within(row).getByText("Acme")).toBeInTheDocument();
    unmount();

    selectClient("o1");
    renderIt(<GroupsPage />);
    await screen.findByText("Own servers");
    expect(screen.queryByRole("columnheader", { name: "Client" })).toBeNull();
  });
});

describe("a hand-made asset", () => {
  it("asks for the client and then its site, and files the asset there", async () => {
    selectClient(null);
    const api = stubApi();
    const user = userEvent.setup();
    renderIt(<AddAssetDialog open onClose={() => {}} />);
    await user.selectOptions(await screen.findByLabelText("Client"), "c-acme");
    // The only site of Acme is chosen for the person (`SiteField`).
    await waitFor(() => expect((screen.getByLabelText("Site") as HTMLSelectElement).value).toBe("site-a"));
    await user.click(screen.getByRole("button", { name: /add asset/i }));
    await waitFor(() => expect(api.createAsset).toHaveBeenCalled());
    expect(api.createAsset.mock.calls[0]?.[0]).toMatchObject({ site_id: "site-a" });
  });

  it("sends nothing without a site, and names the field", async () => {
    selectClient("o1");
    const api = stubApi();
    const user = userEvent.setup();
    renderIt(<AddAssetDialog open onClose={() => {}} />);
    await user.click(await screen.findByRole("button", { name: /add asset/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Site is required");
    expect(api.createAsset).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 13: Las pruebas existentes de devices, corregidas**

`frontend/src/modules/devices/GroupsPage.test.tsx` (arnés del Step 1 con `SCOPE_INTERNAL.id = ORG`):

| Línea | Antes | Después |
|---|---|---|
| 113 | `/** `/me`'s `org_id`: the organisation's, and its internal client's, id. */` | `/** The internal client's id, chosen at the top in these tests (`selectClient(ORG)`). */` |
| 128-130 | `// `org_id` is the internal client's id (MSP clients, phase 1): a new` / `// group, and the preview of one, are filed under it.` | `// A new group, and the preview of one, go to the client chosen at the` / `// top (design §3.1): here the internal client, ORG.` |
| 398-402 | `// RE-POINTED (MSP clients, phase 1): … Until the client selector (phase 3) that is the internal client, whose id is `/me`'s `org_id`.` + `client_id: ORG,` | `// The client chosen at the top (design §3.1): here the internal one.` + `client_id: ORG,` (la afirmación no cambia) |
| 823-826 | `// RE-POINTED: a group not saved yet has no client of its own, so the` / `// preview names the one it will be created in -- the internal client.` + `ORG,` | `// A group not saved yet has no client of its own, so the preview names` / `// the one it will be created in: the client chosen at the top.` + `ORG,` |

`frontend/src/modules/devices/AddAssetDialog.test.tsx`: `renderDialog` (36-52) deja de recibir `siteId` y
monta `<AddAssetDialog open={open} onClose={onClose} />`; el `mockUseApi` devuelve
`{ createAsset, me: () => Promise.resolve({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions: ["assets.manage"] }), listClientSites: () => Promise.resolve([{ id: SITE, client_id: "o1", name: "Principal", address: "", description: "", egress_networks: [], archived_at: null, device_count: 0 }]) }`,
y cada prueba que pulsa «Add asset» espera antes a que el campo Sede tenga `SITE`
(`await waitFor(() => expect((screen.getByLabelText("Site") as HTMLSelectElement).value).toBe(SITE))`). El
comentario 28-33 pasa a «The site a hand-made asset is filed under: the only site of the client chosen at
the top, which `SiteField` picks by itself.»

| Línea | Antes | Después |
|---|---|---|
| 89 | `// RE-POINTED: the site is part of the body now (see `SITE`).` | `// The site the Site field holds (see `SITE`).` |
| 98 | `it("files the asset under the site it was given", …)` | `it("files the asset under the site its Site field holds", …)` |
| 107-119 | `renderDialog(() => {}, null)` y `expect(alert).toHaveTextContent(/account has not loaded/i);` | El `listClientSites` de esa prueba devuelve `[]`, no hay sede que elegir, y `expect(alert).toHaveTextContent("Site is required");`. Nombre: `it("sends nothing, and says why, while no site is chosen", …)`; comentario: «With no site chosen there is nothing to file the asset under, and a body without one is a 422 the operator could do nothing about.» |

Las pruebas de cierre y reapertura (`close`/`reopen`) siguen valiendo: el formulario se desmonta al cerrar
y vuelve vacío.

`frontend/src/modules/devices/devices.es.test.tsx:267-269`: `<AddAssetDialog open onClose={() => {}} siteId="…" />`
pasa a `<AddAssetDialog open onClose={() => {}} />`; su `mockUseApi` añade el mismo `me` y
`listClientSites` de arriba; y la nota esperada `"Un registro sin agente. Todo lo de aquí es opcional."`
pasa a `"Un registro sin agente. Solo la sede es obligatoria."`. La lista de etiquetas de la línea 270 gana
`"Sede"`.

- [ ] **Step 14: Ejecutar y ver que fallan**

```bash
cd frontend && npx vitest run src/modules/devices
```

Esperado: FAIL en `clientSelection.test.tsx` (no hay campo «Client», el cuerpo lleva el cliente interno,
no hay columna «Client», no hay campo «Site») y en las pruebas de `AddAssetDialog` corregidas (el diálogo
todavía exige la prop `siteId`).

- [ ] **Step 15: Implementar `GroupsPage.tsx`**

Imports (línea 95): sustituir `import { internalClientId, requireKnown } from "../../lib/internalClient";` por

```tsx
import { ClientField } from "../../components/ClientField";
import { useClientScope } from "../../lib/clientScope";
import { requireKnown, useNewRowClient } from "../../lib/clientSelection";
import { useClientName } from "../settings/api";
```

Sustituir las líneas 478-485 (el comentario `// PHASE 3: …` y `const internalClient = internalClientId(me.data);`) por

```tsx
  // A group organises ONE client's devices (MSP clients spec §1.5). A new one
  // takes its parent group's client; with no parent, the client chosen at the
  // top, or with every client the editor's Client field (design §3.1). A
  // saved group keeps its own client, which an edit never sends.
  const { selected } = useClientScope();
  const clientName = useClientName();
  const showClient = selected === null;
  const [pickedClient, setPickedClient] = useState<string | null>(null);
```

Tras `const rows = useMemo(() => buildTree(items), [items]);` añadir

```tsx
  const parentClient = editor?.parentGroupId
    ? items.find((group) => group.id === editor.parentGroupId)?.client_id
    : undefined;
  const target = useNewRowClient("devices.manage", parentClient);
  const newClient = target.needsField ? pickedClient : target.clientId;
```

En `save` (538-539), la rama de alta pasa a `api.createDeviceGroup({ ...body, client_id: requireKnown(newClient) });`
sin el comentario `// PHASE 3: …`. En `openCreate` añadir `setPickedClient(null);`. En la vista previa
(634-645): el tercer argumento pasa a `editor?.group ? null : newClient` con el comentario «A saved group's
preview runs over its own client; one not saved yet names the client it will be created in.», y
`enabled: settled !== null && canManage && (editor?.group != null || newClient != null),`.

`parentOptions` (680-694) pasa a:

```tsx
  // The parent options exclude the group being edited (a group cannot be its
  // own parent) and every other client's groups: a parent is of the same
  // client (§3.3). With every client chosen and none decided yet, all are
  // offered, each saying whose it is -- picking one decides.
  const rowClient = editor?.group ? editor.group.client_id : newClient;
  const parentOptions = items
    .filter((group) => group.archived_at === null && group.id !== editor?.group?.id)
    .filter((group) => rowClient === null || group.client_id === rowClient)
    .map((group) => ({
      id: group.id,
      label: showClient
        ? t("devices.groups.editor.parentOfClient", { group: group.name, client: clientName(group.client_id) })
        : group.name,
      sublabel: t("devices.groups.editor.parentSub", {
        precedence: group.precedence,
        count: group.device_count,
      }),
    }));
```

En el editor, antes del `<EntityPicker label={t("devices.groups.editor.parent")} …>`:

```tsx
              {!editor.group && target.needsField ? (
                <ClientField code="devices.manage" value={pickedClient} onChange={setPickedClient} />
              ) : null}
```

En la tabla (778-790): tras `<th scope="col">{t("devices.groups.colGroup")}</th>` añadir
`{showClient ? <th scope="col">{t("devices.groups.colClient")}</th> : null}`; en la fila «sin grupo», tras su
primera `</Td>`, `{showClient ? <td>{t("lib.clients.everyClient")}</td> : null}`; y en cada fila de
`rows.map(({ group, depth }) => …)`, tras su primera celda, `{showClient ? <td>{clientName(group.client_id)}</td> : null}`.

- [ ] **Step 16: Implementar `AddAssetDialog.tsx` y `DevicesPage.tsx`**

`AddAssetDialog.tsx`: imports — sustituir `import { Form, FormActions } from "../../components/form";` por
`import { attemptProblems, Form, FormActions } from "../../components/form"; import type { Attempt } from "../../components/form";`,
sustituir `import { requireKnown } from "../../lib/internalClient";` por

```tsx
import { ClientField } from "../../components/ClientField";
import { SiteField } from "../../components/SiteField";
import { requireKnown, useNewRowClient } from "../../lib/clientSelection";
import { translate } from "../../i18n/core";
```

y sustituir desde `export function AddAssetDialog(` hasta el final de `close()` (líneas 51-117) por:

```tsx
export function AddAssetDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  // MOUNTED ONLY WHILE OPEN (MSP clients phase 3). A closed dialog asks
  // nothing of the client selector, and every opening starts from an empty
  // form with no stale failure -- which `create.reset()` on close used to buy.
  return open ? <AddAssetForm onClose={onClose} /> : null;
}

function AddAssetForm({ onClose }: { onClose: () => void }) {
  const t = useT();
  const api = useApi();
  const queryClient = useQueryClient();
  const [hostname, setHostname] = useState("");
  const [description, setDescription] = useState("");
  const [primaryIp, setPrimaryIp] = useState("");
  const [assetType, setAssetType] = useState<Device["asset_type"]>("computer");
  const [lifecycle, setLifecycle] = useState<Device["lifecycle"]>("in_service");
  const [location, setLocation] = useState("");
  const [pushDetails, setPushDetails] = useState<PushDetails>({ fqdn: "", os: "", arch: "" });
  // THE SITE IS THE ONE REQUIRED FIELD (MSP clients spec §1.5, design §3.2):
  // `POST /devices` takes it and derives the client from it. Its client is the
  // one chosen at the top, or with every client the Client field's.
  const target = useNewRowClient("assets.manage");
  const [pickedClient, setPickedClient] = useState<string | null>(null);
  const clientId = target.needsField ? pickedClient : target.clientId;
  const [siteId, setSiteId] = useState<string | null>(null);
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  const create = useMutation({
    mutationFn: () => {
      // Empty strings are omitted rather than sent. The API rejects a malformed
      // IP, and "" is malformed -- so a form that sent every field would refuse
      // every asset whose IP the operator did not know.
      const body: AssetCreateInput = {
        site_id: requireKnown(siteId),
        asset_type: assetType,
        lifecycle,
      };
      if (hostname.trim()) body.hostname = hostname.trim();
      if (description.trim()) body.description = description.trim();
      if (primaryIp.trim()) body.primary_ip = primaryIp.trim();
      if (location.trim()) body.location = location.trim();
      if (pushDetails.fqdn.trim()) body.fqdn = pushDetails.fqdn.trim();
      if (pushDetails.os) body.os_family = pushDetails.os;
      if (pushDetails.arch) body.architecture = pushDetails.arch;
      return api.createAsset(body);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["devices"] });
      onClose();
    },
  });

  function close() {
    onClose();
  }

  function submit() {
    // Held as rules + values, worded at render, so a language switch rewords them.
    const next: Attempt = {
      rules: () => [
        ...(target.needsField ? [{ name: "client", label: translate("devices.addAsset.client") }] : []),
        { name: "site", label: translate("devices.addAsset.site") },
      ],
      values: { client: clientId, site: siteId },
    };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    create.mutate();
  }
```

En el JSX: `<Dialog open={open} …>` pasa a `<Dialog open …>`; `<Form onSubmit={() => create.mutate()}>` pasa a
`<Form onSubmit={submit}>`; al principio de `<div className="form-grid">`:

```tsx
          {target.needsField ? (
            <ClientField
              code="assets.manage"
              value={pickedClient}
              onChange={(id) => {
                setPickedClient(id);
                // Another client is another set of sites.
                setSiteId(null);
              }}
            />
          ) : null}
          <SiteField clientId={clientId} value={siteId} onChange={setSiteId} />
```

`<FormActions …>` gana `problems={attemptProblems(attempt)}`, y su comentario «NO `problems`. Every field here
is optional on purpose…» pasa a «`problems` names the site, and the client with every client chosen: the
only fields a hand-made asset cannot go without (design §3.2). Everything else stays optional -- a boxed
laptop may have nothing but a description.»

`DevicesPage.tsx`: borrar el import de la línea 51 (`import { internalSiteId } from "../../lib/internalClient";`)
y sustituir 912-920 por

```tsx
      <AddAssetDialog open={adding} onClose={() => setAdding(false)} />
```

- [ ] **Step 17: Textos de devices**

`frontend/src/i18n/en/devices.ts`: en `addAsset`, `note: "A record with no agent. Only its site is required.",`
(sustituye a la actual), `client: "Client",` y `site: "Site",`; en `groups`, `colClient: "Client",`; en
`groups.editor`, `parentOfClient: "{group} · {client}",`.
`frontend/src/i18n/es/devices.ts`: en `addAsset`, `note: "Un registro sin agente. Solo la sede es obligatoria.",`,
`client: "Cliente",`, `site: "Sede",`; en `groups`, `colClient: "Cliente",`; en `groups.editor`,
`parentOfClient: "{group} · {client}",`.

- [ ] **Step 18: Ejecutar y ver que pasan**

```bash
cd frontend && npx vitest run src/modules/devices && npx tsc -b
```

Esperado: PASS.

- [ ] **Step 19: Commit**

```bash
git add frontend/src/modules/devices frontend/src/i18n/en/devices.ts frontend/src/i18n/es/devices.ts
git commit -m "feat(console): device groups and hand-made assets in the right client

A new group takes its parent's client, else the selected one, else a
required Client field; its parent picker lists that client's groups and
the list shows a Client column with every client chosen. A hand-made
asset asks for its site among its client's (design §3.1-§3.4)."
```

#### Agent-releases y enrollment: la sede

- [ ] **Step 20: Las pruebas que fallan, agent-releases y enrollment**

Crear `frontend/src/modules/agent-releases/clientSelection.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentRelease, ApiClient, ClientSiteOut, MeClient } from "../../lib/api";
import { AgentReleasesPage } from "./AgentReleasesPage";

// MSP clients phase 3, design §3.2: the install command, the install link and
// the managed deployment are one SITE's, chosen on the page among the sites of
// the client chosen at the top, or of the page's Client field with every client.

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

const CODES = ["enrollment.manage", "enrollment.link.manage"];
const INTERNAL: MeClient = { id: "o", name: "Novieri", kind: "internal", enabled_modules: [], permissions: CODES };
const ACME: MeClient = { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: CODES };

function selectClient(selected: string | null): void {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients: [INTERNAL, ACME], ready: true });
}

function site(id: string, clientId: string, name: string): ClientSiteOut {
  return { id, client_id: clientId, name, address: "", description: "", egress_networks: [], archived_at: null, device_count: 0 };
}

// The shape of `AgentReleasesPage.test.tsx:43-57`'s `release()`.
const RELEASE: AgentRelease = {
  id: "11111111-1111-1111-1111-111111111111",
  version: "873d666",
  os: "linux",
  arch: "amd64",
  size_bytes: 18_874_368,
  sha256: "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90",
  signed: false,
  notes: "",
  published_at: "2026-08-18T09:00:00Z",
  has_artifact: true,
};

function stubApi(overrides: Partial<Record<keyof ApiClient, unknown>> = {}) {
  const api = {
    me: () => Promise.resolve({ user_id: "u", org_id: "o", email: "a@b.c", display_name: "A", permissions: CODES, timezone: null, clients: [INTERNAL, ACME] }),
    listAgentReleases: () => Promise.resolve([RELEASE]),
    listClientSites: vi.fn((clientId: string) =>
      Promise.resolve(
        clientId === "c-acme" ? [site("site-a1", "c-acme", "Acme HQ"), site("site-a2", "c-acme", "Acme Lab")] : [site("site-o", "o", "Principal")],
      ),
    ),
    generateAgentInstallCommand: vi.fn().mockResolvedValue({ command: "curl …", expires_at: "2026-10-31T12:00:00Z" }),
    getInstallLink: vi.fn().mockResolvedValue({
      enabled: false, has_code: false, networks: [], disable_at: null, hourly_limit: 50, enabled_at: null, joined_since_enabled: 0,
    }),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <AgentReleasesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => selectClient("o"));

describe("where new machines go", () => {
  it("uses the only site of the client chosen at the top, for the command and the link", async () => {
    const api = stubApi();
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.getInstallLink).toHaveBeenCalledWith("site-o"));
    expect(screen.queryByLabelText("Client")).toBeNull();

    await user.click(await screen.findByRole("button", { name: /install command/i }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Generate command" }));
    await waitFor(() => expect(api.generateAgentInstallCommand).toHaveBeenCalledWith(RELEASE.id, 4, "site-o"));
  });

  it("with every client chosen, asks for the client, then the site, and mints for that site", async () => {
    selectClient(null);
    const api = stubApi();
    const user = userEvent.setup();
    renderPage();
    await user.selectOptions(await screen.findByLabelText("Client"), "c-acme");
    const picker = await screen.findByLabelText("Site");
    await within(picker).findByRole("option", { name: "Acme Lab" });
    expect(within(picker).queryByRole("option", { name: "Principal" })).toBeNull();
    await user.selectOptions(picker, "site-a2");
    await waitFor(() => expect(api.getInstallLink).toHaveBeenCalledWith("site-a2"));

    await user.click(screen.getByRole("button", { name: /install command/i }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Generate command" }));
    await waitFor(() => expect(api.generateAgentInstallCommand).toHaveBeenCalledWith(RELEASE.id, 4, "site-a2"));
  });

  it("mints nothing while no site is chosen", async () => {
    selectClient(null);
    const api = stubApi();
    const user = userEvent.setup();
    renderPage();
    await screen.findByLabelText("Client");
    await user.click(await screen.findByRole("button", { name: /install command/i }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Generate command" }));
    expect(api.generateAgentInstallCommand).not.toHaveBeenCalled();
    expect(api.getInstallLink).not.toHaveBeenCalled();
  });
});
```

Crear `frontend/src/modules/enrollment/clientSelection.test.tsx` (mismo patrón de simulación que
`EnrollmentPage.test.tsx:39-56`: `useApi` y las funciones de petición de `./api`):

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, ClientSiteOut, MeClient } from "../../lib/api";
import { EnrollmentPage } from "./EnrollmentPage";

// MSP clients phase 3, design §3.2: an enrollment token is one site's, chosen
// in the form among the sites of the client chosen at the top, or of the
// form's Client field with every client.

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

const listEnrollmentTokens = vi.fn();
const createEnrollmentToken = vi.fn();
vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  listEnrollmentTokens: (...a: unknown[]) => listEnrollmentTokens(...a),
  createEnrollmentToken: (...a: unknown[]) => createEnrollmentToken(...a),
}));

const CODES = ["enrollment.manage", "devices.read"];
const INTERNAL: MeClient = { id: "o", name: "Novieri", kind: "internal", enabled_modules: [], permissions: CODES };
const ACME: MeClient = { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: CODES };

function selectClient(selected: string | null): void {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients: [INTERNAL, ACME], ready: true });
}

function site(id: string, clientId: string, name: string): ClientSiteOut {
  return { id, client_id: clientId, name, address: "", description: "", egress_networks: [], archived_at: null, device_count: 0 };
}

function renderPage() {
  listEnrollmentTokens.mockResolvedValue({ items: [], total: 0, revoked_count: 0, unlimited_count: 0 });
  createEnrollmentToken.mockResolvedValue({
    id: "t1", name: "lab", token: "plain", expires_at: "2099-01-01T00:00:00Z", max_uses: 1,
  });
  mockUseApi.mockReturnValue({
    me: () =>
      Promise.resolve({ user_id: "u", org_id: "o", email: "a@b.c", display_name: "A", permissions: CODES, timezone: null, clients: [INTERNAL, ACME] }),
    listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    listClientSites: vi.fn((clientId: string) =>
      Promise.resolve(
        clientId === "c-acme" ? [site("site-a1", "c-acme", "Acme HQ"), site("site-a2", "c-acme", "Acme Lab")] : [site("site-o", "o", "Principal")],
      ),
    ),
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/enrollment"]}>
        <EnrollmentPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  listEnrollmentTokens.mockReset();
  createEnrollmentToken.mockReset();
  selectClient("o");
});

describe("an enrollment token's site", () => {
  it("with every client chosen, asks for the client and the site, and sends that site", async () => {
    selectClient(null);
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Create token" }));
    await user.selectOptions(await screen.findByLabelText("Client"), "c-acme");
    const picker = await screen.findByLabelText("Site");
    await within(picker).findByRole("option", { name: "Acme Lab" });
    expect(within(picker).queryByRole("option", { name: "Principal" })).toBeNull();
    await user.selectOptions(picker, "site-a2");
    await user.type(screen.getByLabelText("Token name"), "lab");
    await user.click(screen.getByRole("button", { name: "Issue token…" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Issue token" }));
    await waitFor(() =>
      expect(createEnrollmentToken).toHaveBeenCalledWith(expect.anything(), {
        name: "lab", expires_in_hours: 72, max_uses: 1, site_id: "site-a2",
      }),
    );
  });

  it("uses the only site of the client chosen at the top without asking for a client", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Create token" }));
    expect(screen.queryByLabelText("Client")).toBeNull();
    await waitFor(() => expect((screen.getByLabelText("Site") as HTMLSelectElement).value).toBe("site-o"));
  });

  it("refuses to open the confirmation without a site, and names the field", async () => {
    selectClient(null);
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Create token" }));
    await screen.findByLabelText("Client");
    await user.type(screen.getByLabelText("Token name"), "lab");
    await user.click(screen.getByRole("button", { name: "Issue token…" }));
    expect(await screen.findByText(/Site is required/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(createEnrollmentToken).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 21: Las pruebas existentes de agent-releases y enrollment, corregidas**

`frontend/src/modules/agent-releases/AgentReleasesPage.test.tsx`:
- `renderPage` (59-92) y `renderWith` (655-672): el objeto de `mockUseApi` gana
  `listClientSites: () => Promise.resolve([{ id: "s-1", client_id: "o", name: "Principal", address: "", description: "", egress_networks: [], archived_at: null, device_count: 0 }])`.

| Línea | Antes | Después |
|---|---|---|
| 414-419 | comentario «until the client selector (phase 3) that is the internal client's "Principal" site, whose id is the organisation's -- `/me`'s `org_id`, "o" in `renderPage`.» y `expect(generate).toHaveBeenCalledWith("11111111-1111-1111-1111-111111111111", 4, "o");` | comentario «the site chosen on the page: the only site of the client chosen at the top, "s-1".»; la prueba espera `await waitFor(() => expect((screen.getByLabelText("Site") as HTMLSelectElement).value).toBe("s-1"))` antes de pulsar, y `expect(generate).toHaveBeenCalledWith("11111111-1111-1111-1111-111111111111", 4, "s-1");` |
| 680-682 | comentario «the panel manages the internal client's "Principal" site, whose id is `/me`'s `org_id`.» y `expect(getInstallLink).toHaveBeenCalledWith("o");` | comentario «the panel manages the site chosen on the page.» y `await waitFor(() => expect(getInstallLink).toHaveBeenCalledWith("s-1"));` |

`frontend/src/modules/agent-releases/agent-releases.es.test.tsx:148`: su simulación de la API gana el mismo
`listClientSites`.

`frontend/src/modules/enrollment/EnrollmentPage.test.tsx`: `renderPage` (170-180) gana
`listClientSites: () => Promise.resolve([{ id: "s-1", client_id: "o", name: "Principal", address: "", description: "", egress_networks: [], archived_at: null, device_count: 0 }])`
en el objeto de la API.

| Línea | Antes | Después |
|---|---|---|
| 397-400 | comentario «… the internal client's "Principal" site … `org_id`, "o" in `renderPage`.» y `site_id: "o",` | comentario «The site the form's Site field holds: the only site of the client chosen at the top.» y `site_id: "s-1",`; la prueba espera a que el campo Sede valga `s-1` antes de pulsar «Issue token…» |

`frontend/src/modules/enrollment/enrollment.es.test.tsx:115`: su API simulada gana el mismo `listClientSites`.

- [ ] **Step 22: Ejecutar y ver que fallan**

```bash
cd frontend && npx vitest run src/modules/agent-releases src/modules/enrollment
```

Esperado: FAIL en los dos `clientSelection.test.tsx` y en las pruebas corregidas (la página usa todavía
`/me`'s `org_id` como sede, `"o"`, y no hay campos Cliente ni Sede).

- [ ] **Step 23: Implementar `AgentReleasesPage.tsx` y `ManagedDeploymentDialog.tsx`**

`AgentReleasesPage.tsx`, línea 14: sustituir `import { internalSiteId, requireKnown } from "../../lib/internalClient";` por

```tsx
import { ClientField } from "../../components/ClientField";
import { SiteField } from "../../components/SiteField";
import { requireKnown, useNewRowClient } from "../../lib/clientSelection";
```

y añadir, junto a las constantes del módulo, `const ENROLLMENT_MANAGE = "enrollment.manage";`. Sustituir las
líneas 196-202 (el comentario `// PHASE 3: …` y `const internalSite = internalSiteId(me.data);`) por

```tsx
  // WHERE NEW MACHINES GO (MSP clients spec §1.5, design §3.2). The install
  // command's token, the managed deployment's token and the install link each
  // belong to ONE SITE: the one chosen on this page, among the sites of the
  // client chosen at the top -- or, with every client, of the page's Client
  // field. `null` until chosen: nothing that needs it is sent before.
  const target = useNewRowClient(ENROLLMENT_MANAGE);
  const [pickedClient, setPickedClient] = useState<string | null>(null);
  const siteClient = target.needsField ? pickedClient : target.clientId;
  const [siteId, setSiteId] = useState<string | null>(null);
```

En `generate` (209-212): `api.generateAgentInstallCommand(release.id, TOKEN_TTL_HOURS, requireKnown(siteId)),`
sin el comentario `// PHASE 3: …`. Sustituir 310-318 (el comentario y el `InstallLinkPanel`) por

```tsx
      {/* WHERE NEW MACHINES GO: the site the install command, the install
          link and a managed deployment are for (design §3.2). */}
      <Panel title={t("agentReleases.where.title")} subtitle={t("agentReleases.where.subtitle")}>
        <div className="form-grid">
          {target.needsField ? (
            <ClientField
              code={ENROLLMENT_MANAGE}
              value={pickedClient}
              onChange={(id) => {
                setPickedClient(id);
                // Another client is another set of sites.
                setSiteId(null);
              }}
            />
          ) : null}
          <SiteField clientId={siteClient} value={siteId} onChange={setSiteId} />
        </div>
      </Panel>

      {/* The install link (spec 2026-09-28 §1.1): one shared link per site,
          off by default, for technicians setting up machines by hand. */}
      {canManageLink && siteId !== null && <InstallLinkPanel zone={zone} siteId={siteId} />}
```

y en `ManagedDeploymentDialog` (614-617) `siteId={siteId}` sin el comentario `// PHASE 3: …`.

`ManagedDeploymentDialog.tsx:47`: `import { requireKnown } from "../../lib/clientSelection";`. Líneas 132-133:
el comentario pasa a `// The site chosen on the page (design §3.2).`

- [ ] **Step 24: Implementar `EnrollmentPage.tsx`**

Línea 124: sustituir `import { internalSiteId, requireKnown } from "../../lib/internalClient";` por

```tsx
import { ClientField } from "../../components/ClientField";
import { SiteField } from "../../components/SiteField";
import { requireKnown, useNewRowClient } from "../../lib/clientSelection";
```

Tras `const [pendingIssue, setPendingIssue] = useState<TokenDraft | null>(null);` (401) añadir

```tsx
  // A token enrols machines into ONE SITE (MSP clients spec §1.5, design §3.2):
  // the one the form names, among the sites of the client chosen at the top
  // -- or, with every client, of the form's Client field.
  const target = useNewRowClient("enrollment.manage");
  const [pickedClient, setPickedClient] = useState<string | null>(null);
  const tokenClient = target.needsField ? pickedClient : target.clientId;
  const [siteId, setSiteId] = useState<string | null>(null);
```

En `create` (403-412): `site_id: requireKnown(siteId),` sin el comentario `// PHASE 3: …`. En `submitIssue`
(467): sustituir la línea del `Attempt` por

```tsx
    const next: Attempt = {
      rules: () => [
        ...nameAndLifetime(),
        maxUsesRule(),
        ...(target.needsField ? [{ name: "client", label: translate("enrollment.form.client") }] : []),
        { name: "site", label: translate("enrollment.form.site") },
      ],
      values: { ...form, client: tokenClient, site: siteId },
    };
```

En el formulario (752), antes del campo «Token name»:

```tsx
                {target.needsField ? (
                  <ClientField
                    code="enrollment.manage"
                    value={pickedClient}
                    onChange={(id) => {
                      setPickedClient(id);
                      setSiteId(null);
                    }}
                  />
                ) : null}
                <SiteField clientId={tokenClient} value={siteId} onChange={setSiteId} />
```

- [ ] **Step 25: Textos de agent-releases y enrollment**

`frontend/src/i18n/en/agentReleases.ts`, nuevo bloque al nivel de `page`:
`where: { title: "Where new machines go", subtitle: "The site an install command, the install link and a managed deployment enrol machines into." },`.
`frontend/src/i18n/es/agentReleases.ts`:
`where: { title: "Dónde van las máquinas nuevas", subtitle: "La sede en la que inscriben máquinas una instrucción de instalación, el enlace de instalación y un despliegue gestionado." },`.
`frontend/src/i18n/en/enrollment.ts`, en `form`: `client: "Client",` y `site: "Site",`.
`frontend/src/i18n/es/enrollment.ts`, en `form`: `client: "Cliente",` y `site: "Sede",`.

- [ ] **Step 26: Ejecutar y ver que pasan**

```bash
cd frontend && npx vitest run src/modules/agent-releases src/modules/enrollment && npx tsc -b
```

Esperado: PASS.

- [ ] **Step 27: Commit**

```bash
git add frontend/src/modules/agent-releases frontend/src/modules/enrollment frontend/src/i18n/en/agentReleases.ts frontend/src/i18n/es/agentReleases.ts frontend/src/i18n/en/enrollment.ts frontend/src/i18n/es/enrollment.ts
git commit -m "feat(console): install command, install link, managed deployment and tokens name a site

Each is one site's, chosen among the sites of the client selected at the
top, or of a required Client field with every client chosen. The
internal client's Principal site is no longer the only one usable
(design §3.2)."
```

#### Credentials y agent-push

- [ ] **Step 28: Las pruebas que fallan, credentials y agent-push**

Crear `frontend/src/modules/credentials/clientSelection.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, Credential, MeClient } from "../../lib/api";
import { CredentialsPage } from "./CredentialsPage";

// MSP clients phase 3, design §3.1 and §3.4: a push credential goes to the
// client chosen at the top, or to the dialog's Client field with every client;
// any other type serves every client and asks nothing; the list says whose
// each credential is.

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

const INTERNAL: MeClient = { id: "o1", name: "Novieri", kind: "internal", enabled_modules: [], permissions: ["credentials.manage"] };
const ACME: MeClient = { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: ["credentials.manage"] };

function selectClient(selected: string | null): void {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients: [INTERNAL, ACME], ready: true });
}

function credential(overrides: Partial<Credential> = {}): Credential {
  return {
    id: "11111111-1111-1111-1111-111111111111", name: "Branch installer", type: "push_install",
    device_group_id: null, client_id: "c-acme", created_at: "2026-08-01T00:00:00Z", last_used_at: null,
    has_secret: true, ...overrides,
  };
}

function renderPage(api: Partial<ApiClient>) {
  mockUseApi.mockReturnValue({
    me: () => Promise.resolve({ permissions: ["credentials.manage"], org_id: "o1", clients: [INTERNAL, ACME] }),
    ...api,
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <CredentialsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => selectClient("o1"));

describe("a push credential's client", () => {
  it("with every client chosen, asks for it for a push type only, and files the credential there", async () => {
    selectClient(null);
    const createCredential = vi.fn().mockResolvedValue(credential());
    const user = userEvent.setup();
    renderPage({ listCredentials: () => Promise.resolve([]), createCredential });
    await user.click(await screen.findByRole("button", { name: /new credential/i }));
    expect(screen.queryByLabelText("Client")).toBeNull();

    await user.selectOptions(screen.getByLabelText("Type"), "push_install");
    await user.type(screen.getByLabelText("Name"), "Branch installer");
    await user.type(screen.getByLabelText("Secret"), "s3cret");
    await user.click(screen.getByRole("button", { name: "Store" }));
    expect(await screen.findByText("Client is required")).toBeInTheDocument();
    expect(createCredential).not.toHaveBeenCalled();

    await user.selectOptions(screen.getByLabelText("Client"), "c-acme");
    await user.click(screen.getByRole("button", { name: "Store" }));
    await waitFor(() =>
      expect(createCredential).toHaveBeenCalledExactlyOnceWith({
        name: "Branch installer", type: "push_install", client_id: "c-acme", secret: "s3cret",
      }),
    );
  });

  it("shows whose each credential is with every client chosen, and every client's for a shared one", async () => {
    selectClient(null);
    renderPage({
      listCredentials: () =>
        Promise.resolve([credential(), credential({ id: "22222222-2222-2222-2222-222222222222", name: "Domain admin", type: "windows_admin", client_id: null })]),
    });
    const row = (await screen.findByText("Branch installer")).closest("tr") as HTMLElement;
    expect(screen.getByRole("columnheader", { name: "Client" })).toBeInTheDocument();
    expect(within(row).getByText("Acme")).toBeInTheDocument();
    const shared = screen.getByText("Domain admin").closest("tr") as HTMLElement;
    expect(within(shared).getByText("Every client")).toBeInTheDocument();
  });
});
```

(«Every client» es el texto inglés de `lib.clients.everyClient`, Task 8; si la Task 8 lo escribe de otra
forma, la prueba usa `translate("lib.clients.everyClient")`.)

Crear `frontend/src/modules/agent-push/__tests__/clientSelection.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, Device, MeClient } from "../../../lib/api";
import { PushAuthorizationsPage } from "../PushAuthorizationsPage";
import { PushInstallFlow } from "../PushInstallFlow";
import { AGENT_PUSH_SCOPE_MANAGE, type PushAuthorizationOut } from "../types";

// MSP clients phase 3, design §3.1, §3.3 and §3.4.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

const CODES = [AGENT_PUSH_SCOPE_MANAGE, "credentials.read", "agent.push.run"];
const INTERNAL: MeClient = { id: "o1", name: "Novieri", kind: "internal", enabled_modules: [], permissions: CODES };
const ACME: MeClient = { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: CODES };

function selectClient(selected: string | null): void {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients: [INTERNAL, ACME], ready: true });
}

function stubApi(overrides: Partial<Record<keyof ApiClient, unknown>> = {}) {
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions: CODES, clients: [INTERNAL, ACME] }),
    listPushAuthorizations: vi.fn().mockResolvedValue([]),
    createPushAuthorization: vi.fn().mockResolvedValue({}),
    listPushCredentialScopes: vi.fn().mockResolvedValue([]),
    listCredentials: vi.fn().mockResolvedValue([]),
    listSiteServers: vi.fn().mockResolvedValue([]),
    listAgentReleases: vi.fn().mockResolvedValue([]),
    listPushHostKeys: vi.fn().mockResolvedValue([]),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderIt(node: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/agent-push/authorizations"]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => selectClient("o1"));

describe("a push authorization", () => {
  it("with every client chosen, asks for the client and authorizes the prefix in it", async () => {
    selectClient(null);
    const api = stubApi();
    const user = userEvent.setup();
    renderIt(<PushAuthorizationsPage />);
    await user.type(await screen.findByPlaceholderText("10.4.7.0/24"), "10.4.7.0/24");
    await user.selectOptions(screen.getByLabelText("Client"), "c-acme");
    await user.click(screen.getByRole("button", { name: "Authorize" }));
    await waitFor(() =>
      expect(api.createPushAuthorization).toHaveBeenCalledExactlyOnceWith({
        client_id: "c-acme", prefix: "10.4.7.0/24", note: "", expires_at: null,
      }),
    );
  });

  it("shows whose each authorization is with every client chosen", async () => {
    selectClient(null);
    const grant = {
      id: "a1", client_id: "c-acme", prefix: "10.9.0.0/24", note: "", authorized_by: "u1",
      authorized_at: new Date().toISOString(), expires_at: null, revoked_at: null,
    } as unknown as PushAuthorizationOut;
    stubApi({ listPushAuthorizations: vi.fn().mockResolvedValue([grant]) });
    renderIt(<PushAuthorizationsPage />);
    const row = (await screen.findByText("10.9.0.0/24")).closest("tr") as HTMLElement;
    expect(screen.getByRole("columnheader", { name: "Client" })).toBeInTheDocument();
    expect(within(row).getByText("Acme")).toBeInTheDocument();
  });
});

describe("a push install offers only the machines' client's credentials", () => {
  function machine(id: string, clientId: string): Device {
    return { id, hostname: id, primary_ip: "10.4.7.9", os_family: "linux", architecture: "amd64", client_id: clientId } as unknown as Device;
  }
  const CREDENTIALS = [
    { id: "cr-o", name: "Own key", type: "push_install", device_group_id: null, client_id: "o1", created_at: null, last_used_at: null, has_secret: true },
    { id: "cr-a", name: "Acme key", type: "push_install", device_group_id: null, client_id: "c-acme", created_at: null, last_used_at: null, has_secret: true },
  ];

  it("lists the one client's credentials", async () => {
    stubApi({ listCredentials: vi.fn().mockResolvedValue(CREDENTIALS) });
    renderIt(<PushInstallFlow machines={[machine("m1", "c-acme")]} onRemove={() => {}} onClose={() => {}} />);
    await screen.findByRole("option", { name: "Acme key" });
    expect(screen.queryByRole("option", { name: "Own key" })).toBeNull();
  });

  it("offers none for machines of two clients, and says why", async () => {
    stubApi({ listCredentials: vi.fn().mockResolvedValue(CREDENTIALS) });
    renderIt(<PushInstallFlow machines={[machine("m1", "c-acme"), machine("m2", "o1")]} onRemove={() => {}} onClose={() => {}} />);
    expect(await screen.findByText("These machines belong to more than one client. Push to one client's machines at a time.")).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Acme key" })).toBeNull();
  });
});
```

- [ ] **Step 29: Las pruebas existentes de credentials y agent-push, corregidas**

`frontend/src/modules/credentials/CredentialsPage.test.tsx` (arnés del Step 1, `SCOPE_INTERNAL.id = "o1"`):

| Línea | Antes | Después |
|---|---|---|
| 43-44 | `// `org_id` is the internal client's id, which a push credential is filed` / `// under until the client selector exists (`lib/internalClient.ts`).` | `// A push credential goes to the client chosen at the top: here the` / `// internal one, `o1` (`selectClient` above).` |
| 703 | `it("files a push install credential under the internal client, and only a push credential", …)` | `it("files a push install credential under the client chosen at the top, and only a push credential", …)`; la afirmación `client_id: "o1"` no cambia |

`frontend/src/modules/agent-push/__tests__/pushPages.test.tsx` (arnés del Step 1, `"o1"`):

| Línea | Antes | Después |
|---|---|---|
| 266-269 | `it("authorizes the prefix in the internal client", …)` y su comentario «Until the client selector exists the grant is the internal client's, whose id is `/me`'s `org_id`.» | `it("authorizes the prefix in the client chosen at the top", …)` y «The grant is the client chosen at the top's: here the internal one, `o1`.»; la afirmación no cambia |
| 285-295 | `stubApi({ createPushAuthorization, me: vi.fn().mockResolvedValue({ permissions: [SCOPE_MANAGE] }) })` y `expect(await screen.findByText(/cannot tell which client this is for\. Nothing was sent/)).toBeTruthy();` | `mockClientScope.mockReturnValue({ selected: null, select: vi.fn(), clients: [], ready: false }); stubApi({ createPushAuthorization });` y `expect(await screen.findByText(new ClientNotChosen().message)).toBeTruthy();` (import `ClientNotChosen` de `../../../lib/clientSelection`); el nombre `"sends no grant before its client is known"` no cambia |

- [ ] **Step 30: Ejecutar y ver que fallan**

```bash
cd frontend && npx vitest run src/modules/credentials src/modules/agent-push
```

Esperado: FAIL en los dos `clientSelection.test.tsx` (sin campo «Client», sin columna, el flujo ofrece las
dos credenciales) y en el caso corregido de `pushPages.test.tsx:285` (el formulario aún lee `/me`).

- [ ] **Step 31: Implementar `CredentialsPage.tsx`**

Línea 17: sustituir `import { internalClientId, requireKnown } from "../../lib/internalClient";` por

```tsx
import { ClientField } from "../../components/ClientField";
import { useClientScope } from "../../lib/clientScope";
import { requireKnown, useNewRowClient } from "../../lib/clientSelection";
import { useClientName } from "../settings/api";
```

En `CredentialsPage`, tras `const zone = useOrgZone();` añadir
`const { selected } = useClientScope(); const clientName = useClientName(); const showClient = selected === null;`.
En la tabla (318): tras `<Th>{t("credentials.list.name")}</Th>` añadir `{showClient ? <Th>{t("credentials.list.client")}</Th> : null}`,
y en cada fila, tras su primera celda,
`{showClient ? <Td>{credential.client_id === null ? t("lib.clients.everyClient") : clientName(credential.client_id)}</Td> : null}`.

En `CreateCredentialDialog`: sustituir `const me = useQuery<Me>(…);` (549-550) por

```tsx
  // A PUSH CREDENTIAL HAS EXACTLY ONE CLIENT (MSP clients phase 2, Task 12):
  // every push made with it is made in that client -- the one chosen at the
  // top, or with every client the Client field's (design §3.1). Any other
  // type is sent without one and serves every client, as before.
  const pushable = PUSHABLE_CREDENTIAL_TYPES.includes(type);
  const target = useNewRowClient("credentials.manage");
  const [pickedClient, setPickedClient] = useState<string | null>(null);
  const credentialClient = target.needsField ? pickedClient : target.clientId;
```

el cuerpo (565-572) pasa a `...(pushable ? { client_id: requireKnown(credentialClient) } : {}),` sin el comentario
anterior; en `storeCredential` (609-610):

```tsx
    const values = { name, account, secret: secret.trim() === "" ? "" : "x", client: credentialClient };
    const next: Attempt = {
      rules: () => [
        ...(isWindowsDomain ? windowsDomainRules : createRules)(),
        ...(pushable && target.needsField ? [{ name: "client", label: translate("credentials.create.client") }] : []),
      ],
      values,
    };
```

y en el formulario, justo después del campo «Type»:

```tsx
        {pushable && target.needsField ? (
          <ClientField code="credentials.manage" value={pickedClient} onChange={setPickedClient} />
        ) : null}
```

Si tras el cambio `useQuery` o `Me` quedan sin uso en el archivo, se quitan de sus imports; si falta
`translate`, se importa de `../../i18n/core`.

- [ ] **Step 32: Implementar agent-push**

`PushAuthorizationsPage.tsx:11`: sustituir `import { internalClientId, requireKnown } from "../../lib/internalClient";` por

```tsx
import { ClientField } from "../../components/ClientField";
import { useClientScope } from "../../lib/clientScope";
import { requireKnown, useNewRowClient } from "../../lib/clientSelection";
import { useClientName } from "../settings/api";
```

e importar `AGENT_PUSH_SCOPE_MANAGE` de `./types`. En el componente de la lista (el que pinta la tabla de
136-150): `const { selected } = useClientScope(); const clientName = useClientName(); const showClient = selected === null;`,
tras `<th scope="col">{t("agentPush.shared.prefix")}</th>` añadir
`{showClient ? <th scope="col">{t("agentPush.authorizations.colClient")}</th> : null}`, y `GrantRow` (181) recibe
dos props nuevas, `showClient: boolean` y `clientName: (id: string) => string`, y tras su primera celda
pinta `{showClient ? <td>{clientName(grant.client_id)}</td> : null}` (`GrantRow` llama `grant` a la fila, `PushAuthorizationsPage.tsx:182`).

En `GrantForm` (259-400): sustituir `const me = useQuery<Me>(…);` por

```tsx
  // A prefix is one client's network (MSP clients phase 2, Task 12): the
  // client chosen at the top, or with every client the Client field's.
  const target = useNewRowClient(AGENT_PUSH_SCOPE_MANAGE);
  const [pickedClient, setPickedClient] = useState<string | null>(null);
  const grantClient = target.needsField ? pickedClient : target.clientId;
```

en la `<div className="form-grid">`, antes del campo de prefijo:

```tsx
        {target.needsField ? (
          <ClientField code={AGENT_PUSH_SCOPE_MANAGE} value={pickedClient} onChange={setPickedClient} />
        ) : null}
```

y el cuerpo pasa a `client_id: requireKnown(grantClient),`, con el comentario «A prefix is one client's
network: the grant authorizes pushes to that client's machines only.» en lugar del bloque `// PHASE 3: …`.
`useQuery` y `Me` salen de los imports si quedan sin uso.

`api.ts:76-81`: el comentario pasa a «The body is BUILT when the mutation runs, not when the button is
wired: its `client_id` may not be known yet, and `requireKnown` throwing there is a refusal the form
shows, not an exception in a click handler (`lib/clientSelection.ts`).»

`PushInstallFlow.tsx` (66-80): tras `const kind = …` sustituir la línea de `offered` por

```tsx
  // A push credential is ONE client's, and the server refuses it for another
  // client's machine (design §3.3): only the machines' client's are offered,
  // and none when the machines are of more than one client.
  const machineClients = [...new Set(machines.map((m) => m.client_id ?? ""))];
  const oneClient = machineClients.length === 1 ? machineClients[0] : null;
  const offered = (credentials.data ?? []).filter((c) => c.type === kind && oneClient !== null && c.client_id === oneClient);
```

y justo antes del `<select>` de credencial (≈160):

```tsx
            {machineClients.length > 1 ? (
              <p className="notice gold">{t("agentPush.flow.severalClients")}</p>
            ) : null}
```

- [ ] **Step 33: Textos de credentials y agent-push**

`frontend/src/i18n/en/credentials.ts`: en `list`, `client: "Client",`; en `create`, `client: "Client",`.
`frontend/src/i18n/es/credentials.ts`: en `list`, `client: "Cliente",`; en `create`, `client: "Cliente",`.
`frontend/src/i18n/en/agentPush.ts`: en `authorizations`, `colClient: "Client",`; en `flow`,
`severalClients: "These machines belong to more than one client. Push to one client's machines at a time.",`.
`frontend/src/i18n/es/agentPush.ts`: en `authorizations`, `colClient: "Cliente",`; en `flow`,
`severalClients: "Estas máquinas son de más de un cliente. Instala por push en las de un solo cliente cada vez.",`.

- [ ] **Step 34: Ejecutar y ver que pasan**

```bash
cd frontend && npx vitest run src/modules/credentials src/modules/agent-push && npx tsc -b
```

Esperado: PASS.

- [ ] **Step 35: Commit**

```bash
git add frontend/src/modules/credentials frontend/src/modules/agent-push frontend/src/i18n/en/credentials.ts frontend/src/i18n/es/credentials.ts frontend/src/i18n/en/agentPush.ts frontend/src/i18n/es/agentPush.ts
git commit -m "feat(console): push credentials and push authorizations in the right client

A push credential and a push authorization go to the client selected at
the top, or a required Client field with every client chosen; both lists
show a Client column then; a push install offers only the machines'
client's credentials (design §3.1, §3.3, §3.4)."
```

#### Backup

- [ ] **Step 36: Las pruebas que fallan, backup**

Crear `frontend/src/modules/backup/__tests__/clientSelection.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, MeClient } from "../../../lib/api";
import { BackupPolicyEditor } from "../BackupPolicyEditor";
import { RepositoriesPage } from "../RepositoriesPage";
import type { Repository } from "../types";

// MSP clients phase 3, design §3.1, §3.3, §3.4.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

const CODES = ["backup.read", "backup.run", "backup.restore", "backup.policy.manage"];
const INTERNAL: MeClient = { id: "o1", name: "Novieri", kind: "internal", enabled_modules: [], permissions: CODES };
const ACME: MeClient = { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: CODES };

function selectClient(selected: string | null): void {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients: [INTERNAL, ACME], ready: true });
}

function repository(overrides: Partial<Repository>): Repository {
  return { id: "r-o", client_id: "o1", name: "Own vault", storage_ref: "s3://own", state: "active", dedup_params: {}, created_at: "2026-08-01T00:00:00Z", ...overrides };
}

function stubApi(overrides: Partial<Record<keyof ApiClient, unknown>> = {}) {
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions: CODES, clients: [INTERNAL, ACME] }),
    listBackupRepositories: vi.fn().mockResolvedValue([repository({}), repository({ id: "r-a", client_id: "c-acme", name: "Acme vault" })]),
    listBackupPolicies: vi.fn().mockResolvedValue([]),
    listDeviceGroups: vi.fn().mockResolvedValue({ items: [], total: 0, ungrouped_device_count: 0, evaluation: null }),
    createBackupRepository: vi.fn().mockResolvedValue(repository({})),
    upsertBackupPolicy: vi.fn().mockResolvedValue({}),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderIt(node: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/backup"]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => selectClient("o1"));

describe("a new repository", () => {
  it("with every client chosen, asks for the client and files the repository there", async () => {
    selectClient(null);
    const api = stubApi();
    const user = userEvent.setup();
    renderIt(<RepositoriesPage />);
    await user.click(await screen.findByRole("button", { name: /new repository/i }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Name"), "Acme offsite");
    await user.selectOptions(within(dialog).getByLabelText("Client"), "c-acme");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(api.createBackupRepository).toHaveBeenCalled());
    expect(api.createBackupRepository.mock.calls[0]?.[0]).toMatchObject({ client_id: "c-acme", name: "Acme offsite" });
  });

  it("shows whose each repository is with every client chosen", async () => {
    selectClient(null);
    stubApi();
    renderIt(<RepositoriesPage />);
    const row = (await screen.findByText("Acme vault")).closest("tr") as HTMLElement;
    expect(screen.getByRole("columnheader", { name: "Client" })).toBeInTheDocument();
    expect(within(row).getByText("Acme")).toBeInTheDocument();
  });
});

describe("a new client-default policy", () => {
  it("with every client chosen, asks for the client and offers only its repositories", async () => {
    selectClient(null);
    const api = stubApi();
    const user = userEvent.setup();
    renderIt(<BackupPolicyEditor />);
    await user.click(await screen.findByRole("button", { name: /new policy/i }));
    const dialog = await screen.findByRole("dialog");
    await user.selectOptions(within(dialog).getByLabelText("Client"), "c-acme");
    await user.click(within(dialog).getByRole("combobox", { name: /Repository/ }));
    await user.click(await screen.findByRole("option", { name: /Acme vault/ }));
    expect(screen.queryByRole("option", { name: /Own vault/ })).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.upsertBackupPolicy).toHaveBeenCalled());
    expect(api.upsertBackupPolicy.mock.calls[0]?.[0]).toMatchObject({ device_group_id: null, client_id: "c-acme", repository_id: "r-a" });
  });
});
```

Los nombres salen del catálogo: «New repository» (`backup.repositories.newRepository`), «Create»
(`backup.repositories.create`), «New policy» (`backup.policies.newPolicy`), «Repository»
(`backup.policyDialog.repository`, un `EntityPicker`) y «Save» (`backup.policyDialog.save`); los métodos,
de `lib/api.ts:2878-2893`.

- [ ] **Step 37: Las pruebas existentes de backup, corregidas**

`frontend/src/modules/backup/__tests__/backup.test.tsx` (arnés del Step 1, `"o1"`):

| Línea | Antes | Después |
|---|---|---|
| 881 | `it("posts null for the device group, and the internal client, when the client default is left chosen", …)` | `it("posts null for the device group, and the client chosen at the top, when the client default is left chosen", …)` |
| 893-894 | `// PHASE 3: no client selector yet, so a new client default is the` + continuación | `// A new client default is the client chosen at the top's: here the` / `// internal one, `o1`.` (la afirmación `client_id: "o1"` no cambia) |
| 1036 | `// The client default: the internal client's (PHASE 3), whose repository` | `// The client default: the selected client's (here the internal one), whose repository` |
| 1301 | `it("files a new repository under the internal client", …)` | `it("files a new repository under the client chosen at the top", …)` |
| 1309 | `// PHASE 3: the internal client -- `/me`'s `org_id` -- until the selector.` | `// The client chosen at the top: here the internal one, `o1`.` |
| 1813-1814 | `expect(repositoryPickerEmptyHintElsewhere()).not.toMatch(/Repositories tab/);` / `….toMatch(/permission/);` | sin cambios: el texto nuevo (Step 39) sigue sin «Repositories tab» y con «permission» |

- [ ] **Step 38: Ejecutar y ver que fallan**

```bash
cd frontend && npx vitest run src/modules/backup
```

Esperado: FAIL en `clientSelection.test.tsx` (sin campo «Client», sin columna, la política nueva se queda
con los repositorios del cliente interno).

- [ ] **Step 39: Implementar backup**

`RepositoriesPage.tsx:29`: sustituir el import de `internalClient` por

```tsx
import { ClientField } from "../../components/ClientField";
import { useClientScope } from "../../lib/clientScope";
import { requireKnown, useNewRowClient } from "../../lib/clientSelection";
import { useClientName } from "../settings/api";
```

En `RepositoriesPage`: `const { selected } = useClientScope(); const clientName = useClientName(); const showClient = selected === null;`;
tabla (156-180): tras `<Th>{t("backup.repositories.colName")}</Th>` añadir
`{showClient ? <Th>{t("backup.repositories.colClient")}</Th> : null}`, y tras la primera `</Td>` de cada fila
`{showClient ? <Td>{clientName(repo.client_id)}</Td> : null}`. El `<CreateRepositoryDialog …>` (190-202) pierde
la prop `clientId` y su comentario `// PHASE 3: …`. `CreateRepositoryDialog` (209-240):

```tsx
function CreateRepositoryDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT();
  const create = useCreateBackupRepositoryMutation();
  const [name, setName] = useState("");
  // A repository is ONE client's: its key travels to every device that backs
  // up into it (MSP clients phase 2, Task 13). Nothing on this page is a row
  // to take it from, so it is the client chosen at the top, or with every
  // client the dialog's Client field (design §3.1).
  const target = useNewRowClient("backup.policy.manage");
  const [pickedClient, setPickedClient] = useState<string | null>(null);
  const clientId = target.needsField ? pickedClient : target.clientId;
  // Populated ON SUBMIT, never while typing. Held as data (rules and values)
  // so a language switch re-words the sentences.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function submit() {
    // The previous outcome must not be readable as this one.
    create.reset();
    const next: Attempt = {
      rules: () => [
        ...repositoryRules(),
        ...(target.needsField ? [{ name: "client", label: translate("backup.repositories.client") }] : []),
      ],
      values: { name, client: clientId },
    };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    create.mutate(
      () => ({ client_id: requireKnown(clientId), name: name.trim() }),
      {
        onSuccess: () => {
          setName("");
          onClose();
        },
      },
    );
  }
```

y en su formulario, antes del campo Nombre:
`{target.needsField ? <ClientField code="backup.policy.manage" value={pickedClient} onChange={setPickedClient} /> : null}`.

`BackupPolicyEditor.tsx:35`: sustituir el import por

```tsx
import { ClientField } from "../../components/ClientField";
import { useClientScope } from "../../lib/clientScope";
import { requireKnown, useNewRowClient } from "../../lib/clientSelection";
import { useClientName } from "../settings/api";
```

Lista de políticas (205-260): `const { selected } = useClientScope(); const clientName = useClientName(); const showClient = selected === null;`
en el componente de la tabla; tras `<Th>{t("backup.policies.colScope")}</Th>` añadir
`{showClient ? <Th>{t("backup.policies.colClient")}</Th> : null}`, y tras la primera celda de cada fila
`{showClient ? <Td>{policy.client_id ? clientName(policy.client_id) : t("lib.clients.everyClient")}</Td> : null}`
(la tabla llama `policy` a la fila, `BackupPolicyEditor.tsx:219`).

`PolicyDialog` (291-372): quitar `const me = useQuery<Me>(…);` y sustituir el bloque de 314-334 (desde el
comentario «The client this policy is…» hasta `const elsewhere = …`) por

```tsx
  // The client this policy is, from the row where there is one: the policy
  // being edited, or the group chosen. A new client default has no row: it is
  // the client chosen at the top, or with every client the Client field's
  // (design §3.1).
  const groupClient = deviceGroupId
    ? (groups.data ?? []).find((group) => group.id === deviceGroupId)?.client_id
    : undefined;
  const target = useNewRowClient("backup.policy.manage", groupClient);
  const [pickedClient, setPickedClient] = useState<string | null>(null);
  const newClient = target.needsField ? pickedClient : target.clientId;
  const client = policyClient(existing, deviceGroupId, groups.data ?? [], newClient);
  const clientRepositories = repositoriesOfClient(repositories.data ?? [], client);
  const repositoryOptions = repositoryPickerOptions(clientRepositories);
  // A repository of another client is not this policy's to name -- one a
  // stored policy kept from before the rule, or one picked before the group
  // changed -- so it is not selected, and Save asks for one of this client's.
  const chosenRepository = clientRepositories.some((repo) => repo.id === repositoryId)
    ? repositoryId
    : "";
  // Gated on a settled query so a slow fetch never reads as an empty one.
  const noRepositories = repositories.isSuccess && clientRepositories.length === 0;
  // The Repositories page files a repository only in a client where this
  // person may manage policies; for a policy of any other client the hint
  // says so, and there is no link.
  const elsewhere = client !== null && !target.options.some((option) => option.id === client);
```

Antes del selector de grupo en el formulario:

```tsx
        {!existing && deviceGroupId === "" && target.needsField ? (
          <ClientField
            code="backup.policy.manage"
            value={pickedClient}
            onChange={(id) => {
              setPickedClient(id);
              // Another client is another set of repositories.
              setRepositoryId("");
            }}
          />
        ) : null}
```

`useQuery` y `Me` salen de los imports si quedan sin uso.

`pickerOptions.ts:31-39`: el comentario pasa a «The same empty list for a policy of a client this person
cannot file a repository under (they may not manage that client's policies): the hint says so instead,
and the editor offers no link.» `frontend/src/i18n/en/backup.ts:81-82`:
`repositoryEmptyHintElsewhere: "No backup repository of this policy’s client exists, and you cannot create one in that client: ask someone who manages its backups. You may also not have permission to list them.",`.
`frontend/src/i18n/es/backup.ts:80`, el texto equivalente:
`repositoryEmptyHintElsewhere: "No existe ningún repositorio de respaldo del cliente de esta política, y no puedes crear uno en ese cliente: pídeselo a quien gestiona sus respaldos. También puede que no tengas permiso para verlos.",`.

`backup/api.ts:60-66`: en el comentario, `(`requireKnown` before `/me` answers)` pasa a
`(`requireKnown` while the client is not chosen, `lib/clientSelection.ts`)`.

Textos nuevos: `frontend/src/i18n/en/backup.ts`, en `repositories`, `colClient: "Client",` y `client: "Client",`;
en `policies`, `colClient: "Client",`. `frontend/src/i18n/es/backup.ts`: los mismos con `"Cliente"`.

- [ ] **Step 40: Ejecutar y ver que pasan**

```bash
cd frontend && npx vitest run src/modules/backup && npx tsc -b
```

Esperado: PASS.

- [ ] **Step 41: Commit**

```bash
git add frontend/src/modules/backup frontend/src/i18n/en/backup.ts frontend/src/i18n/es/backup.ts
git commit -m "feat(console): backup repositories and client defaults in the right client

A new repository and a new client-default policy go to the client
selected at the top, or a required Client field with every client
chosen; the policy's repository picker lists that client's; both lists
show a Client column then (design §3.1, §3.3, §3.4)."
```

- [ ] **Step 42: Verificación de la Task 10**

Ningún archivo de estas siete carpetas llama ya a `lib/internalClient`:

```bash
cd frontend && grep -rn "internalClient\|PHASE 3" src/modules/{ipam,devices,agent-releases,enrollment,credentials,agent-push,backup}
```

Esperado: ninguna línea.

```bash
cd frontend && npx vitest run && npx tsc -b && npm run build
```

Esperado: la suite completa en verde (la de vocabulario de auditoría lee el backend, sin cambios aquí),
`tsc` sin errores y el build terminado. La Task 10 no toca el backend: no hace falta la suite de pytest.

---

### Task 11: Consola, grupo B — el cliente de cada fila nueva en budget, settings, docs, patch, sla, subscriptions, licenses, change y bcp

Diseño: §3.1, §3.3, §3.4 y §3.5. Corre en paralelo con la Task 10, en su propia worktree, después de la
Task 9. Toca solo las carpetas de módulo de abajo, sus catálogos `i18n/{en,es}/<módulo>.ts` y sus pruebas,
más un comentario en `modules/people/api.ts`. **No toca `lib/api.ts`, `lib/internalClient.ts` (lo borra la
Task 12), `components/forEveryClient.tsx` ni ningún otro archivo compartido.**

**Files:**
- Modify: `frontend/src/modules/budget/api.ts:9-12, 31-55` (`useBudgetClient`), `:107-111, 150-154, 224-228, 351-355, 421-425` (comentarios `PHASE 3`),
  `budgetState.tsx` (gana `useBudgetNeedsClient` y `BudgetPickClient`), `BudgetDashboard.tsx:105-194`,
  `BudgetPlannerPage.tsx:78-92`, `ExpensesPage.tsx:85-109`, `ConfirmationQueuePage.tsx:25-54`,
  `BudgetAdminPage.tsx:104-130`, `types.ts:16-21, 307-308`, `fixtures.ts:54-56`
- Modify: `frontend/src/modules/settings/BudgetTab.tsx:21, 102-110, 140-150`, `settings/api.ts:36, 400-430`,
  `settings/UsersTab.tsx:16-17` (comentario)
- Modify: `frontend/src/modules/docs/DocsPage.tsx:13-16, 248-262, 306-316, 872-883, 1048-1072, 1140-1150`,
  `docs/documents.tsx:44, 870-900, 978-988`
- Modify: `frontend/src/modules/patch/DecisionBar.tsx:1-20, 112-175, 200-250, 365-385`
- Modify: `frontend/src/modules/sla/PolicyResolverPanel.tsx:1-12, 36-45, 56-60`
- Modify: `frontend/src/modules/subscriptions/SubscriptionsPage.tsx:37, 385-420, 520-545` (y el JSX del diálogo),
  `subscriptions/api.ts:50-55` (comentario)
- Modify: `frontend/src/modules/licenses/LicensesPage.tsx:42, 352-380, 480-500` (y el JSX del diálogo),
  `licenses/api.ts:108-113` (comentario)
- Modify: `frontend/src/modules/change/ChangeRequestForm.tsx:11, 140-200`, `change/ChangesPage.tsx:205-230`,
  `change/api.ts:58-63` (comentario)
- Modify: `frontend/src/modules/bcp/PlanEditor.tsx:12, 145-195, 265-276` (y el JSX), `bcp/PlansPage.tsx:250-290`,
  `bcp/api.ts:59-64` (comentario)
- Modify: `frontend/src/modules/people/api.ts:80-84` (comentario)
- Modify: `frontend/src/i18n/en/{budget,settings,patch,sla,subscriptions,licenses,change,bcp,docs}.ts` y sus pares en `es/`
- Modify (pruebas corregidas): `budget/__tests__/budget.test.tsx:515-520, 543, 592, 2015-2020, 2032, 2078`,
  `budget/budget.es.test.tsx:543`, `settings/__tests__/budget.test.tsx:118-122, 174-175`,
  `docs/docsClient.test.tsx:61, 139, 163`, `docs/DocsPage.test.tsx:371-373`, `patch/__tests__/patch.test.tsx:600, 644, 3229, 3282, 3291`,
  `patch/__tests__/changeLink.test.tsx:31-33`, `subscriptions/__tests__/subscriptions.test.tsx:480`,
  `change/__tests__/change.test.tsx:383-385`, `bcp/__tests__/bcp.test.tsx:300`
- Modify (solo el arnés del selector, Step 1): todo `*.test.tsx` bajo `frontend/src/modules/{budget,docs,patch,sla,subscriptions,licenses,change,bcp}/`,
  más `settings/__tests__/budget.test.tsx` y `settings/settings.es.test.tsx`
- Create: `frontend/src/modules/budget/__tests__/clientSelection.test.tsx`,
  `frontend/src/modules/sla/__tests__/resolverClient.test.tsx`

**Interfaces:**
- Consumes: lo mismo que la Task 10 (sus «Consumes» de las Tasks 7, 8 y 9, con las mismas suposiciones).
- Produces: `useBudgetNeedsClient(): boolean` y `BudgetPickClient()` en `budget/budgetState.tsx`.
  `useBudgetClient(): string | null` sigue, y ahora devuelve `useClientScope().selected`.

**El arnés de las pruebas:** el mismo de la Task 10, con `SCOPE_INTERNAL.id` igual al `org_id` del `/me` de
cada archivo (`"o1"` en todos estos módulos; `"org-1"` en `settings/__tests__/budget.test.tsx`, cuyo `/me`
viene de `settings/__tests__/harness.tsx`). En `patch` el `SCOPE_INTERNAL` lleva `kind: "internal"`: es lo
que mantiene la frase «your organisation's own client» en las pruebas que la fijan (D11.3).

**Decisiones del plan de esta tarea:**
- **D11.1.** Presupuesto trabaja con un cliente a la vez. Sus importes están en la moneda base de cada
  cliente y nunca se suman entre clientes (`settings/api.ts:617-622`), así que con «Todos mis clientes» sus
  cinco páginas y la pestaña Presupuesto de Ajustes piden elegir un cliente arriba en lugar de mostrar datos
  o formularios. Con un cliente elegido, toda fila nueva es suya (regla 2 del §3.1) y los selectores de
  categoría y centro de coste son suyos (§3.3). Por eso el presupuesto no lleva campo Cliente ni columna
  Cliente. Los tipos de cambio de `/budget/admin` son configuración global y se ven siempre.
- **D11.2.** El campo Cliente de cambios y de continuidad no filtra por módulo activo (`ClientField` no
  recibe módulo, Task 9). Un cliente sin el módulo da el 404 con frase de la fase 2, que el formulario
  muestra tal cual.
- **D11.3.** La barra de aprobación de parches sigue llamando «your organisation's own client» al cliente
  `internal` de `/me.clients`; a cualquier otro, por su nombre; y, mientras su campo Cliente esté vacío,
  «the client you choose».
- **D11.4.** La vista previa de SLA, con «Todos mis clientes», sigue previsualizando las políticas globales
  (no hay un cliente que elegir) y lo dice en una nota; con un cliente elegido, usa ese con la regla D2.
- **D11.5.** Documentación no gana columna Cliente: sus listas (artículos, registros, documentos) no son
  tablas con columnas, y el §3.5 la deja como está. Gana el campo Cliente en sus tres altas.
- **D11.6.** `people/links.tsx` no cambia: ya envía el cliente del equipo y no usa `requireKnown`. Solo se
  corrige el comentario de `people/api.ts:80-84`, que nombra `InternalScopeUnknown` y `lib/internalClient.ts`.
- **D11.7.** `settings/UsersTab.tsx:16-17` lleva un marcador `PHASE 3` que la prueba de la Task 12 rechazaría;
  esta tarea solo reescribe ese comentario. El cambio del editor de usuarios es de la Task 13.
- **D11.8.** Las columnas Cliente usan una clave por módulo: `subscriptions.list.colClient`,
  `licenses.list.colClient`, `change.list.client`, `bcp.plans.colClient` («Client» / «Cliente»).

---

- [ ] **Step 1: El arnés del selector en las pruebas existentes**

Igual que el Step 1 de la Task 10, en cada `*.test.tsx` bajo
`frontend/src/modules/{budget,docs,patch,sla,subscriptions,licenses,change,bcp}/` y en
`settings/__tests__/budget.test.tsx` y `settings/settings.es.test.tsx`. Después:

```bash
cd frontend && npx vitest run src/modules/budget src/modules/settings src/modules/docs src/modules/patch src/modules/sla src/modules/subscriptions src/modules/licenses src/modules/change src/modules/bcp
```

Esperado: PASS, el mismo número de pruebas que antes.

#### Presupuesto y la pestaña Presupuesto de Ajustes

- [ ] **Step 2: Las pruebas que fallan, presupuesto**

Crear `frontend/src/modules/budget/__tests__/clientSelection.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, MeClient } from "../../../lib/api";
import { BudgetAdminPage } from "../BudgetAdminPage";
import { BudgetDashboard } from "../BudgetDashboard";
import { ExpensesPage } from "../ExpensesPage";

// MSP clients phase 3 (plan Task 11 D11.1): budget is one client's, in its own
// base currency. With a client chosen at the top every read names it; with
// every client chosen the pages ask for one and read nothing, and the global
// exchange rates stay on /budget/admin.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

const CODES = ["budget.read", "budget.manage"];
const INTERNAL: MeClient = { id: "o1", name: "Novieri", kind: "internal", enabled_modules: ["budget"], permissions: CODES };
const ACME: MeClient = { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: ["budget"], permissions: CODES };

function selectClient(selected: string | null): void {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients: [INTERNAL, ACME], ready: true });
}

function stubApi() {
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions: CODES, clients: [INTERNAL, ACME] }),
    getBudgetSettings: vi.fn().mockResolvedValue({ client_id: "c-acme", base_currency: "EUR", fiscal_year_start_month: 1, updated_by: null, updated_at: null }),
    listBudgetCategories: vi.fn().mockResolvedValue([]),
    listCostCenters: vi.fn().mockResolvedValue([]),
    listBudgets: vi.fn().mockResolvedValue([]),
    listExpenses: vi.fn().mockResolvedValue([]),
    listFxRates: vi.fn().mockResolvedValue([]),
    listSubscriptions: vi.fn().mockResolvedValue([]),
    listLicenses: vi.fn().mockResolvedValue([]),
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderAt(node: React.ReactNode, entry: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => selectClient("o1"));

describe("budget follows the client chosen at the top", () => {
  it("reads that client's settings and expenses", async () => {
    selectClient("c-acme");
    const api = stubApi();
    renderAt(<ExpensesPage />, "/budget/expenses");
    await waitFor(() => expect(api.listExpenses).toHaveBeenCalledWith(expect.anything(), "c-acme"));
    expect(screen.queryByText("Choose a client at the top of the page")).toBeNull();
  });

  it("with every client chosen, asks for one and reads nothing of any client", async () => {
    selectClient(null);
    const api = stubApi();
    renderAt(<BudgetDashboard />, "/budget");
    expect(await screen.findByText("Choose a client at the top of the page")).toBeInTheDocument();
    expect(api.getBudgetSettings).not.toHaveBeenCalled();
    expect(api.listBudgets).not.toHaveBeenCalled();
  });

  it("keeps the global exchange rates on the admin page with every client chosen", async () => {
    selectClient(null);
    const api = stubApi();
    renderAt(<BudgetAdminPage />, "/budget/admin");
    expect(await screen.findByText("Choose a client at the top of the page")).toBeInTheDocument();
    await waitFor(() => expect(api.listFxRates).toHaveBeenCalled());
    expect(api.listBudgetCategories).not.toHaveBeenCalled();
    expect(api.getBudgetSettings).not.toHaveBeenCalled();
  });
});
```

(`listFxRates`, `listSubscriptions` y `listLicenses` son métodos que el módulo ya llama, `budget/api.ts`; la
simulación los responde para que ninguna lectura de la página falle.)

En `frontend/src/modules/settings/__tests__/budget.test.tsx` (arnés con `SCOPE_INTERNAL.id = "org-1"`),
añadir dentro de su `describe` de la pestaña:

```tsx
  it("asks for a client at the top, and reads nothing, with every client chosen", async () => {
    selectClient(null);
    renderSettings();
    await openTab();
    expect(await screen.findByText(/choose a client at the top of the page/i)).toBeInTheDocument();
    expect(calls.getBudgetSettings).not.toHaveBeenCalled();
  });
```

- [ ] **Step 3: Las pruebas existentes de presupuesto, corregidas**

`frontend/src/modules/budget/__tests__/budget.test.tsx` (arnés, `"o1"`):

| Línea | Antes | Después |
|---|---|---|
| 515-516 | `// The internal client's subscriptions and licenses (PHASE 3: the` + continuación | `// The selected client's subscriptions and licenses (here the internal` / `// one, `o1`).` |
| 543 | `// `/me` already answered: the expense is the internal client's.` | `// The expense is the client chosen at the top's: here the internal one.` |
| 592 | `// `/me` already answered: the materialization is the internal client's.` | `// The materialization is the client chosen at the top's.` |
| 2015-2020 (comentario del `describe`) | «… the client selector exists (PHASE 3) this console works on the internal …» | «Budget works on the client chosen at the top (MSP clients phase 3, plan Task 11 D11.1); these tests choose the internal one, `o1`.» |
| 2032 | `it("files every new row under the internal client", …)` | `it("files every new row under the client chosen at the top", …)`; la afirmación `client_id: "o1"` no cambia |
| 2078 | `it("reads the internal client's settings and lists", …)` | `it("reads the selected client's settings and lists", …)`; las afirmaciones `("o1")` no cambian |

`frontend/src/modules/budget/budget.es.test.tsx:543`: el comentario «The categories are the internal client's,
read once `/me` has named it» pasa a «Las categorías son del cliente elegido arriba (aquí el interno, `o1`).»

`frontend/src/modules/settings/__tests__/budget.test.tsx`:

| Línea | Antes | Después |
|---|---|---|
| 118-121 | comentario «Until the selector exists (PHASE 3) this tab shows the internal client's, whose id is `/me`'s `org_id`.» y `it("reads the internal client's settings", …)` | «This tab shows the client chosen at the top's (plan Task 11 D11.1); here the internal one, `org-1`.» y `it("reads the selected client's settings", …)`; las afirmaciones `("org-1")` no cambian |
| 174-175 | `// The internal client's settings: its id is `/me`'s `org_id` (PHASE 3:` / `// the selector's).` | `// The selected client's settings: here the internal one, `org-1`.` |

- [ ] **Step 4: Ejecutar y ver que fallan**

```bash
cd frontend && npx vitest run src/modules/budget src/modules/settings/__tests__/budget.test.tsx
```

Esperado: FAIL en los tres casos de `clientSelection.test.tsx` y en el caso nuevo de la pestaña: las páginas
leen todavía el cliente interno desde `/me` y no piden elegir.

- [ ] **Step 5: Implementar presupuesto**

`budget/api.ts`: imports (9-12)

```ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "../../lib/ApiContext";
import { useClientScope } from "../../lib/clientScope";
import { loadingUntilClient, requireKnown } from "../../lib/clientSelection";
```

(`import type { Me }` sale: solo lo usaba `useBudgetClient`). Sustituir el comentario y el cuerpo de
`useBudgetClient` (35-55) por:

```ts
/**
 * BUDGET IS ONE CLIENT'S (MSP clients phase 2, plan 2026-10-26 Task 8): its
 * settings, categories, cost centres, budgets and expenses each belong to a
 * client, in that client's base currency, and budget is a module a client may
 * not use. Every hook below works on the client this returns: reads filter by
 * it (and wait for it -- `enabled`), and creates name it.
 *
 * It is the client chosen at the top (phase 3). With every client chosen it is
 * `null`: no read is sent, the pages ask for a client instead
 * (`budgetState.tsx`'s `useBudgetNeedsClient`), and a create would throw
 * `ClientNotChosen` inside its mutation (`requireKnown`) -- nothing is sent.
 */
export function useBudgetClient(): string | null {
  return useClientScope().selected;
}
```

En 110, 153, 227, 354 y 424, el comentario `// PHASE 3: the selected client; until then the internal one (`useBudgetClient`).`
pasa a `// The client chosen at the top (`useBudgetClient`).`

`budgetState.tsx`: añadir a sus imports `import { EmptyState } from "../../components/emptystate";` y
`import { useClientScope } from "../../lib/clientScope";`, y al final:

```tsx
/**
 * True when a budget page has nothing to show: every client is chosen at the
 * top. Budget figures are one client's, in its base currency, and are never
 * added across clients (plan Task 11 D11.1). False until `/me` has answered,
 * so the pages keep their own loading state meanwhile.
 */
export function useBudgetNeedsClient(): boolean {
  const { selected, ready } = useClientScope();
  return ready && selected === null;
}

/** What a budget page says instead of its figures with every client chosen. */
export function BudgetPickClient() {
  const t = useT();
  return <EmptyState icon="building" title={t("budget.pickClient.title")} body={t("budget.pickClient.body")} />;
}
```

(si `useT` no está importado en `budgetState.tsx`, se importa de `../../i18n/context`).

En cada una de las cuatro páginas, añadir `useBudgetNeedsClient` y `BudgetPickClient` al import de
`./budgetState`, la línea `const needsClient = useBudgetNeedsClient();` junto a sus otros hooks (tras
`const canRead = …`), y justo antes de su `return (` principal (`BudgetDashboard.tsx:194`,
`BudgetPlannerPage.tsx:92`, `ExpensesPage.tsx:109`, `ConfirmationQueuePage.tsx:54`):

```tsx
  // ONE CLIENT AT A TIME (plan Task 11 D11.1): with every client chosen there
  // is no base currency to add these figures in, so the page asks for one.
  if (needsClient) {
    return (
      <div>
        <PageHead title={t("budget.dashboard.title")} description={t("budget.dashboard.description")} />
        <BudgetPageNav canRead={canRead} />
        <BudgetPickClient />
      </div>
    );
  }
```

con las claves de título y descripción de cada página (`budget.dashboard.*`, `budget.planner.*`,
`budget.expenses.*`, `budget.confirm.*`, las de su `PageHead`, líneas 197-198, 95-96, 112-113 y 57-58).

`BudgetAdminPage.tsx:104-130`: añadir `const needsClient = useBudgetNeedsClient();` tras `useScrollToHash(…)`, y
sustituir los tres paneles por cliente por

```tsx
      {/* The settings, categories and cost centres are one client's; the
          exchange rates below are every client's (a global route). */}
      {needsClient ? (
        <BudgetPickClient />
      ) : (
        <>
          <SettingsPanel canManage={canManage} />
          <CategoriesPanel canManage={canManage} />
          <CostCentersPanel canManage={canManage} />
        </>
      )}
```

`types.ts:16-21`: «Until the client selector exists (PHASE 3) these pages work on the internal client
(`api.ts`'s `useBudgetClient`).» pasa a «These pages work on the client chosen at the top (`api.ts`'s
`useBudgetClient`), and ask for one with every client chosen.» `types.ts:307-308` no cambia (no tiene
`PHASE 3`). `fixtures.ts:54-56`: «Every row here is the internal client's (`ORG_ID`, the id convention): what
the console works on until the client selector exists (PHASE 3).» pasa a «Every row here is the internal
client's (`ORG_ID`, the id convention): the client the preview works on when it is the one chosen.»

`settings/BudgetTab.tsx`: la línea 21 (`import { internalClientId } from "../../lib/internalClient";`) pasa a
`import { useClientScope } from "../../lib/clientScope";`. Sustituir 102-110 por

```tsx
  // Budget settings are one client's (base currency, fiscal year): the
  // client chosen at the top. With every client chosen there is none to
  // show, and the tab says so (plan Task 11 D11.1).
  const { selected: clientId, ready } = useClientScope();
  const needsClient = ready && clientId === null;
```

(`useMe` sale del import si queda sin uso), y la cadena del render (≈144) empieza por

```tsx
      {needsClient ? (
        <p className="note">{t("settings.budget.pickClient")}</p>
      ) : settings.isLoading ? (
```

`settings/api.ts:36`: `import { loadingUntilClient, requireKnown } from "../../lib/clientSelection";`. En el
comentario de 400-408: «`clientId` is `null` until `/me` has answered: the read waits for it, and a save
throws `InternalScopeUnknown` (nothing is sent).» pasa a «`clientId` is the client chosen at the top, `null`
with every client: the read waits for one, and a save throws `ClientNotChosen` (nothing is sent).»

`settings/UsersTab.tsx:16-17`: «PHASE 3: the client/site selector. The phase-3 console restyles this editor
(spec §3.4, "editor de asignaciones"); the wire shape it sends is final.» pasa a «The wire shape this editor
sends is final (MSP clients design §3.4); phase 3 adds the per-client view of effective permissions (design
§4.2).»

- [ ] **Step 6: Textos de presupuesto**

`frontend/src/i18n/en/budget.ts`, nuevo bloque: `pickClient: { title: "Choose a client at the top of the page", body: "Budget is kept per client, in that client's base currency, so it is shown one client at a time." },`.
`frontend/src/i18n/es/budget.ts`: `pickClient: { title: "Elige un cliente arriba", body: "El presupuesto se lleva por cliente, en la moneda base de ese cliente, así que se muestra de un cliente en uno." },`.
`frontend/src/i18n/en/settings.ts`, en `budget`: `pickClient: "Budget settings are per client. Choose a client at the top of the page to see its base currency and fiscal year.",`.
`frontend/src/i18n/es/settings.ts`, en `budget`: `pickClient: "Los ajustes de presupuesto son de cada cliente. Elige un cliente arriba para ver su moneda base y su año fiscal.",`.

La prueba de Ajustes busca `/choose a client at the top of the page/i`, que casa con el texto inglés.

- [ ] **Step 7: Ejecutar y ver que pasan**

```bash
cd frontend && npx vitest run src/modules/budget src/modules/settings && npx tsc -b
```

Esperado: PASS.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/modules/budget frontend/src/modules/settings frontend/src/i18n/en/budget.ts frontend/src/i18n/es/budget.ts frontend/src/i18n/en/settings.ts frontend/src/i18n/es/settings.ts
git commit -m "feat(console): budget works on the client chosen at the top

Budget figures are one client's, in its base currency, so the budget
pages and the settings Budget tab read and file under the selected
client, and with every client chosen ask for one instead of showing
figures. Exchange rates stay global (design §3.1, plan Task 11 D11.1)."
```

#### Documentación y aprobaciones de parche: el campo Cliente junto a «Para todos los clientes»

- [ ] **Step 9: Las pruebas que fallan, docs y patch**

En `frontend/src/modules/docs/docsClient.test.tsx` (con el arnés, `"o1"`; su `MANAGER` gana
`clients: [INTERNAL_CLIENT, ACME_CLIENT]`, definidos como en la Task 10 con `permissions: ["docs.read", "docs.manage"]`),
añadir en `describe("a new article", …)`:

```tsx
  it("with every client chosen, asks for the client, and not when it is for every client", async () => {
    selectClient(null, [INTERNAL_CLIENT, ACME_CLIENT]);
    const user = userEvent.setup();
    const createArticle = vi.fn().mockResolvedValue(ARTICLE);
    renderDocs({ createArticle });
    await user.click(await screen.findByRole("button", { name: "New article" }));
    await user.type(screen.getByLabelText("Title"), "VPN");
    await user.selectOptions(screen.getByLabelText("Client"), "c-acme");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(createArticle).toHaveBeenCalledTimes(1));
    expect(createArticle.mock.calls[0]?.[0]).toMatchObject({ title: "VPN", client_id: "c-acme" });
  });

  it("hides the Client field once the article is for every client", async () => {
    selectClient(null, [INTERNAL_CLIENT, ACME_CLIENT]);
    const user = userEvent.setup();
    renderDocs({ createArticle: vi.fn() });
    await user.click(await screen.findByRole("button", { name: "New article" }));
    expect(screen.getByLabelText("Client")).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: /For every client/ }));
    expect(screen.queryByLabelText("Client")).toBeNull();
  });
```

con `const ACME_CLIENT: MeClient = { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: ["docs.read", "docs.manage"] };`
y `const INTERNAL_CLIENT: MeClient = { ...SCOPE_INTERNAL, permissions: ["docs.read", "docs.manage"] };`. Y en
los `describe` del registro (≈139) y del documento (≈163), un caso igual al primero de arriba con su
formulario: para el registro, «New record», Name «VPN gateway» y `createDocRecord` con
`toMatchObject({ name: "VPN gateway", client_id: "c-acme" })`; para el documento, el diálogo de subida que
ya abre la prueba de 163, Title «Contract» y `createDocument` con `toMatchObject({ title: "Contract", client_id: "c-acme" })`.

En `frontend/src/modules/patch/__tests__/patch.test.tsx`, dentro de
`describe("FleetPatchPage — an approval reaches its owner's machines, and says so", …)` (3236), añadir:

```tsx
  it("with every client chosen, asks the bar for the client and files the approval there", async () => {
    selectClient(null, [
      { id: "o1", name: "Novieri", kind: "internal", enabled_modules: [], permissions: ME.permissions },
      { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: ME.permissions },
    ]);
    const user = userEvent.setup();
    const bulkApprovePatches = vi.fn().mockResolvedValue(bulkResult(["p1"], "test"));
    api = { bulkApprovePatches };
    renderWithProviders(<FleetPatchPage />);
    await screen.findByText("2023-10 Cumulative Update");
    await user.click(screen.getByRole("checkbox", { name: "Select KB5031354" }));

    await user.selectOptions(screen.getByLabelText("Client"), "c-acme");
    const confirm = await openApproval(user);
    await user.click(within(confirm).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(bulkApprovePatches).toHaveBeenCalled());
    expect(bulkApprovePatches.mock.calls[0]?.[0]).toMatchObject({ client_id: "c-acme" });
  });
```

(el `api = { bulkApprovePatches }` y `openApproval` son los del `describe`, 3237-3280; «Approve» es el botón
de confirmación que ya pulsa la prueba de 3282).

- [ ] **Step 10: Las pruebas existentes de docs y patch, corregidas**

`docs/docsClient.test.tsx`:

| Línea | Antes | Después |
|---|---|---|
| 61 | `it("is the internal client's", …)` | `it("is the client chosen at the top's", …)`; la afirmación `client_id: "o1"` no cambia |
| 139 | `it("is the internal client's, or every client's when asked", …)` | `it("is the client chosen at the top's, or every client's when asked", …)` |
| 163 | `it("is the internal client's, or every client's when asked", …)` | `it("is the client chosen at the top's, or every client's when asked", …)` |

`docs/DocsPage.test.tsx:371-373`: el comentario «The internal client -- `/me`'s `org_id` -- until the selector» pasa a
«The client chosen at the top: here the internal one, `o1`.»; `client_id: "o1"` no cambia.

`patch/__tests__/patch.test.tsx`:

| Línea | Antes | Después |
|---|---|---|
| 600-601 | `// The approval is the internal client's -- `/me`'s `org_id` -- until the` + continuación | `// The approval is the client chosen at the top's: here the internal one.` |
| 644 | `// reach of THIS approval, the internal client's (final review E: it used` | `// reach of THIS approval, the selected client's (final review E: it used` |
| 3229 | `// the internal client's by default (phase 3 brings the selector), the ROW's` | `// the client chosen at the top's (here the internal one), the ROW's` |
| 3282 | `it("states the internal client's machines by default, and every client's when asked", …)` | `it("states the selected client's machines by default, and every client's when asked", …)`; los textos con «your organisation's own client» no cambian (D11.3) |
| 3291 | `// Unticked: the internal client's 2 of the ring's 3 machines.` | `// Unticked: the selected (internal) client's 2 of the ring's 3 machines.` |

`patch/__tests__/changeLink.test.tsx:31-33`: «`/me`: the approval is filed under its `org_id`, the internal
client …» pasa a «The approval is filed under the client chosen at the top: here the internal one, `o1`.»

- [ ] **Step 11: Ejecutar y ver que fallan**

```bash
cd frontend && npx vitest run src/modules/docs src/modules/patch
```

Esperado: FAIL en los casos nuevos (no hay campo «Client»; el cuerpo lleva el cliente interno).

- [ ] **Step 12: Implementar docs**

`DocsPage.tsx`: la línea 16 pasa a

```tsx
import { ClientField } from "../../components/ClientField";
import { requireKnown, useNewRowClient } from "../../lib/clientSelection";
```

En `DocsPage`, tras `const [forEveryClient, setForEveryClient] = useState(false);` (248):

```tsx
  // A NEW ARTICLE IS ONE CLIENT'S unless it is for every client (§3.5): the
  // client chosen at the top, or with every client the editor's Client field.
  const target = useNewRowClient("docs.manage");
  const [pickedClient, setPickedClient] = useState<string | null>(null);
  const articleClient = target.needsField ? pickedClient : target.clientId;
```

`save` (310-314): `return api.createArticle({ ...draft, ...(forEveryClient ? {} : { client_id: requireKnown(articleClient) }) });`
con el comentario «A new article is the client's above, or, when asked, every client's.» en lugar del bloque
`// PHASE 3: …`. Junto al `ForEveryClientToggle` del editor (872-883), dentro del mismo `<div className="mt-3">`
y tras el interruptor:

```tsx
                    {!forEveryClient && target.needsField ? (
                      <ClientField code="docs.manage" value={pickedClient} onChange={setPickedClient} />
                    ) : null}
```

`NewRecordDialog` (1048-1072): sustituir `const me = useQuery<Me>(…);` por las mismas tres líneas
(`target`, `pickedClient`, con `recordClient` en lugar de `articleClient`), el cuerpo usa
`...(forEveryClient ? {} : { client_id: requireKnown(recordClient) })`, y tras su `ForEveryClientToggle`
(1142-1149) el mismo `ClientField`. Si `Me` y `useQuery` quedan sin uso en el archivo, salen de sus imports.

`documents.tsx`: la línea 44 pasa a los mismos dos imports; en `UploadDocumentDialog` (870-900), sustituir
`const me = useQuery<Me>(…);` por las tres líneas (`documentClient`), el cuerpo de `createDocument` usa
`...(forEveryClient ? {} : { client_id: requireKnown(documentClient) })`, el `useEffect` de apertura añade
`setPickedClient(null);`, y tras su `ForEveryClientToggle` (981-988) el mismo `ClientField`.

- [ ] **Step 13: Implementar patch**

`DecisionBar.tsx`: la línea 9 (`import { internalClientId, newRowClient } from "../../lib/internalClient";`) pasa a

```tsx
import { ClientField } from "../../components/ClientField";
import { useClientScope } from "../../lib/clientScope";
import { requireKnown, useNewRowClient } from "../../lib/clientSelection";
```

El comentario de la prop `rowClientId` (112-115): «with none, under the internal client until phase 3.» pasa a
«with none, under the client chosen at the top, or the bar's Client field's.» Quitar `const me = useQuery<Me>(…);`
(122) y, si quedan sin uso, `useQuery`, `useApi` y `Me` de los imports. El comentario de 129-130 pasa a
«The client was not chosen when the approval was confirmed: nothing was sent, and the bar says why
(`ClientNotChosen`).» Sustituir 144-175 (desde «WHOSE MACHINES THIS APPROVAL CLEARS» hasta `untickedName`) por

```tsx
  // WHOSE MACHINES THIS APPROVAL CLEARS, and how many of them are in the
  // ring: every client's when asked; otherwise the row's client on a
  // machine's drill-down, else the client chosen at the top, else the bar's
  // Client field (design §3.1).
  const clientName = useClientName();
  const { clients } = useClientScope();
  const target = useNewRowClient("patch.approve", rowClientId ?? undefined);
  const [pickedClient, setPickedClient] = useState<string | null>(null);
  const rowOwner = target.needsField ? pickedClient : target.clientId;
  const ownerId = forEveryClient ? null : rowOwner;
  // The organisation's own client keeps its words; another is named; one not
  // chosen yet is "the client you choose" (plan Task 11 D11.3).
  const nameOf = (id: string | null): string =>
    id === null
      ? t("patch.decision.chosenClient")
      : clients.find((client) => client.id === id)?.kind === "internal"
        ? t("patch.decision.ownClient")
        : clientName(id);
  const owner: ApprovalOwner = forEveryClient ? EVERY_CLIENT : { kind: "client", name: nameOf(ownerId) };
  const ownerCount = forEveryClient
    ? ringDeviceCount
    : ownerId == null || option?.clientCounts == null
      ? null
      : (option.clientCounts[ownerId] ?? 0);
  // The reach of THIS approval: its owner's machines (final review E).
  const reach = approvalReachMessage(owner, ring, ownerCount, fleetDeviceTotal);
  // What ticking "For every client" changes, said by the client the approval
  // is otherwise for.
  const untickedName = nameOf(rowOwner);
```

Tras `<RingSelect … />` (≈205) en la barra:

```tsx
          {!forEveryClient && target.needsField ? (
            <ClientField code="patch.approve" value={pickedClient} onChange={setPickedClient} />
          ) : null}
```

y en `onConfirm` (370-382) sustituir el `try` por

```tsx
          try {
            // The ROW's client, the selected one or the bar's; none at all
            // when asked for every client.
            filedUnder = forEveryClient ? {} : { client_id: requireKnown(rowOwner) };
          } catch (error) {
            // The client is not chosen: nothing is sent without it.
            setOwnerError(error);
            return;
          }
```

Textos: `frontend/src/i18n/en/patch.ts`, en `decision`, `chosenClient: "the client you choose",`;
`frontend/src/i18n/es/patch.ts`, en `decision`, `chosenClient: "el cliente que elijas",`.

- [ ] **Step 14: Ejecutar y ver que pasan**

```bash
cd frontend && npx vitest run src/modules/docs src/modules/patch && npx tsc -b
```

Esperado: PASS.

- [ ] **Step 15: Commit**

```bash
git add frontend/src/modules/docs frontend/src/modules/patch frontend/src/i18n/en/patch.ts frontend/src/i18n/es/patch.ts
git commit -m "feat(console): articles, records, documents and patch approvals name their client

Beside For every client, a required Client field with every client
chosen; the selected client otherwise; a machine's client on its
drill-down (design §3.1, §3.5)."
```

#### SLA: la vista previa usa el cliente elegido

- [ ] **Step 16: La prueba que falla, SLA**

Crear `frontend/src/modules/sla/__tests__/resolverClient.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient, MeClient } from "../../../lib/api";
import type { QueueOut } from "../../service-desk/types";
import { PolicyResolverPanel } from "../PolicyResolverPanel";
import type { PolicyOut } from "../types";

// MSP clients phase 3, design §3.5: the preview resolves for the client chosen
// at the top, with phase 2's D2 rule -- a client with active policies of its
// own resolves among those alone; with every client chosen it previews the
// global policies, and says so (plan Task 11 D11.4).

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

const ACME: MeClient = { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: ["sla.read"] };

function policy(overrides: Partial<PolicyOut>): PolicyOut {
  return {
    id: "p", client_id: null, queue_id: null, priority: null, calendar_id: "cal1",
    response_target_minutes: 60, resolution_target_minutes: 480, escalate_priority: false,
    escalate_to_queue_id: null, active: true, created_at: "2026-01-01T00:00:00.000Z", ...overrides,
  };
}

const POLICIES = [
  policy({ id: "global-helpdesk-urgent", queue_id: "q1", priority: "urgent" }),
  policy({ id: "acme-catch-all", client_id: "c-acme" }),
];

function renderPanel(selected: string | null) {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients: [ACME], ready: true });
  mockUseApi.mockReturnValue({
    me: vi.fn().mockResolvedValue({ user_id: "u", org_id: "o1", email: "a@b.c", display_name: "A", permissions: ["sla.read", "ticket.read"], clients: [ACME] }),
    listSlaPolicies: vi.fn().mockResolvedValue(POLICIES),
    listSlaCalendars: vi.fn().mockResolvedValue([]),
    listTicketQueues: vi.fn().mockResolvedValue([{ id: "q1", name: "Helpdesk" } as QueueOut]),
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/sla"]}>
        <PolicyResolverPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function choose(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByRole("option", { name: "Helpdesk" });
  await user.selectOptions(screen.getByLabelText("Queue"), "q1");
  await user.selectOptions(screen.getByLabelText("Priority"), "urgent");
}

describe("the SLA preview's client", () => {
  it("resolves among the selected client's own policies", async () => {
    const user = userEvent.setup();
    renderPanel("c-acme");
    await choose(user);
    expect(await screen.findByText(/Resolved at level 4/)).toBeInTheDocument();
    expect(screen.getByText("Previewing for Acme.")).toBeInTheDocument();
  });

  it("previews the global policies with every client chosen, and says so", async () => {
    const user = userEvent.setup();
    renderPanel(null);
    await choose(user);
    expect(await screen.findByText(/Resolved at level 1/)).toBeInTheDocument();
    expect(screen.getByText("With every client chosen, this previews the global policies.")).toBeInTheDocument();
  });
});
```

- [ ] **Step 17: Ejecutar y ver que falla**

```bash
cd frontend && npx vitest run src/modules/sla
```

Esperado: FAIL en los dos casos: el panel pasa `null` siempre (el primero resuelve al nivel 1) y no hay
ninguna de las dos notas.

- [ ] **Step 18: Implementar SLA**

`PolicyResolverPanel.tsx`, imports:

```tsx
import { useClientScope } from "../../lib/clientScope";
import { useClientName } from "../settings/api";
```

Sustituir 40-44 (el comentario `// PHASE 3: …` y `const preview = …`) por

```tsx
  // The client to preview: the one chosen at the top, with phase 2's D2 rule
  // (a client with active policies of its own resolves among those alone).
  // With every client chosen there is no one client, so the panel previews
  // the global policies -- the ones a client without policies of its own
  // resolves among -- and says so (plan Task 11 D11.4).
  const { selected } = useClientScope();
  const clientName = useClientName();
  const preview =
    queueId === "" ? null : resolutionPreview(policies.data ?? [], queueId, priority, selected);
```

y, como primer hijo del fragmento de la rama de éxito (justo después de `<>`, línea 58), la nota:

```tsx
          <p className="note" style={{ marginBottom: 8 }}>
            {selected === null
              ? t("sla.resolver.everyClientNote")
              : t("sla.resolver.clientNote", { client: clientName(selected) })}
          </p>
```

Textos: `frontend/src/i18n/en/sla.ts`, en `resolver`: `everyClientNote: "With every client chosen, this previews the global policies.",`
y `clientNote: "Previewing for {client}.",`. `frontend/src/i18n/es/sla.ts`, en `resolver`:
`everyClientNote: "Con todos los clientes elegidos, esto previsualiza las políticas globales.",` y
`clientNote: "Vista previa para {client}.",`.

- [ ] **Step 19: Ejecutar y ver que pasa**

```bash
cd frontend && npx vitest run src/modules/sla && npx tsc -b
```

Esperado: PASS, incluida `sla.test.tsx` (con el arnés en el cliente interno `o1`, que no tiene políticas
propias en sus fixtures, la vista previa resuelve entre las globales como antes).

- [ ] **Step 20: Commit**

```bash
git add frontend/src/modules/sla frontend/src/i18n/en/sla.ts frontend/src/i18n/es/sla.ts
git commit -m "feat(console): the SLA preview resolves for the client chosen at the top

It used to pass no client and preview the global policies only. With
every client chosen it still previews the global ones, and says so
(design §3.5, phase 2 D2)."
```

#### Suscripciones, licencias, cambios y continuidad

- [ ] **Step 21: Las pruebas que fallan**

`frontend/src/modules/subscriptions/__tests__/subscriptions.test.tsx` (con el arnés): añadir junto a la prueba
de 463:

```tsx
  it("with every client chosen, asks for the subscription's client and files it there", async () => {
    selectClient(null, [
      { ...SCOPE_INTERNAL, permissions: ["subscriptions.read", "subscriptions.manage"] },
      { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: ["subscriptions.read", "subscriptions.manage"] },
    ]);
    const user = userEvent.setup();
    renderWithProviders(<SubscriptionsPage />);
    await screen.findByText("Acme SaaS", { selector: ".cell-title a" });
    expect(screen.getByRole("columnheader", { name: "Client" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "New subscription" }));
    await user.type(screen.getByLabelText("Service name"), "Globex CRM");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/Client is required/)).toBeInTheDocument();
    expect(api.createSubscription).not.toHaveBeenCalled();

    await user.selectOptions(screen.getByLabelText("Client"), "c-acme");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.createSubscription).toHaveBeenCalledWith(expect.objectContaining({ client_id: "c-acme", service_name: "Globex CRM" })));
  });
```

`frontend/src/modules/licenses/__tests__/licenses.test.tsx`: junto a `openNewLicense` (626):

```tsx
  it("with every client chosen, asks for the license's client and files it there", async () => {
    selectClient(null, [
      { ...SCOPE_INTERNAL, permissions: ["licenses.read", "licenses.manage"] },
      { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: ["licenses.read", "licenses.manage"] },
    ]);
    const user = await openNewLicense();
    expect(screen.getByRole("columnheader", { name: "Client" })).toBeInTheDocument();
    await pick(user, "Product", /Acme Office Suite/);
    await user.selectOptions(screen.getByLabelText("Client"), "c-acme");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.createLicense).toHaveBeenCalledWith(expect.objectContaining({ client_id: "c-acme" })));
  });
```

`frontend/src/modules/change/__tests__/change.test.tsx`: junto a la prueba de 385:

```tsx
  it("with every client chosen, asks for the change's client and files it there", async () => {
    selectClient(null, [
      { ...SCOPE_INTERNAL, permissions: ME.permissions },
      { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: ["change"], permissions: ME.permissions },
    ]);
    const user = userEvent.setup();
    renderNew();
    await user.type(screen.getByLabelText("Title *"), "Upgrade switch firmware");
    await user.type(screen.getByLabelText("Description *"), "Roll out firmware 2.1.");
    await user.selectOptions(screen.getByLabelText("Client"), "c-acme");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(api.createChange).toHaveBeenCalledWith(expect.objectContaining({ client_id: "c-acme", title: "Upgrade switch firmware" })),
    );
  });
```

`frontend/src/modules/bcp/__tests__/bcp.test.tsx`: en el `describe` del editor (≈215), con `api` ampliada con
`listDevices: vi.fn().mockResolvedValue({ items: [{ id: "d-acme", hostname: "acme-01", client_id: "c-acme", agent_status: "online", os_family: "windows" }, { id: "d-own", hostname: "own-01", client_id: "o1", agent_status: "online", os_family: "linux" }], total: 2 })`
(`useScopeDevices` lee `api.listDevices()`, `bcp/api.ts:131`):

```tsx
  it("with every client chosen, asks for the plan's client and offers only its devices", async () => {
    selectClient(null, [
      { ...SCOPE_INTERNAL, permissions: ME.permissions },
      { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: ["bcp"], permissions: ME.permissions },
    ]);
    const user = renderEditor();
    await user.type(screen.getByLabelText("Name *"), "Acme failover");
    await user.selectOptions(screen.getByLabelText("Client"), "c-acme");
    await user.click(screen.getByRole("button", { name: /Add scope/ }));
    await user.click(screen.getAllByRole("combobox").find((box) => box.getAttribute("aria-label") !== "Client") as HTMLElement);
    expect(await screen.findByRole("option", { name: /acme-01/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /own-01/ })).toBeNull();
  });
```

(«Add scope entry» es `bcp.editor.addScope`, `i18n/en/bcp.ts:103`.)

- [ ] **Step 22: Las pruebas existentes, corregidas**

| Archivo:línea | Antes | Después |
|---|---|---|
| `subscriptions/__tests__/subscriptions.test.tsx:480` | `// The internal client (`/me`'s `org_id`) until phase 3's selector.` | `// The client chosen at the top: here the internal one, `o1`.` |
| `change/__tests__/change.test.tsx:383-385` | comentario «Until the selector exists (PHASE 3) that is the internal client, `/me`'s `org_id`.» y `it("files a new change under the internal client", …)` | «A change goes to the client chosen at the top: here the internal one, `o1`.» y `it("files a new change under the client chosen at the top", …)`; la afirmación no cambia |
| `bcp/__tests__/bcp.test.tsx:300` | `// The internal client, `/me`'s `org_id` (PHASE 3: the selector's).` | `// The client chosen at the top: here the internal one, `o1`.` |

- [ ] **Step 23: Ejecutar y ver que fallan**

```bash
cd frontend && npx vitest run src/modules/subscriptions src/modules/licenses src/modules/change src/modules/bcp
```

Esperado: FAIL en los cuatro casos nuevos (ni campo «Client» ni columna «Client»; el plan ofrece todos los
equipos).

- [ ] **Step 24: Implementar suscripciones y licencias**

`SubscriptionsPage.tsx:37`: sustituir el import de `internalClient` por

```tsx
import { ClientField } from "../../components/ClientField";
import { useClientScope } from "../../lib/clientScope";
import { requireKnown, useNewRowClient } from "../../lib/clientSelection";
import { useClientName } from "../settings/api";
```

En la lista (389): `const { selected } = useClientScope(); const clientName = useClientName(); const showClient = selected === null;`
en el componente de la página; tras `<Th>{t("subscriptions.list.colService")}</Th>` añadir
`{showClient ? <Th>{t("subscriptions.list.colClient")}</Th> : null}` y, en cada fila, tras su primera celda,
`{showClient ? <Td>{clientName(sub.client_id)}</Td> : null}` (la tabla llama `sub` a la fila, `SubscriptionsPage.tsx:398`).
En `NewSubscriptionDialog`: sustituir `const me = useQuery<Me>(…);` por

```tsx
  // A subscription is one client's (MSP clients phase 2): its seats are held by
  // that client's people. The client chosen at the top, or with every client
  // the dialog's Client field (design §3.1).
  const target = useNewRowClient("subscriptions.manage");
  const [pickedClient, setPickedClient] = useState<string | null>(null);
  const subscriptionClient = target.needsField ? pickedClient : target.clientId;
```

en `submit`, `values` gana `client: subscriptionClient` y `rules` pasa a
`() => [...subscriptionRules(), ...(target.needsField ? [{ name: "client", label: translate("subscriptions.new.client") }] : [])]`
(`subscriptionRules` es una función, `SubscriptionsPage.tsx:85`); sustituir 527-536 (el
comentario `// PHASE 3: …`, `const internalClient = …` y `client_id: requireKnown(internalClient),`) por
`client_id: requireKnown(subscriptionClient),`; `reset()` gana `setPickedClient(null);`; y en el formulario,
como primer campo:
`{target.needsField ? <ClientField code="subscriptions.manage" value={pickedClient} onChange={setPickedClient} /> : null}`.
`subscriptions/api.ts:50-55`: en su comentario, `InternalScopeUnknown` pasa a `ClientNotChosen`.

`LicensesPage.tsx:42`: los mismos cuatro imports. Lista (356): `showClient` y `clientName` igual que arriba;
cabecera `{showClient ? <Th>{t("licenses.list.colClient")}</Th> : null}` tras `colProduct`, y celda
`{showClient ? <Td>{clientName(license.client_id)}</Td> : null}` tras la primera. En `NewLicenseDialog`:
sustituir `const me = useQuery<Me>(…);` por

```tsx
  // A license is one client's (MSP clients phase 2): the client chosen at the
  // top, or with every client the dialog's Client field (design §3.1).
  const target = useNewRowClient("licenses.manage");
  const [pickedClient, setPickedClient] = useState<string | null>(null);
  const licenseClient = target.needsField ? pickedClient : target.clientId;
```

en `submit` (481-484): `const values = { product_id: productId, entitlements, cost, currency, client: licenseClient };`
y `rules: () => [...licenseRules(values.cost), ...(target.needsField ? [{ name: "client", label: translate("licenses.new.client") }] : [])]`;
sustituir 486-495 por `client_id: requireKnown(licenseClient),`; `reset()` gana `setPickedClient(null);`; y como
primer campo del formulario
`{target.needsField ? <ClientField code="licenses.manage" value={pickedClient} onChange={setPickedClient} /> : null}`.
`licenses/api.ts:108-113`: `InternalScopeUnknown` pasa a `ClientNotChosen`.

`useQuery` y `Me` salen de los imports de ambos archivos si quedan sin uso.

- [ ] **Step 25: Implementar cambios y continuidad**

`ChangeRequestForm.tsx:11`: `import { ClientField } from "../../components/ClientField";` y
`import { requireKnown, useNewRowClient } from "../../lib/clientSelection";`. Sustituir `const me = useQuery<Me>(…);` por

```tsx
  // A change is one client's (MSP clients phase 2), and that client must use
  // change management: the client chosen at the top, or with every client the
  // form's Client field (design §3.1; plan Task 11 D11.2).
  const target = useNewRowClient("change.request");
  const [pickedClient, setPickedClient] = useState<string | null>(null);
  const changeClient = target.needsField ? pickedClient : target.clientId;
```

En `submit`, la regla nueva solo para un alta:
`rules: () => [...changeRules(windowStart, windowEnd, zone), ...(!existing && target.needsField ? [{ name: "client", label: translate("change.form.client") }] : [])]`
y `values: { title, description, window_end: windowEnd, client: changeClient }`; sustituir 191-198 por

```tsx
      // The server's 404 for a client without change management says so in a
      // sentence, which `FormActions` shows as it is.
      create.mutate(() => ({ ...body, client_id: requireKnown(changeClient) }), { onSuccess });
```

y en el formulario, como primer campo de `form-grid`:
`{!existing && target.needsField ? <ClientField code="change.request" value={pickedClient} onChange={setPickedClient} /> : null}`.
`ChangesPage.tsx:210`: `const { selected } = useClientScope(); const clientName = useClientName(); const showClient = selected === null;`,
`{showClient ? <Th>{t("change.list.client")}</Th> : null}` tras `<Th>{t("change.common.name")}</Th>`, y en cada
fila, tras su primera celda, `{showClient ? <Td>{clientName(change.client_id)}</Td> : null}`.
`change/api.ts:58-63`: `InternalScopeUnknown` pasa a `ClientNotChosen`.

`PlanEditor.tsx:12`: los imports de `ClientField`, `useClientName` (`../settings/api`) y
`requireKnown, useNewRowClient`. Sustituir `const me = useQuery<Me>(…);` (147) por

```tsx
  // A plan is one client's (MSP clients phase 2), and that client must use
  // continuity. An edit keeps its own; a new one is the client chosen at the
  // top, or with every client the form's Client field (design §3.1).
  const target = useNewRowClient("bcp.manage");
  const [pickedClient, setPickedClient] = useState<string | null>(null);
  const clientName = useClientName();
```

y, tras `const existing = …` (151):

```tsx
  const planClient = existing ? existing.client_id : target.needsField ? pickedClient : target.clientId;
```

Las opciones (183-185) pasan a ser las del cliente del plan (§3.3), con los artículos globales también,
marcados:

```tsx
  // Built at render, never memoised: the sublabels hold translated words.
  // Only the plan's client's devices, groups and articles may be named
  // (§3.3); an article for every client may too, and says so.
  const deviceOptions = scopeDevicePickerOptions(
    (devices.data ?? []).filter((device) => device.client_id === planClient),
  );
  const groupOptions = scopeGroupPickerOptions(
    (groups.data ?? []).filter((group) => group.client_id === planClient),
  );
  const articleOptions = procedurePickerOptions(
    (articles.data ?? []).filter((article) => article.client_id === planClient || article.client_id === null),
    zone,
  ).map((option) =>
    (articles.data ?? []).find((article) => article.id === option.id)?.client_id === null
      ? { ...option, sublabel: [t("lib.clients.everyClient"), option.sublabel].filter(Boolean).join(" · ") }
      : option,
  );
```

(`groups.data` es `DeviceGroup[]`, que trae `client_id`; `devices.data` es `Device[]`, cuyo `client_id` es
opcional en el tipo y lo trae siempre la API.) La regla nueva en `submit`:
`...(!existing && target.needsField ? [{ name: "client", label: translate("bcp.editor.client") }] : [])`
en `rules`, y `client: planClient` en `values`. Sustituir 266-276 por

```tsx
      // The server's 404 for a client without continuity says so in a
      // sentence, which `FormActions` shows as it is.
      create.mutate(() => ({ ...body, client_id: requireKnown(planClient) }), { onSuccess });
```

y en el formulario, como primer campo:

```tsx
          {!existing && target.needsField ? (
            <ClientField
              code="bcp.manage"
              value={pickedClient}
              onChange={(id) => {
                setPickedClient(id);
                // Another client's devices, groups and articles: the scope and
                // the procedures chosen so far were this one's.
                setScope([]);
                setProcedureRefs([]);
              }}
            />
          ) : null}
```

`PlansPage.tsx:257`: `showClient`/`clientName` como arriba, cabecera
`{showClient ? <Th>{t("bcp.plans.colClient")}</Th> : null}` tras `colName`, y celda
`{showClient ? <Td>{clientName(plan.client_id)}</Td> : null}` tras la primera de cada fila.
`bcp/api.ts:59-64`: `InternalScopeUnknown` pasa a `ClientNotChosen`. `people/api.ts:80-84`: «a body that cannot
name its client yet throws `InternalScopeUnknown` (`lib/internalClient.ts`)» pasa a «a body that cannot name
its client yet throws `ClientNotChosen` (`lib/clientSelection.ts`)».

`useQuery` y `Me` salen de los imports de los archivos tocados si quedan sin uso.

- [ ] **Step 26: Textos**

| Archivo (en / es) | Clave | en | es |
|---|---|---|---|
| `subscriptions.ts` | `list.colClient` | Client | Cliente |
| `subscriptions.ts` | `new.client` | Client | Cliente |
| `licenses.ts` | `list.colClient` | Client | Cliente |
| `licenses.ts` | `new.client` | Client | Cliente |
| `change.ts` | `list.client` | Client | Cliente |
| `change.ts` | `form.client` | Client | Cliente |
| `bcp.ts` | `plans.colClient` | Client | Cliente |
| `bcp.ts` | `editor.client` | Client | Cliente |

Si una de esas claves ya existe con el mismo sentido (`subscriptions.ts:189` y `licenses.ts:60,75` tienen
`colClient` en otros bloques), se añade la nueva en el bloque indicado igualmente: cada bloque es de su
pantalla.

- [ ] **Step 27: Ejecutar y ver que pasan**

```bash
cd frontend && npx vitest run src/modules/subscriptions src/modules/licenses src/modules/change src/modules/bcp && npx tsc -b
```

Esperado: PASS.

- [ ] **Step 28: Commit**

```bash
git add frontend/src/modules/subscriptions frontend/src/modules/licenses frontend/src/modules/change frontend/src/modules/bcp frontend/src/modules/people/api.ts frontend/src/i18n/en/subscriptions.ts frontend/src/i18n/es/subscriptions.ts frontend/src/i18n/en/licenses.ts frontend/src/i18n/es/licenses.ts frontend/src/i18n/en/change.ts frontend/src/i18n/es/change.ts frontend/src/i18n/en/bcp.ts frontend/src/i18n/es/bcp.ts
git commit -m "feat(console): subscriptions, licenses, changes and BCP plans in the right client

Each goes to the client selected at the top, or a required Client
field with every client chosen; their lists show a Client column then;
a plan's scope and procedure pickers list its client's devices, groups
and articles, plus articles for every client (design §3.1, §3.3, §3.4)."
```

- [ ] **Step 29: Verificación de la Task 11**

```bash
cd frontend && grep -rn "internalClient\|PHASE 3\|InternalScopeUnknown" src/modules/{budget,settings,docs,patch,sla,subscriptions,licenses,change,bcp,people}
```

Esperado: ninguna línea.

```bash
cd frontend && npx vitest run && npx tsc -b && npm run build
```

Esperado: la suite completa en verde, `tsc` sin errores y el build terminado. Si alguna prueba de
`settings/` fuera de `budget.test.tsx` y `settings.es.test.tsx` abre la pestaña Presupuesto y falla porque
pide un cliente, se le añade el arnés del Step 1 con `"org-1"`. La Task 11 no toca el backend.



#### Addendum del coordinador (Task 11): la columna Cliente se oculta con un cliente elegido

El diseño (§3.4) oculta la columna Cliente cuando hay un cliente elegido arriba. Tres columnas que ya existen se muestran siempre:
- `frontend/src/modules/licenses/ComplianceDashboard.tsx:47,71`
- `frontend/src/modules/licenses/RenewalCalendar.tsx:51,67`
- `frontend/src/modules/subscriptions/SpendDashboard.tsx:46,61`

Lo mismo vale para las columnas Cliente que esta tarea añade (`licenses.list.colClient`, `subscriptions.list.colClient`, `bcp.plans.colClient`, `change.list.client` como columna).

- [ ] **Step A1: Prueba que falla**, en `licenses/__tests__/licenses.test.tsx` y en `subscriptions/__tests__/subscriptions.test.tsx`, con el mismo mock de `useClientScope` que ya usa el archivo:

```tsx
it("hides the Client column while one client is chosen at the top", async () => {
  selectClient("c-x"); // the file's helper around the mocked useClientScope
  renderCompliance(); // the file's existing helper for ComplianceDashboard
  await screen.findByText(/compliance/i);
  expect(screen.queryByRole("columnheader", { name: "Client" })).toBeNull();
});
it("shows the Client column with every client", async () => {
  selectClient(null);
  renderCompliance();
  expect(await screen.findByRole("columnheader", { name: "Client" })).toBeInTheDocument();
});
```

Haz lo mismo para `RenewalCalendar` y para `SpendDashboard`, con su encabezado.

- [ ] **Step A2:** ejecuta esos archivos. Resultado esperado: FAIL en los casos «hides…».
- [ ] **Step A3: Implementación**, en cada componente y en cada lista con columna Cliente de esta tarea:
  - Arriba del componente: `const { selected } = useClientScope(); const showClient = selected === null;`, importando de `../../lib/clientScope`.
  - El `<Th>` y el `<Td>` de cliente pasan a ir dentro de `{showClient && …}`.
  - Si la tabla calcula un `colSpan` para su fila vacía, réstale 1 cuando `!showClient`.
- [ ] **Step A4:** ejecuta los mismos archivos. Resultado esperado: PASS.
- [ ] **Step A5: Commit.** `git commit -am "fix(console): the Client column hides while one client is chosen"`

### Task 12: Página Clientes, cierre de `internalClient.ts` y prueba anti-`PHASE 3`

Diseño: §4.1 (página Clientes) y §3.6 (para que no vuelva). Oleada 4, en paralelo con las Tasks 13 y 14, cada una en su worktree.

**Archivos compartidos que toca esta tarea** (el coordinador los fusiona):
- `frontend/src/lib/api.ts`: tipos `ClientCreate`, `ClientUpdate`, `SiteCreate`, `SiteUpdate`, `ContactCreate` junto a `ContactUpdate` (`:880-885`); métodos nuevos junto a `listClientSites` (`:1644-1647`) y antes de `updateContact` (`:1798-1808`). La Task 13 toca `DeviceListParams`/`listDevices`/`updateAsset`; la 14, `createBackupRepository`. Bloques distintos.
- `frontend/src/App.tsx`: dos imports y dos `<Route>` (solo esta tarea).
- `frontend/src/shell/nav.ts`: una sección nueva en Admin (solo esta tarea).
- `frontend/scripts/routes.mjs`: `PARAMS.clientId` y dos rutas (solo esta tarea).
- `frontend/src/i18n/{en,es}/index.ts`: registra el catálogo `clients` (solo esta tarea).
- `frontend/src/i18n/{en,es}/shell.ts`, `people.ts`, `ipam.ts` (solo esta tarea).
- `frontend/src/modules/health/types.ts`: **la misma edición, letra por letra, que la Task 14** (`ResolutionLevel` y `client_id`). Git fusiona solo dos cambios idénticos.
- `frontend/src/modules/settings/UsersTab.tsx`: **solo las líneas 15-16** (la cabecera `PHASE 3`). La Task 13 edita el bloque de permisos efectivos (`:741-776`), no la cabecera.

**Files:**
- Create: `frontend/src/modules/clients/api.ts`, `labels.ts`, `ClientsPage.tsx`, `ClientForm.tsx`, `ClientDetailPage.tsx`, `SummaryTab.tsx`, `SitesTab.tsx`, `ContactsTab.tsx`, `ModulesTab.tsx`, `fixtures.ts`
- Create: `frontend/src/i18n/en/clients.ts`, `frontend/src/i18n/es/clients.ts`
- Create: `frontend/src/lib/noPhase3.test.ts`
- Modify: `frontend/src/lib/api.ts` (`:880-885`, `:1636-1647`, `:1798`)
- Modify: `frontend/src/App.tsx` (imports tras `:125`, rutas antes de `path="/settings"` `:1369`)
- Modify: `frontend/src/shell/nav.ts:154` (sección `organization` en Admin)
- Modify: `frontend/scripts/routes.mjs` (`PARAMS` `:23-39`, `ROUTES` antes de `"/settings"` `:159`)
- Modify: `frontend/src/i18n/en/index.ts`, `frontend/src/i18n/es/index.ts` (catálogo `clients`)
- Modify: `frontend/src/i18n/en/shell.ts` (`:19-58`, `:59-65`, `:66-93`) y `frontend/src/i18n/es/shell.ts` (mismos bloques)
- Modify: `frontend/src/i18n/{en,es}/people.ts` (`turnOffBodyContact`, en `:132-136`, es `:128-129`)
- Modify: `frontend/src/i18n/{en,es}/ipam.ts` (`registry.sitesSubtitle`, en `:776-778`, es `:769-770`; clave nueva `registry.manageSites`)
- Modify: `frontend/src/modules/ipam/RegistryPage.tsx` (`SitePanel`, `:155-165`: un enlace a `/clients`)
- Modify: `frontend/src/modules/health/types.ts` (`:63`, `:189-205`)
- Modify: `frontend/src/modules/settings/UsersTab.tsx:15-16`
- Modify: los comentarios que aún nombren `lib/internalClient.ts` o `InternalScopeUnknown` (lista en el Step 9), y `frontend/src/fixtures/core.ts:87-88`, `frontend/src/i18n/en/lib.ts:43-44`
- Delete: `frontend/src/lib/internalClient.ts`, `frontend/src/lib/internalClient.test.ts`
- Test: `frontend/src/modules/clients/__tests__/labels.test.ts`, `frontend/src/modules/clients/__tests__/clients.test.tsx`, `frontend/src/modules/clients/clients.es.test.tsx`, `frontend/src/lib/api.clients.test.ts`, `frontend/src/lib/noPhase3.test.ts`, `frontend/src/shell/nav.test.ts` (un `describe` nuevo al final)

**Interfaces:**
- Consumes (Task 7): `useClientScope(): { selected; select; clients: MeClient[]; ready }` de `frontend/src/lib/clientScope.tsx`; `MeClient` y `Me.permissions_everywhere`, `Me.clients` de `lib/api.ts`; el constructor `new ApiClient(getToken, onUnauthorized, base, getClient)`.
- Consumes (Task 8): `useEverywherePermissions()` (`lib/mePermissions.ts`), misma forma que `useMePermissions` (`has`, `lacks`); `visibleGroups(permissions, modules)`.
- Consumes (Task 9): `requireKnown`, `loadingUntilClient` y `ClientNotChosen` en `lib/clientSelection.ts` (para reapuntar lo que quede importando `lib/internalClient`).
- Consumes (Task 3, backend): `AssignmentOut.client_id` y `level = "client"` en `GET /api/v1/health/assignments`.
- Produces, en `ApiClient` (01-interfaces): `createClient`, `updateClient`, `archiveClient`, `createClientSite`, `updateSite`, `archiveSite`, `listContacts`, `createContact`.
- Produces, **fuera de 01-interfaces**:
  - `ApiClient.getClientRecord(clientId: string): Promise<ClientOut>` (`GET /clients/{id}`, que ya existe; la ficha lo necesita y la lista no da un cliente archivado sin `include_archived`).
  - `ApiClient.listClientSites(clientId: string, params: { include_archived?: boolean } = {})`: el segundo argumento es nuevo y opcional; la llamada de un argumento (selectores de sede, IPAM, Task 9) no cambia.
  - Tipos `ClientCreate`, `ClientUpdate`, `SiteCreate`, `SiteUpdate`, `ContactCreate` en `lib/api.ts`.
  - `modules/clients/labels.ts`: `OPTIONAL_MODULES`, `OptionalModule`, `clientKindLabel`, `clientStatusLabel`, `clientStatusKind`, `moduleLabel`, `moduleHint`, `parseDomains`, `clientHealthDefault`.
  - `modules/clients/api.ts`: `CLIENT_LIST_KEY = "clients-page"`, `CLIENT_KEY = "client"`, `CONTACTS_KEY = "client-contacts"`, los hooks del Step 4.
  - Catálogo `clients` (en/es), `shell.nav.section.organization`, `shell.nav.item.clients`, `shell.nav.hint.clients`, `ipam.registry.manageSites`.

> **Decisión del plan (ficha con otro cliente elegido arriba).** Con `X-Client-Id: Y`, `GET /clients/X` es un 404 (RLS). Si la persona abre `/clients/X` con otro cliente elegido arriba, la ficha no pide nada: dice con qué cliente trabaja la consola y ofrece «Trabajar con este cliente» (`select(X)`), o «Trabajar con todos mis clientes» si X no está en `/me.clients`.
>
> **Decisión del plan (dónde va en el menú).** Una sección nueva, «Organización», la primera del área Admin, con una sola página: Clientes. Ninguna sección existente encaja (Plataforma es el propio servidor).
>
> **Decisión del plan (textos que remitían a la API).** El cuerpo de `ConfirmDialog` es una cadena, así que `people.holdings.confirm.turnOffBodyContact` nombra la página («Clientes, pestaña Contactos del cliente») sin enlace. En IPAM, `Panel.subtitle` también es cadena: el texto cambia y el panel de sedes gana una acción «Administrar las sedes» que enlaza a `/clients`.
>
> **Decisión del plan (perfil de salud en la ficha).** Se lee de `GET /health/assignments` con la misma clave que ya usan `ProfileList` y `AlertReadiness` (`["health-assignments", null]`), y solo si la persona tiene `health.read` en ese cliente. Se muestra el propio del cliente, o el de la organización que se le aplica, o «ninguno».

- [ ] **Step 1: Las pruebas que fallan — etiquetas puras y rutas de la API**

```ts
// frontend/src/modules/clients/__tests__/labels.test.ts
import { describe, expect, it } from "vitest";

import type { AssignmentOut } from "../../health/types";
import { clientHealthDefault, clientKindLabel, parseDomains } from "../labels";

const assignment = (over: Partial<AssignmentOut>): AssignmentOut => ({
  id: "a",
  profile_id: "p",
  profile_name: "Baseline",
  device_id: null,
  device_group_id: null,
  client_id: null,
  level: "org",
  created_by: null,
  created_at: "2026-10-01T00:00:00Z",
  ...over,
});

describe("parseDomains", () => {
  it("takes one domain per line or comma, blanks dropped", () => {
    expect(parseDomains(" andes.example \n\nbodega.example, otra.example ")).toEqual([
      "andes.example",
      "bodega.example",
      "otra.example",
    ]);
    expect(parseDomains("   ")).toEqual([]);
  });
});

describe("clientHealthDefault", () => {
  it("finds the client's own default and the organisation's, and no other client's", () => {
    const rows = [
      assignment({ id: "org", level: "org", profile_name: "Baseline" }),
      assignment({ id: "mine", level: "client", client_id: "c-andes", profile_name: "Servers" }),
      assignment({ id: "theirs", level: "client", client_id: "c-other", profile_name: "Kiosks" }),
      assignment({ id: "group", level: "group", client_id: "c-andes", device_group_id: "g1" }),
    ];
    expect(clientHealthDefault(rows, "c-andes")).toEqual({ own: rows[1], org: rows[0] });
    expect(clientHealthDefault(rows, "c-none")).toEqual({ own: null, org: rows[0] });
    expect(clientHealthDefault([], "c-andes")).toEqual({ own: null, org: null });
  });
});

describe("clientKindLabel", () => {
  it("words the three kinds and passes an unknown one through", () => {
    expect(clientKindLabel("customer")).toBe("Customer");
    expect(clientKindLabel("internal")).toBe("Your own IT");
    expect(clientKindLabel("unassigned")).toBe("Unassigned");
    expect(clientKindLabel("partner")).toBe("partner");
  });
});
```

```ts
// frontend/src/lib/api.clients.test.ts
/**
 * The Clients page's routes (MSP clients phase 3, design §4.1), as
 * `backend/app/api/v1/clients.py` declares them: each method's verb, path and
 * body. An id is one path segment, encoded.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "./api";

function recorder(body: unknown = {}) {
  const fetchMock = vi
    .fn()
    .mockImplementation(() => Promise.resolve(new Response(JSON.stringify(body), { status: 200 })));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function sent(fetchMock: ReturnType<typeof vi.fn>): [string, string, unknown][] {
  return (fetchMock.mock.calls as [string, RequestInit][]).map(([url, init]) => [
    init.method ?? "GET",
    url,
    init.body === undefined ? undefined : JSON.parse(String(init.body)),
  ]);
}

afterEach(() => vi.unstubAllGlobals());

describe("the Clients page's routes", () => {
  it("names each route and sends each body as the API declares it", async () => {
    const fetchMock = recorder();
    const client = new ApiClient(() => "t", vi.fn(), "/api/v1", () => null);
    await client.getClientRecord("c 1");
    await client.createClient({
      name: "Andes",
      email_domains: ["andes.example"],
      phone: "",
      address: "",
      website: "",
      notes: "",
      enabled_modules: ["change"],
    });
    await client.updateClient("c1", { enabled_modules: ["budget"] });
    await client.archiveClient("c1");
    await client.listClientSites("c1", { include_archived: true });
    await client.createClientSite("c1", { name: "Norte", address: "", description: "" });
    await client.updateSite("s1", { name: "Sur" });
    await client.archiveSite("s1");
    await client.listContacts("c1");
    await client.createContact("c1", { email: "ana@andes.example", display_name: "Ana", phone: null });
    expect(sent(fetchMock)).toEqual([
      ["GET", "/api/v1/clients/c%201", undefined],
      [
        "POST",
        "/api/v1/clients",
        {
          name: "Andes",
          email_domains: ["andes.example"],
          phone: "",
          address: "",
          website: "",
          notes: "",
          enabled_modules: ["change"],
        },
      ],
      ["PATCH", "/api/v1/clients/c1", { enabled_modules: ["budget"] }],
      ["POST", "/api/v1/clients/c1/archive", undefined],
      ["GET", "/api/v1/clients/c1/sites?include_archived=true", undefined],
      ["POST", "/api/v1/clients/c1/sites", { name: "Norte", address: "", description: "" }],
      ["PATCH", "/api/v1/sites/s1", { name: "Sur" }],
      ["DELETE", "/api/v1/sites/s1", undefined],
      ["GET", "/api/v1/clients/c1/contacts", undefined],
      ["POST", "/api/v1/clients/c1/contacts", { email: "ana@andes.example", display_name: "Ana", phone: null }],
    ]);
  });

  it("keeps the one-argument sites read that the site pickers use", async () => {
    const fetchMock = recorder([]);
    await new ApiClient(() => "t", vi.fn(), "/api/v1", () => null).listClientSites("c1");
    expect(sent(fetchMock)).toEqual([["GET", "/api/v1/clients/c1/sites", undefined]]);
  });
});
```

- [ ] **Step 2: Comprobar que fallan**

Run (desde `frontend/`): `npx vitest run src/modules/clients/__tests__/labels.test.ts src/lib/api.clients.test.ts`
Expected: FAIL — `Failed to resolve import "../labels"`, y `client.getClientRecord is not a function`.

- [ ] **Step 3: Los tipos y los métodos de `ApiClient`**

En `frontend/src/lib/api.ts`, después de `ContactUpdate` (`:880-885`):

```ts
/** `POST /clients` (`ClientCreate`): a customer client, born with a site called Principal. */
export interface ClientCreate {
  name: string;
  email_domains: string[];
  phone: string;
  address: string;
  website: string;
  notes: string;
  enabled_modules: ("budget" | "bcp" | "change")[];
}

/** `PATCH /clients/{id}` (`ClientUpdate`): only the fields present are written. */
export interface ClientUpdate {
  name?: string;
  email_domains?: string[];
  phone?: string;
  address?: string;
  website?: string;
  notes?: string;
  enabled_modules?: ("budget" | "bcp" | "change")[];
}

/** `POST /clients/{id}/sites` (`SiteCreate`). */
export interface SiteCreate {
  name: string;
  address: string;
  description: string;
}

/** `PATCH /sites/{id}` (`SiteUpdate`): name, address and description only. */
export interface SiteUpdate {
  name?: string;
  address?: string;
  description?: string;
}

/** `POST /clients/{id}/contacts` (`ContactCreate`). A contact cannot sign in to the console. */
export interface ContactCreate {
  email: string;
  display_name: string;
  phone: string | null;
}
```

Sustituye `listClientSites` (`:1644-1647`) y añade, justo después, los métodos de clientes y sedes:

```ts
  /**
   * A client's sites (`clients.read`): archived ones left out unless
   * `include_archived` -- the Clients page's Sites tab asks for them; the site
   * pickers (IPAM, `SiteField`) call it with one argument.
   */
  listClientSites(
    clientId: string,
    params: { include_archived?: boolean } = {},
  ): Promise<ClientSiteOut[]> {
    const suffix = params.include_archived ? "?include_archived=true" : "";
    return this.request<ClientSiteOut[]>(`/clients/${encodeURIComponent(clientId)}/sites${suffix}`);
  }

  /** One client of the request's scope, archived or not (`clients.read`). */
  getClientRecord(clientId: string): Promise<ClientOut> {
    return this.request<ClientOut>(`/clients/${encodeURIComponent(clientId)}`);
  }

  /**
   * A new customer client and its Principal site. `clients.manage` on EVERY
   * client: a global route (`lib/globalRoutes.json`), so it never carries
   * `X-Client-Id`.
   */
  createClient(body: ClientCreate): Promise<ClientOut> {
    return this.request<ClientOut>("/clients", { method: "POST", body: JSON.stringify(body) });
  }

  /** `clients.manage` on that client. An email domain another client holds is a 409. */
  updateClient(clientId: string, body: ClientUpdate): Promise<ClientOut> {
    return this.request<ClientOut>(`/clients/${encodeURIComponent(clientId)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  /**
   * Archive a customer client. `clients.manage` on EVERY client (a global
   * route). 409 while it has active devices, and always for the internal and
   * unassigned clients; the server's sentence says which.
   */
  archiveClient(clientId: string): Promise<ClientOut> {
    return this.request<ClientOut>(`/clients/${encodeURIComponent(clientId)}/archive`, {
      method: "POST",
    });
  }

  /** `sites.manage` on that client. The unassigned client has no sites (422). */
  createClientSite(clientId: string, body: SiteCreate): Promise<ClientSiteOut> {
    return this.request<ClientSiteOut>(`/clients/${encodeURIComponent(clientId)}/sites`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  /** `sites.manage` on the site's client. */
  updateSite(siteId: string, body: SiteUpdate): Promise<ClientSiteOut> {
    return this.request<ClientSiteOut>(`/sites/${encodeURIComponent(siteId)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  /**
   * Archive a site (`DELETE /sites/{id}`, never a hard delete; `sites.manage`).
   * 409 while it holds devices, and for the internal client's Principal site.
   */
  archiveSite(siteId: string): Promise<ClientSiteOut> {
    return this.request<ClientSiteOut>(`/sites/${encodeURIComponent(siteId)}`, { method: "DELETE" });
  }
```

Antes de `updateContact` (`:1798`):

```ts
  /** A client's contacts, disabled ones included (`clients.read`). */
  listContacts(clientId: string): Promise<ContactOut[]> {
    return this.request<ContactOut[]>(`/clients/${encodeURIComponent(clientId)}/contacts`);
  }

  /** `clients.manage` on that client. One email address per organisation (409). */
  createContact(clientId: string, body: ContactCreate): Promise<ContactOut> {
    return this.request<ContactOut>(`/clients/${encodeURIComponent(clientId)}/contacts`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }
```

El comentario de `listClients` (`:1634-1637`) pasa a decir: «The clients of the request's scope, archived ones left out unless asked (`clients.read`). The Users tab's grant scope and the Clients page read these.»

- [ ] **Step 4: La edición compartida de `health/types.ts` (idéntica en la Task 14)**

`frontend/src/modules/health/types.ts:63`:

```ts
export type ResolutionLevel = "device" | "group" | "client" | "org";
```

`:189-205`, `AssignmentCreate` y `AssignmentOut` quedan así:

```ts
export interface AssignmentCreate {
  profile_id: string;
  device_id?: string | null;
  device_group_id?: string | null;
  /** A client's default (MSP clients phase 3, design §5.1): with no device and no group. */
  client_id?: string | null;
}

export interface AssignmentOut {
  id: string;
  profile_id: string;
  profile_name: string;
  device_id: string | null;
  device_group_id: string | null;
  /**
   * The client the assignment is for: the client of a client default, the
   * device's or the group's otherwise, and `null` on the organisation default
   * (MSP clients phase 3, design §5.1). OPTIONAL because additive: an API
   * from before phase 3 does not send it.
   */
  client_id?: string | null;
  // Spec §0.2's resolution level, decided server-side. The console never infers
  // it from which id happens to be null.
  level: ResolutionLevel;
  created_by: string | null;
  created_at: string;
}
```

- [ ] **Step 5: Las etiquetas y los hooks del módulo**

```ts
// frontend/src/modules/clients/labels.ts
/**
 * The Clients page's vocabulary (MSP clients phase 3, design §4.1), worded
 * when read so a language switch shows on the next render.
 */
import { translate } from "../../i18n/core";
import type { Kind } from "../../shell/components";
import type { AssignmentOut } from "../health/types";

/** Held equal to the server's `OptionalModule` (`backend/app/api/v1/clients.py`). */
export const OPTIONAL_MODULES = ["budget", "bcp", "change"] as const;
export type OptionalModule = (typeof OPTIONAL_MODULES)[number];

export function clientKindLabel(kind: string): string {
  switch (kind) {
    case "customer":
      return translate("clients.kind.customer");
    case "internal":
      return translate("clients.kind.internal");
    case "unassigned":
      return translate("clients.kind.unassigned");
    default:
      return kind;
  }
}

export function clientStatusLabel(status: string): string {
  switch (status) {
    case "active":
      return translate("clients.status.active");
    case "archived":
      return translate("clients.status.archived");
    default:
      return status;
  }
}

/** An archived client is history, not a fault: muted, never red. */
export function clientStatusKind(status: string): Kind {
  return status === "active" ? "ok" : "mut";
}

export function moduleLabel(module: OptionalModule): string {
  switch (module) {
    case "budget":
      return translate("clients.module.budget");
    case "bcp":
      return translate("clients.module.bcp");
    case "change":
      return translate("clients.module.change");
  }
}

export function moduleHint(module: OptionalModule): string {
  switch (module) {
    case "budget":
      return translate("clients.module.budgetHint");
    case "bcp":
      return translate("clients.module.bcpHint");
    case "change":
      return translate("clients.module.changeHint");
  }
}

/** One email domain per line (or comma), trimmed, blanks dropped. The server validates. */
export function parseDomains(text: string): string[] {
  return text
    .split(/\r?\n|,/)
    .map((item) => item.trim())
    .filter((item) => item !== "");
}

/**
 * The health profile a client's devices fall back to (design §5.1): its own
 * client default, and the organisation default that applies when there is
 * none. The resolution itself is the server's (`health/resolution.py`).
 */
export function clientHealthDefault(
  assignments: readonly AssignmentOut[],
  clientId: string,
): { own: AssignmentOut | null; org: AssignmentOut | null } {
  return {
    own: assignments.find((a) => a.level === "client" && a.client_id === clientId) ?? null,
    org: assignments.find((a) => a.level === "org") ?? null,
  };
}
```

```ts
// frontend/src/modules/clients/api.ts
/**
 * The Clients page's reads and writes (MSP clients phase 3, design §4.1),
 * over `backend/app/api/v1/clients.py`. Every write refreshes what names a
 * client elsewhere: the Users tab's list (`CLIENTS_KEY`) and `/me`, whose
 * `clients` carry the names and the optional modules the menu follows.
 */
import { type QueryClient, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type {
  ApiError,
  ClientCreate,
  ClientOut,
  ClientSiteOut,
  ClientUpdate,
  ContactCreate,
  ContactOut,
  ContactUpdate,
  SiteCreate,
  SiteUpdate,
} from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { CLIENTS_KEY } from "../settings/api";

export const CLIENT_LIST_KEY = "clients-page";
export const CLIENT_KEY = "client";
/** The same key the site pickers and IPAM read a client's sites under. */
export const CLIENT_SITES_KEY = "client-sites";
export const CONTACTS_KEY = "client-contacts";

const NO_RETRY = { retry: false } as const;

function afterClientChange(queryClient: QueryClient, clientId: string | null): void {
  void queryClient.invalidateQueries({ queryKey: [CLIENT_LIST_KEY] });
  void queryClient.invalidateQueries({ queryKey: [CLIENTS_KEY] });
  void queryClient.invalidateQueries({ queryKey: ["me"] });
  if (clientId !== null) void queryClient.invalidateQueries({ queryKey: [CLIENT_KEY, clientId] });
}

export function useClientList(includeArchived: boolean) {
  const api = useApi();
  return useQuery<ClientOut[], ApiError>({
    queryKey: [CLIENT_LIST_KEY, { includeArchived }],
    queryFn: () => api.listClients(includeArchived ? { include_archived: true } : {}),
    ...NO_RETRY,
  });
}

/** `enabled: false` while the console works on another client (the read would be a 404). */
export function useClientRecord(clientId: string, enabled: boolean) {
  const api = useApi();
  return useQuery<ClientOut, ApiError>({
    queryKey: [CLIENT_KEY, clientId],
    queryFn: () => api.getClientRecord(clientId),
    enabled,
    ...NO_RETRY,
  });
}

/** Archived sites too: the Sites tab lists them, the pickers never do. */
export function useClientSitesWithArchived(clientId: string) {
  const api = useApi();
  return useQuery<ClientSiteOut[], ApiError>({
    queryKey: [CLIENT_SITES_KEY, clientId, "with-archived"],
    queryFn: () => api.listClientSites(clientId, { include_archived: true }),
    ...NO_RETRY,
  });
}

export function useClientContacts(clientId: string) {
  const api = useApi();
  return useQuery<ContactOut[], ApiError>({
    queryKey: [CONTACTS_KEY, clientId],
    queryFn: () => api.listContacts(clientId),
    ...NO_RETRY,
  });
}

export function useCreateClientMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<ClientOut, ApiError, ClientCreate>({
    mutationFn: (body) => api.createClient(body),
    onSuccess: (saved) => afterClientChange(queryClient, saved.id),
  });
}

export function useUpdateClientMutation(clientId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<ClientOut, ApiError, ClientUpdate>({
    mutationFn: (body) => api.updateClient(clientId, body),
    onSuccess: () => afterClientChange(queryClient, clientId),
  });
}

export function useArchiveClientMutation(clientId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<ClientOut, ApiError, void>({
    mutationFn: () => api.archiveClient(clientId),
    onSuccess: () => afterClientChange(queryClient, clientId),
  });
}

function afterSiteChange(queryClient: QueryClient, clientId: string): void {
  // Every reader of a client's sites (pickers, IPAM, this page), and the
  // client's site count.
  void queryClient.invalidateQueries({ queryKey: [CLIENT_SITES_KEY] });
  afterClientChange(queryClient, clientId);
}

export function useCreateSiteMutation(clientId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<ClientSiteOut, ApiError, SiteCreate>({
    mutationFn: (body) => api.createClientSite(clientId, body),
    onSuccess: () => afterSiteChange(queryClient, clientId),
  });
}

export function useUpdateSiteMutation(clientId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<ClientSiteOut, ApiError, { siteId: string; body: SiteUpdate }>({
    mutationFn: ({ siteId, body }) => api.updateSite(siteId, body),
    onSuccess: () => afterSiteChange(queryClient, clientId),
  });
}

export function useArchiveSiteMutation(clientId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<ClientSiteOut, ApiError, string>({
    mutationFn: (siteId) => api.archiveSite(siteId),
    onSuccess: () => afterSiteChange(queryClient, clientId),
  });
}

export function useCreateContactMutation(clientId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<ContactOut, ApiError, ContactCreate>({
    mutationFn: (body) => api.createContact(clientId, body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: [CONTACTS_KEY, clientId] }),
  });
}

export function useUpdateContactMutation(clientId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<ContactOut, ApiError, { contactId: string; body: ContactUpdate }>({
    mutationFn: ({ contactId, body }) => api.updateContact(contactId, body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: [CONTACTS_KEY, clientId] }),
  });
}
```

- [ ] **Step 6: Los catálogos `clients` (en y es) y su registro**

```ts
// frontend/src/i18n/en/clients.ts
/** The Clients page (MSP clients phase 3, design §4.1). */
export const clients = {
  kind: {
    customer: "Customer",
    internal: "Your own IT",
    unassigned: "Unassigned",
  },
  status: {
    active: "Active",
    archived: "Archived",
  },
  module: {
    budget: "IT budget",
    bcp: "Continuity",
    change: "Changes",
    budgetHint: "Budgets, expenses and cost centres for this client.",
    bcpHint: "Continuity plans and recovery tests for this client.",
    changeHint: "Change requests, and their approvals, for this client's devices.",
  },
  list: {
    title: "Clients",
    description: "The companies you serve, with their sites, their contacts and the optional modules each one uses.",
    newClient: "New client",
    showArchived: "Show archived clients",
    tableLabel: "Clients",
    colClient: "Client",
    colKind: "Kind",
    colDevices: "Devices",
    colSites: "Sites",
    colStatus: "Status",
    empty: "No clients to show.",
  },
  form: {
    createTitle: "New client",
    editTitle: "Edit client",
    name: "Name",
    emailDomains: "Email domains",
    emailDomainsHelp: "One per line. Mail from these domains is filed under this client.",
    phone: "Phone",
    address: "Address",
    website: "Website",
    notes: "Notes",
    modules: "Optional modules",
    create: "Create client",
    creating: "Creating…",
    save: "Save",
    saving: "Saving…",
    createFailed: "The client was not created.",
    saveFailed: "The client was not saved.",
  },
  detail: {
    title: "Client",
    allClients: "All clients",
    tabs: {
      summary: "Summary",
      sites: "Sites",
      contacts: "Contacts",
      modules: "Modules",
    },
    otherSelected: "The console is working on {selected}, so this client's records cannot be read from here.",
    workOnThis: "Work on this client",
    workOnAll: "Work on all my clients",
    archived: "This client is archived. Its records are kept and take no changes.",
  },
  summary: {
    title: "Details",
    edit: "Edit",
    archive: "Archive client",
    kind: "Kind",
    status: "Status",
    emailDomains: "Email domains",
    phone: "Phone",
    address: "Address",
    website: "Website",
    notes: "Notes",
    devices: "Devices",
    sites: "Sites",
    notSet: "Not set",
    archiveTitle: "Archive this client",
    archiveBody:
      "Its enrollment tokens are revoked, its install links switched off and its contacts' sign-in turned off. Its records stay. The console cannot bring it back.",
    archiveConfirm: "Archive",
    archiveFailed: "The client was not archived.",
    healthTitle: "Default health profile",
    healthOwn: "{profile}, this client's own default.",
    healthOrg: "None of its own: its devices use the organisation default, {profile}.",
    healthNone:
      "None. Neither this client nor the organisation has a default profile, so a device with no profile of its own or of its group is not checked.",
    healthWhere: "Health profiles",
  },
  sites: {
    title: "Sites",
    subtitle: "The client's offices. A device, an enrollment token and an install link each belong to one.",
    add: "Add site",
    tableLabel: "Sites",
    colSite: "Site",
    colDevices: "Devices",
    colStatus: "Status",
    inUse: "In use",
    archivedOn: "Archived {date}",
    edit: "Edit",
    editAria: "Edit {name}",
    installLink: "Install link",
    installLinkAria: "Install link of {name}",
    archive: "Archive",
    archiveAria: "Archive {name}",
    empty: "This client has no sites.",
    unassigned: "The unassigned client has no sites.",
    formAddTitle: "Add a site",
    formEditTitle: "Edit site",
    name: "Name",
    address: "Address",
    description: "Description",
    save: "Save",
    saving: "Saving…",
    saveFailed: "The site was not saved.",
    archiveTitle: "Archive this site",
    archiveBody:
      "Its enrollment tokens are revoked and its install link switched off. A site that still holds devices cannot be archived.",
    archiveConfirm: "Archive",
    archiveFailed: "The site was not archived.",
  },
  contacts: {
    title: "Contacts",
    subtitle: "The client's people. They will use the portal, never this console.",
    add: "Add contact",
    tableLabel: "Contacts",
    colContact: "Contact",
    colPhone: "Phone",
    colStatus: "Status",
    active: "Active",
    disabled: "Turned off",
    edit: "Edit",
    editAria: "Edit {name}",
    turnOff: "Turn off",
    turnOffAria: "Turn off {name}",
    turnOn: "Turn on",
    turnOnAria: "Turn on {name}",
    empty: "This client has no contacts.",
    unassigned: "The unassigned client has no contacts.",
    formAddTitle: "Add a contact",
    formEditTitle: "Edit contact",
    email: "Email",
    displayName: "Name",
    phone: "Phone",
    save: "Save",
    saving: "Saving…",
    saveFailed: "The contact was not saved.",
    turnOffTitle: "Turn off this contact",
    turnOffBody: "They can no longer use the portal. You can turn them back on here.",
    turnOffConfirm: "Turn off",
    changeFailed: "The contact was not changed.",
  },
  modules: {
    title: "Optional modules",
    subtitle:
      "A module that is off does not exist for this client: its pages show nothing of the client and accept nothing for it.",
    unassigned: "The unassigned client has no optional modules.",
    saveFailed: "The modules were not changed.",
  },
};
```

```ts
// frontend/src/i18n/es/clients.ts
import type { Shape } from "../types";
import type { clients as en } from "../en/clients";

export const clients: Shape<typeof en> = {
  kind: {
    customer: "Cliente",
    internal: "TI propia",
    unassigned: "Sin asignar",
  },
  status: {
    active: "Activo",
    archived: "Archivado",
  },
  module: {
    budget: "Presupuesto de TI",
    bcp: "Continuidad",
    change: "Cambios",
    budgetHint: "Presupuestos, gastos y centros de costo de este cliente.",
    bcpHint: "Planes de continuidad y pruebas de recuperación de este cliente.",
    changeHint: "Solicitudes de cambio, y sus aprobaciones, para los dispositivos de este cliente.",
  },
  list: {
    title: "Clientes",
    description:
      "Las empresas a las que das servicio, con sus sedes, sus contactos y los módulos opcionales que usa cada una.",
    newClient: "Nuevo cliente",
    showArchived: "Mostrar los clientes archivados",
    tableLabel: "Clientes",
    colClient: "Cliente",
    colKind: "Tipo",
    colDevices: "Dispositivos",
    colSites: "Sedes",
    colStatus: "Estado",
    empty: "No hay clientes que mostrar.",
  },
  form: {
    createTitle: "Nuevo cliente",
    editTitle: "Editar el cliente",
    name: "Nombre",
    emailDomains: "Dominios de correo",
    emailDomainsHelp: "Uno por línea. El correo que llega desde estos dominios se archiva en este cliente.",
    phone: "Teléfono",
    address: "Dirección",
    website: "Sitio web",
    notes: "Notas",
    modules: "Módulos opcionales",
    create: "Crear el cliente",
    creating: "Creando…",
    save: "Guardar",
    saving: "Guardando…",
    createFailed: "No se creó el cliente.",
    saveFailed: "No se guardaron los cambios del cliente.",
  },
  detail: {
    title: "Cliente",
    allClients: "Todos los clientes",
    tabs: {
      summary: "Resumen",
      sites: "Sedes",
      contacts: "Contactos",
      modules: "Módulos",
    },
    otherSelected:
      "La consola está trabajando con {selected}, así que desde aquí no se pueden leer los registros de este cliente.",
    workOnThis: "Trabajar con este cliente",
    workOnAll: "Trabajar con todos mis clientes",
    archived: "Este cliente está archivado. Sus registros se conservan y no admiten cambios.",
  },
  summary: {
    title: "Datos",
    edit: "Editar",
    archive: "Archivar el cliente",
    kind: "Tipo",
    status: "Estado",
    emailDomains: "Dominios de correo",
    phone: "Teléfono",
    address: "Dirección",
    website: "Sitio web",
    notes: "Notas",
    devices: "Dispositivos",
    sites: "Sedes",
    notSet: "Sin indicar",
    archiveTitle: "Archivar este cliente",
    archiveBody:
      "Se revocan sus tokens de inscripción, se desactivan sus enlaces de instalación y sus contactos dejan de poder iniciar sesión. Sus registros se conservan. La consola no puede reactivarlo.",
    archiveConfirm: "Archivar",
    archiveFailed: "No se archivó el cliente.",
    healthTitle: "Perfil de estado por defecto",
    healthOwn: "{profile}, el propio de este cliente.",
    healthOrg: "No tiene uno propio: sus dispositivos usan el de la organización, {profile}.",
    healthNone:
      "Ninguno. Ni este cliente ni la organización tienen un perfil por defecto, así que un dispositivo sin perfil propio ni de su grupo no se comprueba.",
    healthWhere: "Perfiles de estado",
  },
  sites: {
    title: "Sedes",
    subtitle: "Las oficinas del cliente. Cada dispositivo, cada token de inscripción y cada enlace de instalación es de una.",
    add: "Añadir una sede",
    tableLabel: "Sedes",
    colSite: "Sede",
    colDevices: "Dispositivos",
    colStatus: "Estado",
    inUse: "En uso",
    archivedOn: "Archivada el {date}",
    edit: "Editar",
    editAria: "Editar {name}",
    installLink: "Enlace de instalación",
    installLinkAria: "Enlace de instalación de {name}",
    archive: "Archivar",
    archiveAria: "Archivar {name}",
    empty: "Este cliente no tiene sedes.",
    unassigned: "El cliente «Sin asignar» no tiene sedes.",
    formAddTitle: "Añadir una sede",
    formEditTitle: "Editar la sede",
    name: "Nombre",
    address: "Dirección",
    description: "Descripción",
    save: "Guardar",
    saving: "Guardando…",
    saveFailed: "No se guardó la sede.",
    archiveTitle: "Archivar esta sede",
    archiveBody:
      "Se revocan sus tokens de inscripción y se desactiva su enlace de instalación. Una sede que aún tiene dispositivos no se puede archivar.",
    archiveConfirm: "Archivar",
    archiveFailed: "No se archivó la sede.",
  },
  contacts: {
    title: "Contactos",
    subtitle: "Las personas del cliente. Usarán el portal, nunca esta consola.",
    add: "Añadir un contacto",
    tableLabel: "Contactos",
    colContact: "Contacto",
    colPhone: "Teléfono",
    colStatus: "Estado",
    active: "Activo",
    disabled: "Desactivado",
    edit: "Editar",
    editAria: "Editar {name}",
    turnOff: "Desactivar",
    turnOffAria: "Desactivar {name}",
    turnOn: "Activar",
    turnOnAria: "Activar {name}",
    empty: "Este cliente no tiene contactos.",
    unassigned: "El cliente «Sin asignar» no tiene contactos.",
    formAddTitle: "Añadir un contacto",
    formEditTitle: "Editar el contacto",
    email: "Correo electrónico",
    displayName: "Nombre",
    phone: "Teléfono",
    save: "Guardar",
    saving: "Guardando…",
    saveFailed: "No se guardó el contacto.",
    turnOffTitle: "Desactivar este contacto",
    turnOffBody: "La persona ya no podrá usar el portal. Puedes volver a activarla aquí.",
    turnOffConfirm: "Desactivar",
    changeFailed: "No se cambió el contacto.",
  },
  modules: {
    title: "Módulos opcionales",
    subtitle:
      "Un módulo apagado no existe para este cliente: sus páginas no muestran nada del cliente y no aceptan nada para él.",
    unassigned: "El cliente «Sin asignar» no tiene módulos opcionales.",
    saveFailed: "No se cambiaron los módulos.",
  },
};
```

Registro: en `frontend/src/i18n/en/index.ts` añade `import { clients } from "./clients";` tras `import { aiAdmin } from "./aiAdmin";` y `clients` al final del objeto `en` (`…, aiAutomation, aiAdmin, clients }`). Lo mismo en `frontend/src/i18n/es/index.ts` (import tras `aiAdmin` y `clients` al final del objeto `es`).

`shell.ts`, el menú. En `en/shell.ts`: `nav.item` gana `clients: "Clients",` (tras `settings: "Settings",` `:55`); `nav.section` gana `organization: "Organization",` (primera clave, `:60`); `nav.hint` gana `clients: "The companies you serve, their sites, contacts and modules.",` (tras `settings` `:92`). En `es/shell.ts`, en los mismos sitios: `clients: "Clientes",`, `organization: "Organización",`, `clients: "Las empresas a las que das servicio, sus sedes, contactos y módulos.",`.

- [ ] **Step 7: Las pruebas que fallan — la página**

```tsx
// frontend/src/modules/clients/__tests__/clients.test.tsx
/**
 * The Clients page (MSP clients phase 3, design §4.1): the list, a client's
 * four tabs and their refusals. The console gates nothing (invariant 6): what
 * is asserted is what it OFFERS, from `/me` -- the server checks every route.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import type {
  ApiClient,
  ClientOut,
  ClientSiteOut,
  ContactOut,
  InstallLinkSettings,
  Me,
  MeClient,
} from "../../../lib/api";
import type { AssignmentOut } from "../../health/types";
import { ClientDetailPage } from "../ClientDetailPage";
import { ClientsPage } from "../ClientsPage";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const scope = vi.hoisted(() => ({
  selected: null as string | null,
  clients: [] as MeClient[],
  select: vi.fn(),
}));
vi.mock("../../../lib/clientScope", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/clientScope")>()),
  useClientScope: () => ({
    selected: scope.selected,
    select: scope.select,
    clients: scope.clients,
    ready: true,
  }),
}));

const ANDES: ClientOut = {
  id: "c-andes",
  name: "Andes Logistics",
  kind: "customer",
  status: "active",
  email_domains: ["andes.example"],
  phone: "+57 1 555 0100",
  address: "Calle 80 #10-20",
  website: "andes.example",
  notes: "",
  enabled_modules: ["budget"],
  device_count: 3,
  site_count: 2,
};
const INTERNAL: ClientOut = {
  ...ANDES,
  id: "org-1",
  name: "Toeshee",
  kind: "internal",
  email_domains: [],
  enabled_modules: ["budget", "bcp", "change"],
  device_count: 40,
  site_count: 1,
};
const UNASSIGNED: ClientOut = {
  ...ANDES,
  id: "c-unassigned",
  name: "Unassigned",
  kind: "unassigned",
  email_domains: [],
  enabled_modules: [],
  device_count: 0,
  site_count: 0,
};

const MAIN: ClientSiteOut = {
  id: "s-main",
  client_id: "c-andes",
  name: "Principal",
  address: "Calle 80 #10-20",
  description: "",
  egress_networks: [],
  archived_at: null,
  device_count: 3,
};
const NORTH: ClientSiteOut = { ...MAIN, id: "s-north", name: "Bodega norte", address: "", device_count: 0 };
const GONE: ClientSiteOut = {
  ...MAIN,
  id: "s-gone",
  name: "Old office",
  address: "",
  archived_at: "2026-09-01T00:00:00Z",
  device_count: 0,
};

const ANA: ContactOut = {
  id: "ct-ana",
  client_id: "c-andes",
  kind: "contact",
  email: "ana@andes.example",
  display_name: "Ana Rojas",
  phone: null,
  status: "active",
};

const LINK: InstallLinkSettings = {
  enabled: false,
  has_code: false,
  networks: [],
  disable_at: null,
  hourly_limit: 20,
  enabled_at: null,
  joined_since_enabled: 0,
};

const HEALTH_ORG: AssignmentOut = {
  id: "ha-org",
  profile_id: "hp-1",
  profile_name: "Baseline",
  device_id: null,
  device_group_id: null,
  client_id: null,
  level: "org",
  created_by: null,
  created_at: "2026-10-01T00:00:00Z",
};
const HEALTH_ANDES: AssignmentOut = {
  ...HEALTH_ORG,
  id: "ha-andes",
  profile_id: "hp-2",
  profile_name: "Servers",
  client_id: "c-andes",
  level: "client",
};

const ALL_CODES = ["clients.read", "clients.manage", "sites.manage", "health.read", "enrollment.link.manage"];

function meClient(client: ClientOut, permissions: string[] = ALL_CODES): MeClient {
  return {
    id: client.id,
    name: client.name,
    kind: client.kind,
    enabled_modules: client.enabled_modules,
    permissions,
  };
}

function me(everywhere: string[] = ALL_CODES): Me {
  return {
    user_id: "u1",
    org_id: "org-1",
    email: "op@example.com",
    display_name: "Operator",
    permissions: ALL_CODES,
    permissions_everywhere: everywhere,
    clients: [],
  };
}

function api(overrides: Partial<ApiClient> = {}): ApiClient {
  return {
    me: vi.fn().mockResolvedValue(me()),
    listClients: vi.fn().mockResolvedValue([INTERNAL, ANDES, UNASSIGNED]),
    getClientRecord: vi.fn().mockResolvedValue(ANDES),
    listClientSites: vi.fn().mockResolvedValue([MAIN, NORTH, GONE]),
    listContacts: vi.fn().mockResolvedValue([ANA]),
    listHealthAssignments: vi.fn().mockResolvedValue([]),
    getInstallLink: vi.fn().mockResolvedValue(LINK),
    ...overrides,
  } as unknown as ApiClient;
}

function renderAt(entry: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/clients" element={<ClientsPage />} />
          <Route path="/clients/:clientId" element={<ClientDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockUseApi.mockReset();
  scope.selected = null;
  scope.clients = [meClient(INTERNAL), meClient(ANDES), meClient(UNASSIGNED)];
  scope.select = vi.fn();
});

describe("the Clients list", () => {
  it("lists each client with its kind, devices, sites and status, linking to its page", async () => {
    mockUseApi.mockReturnValue(api());
    renderAt("/clients");
    const link = await screen.findByRole("link", { name: "Andes Logistics" });
    expect(link).toHaveAttribute("href", "/clients/c-andes");
    const row = link.closest("tr") as HTMLElement;
    expect(within(row).getByText("Customer")).toBeInTheDocument();
    expect(within(row).getByText("3")).toBeInTheDocument();
    expect(within(row).getByText("2")).toBeInTheDocument();
    expect(within(row).getByText("Active")).toBeInTheDocument();
    const own = screen.getByRole("link", { name: "Toeshee" }).closest("tr") as HTMLElement;
    expect(within(own).getByText("Your own IT")).toBeInTheDocument();
  });

  it("reads archived clients only when asked to", async () => {
    const listClients = vi.fn().mockResolvedValue([INTERNAL, ANDES]);
    mockUseApi.mockReturnValue(api({ listClients }));
    const user = userEvent.setup();
    renderAt("/clients");
    await screen.findByRole("link", { name: "Andes Logistics" });
    expect(listClients).toHaveBeenLastCalledWith({});
    await user.click(screen.getByRole("switch", { name: "Show archived clients" }));
    await waitFor(() => expect(listClients).toHaveBeenLastCalledWith({ include_archived: true }));
  });

  it("offers New client only to a person holding clients.manage on every client", async () => {
    const without = vi.fn().mockResolvedValue(me(["clients.read"]));
    mockUseApi.mockReturnValue(api({ me: without }));
    const { unmount } = renderAt("/clients");
    await screen.findByRole("link", { name: "Andes Logistics" });
    await waitFor(() => expect(without).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "New client" })).toBeNull();
    unmount();

    mockUseApi.mockReturnValue(api());
    renderAt("/clients");
    expect(await screen.findByRole("button", { name: "New client" })).toBeInTheDocument();
  });

  it("creates a client with its domains and modules, then opens it", async () => {
    const created: ClientOut = {
      ...ANDES,
      id: "c-new",
      name: "Bodegas del Sur",
      email_domains: ["bodegas.example", "sur.example"],
      enabled_modules: ["change"],
      device_count: 0,
      site_count: 1,
    };
    const createClient = vi.fn().mockResolvedValue(created);
    mockUseApi.mockReturnValue(api({ createClient, getClientRecord: vi.fn().mockResolvedValue(created) }));
    const user = userEvent.setup();
    renderAt("/clients");
    await user.click(await screen.findByRole("button", { name: "New client" }));
    const dialog = await screen.findByRole("dialog", { name: "New client" });
    await user.type(within(dialog).getByLabelText("Name"), "Bodegas del Sur");
    await user.type(within(dialog).getByLabelText("Email domains"), "bodegas.example{enter}sur.example");
    await user.type(within(dialog).getByLabelText("Phone"), "+57 1 555 0199");
    await user.click(within(dialog).getByRole("checkbox", { name: "Changes" }));
    await user.click(within(dialog).getByRole("button", { name: "Create client" }));

    await waitFor(() => expect(createClient).toHaveBeenCalledTimes(1));
    expect(createClient).toHaveBeenCalledWith({
      name: "Bodegas del Sur",
      email_domains: ["bodegas.example", "sur.example"],
      phone: "+57 1 555 0199",
      address: "",
      website: "",
      notes: "",
      enabled_modules: ["change"],
    });
    expect(await screen.findByRole("heading", { name: "Bodegas del Sur", level: 1 })).toBeInTheDocument();
  });

  it("names the field a create is missing instead of sending it", async () => {
    const createClient = vi.fn();
    mockUseApi.mockReturnValue(api({ createClient }));
    const user = userEvent.setup();
    renderAt("/clients");
    await user.click(await screen.findByRole("button", { name: "New client" }));
    const dialog = await screen.findByRole("dialog", { name: "New client" });
    await user.click(within(dialog).getByRole("button", { name: "Create client" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Name");
    expect(createClient).not.toHaveBeenCalled();
  });
});

describe("a client's Summary", () => {
  it("shows the client's details and its own default health profile", async () => {
    mockUseApi.mockReturnValue(
      api({ listHealthAssignments: vi.fn().mockResolvedValue([HEALTH_ORG, HEALTH_ANDES]) }),
    );
    renderAt("/clients/c-andes");
    expect(await screen.findByRole("heading", { name: "Andes Logistics", level: 1 })).toBeInTheDocument();
    expect(screen.getByText("Calle 80 #10-20")).toBeInTheDocument();
    expect(await screen.findByText("Servers, this client's own default.")).toBeInTheDocument();
  });

  it("says the organisation default applies when the client has none of its own", async () => {
    mockUseApi.mockReturnValue(api({ listHealthAssignments: vi.fn().mockResolvedValue([HEALTH_ORG]) }));
    renderAt("/clients/c-andes");
    expect(
      await screen.findByText("None of its own: its devices use the organisation default, Baseline."),
    ).toBeInTheDocument();
  });

  it("does not read health profiles for a person without health.read on the client", async () => {
    scope.clients = [meClient(ANDES, ["clients.read"])];
    const listHealthAssignments = vi.fn().mockResolvedValue([]);
    mockUseApi.mockReturnValue(api({ listHealthAssignments }));
    renderAt("/clients/c-andes");
    await screen.findByRole("heading", { name: "Andes Logistics", level: 1 });
    expect(screen.queryByText("Default health profile")).toBeNull();
    expect(listHealthAssignments).not.toHaveBeenCalled();
  });

  it("explains a refused archive in the server's words", async () => {
    const archiveClient = vi.fn().mockRejectedValue(
      new ApiError(409, JSON.stringify({ detail: "this client has 3 active devices; retire them before archiving it" })),
    );
    mockUseApi.mockReturnValue(api({ archiveClient }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes");
    await user.click(await screen.findByRole("button", { name: "Archive client" }));
    const dialog = await screen.findByRole("dialog", { name: "Archive this client" });
    await user.click(within(dialog).getByRole("button", { name: "Archive" }));
    expect(archiveClient).toHaveBeenCalledWith("c-andes");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The client was not archived. this client has 3 active devices; retire them before archiving it",
    );
  });

  it("does not offer Archive for the internal or the unassigned client, nor without every client", async () => {
    for (const client of [INTERNAL, UNASSIGNED]) {
      scope.clients = [meClient(client)];
      const meFn = vi.fn().mockResolvedValue(me());
      mockUseApi.mockReturnValue(api({ me: meFn, getClientRecord: vi.fn().mockResolvedValue(client) }));
      const { unmount } = renderAt(`/clients/${client.id}`);
      await screen.findByRole("heading", { name: client.name, level: 1 });
      await waitFor(() => expect(meFn).toHaveBeenCalled());
      expect(screen.queryByRole("button", { name: "Archive client" })).toBeNull();
      unmount();
    }
    scope.clients = [meClient(ANDES)];
    const narrow = vi.fn().mockResolvedValue(me(["clients.read"]));
    mockUseApi.mockReturnValue(api({ me: narrow }));
    renderAt("/clients/c-andes");
    await screen.findByRole("heading", { name: "Andes Logistics", level: 1 });
    await waitFor(() => expect(narrow).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Archive client" })).toBeNull();
  });

  it("says when the console works on another client, and reads nothing", async () => {
    scope.selected = "org-1";
    const getClientRecord = vi.fn().mockResolvedValue(ANDES);
    mockUseApi.mockReturnValue(api({ getClientRecord }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes");
    expect(await screen.findByText(/The console is working on Toeshee/)).toBeInTheDocument();
    expect(getClientRecord).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Work on this client" }));
    expect(scope.select).toHaveBeenCalledWith("c-andes");
  });
});

describe("a client's Sites", () => {
  it("lists the sites, adds one, and explains a refused archive", async () => {
    const createClientSite = vi.fn().mockResolvedValue({ ...NORTH, id: "s-new", name: "Sede sur" });
    const archiveSite = vi.fn().mockRejectedValue(
      new ApiError(409, JSON.stringify({ detail: "this site has 3 devices; a site with devices cannot be archived" })),
    );
    const listClientSites = vi.fn().mockResolvedValue([MAIN, NORTH, GONE]);
    mockUseApi.mockReturnValue(api({ createClientSite, archiveSite, listClientSites }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes?tab=sites");

    const main = (await screen.findByText("Principal")).closest("tr") as HTMLElement;
    expect(listClientSites).toHaveBeenCalledWith("c-andes", { include_archived: true });
    expect(within(main).getByText("In use")).toBeInTheDocument();
    const gone = screen.getByText("Old office").closest("tr") as HTMLElement;
    expect(within(gone).getByText(/^Archived /)).toBeInTheDocument();
    expect(within(gone).queryByRole("button", { name: "Archive Old office" })).toBeNull();

    await user.click(screen.getByRole("button", { name: "Add site" }));
    const dialog = await screen.findByRole("dialog", { name: "Add a site" });
    await user.type(within(dialog).getByLabelText("Name"), "Sede sur");
    await user.type(within(dialog).getByLabelText("Address"), "Cra 7 #12-34");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(createClientSite).toHaveBeenCalledWith("c-andes", {
        name: "Sede sur",
        address: "Cra 7 #12-34",
        description: "",
      }),
    );

    await user.click(within(main).getByRole("button", { name: "Archive Principal" }));
    await user.click(
      within(await screen.findByRole("dialog", { name: "Archive this site" })).getByRole("button", {
        name: "Archive",
      }),
    );
    expect(archiveSite).toHaveBeenCalledWith("s-main");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The site was not archived. this site has 3 devices; a site with devices cannot be archived",
    );
  });

  it("opens a site's install link for a person who manages install links on the client", async () => {
    const getInstallLink = vi.fn().mockResolvedValue(LINK);
    mockUseApi.mockReturnValue(api({ getInstallLink }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes?tab=sites");
    await user.click(await screen.findByRole("button", { name: "Install link of Principal" }));
    await waitFor(() => expect(getInstallLink).toHaveBeenCalledWith("s-main"));
  });

  it("offers no install link and no edits without the codes on the client", async () => {
    scope.clients = [meClient(ANDES, ["clients.read"])];
    mockUseApi.mockReturnValue(api());
    renderAt("/clients/c-andes?tab=sites");
    await screen.findByText("Principal");
    expect(screen.queryByRole("button", { name: "Install link of Principal" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add site" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Archive Principal" })).toBeNull();
  });
});

describe("a client's Contacts", () => {
  it("adds a contact and turns one off", async () => {
    const createContact = vi.fn().mockResolvedValue({ ...ANA, id: "ct-new", email: "luis@andes.example" });
    const updateContact = vi.fn().mockResolvedValue({ ...ANA, status: "disabled" });
    mockUseApi.mockReturnValue(api({ createContact, updateContact }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes?tab=contacts");

    const ana = (await screen.findByText("Ana Rojas")).closest("tr") as HTMLElement;
    await user.click(screen.getByRole("button", { name: "Add contact" }));
    const dialog = await screen.findByRole("dialog", { name: "Add a contact" });
    await user.type(within(dialog).getByLabelText("Email"), "luis@andes.example");
    await user.type(within(dialog).getByLabelText("Name"), "Luis Pardo");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(createContact).toHaveBeenCalledWith("c-andes", {
        email: "luis@andes.example",
        display_name: "Luis Pardo",
        phone: null,
      }),
    );

    await user.click(within(ana).getByRole("button", { name: "Turn off Ana Rojas" }));
    await user.click(
      within(await screen.findByRole("dialog", { name: "Turn off this contact" })).getByRole("button", {
        name: "Turn off",
      }),
    );
    expect(updateContact).toHaveBeenCalledWith("ct-ana", { status: "disabled" });
  });
});

describe("a client's Modules", () => {
  it("switches an optional module through enabled_modules", async () => {
    const updateClient = vi.fn().mockResolvedValue({ ...ANDES, enabled_modules: ["budget", "change"] });
    mockUseApi.mockReturnValue(api({ updateClient }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes?tab=modules");
    const changes = await screen.findByRole("switch", { name: "Changes" });
    expect(changes).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("switch", { name: "IT budget" })).toHaveAttribute("aria-checked", "true");
    await user.click(changes);
    await waitFor(() =>
      expect(updateClient).toHaveBeenCalledWith("c-andes", { enabled_modules: ["budget", "change"] }),
    );
  });

  it("offers no switch to a person without clients.manage on the client", async () => {
    scope.clients = [meClient(ANDES, ["clients.read"])];
    mockUseApi.mockReturnValue(api());
    renderAt("/clients/c-andes?tab=modules");
    expect(await screen.findByRole("switch", { name: "Changes" })).toBeDisabled();
  });
});
```

```tsx
// frontend/src/modules/clients/clients.es.test.tsx
/**
 * The Clients page in Spanish: the list and every tab of a client's page.
 * Client, site and contact names read as recorded; the words around them are
 * Spanish.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import type { ApiClient, ClientOut, ClientSiteOut, ContactOut, Me, MeClient } from "../../lib/api";
import { ClientDetailPage } from "./ClientDetailPage";
import { ClientsPage } from "./ClientsPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const scope = vi.hoisted(() => ({ clients: [] as MeClient[] }));
vi.mock("../../lib/clientScope", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/clientScope")>()),
  useClientScope: () => ({ selected: null, select: vi.fn(), clients: scope.clients, ready: true }),
}));

const CLIENT: ClientOut = {
  id: "c-andes",
  name: "Transportes Andes",
  kind: "customer",
  status: "active",
  email_domains: ["andes.example"],
  phone: "",
  address: "Calle 80 #10-20",
  website: "",
  notes: "",
  enabled_modules: ["budget"],
  device_count: 3,
  site_count: 2,
};
const SITES: ClientSiteOut[] = [
  { id: "s-main", client_id: "c-andes", name: "Principal", address: "", description: "", egress_networks: [], archived_at: null, device_count: 3 },
  { id: "s-old", client_id: "c-andes", name: "Bodega vieja", address: "", description: "", egress_networks: [], archived_at: "2026-09-01T00:00:00Z", device_count: 0 },
];
const CONTACTS: ContactOut[] = [
  { id: "ct-1", client_id: "c-andes", kind: "contact", email: "ana@andes.example", display_name: "Ana Rojas", phone: null, status: "active" },
  { id: "ct-2", client_id: "c-andes", kind: "contact", email: "compras@andes.example", display_name: "Compras", phone: null, status: "disabled" },
];
const CODES = ["clients.read", "clients.manage", "sites.manage", "health.read", "enrollment.link.manage"];

function renderAt(entry: string) {
  const me: Me = {
    user_id: "u1",
    org_id: "org-1",
    email: "op@example.com",
    display_name: "Operator",
    permissions: CODES,
    permissions_everywhere: CODES,
    clients: [],
  };
  mockUseApi.mockReturnValue({
    me: vi.fn().mockResolvedValue(me),
    listClients: vi.fn().mockResolvedValue([CLIENT, { ...CLIENT, id: "org-1", name: "Toeshee", kind: "internal" }]),
    getClientRecord: vi.fn().mockResolvedValue(CLIENT),
    listClientSites: vi.fn().mockResolvedValue(SITES),
    listContacts: vi.fn().mockResolvedValue(CONTACTS),
    listHealthAssignments: vi.fn().mockResolvedValue([]),
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/clients" element={<ClientsPage />} />
          <Route path="/clients/:clientId" element={<ClientDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockUseApi.mockReset();
  scope.clients = [
    { id: "c-andes", name: "Transportes Andes", kind: "customer", enabled_modules: ["budget"], permissions: CODES },
  ];
});

describe("Clientes, en español", () => {
  it("words the list", async () => {
    const { container } = renderAt("/clients");
    expect(await screen.findByRole("heading", { name: "Clientes", level: 1 })).toBeInTheDocument();
    expect(await screen.findByText("TI propia")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Nuevo cliente" })).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Mostrar los clientes archivados" })).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it.each([
    ["summary", "Perfil de estado por defecto"],
    ["sites", "Añadir una sede"],
    ["contacts", "Añadir un contacto"],
    ["modules", "Módulos opcionales"],
  ])("words the %s tab", async (tab, text) => {
    const { container } = renderAt(`/clients/c-andes?tab=${tab}`);
    expect(await screen.findByRole("heading", { name: "Transportes Andes", level: 1 })).toBeInTheDocument();
    expect(await screen.findByText(text)).toBeInTheDocument();
    expectNoEnglish(container);
  });
});
```

- [ ] **Step 8: Comprobar que fallan**

Run: `npx vitest run src/modules/clients`
Expected: FAIL — `Failed to resolve import "../ClientDetailPage"` (y lo mismo para `ClientsPage`).

- [ ] **Step 9: La lista y el formulario del cliente**

```tsx
// frontend/src/modules/clients/ClientForm.tsx
/**
 * A client's details as a form: "New client" (with the optional modules, the
 * one moment they are chosen here) and "Edit" (the modules have their own
 * tab). Only the name is required; the server validates the domains and
 * refuses one another client holds (409), and that sentence is shown as is.
 */
import { useState } from "react";

import { Form, FormActions, attemptProblems } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Button, Dialog, Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { ClientOut } from "../../lib/api";
import { useCreateClientMutation, useUpdateClientMutation } from "./api";
import { OPTIONAL_MODULES, type OptionalModule, moduleLabel, parseDomains } from "./labels";

const clientRules = (): readonly FieldRule[] => [{ name: "name", label: translate("clients.form.name") }];

export function ClientFormDialog({
  open,
  client,
  onClose,
  onSaved,
}: {
  open: boolean;
  /** `null` creates a client; a client edits it. */
  client: ClientOut | null;
  onClose: () => void;
  onSaved: (saved: ClientOut) => void;
}) {
  const t = useT();
  const create = useCreateClientMutation();
  const update = useUpdateClientMutation(client?.id ?? "");
  const mutation = client ? update : create;
  const [name, setName] = useState(client?.name ?? "");
  const [domains, setDomains] = useState((client?.email_domains ?? []).join("\n"));
  const [phone, setPhone] = useState(client?.phone ?? "");
  const [address, setAddress] = useState(client?.address ?? "");
  const [website, setWebsite] = useState(client?.website ?? "");
  const [notes, setNotes] = useState(client?.notes ?? "");
  const [modules, setModules] = useState<ReadonlySet<OptionalModule>>(new Set());
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function toggle(module: OptionalModule) {
    setModules((current) => {
      const next = new Set(current);
      if (next.has(module)) next.delete(module);
      else next.add(module);
      return next;
    });
  }

  function submit() {
    mutation.reset();
    const next: Attempt = { rules: clientRules, values: { name } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    const fields = {
      name: name.trim(),
      email_domains: parseDomains(domains),
      phone: phone.trim(),
      address: address.trim(),
      website: website.trim(),
      notes,
    };
    if (client) {
      update.mutate(fields, { onSuccess: onSaved });
    } else {
      create.mutate(
        { ...fields, enabled_modules: OPTIONAL_MODULES.filter((module) => modules.has(module)) },
        { onSuccess: onSaved },
      );
    }
  }

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={client ? t("clients.form.editTitle") : t("clients.form.createTitle")}
    >
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("clients.form.name")}>
            <input className={inputClass} value={name} onChange={(event) => setName(event.target.value)} />
          </Field>
          <Field label={t("clients.form.emailDomains")} help={t("clients.form.emailDomainsHelp")} full>
            <textarea
              className={inputClass}
              rows={3}
              value={domains}
              onChange={(event) => setDomains(event.target.value)}
            />
          </Field>
          <Field label={t("clients.form.phone")}>
            <input className={inputClass} value={phone} onChange={(event) => setPhone(event.target.value)} />
          </Field>
          <Field label={t("clients.form.website")}>
            <input className={inputClass} value={website} onChange={(event) => setWebsite(event.target.value)} />
          </Field>
          <Field label={t("clients.form.address")} full>
            <input className={inputClass} value={address} onChange={(event) => setAddress(event.target.value)} />
          </Field>
          <Field label={t("clients.form.notes")} full>
            <textarea
              className={inputClass}
              rows={3}
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
            />
          </Field>
          {client === null ? (
            <fieldset className="field full">
              <legend>{t("clients.form.modules")}</legend>
              {OPTIONAL_MODULES.map((module) => (
                <label key={module} className="flex items-center gap-2">
                  <input type="checkbox" checked={modules.has(module)} onChange={() => toggle(module)} />
                  <span>{moduleLabel(module)}</span>
                </label>
              ))}
            </fieldset>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.actions.cancel")}
          </Button>
          <FormActions
            label={client ? t("clients.form.save") : t("clients.form.create")}
            pendingLabel={client ? t("clients.form.saving") : t("clients.form.creating")}
            pending={mutation.isPending}
            error={mutation.error}
            failedMessage={client ? t("clients.form.saveFailed") : t("clients.form.createFailed")}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}
```

```tsx
// frontend/src/modules/clients/ClientsPage.tsx
/**
 * Clients (MSP clients phase 3, design §4.1): the companies this organisation
 * serves, and its two system clients -- its own IT (`internal`) and
 * `unassigned`, which `GET /clients` lists only to a person with every
 * client. Archived clients are hidden unless asked for.
 *
 * "NEW CLIENT" NEEDS `clients.manage` ON EVERY CLIENT (`POST /clients` is a
 * global route), so it is offered on `permissions_everywhere`. Offering it is
 * a courtesy: the server checks the code on every request (invariant 6).
 */
import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { useEverywherePermissions } from "../../lib/mePermissions";
import { Btn, FilterBar, PageHead, Panel, QueryState, StatusPill, Toggle } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useClientList } from "./api";
import { ClientFormDialog } from "./ClientForm";
import { clientKindLabel, clientStatusKind, clientStatusLabel } from "./labels";

export function ClientsPage() {
  const t = useT();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const archived = params.get("archived") === "1";
  const everywhere = useEverywherePermissions();
  const clients = useClientList(archived);
  const [creating, setCreating] = useState(false);
  const rows = clients.data ?? [];

  function showArchived(next: boolean) {
    const query = new URLSearchParams(params);
    if (next) query.set("archived", "1");
    else query.delete("archived");
    setParams(query, { replace: true });
  }

  return (
    <>
      <PageHead
        title={t("clients.list.title")}
        description={t("clients.list.description")}
        actions={
          everywhere.has("clients.manage") ? (
            <Btn
              label={t("clients.list.newClient")}
              icon="plus"
              variant="primary"
              onClick={() => setCreating(true)}
            />
          ) : undefined
        }
      />
      <Panel bare>
        <FilterBar>
          <Toggle checked={archived} onChange={showArchived} label={t("clients.list.showArchived")} />
          <span className="filter-spacer" />
        </FilterBar>
        <QueryState
          loading={clients.isLoading}
          error={clients.error}
          empty={rows.length === 0}
          emptyLabel={t("clients.list.empty")}
          emptyIcon="building"
        >
          <Table label={t("clients.list.tableLabel")}>
            <thead>
              <tr>
                <Th>{t("clients.list.colClient")}</Th>
                <Th>{t("clients.list.colKind")}</Th>
                <Th>{t("clients.list.colDevices")}</Th>
                <Th>{t("clients.list.colSites")}</Th>
                <Th>{t("clients.list.colStatus")}</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((client) => (
                <tr key={client.id}>
                  <Td lead={<Icon name="building" size={18} />}>
                    <div className="cell-title">
                      <Link to={`/clients/${client.id}`}>{client.name}</Link>
                    </div>
                    {client.email_domains.length > 0 ? (
                      <div className="cell-sub">{client.email_domains.join(", ")}</div>
                    ) : null}
                  </Td>
                  <Td>{clientKindLabel(client.kind)}</Td>
                  <Td>
                    <span className="mono">{client.device_count}</span>
                  </Td>
                  <Td>
                    <span className="mono">{client.site_count}</span>
                  </Td>
                  <Td>
                    <StatusPill
                      value={client.status}
                      kind={clientStatusKind(client.status)}
                      label={clientStatusLabel(client.status)}
                    />
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </QueryState>
      </Panel>
      {/* KEYED so a refusal cannot outlive the dialog that produced it. */}
      <ClientFormDialog
        key={creating ? "open" : "closed"}
        open={creating}
        client={null}
        onClose={() => setCreating(false)}
        onSaved={(saved) => {
          setCreating(false);
          navigate(`/clients/${saved.id}`);
        }}
      />
    </>
  );
}
```

- [ ] **Step 10: La ficha y sus cuatro pestañas**

```tsx
// frontend/src/modules/clients/ClientDetailPage.tsx
/**
 * One client (MSP clients phase 3, design §4.1): Summary, Sites, Contacts and
 * Modules, the tab in the URL (`?tab=`) so another page can open one.
 *
 * ANOTHER CLIENT CHOSEN ABOVE. Every read here carries `X-Client-Id` (Task 7),
 * and with another client's id this client does not exist for the request:
 * a 404 on every tab. So the page reads nothing and says which client the
 * console is working on, with the one act that fixes it.
 */
import { Link, useParams, useSearchParams } from "react-router-dom";

import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { useClientScope } from "../../lib/clientScope";
import { Btn, PageHead, QueryState, Tabs } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { KIND_ICON } from "../../shell/viz";
import { useClientRecord } from "./api";
import { ContactsTab } from "./ContactsTab";
import { clientKindLabel } from "./labels";
import { ModulesTab } from "./ModulesTab";
import { SitesTab } from "./SitesTab";
import { SummaryTab } from "./SummaryTab";

type Tab = "summary" | "sites" | "contacts" | "modules";

const TABS: readonly { key: Tab; label: MessageKey }[] = [
  { key: "summary", label: "clients.detail.tabs.summary" },
  { key: "sites", label: "clients.detail.tabs.sites" },
  { key: "contacts", label: "clients.detail.tabs.contacts" },
  { key: "modules", label: "clients.detail.tabs.modules" },
];

function parseTab(value: string | null): Tab {
  return TABS.find((tab) => tab.key === value)?.key ?? "summary";
}

export function ClientDetailPage() {
  const t = useT();
  const { clientId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const tab = parseTab(params.get("tab"));
  const { selected, select, clients } = useClientScope();
  const elsewhere = selected !== null && selected !== clientId;
  const client = useClientRecord(clientId, !elsewhere);

  const back = (
    <Link className="btn" to="/clients">
      {t("clients.detail.allClients")}
    </Link>
  );

  if (elsewhere) {
    const reachable = clients.some((item) => item.id === clientId);
    return (
      <>
        <PageHead title={t("clients.detail.title")} actions={back} />
        <div role="status" className="notice mb-4">
          <Icon name={KIND_ICON.info} size={17} />
          <div>
            {t("clients.detail.otherSelected", {
              selected: clients.find((item) => item.id === selected)?.name ?? selected,
            })}{" "}
            <Btn
              small
              label={reachable ? t("clients.detail.workOnThis") : t("clients.detail.workOnAll")}
              onClick={() => select(reachable ? clientId : null)}
            />
          </div>
        </div>
      </>
    );
  }

  function setTab(key: string) {
    const next = new URLSearchParams(params);
    if (key === "summary") next.delete("tab");
    else next.set("tab", key);
    setParams(next, { replace: true });
  }

  const data = client.data;
  return (
    <>
      <PageHead
        title={data?.name ?? t("clients.detail.title")}
        description={data ? clientKindLabel(data.kind) : undefined}
        actions={back}
      />
      <QueryState
        loading={client.isLoading}
        error={client.error}
        empty={false}
        emptyLabel={t("clients.list.empty")}
      >
        {data ? (
          <>
            {data.status === "archived" ? (
              <div role="status" className="notice mb-4">
                <Icon name={KIND_ICON.info} size={17} />
                <div>{t("clients.detail.archived")}</div>
              </div>
            ) : null}
            <Tabs
              items={TABS.map((item) => ({ key: item.key, label: t(item.label) }))}
              active={tab}
              onSelect={setTab}
              panelId="client-tabpanel"
            />
            <div
              id="client-tabpanel"
              role="tabpanel"
              aria-label={t(TABS.find((item) => item.key === tab)?.label ?? "clients.detail.tabs.summary")}
              tabIndex={-1}
            >
              {tab === "summary" ? (
                <SummaryTab client={data} />
              ) : tab === "sites" ? (
                <SitesTab client={data} />
              ) : tab === "contacts" ? (
                <ContactsTab client={data} />
              ) : (
                <ModulesTab client={data} />
              )}
            </div>
          </>
        ) : null}
      </QueryState>
    </>
  );
}
```

```tsx
// frontend/src/modules/clients/SummaryTab.tsx
/**
 * A client's details, its edit (`clients.manage` ON THIS CLIENT, from
 * `/me.clients`), its archive (`clients.manage` ON EVERY CLIENT, a global
 * route; never offered for the internal or the unassigned client) and the
 * health profile its devices fall back to (design §5.1), read-only.
 *
 * AN ARCHIVE REFUSED IS EXPLAINED. The server's 409 says why in the person's
 * language -- "this client has 3 active devices; retire them before archiving
 * it" -- and the page puts it under the button, after a sentence saying the
 * archive did not happen.
 */
import { type ReactNode, useState } from "react";
import { Link } from "react-router-dom";

import { ConfirmDialog } from "../../components/confirm";
import { NoValue } from "../../components/emptystate";
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import type { ClientOut } from "../../lib/api";
import { useClientScope } from "../../lib/clientScope";
import { useEverywherePermissions } from "../../lib/mePermissions";
import { Btn, Panel, QueryState } from "../../shell/components";
import { KV } from "../../shell/viz";
import { useHealthAssignments } from "../health/api";
import { useArchiveClientMutation } from "./api";
import { ClientFormDialog } from "./ClientForm";
import { clientHealthDefault, clientKindLabel, clientStatusLabel } from "./labels";

export function SummaryTab({ client }: { client: ClientOut }) {
  const t = useT();
  const { clients } = useClientScope();
  const everywhere = useEverywherePermissions();
  const mine = clients.find((item) => item.id === client.id);
  const canEdit = client.status === "active" && (mine?.permissions.includes("clients.manage") ?? false);
  const canArchive =
    client.kind === "customer" && client.status === "active" && everywhere.has("clients.manage");
  const canReadHealth = mine?.permissions.includes("health.read") ?? false;
  const archive = useArchiveClientMutation(client.id);
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const notSet = <NoValue label={t("clients.summary.notSet")} />;
  // Typed as tuples: an array literal of pairs would be inferred as arrays,
  // which `KV`'s `readonly [ReactNode, ReactNode]` does not accept.
  const facts: (readonly [string, ReactNode])[] = [
    [t("clients.summary.kind"), clientKindLabel(client.kind)],
    [t("clients.summary.status"), clientStatusLabel(client.status)],
    [t("clients.summary.emailDomains"), client.email_domains.length > 0 ? client.email_domains.join(", ") : notSet],
    [t("clients.summary.phone"), client.phone || notSet],
    [t("clients.summary.address"), client.address || notSet],
    [t("clients.summary.website"), client.website || notSet],
    [t("clients.summary.notes"), client.notes || notSet],
    [t("clients.summary.devices"), String(client.device_count)],
    [t("clients.summary.sites"), String(client.site_count)],
  ];

  return (
    <>
      <Panel
        title={t("clients.summary.title")}
        actions={
          canEdit || canArchive ? (
            <>
              {canEdit ? <Btn label={t("clients.summary.edit")} icon="edit" onClick={() => setEditing(true)} /> : null}
              {canArchive ? (
                <Btn
                  label={t("clients.summary.archive")}
                  icon="archive"
                  variant="danger"
                  onClick={() => {
                    archive.reset();
                    setConfirming(true);
                  }}
                />
              ) : null}
            </>
          ) : undefined
        }
      >
        <KV items={facts} />
        {archive.isError ? (
          <p role="alert" className="form-error">
            {t("clients.summary.archiveFailed")} {describeSubmitError(archive.error)}
          </p>
        ) : null}
      </Panel>

      {canReadHealth ? <HealthDefault clientId={client.id} /> : null}

      <ClientFormDialog
        key={editing ? "open" : "closed"}
        open={editing}
        client={client}
        onClose={() => setEditing(false)}
        onSaved={() => setEditing(false)}
      />
      <ConfirmDialog
        open={confirming}
        title={t("clients.summary.archiveTitle")}
        subject={client.name}
        body={t("clients.summary.archiveBody")}
        confirmLabel={t("clients.summary.archiveConfirm")}
        destructive
        onCancel={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false);
          archive.mutate();
        }}
      />
    </>
  );
}

/** Read-only: the profile is assigned on the Health page (Task 14). */
function HealthDefault({ clientId }: { clientId: string }) {
  const t = useT();
  const assignments = useHealthAssignments();
  const { own, org } = clientHealthDefault(assignments.data ?? [], clientId);
  return (
    <Panel
      title={t("clients.summary.healthTitle")}
      actions={
        <Link className="btn" to="/health#profiles">
          {t("clients.summary.healthWhere")}
        </Link>
      }
    >
      <QueryState
        loading={assignments.isLoading}
        error={assignments.error}
        empty={false}
        emptyLabel={t("clients.summary.healthNone")}
      >
        <p>
          {own
            ? t("clients.summary.healthOwn", { profile: own.profile_name })
            : org
              ? t("clients.summary.healthOrg", { profile: org.profile_name })
              : t("clients.summary.healthNone")}
        </p>
      </QueryState>
    </Panel>
  );
}
```

```tsx
// frontend/src/modules/clients/SitesTab.tsx
/**
 * A client's sites: list (archived ones too), add, edit and archive with
 * `sites.manage` on this client, and each site's install link with
 * `enrollment.link.manage` there -- the same panel Agent packages shows
 * (`agent-releases/InstallLinkPanel.tsx`). The internal client's Principal
 * site (its id is the organisation's) is never archived, so it is not offered;
 * a site holding devices is refused with a 409 the page explains.
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { ConfirmDialog } from "../../components/confirm";
import { Form, FormActions, attemptProblems, describeSubmitError } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { ClientOut, ClientSiteOut, Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useClientScope } from "../../lib/clientScope";
import { formatDate } from "../../lib/datetime";
import { Btn, Panel, QueryState, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { InstallLinkPanel } from "../agent-releases/InstallLinkPanel";
import { useArchiveSiteMutation, useClientSitesWithArchived, useCreateSiteMutation, useUpdateSiteMutation } from "./api";

const siteRules = (): readonly FieldRule[] => [{ name: "name", label: translate("clients.sites.name") }];

export function SitesTab({ client }: { client: ClientOut }) {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const { clients } = useClientScope();
  const mine = clients.find((item) => item.id === client.id);
  const open = client.status === "active" && client.kind !== "unassigned";
  const canManage = open && (mine?.permissions.includes("sites.manage") ?? false);
  const canLink = open && (mine?.permissions.includes("enrollment.link.manage") ?? false);
  const sites = useClientSitesWithArchived(client.id);
  const archive = useArchiveSiteMutation(client.id);
  const [editing, setEditing] = useState<ClientSiteOut | "new" | null>(null);
  const [archiving, setArchiving] = useState<ClientSiteOut | null>(null);
  const [linkFor, setLinkFor] = useState<string | null>(null);
  const rows = sites.data ?? [];

  if (client.kind === "unassigned") {
    return (
      <Panel title={t("clients.sites.title")}>
        <p className="note">{t("clients.sites.unassigned")}</p>
      </Panel>
    );
  }

  return (
    <>
      <Panel
        title={t("clients.sites.title")}
        subtitle={t("clients.sites.subtitle")}
        actions={canManage ? <Btn label={t("clients.sites.add")} icon="plus" onClick={() => setEditing("new")} /> : undefined}
        bare
      >
        <QueryState
          loading={sites.isLoading}
          error={sites.error}
          empty={rows.length === 0}
          emptyLabel={t("clients.sites.empty")}
          emptyIcon="building"
        >
          <Table label={t("clients.sites.tableLabel")}>
            <thead>
              <tr>
                <Th>{t("clients.sites.colSite")}</Th>
                <Th>{t("clients.sites.colDevices")}</Th>
                <Th>{t("clients.sites.colStatus")}</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {rows.map((site) => {
                const live = site.archived_at === null;
                const principalOfInternal = client.kind === "internal" && site.id === client.id;
                return (
                  <tr key={site.id}>
                    <Td lead={<Icon name="map-pin" size={18} />}>
                      <div className="cell-title">{site.name}</div>
                      {site.address || site.description ? (
                        <div className="cell-sub">{site.address || site.description}</div>
                      ) : null}
                    </Td>
                    <Td>
                      <span className="mono">{site.device_count}</span>
                    </Td>
                    <Td>
                      {live ? (
                        <StatusPill value="active" kind="ok" label={t("clients.sites.inUse")} />
                      ) : (
                        <StatusPill
                          value="archived"
                          kind="mut"
                          label={t("clients.sites.archivedOn", { date: formatDate(site.archived_at, me.data?.timezone) })}
                        />
                      )}
                    </Td>
                    <Td>
                      {live && canManage ? (
                        <Btn
                          small
                          label={t("clients.sites.edit")}
                          ariaLabel={t("clients.sites.editAria", { name: site.name })}
                          onClick={() => setEditing(site)}
                        />
                      ) : null}{" "}
                      {live && canLink ? (
                        <Btn
                          small
                          label={t("clients.sites.installLink")}
                          ariaLabel={t("clients.sites.installLinkAria", { name: site.name })}
                          pressed={linkFor === site.id}
                          onClick={() => setLinkFor(linkFor === site.id ? null : site.id)}
                        />
                      ) : null}{" "}
                      {live && canManage && !principalOfInternal ? (
                        <Btn
                          small
                          variant="danger"
                          label={t("clients.sites.archive")}
                          ariaLabel={t("clients.sites.archiveAria", { name: site.name })}
                          onClick={() => {
                            archive.reset();
                            setArchiving(site);
                          }}
                        />
                      ) : null}
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        </QueryState>
        {archive.isError ? (
          <p role="alert" className="form-error">
            {t("clients.sites.archiveFailed")} {describeSubmitError(archive.error)}
          </p>
        ) : null}
      </Panel>

      {linkFor !== null ? <InstallLinkPanel zone={me.data?.timezone} siteId={linkFor} /> : null}

      {editing !== null ? (
        <SiteFormDialog
          clientId={client.id}
          site={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
        />
      ) : null}
      <ConfirmDialog
        open={archiving !== null}
        title={t("clients.sites.archiveTitle")}
        subject={archiving?.name ?? ""}
        body={t("clients.sites.archiveBody")}
        confirmLabel={t("clients.sites.archiveConfirm")}
        destructive
        onCancel={() => setArchiving(null)}
        onConfirm={() => {
          const site = archiving;
          setArchiving(null);
          if (site) archive.mutate(site.id);
        }}
      />
    </>
  );
}

function SiteFormDialog({
  clientId,
  site,
  onClose,
}: {
  clientId: string;
  site: ClientSiteOut | null;
  onClose: () => void;
}) {
  const t = useT();
  const create = useCreateSiteMutation(clientId);
  const update = useUpdateSiteMutation(clientId);
  const mutation = site ? update : create;
  const [name, setName] = useState(site?.name ?? "");
  const [address, setAddress] = useState(site?.address ?? "");
  const [description, setDescription] = useState(site?.description ?? "");
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function submit() {
    mutation.reset();
    const next: Attempt = { rules: siteRules, values: { name } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    const body = { name: name.trim(), address: address.trim(), description: description.trim() };
    if (site) update.mutate({ siteId: site.id, body }, { onSuccess: onClose });
    else create.mutate(body, { onSuccess: onClose });
  }

  return (
    <Dialog
      open
      onClose={onClose}
      title={site ? t("clients.sites.formEditTitle") : t("clients.sites.formAddTitle")}
    >
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("clients.sites.name")}>
            <input className={inputClass} value={name} onChange={(event) => setName(event.target.value)} />
          </Field>
          <Field label={t("clients.sites.address")}>
            <input className={inputClass} value={address} onChange={(event) => setAddress(event.target.value)} />
          </Field>
          <Field label={t("clients.sites.description")} full>
            <textarea
              className={inputClass}
              rows={3}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </Field>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.actions.cancel")}
          </Button>
          <FormActions
            label={t("clients.sites.save")}
            pendingLabel={t("clients.sites.saving")}
            pending={mutation.isPending}
            error={mutation.error}
            failedMessage={t("clients.sites.saveFailed")}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}
```

```tsx
// frontend/src/modules/clients/ContactsTab.tsx
/**
 * A client's contacts (`GET/POST /clients/{id}/contacts`, `PATCH
 * /contacts/{id}`): list, add, edit, and switch off and on, with
 * `clients.manage` on this client. A contact uses the portal, never the
 * console; `/users` is staff-only, so this is where a contact's sign-in is
 * turned off and back on.
 */
import { useState } from "react";

import { ConfirmDialog } from "../../components/confirm";
import { NoValue } from "../../components/emptystate";
import { Form, FormActions, attemptProblems, describeSubmitError } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { ClientOut, ContactOut } from "../../lib/api";
import { useClientScope } from "../../lib/clientScope";
import { Btn, Panel, QueryState, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useClientContacts, useCreateContactMutation, useUpdateContactMutation } from "./api";

const contactRules = (): readonly FieldRule[] => [{ name: "email", label: translate("clients.contacts.email") }];

export function ContactsTab({ client }: { client: ClientOut }) {
  const t = useT();
  const { clients } = useClientScope();
  const mine = clients.find((item) => item.id === client.id);
  const canManage =
    client.status === "active" &&
    client.kind !== "unassigned" &&
    (mine?.permissions.includes("clients.manage") ?? false);
  const contacts = useClientContacts(client.id);
  const change = useUpdateContactMutation(client.id);
  const [editing, setEditing] = useState<ContactOut | "new" | null>(null);
  const [turningOff, setTurningOff] = useState<ContactOut | null>(null);
  const rows = contacts.data ?? [];

  if (client.kind === "unassigned") {
    return (
      <Panel title={t("clients.contacts.title")}>
        <p className="note">{t("clients.contacts.unassigned")}</p>
      </Panel>
    );
  }

  return (
    <>
      <Panel
        title={t("clients.contacts.title")}
        subtitle={t("clients.contacts.subtitle")}
        actions={canManage ? <Btn label={t("clients.contacts.add")} icon="plus" onClick={() => setEditing("new")} /> : undefined}
        bare
      >
        <QueryState
          loading={contacts.isLoading}
          error={contacts.error}
          empty={rows.length === 0}
          emptyLabel={t("clients.contacts.empty")}
          emptyIcon="users"
        >
          <Table label={t("clients.contacts.tableLabel")}>
            <thead>
              <tr>
                <Th>{t("clients.contacts.colContact")}</Th>
                <Th>{t("clients.contacts.colPhone")}</Th>
                <Th>{t("clients.contacts.colStatus")}</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {rows.map((contact) => {
                const name = contact.display_name || contact.email;
                const active = contact.status === "active";
                return (
                  <tr key={contact.id}>
                    <Td lead={<Icon name="user" size={18} />}>
                      <div className="cell-title">{name}</div>
                      <div className="cell-sub">{contact.email}</div>
                    </Td>
                    <Td>{contact.phone || <NoValue label={t("clients.summary.notSet")} />}</Td>
                    <Td>
                      <StatusPill
                        value={contact.status}
                        kind={active ? "ok" : "mut"}
                        label={active ? t("clients.contacts.active") : t("clients.contacts.disabled")}
                      />
                    </Td>
                    <Td>
                      {canManage ? (
                        <>
                          <Btn
                            small
                            label={t("clients.contacts.edit")}
                            ariaLabel={t("clients.contacts.editAria", { name })}
                            onClick={() => setEditing(contact)}
                          />{" "}
                          {active ? (
                            <Btn
                              small
                              variant="danger"
                              label={t("clients.contacts.turnOff")}
                              ariaLabel={t("clients.contacts.turnOffAria", { name })}
                              onClick={() => {
                                change.reset();
                                setTurningOff(contact);
                              }}
                            />
                          ) : (
                            <Btn
                              small
                              label={t("clients.contacts.turnOn")}
                              ariaLabel={t("clients.contacts.turnOnAria", { name })}
                              onClick={() => {
                                change.reset();
                                change.mutate({ contactId: contact.id, body: { status: "active" } });
                              }}
                            />
                          )}
                        </>
                      ) : null}
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        </QueryState>
        {change.isError ? (
          <p role="alert" className="form-error">
            {t("clients.contacts.changeFailed")} {describeSubmitError(change.error)}
          </p>
        ) : null}
      </Panel>

      {editing !== null ? (
        <ContactFormDialog
          clientId={client.id}
          contact={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
        />
      ) : null}
      <ConfirmDialog
        open={turningOff !== null}
        title={t("clients.contacts.turnOffTitle")}
        subject={turningOff ? turningOff.display_name || turningOff.email : ""}
        body={t("clients.contacts.turnOffBody")}
        confirmLabel={t("clients.contacts.turnOffConfirm")}
        destructive
        onCancel={() => setTurningOff(null)}
        onConfirm={() => {
          const contact = turningOff;
          setTurningOff(null);
          if (contact) change.mutate({ contactId: contact.id, body: { status: "disabled" } });
        }}
      />
    </>
  );
}

function ContactFormDialog({
  clientId,
  contact,
  onClose,
}: {
  clientId: string;
  contact: ContactOut | null;
  onClose: () => void;
}) {
  const t = useT();
  const create = useCreateContactMutation(clientId);
  const update = useUpdateContactMutation(clientId);
  const mutation = contact ? update : create;
  const [email, setEmail] = useState(contact?.email ?? "");
  const [displayName, setDisplayName] = useState(contact?.display_name ?? "");
  const [phone, setPhone] = useState(contact?.phone ?? "");
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function submit() {
    mutation.reset();
    const next: Attempt = { rules: contactRules, values: { email } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    const body = { email: email.trim(), display_name: displayName.trim(), phone: phone.trim() || null };
    if (contact) update.mutate({ contactId: contact.id, body }, { onSuccess: onClose });
    else create.mutate(body, { onSuccess: onClose });
  }

  return (
    <Dialog
      open
      onClose={onClose}
      title={contact ? t("clients.contacts.formEditTitle") : t("clients.contacts.formAddTitle")}
    >
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("clients.contacts.email")}>
            <input
              className={inputClass}
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </Field>
          <Field label={t("clients.contacts.displayName")}>
            <input
              className={inputClass}
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
            />
          </Field>
          <Field label={t("clients.contacts.phone")}>
            <input className={inputClass} value={phone} onChange={(event) => setPhone(event.target.value)} />
          </Field>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.actions.cancel")}
          </Button>
          <FormActions
            label={t("clients.contacts.save")}
            pendingLabel={t("clients.contacts.saving")}
            pending={mutation.isPending}
            error={mutation.error}
            failedMessage={t("clients.contacts.saveFailed")}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}
```

```tsx
// frontend/src/modules/clients/ModulesTab.tsx
/**
 * Budget, Continuity and Changes, switched on and off per client
 * (`PATCH /clients/{id}` with `enabled_modules`, `clients.manage` on this
 * client). A module that is off is a 404 on every write for this client
 * (`require_module`, phase 2), and the menu follows `/me.clients` (Task 8),
 * which this refreshes.
 */
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import type { ClientOut } from "../../lib/api";
import { useClientScope } from "../../lib/clientScope";
import { Panel, Toggle } from "../../shell/components";
import { useUpdateClientMutation } from "./api";
import { OPTIONAL_MODULES, moduleHint, moduleLabel } from "./labels";

export function ModulesTab({ client }: { client: ClientOut }) {
  const t = useT();
  const { clients } = useClientScope();
  const mine = clients.find((item) => item.id === client.id);
  const canManage =
    client.status === "active" &&
    client.kind !== "unassigned" &&
    (mine?.permissions.includes("clients.manage") ?? false);
  const update = useUpdateClientMutation(client.id);
  const enabled = new Set(client.enabled_modules);

  return (
    <Panel title={t("clients.modules.title")} subtitle={t("clients.modules.subtitle")}>
      {client.kind === "unassigned" ? <p className="note">{t("clients.modules.unassigned")}</p> : null}
      <div className="flex flex-col gap-3">
        {OPTIONAL_MODULES.map((module) => (
          <div key={module}>
            <Toggle
              checked={enabled.has(module)}
              disabled={!canManage || update.isPending}
              label={moduleLabel(module)}
              onChange={(next) => {
                update.reset();
                update.mutate({
                  enabled_modules: OPTIONAL_MODULES.filter((item) => (item === module ? next : enabled.has(item))),
                });
              }}
            />
            <p className="note">{moduleHint(module)}</p>
          </div>
        ))}
      </div>
      {update.isError ? (
        <p role="alert" className="form-error">
          {t("clients.modules.saveFailed")} {describeSubmitError(update.error)}
        </p>
      ) : null}
    </Panel>
  );
}
```

- [ ] **Step 11: Ruta, menú, recorrido del harmony y fixtures**

`frontend/src/App.tsx`: tras `import { SettingsPage } from "./modules/settings/SettingsPage";` (`:125`):

```tsx
import { ClientDetailPage } from "./modules/clients/ClientDetailPage";
import { ClientsPage } from "./modules/clients/ClientsPage";
```

y antes de `<Route path="/settings"` (`:1369`):

```tsx
            {/* Clients (MSP clients phase 3, design §4.1). */}
            <Route
              path="/clients"
              element={
                <RequireAuth>
                  <ClientsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/clients/:clientId"
              element={
                <RequireAuth>
                  <ClientDetailPage />
                </RequireAuth>
              }
            />
```

`frontend/src/shell/nav.ts:154`, la primera entrada de `sections: [`:

```ts
          // MSP clients phase 3 (design §4.1): the companies served, their
          // sites, contacts and optional modules.
          section("shell.nav.section.organization", [
            page("/clients", "shell.nav.item.clients", "building", "clients.read", { hint: "shell.nav.hint.clients" }),
          ]),
```

`frontend/scripts/routes.mjs`: en `PARAMS`, tras `sessionId: "sess-1",` (`:38`), `clientId: "cli-logistics",`; en `ROUTES`, antes de `"/settings",` (`:159`), `"/clients",` y `"/clients/cli-logistics",`.

```ts
// frontend/src/modules/clients/fixtures.ts
/**
 * Fixtures for the Clients page: one client read by id, and its contacts. The
 * list (`listClients`) is the Settings fixtures', the sites (`listClientSites`)
 * IPAM's and the install link Agent packages', so the same client reads the
 * same on every screen; the records below match `settings/fixtures.ts`.
 */
import type { FixtureMap } from "../../fixtures/client";
import type { ClientOut, ContactOut } from "../../lib/api";

const CLIENTS: ClientOut[] = [
  { id: "org-toeshee", name: "Toeshee", kind: "internal", status: "active", email_domains: ["toeshee.example"], phone: "", address: "", website: "", notes: "", enabled_modules: ["budget", "bcp", "change"], device_count: 42, site_count: 1 },
  { id: "cli-logistics", name: "Andes Logistics", kind: "customer", status: "active", email_domains: ["andeslogistics.example"], phone: "+57 1 555 0100", address: "Calle 80 #10-20, Bogotá", website: "andeslogistics.example", notes: "", enabled_modules: [], device_count: 12, site_count: 2 },
  { id: "cli-unassigned", name: "Unassigned", kind: "unassigned", status: "active", email_domains: [], phone: "", address: "", website: "", notes: "", enabled_modules: [], device_count: 0, site_count: 0 },
];

const CONTACTS: ContactOut[] = [
  { id: "usr-contact-1", client_id: "cli-logistics", kind: "contact", email: "ana.rojas@andeslogistics.example", display_name: "Ana Rojas", phone: "+57 1 555 0101", status: "active" },
  { id: "usr-contact-2", client_id: "cli-logistics", kind: "contact", email: "compras@andeslogistics.example", display_name: "Compras", phone: null, status: "disabled" },
];

const getClientRecord: FixtureMap["getClientRecord"] = (clientId) => {
  const found = CLIENTS.find((client) => client.id === clientId);
  if (!found) throw new Error(`no such client: ${clientId}`);
  return found;
};

const listContacts: FixtureMap["listContacts"] = (clientId) =>
  CONTACTS.filter((contact) => contact.client_id === clientId);

const fixtures: FixtureMap = { getClientRecord, listContacts };

export default fixtures;
```

La prueba del menú, al final de `frontend/src/shell/nav.test.ts` (usa `ALL_ITEMS`, `crumbFor`, `visibleGroups`, `isArea`, que el archivo ya importa o define):

```ts
describe("the Clients page (MSP clients phase 3, design §4.1)", () => {
  it("sits in Admin, gated on clients.read", () => {
    const item = ALL_ITEMS.find((entry) => entry.to === "/clients");
    expect(item?.permission).toBe("clients.read");
    expect(item?.label).toBe("Clients");
    expect(crumbFor("/clients/c1")).toEqual({ section: "Admin", sectionTo: "/admin", title: "Clients" });
  });

  it("is offered to a person holding only clients.read", () => {
    const admin = visibleGroups(["clients.read"], new Set())
      .flatMap((group) => group.items)
      .find((entry) => entry.to === "/admin");
    expect(admin && isArea(admin) ? admin.sections.map((section) => section.label) : []).toEqual([
      "Organization",
    ]);
  });
});
```

- [ ] **Step 12: Los textos que remitían a la API**

`frontend/src/i18n/en/people.ts:132-136`: borra el comentario de dos líneas (`:133-134`) (`// No console page edits a client's contacts yet (phase 3)…`) y deja:

```ts
      // A client's contact is not a Settings user: it is switched back on as a contact.
      turnOffBodyContact:
        "They can no longer use the portal. You can turn it back on in Clients, on the client's Contacts tab.",
```

`frontend/src/i18n/es/people.ts:128-129`:

```ts
      turnOffBodyContact:
        "La persona ya no podrá usar el portal. Puedes volver a activarlo en Clientes, en la pestaña Contactos del cliente.",
```

`frontend/src/i18n/en/ipam.ts:776-778`: borra el comentario `// No console page manages a client's sites yet (phase 3) -- final review E.` y:

```ts
    sitesSubtitle:
      "The client's offices. VLANs and subnets name them; they are added and edited on the Clients page.",
    manageSites: "Manage sites",
```

`frontend/src/i18n/es/ipam.ts:769-770`:

```ts
    sitesSubtitle:
      "Las oficinas del cliente. Las VLAN y las subredes las nombran; se crean y se editan en la página Clientes.",
    manageSites: "Administrar las sedes",
```

`frontend/src/modules/ipam/RegistryPage.tsx`, `SitePanel` (`:155-165`): añade `import { Link } from "react-router-dom";` y `import { useMePermissions } from "../../lib/mePermissions";`, y dentro de `SitePanel`:

```tsx
  // NOT A PERMISSION GATE: the Clients page reads `clients.read` itself.
  const canReadClients = useMePermissions().has("clients.read");
```

y en su `<Panel …>`:

```tsx
      actions={
        canReadClients ? (
          <Link className="btn" to="/clients">
            {t("ipam.registry.manageSites")}
          </Link>
        ) : undefined
      }
```

Pruebas existentes que fijan el texto viejo: `grep -rn "through the API or an AI connection\|mediante la API o una conexión de IA" frontend/src --include=*.test.tsx`. Si alguna lo cita, se corrige al texto nuevo de arriba (la frase vieja → la nueva) y el informe lo dice.

- [ ] **Step 13: Comprobar que pasan**

Run: `npx vitest run src/modules/clients src/lib/api.clients.test.ts src/shell/nav.test.ts src/test/routes-covered.test.ts src/App.routing.test.tsx src/i18n src/modules/ipam src/modules/people`
Expected: PASS.

- [ ] **Step 14: Commit**

```bash
git add frontend/src/lib/api.ts frontend/src/lib/api.clients.test.ts frontend/src/modules/clients frontend/src/i18n/en/clients.ts frontend/src/i18n/es/clients.ts frontend/src/i18n/en/index.ts frontend/src/i18n/es/index.ts frontend/src/i18n/en/shell.ts frontend/src/i18n/es/shell.ts frontend/src/i18n/en/people.ts frontend/src/i18n/es/people.ts frontend/src/i18n/en/ipam.ts frontend/src/i18n/es/ipam.ts frontend/src/modules/ipam/RegistryPage.tsx frontend/src/modules/health/types.ts frontend/src/App.tsx frontend/src/shell/nav.ts frontend/src/shell/nav.test.ts frontend/scripts/routes.mjs
git commit -m "feat(clients): the Clients page -- list, summary, sites, contacts and modules (phase 3 Task 12)"
```

- [ ] **Step 15: La prueba anti-`PHASE 3` que falla**

```ts
// frontend/src/lib/noPhase3.test.ts
/**
 * NOTHING IS LEFT FOR PHASE 3 (MSP clients phase 3, design §3.6).
 *
 * Phases 1 and 2 marked every place that filed a new row under the
 * organisation's internal client "until the selector exists" with a
 * `PHASE 3` line and a call to `internalClientId(me)` or `internalSiteId(me)`.
 * Phase 3 built the selector and moved every one of them to
 * `lib/clientSelection.ts`. A marker or a call that comes back is a form
 * filing rows under the internal client without anyone choosing it -- the
 * thing the operator's Q1 ruled out -- so the whole source tree is read, tests
 * and comments included, and any of the three fails it.
 *
 * This file names all three in its own prose, so it is skipped by path; the
 * markers themselves are built from pieces, so the self-test below proves the
 * matcher rather than a copy of this file's text.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = join(import.meta.dirname, "..");
const SELF = join("lib", "noPhase3.test.ts");
const MARKERS = [["PHASE", "3"].join(" "), ["internal", "ClientId"].join(""), ["internal", "SiteId"].join("")];

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return walk(path);
    return /\.(ts|tsx|mts|js|mjs|css|json)$/.test(name) ? [path] : [];
  });
}

function offenders(files: readonly { path: string; text: string }[]): string[] {
  return files.flatMap(({ path, text }) =>
    MARKERS.filter((marker) => text.includes(marker)).map((marker) => `${path}: ${marker}`),
  );
}

describe("the console keeps nothing marked for phase 3", () => {
  const files = walk(SRC);

  it("reads the whole source tree", () => {
    expect(files.length).toBeGreaterThan(500);
    expect(files.map((file) => relative(SRC, file))).toContain(join("lib", "api.ts"));
  });

  it("has no PHASE 3 marker and no internal-client helper left", () => {
    const tree = files
      .map((file) => ({ path: relative(SRC, file), text: readFileSync(file, "utf8") }))
      .filter((file) => file.path !== SELF);
    expect(offenders(tree)).toEqual([]);
  });

  it("finds each of the three when one is there", () => {
    expect(
      offenders([
        { path: "a.tsx", text: `// ${MARKERS[0]}: the selector` },
        { path: "b.ts", text: `const c = ${MARKERS[1]}(me);` },
        { path: "c.ts", text: `siteId={${MARKERS[2]}(me.data)}` },
        { path: "d.ts", text: "nothing here" },
      ]),
    ).toEqual([`a.tsx: ${MARKERS[0]}`, `b.ts: ${MARKERS[1]}`, `c.ts: ${MARKERS[2]}`]);
  });
});
```

Run: `npx vitest run src/lib/noPhase3.test.ts`
Expected: FAIL — al menos `lib/internalClient.ts`, `lib/internalClient.test.ts` y `modules/settings/UsersTab.tsx: PHASE 3`. Si la lista trae **otro** archivo, es un resto de las Tasks 9–11 y se arregla en el Step 16 igual que la cabecera de `UsersTab.tsx`: se borra la línea `PHASE 3`, y si la frase que la rodea describe el cliente interno «hasta que exista el selector», se reescribe con lo que hace hoy. El informe lo dice.

- [ ] **Step 16: Borrar `internalClient.ts` y lo que lo nombra**

Todos los comandos, desde la raíz de la worktree. Primero, lo que aún importa el módulo (después de las Tasks 9–11 no debería quedar ningún `import`):

```bash
grep -rn "from \"[./]*lib/internalClient\"\|from \"\./internalClient\"" frontend/src
```

Expected: ningún `import`. Si aparece uno, solo puede traer `requireKnown`, `loadingUntilClient` o `InternalScopeUnknown`, que la Task 9 llevó a `lib/clientSelection.ts`: cambia la ruta a `../../lib/clientSelection` (o la relativa que toque) y `InternalScopeUnknown` por `ClientNotChosen`. Si trae `internalClientId`, `internalSiteId` o `newRowClient`, es una tarea anterior sin terminar: para y avisa al coordinador.

Después, borrar y reapuntar los comentarios:

```bash
git rm frontend/src/lib/internalClient.ts frontend/src/lib/internalClient.test.ts
grep -rl "lib/internalClient" frontend/src | xargs -r sed -i 's#lib/internalClient\.ts#lib/clientSelection.ts#g'
grep -rl "InternalScopeUnknown" frontend/src | xargs -r sed -i 's/InternalScopeUnknown/ClientNotChosen/g'
```

Dos comentarios necesitan otra frase, no solo otro nombre de archivo:
- `frontend/src/fixtures/core.ts:87-88`: `/** The organisation -- and its internal client, whose id is the organisation's (MSP clients spec §7 point 2). */`
- `frontend/src/i18n/en/lib.ts:43-44`: `// A create that cannot name its client yet (\`ClientNotChosen\`, lib/clientSelection.ts).`

`frontend/src/modules/settings/UsersTab.tsx:15-16` (solo estas dos líneas; la Task 13 edita otro bloque del archivo):

```ts
// The editor's wire shape is final (MSP clients spec §3.4, "editor de
// asignaciones"); phase 3 kept it and grouped the effective permissions by
// client (design 2026-10-31 §4.2). Its restyle is project 3's.
```

Comprueba que no queda nada:

```bash
grep -rn "internalClient\b\|lib/internalClient\|InternalScopeUnknown" frontend/src
```

Expected: solo la clave de catálogo `internalClient` de `frontend/src/i18n/{en,es}/lib.ts` si la Task 9 la conservó para el texto de `ClientNotChosen` (es una clave, no el módulo).

- [ ] **Step 17: Comprobar**

Run (desde `frontend/`): `npx vitest run src/lib/noPhase3.test.ts src/modules/clients src/modules/settings`
Expected: PASS.

Luego la verificación completa: `npx vitest run`, `npx tsc -b`, `npm run build`. Expected: todo en verde. Esta tarea no toca el backend: la suite de Python no hace falta salvo que el coordinador la pida para la rama fusionada.

- [ ] **Step 18: Commit** (desde la raíz de la worktree)

```bash
git add -A frontend/src
git commit -m "chore(clients): internalClient.ts is gone, and a guard keeps PHASE 3 out of the console (phase 3 Task 12)"
```

---


#### Addendum del coordinador (Task 12)

- **`UsersTab.tsx:15-16`.** La Task 11 (D11.7) ya reescribe ese comentario `PHASE 3`. Si al empezar esta tarea el comentario ya no está, el paso que lo borra no hace nada. La prueba anti-`PHASE 3` de esta tarea tiene que pasar igual.
- **Nombre del método.** En esta tarea, leer un cliente es `ApiClient.getClientRecord(clientId)`. La Task 7 ya usa `getClient` como cuarto argumento del constructor (el cliente elegido), y dos miembros con el mismo nombre no compilan.

### Task 13: Equipos (columnas, filtro por sede, cambio de sede), alta, tickets, usuarios y auditoría

Diseño: §4.2 (usuarios), §4.3 (equipos), §4.4 (agregar un equipo), §4.5 (tickets) y §4.6 (auditoría). Oleada 4, en paralelo con las Tasks 12 y 14.

**Archivos compartidos que toca esta tarea:**
- `frontend/src/lib/api.ts`: `DeviceListParams` (`:522-531`), `listDevices` (`:1845-1856`) y un método nuevo tras `updateAsset` (`:1931-1936`). Las Tasks 12 y 14 tocan otros bloques.
- `frontend/src/modules/settings/UsersTab.tsx`: **solo** el import de `shell/components` (`:74`) y el bloque de permisos efectivos (`:741-776`). La cabecera (`:15-16`) es de la Task 12.
- Catálogos: `frontend/src/i18n/{en,es}/devices.ts`, `inventory.ts`, `onboarding.ts`, `serviceDesk.ts`, `settings.ts` (ninguno lo tocan las Tasks 12 ni 14).
- Backend: `backend/app/api/v1/devices.py`, `backend/app/services/device_list.py`, `backend/app/mcp/descriptions/part_c.py`, `backend/app/api/v1/audit.py` (comentario), `backend/tests/test_devices_list.py`. Ninguna otra tarea de la oleada 4 toca el backend.

**Files:**
- Modify: `backend/app/api/v1/devices.py:392-445` (`site_id` en `list_devices`)
- Modify: `backend/app/services/device_list.py:304-336` (`_filtered`) y `:395-447` (`device_page`)
- Modify: `backend/app/mcp/descriptions/part_c.py:158-162` (`GET /api/v1/devices`)
- Modify: `backend/app/api/v1/audit.py:18-27` (comentario obsoleto)
- Modify: `frontend/src/lib/api.ts` (`:522-531`, `:1851`, tras `:1936`)
- Create: `frontend/src/modules/devices/siteNames.ts`
- Modify: `frontend/src/modules/devices/DevicesPage.tsx` (`:15-26`, `:33-67`, `:237-292`, `:575-596`, `:660-685`, `:776-780`)
- Create: `frontend/src/modules/inventory/DeviceSite.tsx`
- Modify: `frontend/src/modules/inventory/DeviceDetailPage.tsx:524-528`
- Modify: `frontend/src/modules/onboarding/journey.ts` (`:66-80`, `:318-326`), `frontend/src/modules/onboarding/OnboardPage.tsx` (`:40-56`, `:100-190`)
- Create: `frontend/src/modules/onboarding/journeyTarget.ts`
- Modify (una línea cada una): `frontend/src/modules/ipam/SubnetTreePage.tsx:100`, `frontend/src/modules/ipam/ScanAuthorizationsPage.tsx:75`, `frontend/src/modules/devices/CandidatesPage.tsx:151`, `frontend/src/modules/enrollment/EnrollmentPage.tsx:273`, `frontend/src/modules/agent-releases/AgentReleasesPage.tsx:153`
- Modify: `frontend/src/modules/service-desk/TicketTable.tsx` (`:41-79`)
- Create: `frontend/src/modules/settings/grantGroups.ts`
- Modify: `frontend/src/modules/settings/UsersTab.tsx` (`:74`, `:741-776`)
- Modify: `frontend/src/modules/audit/api.ts:3-6`
- Modify: `frontend/src/i18n/{en,es}/devices.ts`, `inventory.ts`, `onboarding.ts`, `serviceDesk.ts`, `settings.ts`
- Test: `backend/tests/test_devices_list.py`, `frontend/src/lib/api.devicesSite.test.ts`, `frontend/src/modules/devices/DevicesPage.test.tsx`, `frontend/src/modules/inventory/DeviceSite.test.tsx`, `frontend/src/modules/inventory/DeviceDetailPage.assets.test.tsx`, `frontend/src/modules/onboarding/__tests__/journey.test.ts`, `frontend/src/modules/onboarding/__tests__/journeyTarget.test.tsx`, `frontend/src/modules/onboarding/OnboardPage.test.tsx`, `frontend/src/modules/onboarding/onboarding.es.test.tsx`, `frontend/src/modules/service-desk/__tests__/ticketQueue.test.tsx`, `frontend/src/modules/settings/__tests__/grantGroups.test.ts`, `frontend/src/modules/settings/__tests__/users.test.tsx`, `frontend/src/modules/settings/settings.es.test.tsx`

**Interfaces:**
- Consumes (Task 7): `useClientScope()`; `Me.clients` (`MeClient[]`); el cuarto argumento del constructor de `ApiClient`.
- Consumes (Task 8): `useClientName()` (`modules/settings/api.ts`), que lee `me.clients`.
- Consumes (Task 9): `ClientField({ code, value, onChange })` (`components/ClientField.tsx`), `SiteField({ clientId, value, onChange })` (`components/SiteField.tsx`). Si la Task 9 tipó `value` como `string` (no `string | null`), pasa `""` donde abajo pone `null` y `value || null` en `onChange`.
- Consumes (Task 10): en `EnrollmentPage` y `AgentReleasesPage`, el estado que guarda la sede elegida en su `SiteField`.
- Produces (01-interfaces): `GET /api/v1/devices?site_id=`; `DeviceListParams.siteId`.
- Produces, **fuera de 01-interfaces**:
  - `ApiClient.changeDeviceSite(deviceId: string, siteId: string): Promise<Device>`.
  - `modules/devices/siteNames.ts::useSitesOf(clientIds: readonly string[]): { sitesOf(clientId: string): readonly ClientSiteOut[] | undefined; siteName(siteId: string | undefined): string | null }`.
  - `modules/inventory/DeviceSite.tsx`: `DeviceSite({ device, canMove })`, `DeviceSiteDialog({ device, onClose })`.
  - `modules/onboarding/journey.ts`: `JOURNEY_CLIENT_PARAM = "client"`, `JOURNEY_SITE_PARAM = "site"`, `journeyQuery(client, site)`, `withJourney(to, query)`, `JourneyInput.linkQuery?: string`.
  - `modules/onboarding/journeyTarget.ts`: `useAdoptJourneyClient(): void`, `useJourneySite(): string | null`, `useJourneySiteState(): [string | null, (id: string | null) => void]`.
  - `modules/settings/grantGroups.ts`: `GrantGroup`, `groupGrantsByClient(grants, name)`.

> **Decisión del plan (el cliente del alta).** Con «Todos mis clientes», elegir el cliente en `/devices/onboard` llama a `select(id)`: la consola pasa a trabajar con ese cliente. Lo mismo hace una página del recorrido abierta con `?client=` bajo «Todos mis clientes» (`useAdoptJourneyClient`); nunca cambia un cliente elegido arriba. Motivo: los pasos del recorrido y las páginas a las que lleva leen listas (subredes, autorizaciones, candidatos) que se estrechan con `X-Client-Id`, y crean filas con la regla 2 del §3.1. Un cliente propio de la página mostraría el recorrido de un cliente sobre las listas de todos. El cliente y la sede siguen viajando en la URL (`?client=…&site=…`), como pide el §4.4.
>
> **Decisión del plan (el filtro por sede).** Se ofrece con un cliente elegido arriba y más de una sede: las sedes son de un cliente. Con «Todos mis clientes» no hay filtro, y un `?site=` que quedara en la URL no se envía. Al cambiar de cliente arriba, el filtro se borra.
>
> **Decisión del plan (los nombres de las sedes).** `GET /devices` da `site_id`, no el nombre. Los nombres salen de `GET /clients/{id}/sites`, una lectura por cliente de la página, con la clave que ya comparten IPAM y los selectores (`["client-sites", id]`), y solo para los clientes donde la persona tiene `clients.read`. Sin ese permiso la celda dice «Sede desconocida».
>
> **Decisión del plan (auditoría, backend).** Además del comentario de `frontend/src/modules/audit/api.ts:3-6`, se corrige el de `backend/app/api/v1/audit.py:18-27`, que dice «until phase 3 decides it»: la fase 3 lo decidió (Q2).

- [ ] **Step 1: La prueba del backend que falla**

En `backend/tests/test_devices_list.py`, cambia el import de modelos (`:39`) por `from app.models import Agent, Device, Organization, Site` y añade tras `:41` `from tests.client_scope_support import client_with_site, everything_on`. Después de `test_the_route_serves_the_page_the_strip_and_the_tiles` (`:382-436`):

```python
async def test_the_route_filters_by_site_and_never_widens(api_env) -> None:
    """`GET /devices?site_id=` (MSP clients phase 3, design §4.3): one site's
    machines, its counts obeying the filter like any other and the tiles
    obeying none. A site of a client outside the scope answers an empty page,
    never a widening: RLS decides what the caller sees, the filter only narrows
    it. A malformed id is a 422, not an unfiltered page."""
    org_id = await api_env.create_org("dl-site")
    x, x_main = await client_with_site(api_env, org_id)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        north = Site(org_id=org_id, client_id=x, name="Bodega norte")
        session.add(north)
        await session.flush()
        session.add_all(
            [
                Device(org_id=org_id, hostname="principal-1", client_id=x, site_id=x_main),
                Device(org_id=org_id, hostname="norte-1", client_id=x, site_id=north.id),
                Device(org_id=org_id, hostname="norte-2", client_id=x, site_id=north.id),
                Device(org_id=org_id, hostname="interno-1", client_id=org_id, site_id=org_id),
            ]
        )
        await session.commit()
        north_id = north.id

    every = await api_env.headers(org_id, await api_env.create_user(org_id, "read-only"))
    body = (
        await api_env.client.get(
            "/api/v1/devices", params={"site_id": str(north_id)}, headers=every
        )
    ).json()
    assert sorted(item["hostname"] for item in body["items"]) == ["norte-1", "norte-2"]
    assert {item["site_id"] for item in body["items"]} == {str(north_id)}
    assert body["total"] == 2
    assert body["state_counts"]["all"] == 2
    assert body["fleet"]["total"] == 4

    internal_only = await everything_on(api_env, org_id, org_id)
    hidden = (
        await api_env.client.get(
            "/api/v1/devices", params={"site_id": str(north_id)}, headers=internal_only
        )
    ).json()
    assert hidden["items"] == []
    assert hidden["total"] == 0

    bad = await api_env.client.get("/api/v1/devices", params={"site_id": "north"}, headers=every)
    assert bad.status_code == 422, bad.text
```

Protege `Device.site_id == site_id` en `_filtered` (Step 3): sin esa cláusula, la página trae los cuatro equipos.

- [ ] **Step 2: Comprobar que falla**

Run (desde `backend/`, con `PYTHONPATH` en la worktree): `.venv/bin/pytest -q tests/test_devices_list.py -k site`
Expected: FAIL — `assert ['interno-1', 'norte-1', 'norte-2', 'principal-1'] == ['norte-1', 'norte-2']` (FastAPI ignora un parámetro que la ruta no declara).

- [ ] **Step 3: El filtro**

`backend/app/services/device_list.py`, `_filtered` (`:304-336`): añade el parámetro tras `client_id: UUID | None,` (`:311`):

```python
    site_id: UUID | None,
```

y tras el bloque `if client_id is not None:` (`:331-332`):

```python
    if site_id is not None:
        clauses.append(Device.site_id == site_id)
```

`device_page` (`:395-410`): tras `client_id: UUID | None = None,` (`:403`) añade `site_id: UUID | None = None,` y en la llamada a `_filtered` (`:439-447`), tras `client_id=client_id,`, `site_id=site_id,`.

`backend/app/api/v1/devices.py`, `list_devices`: tras `client_id: UUID | None = Query(default=None),` (`:392`):

```python
    #: One site's machines -- the console's site filter (MSP clients phase 3,
    #: design §4.3). A filter like the others, never a widening: RLS still
    #: decides what the caller may see, so a site of a client outside the
    #: scope answers an empty page.
    site_id: UUID | None = Query(default=None),
```

y en la llamada a `device_page` (`:432-445`), tras `client_id=client_id,`, `site_id=site_id,`.

`backend/app/mcp/descriptions/part_c.py:158-162`:

```python
    "GET /api/v1/devices": (
        "Lists the fleet one page at a time, filterable by OS, status, provenance, client, site, "
        "search text and tab, with total, per-tab and fleet-wide counts. The assigned person "
        "appears only on devices of clients where the caller also holds the people read permission."
    ),
```

`backend/app/api/v1/audit.py:23-27` (desde «#: client (`require_permission_on_every_client`). Since phase 2…» hasta la línea de `GLOBAL_READS`, incluida) pasa a:

```python
#: client (`require_permission_on_every_client`). Since phase 2 an event carries
#: an informative `client_id`; phase 3 decided NOT to open the log to a person
#: with `audit.read` on some clients (design 2026-10-31, Q2): `audit_events`
#: has no client policy, and filtering it in code would break invariant 5a.
#: `tests/test_global_writes.py::GLOBAL_READS`.
```


- [ ] **Step 4: Comprobar que pasa**

Run: `.venv/bin/pytest -q tests/test_devices_list.py tests/test_mcp_descriptions.py tests/test_devices_assigned_to.py tests/test_devices_screen_capable.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/devices.py backend/app/services/device_list.py backend/app/mcp/descriptions/part_c.py backend/app/api/v1/audit.py backend/tests/test_devices_list.py
git commit -m "feat(devices): GET /devices filters by site, never widening (phase 3 Task 13)"
```

- [ ] **Step 6: Las pruebas de la consola que fallan — la API, la lista y la ficha**

```ts
// frontend/src/lib/api.devicesSite.test.ts
/** One site's devices, and moving a device to another site of its client (design §4.3). */
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("a device's site", () => {
  it("asks for one site's devices, and moves a device with PATCH site_id", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementation(() => Promise.resolve(new Response(JSON.stringify({}), { status: 200 })));
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient(() => "t", vi.fn(), "/api/v1", () => null);
    await client.listDevices({ siteId: "s-north", limit: 50 });
    await client.changeDeviceSite("d1", "s-north");
    const sent = (fetchMock.mock.calls as [string, RequestInit][]).map(([url, init]) => [
      init.method ?? "GET",
      url,
      init.body === undefined ? undefined : JSON.parse(String(init.body)),
    ]);
    expect(sent).toEqual([
      ["GET", "/api/v1/devices?site_id=s-north&limit=50", undefined],
      ["PATCH", "/api/v1/devices/d1", { site_id: "s-north" }],
    ]);
  });
});
```

En `frontend/src/modules/devices/DevicesPage.test.tsx`:
- tras `vi.mock("../../lib/ws", …)` (`:38`):

```tsx
const clientScope = vi.hoisted(() => ({ selected: null as string | null }));
vi.mock("../../lib/clientScope", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/clientScope")>()),
  useClientScope: () => ({ selected: clientScope.selected, select: vi.fn(), clients: [], ready: true }),
}));
```

- en `fakeServer` (`:122-153`), el filtro `scoped` honra la sede como lo hace el servidor:

```ts
    const scoped = estate.filter(
      (device) =>
        (!params.q || matchesSearch(device, params.q)) &&
        (!params.provenance || device.provenance === params.provenance) &&
        (!params.siteId || device.site_id === params.siteId),
    );
```

- en `beforeEach` (`:180-182`) añade `clientScope.selected = null;`.
- al final del archivo:

```tsx
describe("the Client and Site columns (MSP clients phase 3, design §4.3)", () => {
  const ANDES = {
    id: "c-andes",
    name: "Andes Logistics",
    kind: "customer" as const,
    enabled_modules: [],
    permissions: ["devices.read", "clients.read"],
  };
  const SITES = [
    { id: "s-main", client_id: "c-andes", name: "Principal", address: "", description: "", egress_networks: [], archived_at: null, device_count: 1 },
    { id: "s-north", client_id: "c-andes", name: "Bodega norte", address: "", description: "", egress_networks: [], archived_at: null, device_count: 1 },
  ];
  const estate = () =>
    Promise.resolve({
      items: [
        device({ id: "a", hostname: "andes-main", fqdn: null, client_id: "c-andes", site_id: "s-main" }),
        device({ id: "b", hostname: "andes-north", fqdn: null, client_id: "c-andes", site_id: "s-north" }),
      ],
      total: 2,
    });

  it("names each row's client and site with every client chosen", async () => {
    renderPage(estate, { clients: [ANDES] } as Partial<Me>, "/devices", {
      listClientSites: vi.fn().mockResolvedValue(SITES),
    });
    const row = (await screen.findByText("andes-north")).closest("tr") as HTMLElement;
    expect(screen.getByRole("columnheader", { name: "Client" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Site" })).toBeInTheDocument();
    expect(within(row).getByText("Andes Logistics")).toBeInTheDocument();
    expect(await within(row).findByText("Bodega norte")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Filter by site" })).toBeNull();
  });

  it("hides the Client column with one client chosen, and filters by its sites on the server", async () => {
    clientScope.selected = "c-andes";
    const user = userEvent.setup();
    renderPage(estate, { clients: [ANDES] } as Partial<Me>, "/devices", {
      listClientSites: vi.fn().mockResolvedValue(SITES),
    });
    await screen.findByText("andes-main");
    expect(screen.queryByRole("columnheader", { name: "Client" })).toBeNull();
    await user.selectOptions(await screen.findByRole("combobox", { name: "Filter by site" }), "s-north");
    await waitFor(() => expect(screen.queryByText("andes-main")).toBeNull());
    expect(screen.getByText("andes-north")).toBeInTheDocument();
  });

  it("does not ask for the sites of a client the person cannot read", async () => {
    const listClientSites = vi.fn().mockResolvedValue(SITES);
    renderPage(estate, { clients: [{ ...ANDES, permissions: ["devices.read"] }] } as Partial<Me>, "/devices", {
      listClientSites,
    });
    const row = (await screen.findByText("andes-north")).closest("tr") as HTMLElement;
    expect(within(row).getByText("Site not known")).toBeInTheDocument();
    expect(listClientSites).not.toHaveBeenCalled();
  });
});
```

(añade `waitFor` al import de `@testing-library/react`, `:16`).

```tsx
// frontend/src/modules/inventory/DeviceSite.test.tsx
/**
 * A device's site, and moving it inside its client (MSP clients phase 3,
 * design §4.3). `SiteField` is the Task 9 picker, replaced here by a plain
 * select: what is pinned is the request this sends and the refusal it shows.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import type { ApiClient, Device } from "../../lib/api";
import { DeviceSiteDialog } from "./DeviceSite";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../components/SiteField", () => ({
  SiteField: ({ value, onChange }: { value: string | null; onChange: (id: string | null) => void }) => (
    <select aria-label="Site" value={value ?? ""} onChange={(event) => onChange(event.target.value || null)}>
      <option value="" />
      <option value="s-main">Principal</option>
      <option value="s-north">Bodega norte</option>
    </select>
  ),
}));

const DEVICE = { id: "d1", hostname: "andes-01", client_id: "c-andes", site_id: "s-main" } as Device;

function renderDialog(changeDeviceSite: ReturnType<typeof vi.fn>, onClose = vi.fn()) {
  mockUseApi.mockReturnValue({ changeDeviceSite } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <DeviceSiteDialog device={DEVICE} onClose={onClose} />
    </QueryClientProvider>,
  );
  return onClose;
}

beforeEach(() => mockUseApi.mockReset());

describe("moving a device to another site of its client", () => {
  it("sends the chosen site and closes", async () => {
    const changeDeviceSite = vi.fn().mockResolvedValue({ ...DEVICE, site_id: "s-north" });
    const onClose = renderDialog(changeDeviceSite);
    const user = userEvent.setup();
    const dialog = await screen.findByRole("dialog", { name: "Move to another site" });
    expect(within(dialog).getByRole("button", { name: "Move" })).toBeDisabled();
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Site" }), "s-north");
    await user.click(within(dialog).getByRole("button", { name: "Move" }));
    await waitFor(() => expect(changeDeviceSite).toHaveBeenCalledWith("d1", "s-north"));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("says the move did not happen, in the server's words", async () => {
    const changeDeviceSite = vi.fn().mockRejectedValue(
      new ApiError(409, JSON.stringify({ detail: "that site is archived" })),
    );
    renderDialog(changeDeviceSite);
    const user = userEvent.setup();
    const dialog = await screen.findByRole("dialog", { name: "Move to another site" });
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Site" }), "s-north");
    await user.click(within(dialog).getByRole("button", { name: "Move" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "The device was not moved. that site is archived",
    );
  });
});
```

En `frontend/src/modules/inventory/DeviceDetailPage.assets.test.tsx`: `renderPage` (`:73-100`) acepta `extra.me?: Partial<Me>` y `extra.listClientSites?` — el mock queda `me: vi.fn().mockResolvedValue({ ...ME, permissions: extra.permissions ?? ME.permissions, ...extra.me })` y `listClientSites: extra.listClientSites ?? vi.fn().mockResolvedValue([])`. Al final del archivo:

```tsx
describe("the device's site (MSP clients phase 3, design §4.3)", () => {
  const SITES = [
    { id: "s-main", client_id: "c-andes", name: "Principal", address: "", description: "", egress_networks: [], archived_at: null, device_count: 1 },
  ];
  const andes = (permissions: string[]) => ({
    clients: [{ id: "c-andes", name: "Andes", kind: "customer", enabled_modules: [], permissions }],
  });

  it("names the site, and offers to move it to an asset manager of its client", async () => {
    renderPage(asset({ client_id: "c-andes", site_id: "s-main" }), {
      me: andes(["clients.read", "assets.manage"]) as Partial<Me>,
      listClientSites: vi.fn().mockResolvedValue(SITES),
    });
    expect(await screen.findByText("Site: Principal")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Change site" })).toBeInTheDocument();
  });

  it("offers no move without assets.manage on the device's client", async () => {
    renderPage(asset({ client_id: "c-andes", site_id: "s-main" }), {
      me: andes(["clients.read"]) as Partial<Me>,
      listClientSites: vi.fn().mockResolvedValue(SITES),
    });
    expect(await screen.findByText("Site: Principal")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Change site" })).toBeNull();
  });
});
```

- [ ] **Step 7: Comprobar que fallan**

Run (desde `frontend/`): `npx vitest run src/lib/api.devicesSite.test.ts src/modules/devices/DevicesPage.test.tsx src/modules/inventory/DeviceSite.test.tsx src/modules/inventory/DeviceDetailPage.assets.test.tsx`
Expected: FAIL — `client.changeDeviceSite is not a function`; `Unable to find role="columnheader" and name "Client"`; `Failed to resolve import "./DeviceSite"`; `Unable to find an element with the text: Site: Principal`.

- [ ] **Step 8: La API, la lista y la ficha**

`frontend/src/lib/api.ts`, `DeviceListParams` (`:522-531`), tras `clientId?: string;`:

```ts
  /** One site's machines (`?site_id=`): the fleet list's site filter (MSP clients phase 3). */
  siteId?: string;
```

`listDevices`, tras `if (params.clientId) query.set("client_id", params.clientId);` (`:1851`):

```ts
    if (params.siteId) query.set("site_id", params.siteId);
```

Tras `updateAsset` (`:1931-1936`):

```ts
  /**
   * Move a device to another site of ITS client (`PATCH /devices/{id}` with
   * `site_id`, `assets.manage`; MSP clients phase 3, design §4.3). Another
   * client's site is a 422, an archived one a 409, one the request cannot see
   * a 404. Audited `device.site_changed`. A device never changes client.
   */
  changeDeviceSite(deviceId: string, siteId: string): Promise<Device> {
    return this.request<Device>(`/devices/${deviceId}`, {
      method: "PATCH",
      body: JSON.stringify({ site_id: siteId }),
    });
  }
```

```ts
// frontend/src/modules/devices/siteNames.ts
/**
 * Site names for the fleet list's Site column and its filter, and for a
 * device's page (MSP clients phase 3, design §4.3).
 *
 * `GET /devices` sends each row's `site_id`, never the name. The names are
 * each client's own sites (`GET /clients/{id}/sites`), read once per client
 * on the page under the key IPAM and the site pickers already share, so the
 * cache is one. Read only for a client where the person holds `clients.read`
 * -- the route's code -- so the page never asks for a 403; without it the
 * name is unknown and says so.
 */
import { useQueries, useQuery } from "@tanstack/react-query";

import type { ClientSiteOut, Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";

export function useSitesOf(clientIds: readonly string[]): {
  sitesOf(clientId: string): readonly ClientSiteOut[] | undefined;
  siteName(siteId: string | undefined): string | null;
} {
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const readable = new Set(
    (me.data?.clients ?? [])
      .filter((client) => client.permissions.includes("clients.read"))
      .map((client) => client.id),
  );
  const wanted = [...new Set(clientIds)].filter((clientId) => readable.has(clientId));
  const results = useQueries({
    queries: wanted.map((clientId) => ({
      queryKey: ["client-sites", clientId],
      queryFn: () => api.listClientSites(clientId),
      staleTime: 60_000,
      retry: false,
    })),
  });
  const byClient = new Map<string, readonly ClientSiteOut[]>();
  const names = new Map<string, string>();
  wanted.forEach((clientId, index) => {
    const data = results[index]?.data;
    if (!data) return;
    byClient.set(clientId, data);
    for (const site of data) names.set(site.id, site.name);
  });
  return {
    sitesOf: (clientId) => byClient.get(clientId),
    siteName: (siteId) => (siteId ? (names.get(siteId) ?? null) : null),
  };
}
```

`frontend/src/modules/devices/DevicesPage.tsx`:

1. El comentario obsoleto `:15-26` («WHAT THE DESIGN ASKS FOR THAT THE API DOES NOT SERVE…») pasa a:

```ts
 * WHAT THE DESIGN ASKS FOR THAT THE API DOES NOT SERVE. The prototype's
 * Devices table has eight columns; three of them — Patch %, Backup, Risk —
 * have no field behind them. (Site has one since the MSP clients model: it is
 * a column, with the device's client beside it while every client is shown,
 * design 2026-10-31 §4.3.) The same is true of two of its four stat cards:
 * "At risk" has no definition anywhere in the backend, and every card's
 * "↑ 12 vs last 7 days" delta needs a previous value no endpoint serves.
 *
 * Those columns are OMITTED, not faked. Hardcoding the mock's numbers would
 * produce a screen that looks finished and lies — and it would lie most
 * convincingly on the demo where someone decides to trust it. Each gap is
 * listed in the UI port spec §7 so it becomes a backlog item rather than a
 * silent difference from the design.
```

2. Imports: `import { useEffect, useRef, useState } from "react";` (`:40`), y tras `import { useLiveEvents } from "../../lib/ws";` (`:52`):

```ts
import { useClientScope } from "../../lib/clientScope";
import { useClientName } from "../settings/api";
import { useSitesOf } from "./siteNames";
```

3. Tras `const provenance = params.get("provenance") ?? "";` (`:243`):

```ts
  // THE SITE FILTER (design §4.3), in the URL like every filter here. Sites are
  // one client's, so it applies only with a client chosen above.
  const site = params.get("site") ?? "";
  const { selected: chosenClient } = useClientScope();
  const clientName = useClientName();
```

tras `setProvenance` (`:264`):

```ts
  const setSite = (value: string) => update({ site: value, offset: "" });
  // ANOTHER CLIENT CHOSEN ABOVE drops the site: it was the previous client's,
  // and the new one has none of that id.
  const shownFor = useRef(chosenClient);
  useEffect(() => {
    if (shownFor.current === chosenClient) return;
    shownFor.current = chosenClient;
    if (site !== "") update({ site: "", offset: "" }, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only a change of client clears it
  }, [chosenClient]);
```

y en `query` (`:284-292`), tras la línea de `provenance`:

```ts
    siteId: chosenClient !== null && site !== "" ? site : undefined,
```

4. Tras `const items = devices.data?.items ?? [];` (`:364`):

```ts
  // THE COLUMNS OF MSP CLIENTS (design §3.4, §4.3): Client while every client
  // is shown (the same column in every module), Site always.
  const showClient = chosenClient === null;
  const sites = useSitesOf(
    chosenClient !== null
      ? [chosenClient]
      : items.map((device) => device.client_id).filter((id): id is string => Boolean(id)),
  );
  const chosenSites = chosenClient !== null ? (sites.sitesOf(chosenClient) ?? []) : [];
```

5. El filtro, tras el `</select>` de procedencia (`:594`) y antes de `<span className="filter-spacer" />`:

```tsx
          {chosenClient !== null && (chosenSites.length > 1 || site !== "") ? (
            <select
              className="select-field"
              value={site}
              onChange={(event) => setSite(event.target.value)}
              aria-label={t("devices.filters.siteAria")}
            >
              <option value="">{t("devices.filters.siteAny")}</option>
              {chosenSites.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          ) : null}
```

6. Cabecera, tras `<th scope="col">{t("devices.table.lastSeen")}</th>` (`:674`):

```tsx
                  {showClient ? <th scope="col">{t("devices.table.client")}</th> : null}
                  <th scope="col">{t("devices.table.site")}</th>
```

7. Filas, tras la celda de «visto por última vez» (`:777-779`, `<td className="whitespace-nowrap"><RelTime … /></td>`):

```tsx
                      {showClient ? (
                        <td>
                          {device.client_id ? clientName(device.client_id) : <NoValue label={t("devices.row.noClient")} />}
                        </td>
                      ) : null}
                      <td>{sites.siteName(device.site_id) ?? <NoValue label={t("devices.row.noSite")} />}</td>
```

```tsx
// frontend/src/modules/inventory/DeviceSite.tsx
/**
 * A device's site, and moving it to another site of ITS client (MSP clients
 * phase 3, design §4.3; `PATCH /devices/{id}` with `site_id`). Moving a device
 * to another client is out of scope (MSP clients design §6), so `SiteField`
 * offers the client's own sites and the server refuses any other (422).
 *
 * OFFERED to a holder of `assets.manage` IN THE DEVICE'S CLIENT, from
 * `/me.clients`. A courtesy (invariant 6): the route checks the code.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { SiteField } from "../../components/SiteField";
import { Form, FormActions } from "../../components/form";
import { Button, Dialog } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { Device } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { Btn } from "../../shell/components";
import { useSitesOf } from "../devices/siteNames";

export function DeviceSite({ device, canMove }: { device: Device; canMove: boolean }) {
  const t = useT();
  const sites = useSitesOf(device.client_id ? [device.client_id] : []);
  const [open, setOpen] = useState(false);
  const name = sites.siteName(device.site_id);
  return (
    <>
      {name ? <span className="cell-sub">{t("inventory.site.label", { site: name })}</span> : null}
      {canMove ? <Btn small label={t("inventory.site.change")} onClick={() => setOpen(true)} /> : null}
      {open ? <DeviceSiteDialog device={device} onClose={() => setOpen(false)} /> : null}
    </>
  );
}

export function DeviceSiteDialog({ device, onClose }: { device: Device; onClose: () => void }) {
  const t = useT();
  const api = useApi();
  const queryClient = useQueryClient();
  const [siteId, setSiteId] = useState<string | null>(device.site_id ?? null);
  const move = useMutation({
    mutationFn: (target: string) => api.changeDeviceSite(device.id, target),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["device", device.id] });
      void queryClient.invalidateQueries({ queryKey: ["devices"] });
      // Two sites' device counts moved.
      void queryClient.invalidateQueries({ queryKey: ["client-sites"] });
      onClose();
    },
  });
  const unchanged = siteId === null || siteId === device.site_id;

  return (
    <Dialog open onClose={onClose} title={t("inventory.site.dialogTitle")}>
      <Form
        onSubmit={() => {
          if (!unchanged && siteId !== null) move.mutate(siteId);
        }}
      >
        <p className="note mb-3">{t("inventory.site.dialogNote")}</p>
        <div className="form-grid">
          {device.client_id ? <SiteField clientId={device.client_id} value={siteId} onChange={setSiteId} /> : null}
        </div>
        <div className="flex items-center justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.actions.cancel")}
          </Button>
          <FormActions
            label={t("inventory.site.move")}
            pendingLabel={t("inventory.site.moving")}
            pending={move.isPending}
            error={move.isError ? move.error : undefined}
            failedMessage={t("inventory.site.moveFailed")}
            disabledReason={unchanged ? t("inventory.site.unchanged") : undefined}
          />
        </div>
      </Form>
    </Dialog>
  );
}
```

`frontend/src/modules/inventory/DeviceDetailPage.tsx`: `import { DeviceSite } from "./DeviceSite";` junto a los imports del módulo, y tras el bloque de `DeviceAssignedTo` (`:524-528`):

```tsx
        {/* ITS SITE, and the move inside its client (design §4.3). */}
        {device.data?.client_id && device.data.site_id ? (
          <DeviceSite
            device={device.data}
            canMove={(me.data?.clients ?? []).some(
              (client) => client.id === device.data?.client_id && client.permissions.includes("assets.manage"),
            )}
          />
        ) : null}
```

Catálogos. `frontend/src/i18n/en/devices.ts`: en `filters` (`:36-50`), tras `provenanceAny`, `siteAria: "Filter by site",` y `siteAny: "Every site",`; en `table` (`:56-67`), tras `lastSeen`, `client: "Client",` y `site: "Site",`; en `row` (`:68-80`), tras `noArch`, `noClient: "Client not known",` y `noSite: "Site not known",`. `frontend/src/i18n/es/devices.ts`, en los mismos sitios: `siteAria: "Filtrar por sede",`, `siteAny: "Todas las sedes",`, `client: "Cliente",`, `site: "Sede",`, `noClient: "Cliente desconocido",`, `noSite: "Sede desconocida",`.

`frontend/src/i18n/en/inventory.ts`, antes de `software: {` (`:179`):

```ts
  // A device's site, and moving it inside its client (MSP clients phase 3, design §4.3).
  site: {
    label: "Site: {site}",
    change: "Change site",
    dialogTitle: "Move to another site",
    dialogNote: "Only the sites of its own client are offered: a device never changes client.",
    move: "Move",
    moving: "Moving…",
    moveFailed: "The device was not moved.",
    unchanged: "Choose another of its client's sites.",
  },
```

`frontend/src/i18n/es/inventory.ts`, antes de `software: {` (`:177`):

```ts
  site: {
    label: "Sede: {site}",
    change: "Cambiar de sede",
    dialogTitle: "Mover a otra sede",
    dialogNote: "Solo se ofrecen las sedes de su propio cliente: un dispositivo nunca cambia de cliente.",
    move: "Mover",
    moving: "Moviendo…",
    moveFailed: "No se movió el dispositivo.",
    unchanged: "Elige otra sede de su cliente.",
  },
```

- [ ] **Step 9: Comprobar que pasan**

Run: `npx vitest run src/lib/api.devicesSite.test.ts src/modules/devices src/modules/inventory`
Expected: PASS. Las pruebas existentes de `src/modules/devices` montan `DevicesPage` con el mock de `clientScope` de arriba (`selected: null`), así que ven la columna Cliente; ninguna fija el número de columnas más allá de las tres primeras (`DevicesPage.test.tsx:476-484`, intactas). Si `DevicesPushSelection.test.tsx`, `DevicesReview.test.tsx`, `DevicesApproveAll.test.tsx` o `devices.es.test.tsx` fallan con un error de `useClientScope` fuera de su proveedor, añade a cada uno el mismo bloque `clientScope`/`vi.mock` del Step 6.

- [ ] **Step 10: Commit**

```bash
git add frontend/src/lib/api.ts frontend/src/lib/api.devicesSite.test.ts frontend/src/modules/devices frontend/src/modules/inventory frontend/src/i18n/en/devices.ts frontend/src/i18n/es/devices.ts frontend/src/i18n/en/inventory.ts frontend/src/i18n/es/inventory.ts
git commit -m "feat(devices): Client and Site columns, a site filter, and moving a device inside its client (phase 3 Task 13)"
```

- [ ] **Step 11: Las pruebas que fallan — el alta**

En `frontend/src/modules/onboarding/__tests__/journey.test.ts`, cambia el import (`:16`) por `import { currentStep, journeyQuery, journeySteps, withJourney, type JourneyInput, type StepKey } from "../journey";` y añade al final:

```ts
describe("the journey's client and site (MSP clients phase 3, design §4.4)", () => {
  it("carries both on every step's destination", () => {
    const steps = journeySteps(input({ linkQuery: journeyQuery("c-andes", "s-main") }));
    const destinations = steps.map((s) => s.to).filter((to): to is string => to !== null);
    expect(destinations.length).toBeGreaterThan(0);
    for (const to of destinations) expect(to).toMatch(/\?client=c-andes&site=s-main$/);
  });

  it("leaves every destination alone until both are known", () => {
    expect(journeySteps(input()).map((s) => s.to)).toEqual(
      journeySteps(input({ linkQuery: "" })).map((s) => s.to),
    );
    expect(step({}, "register").to).toBe("/ipam");
  });

  it("builds the query, and keeps a fragment last", () => {
    expect(journeyQuery("c 1", "s-main")).toBe("client=c+1&site=s-main");
    expect(journeyQuery("c1", null)).toBe("client=c1");
    expect(withJourney("/ipam", "client=c1&site=s1")).toBe("/ipam?client=c1&site=s1");
    expect(withJourney("/x?a=1", "client=c1")).toBe("/x?a=1&client=c1");
    expect(withJourney("/health#profiles", "client=c1")).toBe("/health?client=c1#profiles");
    expect(withJourney("/ipam", "")).toBe("/ipam");
  });
});
```

```tsx
// frontend/src/modules/onboarding/__tests__/journeyTarget.test.tsx
/**
 * A page opened from the add-a-computer journey (MSP clients phase 3, design
 * §4.4): under «Todos mis clientes» it works on the journey's client, never
 * over a client chosen above, and its site starts at the journey's.
 */
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { MeClient } from "../../../lib/api";
import { useAdoptJourneyClient, useJourneySiteState } from "../journeyTarget";

const clientScope = vi.hoisted(() => ({
  selected: null as string | null,
  clients: [] as MeClient[],
  select: vi.fn(),
}));
vi.mock("../../../lib/clientScope", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/clientScope")>()),
  useClientScope: () => ({
    selected: clientScope.selected,
    select: clientScope.select,
    clients: clientScope.clients,
    ready: true,
  }),
}));

const ANDES: MeClient = { id: "c-andes", name: "Andes", kind: "customer", enabled_modules: [], permissions: [] };

function Probe() {
  useAdoptJourneyClient();
  const [site] = useJourneySiteState();
  return <p>{site ?? "none"}</p>;
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Probe />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  clientScope.selected = null;
  clientScope.clients = [ANDES];
  clientScope.select = vi.fn();
});

describe("a page opened from the journey", () => {
  it("makes an every-client console work on the journey's client", () => {
    renderAt("/enrollment?client=c-andes&site=s-north");
    expect(clientScope.select).toHaveBeenCalledWith("c-andes");
    expect(clientScope.select).toHaveBeenCalledTimes(1);
  });

  it("never overrides a client chosen above, and ignores that client's site", () => {
    clientScope.selected = "c-other";
    renderAt("/enrollment?client=c-andes&site=s-north");
    expect(clientScope.select).not.toHaveBeenCalled();
    expect(screen.getByText("none")).toBeInTheDocument();
  });

  it("ignores a client that is not one of the person's", () => {
    clientScope.clients = [];
    renderAt("/enrollment?client=c-andes&site=s-north");
    expect(clientScope.select).not.toHaveBeenCalled();
  });

  it("starts the site at the journey's once the console works on its client", () => {
    clientScope.selected = "c-andes";
    renderAt("/enrollment?client=c-andes&site=s-north");
    expect(screen.getByText("s-north")).toBeInTheDocument();
  });
});
```

En `frontend/src/modules/onboarding/OnboardPage.test.tsx`:
- imports: `import { render, screen, waitFor } from "@testing-library/react";`, `import userEvent from "@testing-library/user-event";`, `import type { ApiClient, ClientSiteOut, MeClient } from "../../lib/api";`.
- tras `vi.mock("../../lib/ApiContext", …)` (`:32`):

```tsx
const clientScope = vi.hoisted(() => ({
  selected: null as string | null,
  select: vi.fn(),
  clients: [] as MeClient[],
}));
vi.mock("../../lib/clientScope", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/clientScope")>()),
  useClientScope: () => ({
    selected: clientScope.selected,
    select: clientScope.select,
    clients: clientScope.clients,
    ready: true,
  }),
}));
// The Task 9 pickers, as plain selects: what is pinned is what the page does
// with the choice, not how the picker draws.
vi.mock("../../components/ClientField", () => ({
  ClientField: ({ onChange }: { onChange: (id: string | null) => void }) => (
    <select aria-label="Client" defaultValue="" onChange={(event) => onChange(event.target.value || null)}>
      <option value="" />
      <option value="c-andes">Andes Logistics</option>
    </select>
  ),
}));
vi.mock("../../components/SiteField", () => ({
  SiteField: ({ value, onChange }: { value: string | null; onChange: (id: string | null) => void }) => (
    <select aria-label="Site" value={value ?? ""} onChange={(event) => onChange(event.target.value || null)}>
      <option value="" />
      <option value="s-main">Principal</option>
      <option value="s-north">Bodega norte</option>
    </select>
  ),
}));

const ANDES: MeClient = {
  id: "c-andes",
  name: "Andes Logistics",
  kind: "customer",
  enabled_modules: [],
  permissions: ["devices.read", "clients.read"],
};
const MAIN: ClientSiteOut = {
  id: "s-main",
  client_id: "c-andes",
  name: "Principal",
  address: "",
  description: "",
  egress_networks: [],
  archived_at: null,
  device_count: 0,
};
const NORTH: ClientSiteOut = { ...MAIN, id: "s-north", name: "Bodega norte" };
```

- `renderPage` gana `sites = [] as ClientSiteOut[],` en sus parámetros; `me` devuelve además `clients: [ANDES], permissions_everywhere: []`; el mock gana `listClientSites: () => Promise.resolve(sites),`.
- `beforeEach` (`:107-109`) añade `clientScope.selected = null; clientScope.select = vi.fn(); clientScope.clients = [ANDES];`.
- al final:

```tsx
describe("the client and the site come first (MSP clients phase 3, design §4.4)", () => {
  it("asks for the client with every client chosen, and choosing one makes the console work on it", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.selectOptions(await screen.findByRole("combobox", { name: "Client" }), "c-andes");
    expect(clientScope.select).toHaveBeenCalledWith("c-andes");
    // Nothing chosen yet, so the links carry nothing.
    const link = await screen.findByRole("link", { name: /Open IP Address Management/i });
    expect(link.getAttribute("href")).toBe("/ipam");
  });

  it("asks nothing for a client with one site, and every link carries both", async () => {
    clientScope.selected = "c-andes";
    renderPage({ sites: [MAIN] });
    expect(await screen.findByText("Site: Principal, the client's only one.")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Site" })).toBeNull();
    await waitFor(() =>
      expect(screen.getByRole("link", { name: /Open IP Address Management/i }).getAttribute("href")).toBe(
        "/ipam?client=c-andes&site=s-main",
      ),
    );
    expect(screen.getByRole("link", { name: /Get the installer/i }).getAttribute("href")).toBe(
      "/agent-packages?client=c-andes&site=s-main",
    );
    expect(screen.getByRole("link", { name: /Enrollment tokens/i }).getAttribute("href")).toBe(
      "/enrollment?client=c-andes&site=s-main",
    );
  });

  it("asks for the site when the client has several", async () => {
    clientScope.selected = "c-andes";
    const user = userEvent.setup();
    renderPage({ sites: [MAIN, NORTH] });
    const site = await screen.findByRole("combobox", { name: "Site" });
    expect(screen.getByRole("link", { name: /Get the installer/i }).getAttribute("href")).toBe("/agent-packages");
    await user.selectOptions(site, "s-north");
    await waitFor(() =>
      expect(screen.getByRole("link", { name: /Get the installer/i }).getAttribute("href")).toBe(
        "/agent-packages?client=c-andes&site=s-north",
      ),
    );
  });
});
```

`frontend/src/modules/onboarding/onboarding.es.test.tsx`, tras `vi.mock("../../lib/ApiContext", …)` (`:20`):

```tsx
vi.mock("../../lib/clientScope", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/clientScope")>()),
  useClientScope: () => ({ selected: null, select: vi.fn(), clients: [], ready: true }),
}));
vi.mock("../../components/ClientField", () => ({
  ClientField: () => <select aria-label="Cliente" defaultValue="" />,
}));
vi.mock("../../components/SiteField", () => ({
  SiteField: () => <select aria-label="Sede" defaultValue="" />,
}));
```

Su `renderPage` gana `listClientSites: () => Promise.resolve([]),` en el mock y `clients: [], permissions_everywhere: []` en `me`. Un caso nuevo dentro de su `describe`:

```tsx
  it("words the client and site panel", async () => {
    const { container } = renderPage();
    expect(await screen.findByText("Para qué cliente y sede")).toBeInTheDocument();
    expect(
      screen.getByText("Elige primero el cliente y la sede; después los enlaces de abajo los llevan."),
    ).toBeInTheDocument();
    expectNoEnglish(container, ALLOW);
  });
```

- [ ] **Step 12: Comprobar que fallan**

Run: `npx vitest run src/modules/onboarding`
Expected: FAIL — `journeyQuery is not a function` (import sin resolver), `Failed to resolve import "../journeyTarget"`, `Unable to find role="combobox" and name "Client"`.

- [ ] **Step 13: El recorrido con cliente y sede**

`frontend/src/modules/onboarding/journey.ts`. Tras `export type StepKey = …` (`:43`):

```ts
/** The URL parameters that carry the journey's client and site (design §4.4). */
export const JOURNEY_CLIENT_PARAM = "client";
export const JOURNEY_SITE_PARAM = "site";

/** `client=…&site=…` (no `?`): what every page the journey opens is for. */
export function journeyQuery(client: string, site: string | null): string {
  const query = new URLSearchParams();
  query.set(JOURNEY_CLIENT_PARAM, client);
  if (site !== null) query.set(JOURNEY_SITE_PARAM, site);
  return query.toString();
}

/** `to` with the journey's query added, before any `#fragment`. */
export function withJourney(to: string, query: string): string {
  if (query === "") return to;
  const hash = to.indexOf("#");
  const path = hash < 0 ? to : to.slice(0, hash);
  const fragment = hash < 0 ? "" : to.slice(hash);
  return `${path}${path.includes("?") ? "&" : "?"}${query}${fragment}`;
}
```

En `JourneyInput` (`:66-80`), tras `readonly now?: number;`:

```ts
  /**
   * `client=…&site=…` (MSP clients phase 3, design §4.4), added to every
   * step's destination so the page it opens works on that client and site.
   * Absent or "" until both are known.
   */
  readonly linkQuery?: string;
```

`journeySteps` (`:318-326`):

```ts
export function journeySteps(input: JourneyInput): readonly JourneyStep[] {
  const query = input.linkQuery ?? "";
  return [
    registerStep(input),
    authorizeStep(input),
    sweepStep(input),
    reviewStep(input),
    agentStep(input),
  ].map((step) => (step.to === null ? step : { ...step, to: withJourney(step.to, query) }));
}
```

```ts
// frontend/src/modules/onboarding/journeyTarget.ts
/**
 * The add-a-computer journey's client and site on the pages it opens (MSP
 * clients phase 3, design §4.4), carried as `?client=…&site=…`.
 *
 * THE CLIENT IS THE CONSOLE'S. A page opened from the journey under «Todos
 * mis clientes» switches the console to the journey's client, once, when it
 * is one of the person's (`/me.clients`): its lists then narrow to it and its
 * new rows go to it (rule 2 of design §3.1). A client chosen above is never
 * overridden; the journey's site is then ignored, being another client's.
 *
 * THE SITE IS THE PAGE'S FIRST CHOICE. `useJourneySiteState` starts a page's
 * site at the journey's, once the console works on the journey's client; the
 * person can still choose another.
 */
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { useClientScope } from "../../lib/clientScope";
import { JOURNEY_CLIENT_PARAM, JOURNEY_SITE_PARAM } from "./journey";

export function useAdoptJourneyClient(): void {
  const [params] = useSearchParams();
  const wanted = params.get(JOURNEY_CLIENT_PARAM);
  const { selected, select, clients } = useClientScope();
  const known = wanted !== null && clients.some((client) => client.id === wanted);
  useEffect(() => {
    if (known && selected === null && wanted !== null) select(wanted);
  }, [known, selected, select, wanted]);
}

/** The journey's site, while the console works on the journey's client. */
export function useJourneySite(): string | null {
  const [params] = useSearchParams();
  const { selected } = useClientScope();
  const client = params.get(JOURNEY_CLIENT_PARAM);
  return client !== null && client === selected ? params.get(JOURNEY_SITE_PARAM) : null;
}

/** A page's site choice, starting at the journey's once that is usable. */
export function useJourneySiteState(): [string | null, (id: string | null) => void] {
  const journeySite = useJourneySite();
  const [siteId, setSiteId] = useState<string | null>(journeySite);
  const seeded = useRef(journeySite !== null);
  useEffect(() => {
    if (!seeded.current && journeySite !== null) {
      seeded.current = true;
      setSiteId(journeySite);
    }
  }, [journeySite]);
  return [siteId, setSiteId];
}
```

`frontend/src/modules/onboarding/OnboardPage.tsx`:
- imports: `import { Link, useSearchParams } from "react-router-dom";` (`:42`) y, tras los de `../ipam/…`:

```ts
import { ClientField } from "../../components/ClientField";
import { SiteField } from "../../components/SiteField";
import { useClientScope } from "../../lib/clientScope";
import { useSitesOf } from "../devices/siteNames";
import { currentStep, JOURNEY_SITE_PARAM, journeyQuery, journeySteps, withJourney, type JourneyStep, type StepState } from "./journey";
import { useAdoptJourneyClient } from "./journeyTarget";
```

(sustituye el import de `./journey` de `:56`).
- antes de `export function OnboardPage()`:

```ts
/** Adding a computer is offered on the clients whose devices the person can see. */
const ONBOARD_CODE = "devices.read";
```

- dentro de `OnboardPage`, tras `const permissions = …` (`:104`):

```ts
  // CLIENT AND SITE FIRST (design §4.4). The client is the console's: chosen
  // above, or chosen here, which switches the console to it (plan decision).
  // With one site nothing is asked; the pair then rides on every link below.
  useAdoptJourneyClient();
  const { selected, select, clients } = useClientScope();
  const [params, setParams] = useSearchParams();
  const sites = useSitesOf(selected !== null ? [selected] : []);
  const siteList = selected !== null ? (sites.sitesOf(selected) ?? []) : [];
  const onlySite = siteList.length === 1 ? (siteList[0] ?? null) : null;
  const askedSite = params.get(JOURNEY_SITE_PARAM);
  const site = onlySite?.id ?? (siteList.some((item) => item.id === askedSite) ? askedSite : null);
  const linkQuery = selected !== null && site !== null ? journeyQuery(selected, site) : "";

  function chooseSite(id: string | null) {
    const next = new URLSearchParams(params);
    if (id) next.set(JOURNEY_SITE_PARAM, id);
    else next.delete(JOURNEY_SITE_PARAM);
    setParams(next, { replace: true });
  }
```

- en `journeySteps({ … })` (`:133-144`) añade `linkQuery,`.
- el primer panel de la página, entre `<PageHead … />` (`:149-152`) y el panel «One computer» (`:158`):

```tsx
      <Panel title={t("onboarding.target.title")} subtitle={t("onboarding.target.subtitle")}>
        {selected === null ? (
          <div className="form-grid">
            <ClientField
              code={ONBOARD_CODE}
              value={null}
              onChange={(id) => {
                if (id) select(id);
              }}
            />
          </div>
        ) : (
          <>
            <p className="note">
              {t("onboarding.target.client", {
                client: clients.find((item) => item.id === selected)?.name ?? selected,
              })}
            </p>
            {onlySite !== null ? (
              <p className="note">{t("onboarding.target.oneSite", { site: onlySite.name })}</p>
            ) : (
              <div className="form-grid">
                <SiteField clientId={selected} value={site} onChange={chooseSite} />
              </div>
            )}
          </>
        )}
        {linkQuery === "" ? <p className="note">{t("onboarding.target.pickFirst")}</p> : null}
      </Panel>
```

- los dos enlaces del panel «One computer» (`:163`, `:167`): `to={withJourney("/agent-packages", linkQuery)}` y `to={withJourney("/enrollment", linkQuery)}`.

Las páginas a las que lleva el recorrido. Cada una añade `useAdoptJourneyClient();` como primera línea de su componente, con `import { useAdoptJourneyClient } from "../onboarding/journeyTarget";`:
- `frontend/src/modules/ipam/SubnetTreePage.tsx:100` (`SubnetTreePage`, paso 1);
- `frontend/src/modules/ipam/ScanAuthorizationsPage.tsx:75` (`ScanAuthorizationsPage`, paso 2);
- `frontend/src/modules/devices/CandidatesPage.tsx:151` (`CandidatesPage`, paso 4);
- `frontend/src/modules/enrollment/EnrollmentPage.tsx:273` (`EnrollmentPage`);
- `frontend/src/modules/agent-releases/AgentReleasesPage.tsx:153` (`AgentReleasesPage`, paso 5).

Y la sede. La Task 10 dejó en `EnrollmentPage` (formulario del token) y en `AgentReleasesPage` (enlace, instrucción de instalación y despliegue gestionado, que recibe la sede por `siteId`) un estado con la sede del `SiteField`, `const [siteId, setSiteId] = useState<string | null>(null)`. Ese estado pasa a ser `const [siteId, setSiteId] = useJourneySiteState();` (`import { useAdoptJourneyClient, useJourneySiteState } from "../onboarding/journeyTarget";`). Si la Task 10 lo guardó como `string` (`useState("")`), deja su `useState` y añade debajo:

```ts
  const [journeySite] = useJourneySiteState();
  useEffect(() => {
    if (journeySite !== null && siteId === "") setSiteId(journeySite);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- seeds once from the journey
  }, [journeySite]);
```

Catálogos. `frontend/src/i18n/en/onboarding.ts`, antes de `state: {` (`:30`):

```ts
  // The client and site the journey is for (MSP clients phase 3, design §4.4).
  target: {
    title: "Which client and site",
    subtitle: "What you add from the pages below is filed under this client and this site.",
    client: "Client: {client}",
    oneSite: "Site: {site}, the client's only one.",
    pickFirst: "Choose the client and the site first; the links below then carry them.",
  },
```

`frontend/src/i18n/es/onboarding.ts`, antes de `state: {` (`:33`):

```ts
  target: {
    title: "Para qué cliente y sede",
    subtitle: "Lo que añadas desde las páginas de abajo queda en este cliente y en esta sede.",
    client: "Cliente: {client}",
    oneSite: "Sede: {site}, la única del cliente.",
    pickFirst: "Elige primero el cliente y la sede; después los enlaces de abajo los llevan.",
  },
```

- [ ] **Step 14: Comprobar que pasan**

Run: `npx vitest run src/modules/onboarding src/modules/ipam src/modules/enrollment src/modules/agent-releases src/modules/devices/CandidatesPage.test.tsx`
Expected: PASS. Las pruebas de las cinco páginas enlazadas ya montan bajo un `MemoryRouter`; su `useClientScope` lo resolvió la Task 10 (que puso `useNewRowClient`/`SiteField` en esas mismas páginas). Si una falla con `useClientScope` fuera de su proveedor, añade el bloque `clientScope`/`vi.mock` del Step 6 (con `clients: []`).

- [ ] **Step 15: Commit**

```bash
git add frontend/src/modules/onboarding frontend/src/modules/ipam/SubnetTreePage.tsx frontend/src/modules/ipam/ScanAuthorizationsPage.tsx frontend/src/modules/devices/CandidatesPage.tsx frontend/src/modules/enrollment/EnrollmentPage.tsx frontend/src/modules/agent-releases/AgentReleasesPage.tsx frontend/src/i18n/en/onboarding.ts frontend/src/i18n/es/onboarding.ts
git commit -m "feat(onboarding): client and site first, carried as ?client=&site= into every page of the journey (phase 3 Task 13)"
```

- [ ] **Step 16: Las pruebas que fallan — tickets y usuarios**

En `frontend/src/modules/service-desk/__tests__/ticketQueue.test.tsx`, tras `vi.mock("../../../lib/ApiContext", …)` (`:19`):

```tsx
const clientScope = vi.hoisted(() => ({ selected: null as string | null }));
vi.mock("../../../lib/clientScope", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/clientScope")>()),
  useClientScope: () => ({ selected: clientScope.selected, select: vi.fn(), clients: [], ready: true }),
}));
```

`beforeEach` (`:130-133`) añade `clientScope.selected = null;`. Al final:

```tsx
describe("the Client column (MSP clients phase 3, design §4.5)", () => {
  const ME_WITH_CLIENTS = {
    ...ME,
    clients: [
      { id: "c-andes", name: "Andes Logistics", kind: "customer", enabled_modules: [], permissions: ["ticket.read"] },
    ],
    permissions_everywhere: [],
  } as Me;
  const ANDES_TICKET = ticket("7", { client_id: "c-andes" });

  it("names each ticket's client while every client is shown", async () => {
    mockUseApi.mockReturnValue({
      ...api(fakeListTickets([ANDES_TICKET])),
      me: vi.fn().mockResolvedValue(ME_WITH_CLIENTS),
    } as ApiClient);
    renderAt(<TicketListPage />, "/service-desk");
    const row = (await screen.findByRole("link", { name: "INC-7" })).closest("tr") as HTMLElement;
    expect(screen.getByRole("columnheader", { name: "Client" })).toBeInTheDocument();
    expect(await within(row).findByText("Andes Logistics")).toBeInTheDocument();
  });

  it("hides it with one client chosen, on My work too", async () => {
    clientScope.selected = "c-andes";
    mockUseApi.mockReturnValue({
      ...api(fakeListTickets([{ ...ANDES_TICKET, assigned_to: ME.user_id }])),
      me: vi.fn().mockResolvedValue(ME_WITH_CLIENTS),
    } as ApiClient);
    renderAt(<MyWorkPage />, "/service-desk/my-work");
    await screen.findByRole("link", { name: "INC-7" });
    expect(screen.queryByRole("columnheader", { name: "Client" })).toBeNull();
  });
});
```

```ts
// frontend/src/modules/settings/__tests__/grantGroups.test.ts
import { describe, expect, it } from "vitest";

import { groupGrantsByClient } from "../grantGroups";

const names = new Map([
  ["c-b", "Bodegas"],
  ["c-a", "Andes"],
]);
const name = (id: string) => names.get(id) ?? id;

describe("groupGrantsByClient", () => {
  it("puts the every-client grants first, then one group per client by name, codes sorted", () => {
    const groups = groupGrantsByClient(
      [
        { code: "ticket.read", via_role: "technician", client_id: "c-b" },
        { code: "devices.read", via_role: "technician", client_id: null },
        { code: "devices.manage", via_role: "technician", client_id: "c-b" },
        { code: "audit.read", via_role: "auditor", client_id: "c-a" },
      ],
      name,
    );
    expect(groups.map((group) => [group.clientId, group.grants.map((grant) => grant.code)])).toEqual([
      [null, ["devices.read"]],
      ["c-a", ["audit.read"]],
      ["c-b", ["devices.manage", "ticket.read"]],
    ]);
  });

  it("has no every-client group when nothing holds on every client", () => {
    expect(
      groupGrantsByClient([{ code: "x.read", via_role: "r", client_id: "c-a" }], name).map((g) => g.clientId),
    ).toEqual(["c-a"]);
    expect(groupGrantsByClient([], name)).toEqual([]);
  });
});
```

**Pruebas que fijan el formato viejo y se corrigen** (el diseño §4.2 cambia la tabla a propósito):
- `frontend/src/modules/settings/__tests__/users.test.tsx:319-349`, «shows effective permissions annotated with the role that granted each and its scope». Viejo: `within(orgWide).getByText(/everywhere/i)` y `within(scoped).getByText(/limited to Warehouse/i)`. Nuevo, el cuerpo entero del caso tras `waitFor(...)` (`:327-329`):

```tsx
    // RE-POINTED (MSP clients phase 3, design §4.2): the "Applies to" column
    // became the group a grant sits in -- every client first, then each client.
    const every = await within(panel).findByRole("table", { name: "Every client" });
    const orgWide = within(every).getByRole("row", { name: /devices\.read/ });
    expect(within(orgWide).getByText("Technician")).toBeInTheDocument();
    const warehouse = within(panel).getByRole("table", { name: "Warehouse" });
    const scoped = within(warehouse).getByRole("row", { name: /devices\.manage/ });
    expect(within(scoped).getByText("Warehouse technician")).toBeInTheDocument();
    expect(within(panel).getAllByRole("table").map((table) => table.getAttribute("aria-label"))).toEqual([
      "Every client",
      "Warehouse",
    ]);
    expect(within(panel).queryByText(/44444444-4444-4444-8444-444444444444/)).not.toBeInTheDocument();
```

  (y el título del caso pasa a «shows effective permissions grouped by client, with the role that granted each»).
- `frontend/src/modules/settings/settings.es.test.tsx:565-569`. Viejo: los tres encabezados con `getByRole` y `getByText("en todas partes")`, `getByText("limitado a Warehouse")`. Nuevo:

```tsx
    for (const header of ["Permiso", "Concedido por"]) {
      expect(within(section).getAllByRole("columnheader", { name: header })).toHaveLength(2);
    }
    expect(within(section).getByRole("table", { name: "Todos los clientes" })).toBeInTheDocument();
    expect(within(section).getByRole("table", { name: "Warehouse" })).toBeInTheDocument();
```

- [ ] **Step 17: Comprobar que fallan**

Run: `npx vitest run src/modules/service-desk/__tests__/ticketQueue.test.tsx src/modules/settings`
Expected: FAIL — `Unable to find role="columnheader" and name "Client"`, `Failed to resolve import "../grantGroups"`, `Unable to find role="table" and name "Every client"`.

- [ ] **Step 18: La columna Cliente en tickets y los permisos por cliente**

`frontend/src/modules/service-desk/TicketTable.tsx`: imports `import { useClientScope } from "../../lib/clientScope";` y `import { useClientName } from "../settings/api";`. Al principio de `TicketTable`, tras `const t = useT();`:

```ts
  // THE CLIENT COLUMN (design §3.4, §4.5): while every client is shown, the
  // same column as every other list; with one client chosen it says nothing.
  const { selected } = useClientScope();
  const clientName = useClientName();
  const showClient = selected === null;
```

En la cabecera, tras `<Th>{t("serviceDesk.table.ticket")}</Th>`: `{showClient ? <Th>{t("serviceDesk.table.client")}</Th> : null}`. En la fila, tras la primera `Td` (la que cierra en `:79`):

```tsx
              {showClient ? <Td>{clientName(ticket.client_id)}</Td> : null}
```

`frontend/src/i18n/en/serviceDesk.ts`, en `table` (`:113-122`), tras `ticket: "Ticket",`: `client: "Client",`. `frontend/src/i18n/es/serviceDesk.ts`, mismo sitio: `client: "Cliente",`.

```ts
// frontend/src/modules/settings/grantGroups.ts
/**
 * A person's effective permissions grouped by client (MSP clients phase 3,
 * design §4.2): the grants that hold on every client first, then one group
 * per client, by name. `GET /users/{id}/effective-permissions` already says
 * which client each grant is for (`client_id`, `null` for every client).
 */
import { intlLocale } from "../../lib/datetime";
import type { CodeGrantOut } from "./types";

export interface GrantGroup {
  /** `null` for the grants that hold on every client. */
  readonly clientId: string | null;
  readonly grants: readonly CodeGrantOut[];
}

export function groupGrantsByClient(
  grants: readonly CodeGrantOut[],
  name: (clientId: string) => string,
): GrantGroup[] {
  const byClient = new Map<string | null, CodeGrantOut[]>();
  for (const grant of grants) {
    const list = byClient.get(grant.client_id) ?? [];
    list.push(grant);
    byClient.set(grant.client_id, list);
  }
  const sorted = (list: readonly CodeGrantOut[]) =>
    [...list].sort((a, b) => a.code.localeCompare(b.code) || a.via_role.localeCompare(b.via_role));
  const clients = [...byClient.keys()]
    .filter((id): id is string => id !== null)
    .sort((a, b) => name(a).localeCompare(name(b), intlLocale()) || a.localeCompare(b));
  const every = byClient.get(null);
  return [
    ...(every ? [{ clientId: null, grants: sorted(every) }] : []),
    ...clients.map((clientId) => ({ clientId, grants: sorted(byClient.get(clientId) ?? []) })),
  ];
}
```

`frontend/src/modules/settings/UsersTab.tsx`: el import de `:74` pasa a `import { Btn, FieldGroup, KIND_ICON, Panel, QueryState } from "../../shell/components";` y se añade `import { groupGrantsByClient } from "./grantGroups";`. Dentro de `QueryState` del panel de permisos efectivos (`:755-773`), la `Table` única se sustituye por:

```tsx
              {groupGrantsByClient(effective.data?.codes ?? [], (id) => clientNames.get(id) ?? id).map((group) => {
                const label =
                  group.clientId === null
                    ? t("settings.users.effectiveEveryClient")
                    : (clientNames.get(group.clientId) ?? group.clientId);
                return (
                  <FieldGroup key={group.clientId ?? "every"} title={label} icon={group.clientId === null ? "world" : "building"}>
                    <Table label={label}>
                      <thead>
                        <tr>
                          <Th>{t("settings.users.effectiveColumns.permission")}</Th>
                          <Th>{t("settings.users.effectiveColumns.grantedBy")}</Th>
                        </tr>
                      </thead>
                      <tbody>
                        {group.grants.map((grant) => (
                          <tr key={`${grant.code}:${grant.via_role}`}>
                            <Td>
                              <span className="mono">{grant.code}</span>
                            </Td>
                            <Td>{roleDisplayName(grant.via_role)}</Td>
                          </tr>
                        ))}
                      </tbody>
                    </Table>
                  </FieldGroup>
                );
              })}
```

`describeGrantScope` sigue usándose en el directorio y en el panel de roles (`:406`, `:587`, `:610`, `:617`): su import se queda.

`frontend/src/i18n/en/settings.ts` (`:250-257`): tras `effectiveEmpty`, `effectiveEveryClient: "Every client",`, y en `effectiveColumns` se borra `appliesTo: "Applies to",`. `frontend/src/i18n/es/settings.ts` (`:243-250`): `effectiveEveryClient: "Todos los clientes",` y se borra `appliesTo: "Se aplica a",`.

El comentario obsoleto de `frontend/src/modules/audit/api.ts:3-6`:

```ts
/**
 * The two audit routes the kernel serves, and nothing else.
 *
 * Both need `audit.read` ON EVERY CLIENT, server-side
 * (`require_permission_on_every_client("audit.read")` in
 * `backend/app/api/v1/audit.py`): the log describes every client, and phase 3
 * decided not to open it per client (design 2026-10-31, Q2). Whatever this
 * console chooses to render, the server decides what it may have -- invariant 6.
 */
```

- [ ] **Step 19: Comprobar**

Run (desde `frontend/`): `npx vitest run src/modules/service-desk src/modules/settings src/modules/audit`
Expected: PASS. Si alguna prueba existente de `src/modules/service-desk` falla con `useClientScope` fuera de su proveedor (la Task 9 pudo resolverlo en ellas para `NewTicketDialog`), añade el bloque `clientScope`/`vi.mock` del Step 16.

Verificación completa. Desde `backend/`: `.venv/bin/pytest -q` (la suite entera), `.venv/bin/mypy app`, `.venv/bin/ruff check app tests`. Desde `frontend/`: `npx vitest run`, `npx tsc -b`, `npm run build`. Expected: todo en verde.

- [ ] **Step 20: Commit**

```bash
git add frontend/src/modules/service-desk frontend/src/modules/settings/grantGroups.ts frontend/src/modules/settings/UsersTab.tsx frontend/src/modules/settings/__tests__ frontend/src/modules/settings/settings.es.test.tsx frontend/src/modules/audit/api.ts frontend/src/i18n/en/serviceDesk.ts frontend/src/i18n/es/serviceDesk.ts frontend/src/i18n/en/settings.ts frontend/src/i18n/es/settings.ts
git commit -m "feat(clients): the Client column on tickets, effective permissions by client, and the audit comment (phase 3 Task 13)"
```

---

### Task 14: Consola de los extras — salud por cliente y retirar un repositorio

Diseño: §5.1 (perfil de salud por defecto de cada cliente, en la consola) y §5.5 (retirar un repositorio, en la consola). El backend es de las Tasks 3 y 6, ya fusionadas. Oleada 4, en paralelo con las Tasks 12 y 13.

**Archivos compartidos que toca esta tarea:**
- `frontend/src/lib/api.ts`: un método tras `createBackupRepository` (`:2882-2887`). Las Tasks 12 y 13 tocan otros bloques.
- `frontend/src/modules/health/types.ts`: **la misma edición, letra por letra, que la Task 12** (Step 1). Git fusiona solo dos cambios idénticos.
- Catálogos: `frontend/src/i18n/{en,es}/health.ts`, `backup.ts`, `enums.ts` (ninguno lo tocan las Tasks 12 ni 13). `auditVocabulary.ts` solo si la Task 6 no añadió su frase (Step 9).

**Files:**
- Modify: `frontend/src/modules/health/types.ts` (`:63`, `:189-205`)
- Modify: `frontend/src/modules/health/ProfileList.tsx:390-415` (`describeAssignments`)
- Modify: `frontend/src/modules/health/healthState.ts` (función nueva `appliedByLabel`)
- Modify: `frontend/src/modules/health/DeviceHealthPanel.tsx:138-148`
- Create: `frontend/src/modules/health/ProfileAssignments.tsx`
- Modify: `frontend/src/modules/health/HealthPage.tsx:252`
- Modify: `frontend/src/lib/api.ts` (tras `:2887`)
- Modify: `frontend/src/modules/backup/api.ts` (hook nuevo tras `:76`)
- Modify: `frontend/src/modules/backup/types.ts:10`, `:19`
- Modify: `frontend/src/modules/backup/backupState.ts:56-79`
- Modify: `frontend/src/modules/backup/pickerOptions.ts` (`:100-120`, `:166-172`)
- Modify: `frontend/src/modules/backup/RepositoriesPage.tsx` (`:18-44`, `:56-73`, `:114-121`, `:136-140`, `:154-185`)
- Modify: `frontend/src/modules/backup/fixtures.ts:67`
- Modify: `frontend/src/i18n/{en,es}/health.ts`, `backup.ts`, `enums.ts`
- Test: `frontend/src/modules/health/__tests__/assignments.test.tsx`, `frontend/src/modules/backup/__tests__/retire.test.tsx`, y las correcciones de `frontend/src/modules/backup/__tests__/backup.test.tsx` y `frontend/src/modules/backup/backup.es.test.tsx` (Step 7)

**Interfaces:**
- Consumes (Task 3, backend): `POST /api/v1/health/assignments` con `client_id` (sin equipo ni grupo = defecto del cliente; `health.admin` en ese cliente; «Sin asignar» 422, archivado 409, duplicado 409); `AssignmentOut.client_id`, `level = "client"`; `DeviceHealthOut.resolution_level = "client"`.
- Consumes (Task 6, backend): `POST /api/v1/backup/repositories/{repository_id}/retire` → `RepositoryOut` (`backup.policy.manage` en el cliente del repositorio; 409 con una política activa que lo nombre); estados `"active"` y `"retired"`; auditoría `backup.repository_retired`.
- Consumes (Task 7): `useClientScope()`, `Me.clients`. (Task 8): `useEverywherePermissions()`, `useClientName()`. (Task 9): `useNewRowClient(code)`, `ClientField({ code, value, onChange })`.
- Produces, **fuera de 01-interfaces**:
  - `ApiClient.retireBackupRepository(repositoryId: string): Promise<Repository>`.
  - `modules/backup/api.ts::useRetireBackupRepositoryMutation()`.
  - `modules/health/ProfileAssignments.tsx`: `ProfileAssignments({ profileId, canAdmin })`, `describeAssignmentTarget(assignment, clientName)`, `HEALTH_ADMIN`.
  - `modules/health/healthState.ts::appliedByLabel(level, assignment, clientName)`.
  - `RepositoryState` pasa de `"active" | "disabled"` a `"active" | "retired"`.

> **Decisión del plan (no hay pantalla de asignación).** La consola no tiene hoy ninguna pantalla que cree asignaciones de perfil: `useCreateHealthAssignmentMutation` (`health/api.ts:227`) no tiene llamador, y se asigna por la API o MCP. Para que «la asignación de perfiles ofrezca "Por defecto del cliente"» (§5.1) esta tarea añade, bajo el editor del perfil elegido, un panel «Dónde se aplica este perfil»: lista todas sus asignaciones, quita las que la persona puede quitar y asigna el perfil como **defecto de un cliente** o **de la organización**. Las asignaciones a un equipo o a un grupo siguen fuera de la consola, como hasta ahora.
>
> **Decisión del plan (el defecto de la organización).** Se ofrece solo con `health.admin` en `permissions_everywhere` y con «Todos mis clientes» arriba: es una fila global (`client_id` nulo), que solo se escribe con el alcance de todos los clientes, y con `X-Client-Id` la petición iría estrechada.
>
> **Decisión del plan (la etiqueta en español).** La opción se llama «Por defecto del cliente», con las palabras del diseño; su pareja, «Por defecto de la organización». Los textos ya existentes («predeterminado de la organización») no se tocan.
>
> **Decisión del plan (la columna de estado).** `RepositoriesPage` ya tiene columna Estado (`:158`, `:169-175`); lo que cambia es su vocabulario: `disabled`, que nadie escribe, sale, y entra `retired`. El tercer mosaico y su filtro pasan de «Deshabilitados» a «Retirados».

- [ ] **Step 1: La edición compartida de `health/types.ts` (idéntica en la Task 12)**

`frontend/src/modules/health/types.ts:63`:

```ts
export type ResolutionLevel = "device" | "group" | "client" | "org";
```

`:189-205`:

```ts
export interface AssignmentCreate {
  profile_id: string;
  device_id?: string | null;
  device_group_id?: string | null;
  /** A client's default (MSP clients phase 3, design §5.1): with no device and no group. */
  client_id?: string | null;
}

export interface AssignmentOut {
  id: string;
  profile_id: string;
  profile_name: string;
  device_id: string | null;
  device_group_id: string | null;
  /**
   * The client the assignment is for: the client of a client default, the
   * device's or the group's otherwise, and `null` on the organisation default
   * (MSP clients phase 3, design §5.1). OPTIONAL because additive: an API
   * from before phase 3 does not send it.
   */
  client_id?: string | null;
  // Spec §0.2's resolution level, decided server-side. The console never infers
  // it from which id happens to be null.
  level: ResolutionLevel;
  created_by: string | null;
  created_at: string;
}
```

- [ ] **Step 2: Las pruebas que fallan — salud**

```tsx
// frontend/src/modules/health/__tests__/assignments.test.tsx
/**
 * Where a profile applies, and the console's one way to apply it: as a
 * client's default or as the organisation's (MSP clients phase 3, design
 * §5.1). `useNewRowClient` and `ClientField` are Task 9's and are replaced
 * here: what is pinned is the body this panel sends for each answer they give.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../../i18n/core";
import { ApiError } from "../../../lib/api";
import type { ApiClient, Me, MeClient } from "../../../lib/api";
import { appliedByLabel } from "../healthState";
import { ProfileAssignments, describeAssignmentTarget } from "../ProfileAssignments";
import { describeAssignments } from "../ProfileList";
import type { AssignmentOut } from "../types";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const state = vi.hoisted(() => ({
  selected: null as string | null,
  clients: [] as MeClient[],
  newRow: { clientId: null as string | null, needsField: true, options: [] as MeClient[] },
}));
vi.mock("../../../lib/clientScope", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/clientScope")>()),
  useClientScope: () => ({ selected: state.selected, select: vi.fn(), clients: state.clients, ready: true }),
}));
vi.mock("../../../lib/clientSelection", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/clientSelection")>()),
  useNewRowClient: () => state.newRow,
}));
vi.mock("../../../components/ClientField", () => ({
  ClientField: ({ onChange }: { onChange: (id: string | null) => void }) => (
    <select aria-label="Client" defaultValue="" onChange={(event) => onChange(event.target.value || null)}>
      <option value="" />
      <option value="c-andes">Andes Logistics</option>
    </select>
  ),
}));

const ANDES: MeClient = {
  id: "c-andes",
  name: "Andes Logistics",
  kind: "customer",
  enabled_modules: [],
  permissions: ["health.read", "health.admin"],
};
const ORG_DEFAULT: AssignmentOut = {
  id: "ha-org",
  profile_id: "hp-1",
  profile_name: "Baseline",
  device_id: null,
  device_group_id: null,
  client_id: null,
  level: "org",
  created_by: null,
  created_at: "2026-10-01T00:00:00Z",
};
const ANDES_DEFAULT: AssignmentOut = { ...ORG_DEFAULT, id: "ha-andes", client_id: "c-andes", level: "client" };

function me(everywhere: string[]): Me {
  return {
    user_id: "u1",
    org_id: "org-1",
    email: "op@example.com",
    display_name: "Operator",
    permissions: ["health.read", "health.admin"],
    permissions_everywhere: everywhere,
    clients: [ANDES],
  };
}

function renderPanel(api: Partial<ApiClient>, everywhere: string[] = []) {
  mockUseApi.mockReturnValue({
    me: vi.fn().mockResolvedValue(me(everywhere)),
    listHealthAssignments: vi.fn().mockResolvedValue([]),
    ...api,
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ProfileAssignments profileId="hp-1" canAdmin />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockUseApi.mockReset();
  state.selected = null;
  state.clients = [ANDES];
  state.newRow = { clientId: null, needsField: true, options: [ANDES] };
});

afterEach(() => setActiveLanguage("en"));

describe("where a profile applies", () => {
  it("lists every assignment of the profile, a client default by its client's name", async () => {
    const listHealthAssignments = vi.fn().mockResolvedValue([ORG_DEFAULT, ANDES_DEFAULT]);
    renderPanel({ listHealthAssignments });
    expect(await screen.findByText("Organisation default")).toBeInTheDocument();
    expect(await screen.findByText("Default of Andes Logistics")).toBeInTheDocument();
    expect(listHealthAssignments).toHaveBeenCalledWith("hp-1");
  });

  it("removes a client default for a person who administers health on that client", async () => {
    const deleteHealthAssignment = vi.fn().mockResolvedValue(undefined);
    renderPanel({
      listHealthAssignments: vi.fn().mockResolvedValue([ORG_DEFAULT, ANDES_DEFAULT]),
      deleteHealthAssignment,
    });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Remove: Default of Andes Logistics" }));
    await user.click(
      within(await screen.findByRole("dialog", { name: "Remove this assignment" })).getByRole("button", {
        name: "Remove",
      }),
    );
    expect(deleteHealthAssignment).toHaveBeenCalledWith("ha-andes");
    // The organisation default is global: not removable without every client.
    expect(screen.queryByRole("button", { name: "Remove: Organisation default" })).toBeNull();
  });
});

describe("assigning a profile as a client's default", () => {
  it("asks for the client with every client shown, and sends it", async () => {
    const createHealthAssignment = vi.fn().mockResolvedValue(ANDES_DEFAULT);
    renderPanel({ createHealthAssignment });
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByRole("combobox", { name: "Client" }), "c-andes");
    await user.click(screen.getByRole("button", { name: "Assign" }));
    await waitFor(() =>
      expect(createHealthAssignment).toHaveBeenCalledWith({ profile_id: "hp-1", client_id: "c-andes" }),
    );
  });

  it("names the missing client instead of sending anything", async () => {
    const createHealthAssignment = vi.fn();
    renderPanel({ createHealthAssignment });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Assign" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Client is required");
    expect(createHealthAssignment).not.toHaveBeenCalled();
  });

  it("uses the client chosen above without asking, and offers no organisation default", async () => {
    state.selected = "c-andes";
    state.newRow = { clientId: "c-andes", needsField: false, options: [] };
    const createHealthAssignment = vi.fn().mockResolvedValue(ANDES_DEFAULT);
    renderPanel({ createHealthAssignment }, ["health.admin"]);
    const user = userEvent.setup();
    expect(await screen.findByText("For Andes Logistics, the client the console is working on.")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Client" })).toBeNull();
    expect(screen.queryByRole("combobox", { name: "Assign as" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Assign" }));
    await waitFor(() =>
      expect(createHealthAssignment).toHaveBeenCalledWith({ profile_id: "hp-1", client_id: "c-andes" }),
    );
  });

  it("explains a refused assignment in the server's words", async () => {
    const createHealthAssignment = vi.fn().mockRejectedValue(
      new ApiError(409, JSON.stringify({ detail: "this client already has a default health profile" })),
    );
    renderPanel({ createHealthAssignment });
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByRole("combobox", { name: "Client" }), "c-andes");
    await user.click(screen.getByRole("button", { name: "Assign" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The profile was not assigned. this client already has a default health profile",
    );
  });
});

describe("assigning a profile as the organisation default", () => {
  it("is offered only with health.admin on every client, and sends no client", async () => {
    const createHealthAssignment = vi.fn().mockResolvedValue(ORG_DEFAULT);
    renderPanel({ createHealthAssignment }, ["health.admin"]);
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByRole("combobox", { name: "Assign as" }), "org");
    expect(screen.queryByRole("combobox", { name: "Client" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Assign" }));
    await waitFor(() => expect(createHealthAssignment).toHaveBeenCalledWith({ profile_id: "hp-1" }));
  });

  it("is not offered without every client", async () => {
    renderPanel({});
    await screen.findByRole("combobox", { name: "Client" });
    expect(screen.queryByRole("combobox", { name: "Assign as" })).toBeNull();
  });
});

describe("the words for a client default", () => {
  const name = (id: string) => (id === "c-andes" ? "Andes Logistics" : id);

  it("counts client defaults among a profile's assignments", () => {
    expect(
      describeAssignments({ id: "p" }, [
        { profile_id: "p", level: "org" },
        { profile_id: "p", level: "client" },
        { profile_id: "p", level: "client" },
        { profile_id: "p", level: "group" },
      ]),
    ).toBe("org default, default of 2 clients, 1 group");
  });

  it("says a device's profile came from its client's default", () => {
    expect(appliedByLabel("client", { client_id: "c-andes" }, name)).toBe("its client's default (Andes Logistics)");
    expect(appliedByLabel("group", { device_group_id: "g1" }, name)).toBe("a group assignment (g1)");
    expect(appliedByLabel("org", null, name)).toBe("the organisation default assignment");
    expect(appliedByLabel("device", null, name)).toBe("an assignment on this device");
  });

  it("says it in Spanish", () => {
    setActiveLanguage("es");
    expect(describeAssignmentTarget(ANDES_DEFAULT, name)).toBe("Por defecto de Andes Logistics");
    expect(appliedByLabel("client", { client_id: "c-andes" }, name)).toBe(
      "el perfil por defecto de su cliente (Andes Logistics)",
    );
    expect(describeAssignments({ id: "hp-1" }, [ANDES_DEFAULT])).toBe("por defecto de 1 cliente");
  });
});
```

- [ ] **Step 3: Comprobar que fallan**

Run (desde `frontend/`): `npx vitest run src/modules/health/__tests__/assignments.test.tsx`
Expected: FAIL — `Failed to resolve import "../ProfileAssignments"` (y `appliedByLabel` no existe).

- [ ] **Step 4: Salud por cliente**

`frontend/src/modules/health/ProfileList.tsx`, `describeAssignments` (`:390-415`):

```ts
/**
 * "org default", "default of 2 clients", "2 groups", "3 devices" — the
 * resolution levels this profile is attached at (spec §0.2; the client level
 * since MSP clients phase 3, design §5.1), rather than a bare count.
 *
 * The level comes from the SERVER (`AssignmentOut.level`); this only groups.
 */
export function describeAssignments(
  profile: Pick<ProfileOut, "id">,
  assignments: ReadonlyArray<{ profile_id: string; level: string }>,
): string {
  const mine = assignments.filter((a) => a.profile_id === profile.id);
  if (mine.length === 0) return translate("health.profiles.unassigned");
  let device = 0;
  let group = 0;
  let client = 0;
  let org = 0;
  for (const assignment of mine) {
    if (assignment.level === "device") device += 1;
    else if (assignment.level === "group") group += 1;
    else if (assignment.level === "client") client += 1;
    else org += 1;
  }
  const parts: string[] = [];
  if (org > 0) parts.push(translate("health.profiles.orgDefault"));
  if (client > 0) parts.push(translate("health.profiles.clientDefaults", { count: client }));
  if (group > 0) parts.push(translate("health.profiles.groups", { count: group }));
  if (device > 0) parts.push(translate("health.profiles.devices", { count: device }));
  return parts.join(", ");
}
```

`frontend/src/modules/health/healthState.ts`, al final (ya importa `translate`, `:29`); añade `ResolutionLevel` a su import de tipos de `./types`:

```ts
/**
 * "Applied by", in full (spec §0.2): the level AND what it names. A client
 * default (MSP clients phase 3, design §5.1) names its client.
 */
export function appliedByLabel(
  level: ResolutionLevel | null,
  assignment: { device_group_id?: string | null; client_id?: string | null } | null,
  clientName: (clientId: string) => string,
): string {
  switch (level) {
    case "org":
      return translate("health.devicePanel.appliedOrg");
    case "group":
      return translate("health.devicePanel.appliedGroup", { group: String(assignment?.device_group_id) });
    case "client":
      return translate("health.devicePanel.appliedClient", { client: clientName(assignment?.client_id ?? "") });
    default:
      return translate("health.devicePanel.appliedDevice");
  }
}
```

`frontend/src/modules/health/DeviceHealthPanel.tsx:138-148`: el `v:` de «Applied by» pasa a `v: appliedByLabel(device.resolution_level, device.assignment, clientName),`, con `const clientName = useClientName();` al principio del componente (`import { useClientName } from "../settings/api";`) y `appliedByLabel` en el import de `./healthState`.

```tsx
// frontend/src/modules/health/ProfileAssignments.tsx
/**
 * Where one health profile applies, and the console's way to apply it: as a
 * client's default or as the organisation's (MSP clients phase 3, design
 * §5.1; spec §0.2's levels: device, then group, then client, then
 * organisation).
 *
 * A CLIENT DEFAULT needs `health.admin` IN THAT CLIENT. The client is the one
 * chosen above, or, with «Todos mis clientes», the one chosen here from the
 * clients where the person holds the code (`useNewRowClient`, design §3.1).
 * «Sin asignar» is never offered, and the server refuses it (422).
 *
 * THE ORGANISATION DEFAULT is a global row: `health.admin` on every client,
 * and no client chosen above, or the request would be narrowed.
 *
 * Device and group assignments are not made here; they stay with the API and
 * MCP, as before this phase. Every assignment is listed, and removed where the
 * person may. Every check is the server's (invariant 6).
 */
import { useState } from "react";

import { ClientField } from "../../components/ClientField";
import { ConfirmDialog } from "../../components/confirm";
import { Form, FormActions, attemptProblems, describeSubmitError } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Field, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { useClientScope } from "../../lib/clientScope";
import { useNewRowClient } from "../../lib/clientSelection";
import { useEverywherePermissions } from "../../lib/mePermissions";
import { Btn, Panel, QueryState } from "../../shell/components";
import { useClientName } from "../settings/api";
import { useCreateHealthAssignmentMutation, useDeleteHealthAssignmentMutation, useHealthAssignments } from "./api";
import type { AssignmentOut } from "./types";

export const HEALTH_ADMIN = "health.admin";

type Target = "client" | "org";

const clientRules = (): readonly FieldRule[] => [
  { name: "client", label: translate("health.assignments.client") },
];

/** Where an assignment applies, in words. */
export function describeAssignmentTarget(
  assignment: Pick<AssignmentOut, "level" | "client_id" | "device_id" | "device_group_id">,
  clientName: (clientId: string) => string,
): string {
  switch (assignment.level) {
    case "org":
      return translate("health.assignments.levelOrg");
    case "client":
      return translate("health.assignments.levelClient", { client: clientName(assignment.client_id ?? "") });
    case "group":
      return translate("health.assignments.levelGroup", { group: assignment.device_group_id ?? "" });
    case "device":
      return translate("health.assignments.levelDevice", { device: assignment.device_id ?? "" });
  }
}

export function ProfileAssignments({ profileId, canAdmin }: { profileId: string; canAdmin: boolean }) {
  const t = useT();
  const assignments = useHealthAssignments(profileId);
  const create = useCreateHealthAssignmentMutation();
  const remove = useDeleteHealthAssignmentMutation();
  const everywhere = useEverywherePermissions();
  const { selected, clients } = useClientScope();
  const clientName = useClientName();
  const newRow = useNewRowClient(HEALTH_ADMIN);
  const orgAllowed = selected === null && everywhere.has(HEALTH_ADMIN);
  const clientAllowed = newRow.clientId !== null || newRow.options.length > 0;
  const [target, setTarget] = useState<Target>("client");
  const [chosen, setChosen] = useState<string | null>(null);
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [removing, setRemoving] = useState<AssignmentOut | null>(null);
  const effective: Target = clientAllowed ? target : "org";
  const client = newRow.clientId ?? chosen;
  const rows = assignments.data ?? [];

  const mayRemove = (assignment: AssignmentOut): boolean =>
    assignment.level === "org"
      ? orgAllowed
      : clients.some((item) => item.id === assignment.client_id && item.permissions.includes(HEALTH_ADMIN));

  function submit() {
    create.reset();
    if (effective === "org") {
      setAttempt(null);
      create.mutate({ profile_id: profileId });
      return;
    }
    const next: Attempt = { rules: clientRules, values: { client: client ?? "" } };
    setAttempt(next);
    if (attemptProblems(next).length > 0 || client === null) return;
    create.mutate({ profile_id: profileId, client_id: client });
  }

  return (
    <Panel title={t("health.assignments.title")} subtitle={t("health.assignments.subtitle")}>
      <QueryState
        loading={assignments.isLoading}
        error={assignments.error}
        empty={rows.length === 0}
        emptyLabel={t("health.assignments.empty")}
        emptyIcon="health"
      >
        <Table label={t("health.assignments.tableLabel")}>
          <thead>
            <tr>
              <Th>{t("health.assignments.colTarget")}</Th>
              <Th />
            </tr>
          </thead>
          <tbody>
            {rows.map((assignment) => {
              const where = describeAssignmentTarget(assignment, clientName);
              return (
                <tr key={assignment.id}>
                  <Td>{where}</Td>
                  <Td>
                    {canAdmin && mayRemove(assignment) ? (
                      <Btn
                        small
                        variant="danger"
                        label={t("health.assignments.remove")}
                        ariaLabel={t("health.assignments.removeAria", { target: where })}
                        onClick={() => {
                          remove.reset();
                          setRemoving(assignment);
                        }}
                      />
                    ) : null}
                  </Td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      </QueryState>
      {remove.isError ? (
        <p role="alert" className="form-error">
          {t("health.assignments.removeFailed")} {describeSubmitError(remove.error)}
        </p>
      ) : null}

      {canAdmin && (clientAllowed || orgAllowed) ? (
        <Form onSubmit={submit}>
          <div className="form-grid">
            {clientAllowed && orgAllowed ? (
              <Field label={t("health.assignments.target")}>
                <select
                  className="select-field"
                  value={target}
                  onChange={(event) => setTarget(event.target.value === "org" ? "org" : "client")}
                >
                  <option value="client">{t("health.assignments.targetClient")}</option>
                  <option value="org">{t("health.assignments.targetOrg")}</option>
                </select>
              </Field>
            ) : null}
            {effective === "client" ? (
              newRow.needsField ? (
                <ClientField code={HEALTH_ADMIN} value={chosen} onChange={setChosen} />
              ) : (
                <p className="note">
                  {t("health.assignments.forClient", { client: clientName(newRow.clientId ?? "") })}
                </p>
              )
            ) : null}
          </div>
          <FormActions
            label={t("health.assignments.assign")}
            pendingLabel={t("health.assignments.assigning")}
            pending={create.isPending}
            error={create.error}
            failedMessage={t("health.assignments.assignFailed")}
            problems={attemptProblems(attempt)}
          />
        </Form>
      ) : null}

      <ConfirmDialog
        open={removing !== null}
        title={t("health.assignments.removeTitle")}
        subject={removing ? describeAssignmentTarget(removing, clientName) : ""}
        body={t("health.assignments.removeBody")}
        confirmLabel={t("health.assignments.removeConfirm")}
        destructive
        onCancel={() => setRemoving(null)}
        onConfirm={() => {
          const assignment = removing;
          setRemoving(null);
          if (assignment) remove.mutate(assignment.id);
        }}
      />
    </Panel>
  );
}
```

`frontend/src/modules/health/HealthPage.tsx`: `import { ProfileAssignments } from "./ProfileAssignments";` y, tras `{selectedProfileId && <ProfileEditor … />}` (`:252`):

```tsx
        {selectedProfileId && <ProfileAssignments profileId={selectedProfileId} canAdmin={canAdmin} />}
```

Catálogos. `frontend/src/i18n/en/health.ts`: en `devicePanel`, tras `appliedDevice` (`:370`), `appliedClient: "its client's default ({client})",`; en `profiles`, tras `orgDefault` (`:530`), `clientDefaults: { one: "default of {count} client", other: "default of {count} clients" },`; antes de `editor: {` (`:534`):

```ts
  // Where a profile applies, and assigning it as a client's or the
  // organisation's default (MSP clients phase 3, design §5.1).
  assignments: {
    title: "Where this profile applies",
    subtitle:
      "A device takes its own profile; failing that, its group's; then its client's default; then the organisation default.",
    empty: "This profile applies to nothing yet.",
    tableLabel: "Assignments of this profile",
    colTarget: "Applies to",
    levelOrg: "Organisation default",
    levelClient: "Default of {client}",
    levelGroup: "Device group {group}",
    levelDevice: "Device {device}",
    remove: "Remove",
    removeAria: "Remove: {target}",
    removeTitle: "Remove this assignment",
    removeBody: "The devices it covered fall through to the next profile that resolves to them, or to none.",
    removeConfirm: "Remove",
    removeFailed: "The assignment was not removed.",
    target: "Assign as",
    targetClient: "Client default",
    targetOrg: "Organisation default",
    client: "Client",
    forClient: "For {client}, the client the console is working on.",
    assign: "Assign",
    assigning: "Assigning…",
    assignFailed: "The profile was not assigned.",
  },
```

`frontend/src/i18n/es/health.ts`: tras `appliedDevice` (`:373`), `appliedClient: "el perfil por defecto de su cliente ({client})",`; tras `orgDefault` (`:538`), `clientDefaults: { one: "por defecto de {count} cliente", other: "por defecto de {count} clientes" },`; antes de `editor: {` (`:542`):

```ts
  assignments: {
    title: "Dónde se aplica este perfil",
    subtitle:
      "Un dispositivo toma su propio perfil; si no tiene, el de su grupo; después, el perfil por defecto de su cliente, y por último el de la organización.",
    empty: "Este perfil todavía no se aplica a nada.",
    tableLabel: "Asignaciones de este perfil",
    colTarget: "Se aplica a",
    levelOrg: "Por defecto de la organización",
    levelClient: "Por defecto de {client}",
    levelGroup: "Grupo de dispositivos {group}",
    levelDevice: "Dispositivo {device}",
    remove: "Quitar",
    removeAria: "Quitar: {target}",
    removeTitle: "Quitar esta asignación",
    removeBody: "Los dispositivos que cubría pasan al siguiente perfil que se les aplique, o a ninguno.",
    removeConfirm: "Quitar",
    removeFailed: "No se quitó la asignación.",
    target: "Asignar como",
    targetClient: "Por defecto del cliente",
    targetOrg: "Por defecto de la organización",
    client: "Cliente",
    forClient: "Para {client}, el cliente con el que trabaja la consola.",
    assign: "Asignar",
    assigning: "Asignando…",
    assignFailed: "No se asignó el perfil.",
  },
```

Las pruebas que montan `HealthPage` y eligen un perfil (`frontend/src/modules/health/__tests__/health.test.tsx`, `frontend/src/modules/health/health.es.test.tsx`) ven ahora el panel nuevo. Añade a las dos, tras su `vi.mock` de `ApiContext`:

```tsx
vi.mock("../../../lib/clientScope", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/clientScope")>()),
  useClientScope: () => ({ selected: null, select: vi.fn(), clients: [], ready: true }),
}));
```

(con `../../lib/clientScope` en `health.es.test.tsx`, que vive un nivel más arriba). Sin clientes en `/me` ni `health.admin` en todos, el panel solo lista; ninguna prueba existente cambia de resultado.

- [ ] **Step 5: Comprobar que pasan**

Run: `npx vitest run src/modules/health src/modules/inventory`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/modules/health frontend/src/i18n/en/health.ts frontend/src/i18n/es/health.ts
git commit -m "feat(health): assign a profile as a client's default or the organisation's, from the console (phase 3 Task 14)"
```

- [ ] **Step 7: Las pruebas que fallan — retirar un repositorio**

```tsx
// frontend/src/modules/backup/__tests__/retire.test.tsx
/**
 * Retiring a backup repository (MSP clients phase 3, design §5.5): offered on
 * an active repository of a client where the person manages backup policies;
 * a refusal (409: an active policy names it) is explained; the retired ones
 * have their own tile; and a policy can no longer be pointed at one.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError } from "../../../lib/api";
import type { Me } from "../../../lib/api";
import { repositoriesOfClient, repositoryPickerOptions } from "../pickerOptions";
import { RepositoriesPage } from "../RepositoriesPage";
import type { Repository } from "../types";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../../lib/clientScope", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/clientScope")>()),
  useClientScope: () => ({ selected: null, select: vi.fn(), clients: [], ready: true }),
}));

const ACTIVE: Repository = {
  id: "repo-1",
  client_id: "c-andes",
  name: "primary-vault",
  storage_ref: "s3://andes/primary",
  state: "active",
  dedup_params: {},
  created_at: "2026-01-01T00:00:00Z",
};
const RETIRED: Repository = { ...ACTIVE, id: "repo-2", name: "old-vault", state: "retired" };
const THEIRS: Repository = { ...ACTIVE, id: "repo-3", client_id: "c-other", name: "their-vault" };

const CODES = ["backup.read", "backup.policy.manage"];
const ME: Me = {
  user_id: "u1",
  org_id: "org-1",
  email: "op@example.com",
  display_name: "Operator",
  permissions: CODES,
  permissions_everywhere: [],
  clients: [{ id: "c-andes", name: "Andes", kind: "customer", enabled_modules: [], permissions: CODES }],
};

function renderPage(api: Partial<ApiClient>, entry = "/backup/repositories") {
  mockUseApi.mockReturnValue({
    me: vi.fn().mockResolvedValue(ME),
    listBackupRepositories: vi.fn().mockResolvedValue([ACTIVE, RETIRED, THEIRS]),
    listRecoveryKeys: vi.fn().mockResolvedValue([]),
    listOffsiteTargets: vi.fn().mockResolvedValue([]),
    ...api,
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <RepositoriesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const rowOf = (name: string) => screen.getByText(name).closest("tr") as HTMLElement;

beforeEach(() => mockUseApi.mockReset());
afterEach(() => vi.unstubAllGlobals());

describe("retiring a repository", () => {
  it("is offered on an active repository of a client where the person manages policies", async () => {
    const retireBackupRepository = vi.fn().mockResolvedValue({ ...ACTIVE, state: "retired" });
    renderPage({ retireBackupRepository });
    const user = userEvent.setup();
    await screen.findByText("primary-vault");
    expect(within(rowOf("old-vault")).getByText("Retired")).toBeInTheDocument();
    expect(within(rowOf("old-vault")).queryByRole("button", { name: /^Retire/ })).toBeNull();
    expect(within(rowOf("their-vault")).queryByRole("button", { name: /^Retire/ })).toBeNull();
    await user.click(within(rowOf("primary-vault")).getByRole("button", { name: "Retire primary-vault" }));
    await user.click(
      within(await screen.findByRole("dialog", { name: "Retire this repository" })).getByRole("button", {
        name: "Retire",
      }),
    );
    expect(retireBackupRepository).toHaveBeenCalledWith("repo-1");
  });

  it("explains a refusal while an active policy names it", async () => {
    const retireBackupRepository = vi.fn().mockRejectedValue(
      new ApiError(409, JSON.stringify({ detail: "an active backup policy still names this repository" })),
    );
    renderPage({ retireBackupRepository });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Retire primary-vault" }));
    await user.click(
      within(await screen.findByRole("dialog", { name: "Retire this repository" })).getByRole("button", {
        name: "Retire",
      }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The repository was not retired. an active backup policy still names this repository",
    );
  });

  it("lists the retired repositories behind their own tile", async () => {
    renderPage({}, "/backup/repositories?state=retired");
    expect(await screen.findByText("old-vault")).toBeInTheDocument();
    expect(screen.queryByText("primary-vault")).toBeNull();
    const hrefs = [...document.querySelectorAll(".stat-card")].map((tile) => tile.getAttribute("href"));
    expect(hrefs).toContain("/backup/repositories?state=retired");
  });

  it("leaves a retired repository out of what a policy may name", () => {
    expect(repositoriesOfClient([ACTIVE, RETIRED, THEIRS], "c-andes").map((repo) => repo.id)).toEqual(["repo-1"]);
    expect(repositoryPickerOptions([RETIRED])[0]?.sublabel).toBe("s3://andes/primary · retired");
  });

  it("sends the retire to its route", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementation(() => Promise.resolve(new Response(JSON.stringify(ACTIVE), { status: 200 })));
    vi.stubGlobal("fetch", fetchMock);
    await new ApiClient(() => "t", vi.fn(), "/api/v1", () => null).retireBackupRepository("repo 1");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect([init.method, url]).toEqual(["POST", "/api/v1/backup/repositories/repo%201/retire"]);
  });
});
```

**Pruebas que fijan el vocabulario viejo y se corrigen** (el diseño §5.5 sustituye `disabled`, que nadie escribe, por `retired`; si la Task 10 movió las líneas, búscalas por su texto):
- `frontend/src/modules/backup/__tests__/backup.test.tsx:1201`: `expect(repositoryStateVariant("disabled")).toBe("muted");` → `expect(repositoryStateVariant("retired")).toBe("muted");`.
- `backup.test.tsx:1936`: `"/backup/repositories?state=disabled",` → `"/backup/repositories?state=retired",`.
- `backup.test.tsx:1943` y `:1945`: `state: "disabled"` → `state: "retired"`, y `"/backup/repositories?state=disabled"` → `"/backup/repositories?state=retired"`.
- `frontend/src/modules/backup/backup.es.test.tsx:130`: `state: "disabled"` → `state: "retired"`.
- `backup.es.test.tsx:723`: `["Todos los repositorios", "Activos", "No activos"]` → `["Todos los repositorios", "Activos", "Retirados"]`.
- `backup.es.test.tsx:732`: `getByText("Deshabilitado")` → `getByText("Retirado")`.
- `backup.es.test.tsx:1178`: `expect(repositoryStateLabel("disabled")).toBe("Deshabilitado");` → `expect(repositoryStateLabel("retired")).toBe("Retirado");`.
- `backup.es.test.tsx:1231-1232`: `"s3://copias/principal · deshabilitado"` → `"s3://copias/principal · retirado"`.
- `backup.es.test.tsx:1237`: `"s3://copias/principal · disabled"` → `"s3://copias/principal · retired"`.

- [ ] **Step 8: Comprobar que fallan**

Run: `npx vitest run src/modules/backup`
Expected: FAIL — `retireBackupRepository is not a function`, `Unable to find role="button" and name "Retire primary-vault"`, y las correcciones del Step 7 (`"Retirado"`, `?state=retired`).

- [ ] **Step 9: Retirar, de punta a punta**

`frontend/src/lib/api.ts`, tras `createBackupRepository` (`:2882-2887`):

```ts
  /**
   * Retire a repository (`POST /backup/repositories/{id}/retire`,
   * `backup.policy.manage` in the repository's client; MSP clients phase 3,
   * design §5.5). Refused (409) while an active policy names it. A retired
   * repository takes no new backups; its points stay and can be restored, so
   * the space stays used. There is no way back to active.
   */
  retireBackupRepository(repositoryId: string): Promise<Repository> {
    return this.request<Repository>(`/backup/repositories/${encodeURIComponent(repositoryId)}/retire`, {
      method: "POST",
    });
  }
```

`frontend/src/modules/backup/api.ts`, tras `useCreateBackupRepositoryMutation` (`:67-76`), con `import type { ApiError } from "../../lib/api";` y `Repository` en el import de tipos de `./types`:

```ts
export function useRetireBackupRepositoryMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<Repository, ApiError, string>({
    mutationFn: (repositoryId) => api.retireBackupRepository(repositoryId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["backup-repositories"] });
    },
  });
}
```

`frontend/src/modules/backup/types.ts`: la línea `:10` del comentario pasa a `//   - repositories (RepositoryOut.state): active | retired`, y `:19`:

```ts
export type RepositoryState = "active" | "retired";
```

`frontend/src/modules/backup/backupState.ts:56-79`:

```ts
// backup_repositories state vocabulary exactly as written by the backend
// (models.py BackupRepository.state default "active"; repository.py sets
// "active" on create; the retire route, MSP clients phase 3 design §5.5, sets
// "retired" -- no new backups, its points still restorable).
const REPOSITORY_STATE_VARIANTS: Record<string, BadgeVariant> = {
  active: "success",
  retired: "muted",
};
```

y `const REPOSITORY_STATES: ReadonlySet<string> = new Set(["active", "retired"]);`.

`frontend/src/modules/backup/pickerOptions.ts`. En `repositoryPickerOptions` (`:100-120`), el comentario «a `disabled` repository is one somebody retired» pasa a «a `retired` repository takes no new backups», y la rama:

```ts
      repository.state === "active"
        ? translate("backup.picker.repoActive")
        : repository.state === "retired"
          ? translate("backup.picker.repoRetired")
          : repository.state,
```

`repositoriesOfClient` (`:166-172`; si la Task 10 la renombró, aplica esto a la función que filtra los repositorios que ofrece el editor de políticas):

```ts
/**
 * The repositories a policy of `client` may name: that client's ACTIVE ones.
 * A retired repository takes no new backups (MSP clients phase 3, design
 * §5.5), so the policy editor never offers it.
 */
export function repositoriesOfClient(
  repositories: readonly Repository[],
  client: string | null,
): Repository[] {
  return client === null
    ? []
    : repositories.filter((repository) => repository.client_id === client && repository.state !== "retired");
}
```

`frontend/src/modules/backup/fixtures.ts:67`: `state: "disabled",` → `state: "retired",` (el «Legacy NAS» queda retirado, nombrado solo por una política deshabilitada, `pol-pilot`: lo que el 409 permite).

`frontend/src/modules/backup/RepositoriesPage.tsx` (sobre lo que dejó la Task 10):
- imports: `import { ConfirmDialog } from "../../components/confirm";`, `describeSubmitError` en el import de `../../components/form`, `useRetireBackupRepositoryMutation` en el de `./api`, y `import type { Repository } from "./types";`.
- el filtro y los recuentos (`:66-73`):

```ts
  const repositories = useBackupRepositories();
  const items = repositories.data ?? [];
  const activeCount = items.filter((repo) => repo.state === "active").length;
  const retiredCount = items.filter((repo) => repo.state === "retired").length;
  const visible =
    stateFilter === "active"
      ? items.filter((repo) => repo.state === "active")
      : stateFilter === "retired"
        ? items.filter((repo) => repo.state === "retired")
        : items;

  // RETIRE (design §5.5): offered on an active repository of a client where
  // the person holds `backup.policy.manage`, from `/me.clients`. A courtesy
  // (invariant 6): the route checks the code, and refuses with a 409 while an
  // active policy names the repository -- explained below the table.
  const retire = useRetireBackupRepositoryMutation();
  const [retiring, setRetiring] = useState<Repository | null>(null);
  const mayRetire = (repo: Repository) =>
    repo.state === "active" &&
    (me.data?.clients ?? []).some(
      (client) => client.id === repo.client_id && client.permissions.includes("backup.policy.manage"),
    );
```

- el tercer mosaico (`:114-121`): `label={t("backup.repositories.tileRetired")}`, `value={String(retiredCount)}`, `to="/backup/repositories?state=retired"` (el resto igual).
- la opción del filtro (`:139`): `<option value="retired">{t("backup.repositories.tileRetired")}</option>`.
- la tabla (`:154-183`): un `<Th />` tras `colRegistered`, y en cada fila, tras la celda de la fecha:

```tsx
                  <Td>
                    {mayRetire(repo) ? (
                      <Btn
                        small
                        variant="danger"
                        label={t("backup.repositories.retire")}
                        ariaLabel={t("backup.repositories.retireAria", { name: repo.name })}
                        onClick={() => {
                          retire.reset();
                          setRetiring(repo);
                        }}
                      />
                    ) : null}
                  </Td>
```

- tras `</QueryState>` (`:184`), dentro del `Panel`:

```tsx
        {retire.isError ? (
          <p role="alert" className="form-error">
            {t("backup.repositories.retireFailed")} {describeSubmitError(retire.error)}
          </p>
        ) : null}
```

- tras `</Panel>` (`:185`):

```tsx
      <ConfirmDialog
        open={retiring !== null}
        title={t("backup.repositories.retireTitle")}
        subject={retiring?.name ?? ""}
        body={t("backup.repositories.retireBody")}
        confirmLabel={t("backup.repositories.retireConfirm")}
        destructive
        onCancel={() => setRetiring(null)}
        onConfirm={() => {
          const repo = retiring;
          setRetiring(null);
          if (repo) retire.mutate(repo.id);
        }}
      />
```

Catálogos. `frontend/src/i18n/en/backup.ts`: `picker.repoDisabled: "disabled",` (`:89`) → `repoRetired: "retired",`; en `repositories` (`:281-303`), `tileDisabled: "Disabled",` → `tileRetired: "Retired",`, se borra `notActive: "Not active",`, y al final del bloque:

```ts
    retire: "Retire",
    retireAria: "Retire {name}",
    retireTitle: "Retire this repository",
    retireBody:
      "It takes no new backups. Its backup points stay and can still be restored, so the space they use stays used. It cannot be made active again.",
    retireConfirm: "Retire",
    retireFailed: "The repository was not retired.",
```

`frontend/src/i18n/es/backup.ts`: `repoDisabled: "deshabilitado",` (`:87`) → `repoRetired: "retirado",`; en `repositories` (`:288-310`), `tileDisabled: "Deshabilitados",` → `tileRetired: "Retirados",`, se borra `notActive: "No activos",`, y al final del bloque:

```ts
    retire: "Retirar",
    retireAria: "Retirar {name}",
    retireTitle: "Retirar este repositorio",
    retireBody:
      "No acepta copias nuevas. Sus puntos de copia se quedan y se pueden seguir restaurando, así que el espacio que ocupan sigue ocupado. No se puede volver a activar.",
    retireConfirm: "Retirar",
    retireFailed: "No se retiró el repositorio.",
```

`frontend/src/i18n/en/enums.ts:593-596` y `frontend/src/i18n/es/enums.ts:563-566`, `RepositoryState`: `disabled: "Disabled",` → `retired: "Retired",` y `disabled: "Deshabilitado",` → `retired: "Retirado",`.

La frase de auditoría. La Task 6 añadió la acción `backup.repository_retired`, y `auditVocabulary.coverage.test.ts` lee el backend:

```bash
grep -n "backup_repository_retired" frontend/src/i18n/en/auditVocabulary.ts frontend/src/i18n/es/auditVocabulary.ts
```

Expected: una línea en cada archivo (la puso la Task 6). Si falta, añádela tras `backup_repository_created` (en `:92`): en `backup_repository_retired: "Backup repository retired",` y en `backup_repository_retired: "Repositorio de copias retirado",`.

- [ ] **Step 10: Comprobar**

Run (desde `frontend/`): `npx vitest run src/modules/backup src/modules/audit src/lib/enums.coverage.test.ts src/i18n`
Expected: PASS.

Verificación completa: `npx vitest run`, `npx tsc -b`, `npm run build`. Expected: todo en verde. Esta tarea no toca el backend.

- [ ] **Step 11: Commit**

```bash
git add frontend/src/lib/api.ts frontend/src/modules/backup frontend/src/i18n/en/backup.ts frontend/src/i18n/es/backup.ts frontend/src/i18n/en/enums.ts frontend/src/i18n/es/enums.ts frontend/src/i18n/en/auditVocabulary.ts frontend/src/i18n/es/auditVocabulary.ts
git commit -m "feat(backup): retire a repository from the console, and keep retired ones out of the policy editor (phase 3 Task 14)"
```

---


#### Addendum del coordinador (Task 14): quién puede poner el perfil por defecto de un cliente

Desde la Task 8, el `canAdmin` de `HealthPage` significa `health.admin` en **todos** los clientes. El perfil por defecto de un cliente es una escritura de **ese** cliente (Task 3: `health.admin` en él), así que no puede depender de `canAdmin`.

- [ ] **Step A1: Prueba que falla**, en la prueba de `ProfileAssignments`. Prepara una persona con `health.admin` solo en el cliente X: `me.clients = [{ id: "c-x", …, permissions: ["health.admin","health.read"] }]` y `permissions_everywhere = []`. Comprueba que:
  - se ofrece la opción «Client default» (en: `health.assignments.targetClient`) y el campo Cliente lista X;
  - **no** se ofrece «Organisation default».
- [ ] **Step A2:** ejecuta la prueba. Resultado esperado: FAIL, porque con `canAdmin=false` no se ofrece nada.
- [ ] **Step A3: Implementación.** En `ProfileAssignments`:
  - `const clientTarget = useNewRowClient("health.admin");`
  - La opción «Por defecto del cliente» se ofrece cuando `clientTarget.options.length > 0`, con independencia de `canAdmin`.
  - «Por defecto de la organización» sigue exigiendo `canAdmin` (todos los clientes) y «Todos mis clientes» arriba.
  - Quitar una asignación de cliente se ofrece si el cliente de la fila está en `clientTarget.options`.
- [ ] **Step A4:** ejecuta la prueba. Resultado esperado: PASS.
- [ ] **Step A5: Commit.** `git commit -am "fix(console): a client's default health profile is offered to who holds health.admin in that client"`

