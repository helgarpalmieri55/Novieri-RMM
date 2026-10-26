# Clientes MSP — Fase 2: los módulos. Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Las 49 tablas de `client_scope_catalog.PENDING_PHASE_2` (tickets,
personas, licencias, suscripciones, despliegues, IPAM, instalación remota,
repositorios de respaldo y los tres módulos opcionales) quedan bajo la misma
regla que las de la fase 1: tienen `client_id`, la RLS lo impone y el código
de cada módulo lo escribe en cada creación. Al terminar, `PENDING_PHASE_2` no
existe y la nota "Alcance de la fase 1" de `CLAUDE.md` 5a se retira.

**Architecture:**
- Una migración por familia (0233 a 0243). Cada una repite el bloque común de
  este plan (§"Bloque común de migración"): columna, relleno, `NOT NULL`,
  trigger de herencia de 0231, propagación a los hijos, política restrictiva y
  clave foránea de organización. Las migraciones no importan código entre sí.
- El cliente de una fila nueva **sale de su padre o de su objetivo** cuando
  lo tiene (equipo, credencial, VRF, plan, presupuesto...). Solo cuando no
  hay nada de donde derivarlo, el cuerpo de la petición trae `client_id`, que
  se valida con `client_for_new_rows`. Un `client_id` del cuerpo que
  contradice el derivado es un 422.
- Cada tarea saca su familia de `PENDING_PHASE_2` en el mismo commit, así que
  las pruebas estructurales de la fase 1 (`test_client_isolation.py::SWEPT`,
  `test_client_catalog.py`) la cubren desde ese momento sin escribir nada
  nuevo para ellas.
- La consola sigue sin selector (fase 3): cada formulario que gana `client_id`
  envía el cliente interno con `internalClientId(me)` y una marca
  `// PHASE 3`, como en la fase 1.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, PostgreSQL 16 (RLS), pytest; React 19 + TypeScript estricto, TanStack Query, vitest + MSW.

**Spec:** `docs/superpowers/specs/2026-10-23-msp-clientes-design.md` (§1.4, §2.2 a §2.5, §3.2, §3.3, §4, §5, §7).
**Plan anterior:** `docs/superpowers/plans/2026-10-23-msp-clientes-fase1-nucleo.md` (fase 1, integrada en `main` con los PR #3, #4 y #5).
**Investigación de esta fase:** `.git/sdd/phase2/research-{1..6}-*.md` (inventario por familia, con `archivo:línea`). Cada tarea cita el informe que la respalda; quien implementa lo lee antes de empezar.

---

## Decisiones

### Del operador (2026-10-26)

| # | Pregunta | Respuesta |
|---|---|---|
| D1 | Correo de un remitente que no es usuario ni contacto | **Sigue en cuarentena** (no crea ticket, email-intake spec §0.2), pero el mensaje queda marcado con el cliente de su dominio, o con "Sin asignar". Un contacto conocido crea el ticket en su cliente. |
| D2 | SLA: un cliente con políticas propias | **Mandan las del cliente.** Si el cliente del ticket tiene alguna política activa propia, se resuelve solo entre las suyas (la más específica); si no tiene ninguna, entre las globales. |
| D3 | Ejecuciones de instalación remota por subred activas a la vez | **Una por cliente** (hasta 50 equipos en curso cada una). |
| D4 | Un cliente sin el módulo de gestión de cambios | **No le aplican** las compuertas globales ni los cambios automáticos de despliegues y parches. |
| A/B | Sacar una credencial global con alcance limitado (pendiente desde la fase 1) | Sin respuesta. Se mantiene **A**: se rechaza (403), como fija `test_checking_out_a_global_credential_on_one_client_is_refused_until_decided`. Ninguna tarea lo cambia. |

### Del controlador, a la vista del código (cada una cita su evidencia)

| # | Decisión | Por qué |
|---|---|---|
| C1 | Un repositorio de respaldo es de **un solo cliente**, y una política solo nombra un repositorio de su cliente. | Su clave maestra viaja en los parámetros firmados de cada equipo que lo usa, y la deduplicación es por repositorio (research-5 §E1). Compartido, un equipo de Y tendría la clave de X: una fuga, no solo un hueco de RLS. Es 5a. |
| C2 | El cliente interno nace con `enabled_modules = {budget, bcp, change}`. | Spec §1.1 y P3. 0230 lo creó con `'{}'` (research-5 §I3): sin esto, la guarda de módulos dejaría a Novieri sin sus propios módulos. |
| C3 | `ipam_site` se borra sin copiar filas y las tres referencias pasan a `site_id` → `sites`. | Spec §1.2, tal cual (P7: no hay datos que conservar). |
| C4 | La persona de un técnico (`users.kind = 'staff'`) es del cliente interno. | Un técnico no tiene cliente (`users.client_id` nulo); la convención de id del spec §7.2. |
| C5 | En el portal, un usuario `staff` (los contactos aún no inician sesión: proyecto 2) abre tickets en el cliente interno. | El portal no tiene selector y `portal_user` no lee `clients` (research-1 §I6-I7). |
| C6 | `audit_events.client_id` entra en el hash **solo cuando no es nulo**; `GET /audit` sigue exigiendo todos los clientes. | Añadirlo siempre rompe la verificación de todos los eventos guardados (research-6 §1). Abrir el registro por cliente es una decisión de producto que nadie ha pedido. |
| C7 | Promover una dirección de IPAM a activo exige que la subred tenga sede; el equipo queda en el cliente y la sede de la subred. | Un cliente no tiene "sede por defecto" marcada (research-3 §I1); inventarla es más diseño del que pide la fase. |
| C8 | El cambio automático de una aprobación de parche **global** va al cliente interno. | La aprobación global no tiene cliente (research-5 §I1); Novieri es quien la decide. |
| C9 | Un ticket con equipo no se puede mover de cliente (409). | El cliente de un equipo es inmutable y el ticket tiene que coincidir con él (spec §2.4). Se quita el equipo y luego se mueve. |
| C10 | El cliente `unassigned` solo recibe tickets abiertos por correo (§3.3, paso 3) y mensajes de correo entrante; ninguna otra familia le crea filas, y un ticket no se mueve **a** él. | Spec §1.1: "Sin asignar" es la bandeja de entrada de lo que no tiene dueño, no un cliente que se gestione. |

---

## Global Constraints

- RLS falla cerrado: sin GUC de alcance, cero filas y ninguna escritura (spec §2.2).
- Las políticas por cliente son `AS RESTRICTIVE` y para todos los roles: se suman a `tenant_isolation` y a `portal_isolation`, que no se tocan.
- `org_session` exige `clients=` sin valor por defecto. `ClientScope.all()` en `app/` solo en los archivos de `ALL_SCOPE_ALLOWLIST` (`app/core/client_scope.py`); una entrada que deja de usarse se borra, porque la prueba de la lista falla con una entrada sin uso.
- **Convención de id:** el cliente `internal` de una organización y su sede "Principal" tienen `id = organizations.id`.
- **Migraciones:** 0233 en adelante, solo hacia adelante. 0230, 0231 y 0232 están en `main` y **no se editan nunca**. Cada `downgrade()` tiene que funcionar, porque la CI corre `upgrade head → downgrade base → upgrade head` y compara el esquema.
- **El cliente de una fila nueva** sale de su padre u objetivo cuando lo tiene; si no, de `client_id` en el cuerpo, cargado con `app/services/client_sites.py::client_for_new_rows` (404 si no se ve, 409 si está archivado). Un `client_id` del cuerpo distinto del derivado: 422. El cliente `unassigned` se rechaza (422) salvo donde C10 dice lo contrario.
- **Un registro de otro cliente es un 404**, nunca un 403 (spec §3.2, "oráculo de existencia").
- **Comprobaciones de permiso sobre un registro:** `has_permission(session, user_id, code, client_id=<cliente del registro>)` (`app/core/rbac.py`), como `jobs/retry.py:260`. Sin `client_id`, `has_permission` cuenta solo las asignaciones de todos los clientes.
- **Claves únicas:** cada clave natural de una tabla que entra en la política es por cliente (`(org_id, client_id, ...)`), o va en `tests/test_client_catalog.py::ORG_WIDE_KEYS` con su razón, o es segura según `leaky_because` (columna de cliente, fk de herencia, sustituta aleatoria en `SURROGATE_COLUMNS`). Cada clave nueva se declara también en el modelo y en `PER_CLIENT_KEYS` de esa prueba.
- **Cada clave foránea `fk_<tabla>_client_org` y `fk_<tabla>_site_client` nueva** se declara en el modelo con `client_org_fk(...)` / `site_client_fk(...)` (`app/models/base.py`).
- **Cada `ON CONFLICT` que nombre una clave que cambia** se actualiza en el mismo commit (un `index_elements` viejo es un 500 en tiempo de ejecución, no un error de importación).
- **Eventos y auditoría:** cada `events.publish(...)` y cada `audit_service.record(...)` cuyo sujeto es una fila de la familia de la tarea pasa `client_id=` (parámetros que crea la Task 1).
- **MCP:** las herramientas salen de las rutas (`app/mcp/catalog.py`); una ruta nueva necesita su entrada en `app/mcp/descriptions/` o `tests/test_mcp_descriptions.py` falla. Una ruta que se borra sale de las exclusiones y descripciones.
- **Textos:** todo texto nuevo para una persona pasa por `tr()` (`app/core/i18n`) y tiene su traducción en `app/core/i18n/es.py`; en la consola, en `frontend/src/i18n/{en,es}/`. Las acciones de auditoría nuevas tienen su frase en `frontend/src/i18n/{en,es}/auditVocabulary.ts` (lo comprueba `auditVocabulary.coverage.test.ts`).
- **Consola:** un cuerpo que gana `client_id` lo envía con `internalClientId(me)` de `frontend/src/lib/internalClient.ts`, con la línea `// PHASE 3` encima; las salidas (`*Out`) ganan `client_id` y su tipo en TypeScript.
- **Ninguna prueba existente se borra, se salta ni se debilita** (`CLAUDE.md`). Las que construyen filas de una familia ganan `client_id=org_id` (el cliente interno) donde la tabla lo exige.
- **Invariantes 1, 2, 2a, 1b y 1c de `CLAUDE.md`:** ninguna tarea cambia los parámetros firmados de un trabajo, ni el formato del sobre, ni lo que un agente ejecuta. Si una tarea parece necesitarlo, se detiene y se pregunta.
- Comandos de verificación, desde `backend/` (Postgres y Redis arriba: `pg_ctlcluster 16 main start; redis-cli ping || redis-server --daemonize yes`):
  - `.venv/bin/pytest -q <archivo>` para las pruebas de la tarea;
  - `.venv/bin/pytest -q` para la suite completa (tarda más de 30 minutos; la tarea no se da por terminada sin ella);
  - `.venv/bin/mypy app`;
  - `.venv/bin/ruff check app tests`.
- Desde `frontend/`: `npx vitest run <ruta>`, `npx tsc -b` y `npm run build`.

## Fuera de esta fase (fase 3)

- El selector de cliente de la consola, la página Clientes, el editor de asignaciones, el menú según los módulos activos, `?client=` en el WebSocket y no enviar `X-Client-Id` en las páginas de configuración global (spec §3.1, §3.4, prueba 14 del §5).
- Un mapa de permisos por cliente en `/me` (`core/identity/own_profile.py:82`, research-6 §3, punto 1).
- Abrir `GET /audit` a quien tiene `audit.read` en algunos clientes (C6).

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| `backend/migrations/versions/0233_phase2_foundations.py` | Módulos del cliente interno, `audit_events.client_id` |
| `backend/migrations/versions/0234_client_scope_tickets.py` | Tickets y sus cinco hijas |
| `backend/migrations/versions/0235_client_scope_people.py` | Personas, sus cuentas y listas |
| `backend/migrations/versions/0236_client_scope_licenses.py` | Licencias y suscripciones |
| `backend/migrations/versions/0237_client_scope_change_bcp.py` | Gestión de cambios y continuidad |
| `backend/migrations/versions/0238_client_scope_budget.py` | Presupuesto |
| `backend/migrations/versions/0239_client_scope_deployments.py` | Despliegues |
| `backend/migrations/versions/0240_client_scope_ipam.py` | IPAM y la fusión de `ipam_site` en `sites` |
| `backend/migrations/versions/0241_client_scope_push.py` | Instalación remota |
| `backend/migrations/versions/0242_client_scope_backup_repositories.py` | Repositorios de respaldo |
| `backend/migrations/versions/0243_client_rows_for_global_tables.py` | `domain_events` vuelve a la política O genérica |
| `backend/app/services/client_modules.py` | `require_module`: un módulo opcional apagado para un cliente es un 404 |
| `backend/app/core/client_scope_catalog.py` | Se vacía y borra `PENDING_PHASE_2`; `PHASE_1` pasa a llamarse `SCOPED` (Task 15) |
| `backend/tests/test_phase2_foundations.py` | Task 1 |
| `backend/tests/test_client_scope_tickets.py` | Tasks 2 y 3 |
| `backend/tests/test_email_intake_clients.py` | Task 4 |
| `backend/tests/test_client_scope_people.py` | Task 5 |
| `backend/tests/test_client_scope_licenses.py` | Task 6 |
| `backend/tests/test_client_modules.py` | Task 7 |
| `backend/tests/test_client_scope_budget.py` | Task 8 |
| `backend/tests/test_client_scope_deployments.py` | Task 9 |
| `backend/tests/test_client_scope_ipam.py` | Tasks 10 y 11 |
| `backend/tests/test_client_scope_push.py` | Task 12 |
| `backend/tests/test_client_scope_backup.py` | Task 13 |
| `backend/tests/test_client_scoped_config.py` | Task 14 |

---

## Bloque común de migración

Cada migración de familia (0234 a 0242) empieza con este bloque, **copiado
igual**. No se importa: una migración publicada no puede cambiar porque
alguien edite un archivo compartido. Las funciones SQL que usa
(`its_client_visible`, `its_client_from_parent`, `its_client_to_children`)
existen desde 0231.

```python
# --- Bloque común de la fase 2 (plan 2026-10-26, "Bloque común de
# migración"). Copiado igual en cada migración de familia: una migración
# publicada no importa nada que pueda cambiar después. -------------------

VISIBLE = "its_client_visible(client_id)"


def _add_c(table: str, backfill: str = "org_id") -> None:
    """Tabla C: la aplicación escribe `client_id`. `backfill` es la expresión
    SQL que da el cliente de cada fila existente; por defecto, el cliente
    interno, cuyo id es el de la organización."""
    op.execute(f"ALTER TABLE {table} ADD COLUMN client_id uuid REFERENCES clients(id)")
    op.execute(f"UPDATE {table} SET client_id = {backfill}")
    op.execute(f"ALTER TABLE {table} ALTER COLUMN client_id SET NOT NULL")
    op.execute(f"CREATE INDEX ix_{table}_client_id ON {table} (client_id)")
    # Una clave foránea ignora la RLS: sin esta, una fila de la organización A
    # podría nombrar un cliente de B bajo el alcance "todos" (0231).
    op.execute(
        f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_client_org "
        "FOREIGN KEY (client_id, org_id) REFERENCES clients (id, org_id)"
    )


def _add_h(table: str, parent: str, fk: str) -> None:
    """Tabla H: el trigger de 0231 copia el cliente del padre, y el padre se
    lo propaga si cambia de cliente (spec §2.4)."""
    op.execute(f"ALTER TABLE {table} ADD COLUMN client_id uuid REFERENCES clients(id)")
    op.execute(
        f"UPDATE {table} t SET client_id = p.client_id FROM {parent} p WHERE p.id = t.{fk}"
    )
    op.execute(f"ALTER TABLE {table} ALTER COLUMN client_id SET NOT NULL")
    op.execute(f"CREATE INDEX ix_{table}_client_id ON {table} (client_id)")
    op.execute(
        f"CREATE TRIGGER its_client_from_parent BEFORE INSERT OR UPDATE OF {fk}, client_id "
        f"ON {table} FOR EACH ROW EXECUTE FUNCTION its_client_from_parent('{parent}', '{fk}')"
    )
    op.execute(
        f"CREATE TRIGGER its_client_to_{table} AFTER UPDATE ON {parent} FOR EACH ROW "
        "WHEN (OLD.client_id IS DISTINCT FROM NEW.client_id) "
        f"EXECUTE FUNCTION its_client_to_children('{table}', '{fk}')"
    )


def _isolate(table: str) -> None:
    op.execute(
        f"CREATE POLICY client_isolation ON {table} AS RESTRICTIVE FOR ALL "
        f"USING ({VISIBLE}) WITH CHECK ({VISIBLE})"
    )


def _drop_c(table: str) -> None:
    op.execute(f"DROP POLICY IF EXISTS client_isolation ON {table}")
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS fk_{table}_client_org")
    op.execute(f"ALTER TABLE {table} DROP COLUMN client_id")


def _drop_h(table: str, parent: str) -> None:
    op.execute(f"DROP TRIGGER IF EXISTS its_client_to_{table} ON {parent}")
    op.execute(f"DROP POLICY IF EXISTS client_isolation ON {table}")
    op.execute(f"DROP TRIGGER IF EXISTS its_client_from_parent ON {table}")
    op.execute(f"ALTER TABLE {table} DROP COLUMN client_id")
```

**Orden dentro de `upgrade()`:** primero los padres C (`_add_c`), luego los
hijos H en orden de profundidad (`_add_h`), luego las claves que cambian y
las claves foráneas compuestas, y al final `_isolate` en cada tabla. En
`downgrade()`, el orden inverso: `_drop_h` de los hijos más profundos a los
menos, `_drop_c` al final, y cada clave a su forma original **antes** de
borrar la columna `client_id` que contiene.

**Nombres de restricciones creadas sin nombre.** Varias claves que hay que
cambiar se crearon con `UNIQUE` en línea, y Postgres eligió el nombre
(`<tabla>_<columnas>_key`). Los informes de investigación los marcan con
"(PG)". Antes de escribir un `DROP CONSTRAINT`, se confirma el nombre en una
base migrada:

```bash
cd backend && .venv/bin/python - <<'PY'
import asyncio, os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
async def main():
    e = create_async_engine(os.environ.get("ITS_ALEMBIC_URL", "postgresql+asyncpg://postgres@localhost/itsuite"))
    async with e.connect() as c:
        for r in await c.execute(text(
            "SELECT conrelid::regclass, conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE contype IN ('u','p') AND conrelid::regclass::text = ANY(:t)"),
            {"t": ["budget", "cost_center"]}):  # las tablas de la tarea
            print(r)
asyncio.run(main())
PY
```

## Convenciones de prueba de esta fase

- **Datos:** `tests/client_scope_support.py::client_with_site(env, org)` crea un cliente y su sede; `everything_on(env, org, client_id)` da las cabeceras de un técnico con **todos** los permisos en **un** cliente. Con eso, cada prueba de API tiene un técnico limitado a X y un cliente Y del que no debe ver nada.
- **Recorrido del catálogo:** `tests/test_client_isolation.py::PATHS` gana una entrada por tabla C y por camino H de la familia (la clave sigue el formato `"C-<tabla>"` / `"H-<tabla>-via-<padre>"`). `test_another_clients_row_is_invisible_and_untouchable` hace el resto. Si la tabla nueva necesita un padre que `Side` no tiene, `Side` y `_side` lo ganan.
- **La prueba 4 del spec §5** (el filtro del código sin RLS): cada servicio que filtra por cliente a mano (SLA, barrido de IPAM, ritmo de push, despacho de respaldos) tiene una prueba que lo ejecuta con `ClientScope.all()` o con el motor de administración, y con dos clientes que solo se distinguen por el cliente.

---

### Task 1: Cimientos — módulos del cliente interno, cliente en la auditoría y en los eventos

Investigación: research-5 §I3 (módulos), research-6 §1 (auditoría) y §2 (eventos).

**Files:**
- Create: `backend/migrations/versions/0233_phase2_foundations.py`
- Modify: `backend/app/models/core.py` (`AuditEvent.client_id` y su `client_org_fk("audit_events")`)
- Modify: `backend/app/core/audit.py` (`_canonical_fields`, `AuditEntry`, `record`, `record_many`, `verify`)
- Modify: `backend/app/core/events.py` (`publish` gana `client_id`, palabra clave **obligatoria**)
- Modify: las 17 llamadas a `events.publish` (tabla de research-6 §2)
- Modify: `backend/tests/test_client_catalog.py` (conjunto esperado de claves foráneas de cliente)
- Test: `backend/tests/test_phase2_foundations.py`

**Interfaces:**
- Produces: `AuditService.record(..., client_id: UUID | None = None)`; `AuditEntry.client_id: UUID | None = None`; `AuditEvent.client_id: Mapped[UUID | None]`.
- Produces: `events.publish(session, *, org_id, event_type, subject_type, subject_id, payload, actor_user_id, client_id: UUID | None)` — sin valor por defecto: cada llamada decide.
- Produces: el cliente `internal` de cada organización, existente o nueva, tiene `enabled_modules = {budget, bcp, change}` (C2). Las Tasks 7 y 8 dependen de ello.

- [ ] **Step 1: Escribir las pruebas que fallan**

```python
# backend/tests/test_phase2_foundations.py
"""Phase-2 foundations (plan 2026-10-26, Task 1): the internal client is born
with the three optional modules (C2), an audit event can name its subject's
client without breaking the chain of the events that do not (C6), and a
domain event carries its subject's client."""

import subprocess
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import AuditEntry, AuditService
from app.core.client_scope import ClientScope
from app.core.events import publish
from app.core.tenancy import org_session
from app.models import Client, DomainEvent, Organization
from tests.conftest import PgCluster


async def _org(admin_engine: AsyncEngine) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="F2", slug=f"f2-{uuid4().hex[:8]}")
        s.add(org)
        await s.commit()
        return org.id


async def _customer(admin_engine: AsyncEngine, org: UUID) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        client = Client(org_id=org, name=f"C {uuid4().hex[:6]}", kind="customer")
        s.add(client)
        await s.commit()
        return client.id


async def test_a_new_org_s_internal_client_has_the_three_optional_modules(
    admin_engine: AsyncEngine,
) -> None:
    org = await _org(admin_engine)
    async with AsyncSession(admin_engine) as s:
        rows = dict(
            (
                await s.execute(
                    select(Client.kind, Client.enabled_modules).where(Client.org_id == org)
                )
            ).all()
        )
    assert sorted(rows["internal"]) == ["bcp", "budget", "change"]
    assert rows["unassigned"] == []


async def test_a_customer_client_starts_with_no_optional_module(
    admin_engine: AsyncEngine,
) -> None:
    org = await _org(admin_engine)
    customer = await _customer(admin_engine, org)
    async with AsyncSession(admin_engine) as s:
        modules = (
            await s.execute(select(Client.enabled_modules).where(Client.id == customer))
        ).scalar_one()
    assert modules == []


async def test_events_with_and_without_a_client_verify_in_one_chain(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await _org(admin_engine)
    customer = await _customer(admin_engine, org)
    audit = AuditService()
    async with org_session(org, engine=app_engine, clients=ClientScope.all()) as s:
        plain = await audit.record(
            s, org_id=org, actor_type="system", actor_id=None, action="test.plain"
        )
        named = await audit.record(
            s, org_id=org, actor_type="system", actor_id=None, action="test.named",
            client_id=customer,
        )
        await audit.record_many(
            s, org_id=org,
            entries=[AuditEntry(actor_type="system", actor_id=None, action="test.batch",
                                client_id=customer)],
        )
    assert plain.client_id is None and named.client_id == customer
    async with org_session(org, engine=app_engine, clients=ClientScope.all()) as s:
        result = await audit.verify(s, org)
    assert result.ok and result.count == 3


async def test_rewriting_an_event_s_client_breaks_the_chain(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, pg_cluster: PgCluster
) -> None:
    """C6: the client is HASHED when it is set, so moving an event to another
    client is tampering, exactly as rewriting its metadata is
    (`tests/test_audit.py::test_tamper_breaks_verification`)."""
    org = await _org(admin_engine)
    x, y = await _customer(admin_engine, org), await _customer(admin_engine, org)
    audit = AuditService()
    async with org_session(org, engine=app_engine, clients=ClientScope.all()) as s:
        event = await audit.record(
            s, org_id=org, actor_type="system", actor_id=None, action="test.x", client_id=x
        )
    subprocess.run(
        [
            "sudo", "-u", "postgres", "psql", "-d", pg_cluster.dbname, "-c",
            "ALTER TABLE audit_events DISABLE TRIGGER audit_events_immutable;"
            f"UPDATE audit_events SET client_id = '{y}' WHERE id = '{event.id}';"
            "ALTER TABLE audit_events ENABLE TRIGGER audit_events_immutable;",
        ],
        check=True,
        capture_output=True,
    )
    async with org_session(org, engine=app_engine, clients=ClientScope.all()) as s:
        result = await audit.verify(s, org)
    assert not result.ok and result.broken_at == event.id


async def test_an_audit_client_of_another_organisation_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org, other = await _org(admin_engine), await _org(admin_engine)
    foreign = await _customer(admin_engine, other)
    audit = AuditService()
    try:
        async with org_session(org, engine=app_engine, clients=ClientScope.all()) as s:
            await audit.record(
                s, org_id=org, actor_type="system", actor_id=None, action="t",
                client_id=foreign,
            )
    except Exception as exc:  # noqa: BLE001 -- the FK violation, whatever wraps it
        assert "fk_audit_events_client_org" in str(exc)
    else:
        raise AssertionError("an event named another organisation's client")


async def test_publish_stores_the_subject_s_client(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await _org(admin_engine)
    customer = await _customer(admin_engine, org)
    subject = uuid4()
    async with org_session(org, engine=app_engine, clients=ClientScope.all()) as s:
        await publish(
            s, org_id=org, event_type="device.joined_via_link", subject_type="device",
            subject_id=subject, payload={}, actor_user_id=None, client_id=customer,
        )
    async with AsyncSession(admin_engine) as s:
        stored = (
            await s.execute(
                select(DomainEvent.client_id).where(DomainEvent.subject_id == subject)
            )
        ).scalar_one()
    assert stored == customer


async def test_the_audit_column_is_nullable_and_unindexed_by_policy(
    admin_engine: AsyncEngine,
) -> None:
    """C6: informative only -- no client RLS on `audit_events` (spec §2.5)."""
    async with admin_engine.connect() as c:
        policies = set(
            (
                await c.execute(
                    text(
                        "SELECT policyname FROM pg_policies "
                        "WHERE tablename = 'audit_events' AND policyname LIKE 'client%'"
                    )
                )
            ).scalars()
        )
        nullable = (
            await c.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'audit_events' AND column_name = 'client_id'"
                )
            )
        ).scalar_one()
    assert policies == set() and nullable == "YES"
```

- [ ] **Step 2: Comprobar que fallan**

Run: `.venv/bin/pytest -q tests/test_phase2_foundations.py`
Expected: FAIL — `enabled_modules` vacío en el interno, `record() got an unexpected keyword argument 'client_id'`, `publish() got an unexpected keyword argument 'client_id'`.

- [ ] **Step 3: Escribir la migración 0233**

```python
# backend/migrations/versions/0233_phase2_foundations.py
"""Phase-2 foundations (MSP clients phase 2, plan 2026-10-26, Task 1).

1. The internal client holds the three optional modules (spec §1.1, P3):
   0230's `its_org_system_clients` and its backfill created it with
   `enabled_modules = '{}'`, so the module guard of Task 7 would have locked
   Novieri out of its own budget, BCP and change management. The function is
   replaced with one that names them; 0230 is shipped and is never edited.
2. `audit_events.client_id`: nullable and informative (spec §2.5), with no
   client policy. The org-consistency key is the one integrity rule it needs.
   It is never backfilled: the table is append-only (0004).

Revision ID: 0233
Revises: 0232
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0233"
down_revision: str | None = "0232"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INTERNAL_MODULES = "ARRAY['budget', 'bcp', 'change']::varchar(20)[]"


def _system_clients(modules: str) -> str:
    return f"""
        CREATE OR REPLACE FUNCTION its_org_system_clients() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
        BEGIN
            INSERT INTO clients (id, org_id, name, kind, enabled_modules)
                VALUES (NEW.id, NEW.id, NEW.name, 'internal', {modules});
            INSERT INTO clients (id, org_id, name, kind)
                VALUES (gen_random_uuid(), NEW.id, 'Sin asignar', 'unassigned');
            INSERT INTO sites (id, org_id, client_id, name)
                VALUES (NEW.id, NEW.id, NEW.id, 'Principal');
            RETURN NEW;
        END $$
    """


def upgrade() -> None:
    op.execute(_system_clients(INTERNAL_MODULES))
    op.execute(f"UPDATE clients SET enabled_modules = {INTERNAL_MODULES} WHERE kind = 'internal'")
    op.execute("ALTER TABLE audit_events ADD COLUMN client_id uuid")
    op.execute(
        "ALTER TABLE audit_events ADD CONSTRAINT fk_audit_events_client_org "
        "FOREIGN KEY (client_id, org_id) REFERENCES clients (id, org_id)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE audit_events DROP CONSTRAINT fk_audit_events_client_org")
    op.execute("ALTER TABLE audit_events DROP COLUMN client_id")
    op.execute("UPDATE clients SET enabled_modules = '{}' WHERE kind = 'internal'")
    op.execute(_system_clients("'{}'::varchar(20)[]"))
```

- [ ] **Step 4: El modelo y el servicio de auditoría**

En `AuditEvent` (`app/models/core.py:738`):

```python
    # Informative (spec §2.5, plan 2026-10-26 C6): the client of the event's
    # subject, when it has one. No client policy reads it; it is HASHED when
    # set (`app/core/audit.py::_canonical_fields`).
    client_id: Mapped[UUID | None] = mapped_column(nullable=True)
```

y `client_org_fk("audit_events")` en su `__table_args__` (créalo si no
existe, como tupla).

En `app/core/audit.py`:

```python
def _canonical_fields(
    *,
    org_id: UUID,
    ts: datetime,
    actor_type: str,
    actor_id: UUID | None,
    action: str,
    target_type: str,
    target_id: str,
    metadata: dict[str, Any],
    client_id: UUID | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "org_id": str(org_id),
        "ts": rfc3339(ts),
        "actor_type": actor_type,
        "actor_id": str(actor_id) if actor_id else None,
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "metadata": metadata,
    }
    # ONLY WHEN SET (plan 2026-10-26 C6). Every event written before the
    # column existed has NULL here and must hash exactly as it did; a key
    # present with a null value would change every stored link.
    if client_id is not None:
        fields["client_id"] = str(client_id)
    return fields
```

`AuditEntry` gana `client_id: UUID | None = None` (último campo, con valor
por defecto, para que nada que lo construya hoy cambie). `record` gana el
parámetro de palabra clave `client_id: UUID | None = None`, lo pasa a
`_canonical_fields` y a `AuditEvent(..., client_id=client_id)`. `record_many`
hace lo mismo con `entry.client_id` en cada entrada. `verify` pasa
`client_id=event.client_id`. Las tres llamadas a `_canonical_fields` tienen
que quedar en paso: es la única definición del hash.

- [ ] **Step 5: `publish` con cliente obligatorio y sus 17 llamadas**

En `app/core/events.py::publish`, añade `client_id: UUID | None` como último
parámetro de palabra clave, **sin valor por defecto**, y
`client_id=client_id` en el `DomainEvent(...)`. Docstring: "The client of the
subject. None only for an organisation-wide subject (a platform probe), or —
until its family's phase-2 task — a subject whose table has no client yet;
Task 15 returns `domain_events` to the generic O policy, under which a NULL
client is written only under scope all."

Cada llamada (research-6 §2) pasa el cliente así:

| Llamada | `client_id=` |
|---|---|
| `services/device_review.py:294` | `device.client_id` |
| `services/enrollment.py:959` | `device.client_id` |
| `health/alerts.py:436` | el cliente de la alerta **leído de la base**: `(await session.execute(select(HealthAlert.client_id).where(HealthAlert.id == alert.id))).scalar_one()` (H: el atributo caduca tras un UPDATE, research-6 §1) |
| `health/actions.py:688` | igual que la anterior |
| `ipam/handler.py:180` | `await job_client(session, job.id)` (`app/gateway/dispatch.py:134`) |
| `core/health/notify.py:161` | `None` con el comentario `# organisation-wide: a platform probe has no client` |
| `service_desk/tickets.py:534`, `threads.py:223`, `approvals.py:273`, `transitions.py:352,382,415,585`, `sla/breach.py:391` | `None  # plan 2026-10-26 Task 2 sets the ticket's client` |
| `ipam/reconcile.py:838` | `None  # plan 2026-10-26 Task 11 sets the subnet's client` |
| `agent_push/runs.py:851,1032` | `None  # plan 2026-10-26 Task 12 sets the run's client` |
| `subscriptions/alerts.py:92` | `None  # plan 2026-10-26 Task 6 sets the subscription's client` |

Las pruebas que construyen `DomainEvent(` directamente con el motor de
administración no cambian.

- [ ] **Step 6: El catálogo conoce la clave foránea nueva**

En `tests/test_client_catalog.py`, en `test_the_client_constraints_are_declared_on_the_models`, el conjunto `expected` gana la línea:

```python
        # Informative client of an audit event (plan 2026-10-26 Task 1, C6):
        # G table, no client policy, but its client is its own org's.
        | {"fk_audit_events_client_org"}
```

- [ ] **Step 7: Comprobar**

Run: `.venv/bin/pytest -q tests/test_phase2_foundations.py tests/test_audit.py tests/test_audit_batch.py tests/test_client_catalog.py tests/test_events_outbox.py`
Expected: PASS. Después, la suite completa, `mypy` y `ruff`.

- [ ] **Step 8: Commit**

```bash
git add backend/migrations/versions/0233_phase2_foundations.py backend/app backend/tests
git commit -m "feat(clients): internal client holds the optional modules; audit and domain events name a client (phase 2 Task 1)"
```

---

### Task 2: Tickets — esquema y alta en el cliente correcto

Investigación: research-1 (§A, §B, §E1-E4, §H, §I3, §I6-I7, §I10).

**Files:**
- Create: `backend/migrations/versions/0234_client_scope_tickets.py`
- Modify: `backend/app/modules/service_desk/models.py` (`Ticket.client_id` C + `client_org_fk("ticket")`; `client_id` H en `TicketComment`, `TicketAttachment`, `TicketApproval`, `TicketStatusHistory`)
- Modify: `backend/app/modules/sla/models.py` (`TicketSla.client_id` H)
- Modify: `backend/app/modules/service_desk/schema.py` (`TicketCreate.client_id`, `TicketOut.client_id`, filtro `client_id` del listado)
- Modify: `backend/app/modules/service_desk/tickets.py` (`create_ticket`, `update_ticket`), `transitions.py` (`assign_ticket`), `approvals.py` (`_actor_holds_role`), `threads.py`, `api.py`
- Modify: `backend/app/modules/portal/tickets.py` (`create_my_ticket`)
- Modify: `backend/app/modules/health/actions.py` (`_execute_open_ticket`), `backend/app/modules/service_desk/adapters.py:156`, `backend/app/modules/email_intake/intake.py:804`
- Modify: `backend/app/mcp/descriptions/part_a.py` (texto de `service_desk_tickets_create`)
- Modify: `backend/app/core/client_scope_catalog.py` (las seis tablas salen de `PENDING_PHASE_2`)
- Modify: `backend/tests/test_client_catalog.py` (`OTHER_POLICIES`, `ORG_WIDE_KEYS`, `SURROGATE_COLUMNS`), `backend/tests/test_client_isolation.py` (`PATHS`, `World`)
- Modify: las pruebas que construyen `Ticket(`, `TicketCreate(` o llaman `create_ticket(` (lista en research-1 §H): ganan `client_id=org_id`
- Modify: `frontend/src/modules/service-desk/NewTicketDialog.tsx`, `frontend/src/modules/service-desk/types.ts`, `frontend/src/lib/api.ts` (tipos del ticket)
- Test: `backend/tests/test_client_scope_tickets.py`

**Interfaces:**
- Consumes: `events.publish(..., client_id=)` y `audit_service.record(..., client_id=)` (Task 1).
- Produces: `ticket.client_id` C; sus cinco hijas H desde `ticket` por `ticket_id`; trigger `its_ticket_client_guard`.
- Produces: `create_ticket(session, org_id, actor_id, data, *, origin=..., source_alert_id=None, client_id: UUID | None = None) -> Ticket`. `client_id` de la palabra clave lo usan los caminos que no son de un técnico (portal, correo, alerta, cambio); si falta, se usa `data.client_id`.
- Produces: `TicketOut.client_id: UUID`; `GET /service-desk/tickets?client_id=` (filtro opcional).
- Produces: la regla del cliente de un ticket nuevo, que las Tasks 3 y 4 reutilizan: **con equipo**, el del equipo; **sin equipo**, el que se pasa; ninguno de los dos, 422; los dos y distintos, 422.

- [ ] **Step 1: Escribir las pruebas que fallan**

```python
# backend/tests/test_client_scope_tickets.py
"""Tickets belong to a client (MSP clients spec §2.4, §3.2; plan 2026-10-26
Tasks 2 and 3). Two customer clients X and Y in one organisation, and the
client as the ONLY thing that tells their rows apart (spec §5 test 4)."""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session, portal_session
from app.models import Device, Ticket, TicketComment, User
from app.modules.portal.schema import PortalTicketCreate
from app.modules.portal.tickets import create_my_ticket
from tests.client_scope_support import client_with_site, everything_on
from tests.conftest import ApiEnv

API = "/api/v1/service-desk"


async def _user_id(env: ApiEnv, sub: str) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        return (await s.execute(select(User.id).where(User.external_subject == sub))).scalar_one()


async def _desk(env: ApiEnv, org: UUID) -> dict[str, Any]:
    """An every-client admin, a queue and an incident category."""
    sub = await env.create_user(org, "org-admin")
    admin = await env.headers(org, sub)
    queue = await env.client.post(
        f"{API}/queues", json={"key": f"q{uuid4().hex[:6]}", "name": "Helpdesk"}, headers=admin
    )
    category = await env.client.post(
        f"{API}/categories",
        json={"key": f"c{uuid4().hex[:6]}", "name": "HW", "type": "incident",
              "requires_approval": False, "default_queue_id": queue.json()["id"]},
        headers=admin,
    )
    return {
        "admin": admin, "admin_id": await _user_id(env, sub),
        "queue": queue.json()["id"], "category": category.json()["id"],
    }


def _body(desk: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "type": "incident", "category_id": desk["category"], "queue_id": desk["queue"],
        "subject": "printer", "requester_id": str(desk["admin_id"]), **extra,
    }


async def _device(env: ApiEnv, org: UUID, client: UUID, site: UUID) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        d = Device(org_id=org, client_id=client, site_id=site, hostname="pc", os_family="windows")
        s.add(d)
        await s.commit()
        return d.id


async def _contact(env: ApiEnv, org: UUID, client: UUID) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        u = User(org_id=org, external_subject=uuid4().hex, email=f"c-{uuid4().hex[:8]}@x.test",
                 kind="contact", client_id=client)
        s.add(u)
        await s.commit()
        return u.id


async def test_a_ticket_for_a_device_takes_the_device_s_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("t2-dev")
    desk = await _desk(api_env, org)
    x, x_site = await client_with_site(api_env, org)
    device = await _device(api_env, org, x, x_site)
    r = await api_env.client.post(
        f"{API}/tickets", json=_body(desk, device_id=str(device)), headers=desk["admin"]
    )
    assert r.status_code == 201, r.text
    assert r.json()["client_id"] == str(x)


async def test_a_client_that_contradicts_the_device_is_refused(api_env: ApiEnv) -> None:
    org = await api_env.create_org("t2-contra")
    desk = await _desk(api_env, org)
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    device = await _device(api_env, org, x, x_site)
    r = await api_env.client.post(
        f"{API}/tickets", json=_body(desk, device_id=str(device), client_id=str(y)),
        headers=desk["admin"],
    )
    assert r.status_code == 422, r.text


async def test_a_ticket_without_device_needs_a_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("t2-none")
    desk = await _desk(api_env, org)
    r = await api_env.client.post(f"{API}/tickets", json=_body(desk), headers=desk["admin"])
    assert r.status_code == 422, r.text
    r = await api_env.client.post(
        f"{API}/tickets", json=_body(desk, client_id=str(org)), headers=desk["admin"]
    )
    assert r.status_code == 201 and r.json()["client_id"] == str(org)


async def test_the_unassigned_client_takes_no_ticket_from_a_technician(api_env: ApiEnv) -> None:
    """C10: Unassigned is the inbox of what has no owner, not a client a
    technician files work under."""
    org = await api_env.create_org("t2-unassigned")
    desk = await _desk(api_env, org)
    async with AsyncSession(api_env.admin_engine) as s:
        unassigned = (
            await s.execute(
                text("SELECT id FROM clients WHERE org_id = :o AND kind = 'unassigned'"),
                {"o": org},
            )
        ).scalar_one()
    r = await api_env.client.post(
        f"{API}/tickets", json=_body(desk, client_id=str(unassigned)), headers=desk["admin"]
    )
    assert r.status_code == 422, r.text


async def test_a_requester_contact_must_belong_to_the_ticket_s_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("t2-req")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    y_contact = await _contact(api_env, org, y)
    r = await api_env.client.post(
        f"{API}/tickets",
        json=_body(desk, client_id=str(x), requester_id=str(y_contact)),
        headers=desk["admin"],
    )
    assert r.status_code == 422, r.text


async def test_an_assignee_must_see_the_ticket_s_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("t2-assign")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    await everything_on(api_env, org, y)
    async with AsyncSession(api_env.admin_engine) as s:
        y_tech = (
            await s.execute(
                text(
                    "SELECT u.id FROM users u JOIN user_roles r ON r.user_id = u.id "
                    "WHERE r.client_id = :y"
                ),
                {"y": y},
            )
        ).scalar_one()
    r = await api_env.client.post(
        f"{API}/tickets", json=_body(desk, client_id=str(x), assigned_to=str(y_tech)),
        headers=desk["admin"],
    )
    assert r.status_code == 422, r.text


async def test_a_technician_limited_to_x_neither_sees_nor_files_y_tickets(
    api_env: ApiEnv,
) -> None:
    org = await api_env.create_org("t2-narrow")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    y_ticket = (
        await api_env.client.post(
            f"{API}/tickets", json=_body(desk, client_id=str(y)), headers=desk["admin"]
        )
    ).json()["id"]
    x_tech = await everything_on(api_env, org, x)

    listed = await api_env.client.get(f"{API}/tickets", headers=x_tech)
    assert listed.status_code == 200
    assert y_ticket not in {t["id"] for t in listed.json()["items"]}
    assert (await api_env.client.get(f"{API}/tickets/{y_ticket}", headers=x_tech)).status_code == 404
    refused = await api_env.client.post(
        f"{API}/tickets", json=_body(desk, client_id=str(y)), headers=x_tech
    )
    assert refused.status_code == 404, refused.text


async def test_a_comment_inherits_the_ticket_s_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("t2-child")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    ticket = (
        await api_env.client.post(
            f"{API}/tickets", json=_body(desk, client_id=str(x)), headers=desk["admin"]
        )
    ).json()["id"]
    r = await api_env.client.post(
        f"{API}/tickets/{ticket}/comments", json={"body": "hola", "visibility": "public"},
        headers=desk["admin"],
    )
    assert r.status_code == 201, r.text
    async with AsyncSession(api_env.admin_engine) as s:
        clients = set(
            (
                await s.execute(
                    select(TicketComment.client_id).where(TicketComment.ticket_id == UUID(ticket))
                )
            ).scalars()
        )
    assert clients == {x}


async def test_the_database_refuses_a_device_of_another_client(api_env: ApiEnv) -> None:
    """`its_ticket_client_guard`, below the service's own check: under scope
    all nothing else would stop it."""
    org = await api_env.create_org("t2-guard")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    y_device = await _device(api_env, org, y, y_site)
    with pytest.raises(DBAPIError, match="ticket device belongs to another client"):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            s.add(Ticket(
                org_id=org, client_id=x, number=f"T-{uuid4().hex[:8]}", type="incident",
                category_id=UUID(desk["category"]), queue_id=UUID(desk["queue"]), status="new",
                subject="s", requester_id=desk["admin_id"], device_id=y_device,
                origin="technician", created_by=desk["admin_id"],
            ))
            await s.flush()


async def test_a_staff_portal_ticket_lands_in_the_internal_client(
    api_env: ApiEnv, portal_engine
) -> None:
    """C5: contacts do not sign in yet (project 2); a staff member filing a
    ticket of their own through the portal files it under Novieri's own IT."""
    org = await api_env.create_org("t2-portal")
    desk = await _desk(api_env, org)
    async with portal_session(
        org, desk["admin_id"], engine=portal_engine, clients=ClientScope.all()
    ) as s:
        detail = await create_my_ticket(
            s, org_id=org, requester_id=desk["admin_id"],
            data=PortalTicketCreate(category_id=UUID(desk["category"]), subject="mine"),
        )
    async with AsyncSession(api_env.admin_engine) as s:
        client = (await s.execute(select(Ticket.client_id).where(Ticket.id == detail.id))).scalar_one()
    assert client == org
```

- [ ] **Step 2: Comprobar que fallan**

Run: `.venv/bin/pytest -q tests/test_client_scope_tickets.py`
Expected: FAIL (`ticket` no tiene `client_id`; `TicketOut` no tiene `client_id`).

- [ ] **Step 3: La migración 0234**

Empieza con el **Bloque común de migración**, y luego:

```python
"""Tickets belong to a client (MSP clients spec §2.4, §3.2; plan 2026-10-26
Task 2).

`ticket` is C. Its five children take the ticket's client by the 0231
trigger, and follow it when it moves (spec §2.4: tickets DO change client,
the normal way out of "Sin asignar"; the move route is Task 3).

Existing tickets go to their device's client, else to their requester's when
the requester is a contact, else to the organisation's internal client: 0231
filed every phase-1 row under the internal client, but devices enrolled since
live in customer clients, and `its_ticket_client_guard` below holds a ticket
to its device's client.

`its_ticket_client_guard` (SECURITY INVOKER, like the 0231 triggers): the
device and the requester are read under the session's own RLS. A device the
session cannot see reads as NULL and is refused -- a technician limited to X
cannot hang Y's device on a ticket even by naming its id. A requester that is
a CONTACT must be of the ticket's client: otherwise that contact's email
reply (`intake.py`, scope all) would comment on another client's ticket
(research-1 §E2). `portal_user` cannot read `devices` (0028); a portal ticket
carries no device, so the guard reads `devices` only when `device_id` is set.

Revision ID: 0234
Revises: 0233
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0234"
down_revision: str | None = "0233"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (bloque común aquí)

CHILDREN = [
    ("ticket_comment", "ticket_id"),
    ("ticket_attachment", "ticket_id"),
    ("ticket_approval", "ticket_id"),
    ("ticket_status_history", "ticket_id"),
    ("ticket_sla", "ticket_id"),
]

TICKET_BACKFILL = (
    "COALESCE("
    "(SELECT d.client_id FROM devices d WHERE d.id = ticket.device_id), "
    "(SELECT u.client_id FROM users u WHERE u.id = ticket.requester_id AND u.kind = 'contact'), "
    "ticket.org_id)"
)


def upgrade() -> None:
    _add_c("ticket", TICKET_BACKFILL)
    for child, fk in CHILDREN:
        _add_h(child, "ticket", fk)
    op.execute(
        """
        CREATE FUNCTION its_ticket_client_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE d_client uuid; r_kind text; r_client uuid; r_found boolean;
        BEGIN
            IF NEW.device_id IS NOT NULL THEN
                SELECT client_id INTO d_client FROM devices WHERE id = NEW.device_id;
                IF d_client IS DISTINCT FROM NEW.client_id THEN
                    RAISE EXCEPTION 'ticket device belongs to another client'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            IF NEW.requester_id IS NOT NULL THEN
                SELECT true, kind, client_id INTO r_found, r_kind, r_client
                    FROM users WHERE id = NEW.requester_id;
                IF r_found IS NULL THEN
                    RAISE EXCEPTION 'ticket requester is not visible'
                        USING ERRCODE = 'check_violation';
                END IF;
                IF r_kind = 'contact' AND r_client IS DISTINCT FROM NEW.client_id THEN
                    RAISE EXCEPTION 'ticket requester is a contact of another client'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER its_ticket_client_guard BEFORE INSERT OR UPDATE OF "
        "client_id, device_id, requester_id ON ticket "
        "FOR EACH ROW EXECUTE FUNCTION its_ticket_client_guard()"
    )
    for table in ["ticket", *[c for c, _ in CHILDREN]]:
        _isolate(table)


def downgrade() -> None:
    op.execute("DROP TRIGGER its_ticket_client_guard ON ticket")
    op.execute("DROP FUNCTION its_ticket_client_guard()")
    for child, _ in reversed(CHILDREN):
        _drop_h(child, "ticket")
    _drop_c("ticket")
```

Ninguna clave única cambia (research-1 §A): `(org_id, number)` es por
organización a propósito y va a `ORG_WIDE_KEYS`; `email_token` es aleatorio
y va a `SURROGATE_COLUMNS`; las de `ticket_approval` y `ticket_sla` llevan
`ticket_id`, la fk de herencia.

- [ ] **Step 4: Modelos y catálogo**

- `Ticket.client_id: Mapped[UUID] = mapped_column(ForeignKey("clients.id"), index=True)` y `client_org_fk("ticket")` en `__table_args__`.
- En las cuatro hijas de `service_desk/models.py` y en `TicketSla`: `client_id` declarado como el de `HealthAlert` (`app/modules/health/models.py:367`): `ForeignKey("clients.id")`, `index=True`, `server_default=FetchedValue()`, `server_onupdate=FetchedValue()`, con el comentario "Set by trigger its_client_from_parent from ticket (migration 0234): never sent".
- `client_scope_catalog.py`: quita `ticket`, `ticket_comment`, `ticket_attachment`, `ticket_approval`, `ticket_status_history`, `ticket_sla` de `PENDING_PHASE_2`.
- `tests/test_client_catalog.py`:

```python
OTHER_POLICIES: dict[str, set[str]] = {
    "users": {"portal_isolation"},
    "doc_articles": {"portal_isolation"},
    # The portal's own policies (0028, 0038): a requester reads their own
    # tickets. ANDed with the client policy, never replaced by it.
    "ticket": {"portal_isolation"},
    "ticket_comment": {"portal_isolation"},
    "ticket_attachment": {"portal_isolation"},
    "ticket_approval": {"portal_isolation"},
    "ticket_status_history": {"portal_isolation"},
    "ticket_sla": {"portal_isolation"},
}
```

  En `SURROGATE_COLUMNS`: `("ticket", "email_token"): "a random 256-bit reply token (`email_intake/identity.py::mint_email_token`)"`.
  En `ORG_WIDE_KEYS`, con el nombre que confirme `pg_constraint` (research-1 §A: `ticket_org_id_number_key`): `"the ticket number comes from the organisation's one counter (`ticket_number_counter`, G): nobody types it, every client's tickets share the sequence, and a moved ticket keeps its number. Per client, INC-12 could name two tickets"`.
  Confirma la lista de políticas de portal de cada tabla en la base antes de escribirla: si alguna tabla tiene otro nombre de política, el diccionario lleva el real.

- [ ] **Step 5: El cliente de un ticket nuevo**

`TicketCreate` gana `client_id: UUID | None = None` (comentario: "the client
the ticket is filed under (spec §3.2). Taken from the device when there is
one; required when there is none"). `TicketOut` gana `client_id: UUID`.
`list_tickets` y su ruta ganan el filtro opcional `client_id`.

En `create_ticket`, después de resolver el equipo y antes del número:

```python
    claimed = client_id if client_id is not None else data.client_id
    if device_id is not None:
        if claimed is not None and claimed != device.client_id:
            raise TicketValidationError(tr("the ticket's client is not its device's client"))
        ticket_client = device.client_id
    elif claimed is None:
        raise TicketValidationError(tr("choose the client this ticket belongs to"))
    else:
        ticket_client = claimed
    if origin != "portal":
        # portal_user cannot read `clients` (0028); the portal derives its
        # client from the requester itself (create_my_ticket).
        client = await client_for_new_rows(session, org_id=org_id, client_id=ticket_client)
        if client.kind == CLIENT_KIND_UNASSIGNED and origin != "email":
            raise TicketValidationError(tr("tickets reach Unassigned only by email"))
    if requester_id is not None:
        await _requester_belongs(session, requester_id, ticket_client)
    if assigned_to is not None:
        await _assignee_sees(session, assigned_to, ticket_client)
```

- `TicketValidationError` es el error de validación que el módulo ya traduce
  a 422 (usa el que exista en `service_desk/errors.py`; si no hay uno de 422,
  créalo junto a los demás y regístralo donde se registran).
- `ClientNotFound` → 404 y `NotAcceptingNewRows` → 409, como en
  `app/api/v1/clients.py`.
- `_requester_belongs`: si el usuario es `kind == "contact"` y su `client_id`
  no es `ticket_client`, `TicketValidationError(tr("the requester is a contact of another client"))`.
- `_assignee_sees`: el asignado tiene que ser `kind == "staff"` y
  `(await clients_holding(session, assignee_id, "ticket.read")).allows(ticket_client)`
  (`app/core/rbac.py:460`); si no, `TicketValidationError(tr("the assignee cannot see this client's tickets"))`.
- `Ticket(...)` gana `client_id=ticket_client`.
- `update_ticket` (`tickets.py:872`): un `device_id` nuevo tiene que ser de `ticket.client_id` (422 con la primera frase).
- `assign_ticket` (`transitions.py:558`): `_assignee_sees(session, new_assignee, ticket.client_id)`.
- `approvals._actor_holds_role` (`approvals.py:106-122`): la asignación cuenta si `UserRole.client_id IS NULL OR UserRole.client_id == ticket.client_id` (research-1 §I10).
- Cada `events.publish` de ticket (Task 1 dejó `None  # ... Task 2`) pasa `client_id=ticket.client_id`; cada `audit_service.record` con `target_type="ticket"` también.

Quién pasa `client_id=` a `create_ticket`:

| Llamada | Cliente |
|---|---|
| Ruta del técnico (`api.py:658`) | ninguno: lo trae `data` |
| Portal (`portal/tickets.py::create_my_ticket`) | el del solicitante si es `contact`; si es `staff`, el interno (`org_id`, C5). Si el interno no está en `ctx.clients`, el portal responde `PortalNotFoundError` (404, como el resto del portal). El solicitante se lee con `select(User.kind, User.client_id).where(User.id == requester_id)`: `portal_user` ve su propia fila |
| Alerta (`health/actions.py:_execute_open_ticket`) | el cliente de la alerta, leído de la base (H) |
| Cambio (`service_desk/adapters.py:156`) | `org_id  # plan 2026-10-26 Task 7 passes the change's client` |
| Correo (`email_intake/intake.py:804`) | el del remitente si es `contact`; si no, `org_id`, con el comentario `# plan 2026-10-26 Task 4 decides by domain and Unassigned (spec §3.3)` |

- [ ] **Step 6: Consola, MCP y pruebas existentes**

- `NewTicketDialog.tsx` (líneas 120-137 construyen el cuerpo): sin equipo, `client_id: internalClientId(me)` con `// PHASE 3` encima y el `requireKnown` de `internalClient.ts`; con equipo, nada (el servidor lo deriva). `TicketOut` y `TicketListFilters` ganan `client_id` en `types.ts`; los `fixtures.ts` del módulo, también.
- `app/mcp/descriptions/part_a.py` (entrada de `POST /service-desk/tickets`, research-1 §F): una frase que diga que `client_id` es obligatorio sin equipo y que con equipo se toma del equipo.
- Las pruebas de research-1 §H: cada `Ticket(` gana `client_id=org_id` (el cliente interno de su organización); cada `TicketCreate(` sin `device_id`, `client_id=org_id`. Son ~150 llamadas mecánicas: un script de `sed` o `libcst` sobre `tests/`, revisado a mano después, es aceptable; ninguna aserción cambia.
- `tests/test_client_isolation.py`: `World` gana `queue: UUID` y `category: UUID` (G, de la organización), `Side` gana `ticket: UUID` (creado en `_side` con `client_id=client.id`), y `PATHS` gana:

```python
    "C-ticket": ("ticket", lambda w, s: Ticket(
        org_id=w.org, client_id=s.client, number=f"T-{uuid4().hex[:8]}", type="incident",
        category_id=w.category, queue_id=w.queue, status="new", subject="s",
        requester_id=w.actor, origin="technician", created_by=w.actor,
    )),
    "H-ticket_comment-via-ticket": ("ticket_comment", lambda w, s: TicketComment(
        org_id=w.org, ticket_id=s.ticket, author_id=w.actor, body="b", visibility="public",
    )),
```

  (Ajusta los nombres de columna a los del modelo si alguno difiere.)

- [ ] **Step 7: Comprobar**

Run: `.venv/bin/pytest -q tests/test_client_scope_tickets.py tests/test_client_isolation.py tests/test_client_catalog.py tests/test_service_desk_api.py tests/test_portal_tickets.py tests/test_email_intake_intake.py tests/test_sla_wiring.py`
Expected: PASS. Luego la suite completa, `mypy`, `ruff`; y en `frontend/`, `npx vitest run src/modules/service-desk` y `npx tsc -b`.

- [ ] **Step 8: Commit**

```bash
git add backend frontend
git commit -m "feat(clients): tickets belong to a client; new tickets take their device's or the stated client (phase 2 Task 2)"
```

---

### Task 3: Tickets — SLA por cliente (D2), mover entre clientes y avisos dentro del cliente

Investigación: research-1 ("SLA: global row and client row", §E5-E8, §I1, §I4-I5, §I9, §I11).

**Files:**
- Modify: `backend/app/modules/sla/policies.py` (`resolve_policy`, `create_policy`), `backend/app/modules/sla/schema.py` (`PolicyCreate.client_id`, `PolicyOut.client_id`), `backend/app/modules/sla/tracking.py:574,677`
- Modify: `backend/app/modules/service_desk/tickets.py` (nuevo `move_ticket`), `backend/app/modules/service_desk/api.py` (ruta `POST /service-desk/tickets/{id}/move`), `backend/app/modules/service_desk/threads.py` (`add_comment`, `add_attachment` leen el ticket `FOR SHARE`)
- Modify: `backend/app/modules/notification/routing.py` (`_resolve_single_user`, `_resolve_queue_members`)
- Modify: `backend/app/mcp/descriptions/part_a.py` (la ruta nueva), `backend/tests/test_global_writes.py` (la ruta nueva no es global)
- Modify: `frontend/src/modules/sla/slaState.ts` (`resolutionPreview` con la regla D2), `frontend/src/modules/service-desk/TicketDetailPage.tsx`, `frontend/src/modules/service-desk/api.ts`, `frontend/src/lib/api.ts` (`moveTicket`)
- Test: `backend/tests/test_client_scope_tickets.py` (amplía el de la Task 2)

**Interfaces:**
- Consumes: `ticket.client_id` y la propagación a las hijas (Task 2).
- Produces: `resolve_policy(session, org_id, *, client_id: UUID, queue_id: UUID | None, priority: str | None) -> SlaPolicy | None`.
- Produces: `move_ticket(session, org_id, actor_id, ticket_id, *, to_client_id: UUID) -> Ticket`; ruta `POST /service-desk/tickets/{id}/move` con cuerpo `{"client_id": UUID}`, permiso `ticket.write` en el origen **y** en el destino; acción de auditoría `ticket.moved` (`metadata = {"from": ..., "to": ...}`).
- Produces: `PolicyCreate.client_id: UUID | None = None` (sin él, la política es global y exige todos los clientes, como hoy).

- [ ] **Step 1: Escribir las pruebas que fallan** (al final de `test_client_scope_tickets.py`)

```python
from app.models import SlaPolicy, TicketSla  # noqa: E402
from app.modules.sla.policies import resolve_policy  # noqa: E402


async def _policy(env: ApiEnv, org: UUID, client: UUID | None, hours: int, **level: Any) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        p = SlaPolicy(org_id=org, client_id=client, active=True,
                      first_response_minutes=hours * 60, resolution_minutes=hours * 120,
                      **level)
        s.add(p)
        await s.commit()
        return p.id


async def test_a_client_with_policies_of_its_own_uses_only_those(api_env: ApiEnv) -> None:
    """D2. Under scope all -- the email poller and the sweeps run there, and
    RLS then shows every client's policies -- so the client predicate in the
    code is what is tested (spec §5 test 4)."""
    org = await api_env.create_org("t3-sla")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    queue = UUID(desk["queue"])
    global_specific = await _policy(api_env, org, None, 8, queue_id=queue, priority="high")
    x_catch_all = await _policy(api_env, org, x, 1)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        for_x = await resolve_policy(s, org, client_id=x, queue_id=queue, priority="high")
        for_y = await resolve_policy(s, org, client_id=y, queue_id=queue, priority="high")
    assert for_x is not None and for_x.id == x_catch_all
    assert for_y is not None and for_y.id == global_specific


async def test_a_client_without_policies_falls_back_to_the_global_ones(api_env: ApiEnv) -> None:
    org = await api_env.create_org("t3-sla-fallback")
    x, _ = await client_with_site(api_env, org)
    global_catch_all = await _policy(api_env, org, None, 4)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        found = await resolve_policy(s, org, client_id=x, queue_id=None, priority=None)
    assert found is not None and found.id == global_catch_all


async def test_a_ticket_s_sla_comes_from_its_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("t3-sla-ticket")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    await _policy(api_env, org, None, 8)
    x_policy = await _policy(api_env, org, x, 1)
    ticket = (
        await api_env.client.post(
            f"{API}/tickets", json=_body(desk, client_id=str(x)), headers=desk["admin"]
        )
    ).json()["id"]
    async with AsyncSession(api_env.admin_engine) as s:
        policy = (
            await s.execute(select(TicketSla.policy_id).where(TicketSla.ticket_id == UUID(ticket)))
        ).scalar_one()
    assert policy == x_policy


async def test_moving_a_ticket_moves_its_children(api_env: ApiEnv) -> None:
    org = await api_env.create_org("t3-move")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    ticket = (
        await api_env.client.post(
            f"{API}/tickets", json=_body(desk, client_id=str(x)), headers=desk["admin"]
        )
    ).json()["id"]
    await api_env.client.post(
        f"{API}/tickets/{ticket}/comments", json={"body": "uno", "visibility": "public"},
        headers=desk["admin"],
    )
    r = await api_env.client.post(
        f"{API}/tickets/{ticket}/move", json={"client_id": str(y)}, headers=desk["admin"]
    )
    assert r.status_code == 200, r.text
    assert r.json()["client_id"] == str(y)
    async with AsyncSession(api_env.admin_engine) as s:
        for table in ("ticket_comment", "ticket_status_history"):
            clients = set(
                (
                    await s.execute(
                        text(f"SELECT client_id FROM {table} WHERE ticket_id = :t"),
                        {"t": UUID(ticket)},
                    )
                ).scalars()
            )
            assert clients == {y}, table


async def test_a_move_needs_the_permission_on_both_clients(api_env: ApiEnv) -> None:
    org = await api_env.create_org("t3-move-perm")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    ticket = (
        await api_env.client.post(
            f"{API}/tickets", json=_body(desk, client_id=str(x)), headers=desk["admin"]
        )
    ).json()["id"]
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.post(
        f"{API}/tickets/{ticket}/move", json={"client_id": str(y)}, headers=x_tech
    )
    assert r.status_code == 404, r.text  # Y is invisible to them: not a 403 oracle


async def test_a_ticket_with_a_device_does_not_move(api_env: ApiEnv) -> None:
    """C9: the device's client is immutable and the ticket must agree."""
    org = await api_env.create_org("t3-move-dev")
    desk = await _desk(api_env, org)
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    device = await _device(api_env, org, x, x_site)
    ticket = (
        await api_env.client.post(
            f"{API}/tickets", json=_body(desk, device_id=str(device)), headers=desk["admin"]
        )
    ).json()["id"]
    r = await api_env.client.post(
        f"{API}/tickets/{ticket}/move", json={"client_id": str(y)}, headers=desk["admin"]
    )
    assert r.status_code == 409, r.text


async def test_queue_members_outside_the_ticket_s_client_get_no_notice(
    api_env: ApiEnv,
) -> None:
    """research-1 §I9: a queue is the organisation's (G), its members are
    not all of every client's. A member with no role in the ticket's client
    is dropped from the recipients."""
    from app.models import DomainEvent, NotificationChannel, NotificationRule, QueueMember
    from app.modules.notification.routing import resolve_recipients

    org = await api_env.create_org("t3-notify")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    await everything_on(api_env, org, y)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        y_tech, y_email = (
            await s.execute(
                text(
                    "SELECT u.id, u.email FROM users u JOIN user_roles r ON r.user_id = u.id "
                    "WHERE r.client_id = :y"
                ),
                {"y": y},
            )
        ).one()
        s.add(QueueMember(org_id=org, queue_id=UUID(desk["queue"]), user_id=y_tech))
        channel = NotificationChannel(org_id=org, kind="email", name="mail", config={})
        s.add(channel)
        await s.flush()
        s.add(NotificationRule(
            org_id=org, event_type="ticket.assigned", channel_id=channel.id,
            recipient_kind="queue_members", active=True,
        ))
        await s.commit()
    event = DomainEvent(
        org_id=org, client_id=x, event_type="ticket.assigned", subject_type="ticket",
        subject_id=uuid4(), payload={"queue_id": desk["queue"]}, actor_user_id=None,
    )
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        recipients = await resolve_recipients(s, event=event)
    assert y_email not in {r.address for r in recipients}
```

Ajusta los nombres de columna de `SlaPolicy`, `NotificationChannel` y
`NotificationRule` a los de sus modelos (`app/modules/sla/models.py:116`,
`app/modules/notification/models.py`) si alguno difiere: las aserciones no
cambian.

- [ ] **Step 2: Comprobar que fallan**

Run: `.venv/bin/pytest -q tests/test_client_scope_tickets.py -k "sla or move or notice"`
Expected: FAIL (`resolve_policy() got an unexpected keyword argument 'client_id'`, 404 de la ruta `/move`, el técnico de Y entre los destinatarios).

- [ ] **Step 3: `resolve_policy` con la regla D2**

```python
async def resolve_policy(
    session: AsyncSession,
    org_id: UUID,
    *,
    client_id: UUID,
    queue_id: UUID | None,
    priority: str | None,
) -> SlaPolicy | None:
    """Spec §3's resolution order -- most specific first -- among ONE owner's
    policies (plan 2026-10-26 D2): the ticket's client's, when it has any
    active policy of its own; otherwise the global ones (client_id NULL).
    A client's own policies replace the global ones whole, as a contract
    does; they are never mixed level by level.

    THE CLIENT PREDICATE IS EXPLICIT. The email poller and the SLA sweep run
    under scope all, where RLS shows every client's policies: without it a
    global row and a client row at one level raise MultipleResultsFound
    (research-1, a 500 on create and a wedged mailbox), and another client's
    lone policy would be applied to this ticket."""
    has_own = (
        await session.execute(
            select(
                exists().where(
                    SlaPolicy.org_id == org_id,
                    SlaPolicy.client_id == client_id,
                    SlaPolicy.active.is_(True),
                )
            )
        )
    ).scalar_one()
    owner = SlaPolicy.client_id == client_id if has_own else SlaPolicy.client_id.is_(None)
    levels = [(queue_id, priority), (queue_id, None), (None, priority), (None, None)]
    seen: set[tuple[UUID | None, str | None]] = set()
    for level_queue_id, level_priority in levels:
        if (level_queue_id, level_priority) in seen:
            continue
        seen.add((level_queue_id, level_priority))
        conditions = [SlaPolicy.org_id == org_id, SlaPolicy.active.is_(True), owner]
        conditions.append(
            SlaPolicy.queue_id == level_queue_id
            if level_queue_id is not None
            else SlaPolicy.queue_id.is_(None)
        )
        conditions.append(
            SlaPolicy.priority == level_priority
            if level_priority is not None
            else SlaPolicy.priority.is_(None)
        )
        policy = (await session.execute(select(SlaPolicy).where(*conditions))).scalar_one_or_none()
        if policy is not None:
            return policy
    return None
```

Las dos llamadas de `tracking.py` (574, 677) pasan `client_id=ticket.client_id`.
Los índices únicos por cliente de 0231 garantizan una fila por nivel y dueño,
así que `scalar_one_or_none` vuelve a ser seguro.

`PolicyCreate` gana `client_id: UUID | None = None` y `PolicyOut.client_id`.
`create_policy` (`policies.py:348`): con `client_id`, carga el cliente con
`client_for_new_rows` (404/409) y escribe la fila con ese cliente, y **no**
llama a `refuse_unless_every_client`; sin él, la fila es global y la guarda
de hoy se queda. La lista de `tests/test_global_writes.py` ya trata estas
rutas como "O: sla_policy" (research-1 §I11): no cambia.

En la consola, `resolutionPreview` (`frontend/src/modules/sla/slaState.ts:490`)
recibe el `clientId` del ticket y aplica la misma regla: si alguna política
activa tiene ese cliente, solo esas; si no, las de `client_id === null`.
Prueba en `slaState.test.ts` con los dos casos.

- [ ] **Step 4: Mover un ticket**

Servicio, en `tickets.py`:

```python
async def move_ticket(
    session: AsyncSession, org_id: UUID, actor_id: UUID, ticket_id: UUID, *, to_client_id: UUID
) -> Ticket:
    """Spec §2.4: a ticket changes client -- the normal way out of
    Unassigned. The session must see BOTH clients (the route opens it so):
    `its_client_to_children` is SECURITY INVOKER and moves the five child
    tables in the same statement."""
    ticket = (
        await session.execute(
            select(Ticket).where(Ticket.id == ticket_id, Ticket.org_id == org_id).with_for_update()
        )
    ).scalar_one_or_none()
    if ticket is None:
        raise TicketNotFoundError(ticket_id)
    if ticket.client_id == to_client_id:
        return ticket
    if ticket.device_id is not None:
        raise TicketConflictError(tr("remove the ticket's device before moving it to another client"))
    if ticket.source_alert_id is not None or ticket.origin in ("alert", "change"):
        raise TicketConflictError(tr("a ticket opened by an alert or a change stays with its client"))
    destination = await client_for_new_rows(session, org_id=org_id, client_id=to_client_id)
    if destination.kind == CLIENT_KIND_UNASSIGNED:
        raise TicketValidationError(tr("tickets reach Unassigned only by email"))
    await _requester_belongs(session, ticket.requester_id, to_client_id)  # 422 for a contact
    previous_assignee = ticket.assigned_to
    if previous_assignee is not None and not (
        await clients_holding(session, previous_assignee, "ticket.read")
    ).allows(to_client_id):
        ticket.assigned_to = None
    source = ticket.client_id
    ticket.client_id = to_client_id
    await session.flush()
    await recompute_for_retriage(session, org_id, ticket, actor_id=actor_id)  # tracking.py:584
    await audit_service.record(
        session, org_id=org_id, actor_type="user", actor_id=actor_id, action="ticket.moved",
        target_type="ticket", target_id=str(ticket.id), client_id=to_client_id,
        metadata={"from": str(source), "to": str(to_client_id),
                  "unassigned": str(previous_assignee) if ticket.assigned_to is None
                  and previous_assignee is not None else None},
    )
    return ticket
```

- `TicketConflictError` es el error de 409 del módulo (si no existe, créalo en `service_desk/errors.py` y mapéalo como los demás). Ajusta la llamada a `recompute_for_retriage` a su firma real (`tracking.py:584`).
- Una alerta enlazada también impide mover: añade la comprobación de que ningún `health_alert` ni `health_alert_action` tiene `ticket_id = ticket.id` (research-1 §E6), con la misma frase.
- `add_comment` y `add_attachment` (`threads.py`) cargan el ticket con `.with_for_update(read=True)` antes de insertar la hija: el `FOR UPDATE` de `move_ticket` los espera y la hija nace en el cliente nuevo (research-1 §I5).

Ruta, en `service_desk/api.py`:

```python
class TicketMove(BaseModel):
    client_id: UUID


@router.post("/tickets/{ticket_id}/move", response_model=TicketOut)
async def move_ticket_route(
    ticket_id: UUID,
    body: TicketMove,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ticket.write")),
) -> TicketOut:
    """Both clients: the source and the destination. `X-Client-Id` narrows
    what the console LISTS, not where a ticket may go, so the person's own
    scope for `ticket.write` decides (spec §2.4)."""
    holding = await person_scope(
        org_id=ctx.org_id, user_id=ctx.user_id, code="ticket.write",
        engine=request.app.state.app_engine,
    )
    if not holding.allows(body.client_id):
        raise HTTPException(status_code=404, detail=tr("client not found"))
    async with org_session(ctx.org_id, clients=ctx.clients, engine=...) as s:
        source = (
            await s.execute(select(Ticket.client_id).where(Ticket.id == ticket_id))
        ).scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=404, detail=tr("ticket not found"))
    async with org_session(
        ctx.org_id, clients=ClientScope.only([source, body.client_id]), engine=...
    ) as s:
        ticket = await move_ticket(s, ctx.org_id, ctx.user_id, ticket_id, to_client_id=body.client_id)
        return TicketOut.model_validate(ticket)
```

Copia del módulo el `engine=` que usan las demás rutas y su forma de traducir
los errores de servicio. `person_scope` es el de `app/api/deps.py` (lo usa
`app/modules/portal/principal.py:64`). La entrada de MCP en `part_a.py`:
"Move a ticket to another client. Needs ticket.write in both clients; a
ticket with a device, or opened by an alert or a change, does not move."
`tests/test_global_writes.py`: la ruta es por cliente (no va a
`GLOBAL_WRITES`); si la prueba exige clasificar cada ruta de escritura, va
donde van las demás de `ticket.write`.

Consola: en `TicketDetailPage.tsx`, una acción "Mover a otro cliente" junto a
la de asignar (copia `useAssignTicketMutation`, `service-desk/api.ts:170`),
con un selector de los clientes de `GET /clients` y el texto del 409 visible.
`api.moveTicket(id, clientId)` en `lib/api.ts` (cerca de la línea 4027).
Textos en `frontend/src/i18n/{en,es}/` y la acción `ticket.moved` en
`auditVocabulary.ts` de los dos idiomas.

- [ ] **Step 5: Avisos solo a quien ve el cliente**

En `notification/routing.py`, `resolve_recipients` lee `event.client_id`.
Cuando no es nulo, `_resolve_single_user` y `_resolve_queue_members` se
quedan solo con:
- un usuario `kind == "contact"` cuyo `client_id` es el del evento;
- un usuario `kind == "staff"` con algún rol en ese cliente:
  `(await clients_holding(session, user_id, None)).allows(event.client_id)`.

Un evento sin cliente (global) no cambia. Los destinos `channel_default`
(webhooks, un correo configurado por un administrador de todos los clientes)
no son personas y no cambian. Docstring: "A recipient outside the event's
client would read another client's ticket subject (research-1 §I9)."

- [ ] **Step 6: Comprobar**

Run: `.venv/bin/pytest -q tests/test_client_scope_tickets.py tests/test_sla_policies.py tests/test_sla_tracking.py tests/test_sla_wiring.py tests/test_notification_routing.py tests/test_global_writes.py tests/test_mcp_descriptions.py`
Expected: PASS (si un archivo de esa lista no existe con ese nombre, usa el que cubra el módulo: `ls tests | grep -E "sla|notification"`). Luego la suite completa, `mypy`, `ruff`, y en `frontend/` `npx vitest run src/modules/sla src/modules/service-desk` y `npx tsc -b`.

- [ ] **Step 7: Commit**

```bash
git add backend frontend
git commit -m "feat(clients): per-client SLA, moving a ticket between clients, notices only inside the client (phase 2 Task 3)"
```

---

### Task 4: Correo entrante — el cliente de cada mensaje (spec §3.3, D1)

Investigación: research-1 (§B punto 3, §D "What only the email entry needs", §I2).

**Files:**
- Create: `backend/app/services/client_routing.py` (`client_for_sender`, `system_client_id`)
- Modify: `backend/app/modules/email_intake/intake.py` (`process_message`, `_finish_row`)
- Modify: `backend/app/modules/email_intake/identity.py` (`SenderResolution` lleva `kind` y `client_id` del remitente)
- Modify: `docs/modules/email-intake/spec.md` (§0.2 y §8: el cliente del mensaje)
- Test: `backend/tests/test_email_intake_intake.py` (se añaden las pruebas al final; el fixture `email_env` vive ahí)

**Interfaces:**
- Consumes: `create_ticket(..., client_id=)` (Task 2).
- Produces: `client_for_sender(session, org_id, *, sender_kind: str | None, sender_client_id: UUID | None, address: str) -> UUID` — el orden de §3.3: contacto → dominio → "Sin asignar".
- Produces: `system_client_id(session, org_id, kind: Literal["internal", "unassigned"]) -> UUID`.
- Produces: cada fila de `inbound_message` tiene `client_id` (nunca nulo a partir de esta tarea): el cliente decidido, o "Sin asignar".

- [ ] **Step 1: Escribir las pruebas que fallan** (al final de `tests/test_email_intake_intake.py`)

```python
# --- MSP clients phase 2, Task 4: the client of each message (spec §3.3, D1).
# Distinct senders, so each step is the only one that can decide (spec §5
# test 9): the contact's own domain belongs to ANOTHER client, so a domain
# match would pick the wrong one.

from app.models import Client, InboundMessage, User  # noqa: E402


async def _client(env, name: str, *domains: str) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        c = Client(org_id=env.org_id, name=name, kind="customer", email_domains=list(domains))
        s.add(c)
        await s.commit()
        return c.id


async def _sender(env, email: str, *, kind: str = "staff", client: UUID | None = None) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        u = User(org_id=env.org_id, external_subject=uuid4().hex, email=email,
                 kind=kind, client_id=client)
        s.add(u)
        await s.commit()
        return u.id


async def _process(env, sender: str) -> InboundMessage:
    async with org_session(env.org_id, engine=env.engine, clients=ClientScope.all()) as s:
        return await process_message(
            s, env.org_id, env.mailbox, env.parsed(sender=sender), store=env.store
        )


async def _ticket_client(env, row: InboundMessage) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        return (await s.execute(select(Ticket.client_id).where(Ticket.id == row.ticket_id))).scalar_one()


async def test_a_contact_s_mail_opens_a_ticket_in_the_contact_s_client(email_env) -> None:
    x = await _client(email_env, "X")
    await _client(email_env, "Y", "ydomain.test")
    await _sender(email_env, "ana@ydomain.test", kind="contact", client=x)
    row = await _process(email_env, "ana@ydomain.test")
    assert row.disposition == "created"
    assert await _ticket_client(email_env, row) == x
    assert row.client_id == x


async def test_a_staff_sender_s_domain_decides_the_client(email_env) -> None:
    z = await _client(email_env, "Z", "zdomain.test")
    await _sender(email_env, "tech@ZDOMAIN.test")
    row = await _process(email_env, "tech@ZDOMAIN.test")
    assert row.disposition == "created"
    assert await _ticket_client(email_env, row) == z


async def test_an_unmatched_staff_sender_lands_in_unassigned(email_env) -> None:
    # alice@acme.test is staff (email_env) and no client holds acme.test.
    row = await _process(email_env, "alice@acme.test")
    async with AsyncSession(email_env.admin_engine) as s:
        unassigned = (
            await s.execute(
                select(Client.id).where(Client.org_id == email_env.org_id,
                                        Client.kind == "unassigned")
            )
        ).scalar_one()
    assert await _ticket_client(email_env, row) == unassigned


async def test_an_unknown_sender_stays_quarantined_and_is_stamped_with_its_domain_s_client(
    email_env,
) -> None:
    """D1: no ticket for a stranger (email-intake spec §0.2), but the
    quarantine can be read per client."""
    w = await _client(email_env, "W", "wdomain.test")
    row = await _process(email_env, "stranger@wdomain.test")
    assert row.disposition == "quarantined" and row.ticket_id is None
    assert row.client_id == w


async def test_an_unknown_unmatched_sender_is_stamped_unassigned(email_env) -> None:
    row = await _process(email_env, "stranger@nowhere.test")
    async with AsyncSession(email_env.admin_engine) as s:
        kind = (await s.execute(select(Client.kind).where(Client.id == row.client_id))).scalar_one()
    assert row.disposition == "quarantined" and kind == "unassigned"


async def test_an_archived_client_s_domain_matches_nothing(email_env) -> None:
    async with AsyncSession(email_env.admin_engine, expire_on_commit=False) as s:
        s.add(Client(org_id=email_env.org_id, name="Old", kind="customer",
                     email_domains=["old.test"], status="archived"))
        await s.commit()
    await _sender(email_env, "tech@old.test")
    row = await _process(email_env, "tech@old.test")
    async with AsyncSession(email_env.admin_engine) as s:
        kind = (
            await s.execute(select(Client.kind).where(Client.id == (
                await s.execute(select(Ticket.client_id).where(Ticket.id == row.ticket_id))
            ).scalar_one()))
        ).scalar_one()
    assert kind == "unassigned"


async def test_a_reply_keeps_its_ticket_s_client(email_env) -> None:
    x = await _client(email_env, "Rx")
    await _sender(email_env, "rita@x.test", kind="contact", client=x)
    first = await _process(email_env, "rita@x.test")
    async with AsyncSession(email_env.admin_engine) as s:
        token = (await s.execute(select(Ticket.email_token).where(Ticket.id == first.ticket_id))).scalar_one()
    reply = email_env.parsed(sender="rita@x.test", subject=f"Re: [{token}]")
    async with org_session(email_env.org_id, engine=email_env.engine, clients=ClientScope.all()) as s:
        row = await process_message(s, email_env.org_id, email_env.mailbox, reply, store=email_env.store)
    assert row.client_id == x
```

La forma de citar el token en la respuesta es la que ya usan
`test_a_valid_token_from_the_requester_adds_a_public_comment` y
`email_env.parsed`: si esa prueba lo pasa por cabecera o por otro argumento
de `parsed(...)`, usa ese mismo mecanismo aquí.

- [ ] **Step 2: Comprobar que fallan**

Run: `.venv/bin/pytest -q tests/test_email_intake_intake.py -k "client or unassigned or quarantined_and_is_stamped or archived_client"`
Expected: FAIL (el ticket del contacto va al cliente de la Task 2 pero `row.client_id` es nulo; el remitente `staff` va al interno, no al de su dominio).

- [ ] **Step 3: El orden de §3.3**

```python
# backend/app/services/client_routing.py
"""Which client a message belongs to (MSP clients spec §3.3, plan 2026-10-26
Task 4 and D1).

The email poller runs under scope all (`ALL_SCOPE_ALLOWLIST`), so RLS cannot
tell clients apart here: every rule below is an explicit predicate, and a
domain is compared exactly, without case (`its_client_domains_unique`, 0232,
guarantees one client per domain)."""

from typing import Literal
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Client


async def system_client_id(
    session: AsyncSession, org_id: UUID, kind: Literal["internal", "unassigned"]
) -> UUID:
    """The organisation's internal or Unassigned client (0230 creates both).
    The internal one's id IS the org id (plan Global Constraints)."""
    if kind == "internal":
        return org_id
    return (
        await session.execute(
            select(Client.id).where(Client.org_id == org_id, Client.kind == "unassigned")
        )
    ).scalar_one()


async def client_for_sender(
    session: AsyncSession,
    org_id: UUID,
    *,
    sender_kind: str | None,
    sender_client_id: UUID | None,
    address: str,
) -> UUID:
    """1. An active contact: their client. 2. A client whose `email_domains`
    holds the address's domain (active clients only). 3. Unassigned."""
    if sender_kind == "contact" and sender_client_id is not None:
        return sender_client_id
    domain = address.rpartition("@")[2].strip().lower()
    if domain:
        match = (
            await session.execute(
                select(Client.id).where(
                    Client.org_id == org_id,
                    Client.status == "active",
                    text(":domain = ANY (SELECT lower(d) FROM unnest(clients.email_domains) d)"),
                ).params(domain=domain)
            )
        ).scalar_one_or_none()
        if match is not None:
            return match
    return await system_client_id(session, org_id, "unassigned")
```

(`func` sobra si no lo usas; `ruff` lo dirá.)

- [ ] **Step 4: `process_message` decide una vez y lo usa en todo**

- `identity.resolve_sender` devuelve también `kind` y `client_id` del usuario encontrado (`SenderResolution`, `identity.py:70`). Solo los usuarios activos cuentan, como hoy.
- En `process_message`, justo después de `resolve_sender` (línea ~653) y antes de cualquier salida:

```python
    message_client = await client_for_sender(
        session, org_id,
        sender_kind=sender.kind, sender_client_id=sender.client_id,
        address=parsed.from_address or "",
    )
```

- Las salidas anteriores a `resolve_sender` (bucle, mensaje que no se pudo leer) usan `await system_client_id(session, org_id, "unassigned")`: `NULL` en una tabla O significa "todos los clientes", y la fila guarda el cuerpo del correo.
- `_finish_row` recibe `client_id` y lo escribe en la fila. Las diez llamadas (research-1 §B) lo pasan.
- Ticket nuevo: `create_ticket(..., origin="email", client_id=message_client)`.
- Respuesta a un ticket existente: `client_id` de la fila = el del ticket (`ticket.client_id`); el comentario hereda el suyo.
- El remitente `staff` cuyo dominio no es de nadie y abre ticket: va a "Sin asignar" (§3.3 paso 3). El de un dominio de un cliente archivado, también.
- Quita el comentario `# plan 2026-10-26 Task 4 ...` que dejó la Task 2.

- [ ] **Step 5: La spec del módulo**

En `docs/modules/email-intake/spec.md`, §0.2 y §8, un párrafo con fecha
2026-10-26: "Cada mensaje queda en un cliente (spec de clientes §3.3): el del
contacto que lo envía; si no, el del dominio del remitente; si no, Sin
asignar. Un remitente desconocido sigue en cuarentena (decisión del operador
D1, 2026-10-26); su mensaje queda marcado con el cliente de su dominio para
revisarlo por cliente."

- [ ] **Step 6: Comprobar**

Run: `.venv/bin/pytest -q tests/test_email_intake_intake.py tests/test_email_intake_reply_to.py tests/test_email_intake_api.py`
Expected: PASS. Luego la suite completa, `mypy`, `ruff`.

- [ ] **Step 7: Commit**

```bash
git add backend docs/modules/email-intake/spec.md
git commit -m "feat(email-intake): every message belongs to a client: contact, then domain, then Unassigned (phase 2 Task 4)"
```

---

### Task 5: Personas

Investigación: research-2 (§A, §B "People", §C, §D gateway, §E1-E2, §E7-E9, §G, §H, §I1-I6).

**Files:**
- Create: `backend/migrations/versions/0235_client_scope_people.py`
- Modify: `backend/app/modules/people/models.py` (`Person.client_id` C, `client_org_fk("person")`, `UniqueConstraint("id", "client_id", name="uq_person_id_client")`, claves por cliente; `client_id` H en `PersonLogin`, `PersonChecklist`, `PersonChecklistStep`; la clave compuesta `fk_device_person_person_client` en `DevicePerson`)
- Modify: `backend/app/modules/people/service.py` (`_CREATE_FOR_ACCOUNTS`, `_LINK_BY_EMAIL`, `_person_with_email`, `person_id_for_email`, `create_person`, `link_account`, `resolve_login`, `observe_login`, `set_device_person`, `link_seat`), `checklists.py`, `detail.py:190`, `api.py:69-71`, `schema.py`
- Modify: `backend/app/api/v1/devices.py:403` (quién ve el titular de cada equipo)
- Modify: `backend/app/core/client_scope_catalog.py` (las cuatro tablas salen de `PENDING_PHASE_2`)
- Modify: `backend/tests/test_client_catalog.py` (`PER_CLIENT_KEYS`, `ORG_WIDE_KEYS`), `backend/tests/test_client_isolation.py` (`PATHS`)
- Modify: las pruebas de research-2 §H que construyen `Person(` o llaman `create_person(`
- Modify: `frontend/src/lib/api.ts` (`createPerson`), `frontend/src/modules/people/links.tsx:252`, `frontend/src/modules/people/types.ts`, `frontend/src/modules/people/api.ts:196-205` (desactivar el acceso de un contacto)
- Test: `backend/tests/test_client_scope_people.py`

**Interfaces:**
- Produces: `person.client_id` C, inmutable en la práctica (ningún esquema de actualización lo lleva, y las claves compuestas lo fijan); `uq_person_id_client` (el destino de las claves compuestas de las Tasks 5 y 6).
- Produces: `create_person(session, org_id, actor_id, data)` con `data.client_id: UUID` obligatorio.
- Produces: `resolve_login(session, org_id, login, *, client_id: UUID)`; `person_id_for_email(session, org_id, email, *, client_id: UUID)`.
- Produces: la regla del titular: la persona de un usuario `contact` es de su cliente; la de un `staff`, del cliente interno (C4). La impone el trigger `its_person_account_guard`.

- [ ] **Step 1: Escribir las pruebas que fallan**

```python
# backend/tests/test_client_scope_people.py
"""People belong to a client (plan 2026-10-26 Task 5)."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Device, DevicePerson, Person, User
from tests.client_scope_support import client_with_site, everything_on
from tests.conftest import ApiEnv

API = "/api/v1/people"


async def _admin(env: ApiEnv, org: UUID) -> dict[str, str]:
    return await env.headers(org, await env.create_user(org, "org-admin"))


async def _device(env: ApiEnv, org: UUID, client: UUID, site: UUID) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        d = Device(org_id=org, client_id=client, site_id=site, hostname="pc", os_family="windows")
        s.add(d)
        await s.commit()
        return d.id


async def test_a_person_is_created_in_the_stated_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("p-create")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    r = await api_env.client.post(
        API, json={"display_name": "Ana", "client_id": str(x)}, headers=admin
    )
    assert r.status_code == 201, r.text
    assert r.json()["client_id"] == str(x)
    missing = await api_env.client.post(API, json={"display_name": "Sin"}, headers=admin)
    assert missing.status_code == 422


async def test_a_person_of_y_cannot_hold_a_device_of_x(api_env: ApiEnv) -> None:
    org = await api_env.create_org("p-holder")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    y_person = (
        await api_env.client.post(API, json={"display_name": "Yo", "client_id": str(y)},
                                  headers=admin)
    ).json()["id"]
    device = await _device(api_env, org, x, x_site)
    r = await api_env.client.put(
        f"{API}/devices/{device}/override", json={"person_id": y_person}, headers=admin
    )
    assert r.status_code == 422, r.text
    with pytest.raises(DBAPIError, match="fk_device_person_person_client"):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            s.add(DevicePerson(org_id=org, device_id=device, person_id=UUID(y_person)))
            await s.flush()


async def test_a_staff_account_s_person_is_the_internal_client_s(api_env: ApiEnv) -> None:
    """C4. `GET /people` creates the people of accounts that have none
    (`sync_accounts`)."""
    org = await api_env.create_org("p-staff")
    admin = await _admin(api_env, org)
    assert (await api_env.client.get(API, headers=admin)).status_code == 200
    async with AsyncSession(api_env.admin_engine) as s:
        clients = set(
            (
                await s.execute(
                    select(Person.client_id).join(User, User.id == Person.user_id)
                    .where(Person.org_id == org, User.kind == "staff")
                )
            ).scalars()
        )
    assert clients == {org}


async def test_listing_people_as_a_technician_of_one_client_does_not_fail(
    api_env: ApiEnv,
) -> None:
    """research-2 §I1: `sync_accounts` runs inside GET /people. A staff
    account's person belongs to the internal client, which a technician
    limited to X cannot see; the insert must skip it, not raise."""
    org = await api_env.create_org("p-sync")
    x, _ = await client_with_site(api_env, org)
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.get(API, headers=x_tech)
    assert r.status_code == 200, r.text


async def test_the_same_login_in_two_clients_resolves_by_the_device_s_client(
    api_env: ApiEnv,
) -> None:
    from app.modules.people.service import resolve_login

    org = await api_env.create_org("p-login")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    ids = {}
    for client in (x, y):
        person = (
            await api_env.client.post(
                API, json={"display_name": "Juan", "client_id": str(client)}, headers=admin
            )
        ).json()["id"]
        r = await api_env.client.post(f"{API}/{person}/logins", json={"login": "jperez"},
                                      headers=admin)
        assert r.status_code == 201, r.text
        ids[client] = UUID(person)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        found = await resolve_login(s, org, "jperez", client_id=y)
    assert found == ids[y]


async def test_offboarding_a_person_needs_the_permission_in_their_client_only(
    api_env: ApiEnv,
) -> None:
    """T4-C2 (`people/api.py:69-71`): a leaver checklist was refused to
    anyone without people.offboard on EVERY client."""
    org = await api_env.create_org("p-offboard")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    person = (
        await api_env.client.post(API, json={"display_name": "Sale", "client_id": str(x)},
                                  headers=admin)
    ).json()["id"]
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.post(
        f"{API}/{person}/checklists", json={"kind": "leaver"}, headers=x_tech
    )
    assert r.status_code == 201, r.text
```

Ajusta los cuerpos (`display_name`, `kind`, la ruta de la cuenta del equipo)
a los de `people/schema.py` y `people/api.py` si difieren: las aserciones no
cambian.

- [ ] **Step 2: Comprobar que fallan**

Run: `.venv/bin/pytest -q tests/test_client_scope_people.py`
Expected: FAIL (`person` no tiene `client_id`; el técnico de X recibe 403 al dar de baja).

- [ ] **Step 3: La migración 0235**

Empieza con el **Bloque común de migración**, y luego:

```python
"""People belong to a client (plan 2026-10-26 Task 5).

`person` is C. A person tied to an account follows the account: a contact's
person is its client's, a staff member's is the internal client's (C4) --
`its_person_account_guard` holds that for every later write. Its logins and
checklists are H.

`device_person` (phase 1, H from devices) also names a person, and the two
must agree: `(person_id, client_id) REFERENCES person (id, client_id)`, the
0231 technique for sites -- a foreign key ignores RLS, and a NULL person_id
skips the check. Existing links that disagree are UNLINKED (person_id set to
NULL) before the key is added: there is no data to keep (spec P7), and the
alternative is a migration that fails on a populated database.

Natural keys become per client: a person's e-mail and a login are unique
within their client. `person.user_id` stays unique across the organisation
(an account is one person), named in `ORG_WIDE_KEYS`.

Revision ID: 0235
Revises: 0234
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0235"
down_revision: str | None = "0234"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (bloque común aquí)

PERSON_BACKFILL = (
    "COALESCE((SELECT u.client_id FROM users u "
    "WHERE u.id = person.user_id AND u.kind = 'contact'), person.org_id)"
)


def upgrade() -> None:
    _add_c("person", PERSON_BACKFILL)
    _add_h("person_login", "person", "person_id")
    _add_h("person_checklist", "person", "person_id")
    _add_h("person_checklist_step", "person_checklist", "checklist_id")

    op.execute("ALTER TABLE person ADD CONSTRAINT uq_person_id_client UNIQUE (id, client_id)")
    op.execute(
        "UPDATE device_person dp SET person_id = NULL FROM person p "
        "WHERE p.id = dp.person_id AND p.client_id <> dp.client_id"
    )
    op.execute(
        "ALTER TABLE device_person ADD CONSTRAINT fk_device_person_person_client "
        "FOREIGN KEY (person_id, client_id) REFERENCES person (id, client_id)"
    )

    op.execute("DROP INDEX uq_person_org_lower_email")
    op.execute(
        "CREATE UNIQUE INDEX uq_person_client_lower_email "
        "ON person (org_id, client_id, lower(email)) WHERE email IS NOT NULL"
    )
    op.execute("ALTER TABLE person_login DROP CONSTRAINT uq_person_login_org_login")
    op.execute(
        "ALTER TABLE person_login ADD CONSTRAINT uq_person_login_client_login "
        "UNIQUE (org_id, client_id, login)"
    )

    op.execute(
        """
        CREATE FUNCTION its_person_account_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE u_kind text; u_client uuid;
        BEGIN
            IF NEW.user_id IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT kind, client_id INTO u_kind, u_client FROM users WHERE id = NEW.user_id;
            IF u_kind = 'contact' AND u_client IS DISTINCT FROM NEW.client_id THEN
                RAISE EXCEPTION 'a contact''s person belongs to the contact''s client'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF u_kind = 'staff' AND NEW.client_id IS DISTINCT FROM NEW.org_id THEN
                RAISE EXCEPTION 'a staff member''s person belongs to the internal client'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER its_person_account_guard BEFORE INSERT OR UPDATE OF user_id, client_id "
        "ON person FOR EACH ROW EXECUTE FUNCTION its_person_account_guard()"
    )
    for table in ("person", "person_login", "person_checklist", "person_checklist_step"):
        _isolate(table)


def downgrade() -> None:
    op.execute("DROP TRIGGER its_person_account_guard ON person")
    op.execute("DROP FUNCTION its_person_account_guard()")
    op.execute("ALTER TABLE person_login DROP CONSTRAINT uq_person_login_client_login")
    op.execute(
        "ALTER TABLE person_login ADD CONSTRAINT uq_person_login_org_login UNIQUE (org_id, login)"
    )
    op.execute("DROP INDEX uq_person_client_lower_email")
    op.execute(
        "CREATE UNIQUE INDEX uq_person_org_lower_email "
        "ON person (org_id, lower(email)) WHERE email IS NOT NULL"
    )
    op.execute("ALTER TABLE device_person DROP CONSTRAINT fk_device_person_person_client")
    op.execute("ALTER TABLE person DROP CONSTRAINT uq_person_id_client")
    _drop_h("person_checklist_step", "person_checklist")
    _drop_h("person_checklist", "person")
    _drop_h("person_login", "person")
    _drop_c("person")
```

Confirma en `pg_constraint`/`pg_indexes` que `uq_person_org_lower_email` es
un índice y `uq_person_login_org_login` una restricción (research-2 §A dice
eso); si alguno es lo contrario, cambia `DROP INDEX`/`DROP CONSTRAINT`.

- [ ] **Step 4: El código**

- `PersonCreate.client_id: UUID` (obligatorio) y `PersonOut.client_id`. `create_person` carga el cliente con `client_for_new_rows` y rechaza `unassigned` (422, C10). Si trae `user_id`, aplica la regla del titular antes del `INSERT` y responde 422 con frase propia (el trigger es la red).
- `_CREATE_FOR_ACCOUNTS` (`service.py:149-174`): el `INSERT ... SELECT` escribe `client_id = CASE WHEN u.kind = 'contact' THEN u.client_id ELSE u.org_id END` y añade al `WHERE` `its_client_visible(CASE WHEN u.kind = 'contact' THEN u.client_id ELSE u.org_id END)` (la función SQL de 0231): un técnico limitado no intenta crear personas de clientes que no ve, en vez de chocar con la RLS (research-2 §I1).
- `_LINK_BY_EMAIL` (`service.py:134-147`): solo enlaza una cuenta con una persona del cliente que dicta la regla del titular.
- `_person_with_email`, `person_id_for_email` y `resolve_login` reciben `client_id` y lo filtran; todos sus llamadores lo pasan (research-2 §I2, §I6). `observe_login` pasa `device.client_id`.
- `set_device_person` (`service.py:554`): la persona tiene que ser del cliente del equipo; si no, 422 `tr("that person belongs to another client")`.
- `link_seat` (`service.py:728`) crea la persona en `org_id` por ahora, con `# plan 2026-10-26 Task 6 files it under the seat's client`.
- `_require_offboard` (`api.py:69-71`) recibe la persona y llama `has_permission(..., "people.offboard", client_id=person.client_id)`; en `start_checklist` la persona se carga antes de comprobar. `person_detail` (`detail.py:190`) calcula los códigos con `client_id=person.client_id`.
- `app/api/v1/devices.py:403`: en vez de `has_permission(..., "people.read")` sin cliente, `holders = await clients_holding(session, ctx.user_id, "people.read")`, y el titular de un equipo se muestra solo si `holders.allows(device.client_id)`.
- Auditoría de personas (`target_type="person"`): `client_id=person.client_id`.
- `client_scope_catalog.py`: fuera de `PENDING_PHASE_2` `person`, `person_login`, `person_checklist`, `person_checklist_step`.
- `tests/test_client_catalog.py`: `PER_CLIENT_KEYS` gana `uq_person_login_client_login` (`("person_login", ("org_id", "client_id", "login"), False)`) y `uq_person_client_lower_email` con sus columnas como las lee `_unique_keys` (una columna de expresión aparece como `""`); si `test_the_per_client_keys_are_in_the_database_and_on_the_models` no sabe leer una expresión del modelo, enséñale (las columnas de un `Index` con expresión están en `con.expressions`). `ORG_WIDE_KEYS` gana la clave única de `person.user_id` con su nombre real y la razón "an account is one person: one user, one person row, in the client the account belongs to (`its_person_account_guard`)".
- `tests/test_client_isolation.py::PATHS`: `"C-person"` y `"H-person_login-via-person"`.
- Consola: `createPerson` envía `client_id: internalClientId(me)` con `// PHASE 3`; `PersonOut` gana `client_id`. "Quitar el acceso" de una persona cuya cuenta es un contacto llama a `PATCH /contacts/{id}` con `{"status": "disabled"}` (fase 1, Task 9), no a `/users/{id}/disable`.

- [ ] **Step 5: Comprobar**

Run: `.venv/bin/pytest -q tests/test_client_scope_people.py tests/test_people_service.py tests/test_people_api.py tests/test_people_links.py tests/test_devices_assigned_to.py tests/test_client_catalog.py tests/test_client_isolation.py`
Expected: PASS. Luego la suite completa, `mypy`, `ruff`, y `npx vitest run src/modules/people` y `npx tsc -b`.

- [ ] **Step 6: Commit**

```bash
git add backend frontend
git commit -m "feat(clients): people belong to a client; a device's holder is of the device's client (phase 2 Task 5)"
```

---

### Task 6: Licencias y suscripciones

Investigación: research-2 (§A, §B "Licenses" y "Subscriptions", §C, §D, §E3-E6, §G, §H, §I7-I9).

**Files:**
- Create: `backend/migrations/versions/0236_client_scope_licenses.py`
- Modify: `backend/app/modules/licenses/models.py`, `service.py` (`create_license`, `allocate`, `_validate_allocation_target`, `ignore_software`, `review_queue`), `reporting.py` (`compliance_summary`, `/costs`, `/renewals`), `matching.py` (`product_installed_device_ids`), `schema.py`
- Modify: `backend/app/modules/subscriptions/models.py`, `service.py` (`create_subscription`, `assign_seat`, `_license_ref`), `alerts.py`, `reporting.py` (`/spend`), `schema.py`
- Modify: `backend/app/modules/people/service.py:728` (`link_seat` en el cliente del asiento)
- Modify: `backend/app/worker.py:1965-2010` (`licenses_compliance_sweep_all_orgs` por cliente)
- Modify: `backend/app/core/client_scope_catalog.py` (las siete tablas salen de `PENDING_PHASE_2`)
- Modify: `backend/tests/test_client_isolation.py` (`PATHS`), `backend/tests/test_worker_sweep_containment.py:353-358` (el doble de `compliance_summary`)
- Modify: las pruebas de research-2 §H que construyen `License(`/`Subscription(` o llaman `create_license(`/`create_subscription(`
- Modify: `frontend/src/lib/api.ts` (`createLicense`, `createSubscription`), `frontend/src/modules/licenses/LicensesPage.tsx:466`, `frontend/src/modules/subscriptions/SubscriptionsPage.tsx:523`, sus `types.ts` y `fixtures.ts`
- Test: `backend/tests/test_client_scope_licenses.py`

**Interfaces:**
- Consumes: `person.client_id`, `uq_person_id_client`, `person_id_for_email(..., client_id=)` (Task 5).
- Produces: `license.client_id`, `subscription.client_id`, `license_compliance_snapshot.client_id` (C); `license_allocation`, `seat_assignment`, `subscription_event`, `subscription_alert_sent` (H).
- Produces: `compliance_summary(session, org_id, *, client_id: UUID) -> list[dict[str, Any]]`; `ComplianceRow.client_id`.
- Produces: `ReviewIgnoreRequest.client_id: UUID | None` (sin él, la fila es global y exige todos los clientes, como hoy).

- [ ] **Step 1: Escribir las pruebas que fallan**

```python
# backend/tests/test_client_scope_licenses.py
"""Licenses and subscriptions belong to a client (plan 2026-10-26 Task 6)."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Device, InstalledSoftware, LicenseAllocation, SeatAssignment, SoftwareProduct
from app.modules.licenses.reporting import compliance_summary
from tests.client_scope_support import client_with_site, everything_on
from tests.conftest import ApiEnv


async def _admin(env: ApiEnv, org: UUID) -> dict[str, str]:
    return await env.headers(org, await env.create_user(org, "org-admin"))


async def _product(env: ApiEnv, admin: dict[str, str], name: str) -> str:
    r = await env.client.post(
        "/api/v1/licenses/products",
        json={"name": name, "match_rules": [{"field": "name", "op": "equals", "value": name}]},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _license(env: ApiEnv, admin: dict[str, str], product: str, client: UUID, seats: int) -> str:
    r = await env.client.post(
        "/api/v1/licenses",
        json={"product_id": product, "client_id": str(client), "entitlements": seats,
              "name": f"L {uuid4().hex[:4]}"},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _installed(env: ApiEnv, org: UUID, client: UUID, site: UUID, name: str) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        d = Device(org_id=org, client_id=client, site_id=site, hostname="h", os_family="windows")
        s.add(d)
        await s.flush()
        s.add(InstalledSoftware(org_id=org, device_id=d.id, name=name, version="1"))
        await s.commit()
        return d.id


async def test_a_license_is_its_client_s_and_needs_one(api_env: ApiEnv) -> None:
    org = await api_env.create_org("l-create")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    product = await _product(api_env, admin, "Office")
    license_id = await _license(api_env, admin, product, x, 5)
    got = await api_env.client.get(f"/api/v1/licenses/{license_id}", headers=admin)
    assert got.json()["client_id"] == str(x)
    missing = await api_env.client.post(
        "/api/v1/licenses", json={"product_id": product, "entitlements": 1, "name": "n"},
        headers=admin,
    )
    assert missing.status_code == 422


async def test_an_allocation_cannot_name_another_client_s_device(api_env: ApiEnv) -> None:
    org = await api_env.create_org("l-alloc")
    x, _ = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    product = await _product(api_env, admin, "Visio")
    license_id = await _license(api_env, admin, product, x, 5)
    y_device = await _installed(api_env, org, y, y_site, "Visio")
    r = await api_env.client.post(
        f"/api/v1/licenses/{license_id}/allocations",
        json={"target_type": "device", "target_id": str(y_device)}, headers=admin,
    )
    assert r.status_code == 422, r.text
    with pytest.raises(DBAPIError, match="allocation target belongs to another client"):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            s.add(LicenseAllocation(org_id=org, license_id=UUID(license_id),
                                    target_type="device", target_id=y_device))
            await s.flush()


async def test_compliance_is_counted_per_client(api_env: ApiEnv) -> None:
    """Two clients, one product: X holds 1 seat and installs it twice (a
    breach); Y holds 5 and installs it once. An org-wide sum (6 vs 3) would
    hide X's breach. Under scope all: the code's predicate is tested."""
    org = await api_env.create_org("l-compliance")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    product = await _product(api_env, admin, "Acrobat")
    await _license(api_env, admin, product, x, 1)
    await _license(api_env, admin, product, y, 5)
    for _ in range(2):
        await _installed(api_env, org, x, x_site, "Acrobat")
    await _installed(api_env, org, y, y_site, "Acrobat")
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        for_x = await compliance_summary(s, org, client_id=x)
        for_y = await compliance_summary(s, org, client_id=y)
    row_x = next(r for r in for_x if str(r["product_id"]) == product)
    row_y = next(r for r in for_y if str(r["product_id"]) == product)
    assert (row_x["entitled"], row_x["installed"], row_x["breach"]) == (1, 2, True)
    assert (row_y["entitled"], row_y["installed"], row_y["breach"]) == (5, 1, False)


async def test_a_seat_by_email_finds_the_person_in_the_subscription_s_client(
    api_env: ApiEnv,
) -> None:
    org = await api_env.create_org("s-seat")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    people = {}
    for client in (x, y):
        r = await api_env.client.post(
            "/api/v1/people",
            json={"display_name": "Eva", "email": "eva@shared.test", "client_id": str(client)},
            headers=admin,
        )
        people[client] = r.json()["id"]
    sub = await api_env.client.post(
        "/api/v1/subscriptions",
        json={"name": "M365", "client_id": str(y), "seats": 10, "billing_period": "monthly"},
        headers=admin,
    )
    assert sub.status_code == 201, sub.text
    seat = await api_env.client.post(
        f"/api/v1/subscriptions/{sub.json()['id']}/seats",
        json={"assignee_name": "Eva", "assignee_email": "eva@shared.test"}, headers=admin,
    )
    assert seat.status_code == 201, seat.text
    async with AsyncSession(api_env.admin_engine) as s:
        person = (
            await s.execute(select(SeatAssignment.person_id).where(
                SeatAssignment.id == UUID(seat.json()["id"])))
        ).scalar_one()
    assert str(person) == people[y]


async def test_ignoring_a_name_for_x_does_not_hide_it_for_y(api_env: ApiEnv) -> None:
    from app.modules.licenses.service import review_queue

    org = await api_env.create_org("l-ignore")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    await _installed(api_env, org, x, x_site, "Toolbar")
    await _installed(api_env, org, y, y_site, "Toolbar")
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.post(
        "/api/v1/licenses/review-queue/ignore", json={"name": "Toolbar", "client_id": str(x)},
        headers=x_tech,
    )
    assert r.status_code in (200, 201), r.text
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        queue = await review_queue(s, org)
    toolbar = [row for row in queue if row["name"] == "Toolbar"]
    assert toolbar and toolbar[0]["device_count"] == 1
```

Ajusta los cuerpos a los esquemas reales (`licenses/schema.py`,
`subscriptions/schema.py`, `LicenseProduct`/`match_rules`) si difieren; las
aserciones no cambian. Si `review_queue` agrupa por cliente después de esta
tarea, la aserción pasa a "una fila de Y y ninguna de X".

- [ ] **Step 2: Comprobar que fallan**

Run: `.venv/bin/pytest -q tests/test_client_scope_licenses.py`
Expected: FAIL.

- [ ] **Step 3: La migración 0236**

Empieza con el **Bloque común de migración**, y luego:

```python
"""Licenses and subscriptions belong to a client (plan 2026-10-26 Task 6).

`license`, `subscription` and `license_compliance_snapshot` are C (the
snapshot is per client x product, with no license to inherit from); the rest
are H. Two second links must agree with the row's client:

* a seat's and an allocation's `person_id`: `(person_id, client_id)
  REFERENCES person (id, client_id)` (Task 5's `uq_person_id_client`).
  Existing rows that disagree are unlinked first (spec P7);
* an allocation's polymorphic target (`target_type` + `target_id`, no FK
  possible): `its_license_allocation_target_guard`, SECURITY INVOKER -- a
  target the session cannot see reads as NULL and is refused. A `user`
  target is a contact of the license's client, or a staff member holding
  an internal-client license (C4).

`subscription.license_ref` stays advisory (subscriptions spec §143-148): the
application checks it, no key.

Revision ID: 0236
Revises: 0235
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0236"
down_revision: str | None = "0235"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (bloque común aquí)

H = [
    ("license_allocation", "license", "license_id"),
    ("seat_assignment", "subscription", "subscription_id"),
    ("subscription_event", "subscription", "subscription_id"),
    ("subscription_alert_sent", "subscription", "subscription_id"),
]
PERSON_LINKED = ["license_allocation", "seat_assignment"]


def upgrade() -> None:
    for table in ("license", "subscription", "license_compliance_snapshot"):
        _add_c(table)
    for table, parent, fk in H:
        _add_h(table, parent, fk)
    for table in PERSON_LINKED:
        op.execute(
            f"UPDATE {table} t SET person_id = NULL FROM person p "
            "WHERE p.id = t.person_id AND p.client_id <> t.client_id"
        )
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_person_client "
            "FOREIGN KEY (person_id, client_id) REFERENCES person (id, client_id)"
        )
    op.execute(
        """
        CREATE FUNCTION its_license_allocation_target_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE t_client uuid; t_kind text; t_org uuid;
        BEGIN
            IF NEW.target_type = 'device' THEN
                SELECT client_id INTO t_client FROM devices WHERE id = NEW.target_id;
            ELSIF NEW.target_type = 'person' THEN
                SELECT client_id INTO t_client FROM person WHERE id = NEW.target_id;
            ELSIF NEW.target_type = 'user' THEN
                SELECT kind, client_id, org_id INTO t_kind, t_client, t_org
                    FROM users WHERE id = NEW.target_id;
                IF t_kind = 'staff' THEN
                    t_client := t_org;  -- a staff member holds the internal client's licenses
                END IF;
            END IF;
            IF t_client IS DISTINCT FROM NEW.client_id THEN
                RAISE EXCEPTION 'allocation target belongs to another client'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER its_license_allocation_target_guard BEFORE INSERT OR UPDATE OF "
        "target_type, target_id, client_id ON license_allocation "
        "FOR EACH ROW EXECUTE FUNCTION its_license_allocation_target_guard()"
    )
    for table in ("license", "subscription", "license_compliance_snapshot", *[t for t, _, _ in H]):
        _isolate(table)


def downgrade() -> None:
    op.execute("DROP TRIGGER its_license_allocation_target_guard ON license_allocation")
    op.execute("DROP FUNCTION its_license_allocation_target_guard()")
    for table in PERSON_LINKED:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT fk_{table}_person_client")
    for table, parent, _ in reversed(H):
        _drop_h(table, parent)
    for table in ("license_compliance_snapshot", "subscription", "license"):
        _drop_c(table)
```

El trigger de herencia de `license_allocation` corre antes que el guarda
(los dos son `BEFORE`, y Postgres los ordena por nombre:
`its_client_from_parent` < `its_license_allocation_target_guard`), así que el
guarda ve el cliente ya copiado de la licencia. Compruébalo en la prueba
del Step 1, que inserta sin `client_id`.

- [ ] **Step 4: El código**

- `LicenseCreate.client_id: UUID` y `SubscriptionCreate.client_id: UUID` obligatorios, cargados con `client_for_new_rows` y rechazando `unassigned` (422). `LicenseOut`/`SubscriptionOut` ganan `client_id`.
- `_validate_allocation_target` (`licenses/service.py:559-589`): el objetivo tiene que ser del cliente de la licencia, con la misma regla del trigger; si no, 422 `tr("the allocation target belongs to another client")`. La persona de `person_id` también.
- `subscriptions/service.py::_license_ref` (`:137-148`): la licencia referida tiene que ser del cliente de la suscripción (422). `assign_seat` busca la persona con `person_id_for_email(..., client_id=subscription.client_id)`; una `person_id` explícita de otro cliente es 422. `people/service.py::link_seat` crea la persona en `seat.client_id` (quita el comentario de la Task 5).
- `compliance_summary(session, org_id, *, client_id)` y `product_installed_device_ids(..., client_id=...)` filtran licencias y equipos por ese cliente con predicado explícito. `ComplianceRow` gana `client_id`. La ruta `/licenses/compliance` devuelve una fila por (cliente, producto) de los clientes del alcance: itera `Client` visibles (activos) y llama a `compliance_summary` por cada uno. Igual `/costs`, `/renewals` y `/subscriptions/spend`: cada fila lleva su `client_id` y nada se suma entre clientes.
- `worker.py::licenses_compliance_sweep_all_orgs`: por organización, por cliente activo, por producto: una fila de `LicenseComplianceSnapshot` con `client_id`. Ajusta el doble de `test_worker_sweep_containment.py:353-358` a la firma nueva.
- `alerts.py`: `publish(..., client_id=sub.client_id)` (quita el `None  # ... Task 6`); `_claim` no envía cliente (H).
- `software_ignore` (fase 1, O): `ReviewIgnoreRequest` gana `client_id: UUID | None = None`. Con él: cargado con `client_for_new_rows`, y la fila es de ese cliente (un técnico de X puede ignorar para X). Sin él: global, y la guarda de hoy (todos los clientes). `ignore_software` inserta con `on_conflict_do_nothing(constraint="uq_software_ignore_client_name")` en vez de buscar y luego insertar (research-2 §I9: la carrera da 500). `review_queue` esconde un nombre en un equipo solo si hay una fila global o una del cliente de ese equipo: el `NOT EXISTS` compara `software_ignore.client_id IS NULL OR software_ignore.client_id = installed_software.client_id`.
- Auditoría con `target_type` `license`, `subscription`, `allocation`, `seat`: `client_id=` del registro.
- `client_scope_catalog.py`: fuera de `PENDING_PHASE_2` `license`, `license_allocation`, `license_compliance_snapshot`, `subscription`, `seat_assignment`, `subscription_event`, `subscription_alert_sent`.
- `tests/test_client_isolation.py::PATHS`: `"C-license"`, `"C-subscription"`, `"H-subscription_event-via-subscription"`.
- Consola: `createLicense` y `createSubscription` envían `client_id: internalClientId(me)` con `// PHASE 3`; las vistas de cumplimiento, costes y renovaciones muestran el cliente de cada fila (una columna "Cliente" con el nombre de `GET /clients`).

- [ ] **Step 5: Comprobar**

Run: `.venv/bin/pytest -q tests/test_client_scope_licenses.py tests/test_licenses_service.py tests/test_licenses_allocation.py tests/test_licenses_api.py tests/test_licenses_sweep.py tests/test_subscriptions_service.py tests/test_subscriptions_seats.py tests/test_subscriptions_api.py tests/test_subscriptions_alerts.py tests/test_worker_sweep_containment.py tests/test_client_isolation.py tests/test_client_catalog.py`
Expected: PASS. Luego la suite completa, `mypy`, `ruff`, `npx vitest run src/modules/licenses src/modules/subscriptions`, `npx tsc -b`.

- [ ] **Step 6: Commit**

```bash
git add backend frontend
git commit -m "feat(clients): licenses and subscriptions belong to a client; compliance is counted per client (phase 2 Task 6)"
```

---

### Task 7: Módulos opcionales — la guarda por cliente, gestión de cambios y continuidad (BCP)

Investigación: research-5 (§A change/bcp, §B "BCP" y "Change", §C, §D `gates_for`, §E5-E7, §I "Optional-module gating", §I1, §I5).

**Files:**
- Create: `backend/app/services/client_modules.py`
- Create: `backend/migrations/versions/0237_client_scope_change_bcp.py`
- Modify: `backend/app/modules/change_mgmt/models.py`, `service.py` (`create_change`), `links.py` (`_LINK_RESOLVERS`), `gates.py` (`gates_for`, `patch_approval_gate`, `deployment_gate`, `record_auto_change`, `set_gate`), `api.py`, `schema.py`
- Modify: `backend/app/modules/bcp/models.py`, `service.py` (`create_plan`, `_validate_scope`, `_validate_procedure_refs`, findings' `change_ref`), `api.py`, `schema.py`
- Modify: `backend/app/modules/service_desk/adapters.py` (el ticket de un cambio va al cliente del cambio)
- Modify: `backend/app/modules/patch/apply.py:126,189`, `backend/app/modules/patch/api.py:1065`, `backend/app/modules/deploy/api.py:618`, `backend/app/modules/deploy/dispatch.py:78`
- Modify: `backend/app/core/client_scope_catalog.py` (las ocho tablas salen de `PENDING_PHASE_2`)
- Modify: las pruebas `test_change_mgmt_*`, `test_bcp_*`, `test_service_desk_adapter.py` (filas con `client_id=org_id`)
- Modify: `frontend/src/lib/api.ts` (`createChange`, `createBcpPlan`), `frontend/src/modules/change-mgmt/ChangeRequestForm.tsx:185`, `frontend/src/modules/bcp/PlanEditor.tsx:262`, sus tipos
- Test: `backend/tests/test_client_modules.py`

**Interfaces:**
- Produces: `class ModuleNotEnabled(LookupError)` (404) y `async def require_module(session, *, org_id: UUID, client_id: UUID, module: Literal["budget", "bcp", "change"]) -> None`; `async def module_enabled(session, *, org_id, client_id, module) -> bool`. La Task 8 los usa.
- Produces: `change_request.client_id`, `bcp_plan.client_id` (C) y sus hijas H.
- Produces: `gates_for(session, org_id, kind, *, client_id: UUID) -> Gates` (D4: sin el módulo, ninguna compuerta); `record_auto_change(..., client_id: UUID) -> ChangeRequest | None` (`None` cuando el cliente no tiene el módulo).

- [ ] **Step 1: Escribir las pruebas que fallan**

```python
# backend/tests/test_client_modules.py
"""Optional modules per client (spec §1.4, §3.2; plan 2026-10-26 Task 7 and
D4): a write in budget, BCP or change management for a client without the
module is a 404; a client without change management meets no change gate and
gets no automatic change."""

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.main import create_app
from app.models import ChangeGate
from app.modules.change_mgmt.gates import gates_for
from tests.client_scope_support import client_with_site
from tests.conftest import ApiEnv


async def _admin(env: ApiEnv, org: UUID) -> dict[str, str]:
    return await env.headers(org, await env.create_user(org, "org-admin"))


async def _modules(env: ApiEnv, admin: dict[str, str], client: UUID, *modules: str) -> None:
    r = await env.client.patch(
        f"/api/v1/clients/{client}", json={"enabled_modules": list(modules)}, headers=admin
    )
    assert r.status_code == 200, r.text


async def test_a_change_for_a_client_without_the_module_is_a_404(api_env: ApiEnv) -> None:
    org = await api_env.create_org("m-change")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    body = {"title": "Cambiar firewall", "client_id": str(x), "risk": "low",
            "affected": [], "plan": "p", "rollback": "r"}
    off = await api_env.client.post("/api/v1/changes", json=body, headers=admin)
    assert off.status_code == 404, off.text
    await _modules(api_env, admin, x, "change")
    on = await api_env.client.post("/api/v1/changes", json=body, headers=admin)
    assert on.status_code == 201, on.text
    assert on.json()["client_id"] == str(x)


async def test_a_bcp_plan_for_a_client_without_the_module_is_a_404(api_env: ApiEnv) -> None:
    org = await api_env.create_org("m-bcp")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    body = {"name": "Caída del ERP", "client_id": str(x), "scope": [], "rto_minutes": 60}
    assert (await api_env.client.post("/api/v1/bcp/plans", json=body, headers=admin)).status_code == 404
    await _modules(api_env, admin, x, "bcp")
    created = await api_env.client.post("/api/v1/bcp/plans", json=body, headers=admin)
    assert created.status_code == 201, created.text


async def test_the_internal_client_has_every_optional_module(api_env: ApiEnv) -> None:
    org = await api_env.create_org("m-internal")
    admin = await _admin(api_env, org)
    body = {"title": "Interno", "client_id": str(org), "risk": "low",
            "affected": [], "plan": "p", "rollback": "r"}
    r = await api_env.client.post("/api/v1/changes", json=body, headers=admin)
    assert r.status_code == 201, r.text


async def test_turning_a_module_off_refuses_every_write_on_its_rows(api_env: ApiEnv) -> None:
    """Every write route of the change and BCP routers, on rows that exist
    from before the module was turned off. The route list is read from the
    app, so a new write route without a case here fails."""
    org = await api_env.create_org("m-off")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _modules(api_env, admin, x, "change", "bcp")
    change = (await api_env.client.post(
        "/api/v1/changes",
        json={"title": "t", "client_id": str(x), "risk": "low", "affected": [],
              "plan": "p", "rollback": "r"},
        headers=admin,
    )).json()["id"]
    plan = (await api_env.client.post(
        "/api/v1/bcp/plans",
        json={"name": "n", "client_id": str(x), "scope": [], "rto_minutes": 60},
        headers=admin,
    )).json()["id"]
    test = (await api_env.client.post(
        f"/api/v1/bcp/plans/{plan}/tests", json={"kind": "tabletop"}, headers=admin
    )).json()["id"]
    finding = (await api_env.client.post(
        f"/api/v1/bcp/tests/{test}/findings", json={"summary": "s", "severity": "low"},
        headers=admin,
    )).json()["id"]
    await _modules(api_env, admin, x)

    cases: dict[tuple[str, str], tuple[str, dict[str, Any] | None]] = {
        ("POST", "/api/v1/changes"): ("/api/v1/changes", {
            "title": "t", "client_id": str(x), "risk": "low", "affected": [],
            "plan": "p", "rollback": "r"}),
        ("PATCH", "/api/v1/changes/{change_id}"): (f"/api/v1/changes/{change}", {"title": "u"}),
        ("POST", "/api/v1/changes/{change_id}/submit"): (f"/api/v1/changes/{change}/submit", None),
        ("POST", "/api/v1/changes/{change_id}/approve"): (f"/api/v1/changes/{change}/approve", {}),
        ("POST", "/api/v1/changes/{change_id}/reject"): (f"/api/v1/changes/{change}/reject", {"reason": "r"}),
        ("POST", "/api/v1/changes/{change_id}/transition"): (
            f"/api/v1/changes/{change}/transition", {"to": "in_progress"}),
        ("POST", "/api/v1/changes/{change_id}/links"): (
            f"/api/v1/changes/{change}/links", {"action_type": "patch_run", "action_id": str(x)}),
        ("DELETE", "/api/v1/changes/{change_id}/links/{link_id}"): (
            f"/api/v1/changes/{change}/links/{x}", None),
        ("POST", "/api/v1/bcp/plans"): ("/api/v1/bcp/plans", {
            "name": "n", "client_id": str(x), "scope": [], "rto_minutes": 60}),
        ("PATCH", "/api/v1/bcp/plans/{plan_id}"): (f"/api/v1/bcp/plans/{plan}", {"name": "m"}),
        ("POST", "/api/v1/bcp/plans/{plan_id}/review"): (f"/api/v1/bcp/plans/{plan}/review", {}),
        ("POST", "/api/v1/bcp/plans/{plan_id}/tests"): (
            f"/api/v1/bcp/plans/{plan}/tests", {"kind": "tabletop"}),
        ("PATCH", "/api/v1/bcp/tests/{test_id}"): (f"/api/v1/bcp/tests/{test}", {"notes": "n"}),
        ("PUT", "/api/v1/bcp/tests/{test_id}/steps"): (f"/api/v1/bcp/tests/{test}/steps", {"steps": []}),
        ("POST", "/api/v1/bcp/tests/{test_id}/findings"): (
            f"/api/v1/bcp/tests/{test}/findings", {"summary": "s", "severity": "low"}),
        ("PATCH", "/api/v1/bcp/findings/{finding_id}"): (
            f"/api/v1/bcp/findings/{finding}", {"summary": "t"}),
    }
    # Gates are not per-module writes: a gate is configuration on a group
    # (or global), handled by D4 at evaluation time.
    exempt = {("PUT", "/api/v1/changes/gates"), ("DELETE", "/api/v1/changes/gates/{gate_id}")}
    routes = {
        (method, route.path)
        for route in create_app().routes
        for method in getattr(route, "methods", set())
        if method in {"POST", "PUT", "PATCH", "DELETE"}
        and (route.path.startswith("/api/v1/changes") or route.path.startswith("/api/v1/bcp"))
    }
    assert routes - exempt == set(cases), sorted((routes - exempt) ^ set(cases))
    for (method, _), (url, body) in cases.items():
        r = await api_env.client.request(method, url, json=body, headers=admin)
        assert r.status_code == 404, (method, url, r.status_code, r.text)


async def test_a_client_without_change_management_meets_no_gate(api_env: ApiEnv) -> None:
    """D4: a global "every device" gate does not stop a client that does not
    use change management -- it could never create the change it would need."""
    org = await api_env.create_org("m-gate")
    x, _ = await client_with_site(api_env, org)
    async with AsyncSession(api_env.admin_engine) as s:
        s.add(ChangeGate(org_id=org, device_group_id=None, applies_to_patches=True,
                         applies_to_deployments=True))
        await s.commit()
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        for_x = await gates_for(s, org, "deployments", client_id=x)
        for_internal = await gates_for(s, org, "deployments", client_id=org)
    assert not for_x.any
    assert for_internal.everywhere


async def test_a_client_gate_without_a_group_reaches_only_that_client(api_env: ApiEnv) -> None:
    """research-5 §D: `everywhere` was any NULL group -- a client's own
    group-less gate would have gated every client."""
    org = await api_env.create_org("m-gate-client")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    for client in (x, y):
        await _modules(api_env, admin, client, "change")
    async with AsyncSession(api_env.admin_engine) as s:
        s.add(ChangeGate(org_id=org, client_id=x, device_group_id=None,
                         applies_to_patches=True, applies_to_deployments=True))
        await s.commit()
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        assert (await gates_for(s, org, "deployments", client_id=x)).everywhere
        assert not (await gates_for(s, org, "deployments", client_id=y)).any
```

Los cuerpos (`title`, `risk`, `kind`, `severity`, `to`...) son los de
`change_mgmt/schema.py` y `bcp/schema.py`: ajústalos si difieren, y deja que
la prueba de cobertura de rutas mande. Un caso cuya respuesta, con el
módulo encendido, ya sería 404 o 422 por otra razón no demuestra nada: para
cada caso, comprueba una vez a mano que con el módulo encendido no da 404.

- [ ] **Step 2: Comprobar que fallan**

Run: `.venv/bin/pytest -q tests/test_client_modules.py`
Expected: FAIL.

- [ ] **Step 3: La guarda**

```python
# backend/app/services/client_modules.py
"""Optional modules per client (MSP clients spec §1.4, §3.2; plan
2026-10-26 Task 7). Budget, BCP and change management are on or off per
client (`clients.enabled_modules`). A WRITE for a client without the module
is a 404 -- the module does not exist for that client, the same answer as a
client outside the scope. Reads need nothing: that client simply has no
rows (spec §3.2).

Called in the SERVICE, after the row's client is known -- from the body on a
create, from the loaded parent on every other write -- because a FastAPI
dependency runs before the parent row is read (research-5 §I)."""

from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import tr
from app.models import Client

Module = Literal["budget", "bcp", "change"]


class ModuleNotEnabled(LookupError):
    """A 404 whose `detail` is the sentence."""

    def __init__(self, module: Module) -> None:
        super().__init__(module)
        self.detail = tr("this client does not use that module")


async def module_enabled(
    session: AsyncSession, *, org_id: UUID, client_id: UUID, module: Module
) -> bool:
    modules = (
        await session.execute(
            select(Client.enabled_modules).where(Client.id == client_id, Client.org_id == org_id)
        )
    ).scalar_one_or_none()
    return modules is not None and module in modules


async def require_module(
    session: AsyncSession, *, org_id: UUID, client_id: UUID, module: Module
) -> None:
    if not await module_enabled(session, org_id=org_id, client_id=client_id, module=module):
        raise ModuleNotEnabled(module)
```

`ModuleNotEnabled` se traduce a 404 con su `detail` en un manejador de
excepciones de `app/main.py`, junto al de `GlobalRowNotWritable`
(`app/main.py:266-272`), para que ningún router tenga que acordarse.

- [ ] **Step 4: La migración 0237**

Empieza con el **Bloque común de migración**, y luego:

```python
"""Change management and BCP belong to a client (plan 2026-10-26 Task 7).

`change_request` and `bcp_plan` are C; their children H. No natural key
changes: `uq_change_link` holds `change_id`, the inheritance fk (research-5
§A). Existing rows go to the internal client, which holds both modules
since 0233.

Revision ID: 0237
Revises: 0236
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0237"
down_revision: str | None = "0236"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (bloque común aquí)

C = ["change_request", "bcp_plan"]
H = [
    ("change_approval", "change_request", "change_id"),
    ("change_link", "change_request", "change_id"),
    ("change_external_ref", "change_request", "change_id"),
    ("bcp_test", "bcp_plan", "plan_id"),
    ("bcp_test_step", "bcp_test", "test_id"),
    ("bcp_finding", "bcp_test", "test_id"),
]


def upgrade() -> None:
    for table in C:
        _add_c(table)
    for table, parent, fk in H:
        _add_h(table, parent, fk)
    for table in [*C, *[t for t, _, _ in H]]:
        _isolate(table)


def downgrade() -> None:
    for table, parent, _ in reversed(H):
        _drop_h(table, parent)
    for table in reversed(C):
        _drop_c(table)
```

- [ ] **Step 5: El código**

Gestión de cambios:
- `ChangeCreate.client_id: UUID` obligatorio; `create_change` lo carga con `client_for_new_rows`, rechaza `unassigned` (422) y llama `require_module(..., module="change")`. `ChangeOut.client_id`.
- Cada escritura sobre un cambio existente (`update`, `submit`, `approve`, `reject`, `transition`, `links`, borrar un enlace) carga el cambio y llama `require_module(..., client_id=change.client_id, module="change")` antes de cambiar nada.
- `links.py::_LINK_RESOLVERS` (`:45-50`): el objetivo tiene que ser del cliente del cambio; una aprobación de parche global (`client_id IS NULL`) se acepta. Otro cliente: 404 (no se ve, o se oculta igual).
- `gates_for(session, org_id, kind, *, client_id)`: si `not await module_enabled(..., client_id=client_id, module="change")`, devuelve `Gates(everywhere=False, group_ids=frozenset())` (D4). Si no, filtra `ChangeGate.client_id.is_(None) | (ChangeGate.client_id == client_id)`; `everywhere` es "hay una fila sin grupo" **de esas**.
- `deployment_gate`: el cliente es el del objetivo (equipo o grupo, leído con el `org_id`). `patch_approval_gate`: la población del anillo puede abarcar varios clientes; agrupa los equipos por cliente y evalúa `gates_for` por cada uno; hay compuerta si alguno toca. `patch/apply.py:189`: `gates_for` por cliente del equipo, con una caché por cliente dentro del barrido.
- `record_auto_change(..., client_id)`: si el cliente no tiene `change`, no escribe nada y devuelve `None` (D4). `deploy/api.py:618` pasa `org_id  # plan 2026-10-26 Task 9 passes the deployment's client`; `patch/api.py:1065` pasa el cliente de la aprobación, o `org_id` si es global (C8).
- `set_gate` (`gates.py:423`): una compuerta con grupo exige `change` en el cliente del grupo (404); sin grupo y sin cliente es global (todos los clientes, como hoy).
- `service_desk/adapters.py:156`: el ticket del cambio se crea con `client_id=change.client_id` (quita el comentario de la Task 2).

Continuidad:
- `PlanCreate.client_id: UUID` obligatorio; `create_plan` carga el cliente, rechaza `unassigned` y llama `require_module(..., module="bcp")`. Las demás escrituras cargan el plan (o la prueba, o el hallazgo, y de ahí el plan) y llaman `require_module` con su cliente.
- `_validate_scope` (`service.py:59-111`): equipos y grupos del cliente del plan; `_validate_procedure_refs` (`:114-131`): artículos globales o del cliente del plan; el `change_ref` de un hallazgo, un cambio del mismo cliente. Fuera de eso, 422.

Común:
- Auditoría y `publish` de cambios y planes: `client_id=` del registro.
- `client_scope_catalog.py`: fuera de `PENDING_PHASE_2` las ocho tablas.
- `tests/test_client_isolation.py::PATHS`: `"C-change_request"`, `"C-bcp_plan"`, `"H-bcp_test_step-via-bcp_test"` (si `Side` necesita un plan y una prueba, los gana).
- Consola: `createChange` y `createBcpPlan` envían `client_id: internalClientId(me)` con `// PHASE 3`; el 404 del módulo apagado se muestra con su frase.

- [ ] **Step 6: Comprobar**

Run: `.venv/bin/pytest -q tests/test_client_modules.py tests/test_change_mgmt_service.py tests/test_change_mgmt_api.py tests/test_change_mgmt_links.py tests/test_bcp_api.py tests/test_bcp_plan_service.py tests/test_service_desk_adapter.py tests/test_patch_apply.py tests/test_deploy_dispatch.py tests/test_client_isolation.py tests/test_client_catalog.py`
Expected: PASS (usa `ls tests | grep -E "change|bcp|patch_apply|deploy_dispatch"` si algún nombre difiere). Luego la suite completa, `mypy`, `ruff`, `npx vitest run src/modules/change-mgmt src/modules/bcp`, `npx tsc -b`.

- [ ] **Step 7: Commit**

```bash
git add backend frontend
git commit -m "feat(clients): optional modules are per client; change management and BCP belong to a client (phase 2 Task 7)"
```

---

### Task 8: Presupuesto por cliente

Investigación: research-5 (§A budget, §B "Budget", §C "Budget", §E4, §I "Optional-module gating", §I2, §H).

**Files:**
- Create: `backend/migrations/versions/0238_client_scope_budget.py`
- Modify: `backend/app/modules/budget/models.py`, `fx.py` (`get_settings`), `rates.py` (`set_base_currency`, `set_fiscal_year_start`, `create_rate`), `refdata.py` (`seed_default_categories`, `create_category`, `create_cost_center`, `list_categories`), `budgets.py`, `expenses.py` (`create_expense`, `_resolve_link`), `feeds.py` (`project_costs`, `materialize`), `reporting.py` (`variance`, `position`, `forecast`, exportaciones), `api.py`, `schema.py`
- Modify: `backend/app/core/client_scope_catalog.py` (las seis tablas salen de `PENDING_PHASE_2`)
- Modify: `backend/tests/test_client_catalog.py` (`PER_CLIENT_KEYS`), las pruebas `test_budget_*` (filas con `client_id=org_id`; las rutas de lectura ganan `client_id`)
- Modify: `frontend/src/lib/api.ts` (`getBudgetSettings`, `updateBudgetSettings`, `createCategory`, `createCostCenter`, `createBudget`, `createExpense`, `materializeExpenses`, `getForecast`), `frontend/src/modules/budget/*` y `frontend/src/modules/settings/BudgetTab.tsx:137`
- Test: `backend/tests/test_client_scope_budget.py`

**Interfaces:**
- Consumes: `require_module`, `module_enabled` (Task 7); `license.client_id`, `subscription.client_id` (Task 6).
- Produces: `budget_settings` con clave primaria `client_id`; `get_settings(session, org_id, client_id) -> BudgetSettings` (pura: no inserta si el cliente no tiene el módulo).
- Produces: `GET/PUT /budget/settings?client_id=` y `GET /budget/forecast?client_id=` y las exportaciones con `client_id` obligatorio en la consulta; `GET /budget/categories`, `/cost-centers`, `/budgets`, `/expenses` con `client_id` opcional como filtro.
- Produces: `FxRateCreate.to_currency: str | None` — sin él, la moneda base del cliente interno.

- [ ] **Step 1: Escribir las pruebas que fallan**

```python
# backend/tests/test_client_scope_budget.py
"""Budget per client (plan 2026-10-26 Task 8)."""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import create_app
from app.models import BudgetSettings, ExpenseCategory
from tests.client_scope_support import client_with_site
from tests.conftest import ApiEnv

B = "/api/v1/budget"


async def _admin(env: ApiEnv, org: UUID) -> dict[str, str]:
    return await env.headers(org, await env.create_user(org, "org-admin"))


async def _budget_on(env: ApiEnv, admin: dict[str, str], client: UUID) -> None:
    r = await env.client.patch(
        f"/api/v1/clients/{client}", json={"enabled_modules": ["budget"]}, headers=admin
    )
    assert r.status_code == 200, r.text


async def test_each_client_has_its_own_base_currency(api_env: ApiEnv) -> None:
    org = await api_env.create_org("b-settings")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _budget_on(api_env, admin, x)
    r = await api_env.client.put(
        f"{B}/settings", params={"client_id": str(x)}, json={"base_currency": "EUR"},
        headers=admin,
    )
    assert r.status_code == 200, r.text
    internal = await api_env.client.get(f"{B}/settings", params={"client_id": str(org)},
                                        headers=admin)
    mine = await api_env.client.get(f"{B}/settings", params={"client_id": str(x)},
                                    headers=admin)
    assert mine.json()["base_currency"] == "EUR"
    assert internal.json()["base_currency"] != "EUR" or internal.json() != mine.json()


async def test_reading_a_client_without_budget_writes_nothing(api_env: ApiEnv) -> None:
    """research-5 §I: `get_settings`, `list_categories`, `variance` and
    `forecast` seeded rows on READ. For a client without the module they
    must not."""
    org = await api_env.create_org("b-pure")
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await api_env.client.get(f"{B}/categories", params={"client_id": str(y)}, headers=admin)
    await api_env.client.get(f"{B}/settings", params={"client_id": str(y)}, headers=admin)
    async with AsyncSession(api_env.admin_engine) as s:
        rows = (
            await s.execute(select(func.count()).select_from(ExpenseCategory)
                            .where(ExpenseCategory.client_id == y))
        ).scalar_one() + (
            await s.execute(select(func.count()).select_from(BudgetSettings)
                            .where(BudgetSettings.client_id == y))
        ).scalar_one()
    assert rows == 0


async def test_a_budget_line_cannot_use_another_client_s_category(api_env: ApiEnv) -> None:
    org = await api_env.create_org("b-line")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    for client in (x, y):
        await _budget_on(api_env, admin, client)
    y_category = (await api_env.client.post(
        f"{B}/categories", json={"client_id": str(y), "key": "hw", "name": "Hardware"},
        headers=admin,
    )).json()["id"]
    budget = (await api_env.client.post(
        f"{B}/budgets", json={"client_id": str(x), "fiscal_year": 2027, "name": "TI"},
        headers=admin,
    )).json()["id"]
    r = await api_env.client.put(
        f"{B}/budgets/{budget}/lines",
        json={"lines": [{"category_id": y_category, "amount_minor": 1000, "period": "2027-01"}]},
        headers=admin,
    )
    assert r.status_code == 422, r.text


async def test_every_budget_write_refuses_a_client_without_the_module(api_env: ApiEnv) -> None:
    """The route list is read from the app (as in tests/test_client_modules.py).
    The FX table is the organisation's (G) and stays every-client config."""
    org = await api_env.create_org("b-off")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _budget_on(api_env, admin, x)
    category = (await api_env.client.post(
        f"{B}/categories", json={"client_id": str(x), "key": "sw", "name": "Software"},
        headers=admin)).json()["id"]
    center = (await api_env.client.post(
        f"{B}/cost-centers", json={"client_id": str(x), "code": "IT", "name": "TI"},
        headers=admin)).json()["id"]
    budget = (await api_env.client.post(
        f"{B}/budgets", json={"client_id": str(x), "fiscal_year": 2027, "name": "TI"},
        headers=admin)).json()["id"]
    lines = (await api_env.client.put(
        f"{B}/budgets/{budget}/lines",
        json={"lines": [{"category_id": category, "amount_minor": 1, "period": "2027-01"}]},
        headers=admin)).json()
    expense = (await api_env.client.post(
        f"{B}/expenses", json={"client_id": str(x), "category_id": category,
                               "amount_minor": 100, "currency": "USD", "date": "2027-01-10",
                               "description": "d"},
        headers=admin)).json()["id"]
    r = await api_env.client.patch(f"/api/v1/clients/{x}", json={"enabled_modules": []},
                                   headers=admin)
    assert r.status_code == 200

    cases: dict[tuple[str, str], tuple[str, Any]] = {
        ("PUT", f"{B}/settings"): (f"{B}/settings?client_id={x}", {"base_currency": "EUR"}),
        ("POST", f"{B}/categories"): (f"{B}/categories", {"client_id": str(x), "key": "k", "name": "n"}),
        ("PATCH", f"{B}/categories/{{category_id}}"): (f"{B}/categories/{category}", {"name": "m"}),
        ("POST", f"{B}/cost-centers"): (f"{B}/cost-centers", {"client_id": str(x), "code": "C", "name": "n"}),
        ("PATCH", f"{B}/cost-centers/{{cost_center_id}}"): (f"{B}/cost-centers/{center}", {"name": "m"}),
        ("POST", f"{B}/budgets"): (f"{B}/budgets", {"client_id": str(x), "fiscal_year": 2028, "name": "n"}),
        ("PATCH", f"{B}/budgets/{{budget_id}}"): (f"{B}/budgets/{budget}", {"name": "m"}),
        ("POST", f"{B}/budgets/{{budget_id}}/close"): (f"{B}/budgets/{budget}/close", None),
        ("PUT", f"{B}/budgets/{{budget_id}}/lines"): (f"{B}/budgets/{budget}/lines", {"lines": []}),
        ("DELETE", f"{B}/budgets/{{budget_id}}/lines/{{line_id}}"): (
            f"{B}/budgets/{budget}/lines/{lines[0]['id']}", None),
        ("POST", f"{B}/expenses/materialize"): (
            f"{B}/expenses/materialize", {"client_id": str(x), "from": "2027-01-01", "to": "2027-01-31"}),
        ("POST", f"{B}/expenses"): (f"{B}/expenses", {
            "client_id": str(x), "category_id": category, "amount_minor": 1, "currency": "USD",
            "date": "2027-01-11", "description": "d"}),
        ("PATCH", f"{B}/expenses/{{expense_id}}"): (f"{B}/expenses/{expense}", {"description": "e"}),
        ("POST", f"{B}/expenses/{{expense_id}}/confirm"): (f"{B}/expenses/{expense}/confirm", None),
        ("POST", f"{B}/expenses/{{expense_id}}/void"): (f"{B}/expenses/{expense}/void", {"reason": "r"}),
        ("POST", f"{B}/expenses/{{expense_id}}/attachment"): (f"{B}/expenses/{expense}/attachment", None),
    }
    global_config = {("POST", f"{B}/fx-rates"), ("PATCH", f"{B}/fx-rates/{{fx_rate_id}}")}
    routes = {
        (m, route.path) for route in create_app().routes for m in getattr(route, "methods", set())
        if m in {"POST", "PUT", "PATCH", "DELETE"} and route.path.startswith(B)
    }
    assert routes - global_config == set(cases), sorted((routes - global_config) ^ set(cases))
    for (method, _), (url, body) in cases.items():
        if url.endswith("/attachment"):
            r = await api_env.client.post(url, files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
                                          headers=admin)
        else:
            r = await api_env.client.request(method, url, json=body, headers=admin)
        assert r.status_code == 404, (method, url, r.status_code, r.text)
```

Ajusta los cuerpos a `budget/schema.py` si difieren (por ejemplo, el nombre
del periodo de una línea o el formato de fechas de `materialize`); la
aserción de cobertura de rutas manda. Como en la Task 7, comprueba una vez
que cada caso no da 404 con el módulo encendido.

- [ ] **Step 2: Comprobar que fallan**

Run: `.venv/bin/pytest -q tests/test_client_scope_budget.py`
Expected: FAIL.

- [ ] **Step 3: La migración 0238**

Empieza con el **Bloque común de migración**, y luego. **Antes de escribir
los `DROP CONSTRAINT`, confirma los nombres** (research-5 marca con "(PG)" los
creados en línea en 0020) con el script de la sección "Nombres de
restricciones creadas sin nombre":

```python
"""Budget per client (plan 2026-10-26 Task 8).

`budget_settings` had the organisation as its primary key: one base currency
and one fiscal year for every client. Its key becomes the client. The other
C tables' natural keys become per client; `budget_line` is H; a line's and an
expense's category and cost centre must be the row's client's, pinned by
`(x_id, client_id) REFERENCES <table> (id, client_id)`.

`fx_rate` stays G: one EUR->USD rate serves every client.

Revision ID: 0238
Revises: 0237
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0238"
down_revision: str | None = "0237"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (bloque común aquí)

C = ["budget_settings", "cost_center", "expense_category", "budget", "expense"]

# (table, old constraint, old definition, new constraint, new definition).
# Old names as `pg_constraint` shows them -- confirm before running.
KEYS = [
    ("cost_center", "cost_center_org_id_code_key", "UNIQUE (org_id, code)",
     "uq_cost_center_client_code", "UNIQUE (org_id, client_id, code)"),
    ("expense_category", "expense_category_org_id_key_key", "UNIQUE (org_id, key)",
     "uq_expense_category_client_key", "UNIQUE (org_id, client_id, key)"),
    ("budget", "budget_org_id_fiscal_year_name_key", "UNIQUE (org_id, fiscal_year, name)",
     "uq_budget_client_year_name", "UNIQUE (org_id, client_id, fiscal_year, name)"),
]


def upgrade() -> None:
    for table in C:
        _add_c(table)
    _add_h("budget_line", "budget", "budget_id")

    op.execute("ALTER TABLE budget_settings DROP CONSTRAINT budget_settings_pkey")
    op.execute("ALTER TABLE budget_settings ADD CONSTRAINT budget_settings_pkey PRIMARY KEY (client_id)")

    for table, old, _, new, definition in KEYS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {old}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {new} {definition}")
    op.execute("DROP INDEX uq_expense_category_system_key")
    op.execute(
        "CREATE UNIQUE INDEX uq_expense_category_client_system_key "
        "ON expense_category (org_id, client_id, system_key) WHERE system_key IS NOT NULL"
    )
    op.execute("DROP INDEX uq_expense_autofeed")
    op.execute(
        "CREATE UNIQUE INDEX uq_expense_client_autofeed "
        "ON expense (org_id, client_id, source, link_id, period_key) WHERE source <> 'manual'"
    )

    for parent in ("expense_category", "cost_center"):
        op.execute(f"ALTER TABLE {parent} ADD CONSTRAINT uq_{parent}_id_client UNIQUE (id, client_id)")
    for table in ("budget_line", "expense"):
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_category_client "
            "FOREIGN KEY (category_id, client_id) REFERENCES expense_category (id, client_id)"
        )
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_cost_center_client "
            "FOREIGN KEY (cost_center_id, client_id) REFERENCES cost_center (id, client_id)"
        )
    for table in [*C, "budget_line"]:
        _isolate(table)


def downgrade() -> None:
    for table in ("budget_line", "expense"):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT fk_{table}_cost_center_client")
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT fk_{table}_category_client")
    for parent in ("cost_center", "expense_category"):
        op.execute(f"ALTER TABLE {parent} DROP CONSTRAINT uq_{parent}_id_client")
    op.execute("DROP INDEX uq_expense_client_autofeed")
    op.execute(
        "CREATE UNIQUE INDEX uq_expense_autofeed "
        "ON expense (org_id, source, link_id, period_key) WHERE source <> 'manual'"
    )
    op.execute("DROP INDEX uq_expense_category_client_system_key")
    op.execute(
        "CREATE UNIQUE INDEX uq_expense_category_system_key "
        "ON expense_category (org_id, system_key) WHERE system_key IS NOT NULL"
    )
    for table, old, definition, new, _ in reversed(KEYS):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {new}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {old} {definition}")
    op.execute("ALTER TABLE budget_settings DROP CONSTRAINT budget_settings_pkey")
    op.execute("ALTER TABLE budget_settings ADD CONSTRAINT budget_settings_pkey PRIMARY KEY (org_id)")
    _drop_h("budget_line", "budget")
    for table in reversed(C):
        _drop_c(table)
```

Si `budget_line.cost_center_id` o `expense.cost_center_id` admiten nulo, la
clave compuesta no comprueba nada para esa fila (MATCH SIMPLE): está bien,
"sin centro de coste" no es de nadie. La definición exacta de
`uq_expense_autofeed` y su `WHERE` se copian de 0020:201 (research-5 §A).

- [ ] **Step 4: El código**

- Modelos: `client_id` en las cinco C con `client_org_fk(...)`, `BudgetLine.client_id` H; `BudgetSettings` con `client_id` como clave primaria; las claves nuevas en sus modelos y en `PER_CLIENT_KEYS` de `tests/test_client_catalog.py`.
- **Cada escritura** carga su cliente (del cuerpo en las altas, del padre en el resto) y llama `require_module(..., module="budget")`. Las altas (`CategoryCreate`, `CostCenterCreate`, `BudgetCreate`, `ExpenseCreate`) ganan `client_id: UUID` obligatorio, cargado con `client_for_new_rows` y sin `unassigned` (422).
- **Cada `on_conflict_do_nothing`** de research-5 §B (`budgets.py:~143`, `fx.py:~67`, `refdata.py:~165,~255,~411`, `feeds.py:~512`) nombra la clave nueva (`index_elements` con `client_id`, o `constraint=` con el nombre) y escribe `client_id` en `.values(...)`.
- **Lecturas puras:** `get_settings(session, org_id, client_id)`, `list_categories`, `variance` y `forecast` solo siembran (la fila de ajustes, las categorías por defecto) si `await module_enabled(..., module="budget")`; si no, devuelven los valores por defecto sin escribir, o una lista vacía.
- `PUT /budget/settings`, `GET /budget/settings`, `GET /budget/forecast` y las dos exportaciones toman `client_id` obligatorio en la consulta: cada cliente tiene su moneda base y su año fiscal (research-5 §I2), y sumar entre clientes mezclaría monedas.
- `expenses.py::_resolve_link` (`:156-179`): la suscripción, licencia, equipo o cambio enlazado tiene que ser del cliente del gasto (422). `feeds.py::project_costs` y `materialize`: `MaterializeRequest` gana `client_id`; solo se materializan suscripciones y licencias de ese cliente, cada gasto con ese cliente.
- `rates.py::create_rate`: `FxRateCreate.to_currency: str | None`; sin él, la moneda base del cliente interno (`get_settings(session, org_id, org_id)`). La ruta sigue siendo de todos los clientes.
- `client_scope_catalog.py`: fuera de `PENDING_PHASE_2` `budget_settings`, `cost_center`, `expense_category`, `budget`, `budget_line`, `expense`.
- `tests/test_client_isolation.py::PATHS`: `"C-budget"`, `"C-expense_category"`, `"H-budget_line-via-budget"`.
- Consola: las altas envían `client_id: internalClientId(me)` con `// PHASE 3`; `getBudgetSettings`, `updateBudgetSettings`, `getForecast` y las exportaciones envían `client_id=internalClientId(me)` en la consulta, también con `// PHASE 3`.

- [ ] **Step 5: Comprobar**

Run: `.venv/bin/pytest -q tests/test_client_scope_budget.py tests/test_budget_budgets.py tests/test_budget_expenses.py tests/test_budget_api.py tests/test_budget_reporting.py tests/test_budget_feeds.py tests/test_client_catalog.py tests/test_client_isolation.py tests/test_global_writes.py`
Expected: PASS. Luego la suite completa, `mypy`, `ruff`, `npx vitest run src/modules/budget src/modules/settings`, `npx tsc -b`.

- [ ] **Step 6: Commit**

```bash
git add backend frontend
git commit -m "feat(clients): budget per client, with its own currency and fiscal year (phase 2 Task 8)"
```

---

### Task 9: Despliegues de software

Investigación: research-3 (§A `deployments`, §C, §D `deploy_window_sweep_all_orgs`, §E5-E6, §G, §H).

**Files:**
- Create: `backend/migrations/versions/0239_client_scope_deployments.py`
- Modify: `backend/app/modules/deploy/models.py` (`Deployment.client_id`, que llena el trigger), `api.py` (`create_deployment` resuelve el objetivo; `record_auto_change` con el cliente), `dispatch.py` (`dispatch_deployment` filtra por cliente), `schema.py` (`DeploymentOut.client_id`)
- Modify: `backend/app/core/client_scope_catalog.py` (`deployments` sale de `PENDING_PHASE_2`)
- Modify: las pruebas de research-3 §H que construyen `Deployment(`
- Modify: `frontend/src/modules/deploy/types.ts` y `fixtures.ts` (`client_id` en la salida; el cuerpo no cambia: el servidor lo deriva)
- Test: `backend/tests/test_client_scope_deployments.py`

**Interfaces:**
- Consumes: `record_auto_change(..., client_id=)` y `gates_for(..., client_id=)` (Task 7).
- Produces: `deployments.client_id` C, **escrito por el trigger** `its_deployment_client` desde el objetivo (equipo o grupo), como `its_job_client` de 0231; la aplicación no lo envía. Un `client_id` enviado que no coincide es un error de la base.

- [ ] **Step 1: Escribir las pruebas que fallan**

```python
# backend/tests/test_client_scope_deployments.py
"""A deployment belongs to its target's client (plan 2026-10-26 Task 9)."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Deployment, Device, Package
from tests.client_scope_support import client_with_site, everything_on
from tests.conftest import ApiEnv


async def _device(env: ApiEnv, org: UUID, client: UUID, site: UUID) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        d = Device(org_id=org, client_id=client, site_id=site, hostname="pc", os_family="windows")
        s.add(d)
        await s.commit()
        return d.id


async def _package(env: ApiEnv, org: UUID) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        p = Package(org_id=org, name=f"7zip-{uuid4().hex[:4]}", version="1", os_family="windows",
                    installer_type="msi", sha256="0" * 64, size_bytes=1)
        s.add(p)
        await s.commit()
        return p.id


async def test_a_deployment_takes_its_target_s_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("d-client")
    x, x_site = await client_with_site(api_env, org)
    admin = await api_env.headers(org, await api_env.create_user(org, "org-admin"))
    device = await _device(api_env, org, x, x_site)
    package = await _package(api_env, org)
    r = await api_env.client.post(
        "/api/v1/deploy/deployments",
        json={"package_id": str(package), "target_type": "device", "target_id": str(device)},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    assert r.json()["client_id"] == str(x)


async def test_a_technician_of_x_cannot_deploy_to_y(api_env: ApiEnv) -> None:
    """research-3 §E5: `create_deployment` never resolved `target_id`, and
    the window sweep dispatched it later under scope all."""
    org = await api_env.create_org("d-narrow")
    x, _ = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    y_device = await _device(api_env, org, y, y_site)
    package = await _package(api_env, org)
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.post(
        "/api/v1/deploy/deployments",
        json={"package_id": str(package), "target_type": "device", "target_id": str(y_device)},
        headers=x_tech,
    )
    assert r.status_code == 404, r.text


async def test_the_database_derives_the_client_and_refuses_a_contradiction(
    api_env: ApiEnv,
) -> None:
    org = await api_env.create_org("d-trigger")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    device = await _device(api_env, org, x, x_site)
    package = await _package(api_env, org)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        d = Deployment(org_id=org, package_id=package, target_type="device", target_id=device,
                       state="pending")
        s.add(d)
        await s.flush()
        await s.refresh(d)
        assert d.client_id == x
    with pytest.raises(DBAPIError, match="differs from its target"):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            s.add(Deployment(org_id=org, client_id=y, package_id=package, target_type="device",
                             target_id=device, state="pending"))
            await s.flush()
```

Ajusta las columnas de `Package` y `Deployment` (`state`, `installer_type`...)
a sus modelos (`app/modules/deploy/models.py`, `app/models/`) si difieren.

- [ ] **Step 2: Comprobar que fallan**

Run: `.venv/bin/pytest -q tests/test_client_scope_deployments.py`
Expected: FAIL.

- [ ] **Step 3: La migración 0239**

Empieza con el **Bloque común de migración**, y luego:

```python
"""A deployment belongs to its target's client (plan 2026-10-26 Task 9).

`deployments` is C, but its client is never chosen: it is the target's -- one
device or one group, and a group is one client's (0231). Like `jobs` (0231's
`its_job_client`), a trigger derives it from `target_type` + `target_id`,
which has no foreign key. SECURITY INVOKER: a target the session cannot see
reads as NULL and the write is refused by name.

Revision ID: 0239
Revises: 0238
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0239"
down_revision: str | None = "0238"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (bloque común aquí)

BACKFILL = (
    "COALESCE("
    "(SELECT client_id FROM devices WHERE id = deployments.target_id), "
    "(SELECT client_id FROM device_groups WHERE id = deployments.target_id), "
    "deployments.org_id)"
)


def upgrade() -> None:
    _add_c("deployments", BACKFILL)
    op.execute(
        """
        CREATE FUNCTION its_deployment_client() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE target_client uuid; claimed boolean;
        BEGIN
            IF NEW.target_type = 'device' THEN
                SELECT client_id INTO target_client FROM devices WHERE id = NEW.target_id;
            ELSIF NEW.target_type = 'group' THEN
                SELECT client_id INTO target_client FROM device_groups WHERE id = NEW.target_id;
            ELSE
                RAISE EXCEPTION 'deployments.target_type % has no client', NEW.target_type
                    USING ERRCODE = 'check_violation';
            END IF;
            IF target_client IS NULL THEN
                RAISE EXCEPTION 'deployment target is not visible'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF TG_OP = 'INSERT' THEN
                claimed := NEW.client_id IS NOT NULL;
            ELSE
                claimed := NEW.client_id IS NOT NULL
                    AND NEW.client_id IS DISTINCT FROM OLD.client_id;
            END IF;
            IF claimed AND NEW.client_id <> target_client THEN
                RAISE EXCEPTION 'deployments.client_id differs from its target''s client'
                    USING ERRCODE = 'check_violation';
            END IF;
            NEW.client_id := target_client;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER its_deployment_client BEFORE INSERT OR UPDATE OF target_type, "
        "target_id, client_id ON deployments FOR EACH ROW EXECUTE FUNCTION its_deployment_client()"
    )
    _isolate("deployments")


def downgrade() -> None:
    op.execute("DROP TRIGGER its_deployment_client ON deployments")
    op.execute("DROP FUNCTION its_deployment_client()")
    _drop_c("deployments")
```

- [ ] **Step 4: El código**

- `Deployment.client_id`: `ForeignKey("clients.id")`, `index=True`, `server_default=FetchedValue()`, `server_onupdate=FetchedValue()`, y `client_org_fk("deployments")`. Comentario: "Set by trigger its_deployment_client from the target (migration 0239): never sent".
- `create_deployment` (`api.py:523-635`): antes de crear, carga el equipo o el grupo del objetivo en la sesión de la petición; si no se ve, 404 (`tr("deployment target not found")`). El `change_id` (`authorizing_change`) tiene que ser del cliente del objetivo (422). `record_auto_change(..., client_id=<cliente del objetivo>)` (quita el comentario de la Task 7); `deployment_refusal` usa `gates_for(..., client_id=deployment.client_id)`.
- `dispatch_deployment` (`dispatch.py:93-270`): las búsquedas del equipo (`:178`) y de los miembros del grupo (`:189`) añaden `Device.client_id == deployment.client_id` (spec §5 prueba 4: el barrido corre con alcance `all`).
- Auditoría y `publish` de despliegues: `client_id=deployment.client_id` (léelo de la base después del `flush`: lo escribió el trigger).
- `client_scope_catalog.py`: `deployments` sale de `PENDING_PHASE_2`.
- `tests/test_client_isolation.py::PATHS`: `"C-deployments"` con un `Deployment` sobre `s.device` (el trigger pone el cliente; la prueba de inserción en Y espera `"deployment target is not visible"`, así que añade `"C-deployments": "deployment target is not visible"` a `GUARDED`).

- [ ] **Step 5: Comprobar**

Run: `.venv/bin/pytest -q tests/test_client_scope_deployments.py tests/test_deploy_dispatch.py tests/test_deploy_worker.py tests/test_deploy_models.py tests/test_deploy_result.py tests/test_change_mgmt_links.py tests/test_client_isolation.py tests/test_client_catalog.py`
Expected: PASS. Luego la suite completa, `mypy`, `ruff`, `npx vitest run src/modules/deploy`, `npx tsc -b`.

- [ ] **Step 6: Commit**

```bash
git add backend frontend
git commit -m "feat(clients): a deployment belongs to its target's client (phase 2 Task 9)"
```

---

### Task 10: IPAM — esquema por cliente y `ipam_site` fusionado en `sites`

Investigación: research-3 (§A, §A.2, §B "IPAM", §C, §E1-E3, §F, §G, §H, §I2, §I6-I7).

**Files:**
- Create: `backend/migrations/versions/0240_client_scope_ipam.py`
- Modify: `backend/app/modules/ipam/models.py` (se borra `IpamSite`; `client_id` en todas; `site_id` → `sites`), `registry.py` (`ensure_default_vrf`, `create_vrf`, `create_vlan`; se borran `create_site`, `update_site`, `delete_site`), `subnets.py` (`create_subnet`, `update_subnet`), `addresses.py` (`_resolve_target`, `create_address`), `authorization.py` (`create_authorization`), `api.py` (se borran `GET/POST /ipam/sites`), `schema.py`
- Modify: `backend/app/modules/site_server/models.py:112`, `schema.py:145,157,178`, `api.py:108,118,130,155`, `registry.py` (`promote`, `update_site_server`): `ipam_site_id` → `site_id`
- Modify: `backend/app/models/__init__.py:132,296` (exportaciones de `IpamSite`)
- Modify: `backend/app/core/client_scope_catalog.py` (`ipam_site` sale del catálogo; `ipam_subnet` pasa a `H("ipam_vrf", "vrf_id")`; las nueve tablas IPAM salen de `PENDING_PHASE_2`)
- Modify: `docs/superpowers/specs/2026-10-23-msp-clientes-design.md` Anexo A (`ipam_subnet` es H desde `ipam_vrf`) y §7 (punto nuevo, Task 15)
- Modify: `backend/tests/test_client_catalog.py` (claves foráneas `site_client` esperadas, `PER_CLIENT_KEYS`), `backend/tests/test_client_isolation.py` (`PATHS`), `backend/tests/test_ipam_api.py` (`EXPECTED_ROUTE_COUNT`), `backend/tests/ipam_fixtures.py`, `backend/tests/site_server_fixtures.py:58` y los 15 archivos que nombran `ipam_site`/`IpamSite`/`ipam_site_id` (research-3 §I6)
- Modify: `backend/app/mcp/descriptions/part_a.py:220-310`, `part_b.py:236-255`, `backend/app/mcp/exclusions.py` (fuera `ipam_sites_*`)
- Modify: `frontend/src/modules/ipam/RegistryPage.tsx` (fuera el alta de sitios; los selectores de sede usan `GET /clients/{id}/sites`), `SubnetTreePage.tsx:587`, `AddressDialog.tsx:193,212`, `ScanAuthorizationsPage.tsx:460`, `types.ts`, `fixtures.ts`; `frontend/src/lib/api.ts` (fuera `createIpamSite`; `createIpamVrf`, `createIpamVlan`, `createIpamSubnet`, `createIpamAddress`, `createIpamScanAuthorization` con cliente); `frontend/src/modules/site-servers/*` (`ipam_site_id` → `site_id`)
- Test: `backend/tests/test_client_scope_ipam.py`

**Interfaces:**
- Produces: `ipam_vrf.client_id`, `ipam_vlan.client_id` (C); `ipam_subnet`, `ipam_range`, `ipam_address`, `ipam_observation`, `ipam_drift`, `ipam_scan_authorization`, `ipam_sweep_run` (H).
- Produces: `ipam_vlan.site_id`, `ipam_subnet.site_id`, `site_server.site_id` → `sites`, con `(site_id, client_id) REFERENCES sites (id, client_id)`.
- Produces: `ensure_default_vrf(session, org_id, client_id) -> UUID` (un VRF `global` por cliente).
- Produces: las altas de VRF y de VLAN sin sede, y las de subred, dirección y autorización de barrido **sin VRF**, llevan `client_id` en el cuerpo; con VRF o con sede, el cliente sale de ahí (422 si el cuerpo lo contradice).

- [ ] **Step 1: Escribir las pruebas que fallan**

```python
# backend/tests/test_client_scope_ipam.py
"""IPAM per client (plan 2026-10-26 Tasks 10 and 11)."""

from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import IpamSubnet, IpamVrf
from tests.client_scope_support import client_with_site, everything_on
from tests.conftest import ApiEnv

I = "/api/v1/ipam"


async def _admin(env: ApiEnv, org: UUID) -> dict[str, str]:
    return await env.headers(org, await env.create_user(org, "org-admin"))


async def test_two_clients_can_both_have_10_0_0_0_24(api_env: ApiEnv) -> None:
    """The reason IPAM is per client: two networks, one prefix."""
    org = await api_env.create_org("i-overlap")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    for client in (x, y):
        r = await api_env.client.post(
            f"{I}/subnets", json={"prefix": "10.0.0.0/24", "client_id": str(client)},
            headers=admin,
        )
        assert r.status_code == 201, r.text
        assert r.json()["client_id"] == str(client)
    async with AsyncSession(api_env.admin_engine) as s:
        vrfs = (await s.execute(select(IpamVrf.client_id, IpamVrf.name)
                                .where(IpamVrf.org_id == org))).all()
    assert {(c, n) for c, n in vrfs if c in (x, y)} == {(x, "global"), (y, "global")}


async def test_a_subnet_takes_its_vrf_s_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("i-vrf")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    vrf = (await api_env.client.post(f"{I}/vrfs", json={"name": "LAN", "client_id": str(x)},
                                     headers=admin)).json()["id"]
    ok = await api_env.client.post(f"{I}/subnets", json={"prefix": "10.1.0.0/24", "vrf_id": vrf},
                                   headers=admin)
    assert ok.status_code == 201 and ok.json()["client_id"] == str(x)
    contradiction = await api_env.client.post(
        f"{I}/subnets", json={"prefix": "10.2.0.0/24", "vrf_id": vrf, "client_id": str(y)},
        headers=admin,
    )
    assert contradiction.status_code == 422


async def test_a_subnet_s_site_must_be_its_client_s(api_env: ApiEnv) -> None:
    org = await api_env.create_org("i-site")
    x, _ = await client_with_site(api_env, org)
    _, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    r = await api_env.client.post(
        f"{I}/subnets", json={"prefix": "10.3.0.0/24", "client_id": str(x), "site_id": str(y_site)},
        headers=admin,
    )
    assert r.status_code == 422, r.text
    with pytest.raises(DBAPIError, match="fk_ipam_subnet_site_client"):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            vrf = (await s.execute(select(IpamVrf.id).where(IpamVrf.client_id == x))).scalar_one()
            s.add(IpamSubnet(org_id=org, vrf_id=vrf, prefix="10.4.0.0/24", site_id=y_site))
            await s.flush()


async def test_the_old_ipam_site_routes_are_gone(api_env: ApiEnv) -> None:
    org = await api_env.create_org("i-gone")
    admin = await _admin(api_env, org)
    assert (await api_env.client.get(f"{I}/sites", headers=admin)).status_code in (404, 405)


async def test_a_technician_of_x_sees_only_x_s_subnets(api_env: ApiEnv) -> None:
    org = await api_env.create_org("i-narrow")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    y_subnet = (await api_env.client.post(
        f"{I}/subnets", json={"prefix": "10.9.0.0/24", "client_id": str(y)}, headers=admin
    )).json()["id"]
    x_tech = await everything_on(api_env, org, x)
    assert (await api_env.client.get(f"{I}/subnets/{y_subnet}", headers=x_tech)).status_code == 404
```

Ajusta cuerpos y rutas a `ipam/schema.py` si difieren (por ejemplo, si la
lista de subredes es otra ruta).

- [ ] **Step 2: Comprobar que fallan**

Run: `.venv/bin/pytest -q tests/test_client_scope_ipam.py`
Expected: FAIL.

- [ ] **Step 3: La migración 0240**

Empieza con el **Bloque común de migración**, y luego:

```python
"""IPAM per client; `ipam_site` merged into `sites` (spec §1.2; plan
2026-10-26 Task 10, C3).

`ipam_vrf` and `ipam_vlan` are C. `ipam_subnet` becomes H from `ipam_vrf`
(Annex A amended): a subnet's VRF is NOT NULL, so the subnet's client is the
VRF's, and nothing else could keep them in agreement. Everything below a VRF
or a subnet is H.

`ipam_site` is dropped WITHOUT copying rows (spec §1.2, P7): the three
columns that named it are dropped with it, and `site_id` columns naming a
client's `sites` row take their place, pinned to the row's client by
`(site_id, client_id) REFERENCES sites (id, client_id)` (0231's technique).
Existing subnets, VLANs and site servers lose their old IPAM site.

Natural keys: a VRF's name and a site-less VLAN's number are per client; a
VLAN with a site is unique per site (the site pins the client).

DOWNGRADE recreates `ipam_site` and the three old columns exactly as 0043
and 0050 created them -- the earlier migrations' own downgrades drop them by
name -- and loses the new `site_id` values (CI's round trip only).

Revision ID: 0240
Revises: 0239
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0240"
down_revision: str | None = "0239"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (bloque común aquí)

C = ["ipam_vrf", "ipam_vlan"]
H = [
    ("ipam_subnet", "ipam_vrf", "vrf_id"),
    ("ipam_range", "ipam_subnet", "subnet_id"),
    ("ipam_address", "ipam_vrf", "vrf_id"),
    ("ipam_observation", "ipam_vrf", "vrf_id"),
    ("ipam_drift", "ipam_vrf", "vrf_id"),
    ("ipam_scan_authorization", "ipam_vrf", "vrf_id"),
    ("ipam_sweep_run", "ipam_subnet", "subnet_id"),
]
SITED = ["ipam_vlan", "ipam_subnet", "site_server"]


def upgrade() -> None:
    # The old site references go first: a VLAN's client cannot be derived
    # from an IPAM site that has none.
    op.execute("ALTER TABLE site_server DROP COLUMN ipam_site_id")
    op.execute("ALTER TABLE ipam_subnet DROP COLUMN site_id")
    op.execute("ALTER TABLE ipam_vlan DROP COLUMN site_id")  # drops both vid indexes' site leg
    op.execute("DROP TABLE ipam_site")

    for table in C:
        _add_c(table)
    for table, parent, fk in H:
        _add_h(table, parent, fk)

    for table in SITED:
        op.execute(f"ALTER TABLE {table} ADD COLUMN site_id uuid REFERENCES sites(id)")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_site_client "
            "FOREIGN KEY (site_id, client_id) REFERENCES sites (id, client_id)"
        )
        op.execute(f"CREATE INDEX ix_{table}_site_id ON {table} (site_id)")

    op.execute("ALTER TABLE ipam_vrf DROP CONSTRAINT uq_ipam_vrf_org_name")
    op.execute(
        "ALTER TABLE ipam_vrf ADD CONSTRAINT uq_ipam_vrf_client_name UNIQUE (org_id, client_id, name)"
    )
    op.execute("DROP INDEX IF EXISTS uq_ipam_vlan_org_site_vid")
    op.execute("DROP INDEX IF EXISTS uq_ipam_vlan_org_vid_global")
    op.execute(
        "CREATE UNIQUE INDEX uq_ipam_vlan_site_vid ON ipam_vlan (org_id, site_id, vid) "
        "WHERE site_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_ipam_vlan_client_vid ON ipam_vlan (org_id, client_id, vid) "
        "WHERE site_id IS NULL"
    )
    for table in [*C, *[t for t, _, _ in H]]:
        _isolate(table)


def downgrade() -> None:
    op.execute("DROP INDEX uq_ipam_vlan_client_vid")
    op.execute("DROP INDEX uq_ipam_vlan_site_vid")
    op.execute("ALTER TABLE ipam_vrf DROP CONSTRAINT uq_ipam_vrf_client_name")
    op.execute("ALTER TABLE ipam_vrf ADD CONSTRAINT uq_ipam_vrf_org_name UNIQUE (org_id, name)")
    for table in SITED:
        op.execute(f"ALTER TABLE {table} DROP COLUMN site_id")
    for table, parent, _ in reversed(H):
        _drop_h(table, parent)
    for table in reversed(C):
        _drop_c(table)
    # ipam_site and the three old columns, as 0043 (lines 190-233) and 0050
    # (line 100) created them: same columns, same names, same indexes, same
    # tenant policy (copy that policy from 0043's upgrade too).
    ...
```

**El `...` del final no es un hueco:** son las sentencias que recrean
`ipam_site` (con `uq_ipam_site_org_name`, su política `tenant_isolation`,
`ENABLE`/`FORCE ROW LEVEL SECURITY` y los `GRANT` a `app_user` que 0043 le
dio), las columnas `ipam_vlan.site_id`, `ipam_subnet.site_id` y
`site_server.ipam_site_id` con sus claves foráneas, y los índices
`uq_ipam_vlan_org_site_vid` / `uq_ipam_vlan_org_vid_global` con sus
definiciones de 0043:155-158. Cópialas de 0043 y 0050 (léelas: son SQL de
Alembic en esos archivos) y comprueba la vuelta con
`alembic upgrade head && alembic downgrade 0239 && alembic upgrade head`
antes de la suite.

- [ ] **Step 4: El código**

- Modelos: se borra `IpamSite` (y sus exportaciones). `IpamVrf`/`IpamVlan` con `client_id` + `client_org_fk`; las H con `client_id` de trigger; `site_id` con `ForeignKey("sites.id")` y `site_client_fk(...)` en `IpamVlan`, `IpamSubnet` y `SiteServer`; las claves nuevas en sus modelos y en `PER_CLIENT_KEYS` (`uq_ipam_vrf_client_name`, `uq_ipam_vlan_client_vid`).
- `ensure_default_vrf(session, org_id, client_id)`: `ON CONFLICT` sobre `(org_id, client_id, name)`. Sus llamadores pasan el cliente: el gancho de inventario (`observations.py:298`) el del equipo; la importación, el de su petición; `create_subnet`/`_resolve_target`/la autorización de barrido, el del cuerpo cuando no hay VRF.
- Altas: `VrfCreate.client_id` obligatorio. `VlanCreate`: con `site_id`, el cliente de la sede (`site_for_new_rows`); sin sede, `client_id` obligatorio. `SubnetCreate`, `AddressCreate`, `ScanAuthorizationCreate`: con `vrf_id`, el cliente del VRF; sin él, `client_id` obligatorio y el VRF `global` de ese cliente. En todas: un `client_id` del cuerpo que contradice el derivado, 422; `unassigned`, 422; la sede y la VLAN de una subred, del cliente de la subred (422 antes de que la clave compuesta lo diga). `SubnetOut`, `VrfOut`, `VlanOut`, `AddressOut` ganan `client_id`.
- `update_subnet` (`subnets.py:551`): `site_id`/`vlan_id` nuevos, del cliente de la subred (422).
- Se borran `GET/POST /ipam/sites` y `registry.create_site/update_site/delete_site`; `EXPECTED_ROUTE_COUNT` baja en 2. Las sedes se gestionan en `/clients/{id}/sites` (fase 1).
- Site servers: `ipam_site_id` → `site_id` en esquema, API y registro; `promote`/`update_site_server` validan que la sede es del cliente del equipo (422). Es un cambio de API (research-3 §I6): el MCP lo recoge solo del esquema, y las descripciones se actualizan.
- Las acciones de auditoría `ipam_site.*` dejan de emitirse; sus frases en `auditVocabulary.ts` se quedan (el registro guarda eventos viejos).
- `client_scope_catalog.py`: `ipam_site` sale de `CATALOG` y de `PENDING_PHASE_2`; `"ipam_subnet": H("ipam_vrf", "vrf_id")`; fuera de `PENDING_PHASE_2` las nueve IPAM.
- `tests/test_client_catalog.py`: el conjunto de claves `fk_<t>_site_client` esperadas pasa de `("devices", "enrollment_tokens", "install_links")` a esa tupla más `("ipam_vlan", "ipam_subnet", "site_server")`.
- `tests/test_client_isolation.py::PATHS`: `"C-ipam_vrf"`, `"H-ipam_subnet-via-ipam_vrf"`, `"H-ipam_range-via-ipam_subnet"` (con `Side.vrf`/`Side.subnet` nuevos si hacen falta).
- Consola: el alta de sitios de IPAM desaparece de `RegistryPage.tsx`; los selectores de sede (VLAN, subred) listan `GET /clients/{internal}/sites`; `createIpamVrf`, `createIpamSubnet` (sin VRF), `createIpamAddress` (sin VRF, `AddressDialog` hoy no envía VRF) y `createIpamScanAuthorization` (sin VRF) envían `client_id: internalClientId(me)`, todo con `// PHASE 3`. En site servers, `ipam_site_id` → `site_id`.

- [ ] **Step 5: Comprobar**

Run: `.venv/bin/pytest -q tests/test_client_scope_ipam.py tests/test_ipam_api.py tests/test_ipam_registry.py tests/test_ipam_subnets.py tests/test_ipam_addresses.py tests/test_ipam_authorization.py tests/test_ipam_migration.py tests/test_site_server_api.py tests/test_site_server_registry.py tests/test_client_catalog.py tests/test_client_isolation.py tests/test_mcp_descriptions.py tests/test_migrations.py`
Expected: PASS. Luego la suite completa, `mypy`, `ruff`, `npx vitest run src/modules/ipam src/modules/site-servers`, `npx tsc -b`.

- [ ] **Step 6: Commit**

```bash
git add backend frontend docs/superpowers/specs/2026-10-23-msp-clientes-design.md
git commit -m "feat(clients): IPAM per client; ipam_site merged into sites (phase 2 Task 10)"
```

---

### Task 11: IPAM — barridos, candidatos y promoción dentro del cliente

Investigación: research-3 (§B "Promotion to asset", §D sweeps, §E1, §I1, §I3-I4).

**Files:**
- Modify: `backend/app/modules/ipam/sweep.py` (`select_sweeper`, `_designated`, `create_sweep`), `subnets.py:889-980` (`set_sweep_settings`), `candidates.py` (`_CANDIDATES_SQL`, `_known_inventory`, `promote`, `auto_create_from_sweep`), `handler.py` (`handle_sweep_result`, `_reject`), `reconcile.py:838`, `allocation.py:602,651` (`claim_address`), `addresses.py` (`assign`), `observations.py:261-298` (`record_inventory_observations`), `importer.py:349,430,479-486`
- Modify: `backend/app/api/v1/devices.py:527-546` (`POST /devices/candidates/promote`: fuera `require_all_clients`; `client_id` en el cuerpo)
- Modify: `frontend/src/modules/devices/CandidatesPage.tsx:155` y `frontend/src/lib/api.ts:1826` (`promoteAssetCandidates` con cliente), `frontend/src/modules/ipam/SweepPanel.tsx:212` (el selector de barredor lista equipos del cliente de la subred)
- Test: `backend/tests/test_client_scope_ipam.py` (amplía el de la Task 10)

**Interfaces:**
- Consumes: el esquema de la Task 10 (`ipam_subnet.client_id`, `ipam_subnet.site_id` → `sites`).
- Produces: `select_sweeper(session, org_id, subnet, *, now=None)` que solo elige equipos y site servers de `subnet.client_id`.
- Produces: `promote(session, org_id, *, actor_id, client_id: UUID, addresses: list[str], overrides=None)`; un candidato de una subred sin sede da un resultado de rechazo con la frase `tr("assign the subnet to a site before adding its machines")` (C7).

- [ ] **Step 1: Escribir las pruebas que fallan** (al final de `test_client_scope_ipam.py`)

```python
from datetime import UTC, datetime  # noqa: E402

from app.models import Agent, Device, IpamObservation  # noqa: E402
from app.modules.ipam.sweep import select_sweeper  # noqa: E402


async def _device_seen_in(env: ApiEnv, org: UUID, client: UUID, site: UUID, vrf: UUID,
                          address: str) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        d = Device(org_id=org, client_id=client, site_id=site, hostname="h", os_family="linux",
                   status="approved")
        s.add(d)
        await s.flush()
        s.add(Agent(org_id=org, device_id=d.id, status="online", last_seen_at=datetime.now(UTC),
                    cert_fingerprint=uuid4().hex, capabilities=["net.sweep"]))
        s.add(IpamObservation(org_id=org, vrf_id=vrf, address=address, source="inventory",
                              mac_key="", device_id=d.id, last_seen_at=datetime.now(UTC)))
        await s.commit()
        return d.id


async def test_a_sweep_never_borrows_another_client_s_machine(api_env: ApiEnv) -> None:
    """research-3 §D: `select_sweeper` chose any approved device of the ORG
    whose inventory showed an address inside the prefix. Y's machine sits on
    Y's 10.0.0.0/24, which is not X's 10.0.0.0/24."""
    org = await api_env.create_org("i-sweeper")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    subnets = {}
    for client in (x, y):
        subnets[client] = (await api_env.client.post(
            f"{I}/subnets", json={"prefix": "10.0.0.0/24", "client_id": str(client)},
            headers=admin)).json()
    async with AsyncSession(api_env.admin_engine) as s:
        y_vrf = (await s.execute(select(IpamVrf.id).where(IpamVrf.client_id == y))).scalar_one()
    await _device_seen_in(api_env, org, y, y_site, y_vrf, "10.0.0.7")
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        x_subnet = await s.get(IpamSubnet, UUID(subnets[x]["id"]))
        chosen = await select_sweeper(s, org, x_subnet)
    assert chosen is None  # no X machine on X's wire: `no_agent`, never Y's


async def test_sweep_settings_name_a_machine_of_the_subnet_s_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("i-settings")
    x, _ = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    subnet = (await api_env.client.post(
        f"{I}/subnets", json={"prefix": "10.5.0.0/24", "client_id": str(x)}, headers=admin
    )).json()["id"]
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        d = Device(org_id=org, client_id=y, site_id=y_site, hostname="y", os_family="linux")
        s.add(d)
        await s.commit()
    r = await api_env.client.put(
        f"{I}/subnets/{subnet}/sweep-settings",
        json={"enabled": True, "interval_minutes": 60, "sweep_device_id": str(d.id)},
        headers=admin,
    )
    assert r.status_code == 422, r.text


async def test_a_promoted_address_becomes_a_device_of_the_subnet_s_client_and_site(
    api_env: ApiEnv,
) -> None:
    org = await api_env.create_org("i-promote")
    x, x_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    subnet = (await api_env.client.post(
        f"{I}/subnets", json={"prefix": "10.6.0.0/24", "client_id": str(x),
                             "site_id": str(x_site)}, headers=admin)).json()
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        s.add(IpamObservation(org_id=org, vrf_id=UUID(subnet["vrf_id"]), address="10.6.0.9",
                              source="sweep", mac_key="aa:bb:cc:dd:ee:ff",
                              subnet_id=UUID(subnet["id"]), last_seen_at=datetime.now(UTC)))
        await s.commit()
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.post(
        "/api/v1/devices/candidates/promote",
        json={"client_id": str(x), "addresses": ["10.6.0.9"]}, headers=x_tech,
    )
    assert r.status_code in (200, 201), r.text
    async with AsyncSession(api_env.admin_engine) as s:
        client, site = (await s.execute(
            select(Device.client_id, Device.site_id).where(Device.org_id == org,
                                                           Device.client_id == x)
        )).one()
    assert (client, site) == (x, x_site)


async def test_a_subnet_without_a_site_cannot_promote(api_env: ApiEnv) -> None:
    """C7: a device needs a site, and a client has no marked default."""
    org = await api_env.create_org("i-nosite")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    subnet = (await api_env.client.post(
        f"{I}/subnets", json={"prefix": "10.7.0.0/24", "client_id": str(x)}, headers=admin
    )).json()
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        s.add(IpamObservation(org_id=org, vrf_id=UUID(subnet["vrf_id"]), address="10.7.0.9",
                              source="sweep", mac_key="", subnet_id=UUID(subnet["id"]),
                              last_seen_at=datetime.now(UTC)))
        await s.commit()
    r = await api_env.client.post(
        "/api/v1/devices/candidates/promote",
        json={"client_id": str(x), "addresses": ["10.7.0.9"]}, headers=admin,
    )
    body = r.json()
    assert r.status_code in (200, 201, 422), r.text
    assert "site" in str(body).lower()
```

Las columnas de `Agent`, `Device.status` e `IpamObservation` se copian de
sus modelos y de `tests/ipam_fixtures.py` (`seed_*`), que ya construye estas
filas: si un fixture de allí lo hace en una línea, úsalo en vez del
constructor. Añade `from uuid import uuid4` arriba si falta.

- [ ] **Step 2: Comprobar que fallan**

Run: `.venv/bin/pytest -q tests/test_client_scope_ipam.py -k "sweep or promote or site"`
Expected: FAIL.

- [ ] **Step 3: El código**

- `select_sweeper` (`sweep.py:244`): el equipo designado (`_designated:212`) tiene que ser del cliente de la subred (si no, se ignora con el mismo motivo que un designado sin capacidad); la búsqueda de equipos (`:308-311`) y la de site servers (`:328`) añaden `d.client_id = :client` / `s.client_id = :client` con `subnet.client_id`. Precedente y frase: `agent_push/preflight.py:_site_server_for:488` y su docstring (`:52-56`).
- `set_sweep_settings` (`subnets.py:944`): `sweep_device_id` del cliente de la subred (422 `tr("that machine belongs to another client")`).
- Candidatos (`candidates.py`): `_CANDIDATES_SQL` (`:108`), `_known_inventory` (`:423`), el mapa `existing` de `promote` (`:229`) y el `NOT EXISTS devices` comparan dentro de un cliente: agrupan por `(client_id, address)` y cruzan con los equipos de ese cliente (la misma dirección en X e Y son dos candidatos distintos).
- `promote(..., client_id, addresses)`: re-deriva los candidatos de ese cliente; cada uno toma `client_id` y `site_id` de su subred; sin subred o sin sede, resultado de rechazo con la frase de C7. La ruta (`devices.py:527`) quita `require_all_clients` y pide `assets.manage` con el alcance de la petición (`ctx.clients`): la RLS deja fuera lo que no ve. `PromoteRequest.client_id` obligatorio.
- `auto_create_from_sweep`: recibe la subred y usa su cliente y su sede; sin sede, no crea el equipo y lo registra como candidato (el resultado lo dice).
- `claim_address` (`allocation.py:651`) y `addresses.assign` (`:355`): el equipo, del cliente de la dirección (422).
- `record_inventory_observations` (`observations.py:261`): el VRF por defecto del cliente del equipo (`ensure_default_vrf(session, org_id, device.client_id)`).
- `importer.py`: la importación lleva `client_id` (en la petición; la consola envía el interno con `// PHASE 3`); la búsqueda de `written` (`:479-486`) filtra por VRF, no solo por dirección (research-3 §I3: `scalar_one()` falla con dos VRF que tienen la misma dirección).
- `reconcile.py:838` y `handler.py:180`: `publish(..., client_id=...)` con el cliente de la subred o del trabajo (quita el `None  # ... Task 11`).
- Consola: `promoteAssetCandidates` envía `client_id: internalClientId(me)` con `// PHASE 3`; `SweepPanel` pide los equipos del cliente de la subred (`GET /devices?client_id=` si existe el filtro; si no, filtra por `client_id` de `DeviceOut`, que la fase 1 ya expone o esta tarea añade).

- [ ] **Step 4: Comprobar**

Run: `.venv/bin/pytest -q tests/test_client_scope_ipam.py tests/test_ipam_sweep_dispatch.py tests/test_ipam_sweep_result.py tests/test_ipam_sweep_autoasset.py tests/test_ipam_site_server_preference.py tests/test_ipam_observations.py tests/test_ipam_import.py tests/test_ipam_reconcile.py tests/test_ipam_allocation.py tests/test_asset_candidates.py tests/test_asset_promote.py tests/test_worker_ipam_sweep_containment.py`
Expected: PASS. Luego la suite completa, `mypy`, `ruff`, `npx vitest run src/modules/ipam src/modules/devices`, `npx tsc -b`.

- [ ] **Step 5: Commit**

```bash
git add backend frontend
git commit -m "feat(clients): IPAM sweeps, candidates and promotion stay inside the subnet's client (phase 2 Task 11)"
```

---

### Task 12: Instalación remota (push) dentro del cliente

Investigación: research-4 (todo). **Invariante 2a de `CLAUDE.md`:** ninguna línea de esta tarea toca los parámetros firmados (`agent_push/jobtypes.py`), el sobre ni lo que ejecuta el agente. La tarea **estrecha** 2a(d): el activo avalado, la autorización y la credencial tienen que ser además del mismo cliente. Si algún paso pareciera exigir otra cosa, se detiene y se pregunta.

**Files:**
- Create: `backend/migrations/versions/0241_client_scope_push.py`
- Modify: `backend/app/modules/agent_push/models.py`, `authorization.py` (`create_authorization`, su `on_conflict`), `credential_scope.py` (`bind_scope`, `resolve_push_credential`), `credential_realm.py`, `preflight.py` (`evaluate_targets`, `_vouched_asset`, `_authorization_for`, `request_push_batch`, el texto de `ambiguous_asset`), `run_list.py` (`:139`, `:281`, `:310-313`, `RunList`), `runs.py` (`create_run`, `_lock_org_runs`, `resume_run`, `_owner_may_push`, `advance_run`, `tick`, `org_ids_with_live_runs`, `:851,1032`), `dispatch.py` (`dispatch_push_batch`), `api.py`, `schema.py`
- Create: `backend/app/modules/agent_push/locks.py` (`lock_target_scope`)
- Modify: `backend/app/api/v1/credentials.py` (`CredentialCreate.client_id`)
- Modify: `backend/app/worker.py:2082-2130` (`push_run_tick_all_orgs`), `backend/app/core/client_scope.py:148` (fuera la entrada si deja de usarse)
- Modify: `backend/app/modules/provisioning/dispatch.py:117` (Wake-on-LAN, `client_id`)
- Modify: `backend/app/core/client_scope_catalog.py` (las cinco tablas salen de `PENDING_PHASE_2`)
- Modify: `backend/tests/agent_push_fixtures.py` (`seed_credential` con `client_id=org_id` por defecto; `seed_push_authorization`, `seed_push_credential_scope` con cliente), `backend/tests/push_run_fixtures.py`, `backend/tests/test_agent_push_migration.py`, `backend/tests/test_push_run_migration.py`, `backend/tests/test_agent_push_client_scope.py:155,226,261` (ver Step 4), `backend/tests/test_client_catalog.py` (`PER_CLIENT_KEYS`), `backend/tests/test_client_isolation.py` (`PATHS`)
- Modify: `backend/app/mcp/descriptions/part_b.py:88-155`
- Modify: `frontend/src/modules/agent-push/PushAuthorizationsPage.tsx:355`, `PushBySubnetFlow.tsx:49-95` (la credencial antes que el prefijo), `types.ts`, `frontend/src/modules/credentials/CredentialsPage.tsx:557`, `frontend/src/lib/api.ts:2263,2329-2367`, `frontend/src/i18n/{en,es}/agentPush.ts`
- Test: `backend/tests/test_client_scope_push.py`

**Interfaces:**
- Produces: `push_authorization.client_id`, `push_run.client_id` (C); `push_credential_scope`, `push_credential_realm` (H desde `credentials`), `push_run_target` (H desde `push_run`).
- Produces: el cliente de un push es **el de su credencial** (una credencial de push tiene exactamente un cliente); la dirección tiene que ser un activo avalado de ese cliente, cubierta por una autorización de ese cliente, y la origina un site server de ese cliente.
- Produces: `evaluate_targets(..., client_id: UUID)` (obligatorio); `RunList.ambiguous_count` y `RunPreviewOut.ambiguous_count`.
- Produces: una ejecución activa **por cliente** (D3): `uq_push_run_active` sobre `(org_id, client_id)`.
- Produces: `lock_target_scope(session, org_id, site_ids: Iterable[UUID]) -> None`.

- [ ] **Step 1: Escribir las pruebas que fallan**

```python
# backend/tests/test_client_scope_push.py
"""Push install inside the client (plan 2026-10-26 Task 12; CLAUDE.md 2a,
narrowed: the vouched asset, the authorization and the credential are one
client's)."""

from uuid import UUID

import pytest
from sqlalchemy.exc import DBAPIError

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import PushRun
from tests import agent_push_fixtures as F
from tests.client_scope_support import client_with_site, everything_on
from tests.conftest import ApiEnv

P = "/api/v1/agent-push"


async def test_a_technician_with_both_codes_on_one_client_can_push_there(
    api_env: ApiEnv,
) -> None:
    """research-4 §C: five `has_permission` calls without a client counted
    only every-client grants, so this technician was refused."""
    org = await api_env.create_org("push-own")
    x, x_site = await client_with_site(api_env, org)
    world = await F.push_world(api_env, org, client_id=x, site_id=x_site)
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.post(
        f"{P}/requests",
        json={"credential_id": str(world.credential), "targets": [world.address],
              "mode": "probe"},
        headers=x_tech,
    )
    assert r.status_code in (200, 202), r.text


async def test_another_client_s_credential_cannot_push_to_x(api_env: ApiEnv) -> None:
    org = await api_env.create_org("push-cred")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    world = await F.push_world(api_env, org, client_id=x, site_id=x_site)
    y_credential = await F.seed_credential(api_env, org, client_id=y)
    admin = await api_env.headers(org, await api_env.create_user(org, "org-admin"))
    r = await api_env.client.post(
        f"{P}/requests",
        json={"credential_id": str(y_credential), "targets": [world.address], "mode": "probe"},
        headers=admin,
    )
    # Refused before any job is signed: Y's credential is not X's, so the
    # ladder answers `credential_missing` for X's address (2a(d), narrowed).
    assert r.status_code not in (200, 201, 202), r.text
    assert "credential_missing" in r.text


async def test_two_clients_may_each_run_a_push_at_once(api_env: ApiEnv) -> None:
    """D3: one active run per CLIENT."""
    org = await api_env.create_org("push-runs")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        for client in (x, y):
            credential = await F.seed_credential_in(s, org, client_id=client)
            s.add(PushRun(org_id=org, client_id=client, credential_id=credential,
                          prefix="10.0.0.0/24", list_digest="d" * 64, target_count=0,
                          state="running", created_by=None))
        await s.flush()
    with pytest.raises(DBAPIError, match="uq_push_run_active"):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            credential = await F.seed_credential_in(s, org, client_id=x)
            s.add(PushRun(org_id=org, client_id=x, credential_id=credential,
                          prefix="10.1.0.0/24", list_digest="e" * 64, target_count=0,
                          state="running", created_by=None))
            await s.flush()


async def test_a_run_takes_its_credential_s_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("push-run-client")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    with pytest.raises(DBAPIError, match="push run belongs to its credential's client"):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            credential = await F.seed_credential_in(s, org, client_id=x)
            s.add(PushRun(org_id=org, client_id=y, credential_id=credential,
                          prefix="10.2.0.0/24", list_digest="f" * 64, target_count=0,
                          state="running", created_by=None))
            await s.flush()


async def test_a_push_credential_needs_a_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("push-cred-client")
    admin = await api_env.headers(org, await api_env.create_user(org, "org-admin"))
    r = await api_env.client.post(
        "/api/v1/credentials",
        json={"name": "dom", "type": "windows_domain",
              "secret": {"username": "u", "password": "p", "domain": "AD"}},
        headers=admin,
    )
    assert r.status_code == 422, r.text
```

`F.push_world(...)` y `F.seed_credential_in(...)` no existen todavía: créalos
en `tests/agent_push_fixtures.py` a partir de `seed_asset:100`,
`seed_credential:227`, `seed_push_authorization:264`,
`seed_push_credential_scope:311` y `seed_push_site_server:347`, con el
cliente y la sede como parámetros: `push_world` devuelve un objeto con
`credential`, `address` (de un activo avalado del cliente), la autorización
que cubre su prefijo y un site server del cliente. Ajusta los cuerpos de
`POST /agent-push/requests` y de `POST /credentials` a sus esquemas (`mode`,
`secret`).

- [ ] **Step 2: Comprobar que fallan**

Run: `.venv/bin/pytest -q tests/test_client_scope_push.py`
Expected: FAIL.

- [ ] **Step 3: La migración 0241**

Empieza con el **Bloque común de migración**, y luego:

```python
"""Push install inside the client (plan 2026-10-26 Task 12).

A push credential has exactly one client (Annex A; `push_credential_scope`
and `push_credential_realm` are H from `credentials` and NOT NULL). Existing
push-type credentials with no client are filed under the internal client
first, so their scope and realm rows can inherit one.

`push_authorization` is C (a prefix means something only inside one
client's network). `push_run` is C and its client IS its credential's
(`its_push_run_client_guard`; the credential may later go to NULL, the one
change 0200's `push_run_fixed` allows). One active run per client (D3):
`uq_push_run_active` gains `client_id`. A run's target device is of the
run's client (`its_push_run_target_device_guard`).

Revision ID: 0241
Revises: 0240
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0241"
down_revision: str | None = "0240"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (bloque común aquí)

PUSH_TYPES = "('push_install', 'windows_domain')"
ACTIVE = "state IN ('running', 'paused', 'stopped_credential')"


def upgrade() -> None:
    op.execute(
        f"UPDATE credentials SET client_id = org_id WHERE client_id IS NULL AND type IN {PUSH_TYPES}"
    )
    _add_c("push_authorization")
    _add_c(
        "push_run",
        "COALESCE((SELECT c.client_id FROM credentials c WHERE c.id = push_run.credential_id), "
        "push_run.org_id)",
    )
    _add_h("push_credential_scope", "credentials", "credential_id")
    _add_h("push_credential_realm", "credentials", "credential_id")
    _add_h("push_run_target", "push_run", "run_id")

    op.execute("DROP INDEX uq_push_authorization_active")
    op.execute(
        "CREATE UNIQUE INDEX uq_push_authorization_client_active "
        "ON push_authorization (org_id, client_id, prefix) WHERE revoked_at IS NULL"
    )
    op.execute("DROP INDEX uq_push_run_active")
    op.execute(f"CREATE UNIQUE INDEX uq_push_run_active ON push_run (org_id, client_id) WHERE {ACTIVE}")

    op.execute(
        """
        CREATE FUNCTION its_push_run_client_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE c_client uuid;
        BEGIN
            IF TG_OP = 'UPDATE' AND NEW.client_id IS DISTINCT FROM OLD.client_id THEN
                RAISE EXCEPTION 'a push run never changes client'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.credential_id IS NOT NULL THEN
                SELECT client_id INTO c_client FROM credentials WHERE id = NEW.credential_id;
                IF c_client IS DISTINCT FROM NEW.client_id THEN
                    RAISE EXCEPTION 'push run belongs to its credential''s client'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER its_push_run_client_guard BEFORE INSERT OR UPDATE OF credential_id, "
        "client_id ON push_run FOR EACH ROW EXECUTE FUNCTION its_push_run_client_guard()"
    )
    op.execute(
        """
        CREATE FUNCTION its_push_run_target_device_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE d_client uuid;
        BEGIN
            IF NEW.device_id IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT client_id INTO d_client FROM devices WHERE id = NEW.device_id;
            IF d_client IS DISTINCT FROM NEW.client_id THEN
                RAISE EXCEPTION 'push target device belongs to another client'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER its_push_run_target_device_guard BEFORE INSERT OR UPDATE OF device_id, "
        "client_id ON push_run_target FOR EACH ROW "
        "EXECUTE FUNCTION its_push_run_target_device_guard()"
    )
    for table in ("push_authorization", "push_run", "push_credential_scope",
                  "push_credential_realm", "push_run_target"):
        _isolate(table)


def downgrade() -> None:
    op.execute("DROP TRIGGER its_push_run_target_device_guard ON push_run_target")
    op.execute("DROP FUNCTION its_push_run_target_device_guard()")
    op.execute("DROP TRIGGER its_push_run_client_guard ON push_run")
    op.execute("DROP FUNCTION its_push_run_client_guard()")
    op.execute("DROP INDEX uq_push_run_active")
    op.execute(f"CREATE UNIQUE INDEX uq_push_run_active ON push_run (org_id) WHERE {ACTIVE}")
    op.execute("DROP INDEX uq_push_authorization_client_active")
    op.execute(
        "CREATE UNIQUE INDEX uq_push_authorization_active "
        "ON push_authorization (org_id, prefix) WHERE revoked_at IS NULL"
    )
    _drop_h("push_run_target", "push_run")
    _drop_h("push_credential_realm", "credentials")
    _drop_h("push_credential_scope", "credentials")
    _drop_c("push_run")
    _drop_c("push_authorization")
```

Copia las definiciones viejas de los dos índices de `0077_agent_push.py:118-119`
y `0200_push_runs.py:159` (estado exacto de la lista y forma del `WHERE`) y
úsalas en el `downgrade`; la nueva de `uq_push_run_active` repite el mismo
`WHERE`. Los valores de `type` de una credencial de push son los de
`app/api/v1/credentials.py` (confírmalos). El trigger de herencia de
`push_run_target` (`its_client_from_parent`) corre antes que el guarda
(orden alfabético de nombres), así que el guarda compara con el cliente ya
copiado.

- [ ] **Step 4: El código**

- **Credenciales** (`app/api/v1/credentials.py`): `CredentialCreate.client_id: UUID | None`. Con `device_group_id`, el cliente sale del grupo (422 si el cuerpo lo contradice); sin grupo, `client_id` se carga con `client_for_new_rows`; un tipo de push (`push_install`, `windows_domain`) **sin cliente** es 422 `tr("a push credential belongs to one client")`. Sin cliente y de otro tipo, es global (todos los clientes, como hoy). `CredentialOut.client_id`.
- **Autorizaciones:** `AuthorizationCreate.client_id: UUID` obligatorio (`client_for_new_rows`, sin `unassigned`); el `on_conflict_do_nothing` de `authorization.py:127-130` nombra `["org_id", "client_id", "prefix"]` con su `index_where`.
- **Ámbito de credencial:** `bind_scope` rechaza una credencial sin cliente (422, la misma frase). `resolve_push_credential` (`credential_scope.py:84`) devuelve `credential_missing` si la credencial no es del cliente del objetivo.
- **Escalera de comprobaciones:** `evaluate_targets(..., client_id)` obligatorio; `_vouched_asset` (`preflight.py:356`) y `_authorization_for` (`:406`) filtran por ese cliente; `_site_server_for` ya filtra (`:488`) y ahora recibe ese cliente. El cliente de una petición de push es el de su credencial. `ambiguous_asset` queda como guarda inalcanzable con cliente y su texto (`preflight.py:138-146`, `app/core/i18n/es.py:2715`, `frontend/src/i18n/*/agentPush.ts:372-418`) pasa a hablar del cliente de la credencial, no de un selector.
- **Permisos:** las cinco llamadas de research-4 §C (`preflight.py:744`, `run_list.py:139,281`, `runs.py:462`, `runs.py:647-649`) pasan `client_id=` del cliente de la credencial o de la ejecución.
- **Ejecuciones:** `create_run` toma el cliente de la credencial; el prefijo tiene que ser de una autorización de ese cliente; `_lock_org_runs` toma un bloqueo consultivo con el cliente en la clave (`hashtext('push_run:' || org || ':' || client)`); la comprobación de "ya hay una activa" filtra por cliente. `RunOut`, `AuthorizationOut`, `CredentialScopeOut`, `RunPrefixOut` ganan `client_id`. `GET /agent-push/runs/prefixes` acepta `?credential_id=` y devuelve los prefijos del cliente de esa credencial.
- **Ambiguas contadas** (research-4 §I4): en `run_list.py:310-313`, las direcciones con `ambiguous_asset` suman en `ambiguous_count` (`RunList`, `RunPreviewOut`, `api.py:727-750`, el audit `push.run.started`). Con cliente siempre es 0; una prueba lo fija.
- **El ritmo** (`worker.py:push_run_tick_all_orgs`): `org_ids_with_live_runs` (`runs.py:1151`) devuelve `(org_id, run_id, client_id)`; el worker abre una `org_session(..., clients=ClientScope.only([client_id]))` por ejecución y llama `advance_run` para esa sola. Si la función ya no usa `ClientScope.all()`, borra su entrada en `client_scope.py:148`. El ritmo sigue sin poder iniciar, reanudar ni ampliar una ejecución (2a(e)).
- **Orden de bloqueos** (research-4 §I5): `agent_push/locks.py`:

```python
async def lock_target_scope(session: AsyncSession, org_id: UUID, site_ids: Iterable[UUID]) -> None:
    """Every site, then every client, the batch will mint tokens under, FOR
    SHARE and SORTED, before the first audit write: `archive_site` takes the
    site then the client, and `archive_client` the client then the audit
    chain (`app/services/client_sites.py` LOCK ORDER). Taken one target at a
    time, after the previous target's audit, they would invert that order."""
    ids = sorted(set(site_ids))
    if not ids:
        return
    rows = (
        await session.execute(
            select(Site.id, Site.client_id).where(Site.org_id == org_id, Site.id.in_(ids))
            .order_by(Site.id).with_for_update(read=True)
        )
    ).all()
    clients = sorted({client for _, client in rows})
    await session.execute(
        select(Client.id).where(Client.id.in_(clients)).order_by(Client.id)
        .with_for_update(read=True)
    )
```

  Llamadas: en `dispatch_push_batch`, rama de instalación, después de las negativas (`:633-638`) y antes de `batch_id = uuid4()` (`:645`); en `create_run`, antes de `_lock_org_runs` (`:238`); en `advance_run`, después del `FOR UPDATE` de la ejecución (`:1078-1084`), con las sedes de sus objetivos en espera.
- **Wake-on-LAN** (`provisioning/dispatch.py:117`): `list_site_servers(..., client_id=org_id)` (el aprovisionamiento es del cliente interno y exige todos los clientes; research-4 §I6).
- `runs.py:851,1032`: `publish(..., client_id=run.client_id)` (quita el `None  # ... Task 12`). Auditoría de push con `client_id`.
- `tests/test_agent_push_client_scope.py`: `test_the_pacer_refuses_a_target_that_became_ambiguous` (`:226`) codifica "ambiguo se rechaza" sin cliente. Con la escalera filtrada por cliente, ese objetivo es del cliente de la ejecución y se empuja: **reescríbela a propósito** para que diga lo nuevo ("dos activos avalados de dos clientes con la misma dirección: la ejecución de X empuja el de X"), con un comentario que cite esta tarea. No es debilitar la prueba: la regla que fijaba cambió por decisión (2a(d) estrechado), y la prueba nueva fija la regla nueva.
- `client_scope_catalog.py`: fuera de `PENDING_PHASE_2` `push_authorization`, `push_run`, `push_run_target`, `push_credential_scope`, `push_credential_realm`.
- `tests/test_client_catalog.py::PER_CLIENT_KEYS`: `uq_push_authorization_client_active`, `uq_push_run_active` (columnas `("org_id", "client_id")`).
- `tests/test_client_isolation.py::PATHS`: `"C-push_authorization"`, `"H-push_run_target-via-push_run"`.
- Consola: `PushAuthorizationsPage` y el alta de credenciales de push envían `client_id: internalClientId(me)` con `// PHASE 3`; `PushBySubnetFlow` pide primero la credencial y luego los prefijos de su cliente.
- `HostKeyOut` gana `client_id`, y `PushPreflightPanel.tsx` indexa las claves de host por `(client_id, address)`, no solo por dirección (ledger de la fase 1: dos clientes pueden tener la misma dirección).

- [ ] **Step 5: Comprobar**

Run: `.venv/bin/pytest -q tests/test_client_scope_push.py tests/test_agent_push_api.py tests/test_agent_push_authorization.py tests/test_agent_push_client_scope.py tests/test_agent_push_credential_scope.py tests/test_agent_push_dispatch.py tests/test_agent_push_preflight.py tests/test_push_run_api.py tests/test_push_run_dispatcher.py tests/test_push_run_lifecycle.py tests/test_push_run_list.py tests/test_push_run_pacer.py tests/test_push_run_tick.py tests/test_credentials_windows_domain.py tests/test_agent_push_migration.py tests/test_push_run_migration.py tests/test_site_server_provisioning_wake.py tests/test_client_catalog.py tests/test_client_isolation.py`
Expected: PASS. Luego la suite completa, `mypy`, `ruff`, `npx vitest run src/modules/agent-push src/modules/credentials`, `npx tsc -b`. Y el job de CI `agent-winrm`, que la suite local no corre: no debería cambiar (no se tocan los parámetros), pero se mira en el PR.

- [ ] **Step 6: Commit**

```bash
git add backend frontend
git commit -m "feat(clients): push install stays inside the credential's client; one active run per client (phase 2 Task 12)"
```

---

### Task 13: Repositorios de respaldo de un solo cliente (C1)

Investigación: research-5 (§A backup, §B "Backup", §D backup, §E1-E3).

**Files:**
- Create: `backend/migrations/versions/0242_client_scope_backup_repositories.py`
- Modify: `backend/app/modules/backup/models.py`, `repository.py` (`create_repository`), `policy.py:57-84` (resolución por cliente), `api.py:259,305-372` (`POST /backup/repositories`, `PUT /backup/policies`), `dispatch.py:242-290` (`_dispatch_for_policy`), `offsite.py` (`set_repository_target:369-411`, `prepare_repository:622`), `schema.py`
- Modify: `backend/app/core/client_scope_catalog.py` (las tres tablas salen de `PENDING_PHASE_2`)
- Modify: `backend/tests/backup_dedup_fixtures.py`, `backend/tests/backup_offsite_fixtures.py`, `backend/tests/test_backup_*` que construyen repositorios o políticas; `backend/tests/test_client_catalog.py` (`PER_CLIENT_KEYS`), `backend/tests/test_client_isolation.py` (`PATHS`)
- Modify: `frontend/src/modules/backup/RepositoriesPage.tsx:217`, `BackupPolicyEditor.tsx:323` (el selector de repositorio filtra por el cliente de la política), `frontend/src/lib/api.ts:2800,2811`, tipos
- Test: `backend/tests/test_client_scope_backup.py`

**Interfaces:**
- Produces: `backup_repositories.client_id` C (obligatorio al crear); `backup_chunks`, `backup_chunk_copies` H.
- Produces: `uq_backup_repositories_id_client` y `(repository_id, client_id) REFERENCES backup_repositories (id, client_id)` en `backup_runs` y `backup_points`.
- Produces: una política solo nombra un repositorio de su cliente (trigger `its_backup_policy_repository_guard`); la política por defecto de un cliente es la de `client_id = <cliente>` sin grupo; una política global (sin cliente ni grupo) no puede nombrar un repositorio.

- [ ] **Step 1: Escribir las pruebas que fallan**

```python
# backend/tests/test_client_scope_backup.py
"""A backup repository serves ONE client (plan 2026-10-26 Task 13, C1).
Its master key travels in the signed params of every device that uses it
and chunks are deduplicated per repository: shared, a device of Y would hold
X's key and could probe X's content (research-5 §E1)."""

from uuid import UUID

import pytest
from sqlalchemy.exc import DBAPIError

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import BackupPolicy, DeviceGroup
from tests.client_scope_support import client_with_site
from tests.conftest import ApiEnv

BK = "/api/v1/backup"


async def _admin(env: ApiEnv, org: UUID) -> dict[str, str]:
    return await env.headers(org, await env.create_user(org, "org-admin"))


async def _repo(env: ApiEnv, admin: dict[str, str], client: UUID, name: str) -> str:
    r = await env.client.post(f"{BK}/repositories", json={"name": name, "client_id": str(client)},
                              headers=admin)
    assert r.status_code == 201, r.text
    assert r.json()["client_id"] == str(client)
    return str(r.json()["id"])


async def test_a_repository_needs_a_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("bk-need")
    admin = await _admin(api_env, org)
    r = await api_env.client.post(f"{BK}/repositories", json={"name": "r"}, headers=admin)
    assert r.status_code == 422


async def test_two_clients_may_name_their_repositories_alike(api_env: ApiEnv) -> None:
    org = await api_env.create_org("bk-names")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _repo(api_env, admin, x, "principal")
    await _repo(api_env, admin, y, "principal")


async def test_a_policy_cannot_name_another_client_s_repository(api_env: ApiEnv) -> None:
    org = await api_env.create_org("bk-policy")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    y_repo = await _repo(api_env, admin, y, "y")
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        group = DeviceGroup(org_id=org, client_id=x, name="Servidores")
        s.add(group)
        await s.flush()
        group_id = group.id
    r = await api_env.client.put(
        f"{BK}/policies",
        json={"device_group_id": str(group_id), "repository_id": y_repo, "schedule": "0 2 * * *"},
        headers=admin,
    )
    assert r.status_code == 422, r.text
    with pytest.raises(DBAPIError, match="backup policy names another client's repository"):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            s.add(BackupPolicy(org_id=org, device_group_id=group_id, repository_id=UUID(y_repo),
                               schedule="0 2 * * *"))
            await s.flush()


async def test_a_default_policy_for_every_client_cannot_name_a_repository(
    api_env: ApiEnv,
) -> None:
    org = await api_env.create_org("bk-global")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    repo = await _repo(api_env, admin, x, "x")
    r = await api_env.client.put(
        f"{BK}/policies", json={"repository_id": repo, "schedule": "0 2 * * *"}, headers=admin
    )
    assert r.status_code == 422, r.text
```

Ajusta los cuerpos de `PUT /backup/policies` y las columnas de `BackupPolicy`
(`schedule`, retención...) a `backup/schema.py` y al modelo. Añade, con
`tests/backup_dedup_fixtures.py`, una prueba de despacho: un equipo de Y cuya
política (forzada con el motor de administración, sin el trigger)
nombra el repositorio de X **no** recibe un trabajo de respaldo, y el motivo
queda registrado (spec §5 prueba 4: la ventana corre con alcance `all`).

- [ ] **Step 2: Comprobar que fallan**

Run: `.venv/bin/pytest -q tests/test_client_scope_backup.py`
Expected: FAIL.

- [ ] **Step 3: La migración 0242**

Empieza con el **Bloque común de migración**, y luego. Confirma el nombre de
la clave única de `backup_repositories` (research-5 §A: 0014 la creó en
línea, "(PG)" `backup_repositories_org_id_name_key`):

```python
"""A backup repository serves one client (plan 2026-10-26 Task 13, C1).

Existing repositories go to the internal client, and so do the group-less,
client-less ("every client") policies that named one: a policy for every
client can no longer name a repository, which belongs to one. Customer
clients then get no backup until a person gives each its own repository and
default policy -- refused by silence, the right direction (no data to keep,
spec P7).

Runs and points carry their device's client (H, phase 1); the repository
they name must be the same client's: `(repository_id, client_id) REFERENCES
backup_repositories (id, client_id)`, added NOT VALID -- existing rows of a
device in a customer client that wrote to an internal repository are history,
not checked; every new row is.

Revision ID: 0242
Revises: 0241
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0242"
down_revision: str | None = "0241"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (bloque común aquí)

H = [
    ("backup_chunks", "backup_repositories", "repository_id"),
    ("backup_chunk_copies", "backup_repositories", "repository_id"),
]
RUN_TABLES = ["backup_runs", "backup_points"]


def upgrade() -> None:
    _add_c("backup_repositories")
    for table, parent, fk in H:
        _add_h(table, parent, fk)
    op.execute("ALTER TABLE backup_repositories DROP CONSTRAINT backup_repositories_org_id_name_key")
    op.execute(
        "ALTER TABLE backup_repositories ADD CONSTRAINT uq_backup_repositories_client_name "
        "UNIQUE (org_id, client_id, name)"
    )
    op.execute(
        "ALTER TABLE backup_repositories ADD CONSTRAINT uq_backup_repositories_id_client "
        "UNIQUE (id, client_id)"
    )
    for table in RUN_TABLES:
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_repository_client "
            "FOREIGN KEY (repository_id, client_id) "
            "REFERENCES backup_repositories (id, client_id) NOT VALID"
        )
    op.execute(
        "UPDATE backup_policies SET client_id = org_id "
        "WHERE client_id IS NULL AND device_group_id IS NULL AND repository_id IS NOT NULL"
    )
    op.execute(
        """
        CREATE FUNCTION its_backup_policy_repository_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE r_client uuid;
        BEGIN
            IF NEW.repository_id IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT client_id INTO r_client FROM backup_repositories WHERE id = NEW.repository_id;
            IF r_client IS DISTINCT FROM NEW.client_id THEN
                RAISE EXCEPTION 'backup policy names another client''s repository'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    # After `its_client_from_parent` (alphabetical: c < z), which has already
    # copied the group's client when the policy has a group.
    op.execute(
        "CREATE TRIGGER its_zz_backup_policy_repository_guard BEFORE INSERT OR UPDATE OF "
        "repository_id, client_id, device_group_id ON backup_policies "
        "FOR EACH ROW EXECUTE FUNCTION its_backup_policy_repository_guard()"
    )
    for table in ["backup_repositories", *[t for t, _, _ in H]]:
        _isolate(table)


def downgrade() -> None:
    op.execute("DROP TRIGGER its_zz_backup_policy_repository_guard ON backup_policies")
    op.execute("DROP FUNCTION its_backup_policy_repository_guard()")
    for table in RUN_TABLES:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT fk_{table}_repository_client")
    op.execute("ALTER TABLE backup_repositories DROP CONSTRAINT uq_backup_repositories_id_client")
    op.execute("ALTER TABLE backup_repositories DROP CONSTRAINT uq_backup_repositories_client_name")
    op.execute(
        "ALTER TABLE backup_repositories ADD CONSTRAINT backup_repositories_org_id_name_key "
        "UNIQUE (org_id, name)"
    )
    for table, parent, _ in reversed(H):
        _drop_h(table, parent)
    _drop_c("backup_repositories")
```

Comprueba que el trigger de herencia de `backup_policies` se llama
`its_client_from_parent` (0231) y que Postgres dispara los `BEFORE` por
orden de nombre: el guarda, `its_zz_...`, ve el cliente ya copiado del
grupo. Un `UPDATE` de la fila migrada no cambia su cliente: el trigger de
0231 solo copia cuando hay grupo.

- [ ] **Step 4: El código**

- `RepositoryCreateRequest.client_id: UUID` obligatorio (`client_for_new_rows`, sin `unassigned`); `RepositoryOut.client_id`. La clave maestra y `storage_ref` no cambian.
- `PUT /backup/policies`: el cliente de la política es el de su grupo, o el `client_id` del cuerpo si no hay grupo (nuevo campo opcional); el repositorio tiene que ser de ese cliente (422 `tr("that repository belongs to another client")`); sin grupo ni cliente, nombrar un repositorio es 422 `tr("a policy for every client cannot name a repository; give each client its own")`.
- `policy.py:57-84`: la resolución es política del grupo → política por defecto del cliente del equipo (`client_id = device.client_id`, sin grupo) → política global; una política global ya no tiene repositorio, así que solo sirve para lo que no es el repositorio (horario, retención).
- `dispatch.py::_dispatch_for_policy`: si `repo.client_id != device.client_id`, no despacha y registra el motivo (como cualquier otra negativa del módulo) — la red bajo la base, por si una fila antigua lo permite.
- `offsite.py::set_repository_target`: el destino externo tiene que ser global o del cliente del repositorio (422). `prepare_repository`: envuelve la clave solo con las claves de recuperación globales o del cliente del repositorio (`client_id IS NULL OR client_id = repo.client_id`).
- Auditoría de repositorios y políticas con `client_id`.
- `client_scope_catalog.py`: fuera de `PENDING_PHASE_2` `backup_repositories`, `backup_chunks`, `backup_chunk_copies`.
- `tests/test_client_catalog.py::PER_CLIENT_KEYS`: `uq_backup_repositories_client_name`.
- `tests/test_client_isolation.py::PATHS`: `"C-backup_repositories"`, `"H-backup_chunks-via-backup_repositories"`.
- Consola: `RepositoriesPage` envía `client_id: internalClientId(me)` con `// PHASE 3`; `BackupPolicyEditor` muestra solo repositorios del cliente de la política.

- [ ] **Step 5: Comprobar**

Run: `.venv/bin/pytest -q tests/test_client_scope_backup.py tests/test_backup_repository.py tests/test_backup_policy.py tests/test_backup_dispatch.py tests/test_backup_offsite.py tests/test_backup_dedup.py tests/test_client_catalog.py tests/test_client_isolation.py` (usa `ls tests | grep backup` para los nombres reales). Luego la suite completa, `mypy`, `ruff`, `npx vitest run src/modules/backup`, `npx tsc -b`. El job de CI `backup end to end` corre un respaldo y una restauración reales: se mira en el PR.

- [ ] **Step 6: Commit**

```bash
git add backend frontend
git commit -m "feat(clients): a backup repository serves one client (phase 2 Task 13)"
```

---

### Task 14: Configuración por cliente y permisos que solo contaban "todos los clientes"

Investigación: research-6 §3 (T4-C2) y §4 (product gap); ledger de la fase 1 ("PRODUCT GAP (phase 2)", "T4-C2").

Lo que la fase 1 dejó global por no poder elegir cliente: documentación
(artículos, registros, documentos), aprobaciones de parches. (Las políticas
de SLA y las credenciales sin grupo ya ganaron cliente en las Tasks 3 y 12.)
Y las comprobaciones dentro de una ruta que exigían el permiso en todos los
clientes (T4-C2), ahora que los registros llevan cliente.

**Files:**
- Modify: `backend/app/modules/documentation/api.py:24-27,77-80,144,397`, `service.py:123-152,218-250` (`create_article`, `create_record`), `documents_api.py:58-65,185`, `documents.py:119-147`, `service.py::_LINK_TARGET_MODELS` (enlaces)
- Modify: `backend/app/modules/patch/api.py:205-226,957,971,634`, `approve.py:92-132,168-184` (`_approve_one`), `apply.py:76,115`, `backend/app/modules/reporting/definitions/patch_compliance.py:321`
- Modify: `backend/app/modules/health/api.py:1327,1373`, `health/actions.py:265`
- Modify: `backend/app/modules/ai_admin/api.py:489,637,1077-1103,1185,1224`, `backend/app/core/ai/act.py:951,1240`
- Modify: `backend/app/modules/ai_copilot/api.py:141-144,323`
- Modify: `backend/app/modules/remote/api.py:60-80,258,291`, `backend/app/modules/remote/tunnel.py:48-61`
- Modify: `backend/app/modules/reporting/api.py:100`, `backend/app/modules/reporting/deps.py:75`
- Modify: `frontend/src/lib/api.ts:2027,2080,2134` (documentación), las páginas de aprobación de parches
- Test: `backend/tests/test_client_scoped_config.py`

**Interfaces:**
- Produces: `ArticleCreate.client_id`, `RecordCreate.client_id`, `DocumentCreate.client_id`, el cuerpo de `POST /patch/approvals` y `/approvals/bulk` con `client_id: UUID | None = None`. Sin él, la fila es global y exige todos los clientes (la guarda de `app/core/global_rows.py`, como hoy); con él, se carga con `client_for_new_rows` y basta el permiso en ese cliente.
- Produces: una aprobación de parche de un cliente solo autoriza equipos de ese cliente; una global, los de todos.

- [ ] **Step 1: Escribir las pruebas que fallan**

```python
# backend/tests/test_client_scoped_config.py
"""Per-client configuration and per-record permission checks (plan
2026-10-26 Task 14; phase-1 ledger PRODUCT GAP and T4-C2)."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Device, Patch, PatchApproval
from tests.client_scope_support import client_with_site, everything_on
from tests.conftest import ApiEnv


async def test_a_technician_of_x_documents_x(api_env: ApiEnv) -> None:
    org = await api_env.create_org("cfg-docs")
    x, _ = await client_with_site(api_env, org)
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.post(
        "/api/v1/docs/articles", json={"title": "VPN de X", "body": "", "client_id": str(x)},
        headers=x_tech,
    )
    assert r.status_code == 201, r.text
    assert r.json()["client_id"] == str(x)
    global_one = await api_env.client.post(
        "/api/v1/docs/articles", json={"title": "Global", "body": ""}, headers=x_tech
    )
    assert global_one.status_code == 403  # a global row is every client's config


async def test_a_client_s_patch_approval_reaches_only_that_client(api_env: ApiEnv) -> None:
    """research-6 §4: approvals were matched by (org, ring, patch) only. Under
    the apply sweep (scope all) an approval of X authorised Y's devices."""
    from app.modules.patch.apply import _ring_approved_patch_ids

    org = await api_env.create_org("cfg-patch")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        patch = Patch(org_id=org, kb="KB1", title="t", os_family="windows", severity="important")
        s.add(patch)
        await s.flush()
        s.add(PatchApproval(org_id=org, client_id=x, patch_id=patch.id, ring_name="prod"))
        y_device = Device(org_id=org, client_id=y, site_id=y_site, hostname="y", os_family="windows")
        x_device = Device(org_id=org, client_id=x, site_id=x_site, hostname="x", os_family="windows")
        s.add_all([y_device, x_device])
        await s.commit()
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        for_x = await _ring_approved_patch_ids(
            s, org_id=org, ring_name="prod", patch_ids={patch.id}, client_id=x
        )
        for_y = await _ring_approved_patch_ids(
            s, org_id=org, ring_name="prod", patch_ids={patch.id}, client_id=y
        )
    assert for_x == {patch.id} and for_y == set()


async def test_a_technician_of_x_runs_a_remediation_on_x_s_alert(api_env: ApiEnv) -> None:
    """T4-C2 item 10 (`health/actions.py:265`): the action's second
    permission was looked up on every client only."""
    from tests.health_fixtures import seed_alert_with_remediation  # existing helper or new

    org = await api_env.create_org("cfg-remed")
    x, x_site = await client_with_site(api_env, org)
    alert = await seed_alert_with_remediation(api_env, org, client_id=x, site_id=x_site)
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.post(
        f"/api/v1/health/alerts/{alert.alert_id}/actions/{alert.action_id}/run", headers=x_tech
    )
    assert r.status_code in (200, 202), r.text


async def test_the_report_catalogue_counts_codes_held_on_any_client(api_env: ApiEnv) -> None:
    """T4-C2 items 2-3: a technician holding every code on X sees the reports
    they can run (over X, by RLS), not an empty catalogue."""
    org = await api_env.create_org("cfg-reports")
    x, _ = await client_with_site(api_env, org)
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.get("/api/v1/reports", headers=x_tech)
    assert r.status_code == 200 and len(r.json()) > 0, r.text
```

`tests/health_fixtures.py::seed_alert_with_remediation` y la ruta exacta de
"ejecutar una remediación" (`health/api.py:1327`) se toman de las pruebas de
salud existentes (`grep -n "remediat" tests/test_health_*.py`): si hay un
helper que ya siembra una alerta con una acción, úsalo y añade solo el
cliente; si no, créalo ahí. Las columnas de `Patch` y `PatchApproval` son las
de sus modelos.

- [ ] **Step 2: Comprobar que fallan**

Run: `.venv/bin/pytest -q tests/test_client_scoped_config.py`
Expected: FAIL.

- [ ] **Step 3: Configuración con cliente**

- **Documentación:** los tres cuerpos de alta ganan `client_id: UUID | None = None`. Con él, `client_for_new_rows` (404/409) y la fila se escribe con ese cliente; sin él, igual que hoy (global; la guarda de `global_rows` responde 403 a un alcance que no es `all`). Un enlace (`_LINK_TARGET_MODELS`) de un artículo de un cliente solo apunta a objetivos de ese cliente o globales (422).
- **Aprobaciones de parches:** el cuerpo gana `client_id`; `_approve_one` (`approve.py:105`) busca y escribe la aprobación de **ese** dueño (`client_id == :client` o `IS NULL`), no siempre la global (`:125-132,179-184`), y la población del anillo (`:92-103`) son solo equipos de ese cliente cuando lo hay. **Lectura:** `_ring_approved_patch_ids` (`apply.py:76`) y la búsqueda de `:115` reciben `client_id` (el del equipo) y aceptan `PatchApproval.client_id.is_(None) | (PatchApproval.client_id == client_id)`; lo mismo `patch/api.py:634` y `reporting/definitions/patch_compliance.py:321`, que comparan con el cliente de cada equipo. Sus llamadores pasan `device.client_id` (el barrido agrupa por cliente). `record_auto_change` de una aprobación de cliente usa ese cliente (Task 7 ya lo dejó así para las globales: C8).

- [ ] **Step 4: Permisos por registro (T4-C2)**

Cada comprobación de research-6 §3, con el cliente del registro:

| # | Dónde | Cambio |
|---|---|---|
| 2-3 | `reporting/api.py:100`, `reporting/deps.py:75` | `codes_held_anywhere(session, user_id)` en vez de `user_permission_codes` sin cliente: el informe corre con la RLS de la petición |
| 10 | `health/api.py:1327,1373` → `health/actions.py:265` | el segundo permiso de la acción con `client_id=<cliente de la alerta>` (leído de la base: H) |
| 11 | `ai_admin/api.py:489,637,1103,1185,1224`, `core/ai/act.py:951,1240` | `ai.act.propose`/`confirm` y `devices.read` con `client_id=<cliente del equipo de la acción>`; el prefiltro de herramientas (`:734`, `tools_for_permissions`) usa `codes_held_anywhere` (es ergonomía: la ejecución vuelve a comprobar) |
| 12 | `ai_copilot/api.py:144,323` | `ai.use` y la lista de herramientas con `client_id=<cliente del ticket>` |
| 13 | `remote/api.py:60-80` (lecturas `:258,291`), `remote/tunnel.py:61` | grabaciones y "detener la sesión de otro" con `client_id=<cliente del equipo de la sesión>` |

Se quedan como están, por diseño: `/me` (fase 3), `agent_update/api.py:54`
(los despliegues del agente son G), `mcp/api.py:462` (revocar a todos),
`rbac_admin.py` (la salvaguarda de no quedarse sin administrador) y
`provisioning/dispatch.py:107` (el aprovisionamiento exige todos los
clientes; research-6 §3 punto 6). Un comentario en cada una dice por qué.

- [ ] **Step 4b: Lo que la fase 1 dejó anotado para esta fase**

- **Resolución de salud** (`health/resolution.py:156-165`): la asignación por defecto de la organización suponía una fila por nivel. Desde 0231 puede haber una global y una por cliente (`uq_health_assignment_client_org`). El orden pasa a ser: asignación del equipo → del grupo → por defecto **del cliente del equipo** → global; con predicado de cliente explícito (corre con alcance `all` en el barrido de salud). Prueba: dos clientes, una asignación por defecto en X, un equipo de Y recibe la global.
- **Carreras de alta** (ledger "upsert first-writer races"): `change_mgmt/gates.py::set_gate` y `agent_push/host_key.py::record_observation` pasan a `INSERT ... ON CONFLICT` sobre su clave por cliente, en vez de buscar y luego insertar (dos escritores a la vez daban 500).
- **Cambiar la sede de un equipo** (spec §1.5; ledger de la Task 9 de la fase 1): `PATCH /devices/{id}` acepta `site_id`; la sede se carga con `site_for_new_rows` y tiene que ser del cliente del equipo (422; el trigger `its_device_client_guard` es la red). Auditoría `device.site_changed` con las dos sedes, y su frase en `auditVocabulary.ts`. Prueba: dentro del cliente, 200; a una sede de otro cliente, 422.

- [ ] **Step 5: Consola**

`createArticle`, `createRecord`, `createDocument` (`lib/api.ts:2027,2080,2134`)
y la aprobación de parches envían `client_id: internalClientId(me)` con
`// PHASE 3` **solo si** el formulario no ofrece hoy "para todos los
clientes": como la consola de la fase 1 trabaja en el cliente interno, lo
que un técnico documenta va al interno, y lo global queda para quien lo pida
explícitamente (un interruptor "Para todos los clientes", visible solo con
alcance de todos los clientes, que omite `client_id`).

- [ ] **Step 6: Comprobar**

Run: `.venv/bin/pytest -q tests/test_client_scoped_config.py tests/test_documentation_api.py tests/test_patch_api.py tests/test_patch_apply.py tests/test_patch_approve.py tests/test_reporting_api.py tests/test_health_actions.py tests/test_ai_admin_api.py tests/test_ai_copilot.py tests/test_remote_api.py tests/test_global_rows_api.py tests/test_global_writes.py` (nombres reales con `ls tests | grep -E "doc|patch|report|health_act|ai_|remote|global"`). Luego la suite completa, `mypy`, `ruff`, `npx vitest run`, `npx tsc -b`.

- [ ] **Step 7: Commit**

```bash
git add backend frontend
git commit -m "feat(clients): per-client docs and patch approvals; permission checks on the record's client (phase 2 Task 14)"
```

---

### Task 15: Cierre — `domain_events` en la regla O, sin `PENDING_PHASE_2`, documentación y verificación

Investigación: research-6 §2 y §6.

**Files:**
- Create: `backend/migrations/versions/0243_client_rows_for_global_tables.py`
- Modify: `backend/app/core/global_rows.py` (fuera `NULL_INSERTABLE_FROM_ANY_SCOPE`), `backend/tests/client_scope_support.py` (fuera `NULL_WRITABLE_FROM_ANY_SCOPE`), `backend/tests/test_client_scope_rls.py:240-262,566-590`
- Modify: `backend/app/core/client_scope_catalog.py` (se borra `PENDING_PHASE_2`; `PHASE_1` pasa a llamarse `SCOPED`; docstring), y cada uso de `PHASE_1` (`app/gateway/dispatch.py:10`, `tests/test_client_catalog.py`, `tests/client_scope_support.py`, `tests/test_client_isolation.py`, `tests/test_ui_ws.py:445`)
- Modify: `backend/tests/test_client_catalog.py` (se borra `test_pending_tables_are_in_the_catalog_and_not_global`)
- Modify: `CLAUDE.md` (la nota "Alcance de la fase 1" bajo 5a)
- Modify: `docs/superpowers/specs/2026-10-23-msp-clientes-design.md` (§7: puntos nuevos; §7 punto 9 queda como historia)
- Modify: `docs/modules/service-desk/spec.md` §2, `docs/modules/sla/spec.md` §3, `docs/modules/agent-distribution/spec-push-install.md` §3 y §6.3, la spec de IPAM en `docs/modules/`, `docs/04-data-model.md`
- Test: `backend/tests/test_client_catalog.py` y `backend/tests/test_phase2_foundations.py` (amplía)

- [ ] **Step 1: Escribir las pruebas que fallan** (en `test_phase2_foundations.py`)

```python
from pathlib import Path  # noqa: E402

import app.core.client_scope_catalog as catalog  # noqa: E402


def test_no_family_is_pending_any_more() -> None:
    assert not hasattr(catalog, "PENDING_PHASE_2")
    assert not hasattr(catalog, "PHASE_1")
    assert set(catalog.SCOPED) == {
        t for t, e in catalog.CATALOG.items() if e.kind != "G"
    }


def test_only_a_platform_probe_publishes_without_a_client() -> None:
    """Task 1 made `client_id` a required keyword of `publish`; every family
    task replaced its `None`. What is left is the platform probe."""
    root = Path(__file__).resolve().parents[1] / "app"
    hits = [
        f"{p.relative_to(root)}:{n}"
        for p in root.rglob("*.py")
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if "client_id=None" in line and "plan 2026-10-26" in line
    ]
    assert hits == [], hits


async def test_a_narrowed_scope_cannot_write_a_client_less_domain_event(
    admin_engine, app_engine
) -> None:
    """`domain_events` back under the generic O check (0243): a NULL client
    is written only under scope all."""
    org = await _org(admin_engine)
    customer = await _customer(admin_engine, org)
    try:
        async with org_session(org, engine=app_engine, clients=ClientScope.only([customer])) as s:
            await publish(
                s, org_id=org, event_type="device.joined_via_link", subject_type="device",
                subject_id=uuid4(), payload={}, actor_user_id=None, client_id=None,
            )
    except Exception as exc:  # noqa: BLE001 -- RLS or the global-rows guard
        assert "row-level security" in str(exc) or "every client" in str(exc)
    else:
        raise AssertionError("a narrowed scope wrote an event for every client")
```

- [ ] **Step 2: Comprobar que fallan**

Run: `.venv/bin/pytest -q tests/test_phase2_foundations.py -k "pending or platform_probe or client_less"`
Expected: FAIL (`PENDING_PHASE_2` todavía existe; `domain_events` sigue aceptando `NULL` desde cualquier alcance).

- [ ] **Step 3: La migración 0243**

```python
"""`domain_events` under the generic O check (plan 2026-10-26 Task 15).

Phase 1 (0231) let any pinned scope write a client-less event because
`events.publish` could not name the subject's client. Every caller names it
since phase 2 (Task 1 made it a required keyword; each family task filled
it), so a NULL client -- an event for every client -- is written only under
scope all, as on every other O table.

Revision ID: 0243
Revises: 0242
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0243"
down_revision: str | None = "0242"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCOPE = "current_setting('app.client_scope', true)"
VISIBLE = "its_client_visible(client_id)"
SCOPE_PINNED = (
    f"({SCOPE} = 'all' OR ({SCOPE} = 'list' AND "
    "nullif(current_setting('app.current_clients', true), '') IS NOT NULL))"
)
READ = f"(client_id IS NULL AND {SCOPE_PINNED}) OR {VISIBLE}"
MODIFY = f"(client_id IS NULL AND {SCOPE} = 'all') OR {VISIBLE}"


def _policy(check: str) -> None:
    op.execute("DROP POLICY client_isolation ON domain_events")
    op.execute(
        "CREATE POLICY client_isolation ON domain_events AS RESTRICTIVE FOR ALL "
        f"USING ({READ}) WITH CHECK ({check})"
    )


def upgrade() -> None:
    _policy(MODIFY)


def downgrade() -> None:
    _policy(READ)
```

(Las expresiones son las de 0231, copiadas: `tests/client_scope_support.py::expected_client_policies` las compara deparseadas, así que una diferencia de forma se ve.)

- [ ] **Step 4: El código y el catálogo**

- `app/core/global_rows.py`: se borra `NULL_INSERTABLE_FROM_ANY_SCOPE` y su uso (`:73,287,300`): `domain_events` es una O más. `tests/client_scope_support.py`: se borra `NULL_WRITABLE_FROM_ANY_SCOPE` y su rama en `expected_client_policies`. `tests/test_client_scope_rls.py:240-262,566-590`: las dos pruebas que fijaban la excepción pasan a fijar la regla general (un `NULL` desde un alcance limitado se rechaza); el cambio es el que pide este paso, no un debilitamiento.
- `client_scope_catalog.py`: se borra `PENDING_PHASE_2`; `PHASE_1` pasa a `SCOPED = {t: e for t, e in CATALOG.items() if e.kind != "G"}`; el docstring pierde el párrafo de `PENDING_PHASE_2` y dice que toda tabla C, H u O está bajo la política. Cada `PHASE_1` del repositorio pasa a `SCOPED` (`grep -rn "PHASE_1" backend`).
- `tests/test_client_catalog.py`: se borra `test_pending_tables_are_in_the_catalog_and_not_global` (su objeto deja de existir; `test_no_family_is_pending_any_more` ocupa su lugar).
- `ALL_SCOPE_ALLOWLIST`: `pytest -q tests/test_client_scope.py` (o la prueba de la lista) dice qué entradas ya no se usan; se borran.

- [ ] **Step 5: Documentación**

- `CLAUDE.md`, bajo 5a: el párrafo *"Alcance de la fase 1 (2026-10-24)"* se sustituye por:

  > *Fase 2 (completada el <fecha del merge, escrita en el commit>):* la base de datos impone 5a en toda tabla C, H u O de `client_scope_catalog.SCOPED` (`backend/app/core/client_scope_catalog.py`); ninguna familia queda pendiente. Las tablas sin cliente que guardan datos de todos los clientes — el registro de auditoría (con un `client_id` informativo), el registro de correo entrante y el de entregas de notificaciones — solo se leen con el permiso en todos los clientes. La instalación remota (2a) queda además dentro de un cliente: el activo avalado, la autorización, la credencial y el site server son del mismo cliente.

  La fecha se escribe en el commit de esta tarea con la fecha real del día (`date -u +%F`), no con un marcador.

- Spec de clientes, §7: puntos nuevos 10 a 16, uno por decisión de esta fase (D1-D4, C1, C7 y C9) y por cada refinamiento de implementación (`ipam_subnet` H desde `ipam_vrf`; `deployments` con cliente por trigger desde su objetivo; una ejecución de push por cliente; repositorio de respaldo de un cliente; presupuesto con moneda y año fiscal por cliente; avisos solo a quien tiene un rol en el cliente). El punto 9 queda como historia, con una línea "Cerrado por la fase 2 (plan 2026-10-26)". El Anexo A ya cambió en la Task 10.
- Módulos: un párrafo con fecha en cada spec de la lista de Files que diga qué cambió para ese módulo y remita al plan.

- [ ] **Step 6: Verificación completa**

Desde `backend/`:

```bash
.venv/bin/pytest -q
.venv/bin/mypy app
.venv/bin/ruff check app tests
.venv/bin/alembic upgrade head && .venv/bin/alembic downgrade base && .venv/bin/alembic upgrade head
grep -rn "PENDING_PHASE_2\|PHASE_1" app tests ../CLAUDE.md ../docs/superpowers/specs || echo "none left"
grep -rn "plan 2026-10-26 Task [0-9]* " app | grep -i "none\|org_id  #" || echo "no placeholder left"
```

Desde `frontend/`:

```bash
npx vitest run
npx tsc -b
npm run build
```

Expected: todo en verde; `none left` y `no placeholder left`.

- [ ] **Step 7: Commit**

```bash
git add backend frontend CLAUDE.md docs
git commit -m "feat(clients): domain events under the O rule; no family pending; phase 2 docs (phase 2 Task 15)"
```
