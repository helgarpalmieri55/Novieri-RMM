# Clientes MSP — Fase 1: núcleo. Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clientes y sedes existen. Cada sesión con la base de datos lleva un
alcance de clientes, y la RLS lo impone en todas las tablas de la fase 1:
equipos, grupos, inscripción, todo lo que cuelga de un equipo o de un trabajo, y
las tablas de configuración opcional.

**Architecture:**
- Dos migraciones. La 0230 crea `clients` y `sites` y adapta `users` y
  `user_roles`. La 0231 añade `client_id`, una política RLS **restrictiva** y,
  donde aplica, un trigger de herencia a cada tabla de la fase 1.
- Un tipo `ClientScope` viaja desde la petición hasta `org_session`, que lo fija
  en dos GUC.
- El alcance de cada petición sale del permiso de la ruta: los clientes donde
  el técnico tiene ese permiso, estrechados por `X-Client-Id`.
- Dos codemods de libcst actualizan las 3.028 llamadas a `org_session` y los
  constructores de `Device`/`DeviceGroup` en las pruebas.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, PostgreSQL 16 (RLS), pytest, libcst.

**Spec:** `docs/superpowers/specs/2026-10-23-msp-clientes-design.md`.

## Global Constraints

- RLS falla cerrado: sin GUC de alcance, cero filas y ninguna escritura (spec §2.2).
- Las políticas por cliente son `AS RESTRICTIVE`: se suman a `tenant_isolation`, que no se toca.
- `org_session` exige `clients=` como argumento de palabra clave **sin valor por defecto** (spec §2.3).
- `ClientScope.all()` en `app/` solo en los archivos de `ALL_SCOPE_ALLOWLIST` (Task 3).
- **Convención de id:** el cliente `internal` de una organización y su sede "Principal" tienen `id = organizations.id`. El cliente `unassigned` tiene un id aleatorio.
- `devices.client_id` es inmutable; `devices.site_id` solo puede apuntar a una sede del mismo cliente.
- Ninguna prueba existente se borra, se salta ni se debilita (`CLAUDE.md`).
- Migraciones solo hacia adelante. Su `downgrade()` tiene que funcionar, porque la CI corre `upgrade → downgrade → upgrade`.
- Todo texto nuevo dirigido a una persona pasa por `tr()` (`app/core/i18n`) y tiene traducción al español.
- Comandos de verificación, desde `backend/`:
  - `.venv/bin/pytest -q <archivo>`
  - `.venv/bin/mypy app`
  - `.venv/bin/ruff check app tests`
  - Postgres y Redis tienen que estar arriba: `service postgresql start; redis-cli ping || redis-server --daemonize yes`.

## Fuera de esta fase (fase 2)

Familias que necesitan que el código de su módulo ponga `client_id`. Quedan en
`PENDING_PHASE_2` (Task 5), y la fase 2 tiene que vaciar ese conjunto:

- **Tickets:** `ticket`, `ticket_comment`, `ticket_attachment`, `ticket_approval`, `ticket_status_history`, `ticket_sla`.
- **Personas:** `person`, `person_login`, `person_checklist`, `person_checklist_step`.
- **Licencias:** `license`, `license_allocation`, `license_compliance_snapshot`.
- **Suscripciones:** `subscription`, `seat_assignment`, `subscription_event`, `subscription_alert_sent`.
- **Despliegues:** `deployments`.
- **IPAM:** `ipam_vrf`, `ipam_vlan`, `ipam_subnet`, `ipam_range`, `ipam_address`, `ipam_observation`, `ipam_drift`, `ipam_scan_authorization`, `ipam_sweep_run`, y la fusión de `ipam_site` en `sites`.
- **Instalación remota:** `push_authorization`, `push_run`, `push_run_target`, `push_credential_scope`, `push_credential_realm`. `push_host_key` es O y entra en la fase 1, porque no necesita cambios de código.
- **Respaldos:** `backup_repositories`, `backup_chunks`, `backup_chunk_copies`.
- **Módulos opcionales:** `bcp_plan`, `bcp_test`, `bcp_test_step`, `bcp_finding`, `budget_settings`, `cost_center`, `expense_category`, `budget`, `budget_line`, `expense`, `change_request`, `change_approval`, `change_link`, `change_external_ref`.

**Cobertura del spec que también va en la fase 2:**
- Mover tickets entre clientes y su trigger de propagación (§2.4).
- `audit_events.client_id` informativo (§2.5).
- El rechazo de escrituras en módulos opcionales apagados (§3.2).
- La asignación de tickets por correo (§3.3).
- Las pruebas 7 (parte de tickets) y 9 del §5.

**Fase 3:** la consola (§3.1, §3.4 y la prueba 14 del §5).

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| `backend/migrations/versions/0230_clients_and_sites.py` | Crea `clients` y `sites`, el trigger que crea los clientes de sistema de cada organización, `users.kind` y `users.client_id`, y cambia `user_roles.device_group_id` por `client_id` |
| `backend/migrations/versions/0231_client_scope_rls.py` | Función `its_client_visible`, triggers de herencia, `client_id` y políticas restrictivas en las tablas de la fase 1 |
| `backend/app/models/clients.py` | Modelos `Client` y `Site` |
| `backend/app/core/client_scope.py` | Tipo `ClientScope`, parseo de `X-Client-Id` y lista revisada de `ClientScope.all()` |
| `backend/app/core/client_scope_catalog.py` | Catálogo C/H/O/G (Anexo A) y `PENDING_PHASE_2` |
| `backend/app/core/tenancy.py` | `org_session(..., clients=)` y `portal_session(..., clients=)` |
| `backend/app/core/rbac.py` | Permisos por cliente y `clients_holding()` |
| `backend/app/api/deps.py` | `AuthContext.clients`, rechazo de contactos y estrechamiento por permiso |
| `backend/app/services/clients.py` | Crear y archivar clientes, sedes y contactos |
| `backend/app/api/v1/clients.py` | Rutas de §3.2 del spec |
| `backend/scripts/codemods/add_client_scope.py` | Codemod de `org_session` |
| `backend/scripts/codemods/add_device_client.py` | Codemod de `Device(`/`DeviceGroup(` en las pruebas |
| `backend/tests/test_client_scope.py` | Tipo y GUC |
| `backend/tests/test_client_catalog.py` | Pruebas 1, 2 y 11 del spec §5 |
| `backend/tests/test_client_isolation.py` | Pruebas 3 a 8 del spec §5 |
| `backend/tests/test_clients_api.py` | Rutas de clientes, sedes y contactos |

---

### Task 1: Migración 0230 y modelos `Client` / `Site`

**Files:**
- Create: `backend/migrations/versions/0230_clients_and_sites.py`
- Create: `backend/app/models/clients.py`
- Modify: `backend/app/models/__init__.py` (exportar `Client`, `Site`)
- Modify: `backend/app/models/core.py` (`User.kind`, `User.client_id`; `UserRole.client_id` en lugar de `device_group_id`)
- Test: `backend/tests/test_clients_schema.py`

**Interfaces:**
- Produces: tablas `clients` (`id, org_id, name, kind, status, email_domains text[], phone, address, website, notes, enabled_modules text[], created_at, updated_at`) y `sites` (`id, org_id, client_id, name, address, description, egress_networks cidr[], doc_record_id, created_at, updated_at`). Modelos `app.models.Client` y `app.models.Site`. Constantes `CLIENT_KIND_INTERNAL = "internal"`, `CLIENT_KIND_UNASSIGNED = "unassigned"` y `CLIENT_KIND_CUSTOMER = "customer"` en `app/models/clients.py`.

- [ ] **Step 1: Escribir la prueba que falla**

```python
# backend/tests/test_clients_schema.py
"""Every organization is born with its two system clients and the internal
client's "Principal" site (spec §1.1, §1.2), and the internal pair carries the
organization's own id (plan Global Constraints: id convention)."""

from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Client, Organization, Site


async def _new_org(admin_engine) -> Organization:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="Acme", slug=f"acme-{uuid4().hex[:8]}")
        s.add(org)
        await s.commit()
        return org


async def test_an_org_is_born_with_internal_and_unassigned_clients(admin_engine) -> None:
    org = await _new_org(admin_engine)
    async with AsyncSession(admin_engine) as s:
        rows = (
            await s.execute(select(Client.kind, Client.id, Client.name).where(Client.org_id == org.id))
        ).all()
    kinds = {k: (i, n) for k, i, n in rows}
    assert set(kinds) == {"internal", "unassigned"}
    assert kinds["internal"][0] == org.id
    assert kinds["internal"][1] == "Acme"
    assert kinds["unassigned"][0] != org.id


async def test_the_internal_client_has_a_principal_site_with_the_org_id(admin_engine) -> None:
    org = await _new_org(admin_engine)
    async with AsyncSession(admin_engine) as s:
        sites = (await s.execute(select(Site).where(Site.org_id == org.id))).scalars().all()
    assert [(x.id, x.client_id, x.name) for x in sites] == [(org.id, org.id, "Principal")]


async def test_only_one_internal_and_one_unassigned_client_per_org(admin_engine) -> None:
    org = await _new_org(admin_engine)
    async with AsyncSession(admin_engine) as s:
        for kind in ("internal", "unassigned"):
            try:
                async with s.begin():
                    await s.execute(
                        text(
                            "INSERT INTO clients (id, org_id, name, kind) "
                            "VALUES (gen_random_uuid(), :o, :n, :k)"
                        ),
                        {"o": org.id, "n": f"dup-{kind}", "k": kind},
                    )
            except Exception as exc:  # noqa: BLE001 -- the assertion is that it raised
                assert "uq_clients_one_" in str(exc)
            else:
                raise AssertionError(f"a second {kind} client was accepted")


async def test_a_contact_must_name_a_client_and_staff_must_not(admin_engine) -> None:
    org = await _new_org(admin_engine)
    async with AsyncSession(admin_engine) as s:
        for kind, client in (("contact", None), ("staff", org.id)):
            try:
                async with s.begin():
                    await s.execute(
                        text(
                            "INSERT INTO users (id, org_id, external_subject, email, kind, client_id) "
                            "VALUES (gen_random_uuid(), :o, :sub, 'x@test.local', :k, :c)"
                        ),
                        {"o": org.id, "sub": uuid4().hex, "k": kind, "c": client},
                    )
            except Exception as exc:  # noqa: BLE001
                assert "ck_users_kind_client" in str(exc)
            else:
                raise AssertionError(f"{kind} with client_id={client} was accepted")
```

- [ ] **Step 2: Ejecutar y ver que falla**

Run: `.venv/bin/pytest -q tests/test_clients_schema.py`
Expected: FAIL en la recolección con `ImportError: cannot import name 'Client' from 'app.models'`.

- [ ] **Step 3: Escribir los modelos**

```python
# backend/app/models/clients.py
"""Clients and their sites (`docs/superpowers/specs/2026-10-23-msp-clientes-design.md` §1).

A client is a company Novieri serves; a site is one of its offices, i.e. one
LAN. Every organization is born with two system clients -- `internal` (Novieri's
own IT) and `unassigned` (tickets from unknown senders) -- created by the
`its_org_system_clients` trigger of migration 0230, not by application code, so
no path that creates an organization can forget them.

THE INTERNAL PAIR CARRIES THE ORGANIZATION'S ID: the `internal` client and its
"Principal" site both have `id = organizations.id`. Ids are UUIDs and unique per
table, so the coincidence is harmless, and it gives every caller (setup, tests,
the codemods) the internal client without a lookup.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ARRAY, CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import CIDR
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, uuid7_pk

CLIENT_KIND_CUSTOMER = "customer"
CLIENT_KIND_INTERNAL = "internal"
CLIENT_KIND_UNASSIGNED = "unassigned"
CLIENT_KINDS = (CLIENT_KIND_CUSTOMER, CLIENT_KIND_INTERNAL, CLIENT_KIND_UNASSIGNED)

OPTIONAL_MODULES = ("budget", "bcp", "change")


class Client(TimestampMixin, Base):
    __tablename__ = "clients"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('customer', 'internal', 'unassigned')", name="ck_clients_kind"
        ),
        CheckConstraint("status IN ('active', 'archived')", name="ck_clients_status"),
        Index("uq_clients_org_name", "org_id", text("lower(name)"), unique=True),
        Index(
            "uq_clients_one_internal",
            "org_id",
            unique=True,
            postgresql_where=text("kind = 'internal'"),
        ),
        Index(
            "uq_clients_one_unassigned",
            "org_id",
            unique=True,
            postgresql_where=text("kind = 'unassigned'"),
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20), default=CLIENT_KIND_CUSTOMER)
    status: Mapped[str] = mapped_column(String(20), default="active")
    email_domains: Mapped[list[str]] = mapped_column(
        ARRAY(String(253)), default=list, server_default=text("'{}'")
    )
    phone: Mapped[str] = mapped_column(String(40), default="", server_default="")
    address: Mapped[str] = mapped_column(Text, default="", server_default="")
    website: Mapped[str] = mapped_column(String(300), default="", server_default="")
    notes: Mapped[str] = mapped_column(Text, default="", server_default="")
    enabled_modules: Mapped[list[str]] = mapped_column(
        ARRAY(String(20)), default=list, server_default=text("'{}'")
    )


class Site(TimestampMixin, Base):
    __tablename__ = "sites"
    __table_args__ = (Index("uq_sites_client_name", "client_id", text("lower(name)"), unique=True),)

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    client_id: Mapped[UUID] = mapped_column(ForeignKey("clients.id"))
    name: Mapped[str] = mapped_column(String(200))
    address: Mapped[str] = mapped_column(Text, default="", server_default="")
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    egress_networks: Mapped[list[Any]] = mapped_column(
        ARRAY(CIDR()), default=list, server_default=text("'{}'::cidr[]")
    )
    doc_record_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("doc_records.id"), default=None
    )
    archived_at: Mapped[datetime | None] = mapped_column(default=None)
```

Add `from app.models.clients import Client, Site` and both names to `__all__` in `backend/app/models/__init__.py`, following the existing import block's order.

In `backend/app/models/core.py`, class `User` (after `locale`):

```python
    # Spec §1.3. `staff` is a Novieri technician (console); `contact` is a
    # person of one client (portal only, refused by `auth_context`). The CHECK
    # `ck_users_kind_client` (migration 0230) ties the two columns together.
    kind: Mapped[str] = mapped_column(String(10), default="staff", server_default="staff")
    client_id: Mapped[UUID | None] = mapped_column(ForeignKey("clients.id"), default=None)
```

In class `UserRole`, replace the `UniqueConstraint("user_id", "role_id", "device_group_id")`, the index `postgresql_where=text("device_group_id IS NULL")` and the `device_group_id` column with:

```python
    __table_args__ = (
        UniqueConstraint("user_id", "role_id", "client_id"),
        Index(
            "uq_user_roles_user_role_org_wide",
            "user_id",
            "role_id",
            unique=True,
            postgresql_where=text("client_id IS NULL"),
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    role_id: Mapped[UUID] = mapped_column(ForeignKey("roles.id"))
    # Spec §2.1: NULL is "every client, including ones created later".
    client_id: Mapped[UUID | None] = mapped_column(ForeignKey("clients.id"), default=None)
```

Update the first paragraph of the `UserRole` docstring to read "One grant of one role to one user, for every client or for one client (spec §2.1)." Leave the rest of the docstring: its NULL-uniqueness argument holds unchanged for `client_id`.

- [ ] **Step 4: Escribir la migración 0230**

```python
# backend/migrations/versions/0230_clients_and_sites.py
"""Clients and sites (MSP clients spec §1, §2.1).

`clients` and `sites`, both tenant-scoped like everything else. Every
organization -- existing ones here, new ones through the
`its_org_system_clients` trigger -- gets an `internal` client and an
`unassigned` one, and the internal client gets a "Principal" site. The internal
client and its site take the organization's own id (plan Global Constraints).

`users` gains `kind` and `client_id` (spec §1.3). `user_roles.device_group_id`
becomes `client_id` (spec §2.1); a group-scoped grant becomes a grant on the
group's client, which 0231 fills in. Here the group column is dropped only
after the client column exists, so no grant is lost on a populated database.

Revision ID: 0230
Revises: 0220
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0230"
down_revision: str | None = "0220"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORG_CTX = "current_setting('app.current_org', true)::uuid"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE clients (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            name varchar(200) NOT NULL,
            kind varchar(20) NOT NULL DEFAULT 'customer',
            status varchar(20) NOT NULL DEFAULT 'active',
            email_domains varchar(253)[] NOT NULL DEFAULT '{}',
            phone varchar(40) NOT NULL DEFAULT '',
            address text NOT NULL DEFAULT '',
            website varchar(300) NOT NULL DEFAULT '',
            notes text NOT NULL DEFAULT '',
            enabled_modules varchar(20)[] NOT NULL DEFAULT '{}',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_clients_kind CHECK (kind IN ('customer', 'internal', 'unassigned')),
            CONSTRAINT ck_clients_status CHECK (status IN ('active', 'archived'))
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX uq_clients_org_name ON clients (org_id, lower(name))")
    op.execute(
        "CREATE UNIQUE INDEX uq_clients_one_internal ON clients (org_id) WHERE kind = 'internal'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_clients_one_unassigned ON clients (org_id) "
        "WHERE kind = 'unassigned'"
    )
    op.execute(
        """
        CREATE TABLE sites (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            client_id uuid NOT NULL REFERENCES clients(id),
            name varchar(200) NOT NULL,
            address text NOT NULL DEFAULT '',
            description text NOT NULL DEFAULT '',
            egress_networks cidr[] NOT NULL DEFAULT '{}'::cidr[],
            doc_record_id uuid REFERENCES doc_records(id),
            archived_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX uq_sites_client_name ON sites (client_id, lower(name))")
    for table in ("clients", "sites"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (org_id = {ORG_CTX}) WITH CHECK (org_id = {ORG_CTX})"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO app_user")
    op.execute("GRANT SELECT ON clients, sites TO portal_user")

    # A site belongs to its client's org and the unassigned client has none.
    op.execute(
        """
        CREATE FUNCTION its_site_client_check() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE c_org uuid; c_kind text;
        BEGIN
            SELECT org_id, kind INTO c_org, c_kind FROM clients WHERE id = NEW.client_id;
            IF c_org IS DISTINCT FROM NEW.org_id THEN
                RAISE EXCEPTION 'site % names a client of another organization', NEW.id
                    USING ERRCODE = 'check_violation';
            END IF;
            IF c_kind = 'unassigned' THEN
                RAISE EXCEPTION 'the unassigned client cannot have sites'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER its_site_client_check BEFORE INSERT OR UPDATE OF client_id, org_id "
        "ON sites FOR EACH ROW EXECUTE FUNCTION its_site_client_check()"
    )

    # Every organization's system clients. SECURITY DEFINER because the
    # inserting role (admin in setup and tests) is the only one that creates
    # organizations, and the rows must exist whatever RLS context it holds.
    op.execute(
        """
        CREATE FUNCTION its_org_system_clients() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
        BEGIN
            INSERT INTO clients (id, org_id, name, kind)
                VALUES (NEW.id, NEW.id, NEW.name, 'internal');
            INSERT INTO clients (id, org_id, name, kind)
                VALUES (gen_random_uuid(), NEW.id, 'Sin asignar', 'unassigned');
            INSERT INTO sites (id, org_id, client_id, name)
                VALUES (NEW.id, NEW.id, NEW.id, 'Principal');
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER its_org_system_clients AFTER INSERT ON organizations "
        "FOR EACH ROW EXECUTE FUNCTION its_org_system_clients()"
    )
    op.execute(
        """
        INSERT INTO clients (id, org_id, name, kind)
            SELECT id, id, name, 'internal' FROM organizations;
        INSERT INTO clients (id, org_id, name, kind)
            SELECT gen_random_uuid(), id, 'Sin asignar', 'unassigned' FROM organizations;
        INSERT INTO sites (id, org_id, client_id, name)
            SELECT id, id, id, 'Principal' FROM organizations;
        """
    )

    # users: spec §1.3.
    op.execute("ALTER TABLE users ADD COLUMN kind varchar(10) NOT NULL DEFAULT 'staff'")
    op.execute("ALTER TABLE users ADD COLUMN client_id uuid REFERENCES clients(id)")
    op.execute(
        "ALTER TABLE users ADD CONSTRAINT ck_users_kind_client CHECK ("
        "(kind = 'staff' AND client_id IS NULL) OR (kind = 'contact' AND client_id IS NOT NULL))"
    )

    # user_roles: spec §2.1. Group-scoped grants keep their scope on the
    # group's client once 0231 gives groups one; until then they are mapped
    # to the internal client, which is where 0231 puts every existing group.
    op.execute("ALTER TABLE user_roles ADD COLUMN client_id uuid REFERENCES clients(id)")
    op.execute(
        "UPDATE user_roles SET client_id = org_id WHERE device_group_id IS NOT NULL"
    )
    op.execute("DROP INDEX IF EXISTS uq_user_roles_user_role_org_wide")
    op.execute(
        "ALTER TABLE user_roles DROP CONSTRAINT IF EXISTS "
        "user_roles_user_id_role_id_device_group_id_key"
    )
    op.execute("ALTER TABLE user_roles DROP COLUMN device_group_id")
    op.execute(
        "ALTER TABLE user_roles ADD CONSTRAINT user_roles_user_id_role_id_client_id_key "
        "UNIQUE (user_id, role_id, client_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_user_roles_user_role_org_wide ON user_roles (user_id, role_id) "
        "WHERE client_id IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_user_roles_user_role_org_wide")
    op.execute(
        "ALTER TABLE user_roles DROP CONSTRAINT IF EXISTS user_roles_user_id_role_id_client_id_key"
    )
    op.execute("ALTER TABLE user_roles ADD COLUMN device_group_id uuid REFERENCES device_groups(id)")
    op.execute("ALTER TABLE user_roles DROP COLUMN client_id")
    op.execute(
        "ALTER TABLE user_roles ADD CONSTRAINT user_roles_user_id_role_id_device_group_id_key "
        "UNIQUE (user_id, role_id, device_group_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_user_roles_user_role_org_wide ON user_roles (user_id, role_id) "
        "WHERE device_group_id IS NULL"
    )
    op.execute("ALTER TABLE users DROP CONSTRAINT ck_users_kind_client")
    op.execute("ALTER TABLE users DROP COLUMN client_id")
    op.execute("ALTER TABLE users DROP COLUMN kind")
    op.execute("DROP TRIGGER its_org_system_clients ON organizations")
    op.execute("DROP FUNCTION its_org_system_clients()")
    op.execute("DROP TABLE sites")
    op.execute("DROP FUNCTION its_site_client_check()")
    op.execute("DROP TABLE clients")
```

Before writing the `DROP CONSTRAINT` lines, confirm the real constraint name:

Run: `sudo -u postgres psql -At -c "SELECT conname FROM pg_constraint WHERE conrelid = 'user_roles'::regclass AND contype = 'u'" <db>`, against a database migrated to 0220.

The DSN is printed by `tests/conftest.py`'s `pg_cluster` fixture; alternatively run `.venv/bin/alembic upgrade 0220` against a scratch database. If the name differs, use the real one; `IF EXISTS` would otherwise hide the mistake.

- [ ] **Step 5: Ejecutar las pruebas del paso 1 y las de RLS**

Run: `.venv/bin/pytest -q tests/test_clients_schema.py tests/test_rls.py tests/test_tenant_column_sweep.py tests/test_rbac.py`
Expected: `test_clients_schema.py` PASS. `test_rbac.py` FAILS where it builds `UserRole(..., device_group_id=...)`; Task 4 rewrites those tests. Note the failing test ids in the commit message.

- [ ] **Step 6: Migración de ida y vuelta**

Run: `.venv/bin/pytest -q tests/test_migrations_roundtrip.py`. If that file does not exist, reproduce CI's `migrations` job: `grep -n "downgrade base" ../.github/workflows/ci.yml` shows the exact commands; run them against a scratch database.
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/migrations/versions/0230_clients_and_sites.py backend/app/models/clients.py backend/app/models/__init__.py backend/app/models/core.py backend/tests/test_clients_schema.py
git commit -m "feat(clients): clients and sites tables, system clients per org (spec §1)"
```

---

### Task 2: `ClientScope` y `org_session(..., clients=)`

**Files:**
- Create: `backend/app/core/client_scope.py`
- Modify: `backend/app/core/tenancy.py:20-75`
- Test: `backend/tests/test_client_scope.py`

**Interfaces:**
- Produces:
  - `ClientScope` (frozen dataclass) with `ClientScope.all() -> ClientScope` and `ClientScope.only(ids: Iterable[UUID]) -> ClientScope`.
  - `.narrow(client_id: UUID | None) -> ClientScope` and `.allows(client_id: UUID) -> bool`.
  - `.is_all: bool`, `.ids: frozenset[UUID]`.
  - `parse_client_header(value: str | None) -> UUID | None`, which raises `ValueError` on a malformed value.
  - `org_session(org_id, *, clients: ClientScope, engine=None)` and `portal_session(org_id, requester_id, *, clients: ClientScope, engine=None)`.

- [ ] **Step 1: Escribir la prueba que falla**

```python
# backend/tests/test_client_scope.py
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.client_scope import ClientScope, parse_client_header
from app.core.tenancy import org_session

A, B, C = uuid4(), uuid4(), uuid4()


def test_all_narrows_to_one_client() -> None:
    assert ClientScope.all().narrow(A) == ClientScope.only([A])


def test_narrowing_never_widens() -> None:
    assert ClientScope.only([A, B]).narrow(C) == ClientScope.only([])
    assert ClientScope.only([A, B]).narrow(A) == ClientScope.only([A])
    assert ClientScope.only([A]).narrow(None) == ClientScope.only([A])


def test_allows() -> None:
    assert ClientScope.all().allows(C)
    assert ClientScope.only([A]).allows(A)
    assert not ClientScope.only([A]).allows(B)


@pytest.mark.parametrize("raw", ["", "not-a-uuid", "1,2"])
def test_a_malformed_header_is_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_client_header(raw)


def test_an_absent_header_is_none() -> None:
    assert parse_client_header(None) is None


async def test_org_session_pins_both_gucs(app_engine) -> None:
    org = uuid4()
    async with org_session(org, clients=ClientScope.only([A, B]), engine=app_engine) as s:
        scope = (await s.execute(text("SELECT current_setting('app.client_scope', true)"))).scalar()
        ids = (await s.execute(text("SELECT current_setting('app.current_clients', true)"))).scalar()
    assert scope == "list"
    assert set(ids.split(",")) == {str(A), str(B)}


async def test_org_session_all(app_engine) -> None:
    async with org_session(uuid4(), clients=ClientScope.all(), engine=app_engine) as s:
        scope = (await s.execute(text("SELECT current_setting('app.client_scope', true)"))).scalar()
    assert scope == "all"


async def test_an_empty_list_is_a_list_of_nothing_not_all(app_engine) -> None:
    async with org_session(uuid4(), clients=ClientScope.only([]), engine=app_engine) as s:
        scope = (await s.execute(text("SELECT current_setting('app.client_scope', true)"))).scalar()
        ids = (await s.execute(text("SELECT current_setting('app.current_clients', true)"))).scalar()
    assert (scope, ids) == ("list", "")
```

- [ ] **Step 2: Ejecutar y ver que falla**

Run: `.venv/bin/pytest -q tests/test_client_scope.py`
Expected: FAIL con `ModuleNotFoundError: No module named 'app.core.client_scope'`.

- [ ] **Step 3: Implementar**

```python
# backend/app/core/client_scope.py
"""Which clients a database session may see (MSP clients spec §2.2, §2.3).

A `ClientScope` is "every client" or "exactly these ids". It travels from the
request (`AuthContext.clients`) to `org_session`, which pins it in two GUCs
that the restrictive `client_isolation` policies of migration 0231 read.

NARROWING NEVER WIDENS. `narrow()` intersects; asking a list scope for a client
outside it yields the EMPTY list, which RLS answers with zero rows -- never the
original scope (spec §2.2: the header is never ignored).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class ClientScope:
    is_all: bool
    ids: frozenset[UUID]

    @classmethod
    def all(cls) -> "ClientScope":
        return cls(is_all=True, ids=frozenset())

    @classmethod
    def only(cls, ids: Iterable[UUID]) -> "ClientScope":
        return cls(is_all=False, ids=frozenset(ids))

    def narrow(self, client_id: UUID | None) -> "ClientScope":
        if client_id is None:
            return self
        if self.is_all or client_id in self.ids:
            return ClientScope.only([client_id])
        return ClientScope.only([])

    def allows(self, client_id: UUID) -> bool:
        return self.is_all or client_id in self.ids

    def guc_values(self) -> tuple[str, str]:
        """(`app.client_scope`, `app.current_clients`) for `set_config`."""
        if self.is_all:
            return "all", ""
        return "list", ",".join(sorted(str(i) for i in self.ids))


def parse_client_header(value: str | None) -> UUID | None:
    """`X-Client-Id`: absent is None; anything but one UUID raises ValueError."""
    if value is None:
        return None
    return UUID(value.strip())  # raises ValueError on "", garbage, or a list


# Files under app/ allowed to call `ClientScope.all()` (spec §2.3). Every one is
# a process acting for the whole organization, or identity resolution that
# must read the user before their scope exists. `tests/test_client_catalog.py`
# fails on any other use. A new entry is a security decision: it says why.
ALL_SCOPE_ALLOWLIST: dict[str, str] = {
    "app/api/deps.py": "resolves the user and their grants before the scope exists (spec §2.2)",
    "app/modules/portal/principal.py": "resolves the portal user before the scope exists",
    "app/core/client_scope.py": "defines it",
}
```

In `backend/app/core/tenancy.py`, add `from app.core.client_scope import ClientScope` to the imports. Replace `org_session` and `portal_session` with:

```python
@asynccontextmanager
async def org_session(
    org_id: UUID, *, clients: ClientScope, engine: AsyncEngine | None = None
) -> AsyncIterator[AsyncSession]:
    engine = engine or db.get_engine()
    scope, ids = clients.guc_values()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        async with session.begin():
            await session.execute(
                text(
                    "SELECT set_config('app.current_org', :org, true), "
                    "set_config('app.client_scope', :scope, true), "
                    "set_config('app.current_clients', :ids, true)"
                ),
                {"org": str(org_id), "scope": scope, "ids": ids},
            )
            yield session
```

```python
@asynccontextmanager
async def portal_session(
    org_id: UUID,
    requester_id: UUID,
    *,
    clients: ClientScope,
    engine: AsyncEngine | None = None,
) -> AsyncIterator[AsyncSession]:
    # (keep the existing docstring, adding one paragraph:)
    # THE CLIENT GUCs ARE THE THIRD PIN (MSP clients spec §2.3): a contact is
    # fixed to their own client, so a ticket that somehow carried their
    # requester id under another client still does not show.
    engine = engine or db.get_portal_engine()
    scope, ids = clients.guc_values()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        async with session.begin():
            await session.execute(
                text(
                    "SELECT set_config('app.current_org', :org, true), "
                    "set_config('app.current_requester', :req, true), "
                    "set_config('app.client_scope', :scope, true), "
                    "set_config('app.current_clients', :ids, true)"
                ),
                {"org": str(org_id), "req": str(requester_id), "scope": scope, "ids": ids},
            )
            yield session
```

Update the module docstring's first paragraph to say `org_session()` pins the tenant **and the client scope**.

- [ ] **Step 4: Ejecutar**

Run: `.venv/bin/pytest -q tests/test_client_scope.py`
Expected: PASS. (`mypy app` now reports every `org_session` call without `clients=`. That is expected; Task 3 clears them.)

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/client_scope.py backend/app/core/tenancy.py backend/tests/test_client_scope.py
git commit -m "feat(clients): ClientScope and org_session(clients=) (spec §2.2-2.3)"
```

---

### Task 3: Codemod de las llamadas a `org_session` y `portal_session`

**Files:**
- Create: `backend/scripts/codemods/add_client_scope.py`
- Modify: every file under `backend/app/` and `backend/tests/` that calls `org_session(` or `portal_session(` (the codemod's output)
- Test: `backend/tests/test_client_catalog.py` (allowlist part)

**Interfaces:**
- Consumes: `ClientScope`, `ALL_SCOPE_ALLOWLIST` (Task 2).
- Produces:
  - Every call carries `clients=`.
  - Inside a function that has an `AuthContext`-typed parameter, or a local named `ctx`, the value is `<that name>.clients`. In `app/` route modules with no such context, it is `ClientScope.all()`, and the module is listed in a `REVIEW_REQUIRED.txt` for Step 4. In tests it is always `ClientScope.all()`.

- [ ] **Step 0: Dar a los contextos un campo `clients`, provisionalmente `ClientScope.all()`**

The codemod writes `ctx.clients`, so the field must exist first. In `backend/app/api/deps.py`, add `clients: ClientScope` as the last field of `AuthContext`. In `auth_context`, return `AuthContext(org_id=org_id, user_id=user_id, claims=claims, clients=ClientScope.all())`, with the comment `# Task 4 replaces this with the person's scope.`

Do the same for `PortalContext` in `backend/app/modules/portal/principal.py`. Fix the 3 manual constructions in `tests/test_bound_auth_context.py` by adding `clients=ClientScope.all()`.

This intermediate state restricts nothing, which is exactly today's behavior: no client policy exists until Task 6, and Task 4 replaces both values.

- [ ] **Step 1: Escribir el codemod**

```python
# backend/scripts/codemods/add_client_scope.py
"""Add `clients=` to every org_session(...) / portal_session(...) call.

Usage: python -I scripts/codemods/add_client_scope.py <path>... > REVIEW_REQUIRED.txt

- A call that already passes `clients=` is left alone (idempotent).
- If the enclosing function has a parameter annotated `AuthContext` (or a
  parameter/local named `ctx`), the value is `<name>.clients`.
- Otherwise `ClientScope.all()`, and for files under app/ the path is printed
  so a person reviews it (plan Task 3 Step 4).
- `from app.core.client_scope import ClientScope` is added where needed.
"""

import sys
from pathlib import Path

import libcst as cst
from libcst.codemod import CodemodContext
from libcst.codemod.visitors import AddImportsVisitor

TARGETS = {"org_session", "portal_session"}


class _Transformer(cst.CSTTransformer):
    def __init__(self) -> None:
        self.ctx_stack: list[str | None] = []
        self.used_all = False
        self.changed = False

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        name: str | None = None
        for p in [*node.params.params, *node.params.kwonly_params]:
            ann = p.annotation
            if ann is not None and isinstance(ann.annotation, cst.Name) and (
                ann.annotation.value in {"AuthContext", "PortalContext"}
            ):
                name = p.name.value
                break
            if p.name.value == "ctx":
                name = "ctx"
        if name is None:
            # a local `ctx = await auth_context(...)` / `= Depends(...)` result
            for stmt in node.body.body if isinstance(node.body, cst.IndentedBlock) else []:
                if isinstance(stmt, cst.SimpleStatementLine):
                    for s in stmt.body:
                        if isinstance(s, cst.Assign) and any(
                            isinstance(t.target, cst.Name) and t.target.value == "ctx"
                            for t in s.targets
                        ):
                            name = "ctx"
        self.ctx_stack.append(name if name is not None else (self.ctx_stack[-1] if self.ctx_stack else None))

    def leave_FunctionDef(self, original_node, updated_node):  # type: ignore[no-untyped-def]
        self.ctx_stack.pop()
        return updated_node

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.Call:
        func = updated_node.func
        fname = func.value if isinstance(func, cst.Name) else (
            func.attr.value if isinstance(func, cst.Attribute) else None
        )
        if fname not in TARGETS:
            return updated_node
        if any(a.keyword is not None and a.keyword.value == "clients" for a in updated_node.args):
            return updated_node
        ctx_name = self.ctx_stack[-1] if self.ctx_stack else None
        if ctx_name is not None:
            value: cst.BaseExpression = cst.Attribute(cst.Name(ctx_name), cst.Name("clients"))
        else:
            value = cst.Call(cst.Attribute(cst.Name("ClientScope"), cst.Name("all")))
            self.used_all = True
        self.changed = True
        args = list(updated_node.args)
        if args:
            last = args[-1]
            args[-1] = last.with_changes(comma=cst.MaybeSentinel.DEFAULT)
        args.append(cst.Arg(keyword=cst.Name("clients"), value=value,
                            equal=cst.AssignEqual(cst.SimpleWhitespace(""), cst.SimpleWhitespace(""))))
        return updated_node.with_changes(args=args)


def rewrite(path: Path) -> bool:
    src = path.read_text()
    module = cst.parse_module(src)
    t = _Transformer()
    new = module.visit(t)
    if not t.changed:
        return False
    if t.used_all:
        context = CodemodContext()
        AddImportsVisitor.add_needed_import(context, "app.core.client_scope", "ClientScope")
        new = new.visit(AddImportsVisitor(context))
    path.write_text(new.code)
    return t.used_all


def main(paths: list[str]) -> None:
    for root in paths:
        for path in sorted(Path(root).rglob("*.py")):
            if "org_session(" not in path.read_text() and "portal_session(" not in path.read_text():
                continue
            if rewrite(path) and "/app/" in f"/{path}":
                print(path)


if __name__ == "__main__":
    main(sys.argv[1:])
```

- [ ] **Step 2: Ejecutarlo**

Run (desde `backend/`): `.venv/bin/python -I scripts/codemods/add_client_scope.py app tests > /tmp/review_required.txt; wc -l < /tmp/review_required.txt; .venv/bin/ruff check --fix app tests -q; .venv/bin/ruff format app tests -q`
Expected: the list of `app/` files that received `ClientScope.all()`.

Check that `ruff format` is the repo's formatter before running it: `grep -n "ruff format" ../.github/workflows/ci.yml`. If CI does not run it, skip `ruff format` and fix any line-length findings with `ruff check` by hand.

- [ ] **Step 3: Verificar que no queda ninguna llamada sin alcance**

Run: `.venv/bin/mypy app 2>&1 | grep -c 'Missing named argument "clients"'`
Expected: `0`.

- [ ] **Step 4: Revisar cada archivo de `app/` que recibió `ClientScope.all()`**

For each path in `/tmp/review_required.txt`, open the call sites and classify each one:

- **(a) Worker, beat, gateway, replicator or another process with no person behind it.** Keep `ClientScope.all()` and add the file to `ALL_SCOPE_ALLOWLIST` with the reason.
- **(b) A route handler or a service called from one, where the person's `ctx` is not in scope at that frame.** Thread `clients: ClientScope` through as a parameter from the route that has `ctx`, and pass `ctx.clients`. Do not allowlist it.
- **(c) The two identity-resolution sites already in the allowlist** (`deps.py`, `portal/principal.py`). Keep them.

`app/api/v1/*` and `app/modules/*/api.py` may only end up in (a) for the routes named in spec §2.3, which arrive in Task 9. Anything else in those files is (b).

- [ ] **Step 5: Añadir la prueba de la lista revisada**

```python
# backend/tests/test_client_catalog.py  (first part; Task 5 adds the rest)
"""Spec §5 tests 1, 2 and 11: the client-scope catalog and its guards."""

import ast
from pathlib import Path

from app.core.client_scope import ALL_SCOPE_ALLOWLIST

BACKEND = Path(__file__).resolve().parent.parent


def _files_calling_all() -> set[str]:
    found: set[str] = set()
    for path in (BACKEND / "app").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "all"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "ClientScope"
            ):
                found.add(str(path.relative_to(BACKEND)))
    return found


def test_client_scope_all_only_in_reviewed_files() -> None:
    unreviewed = _files_calling_all() - set(ALL_SCOPE_ALLOWLIST)
    assert not unreviewed, (
        "ClientScope.all() outside the reviewed list (spec §2.3). Pass the "
        f"person's scope, or add the file with a reason: {sorted(unreviewed)}"
    )


def test_the_allowlist_has_not_rotted() -> None:
    stale = set(ALL_SCOPE_ALLOWLIST) - _files_calling_all() - {"app/core/client_scope.py"}
    assert not stale, f"allowlisted files that no longer call ClientScope.all(): {sorted(stale)}"


def test_the_allowlist_guard_is_not_vacuous() -> None:
    assert "app/api/deps.py" in _files_calling_all()
```

- [ ] **Step 6: Ejecutar toda la suite**

Run: `.venv/bin/pytest -q -x -p no:cacheprovider 2>&1 | tail -15` (about 25 minutes).
Expected: every failure is one of two kinds, and nothing else:
- the `test_rbac.py` / `device_group_id` failures already noted in Task 1;
- `test_client_catalog.py::test_the_allowlist_guard_is_not_vacuous`, which goes green in Task 4.

Any other failure is a codemod defect. Fix it before committing.

- [ ] **Step 7: Commit**

```bash
git add backend/scripts/codemods/add_client_scope.py backend/app backend/tests
git commit -m "refactor(clients): every org_session call states its client scope (spec §2.3)"
```

---

### Task 4: RBAC por cliente y `AuthContext.clients`

**Files:**
- Modify: `backend/app/core/rbac.py:413-468`
- Modify: `backend/app/api/deps.py` (`AuthContext`, `auth_context`, `require_permission`)
- Modify: `backend/app/modules/portal/principal.py` (`PortalContext.clients`)
- Modify: `backend/app/modules/user_admin/service.py`, `backend/app/modules/user_admin/api.py` (`device_group_id` → `client_id` en las asignaciones)
- Modify: the tests that built `UserRole(..., device_group_id=...)`: `tests/test_rbac.py`, `tests/test_user_admin_api.py`, `tests/test_device_group_write.py`, `tests/reporting_fixtures.py`, `tests/test_health_api.py`, `tests/test_setup_api.py`, `tests/test_agent_update_api.py`
- Test: `backend/tests/test_client_rbac.py`

**Interfaces:**
- Consumes: `ClientScope`, `parse_client_header`.
- Produces:
  - `user_permission_codes(session, user_id, *, client_id: UUID | None = None) -> set[str]`: counts grants with `client_id IS NULL` plus, when `client_id` is given, grants on that client.
  - `has_permission(session, user_id, code, *, client_id: UUID | None = None) -> bool`.
  - `clients_holding(session, user_id, code: str | None) -> ClientScope`: the scope where the user holds `code`, or holds any role when `code is None`.
  - `AuthContext(org_id, user_id, claims, clients: ClientScope)`.

**Por qué el alcance de una petición sale del permiso de su ruta.** Hoy
`require_permission` solo cuenta los permisos dados a toda la organización
(`deps.py:376`, `has_permission` sin `device_group_id`). Los permisos limitados
a un grupo no se aplican en ninguna ruta.

Si el alcance de la sesión es "los clientes donde el técnico tiene el permiso
de esta ruta", la regla del spec §2.1 queda impuesta por la RLS, sin tener que
comprobar el permiso registro por registro. Por ejemplo, si Ana es Técnico en X
y Solo lectura en Z, un `POST` que exige `script.run` abre una sesión con
alcance `[X]`. El equipo de Z no existe para esa petición y la respuesta es 404.

- [ ] **Step 1: Escribir la prueba que falla**

```python
# backend/tests/test_client_rbac.py
"""Spec §2.1-2.2: per-client grants and the request scope they produce."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.rbac import clients_holding, has_permission
from app.core.tenancy import org_session
from app.models import Client, Role, User, UserRole


async def _seed(admin_engine, env, grants: list[tuple[str, str | None]]) -> tuple[UUID, UUID, dict[str, UUID]]:
    """An org with customers X and Z; `grants` is [(role name, 'X'|'Z'|None)]."""
    org_id = await env.create_org("rbac")
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        clients = {}
        for name in ("X", "Z"):
            c = Client(org_id=org_id, name=name, kind="customer")
            s.add(c)
            await s.flush()
            clients[name] = c.id
        user = User(org_id=org_id, external_subject=f"sub-{org_id}", email="ana@test.local")
        s.add(user)
        await s.flush()
        for role_name, client in grants:
            role = (await s.execute(select(Role).where(Role.name == role_name, Role.org_id.is_(None)))).scalar_one()
            s.add(UserRole(org_id=org_id, user_id=user.id, role_id=role.id,
                           client_id=clients[client] if client else None))
        await s.commit()
    return org_id, user.id, clients


async def test_a_client_grant_counts_only_on_that_client(api_env) -> None:
    env = api_env
    org, user, c = await _seed(env.admin_engine, env, [("technician", "X")])
    async with org_session(org, clients=ClientScope.all(), engine=env.app_engine) as s:
        assert await has_permission(s, user, "device.read", client_id=c["X"])
        assert not await has_permission(s, user, "device.read", client_id=c["Z"])
        assert not await has_permission(s, user, "device.read")  # no all-clients grant


async def test_clients_holding_follows_the_permission(api_env) -> None:
    env = api_env
    org, user, c = await _seed(env.admin_engine, env, [("technician", "X"), ("viewer", "Z")])
    async with org_session(org, clients=ClientScope.all(), engine=env.app_engine) as s:
        assert await clients_holding(s, user, "device.read") == ClientScope.only([c["X"], c["Z"]])
        assert await clients_holding(s, user, "script.run") == ClientScope.only([c["X"]])
        assert await clients_holding(s, user, None) == ClientScope.only([c["X"], c["Z"]])


async def test_an_all_clients_grant_is_all(api_env) -> None:
    env = api_env
    org, user, _ = await _seed(env.admin_engine, env, [("technician", None)])
    async with org_session(org, clients=ClientScope.all(), engine=env.app_engine) as s:
        assert await clients_holding(s, user, "device.read") == ClientScope.all()
```

Before running, open `backend/app/core/rbac.py` and confirm three things:
- Built-in roles named `technician` and `viewer` exist. Run `grep -n '"technician"\|"viewer"\|"org-admin"' app/core/rbac.py migrations/versions/*.py | head`.
- `device.read` is held by both roles.
- `script.run` is held by `technician` only.

If the names differ, use the real role and permission names with the same shape: one role holding both codes, one holding only the read. Do not change what the test proves.

- [ ] **Step 2: Ejecutar y ver que falla**

Run: `.venv/bin/pytest -q tests/test_client_rbac.py`
Expected: FAIL con `ImportError: cannot import name 'clients_holding'`.

- [ ] **Step 3: Implementar en `rbac.py`**

Replace `user_permission_codes` and `has_permission` (`app/core/rbac.py:413-468`) with:

```python
async def _grants(session: AsyncSession, user_id: UUID) -> list[tuple[str, UUID | None]]:
    return [
        (code, client_id)
        for code, client_id in (
            await session.execute(
                select(Permission.code, UserRole.client_id)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .join(UserRole, UserRole.role_id == RolePermission.role_id)
                .where(UserRole.user_id == user_id)
            )
        ).all()
    ]


async def user_permission_codes(
    session: AsyncSession, user_id: UUID, *, client_id: UUID | None = None
) -> set[str]:
    """Codes the user holds: all-clients grants always count; a client grant
    counts only when asking about that same client (spec §2.1)."""
    return {
        code
        for code, scoped in await _grants(session, user_id)
        if scoped is None or (client_id is not None and scoped == client_id)
    }


async def has_permission(
    session: AsyncSession,
    user_id: UUID,
    code: str,
    *,
    client_id: UUID | None = None,
) -> bool:
    if code not in PERMISSIONS:
        raise ValueError(  # i18n-exempt: programmer error, unhandled 500
            f"unknown permission code {code!r}"
        )
    return code in await user_permission_codes(session, user_id, client_id=client_id)


async def clients_holding(session: AsyncSession, user_id: UUID, code: str | None) -> ClientScope:
    """The client scope in which the user holds `code` -- or any role at all
    when `code` is None (an ungated route). An all-clients grant makes it
    `ClientScope.all()`. This IS the request's RLS scope (plan Task 4)."""
    if code is not None and code not in PERMISSIONS:
        raise ValueError(  # i18n-exempt: programmer error, unhandled 500
            f"unknown permission code {code!r}"
        )
    scoped: set[UUID] = set()
    if code is None:
        rows = (
            await session.execute(select(UserRole.client_id).where(UserRole.user_id == user_id))
        ).scalars()
        for client_id in rows:
            if client_id is None:
                return ClientScope.all()
            scoped.add(client_id)
        return ClientScope.only(scoped)
    for held, client_id in await _grants(session, user_id):
        if held != code:
            continue
        if client_id is None:
            return ClientScope.all()
        scoped.add(client_id)
    return ClientScope.only(scoped)
```

Add `from app.core.client_scope import ClientScope` to `rbac.py`'s imports.

Then find every remaining `device_group_id=` passed to `has_permission` or `user_permission_codes`:

Run: `grep -rn "has_permission(\|user_permission_codes(" app | grep device_group`
Expected: no output. If there is any, it is a caller that was never enforced. Replace it with `client_id=<the record's client>`.

- [ ] **Step 4: Implementar en `deps.py`**

```python
@dataclass(frozen=True)
class AuthContext:
    org_id: UUID
    user_id: UUID
    claims: TokenClaims
    # The request's client scope (spec §2.2), already narrowed by X-Client-Id.
    # `bound_auth_context`: every client the user holds any role in.
    # `require_permission(code)`: every client the user holds `code` in.
    clients: ClientScope
```

In `auth_context`:

1. Read the header before the tenant session. Malformed means 400:

```python
    try:
        requested_client = parse_client_header(request.headers.get("X-Client-Id"))
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, tr("X-Client-Id must be one client id")
        ) from exc
```

2. The existing `async with org_session(org_id, engine=...)` (the codemod made it `clients=ClientScope.all()`) stays `ClientScope.all()`, and its comment says why: spec §2.2 "Resolver quién es el técnico".

3. Inside it, right after the `user` row is resolved or created, refuse contacts:

```python
        if user.kind == "contact":
            log_admission_refusal("http", sub=claims.sub, email=claims.email, reason="contact")
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, tr("this account can only use the portal")
            )
        scope = (await clients_holding(session, user.id, None)).narrow(requested_client)
```

4. Return `AuthContext(org_id=org_id, user_id=user_id, claims=claims, clients=scope)`.

5. Store `request.state.requested_client = requested_client`, next to `org_id` and `user_id`, so `require_permission` can re-narrow.

In `require_permission`'s `dependency`, replace the permission check with:

```python
        async with org_session(ctx.org_id, clients=ClientScope.all(),
                               engine=request.app.state.app_engine) as session:
            holding = await clients_holding(session, ctx.user_id, code)
        if not holding.is_all and not holding.ids:
            raise HTTPException(status.HTTP_403_FORBIDDEN, tr("requires {code}", code=code))
        yield dataclasses.replace(
            ctx, clients=holding.narrow(getattr(request.state, "requested_client", None))
        )
```

Import `dataclasses`, `ClientScope`, `parse_client_header` and `clients_holding`.

**Holding the code in some client is enough to pass the route gate**, which is the same rule as today for an org-wide grant. The record-level rule is the scope itself.

- [ ] **Step 5: `PortalContext`**

In `backend/app/modules/portal/principal.py`:
- Add `clients: ClientScope` to `PortalContext`.
- When building it: a `contact` user gets `ClientScope.only([user.client_id])`. A staff user using the portal gets `await clients_holding(session, user.id, None)`.
- Every `portal_session(...)` call in the portal module passes `clients=pctx.clients` instead of the codemod's `ClientScope.all()`, and `portal/principal.py` keeps its allowlist entry only for the identity lookup.

- [ ] **Step 6: Asignaciones en `user_admin`**

In `app/modules/user_admin/service.py` and `api.py`, every `device_group_id` that names a grant's scope becomes `client_id`:
- the request and response schemas, as field `client_id: UUID | None`;
- the duplicate check;
- the audit metadata key: `"client_id"`, keeping the action names.

Validation:
- a `client_id` must name a client of the org whose `kind != 'unassigned'`; otherwise 422 with `tr("a role cannot be granted on the unassigned client")` (spec §2.2);
- an unknown id is 422 `tr("unknown client")`.

Run: `grep -n "device_group_id" app/modules/user_admin/*.py`
Expected: no matches, except where the module deals with device groups as groups. If it does, leave those lines and add a comment saying why.

Update the tests listed in **Files** the same way: `device_group_id=<group>` on a `UserRole` becomes `client_id=<the group's client>`. Where a test proved "group-scoped grants do not count org-wide", the same test now proves it for client grants. Keep the assertion and change the fixture.

- [ ] **Step 7: Ejecutar**

Run: `.venv/bin/pytest -q tests/test_client_rbac.py tests/test_rbac.py tests/test_user_admin_api.py tests/test_client_catalog.py tests/test_portal_api.py && .venv/bin/mypy app`
Expected: PASS and `Success: no issues found`.

- [ ] **Step 8: Prueba de contacto**

Append to `tests/test_client_rbac.py`:

```python
async def test_a_contact_is_refused_at_the_console(api_env) -> None:
    env = api_env
    org = await env.create_org("contact")
    sub = await env.create_user(org, "technician")
    async with AsyncSession(env.admin_engine) as s:
        user = (await s.execute(select(User).where(User.external_subject == sub))).scalar_one()
        user.kind, user.client_id = "contact", org  # the internal client (id convention)
        await s.commit()
    r = await env.client.get("/api/v1/me", headers=await env.headers(org, sub))
    assert r.status_code == 403
```

Run: `.venv/bin/pytest -q tests/test_client_rbac.py`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add -A backend/app/core/rbac.py backend/app/api/deps.py backend/app/modules/portal backend/app/modules/user_admin backend/tests
git commit -m "feat(clients): per-client grants; request scope = clients holding the route's code (spec §2.1-2.2)"
```

---

### Task 5: Catálogo de clasificación y sus pruebas

**Files:**
- Create: `backend/app/core/client_scope_catalog.py`
- Modify: `backend/tests/test_client_catalog.py`

**Interfaces:**
- Produces:
  - `CATALOG: dict[str, Entry]`, where `Entry` is a frozen dataclass with `kind: Literal["C", "H", "O", "G"]`, `parent: str | None` (the parent table) and `fk: str | None` (the FK column).
  - `PENDING_PHASE_2: frozenset[str]`.
  - Migration 0231 holds its own literal copy of the C/H/O rows. Migrations do not import app code. Test 2 below proves the two agree through `pg_policies` and `pg_trigger`.

- [ ] **Step 1: Escribir el catálogo**

```python
# backend/app/core/client_scope_catalog.py
"""Every org-scoped table's relation to clients (MSP clients spec, Annex A).

C  client_id NOT NULL, set by the application.
H  client_id NOT NULL, set by the `its_client_from_parent` trigger from
   `parent` through `fk`.
O  client_id nullable: NULL means "every client" (config). When `fk` is set
   and non-null on a row, the trigger derives the client from it.
G  no client_id.

`PENDING_PHASE_2` are C/H tables whose module code must start writing
client_id before the column can be NOT NULL (plan "Fuera de esta fase"). The
phase-2 plan's last task deletes that set; until then those tables are exempt
from the policy check, and from nothing else.
"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Entry:
    kind: Literal["C", "H", "O", "G"]
    parent: str | None = None
    fk: str | None = None


C, G = Entry("C"), Entry("G")


def H(parent: str, fk: str) -> Entry:
    return Entry("H", parent, fk)


def O(parent: str | None = None, fk: str | None = None) -> Entry:  # noqa: E743
    return Entry("O", parent, fk)


CATALOG: dict[str, Entry] = {
    # identity and platform
    "organizations": G, "roles": G, "permissions": G, "role_permissions": G,
    "user_roles": G, "users": O(), "audit_events": G, "keycloak_event_ingest": G,
    "agent_releases": G, "dashboard_layouts": G, "mcp_revocation": G,
    "platform_error_group": G, "platform_error_event": G, "platform_health_sample": G,
    "platform_probe_notice": G, "org_vault_keys": G, "domain_events": O(),
    # clients themselves
    "clients": G, "sites": C,
    # fleet
    "device_groups": C, "device_group_evaluation": G, "devices": C,
    "suggested_asset_match": H("devices", "device_id"),
    "device_tags": H("devices", "device_id"), "agents": H("devices", "device_id"),
    "policies": O("device_groups", "device_group_id"),
    "credentials": O("device_groups", "device_group_id"),
    "jobs": H("devices", "target_id"),  # special-cased: target_type device|group
    "job_logs": H("jobs", "job_id"),
    "enrollment_tokens": C, "install_links": C,
    # AI
    "ai_autonomy_ceiling": G, "ai_session": G, "ai_message": G, "ai_injection_flag": G,
    "ai_settings": G, "ai_action": H("devices", "target_device_id"),
    "approved_action": G, "approved_action_script_exemption": G,
    # documentation (O: NULL = Novieri's own knowledge base)
    "doc_articles": O(), "doc_records": O(), "doc_documents": O(),
    "doc_links": O("doc_articles", "article_id"),
    "doc_versions": O("doc_articles", "article_id"),
    "doc_document_versions": O("doc_documents", "document_id"),
    "doc_document_links": O("doc_documents", "document_id"),
    # inventory and security
    "inventory_snapshots": H("devices", "device_id"),
    "installed_software": H("devices", "device_id"),
    "inventory_changes": H("devices", "device_id"),
    "hardware_components": H("devices", "device_id"),
    "security_events": H("devices", "device_id"),
    # push install
    "push_authorization": C, "push_run": C,
    "push_credential_scope": H("credentials", "credential_id"),
    "push_credential_realm": H("credentials", "credential_id"),
    "push_host_key": O("devices", "device_id"),
    "push_batch_target": H("jobs", "job_id"),
    "push_run_target": H("push_run", "run_id"),
    # agent update
    "agent_update_rollouts": G,
    "agent_update_rings": O("device_groups", "device_group_id"),
    "agent_update_targets": H("devices", "device_id"),
    # backup
    "backup_repositories": C,
    "backup_chunks": H("backup_repositories", "repository_id"),
    "backup_chunk_copies": H("backup_repositories", "repository_id"),
    "backup_policies": O("device_groups", "device_group_id"),
    "backup_offsite_targets": O(), "backup_recovery_keys": O(),
    "backup_runs": H("devices", "device_id"), "backup_points": H("devices", "device_id"),
    "restore_jobs": H("devices", "device_id"),
    "backup_point_copies": H("backup_points", "point_id"),
    # optional modules
    "bcp_plan": C, "bcp_test": H("bcp_plan", "plan_id"),
    "bcp_test_step": H("bcp_test", "test_id"), "bcp_finding": H("bcp_test", "test_id"),
    "budget_settings": C, "cost_center": C, "expense_category": C, "budget": C,
    "expense": C, "budget_line": H("budget", "budget_id"), "fx_rate": G,
    "change_request": C,
    "change_approval": H("change_request", "change_id"),
    "change_link": H("change_request", "change_id"),
    "change_external_ref": H("change_request", "change_id"),
    "change_gates": O("device_groups", "device_group_id"),
    # software
    "packages": G, "scripts": G, "deployments": C,
    "deployment_results": H("devices", "device_id"),
    "script_runs": H("devices", "device_id"),
    "script_policies": H("device_groups", "device_group_id"),
    # email intake
    "email_mailbox": G, "email_oauth_app": G, "email_oauth_authorization": G,
    "inbound_message": O(),
    # health
    "health_profile": G, "health_check": G, "health_org_settings": G,
    "health_baseline_seed": G,
    "health_profile_assignment": O("devices", "device_id"),  # or device_group_id, see 0231
    "health_alert": H("devices", "device_id"),
    "device_health_check_state": H("devices", "device_id"),
    "health_flap_state": H("devices", "device_id"),
    "device_health_policy_ack": H("devices", "device_id"),
    "health_alert_action": H("health_alert", "alert_id"),
    # ipam (ipam_site is merged into sites in phase 2)
    "ipam_site": C, "ipam_vrf": C, "ipam_vlan": C, "ipam_subnet": C,
    "ipam_scan_authorization": H("ipam_vrf", "vrf_id"),
    "ipam_range": H("ipam_subnet", "subnet_id"),
    "ipam_address": H("ipam_vrf", "vrf_id"),
    "ipam_observation": H("ipam_vrf", "vrf_id"),
    "ipam_drift": H("ipam_vrf", "vrf_id"),
    "ipam_sweep_run": H("ipam_subnet", "subnet_id"),
    # licenses and subscriptions
    "vendor": G, "software_product": G, "license": C,
    "license_compliance_snapshot": C, "license_allocation": H("license", "license_id"),
    "software_ignore": O(),
    "subscription": C, "seat_assignment": H("subscription", "subscription_id"),
    "subscription_event": H("subscription", "subscription_id"),
    "subscription_alert_sent": H("subscription", "subscription_id"),
    # notification
    "notification_channels": G, "notification_rules": G, "queue_members": G,
    "notification_deliveries": G, "webhook_allowlist_entries": G,
    # patch
    "patches": G, "patch_policies": O("device_groups", "device_group_id"),
    "patch_approvals": O(), "device_patch_status": H("devices", "device_id"),
    "patch_reboots": H("devices", "device_id"), "patch_runs": H("devices", "device_id"),
    # people
    "person": C, "person_login": H("person", "person_id"),
    "person_checklist": H("person", "person_id"),
    "person_checklist_step": H("person_checklist", "checklist_id"),
    "device_person": H("devices", "device_id"),
    # provisioning (module hidden)
    "os_image": G, "provision_profile": G, "provision_run": O("devices", "device_id"),
    # remote
    "remote_sessions": H("devices", "device_id"),
    "remote_policies": O("device_groups", "device_group_id"),
    # service desk and SLA
    "ticket_queue": G, "ticket_category": G, "ticket_number_counter": G,
    "ticket_canned_response": G, "ticket": C,
    "ticket_comment": H("ticket", "ticket_id"), "ticket_attachment": H("ticket", "ticket_id"),
    "ticket_approval": H("ticket", "ticket_id"),
    "ticket_status_history": H("ticket", "ticket_id"),
    "ticket_sla": H("ticket", "ticket_id"),
    "sla_calendar": G, "sla_calendar_window": G, "sla_calendar_holiday": G,
    "sla_org_settings": G, "sla_policy": O(),
    # site servers
    "site_server": H("devices", "device_id"),
}

PENDING_PHASE_2: frozenset[str] = frozenset(
    {
        "ticket", "ticket_comment", "ticket_attachment", "ticket_approval",
        "ticket_status_history", "ticket_sla",
        "person", "person_login", "person_checklist", "person_checklist_step",
        "license", "license_allocation", "license_compliance_snapshot",
        "subscription", "seat_assignment", "subscription_event", "subscription_alert_sent",
        "deployments",
        "ipam_site", "ipam_vrf", "ipam_vlan", "ipam_subnet", "ipam_range", "ipam_address",
        "ipam_observation", "ipam_drift", "ipam_scan_authorization", "ipam_sweep_run",
        "push_authorization", "push_run", "push_run_target", "push_credential_scope",
        "push_credential_realm",
        "backup_repositories", "backup_chunks", "backup_chunk_copies",
        "bcp_plan", "bcp_test", "bcp_test_step", "bcp_finding",
        "budget_settings", "cost_center", "expense_category", "budget", "budget_line",
        "expense", "change_request", "change_approval", "change_link", "change_external_ref",
    }
)

PHASE_1 = {t: e for t, e in CATALOG.items() if e.kind != "G" and t not in PENDING_PHASE_2}
```

**Classification changes against the spec's Annex A.** Each has its reason from `scratchpad/fks.txt`; fix the annex in the same commit.
- `device_group_evaluation` is G: it is per-organization worker state and has no group FK.
- `ipam_address`, `ipam_observation` and `ipam_drift` are H from `ipam_vrf`: their `subnet_id` is nullable, but `vrf_id` is not.
- `ipam_scan_authorization` is H from `ipam_vrf`.
- `ai_action` is H from `devices` through `target_device_id`, which is NOT NULL.
- `push_host_key` is O: `device_id` is nullable.
- `push_batch_target` is H from `jobs`.
- `provision_run` is O: `device_id` is nullable.
- The doc family is O, with derivation from the parent where it exists.
- `clients` is G, with its own policy (Task 6).

- [ ] **Step 2: Pruebas del catálogo**

Append to `backend/tests/test_client_catalog.py`:

```python
from sqlalchemy import text

from app.core.client_scope_catalog import CATALOG, PENDING_PHASE_2, PHASE_1
from app.models.base import Base


def test_every_table_is_classified() -> None:
    live = {t.name for t in Base.metadata.sorted_tables}
    assert live - set(CATALOG) == set(), "unclassified tables (spec §5 test 1)"
    assert set(CATALOG) - live == set(), "catalog names tables that do not exist"


def test_pending_tables_are_in_the_catalog_and_not_global() -> None:
    assert PENDING_PHASE_2 <= set(CATALOG)
    assert all(CATALOG[t].kind != "G" for t in PENDING_PHASE_2)


def test_h_parents_are_scoped_tables() -> None:
    for table, entry in CATALOG.items():
        if entry.kind == "H":
            assert entry.parent in CATALOG and CATALOG[entry.parent].kind in {"C", "H"}, table


async def test_phase_1_tables_carry_the_restrictive_policy(admin_engine) -> None:
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT tablename, permissive, qual, with_check FROM pg_policies "
                    "WHERE policyname = 'client_isolation'"
                )
            )
        ).all()
    by_table = {r.tablename: r for r in rows}
    for table, entry in PHASE_1.items():
        row = by_table.get(table)
        assert row is not None, f"{table}: no client_isolation policy (spec §5 test 2)"
        assert row.permissive == "RESTRICTIVE", table
        assert "its_client_visible" in row.qual, table
        if entry.kind == "O":
            assert "IS NULL" in row.qual, f"{table}: O tables must read NULL rows"


async def test_h_tables_carry_the_inheritance_trigger(admin_engine) -> None:
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE t.tgname = 'its_client_from_parent'"
                )
            )
        ).scalars().all()
    expected = {
        t for t, e in PHASE_1.items() if e.parent is not None and t != "jobs"
    }
    assert expected <= set(rows), f"missing triggers: {sorted(expected - set(rows))}"
```

- [ ] **Step 3: Ejecutar**

Run: `.venv/bin/pytest -q tests/test_client_catalog.py`
Expected:
- `test_every_table_is_classified`, `test_pending_*` and `test_h_parents_*` PASS.
- The two DB tests FAIL. Task 6 makes them pass.

- [ ] **Step 4: Commit**

```bash
git add backend/app/core/client_scope_catalog.py backend/tests/test_client_catalog.py docs/superpowers/specs/2026-10-23-msp-clientes-design.md
git commit -m "feat(clients): client-scope catalog of every table (spec Annex A, §5 tests 1-2)"
```

---

### Task 6: Migración 0231 — `client_id`, herencia y políticas restrictivas

**Files:**
- Create: `backend/migrations/versions/0231_client_scope_rls.py`
- Modify: `backend/app/models/core.py` (`client_id` on `Device`, `DeviceGroup`, `EnrollmentToken`, `InstallLink`, `Agent`, `Job`, …; `site_id` on `Device`, `EnrollmentToken`, `InstallLink`)
- Modify: the model classes of every other PHASE_1 table (`client_id: Mapped[UUID]` for C/H, `Mapped[UUID | None]` for O), so the ORM knows the column. A missing one makes the ORM omit it from its reads, which is harmless, but mypy-visible code that reads `.client_id` needs it.
- Test: `backend/tests/test_client_catalog.py` (Task 5's DB tests go green)

**Interfaces:**
- Consumes: CATALOG (copied literally), `clients`/`sites` (Task 1).
- Produces:
  - SQL function `its_client_visible(uuid) RETURNS boolean`.
  - Triggers `its_client_from_parent` (H and O-with-parent), `its_job_client` (jobs), `its_device_client_guard` (devices) and `its_group_client_guard` (device_groups).
  - A `client_isolation` restrictive policy on every PHASE_1 table, and on `clients` itself.

- [ ] **Step 1: Escribir la migración**

```python
# backend/migrations/versions/0231_client_scope_rls.py
"""Client scope at the database (MSP clients spec §2.2-2.4, plan Task 6).

1. `its_client_visible(client)`: the scope predicate over the two GUCs that
   `org_session` pins. Unset GUCs make it NULL -- no row, no write.
2. `client_id` on every phase-1 table (C, H, O), backfilled for existing rows
   (C: the organization's internal client, whose id IS the org id; H: from
   the parent), then NOT NULL for C and H.
3. `its_client_from_parent`: H tables and O tables with a parent take the
   parent's client; a different explicit value raises. SECURITY INVOKER, so a
   parent the session cannot see yields NULL and NOT NULL refuses the write.
4. A RESTRICTIVE `client_isolation` policy on each, ANDed with the existing
   permissive `tenant_isolation`, which is not touched.

The table list is a LITERAL copy of `app/core/client_scope_catalog.py`'s
phase-1 rows: migrations never import application code, and
`tests/test_client_catalog.py` proves the two agree through `pg_policies`.

Revision ID: 0231
Revises: 0230
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0231"
down_revision: str | None = "0230"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

C_TABLES = ["sites", "device_groups", "devices", "enrollment_tokens", "install_links"]

H_TABLES: list[tuple[str, str, str]] = [  # (table, parent, fk)
    ("suggested_asset_match", "devices", "device_id"),
    ("device_tags", "devices", "device_id"),
    ("agents", "devices", "device_id"),
    ("job_logs", "jobs", "job_id"),
    ("ai_action", "devices", "target_device_id"),
    ("inventory_snapshots", "devices", "device_id"),
    ("installed_software", "devices", "device_id"),
    ("inventory_changes", "devices", "device_id"),
    ("hardware_components", "devices", "device_id"),
    ("security_events", "devices", "device_id"),
    ("push_batch_target", "jobs", "job_id"),
    ("agent_update_targets", "devices", "device_id"),
    ("backup_runs", "devices", "device_id"),
    ("backup_points", "devices", "device_id"),
    ("restore_jobs", "devices", "device_id"),
    ("backup_point_copies", "backup_points", "point_id"),
    ("deployment_results", "devices", "device_id"),
    ("script_runs", "devices", "device_id"),
    ("script_policies", "device_groups", "device_group_id"),
    ("health_alert", "devices", "device_id"),
    ("device_health_check_state", "devices", "device_id"),
    ("health_flap_state", "devices", "device_id"),
    ("device_health_policy_ack", "devices", "device_id"),
    ("health_alert_action", "health_alert", "alert_id"),
    ("device_patch_status", "devices", "device_id"),
    ("patch_reboots", "devices", "device_id"),
    ("patch_runs", "devices", "device_id"),
    ("device_person", "devices", "device_id"),
    ("remote_sessions", "devices", "device_id"),
    ("site_server", "devices", "device_id"),
]

O_TABLES: list[tuple[str, str | None, str | None]] = [  # (table, parent, fk)
    ("users", None, None),
    ("domain_events", None, None),
    ("policies", "device_groups", "device_group_id"),
    ("credentials", "device_groups", "device_group_id"),
    ("doc_articles", None, None),
    ("doc_records", None, None),
    ("doc_documents", None, None),
    ("doc_links", "doc_articles", "article_id"),
    ("doc_versions", "doc_articles", "article_id"),
    ("doc_document_versions", "doc_documents", "document_id"),
    ("doc_document_links", "doc_documents", "document_id"),
    ("push_host_key", "devices", "device_id"),
    ("agent_update_rings", "device_groups", "device_group_id"),
    ("backup_policies", "device_groups", "device_group_id"),
    ("backup_offsite_targets", None, None),
    ("backup_recovery_keys", None, None),
    ("change_gates", "device_groups", "device_group_id"),
    ("inbound_message", None, None),
    ("health_profile_assignment", "devices", "device_id"),
    ("software_ignore", None, None),
    ("patch_policies", "device_groups", "device_group_id"),
    ("patch_approvals", None, None),
    ("provision_run", "devices", "device_id"),
    ("remote_policies", "device_groups", "device_group_id"),
    ("sla_policy", None, None),
]

VISIBLE = "its_client_visible(client_id)"


def _policy(table: str, kind: str) -> None:
    if kind == "O":
        using = f"client_id IS NULL OR {VISIBLE}"
        check = (
            "(client_id IS NULL AND current_setting('app.client_scope', true) = 'all') "
            f"OR {VISIBLE}"
        )
    else:
        using = check = VISIBLE
    op.execute(
        f"CREATE POLICY client_isolation ON {table} AS RESTRICTIVE "
        f"USING ({using}) WITH CHECK ({check})"
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION its_client_visible(c uuid) RETURNS boolean
        LANGUAGE sql STABLE AS $$
            SELECT current_setting('app.client_scope', true) = 'all'
                OR c = ANY (string_to_array(
                       nullif(current_setting('app.current_clients', true), ''), ','
                   )::uuid[])
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION its_client_from_parent() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE parent_id uuid; parent_client uuid;
        BEGIN
            EXECUTE format('SELECT ($1).%I', TG_ARGV[1]) USING NEW INTO parent_id;
            IF parent_id IS NULL THEN
                RETURN NEW;
            END IF;
            EXECUTE format('SELECT client_id FROM %I WHERE id = $1', TG_ARGV[0])
                USING parent_id INTO parent_client;
            IF NEW.client_id IS NOT NULL AND parent_client IS NOT NULL
               AND NEW.client_id <> parent_client THEN
                RAISE EXCEPTION '%.client_id % differs from its %''s client %',
                    TG_TABLE_NAME, NEW.client_id, TG_ARGV[0], parent_client
                    USING ERRCODE = 'check_violation';
            END IF;
            NEW.client_id := parent_client;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION its_job_client() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE target_client uuid;
        BEGIN
            IF NEW.target_type = 'group' THEN
                SELECT client_id INTO target_client FROM device_groups WHERE id = NEW.target_id;
            ELSE
                SELECT client_id INTO target_client FROM devices WHERE id = NEW.target_id;
            END IF;
            IF NEW.client_id IS NOT NULL AND target_client IS NOT NULL
               AND NEW.client_id <> target_client THEN
                RAISE EXCEPTION 'jobs.client_id differs from its target''s client'
                    USING ERRCODE = 'check_violation';
            END IF;
            NEW.client_id := target_client;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION its_device_client_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE s_client uuid; g_client uuid;
        BEGIN
            IF TG_OP = 'UPDATE' AND NEW.client_id <> OLD.client_id THEN
                RAISE EXCEPTION 'a device cannot move to another client (spec §1.5)'
                    USING ERRCODE = 'check_violation';
            END IF;
            SELECT client_id INTO s_client FROM sites WHERE id = NEW.site_id;
            IF s_client IS DISTINCT FROM NEW.client_id THEN
                RAISE EXCEPTION 'device site belongs to another client'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.device_group_id IS NOT NULL THEN
                SELECT client_id INTO g_client FROM device_groups WHERE id = NEW.device_group_id;
                IF g_client IS DISTINCT FROM NEW.client_id THEN
                    RAISE EXCEPTION 'device group belongs to another client'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION its_group_client_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE p_client uuid;
        BEGIN
            IF TG_OP = 'UPDATE' AND NEW.client_id <> OLD.client_id THEN
                RAISE EXCEPTION 'a device group cannot move to another client'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.parent_group_id IS NOT NULL THEN
                SELECT client_id INTO p_client FROM device_groups WHERE id = NEW.parent_group_id;
                IF p_client IS DISTINCT FROM NEW.client_id THEN
                    RAISE EXCEPTION 'parent group belongs to another client'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END $$
        """
    )

    # C tables. `sites` already has client_id (0230).
    for table in C_TABLES[1:]:
        op.execute(f"ALTER TABLE {table} ADD COLUMN client_id uuid REFERENCES clients(id)")
        op.execute(f"UPDATE {table} SET client_id = org_id")  # the internal client
        op.execute(f"ALTER TABLE {table} ALTER COLUMN client_id SET NOT NULL")
        op.execute(f"CREATE INDEX ix_{table}_client ON {table} (client_id)")
    for table in ("devices", "enrollment_tokens", "install_links"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN site_id uuid REFERENCES sites(id)")
        op.execute(f"UPDATE {table} SET site_id = org_id")  # the internal Principal site
        op.execute(f"ALTER TABLE {table} ALTER COLUMN site_id SET NOT NULL")
    op.execute("ALTER TABLE install_links DROP CONSTRAINT uq_install_links_org")
    op.execute("ALTER TABLE install_links ADD CONSTRAINT uq_install_links_site UNIQUE (site_id)")
    op.execute(
        "CREATE TRIGGER its_device_client_guard BEFORE INSERT OR UPDATE ON devices "
        "FOR EACH ROW EXECUTE FUNCTION its_device_client_guard()"
    )
    op.execute(
        "CREATE TRIGGER its_group_client_guard BEFORE INSERT OR UPDATE ON device_groups "
        "FOR EACH ROW EXECUTE FUNCTION its_group_client_guard()"
    )
    # A group-scoped grant migrated by 0230 to the internal client keeps it:
    # every existing group is the internal client's now.

    # jobs (H, special)
    op.execute("ALTER TABLE jobs ADD COLUMN client_id uuid REFERENCES clients(id)")
    op.execute(
        "UPDATE jobs j SET client_id = COALESCE("
        "(SELECT client_id FROM devices WHERE id = j.target_id),"
        "(SELECT client_id FROM device_groups WHERE id = j.target_id), j.org_id)"
    )
    op.execute("ALTER TABLE jobs ALTER COLUMN client_id SET NOT NULL")
    op.execute("CREATE INDEX ix_jobs_client ON jobs (client_id)")
    op.execute(
        "CREATE TRIGGER its_job_client BEFORE INSERT OR UPDATE OF target_id, target_type "
        "ON jobs FOR EACH ROW EXECUTE FUNCTION its_job_client()"
    )

    for table, parent, fk in H_TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN client_id uuid REFERENCES clients(id)")
        op.execute(
            f"UPDATE {table} t SET client_id = p.client_id FROM {parent} p WHERE p.id = t.{fk}"
        )
        op.execute(f"ALTER TABLE {table} ALTER COLUMN client_id SET NOT NULL")
        op.execute(f"CREATE INDEX ix_{table}_client ON {table} (client_id)")
        op.execute(
            f"CREATE TRIGGER its_client_from_parent BEFORE INSERT OR UPDATE OF {fk} ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION its_client_from_parent('{parent}', '{fk}')"
        )

    for table, parent, fk in O_TABLES:
        if table != "users":  # users.client_id exists since 0230
            op.execute(f"ALTER TABLE {table} ADD COLUMN client_id uuid REFERENCES clients(id)")
        if parent is not None:
            op.execute(
                f"UPDATE {table} t SET client_id = p.client_id FROM {parent} p "
                f"WHERE p.id = t.{fk}"
            )
            op.execute(
                f"CREATE TRIGGER its_client_from_parent BEFORE INSERT OR UPDATE OF {fk} "
                f"ON {table} FOR EACH ROW EXECUTE FUNCTION its_client_from_parent('{parent}', '{fk}')"
            )
        op.execute(f"CREATE INDEX ix_{table}_client ON {table} (client_id)")

    for table in C_TABLES + ["jobs"] + [t for t, _, _ in H_TABLES]:
        _policy(table, "C")
    for table, _, _ in O_TABLES:
        _policy(table, "O")

    # `clients` itself: a list scope sees its own clients; never `unassigned`
    # unless the scope is all (spec §1.1).
    op.execute(
        "CREATE POLICY client_isolation ON clients AS RESTRICTIVE "
        "USING (its_client_visible(id) AND (kind <> 'unassigned' "
        "OR current_setting('app.client_scope', true) = 'all')) "
        "WITH CHECK (current_setting('app.client_scope', true) = 'all' OR its_client_visible(id))"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS client_isolation ON clients")
    for table, parent, _ in O_TABLES:
        op.execute(f"DROP POLICY IF EXISTS client_isolation ON {table}")
        if parent is not None:
            op.execute(f"DROP TRIGGER IF EXISTS its_client_from_parent ON {table}")
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_client")
        if table != "users":
            op.execute(f"ALTER TABLE {table} DROP COLUMN client_id")
    for table, _, _ in H_TABLES:
        op.execute(f"DROP POLICY IF EXISTS client_isolation ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS its_client_from_parent ON {table}")
        op.execute(f"ALTER TABLE {table} DROP COLUMN client_id")
    op.execute("DROP POLICY IF EXISTS client_isolation ON jobs")
    op.execute("DROP TRIGGER IF EXISTS its_job_client ON jobs")
    op.execute("ALTER TABLE jobs DROP COLUMN client_id")
    op.execute("DROP TRIGGER IF EXISTS its_device_client_guard ON devices")
    op.execute("DROP TRIGGER IF EXISTS its_group_client_guard ON device_groups")
    op.execute("ALTER TABLE install_links DROP CONSTRAINT uq_install_links_site")
    op.execute("ALTER TABLE install_links ADD CONSTRAINT uq_install_links_org UNIQUE (org_id)")
    for table in ("devices", "enrollment_tokens", "install_links"):
        op.execute(f"ALTER TABLE {table} DROP COLUMN site_id")
    for table in C_TABLES:
        op.execute(f"DROP POLICY IF EXISTS client_isolation ON {table}")
    for table in C_TABLES[1:]:
        op.execute(f"ALTER TABLE {table} DROP COLUMN client_id")
    for fn in (
        "its_group_client_guard()", "its_device_client_guard()", "its_job_client()",
        "its_client_from_parent()", "its_client_visible(uuid)",
    ):
        op.execute(f"DROP FUNCTION {fn}")
```

Confirm `uq_install_links_org` is a constraint and not only an index:

Run: `grep -n "uq_install_links_org" migrations/versions/*.py`

If it was created as an index, use `DROP INDEX` instead of `DROP CONSTRAINT`.

`health_profile_assignment` targets a device **or** a group. Its trigger in the loop above derives from `device_id` only. Add a second trigger for `device_group_id`, after the O loop:

```python
    op.execute(
        "CREATE TRIGGER its_client_from_group BEFORE INSERT OR UPDATE OF device_group_id "
        "ON health_profile_assignment FOR EACH ROW "
        "EXECUTE FUNCTION its_client_from_parent('device_groups', 'device_group_id')"
    )
```

Add `op.execute("DROP TRIGGER IF EXISTS its_client_from_group ON health_profile_assignment")` at the top of `downgrade()`.

- [ ] **Step 2: Modelos**

For each table in the three lists, add the column to its model class.

C and H tables:

```python
    # MSP clients spec, Annex A (C|H). Set by <the application|trigger its_client_from_parent>.
    client_id: Mapped[UUID] = mapped_column(ForeignKey("clients.id"))
```

`Device`, `EnrollmentToken` and `InstallLink` also get:

```python
    site_id: Mapped[UUID] = mapped_column(ForeignKey("sites.id"))
```

O tables get `client_id: Mapped[UUID | None] = mapped_column(ForeignKey("clients.id"), default=None)`.

For H tables the ORM must not send `NULL` explicitly over the trigger's value. With `default=None` absent and the column omitted from the constructor, SQLAlchemy omits it from the INSERT, so the `BEFORE INSERT` trigger fills it. Mark H columns with `server_default=FetchedValue()` (`from sqlalchemy import FetchedValue`) so the ORM reads the value back after flush.

In `InstallLink.__table_args__`, replace `UniqueConstraint("org_id", name="uq_install_links_org")` with `UniqueConstraint("site_id", name="uq_install_links_site")`.

- [ ] **Step 3: Ejecutar las pruebas del catálogo**

Run: `.venv/bin/pytest -q tests/test_client_catalog.py tests/test_clients_schema.py tests/test_rls.py tests/test_rls_invariant_sql.py`
Expected: PASS.

- [ ] **Step 4: Commit (todavía rojo en la suite: la Task 7 arregla los constructores)**

```bash
git add backend/migrations/versions/0231_client_scope_rls.py backend/app/models
git commit -m "feat(clients): client_id, inheritance triggers and restrictive RLS on phase-1 tables (spec §2.2-2.4)"
```

---

### Task 7: Equipos, grupos e inscripción con cliente y sede

**Files:**
- Create: `backend/scripts/codemods/add_device_client.py`
- Modify: the tests that construct `Device(`, `DeviceGroup(`, `EnrollmentToken(` and `InstallLink(` (the codemod's output; 114 + 44 + 5 + 5 sites)
- Modify: `backend/app/services/enrollment.py:566-580, 770-786`, `backend/app/modules/ipam/candidates.py:266, 558`, `backend/app/api/v1/devices.py:981`, `backend/app/services/device_groups.py` (create)
- Modify: `backend/app/api/v1/enrollment.py` (token creation requires `site_id`) and `backend/app/services/install_link.py` (per-site link, networks from `sites.egress_networks`)
- Test: `backend/tests/test_client_enrollment.py`

**Interfaces:**
- Consumes: `Site`, `Client`, the id convention.
- Produces:
  - `POST /enrollment-tokens` body gains `site_id: UUID`, which is required. `client_id` is derived from the site.
  - The install link is addressed by site: `GET`/`PUT /sites/{site_id}/install-link`. The old org-level route answers 404 so no stale client can use it.
  - Enrollment sessions run with `ClientScope.only([token.client_id])`.

- [ ] **Step 1: Codemod de constructores en las pruebas**

```python
# backend/scripts/codemods/add_device_client.py
"""Tests only: give Device/DeviceGroup/EnrollmentToken/InstallLink their client.

Every such constructor in tests/ already passes `org_id=<expr>`. By the id
convention (plan Global Constraints) the internal client and its Principal
site have that same id, so this adds `client_id=<expr>` (and `site_id=<expr>`
where the model has one). Idempotent. Prints files it could not rewrite (a
constructor without `org_id=`), which are fixed by hand.
"""

import sys
from pathlib import Path

import libcst as cst

WITH_SITE = {"Device", "EnrollmentToken", "InstallLink"}
TARGETS = WITH_SITE | {"DeviceGroup"}


class _T(cst.CSTTransformer):
    def __init__(self) -> None:
        self.missing = False

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.Call:
        f = updated_node.func
        name = f.value if isinstance(f, cst.Name) else None
        if name not in TARGETS:
            return updated_node
        kw = {a.keyword.value: a for a in updated_node.args if a.keyword is not None}
        if "org_id" not in kw:
            self.missing = True
            return updated_node
        org = kw["org_id"].value
        add = []
        eq = cst.AssignEqual(cst.SimpleWhitespace(""), cst.SimpleWhitespace(""))
        if "client_id" not in kw:
            add.append(cst.Arg(keyword=cst.Name("client_id"), value=org, equal=eq))
        if name in WITH_SITE and "site_id" not in kw:
            add.append(cst.Arg(keyword=cst.Name("site_id"), value=org, equal=eq))
        if not add:
            return updated_node
        args = list(updated_node.args)
        args[-1] = args[-1].with_changes(comma=cst.Comma(whitespace_after=cst.SimpleWhitespace(" ")))
        return updated_node.with_changes(args=args + add)


def main(root: str) -> None:
    for path in sorted(Path(root).rglob("*.py")):
        src = path.read_text()
        if not any(f"{t}(" in src for t in TARGETS):
            continue
        t = _T()
        new = cst.parse_module(src).visit(t)
        if new.code != src:
            path.write_text(new.code)
        if t.missing:
            print(path)


if __name__ == "__main__":
    main(sys.argv[1])
```

Run: `.venv/bin/python -I scripts/codemods/add_device_client.py tests > /tmp/by_hand.txt; cat /tmp/by_hand.txt`

For each file listed, add `client_id=` (and `site_id=`) by hand with the org's id. Raw `INSERT INTO devices` in tests (2 sites, `grep -rn "INSERT INTO devices" tests`) get `client_id` and `site_id` columns with the org id.

- [ ] **Step 2: Escribir las pruebas de inscripción que fallan**

```python
# backend/tests/test_client_enrollment.py
"""Spec §5 test 10 and §1.5: a device lands in its link's client and site."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Client, Device, Site


async def _customer_with_site(env, org_id):
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        c = Client(org_id=org_id, name="Cliente X", kind="customer")
        s.add(c)
        await s.flush()
        site = Site(org_id=org_id, client_id=c.id, name="Bogotá")
        s.add(site)
        await s.commit()
        return c.id, site.id


async def test_a_token_is_minted_for_a_site_and_carries_its_client(api_env) -> None:
    env = api_env
    org = await env.create_org("enr")
    sub = await env.create_user(org, "org-admin")
    client_id, site_id = await _customer_with_site(env, org)
    r = await env.client.post(
        "/api/v1/enrollment-tokens",
        json={"name": "x", "max_uses": 1, "expires_in_hours": 1, "site_id": str(site_id)},
        headers=await env.headers(org, sub),
    )
    assert r.status_code == 201, r.text
    assert r.json()["client_id"] == str(client_id)
    assert r.json()["site_id"] == str(site_id)


async def test_a_token_without_a_site_is_refused(api_env) -> None:
    env = api_env
    org = await env.create_org("enr")
    sub = await env.create_user(org, "org-admin")
    r = await env.client.post(
        "/api/v1/enrollment-tokens",
        json={"name": "x", "max_uses": 1, "expires_in_hours": 1},
        headers=await env.headers(org, sub),
    )
    assert r.status_code == 422
```

Before running, confirm the token-creation body's existing field names in `backend/app/api/v1/enrollment.py:18-55`; `max_uses` and the expiry field may be named differently. Use the real names. Then add one test that enrolls an agent with such a token, reusing the helper that `tests/test_enrollment*.py` already uses to post a CSR (`grep -ln "enroll/v1" tests | head -3`), and asserts the new `Device` row has `client_id == client_id` and `site_id == site_id`.

- [ ] **Step 3: Ejecutar y ver que fallan**

Run: `.venv/bin/pytest -q tests/test_client_enrollment.py`
Expected: FAIL. The body is accepted without `site_id`, and the response has no `client_id`.

- [ ] **Step 4: Implementar**

- **`api/v1/enrollment.py`:**
  - The create body gains `site_id: UUID`, required.
  - Load the site in the request's session; RLS makes another client's site invisible, which gives 404.
  - Write `client_id=site.client_id, site_id=site.id` on the `EnrollmentToken`.
  - Return both in the response model.
- **`services/enrollment.py` (token path, `:566`):** the `Device(...)` gets `client_id=token.client_id, site_id=token.site_id`.
  - Load the token row's `client_id`/`site_id` in the same `UPDATE ... RETURNING`: add `EnrollmentToken.client_id, EnrollmentToken.site_id` to `.returning(...)`.
  - The enrollment session that runs `resolve_enrolment_target` is opened with `clients=ClientScope.only([client_id])`, so an asset row of another client is never linked (spec §1.5).
  - Read `services/enrollment.py` around `:520` to find where that session is opened, and change its `clients=` from the codemod's `ClientScope.all()` to the token's scope. That removes the file from the review list.
- **`services/enrollment.py` (link path, `:771`):** the same, from the link's `client_id`/`site_id`.
- **`services/install_link.py`:** the link is per site.
  - The allowed networks are `sites.egress_networks`; the link's own `networks` column stays in the table, but the service stops reading it. Its removal is in the phase-2 cleanup list, as the docstring says.
  - The refusal at `:293` ("requires ≥1 allowed network") now names the site.
- **`api/v1/devices.py:981` (manual asset create):** the body gains `site_id`, required; `client_id` comes from the site.
- **`modules/ipam/candidates.py:266, 558` (promotion of a discovered machine):** the device takes the subnet's client and site. In phase 1 IPAM is still unclient-ed, so use the **internal** client and Principal site (`org_id`, by the id convention) and add the comment `# PHASE 2: the subnet's client/site (plan "Fuera de esta fase")`. Phase 2's IPAM task replaces it.
- **`services/device_groups.py` (create):** the body gains `client_id`, required, and a parent group must be of the same client (the trigger enforces it; map its `check_violation` to 422).
  - Dynamic-rule evaluation moves a device only into groups of the device's client. Add `DeviceGroup.client_id == device.client_id` to the evaluator's candidate-group query (`grep -n "def evaluate" app/services/device_groups.py`).

- [ ] **Step 5: Ejecutar toda la suite**

Run: `.venv/bin/pytest -q -p no:cacheprovider 2>&1 | tail -20`
Expected: all green. Any failure names a constructor or route that does not yet pass client/site. Fix it at the source and do not relax the column.

- [ ] **Step 6: Commit**

```bash
git add -A backend/app backend/tests backend/scripts/codemods/add_device_client.py
git commit -m "feat(clients): devices, groups and enrollment carry client and site (spec §1.5, §3.2)"
```

---

### Task 8: Pruebas de aislamiento sobre todo el catálogo

**Files:**
- Create: `backend/tests/test_client_isolation.py`

**Interfaces:**
- Consumes: `PHASE_1`, `ClientScope`, `org_session`.

- [ ] **Step 1: Escribir las pruebas**

```python
# backend/tests/test_client_isolation.py
"""Spec §5 tests 3-8, over every phase-1 table, through the real RLS.

Seeding is through the ADMIN engine (no RLS), asserting is through
`org_session` (RLS on). The fixture makes the client the ONLY discriminator:
X and Y share the org, a device each, a group each, and one job each; any
other column that separated them would let a missing predicate hide
(HANDOFF.md §4).
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.client_scope_catalog import PHASE_1
from app.core.tenancy import org_session


async def _two_clients(admin_engine) -> tuple[UUID, UUID, UUID, UUID, UUID]:
    org = uuid4()
    x, y = uuid4(), uuid4()
    sx, sy = uuid4(), uuid4()
    dx, dy = uuid4(), uuid4()
    async with AsyncSession(admin_engine) as s:
        await s.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:o, 'O', :slug)"),
                        {"o": org, "slug": f"iso-{org.hex[:8]}"})
        for c, site, dev in ((x, sx, dx), (y, sy, dy)):
            await s.execute(text("INSERT INTO clients (id, org_id, name, kind) VALUES (:c, :o, :n, 'customer')"),
                            {"c": c, "o": org, "n": f"c-{c.hex[:6]}"})
            await s.execute(text("INSERT INTO sites (id, org_id, client_id, name) VALUES (:s, :o, :c, 'S')"),
                            {"s": site, "o": org, "c": c})
            await s.execute(text("INSERT INTO devices (id, org_id, client_id, site_id, hostname) "
                                 "VALUES (:d, :o, :c, :s, 'h')"),
                            {"d": dev, "o": org, "c": c, "s": site})
        await s.commit()
    return org, x, y, dx, dy


async def test_a_list_scope_sees_only_its_clients_devices(admin_engine, app_engine) -> None:
    org, x, y, dx, dy = await _two_clients(admin_engine)
    async with org_session(org, clients=ClientScope.only([x]), engine=app_engine) as s:
        seen = set((await s.execute(text("SELECT id FROM devices"))).scalars())
    assert seen == {dx}


async def test_no_scope_sees_nothing(admin_engine, app_engine) -> None:
    org, *_ = await _two_clients(admin_engine)
    async with AsyncSession(app_engine) as s, s.begin():
        await s.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(org)})
        assert (await s.execute(text("SELECT count(*) FROM devices"))).scalar() == 0


async def test_cannot_insert_into_another_client(admin_engine, app_engine) -> None:
    org, x, y, dx, dy = await _two_clients(admin_engine)
    with pytest.raises(Exception, match="row-level security|violates"):
        async with org_session(org, clients=ClientScope.only([x]), engine=app_engine) as s:
            await s.execute(text("INSERT INTO device_tags (id, org_id, device_id, tag) "
                                 "VALUES (gen_random_uuid(), :o, :d, 't')"), {"o": org, "d": dy})


async def test_a_child_inherits_and_cannot_contradict(admin_engine, app_engine) -> None:
    org, x, y, dx, dy = await _two_clients(admin_engine)
    async with org_session(org, clients=ClientScope.all(), engine=app_engine) as s:
        await s.execute(text("INSERT INTO device_tags (id, org_id, device_id, tag) "
                             "VALUES (gen_random_uuid(), :o, :d, 't')"), {"o": org, "d": dx})
        got = (await s.execute(text("SELECT client_id FROM device_tags WHERE device_id = :d"),
                               {"d": dx})).scalar()
    assert got == x
    with pytest.raises(Exception, match="differs"):
        async with org_session(org, clients=ClientScope.all(), engine=app_engine) as s:
            await s.execute(text("INSERT INTO device_tags (id, org_id, device_id, tag, client_id) "
                                 "VALUES (gen_random_uuid(), :o, :d, 't', :y)"),
                            {"o": org, "d": dx, "y": y})


async def test_a_device_cannot_change_client(admin_engine, app_engine) -> None:
    org, x, y, dx, dy = await _two_clients(admin_engine)
    with pytest.raises(Exception, match="cannot move to another client"):
        async with org_session(org, clients=ClientScope.all(), engine=app_engine) as s:
            await s.execute(text("UPDATE devices SET client_id = :y WHERE id = :d"), {"y": y, "d": dx})


async def test_a_restricted_scope_reads_global_config_but_cannot_write_it(admin_engine, app_engine) -> None:
    org, x, *_ = await _two_clients(admin_engine)
    async with AsyncSession(admin_engine) as s:
        await s.execute(text("INSERT INTO software_ignore (id, org_id, name) VALUES (gen_random_uuid(), :o, 'g')"),
                        {"o": org})
        await s.commit()
    async with org_session(org, clients=ClientScope.only([x]), engine=app_engine) as s:
        assert (await s.execute(text("SELECT count(*) FROM software_ignore"))).scalar() == 1
    with pytest.raises(Exception, match="row-level security"):
        async with org_session(org, clients=ClientScope.only([x]), engine=app_engine) as s:
            await s.execute(text("INSERT INTO software_ignore (id, org_id, name) "
                                 "VALUES (gen_random_uuid(), :o, 'g2')"), {"o": org})


@pytest.mark.parametrize("table", sorted(t for t, e in PHASE_1.items() if e.kind in {"C", "H"}))
async def test_every_scoped_table_hides_other_clients_rows(table, admin_engine, app_engine) -> None:
    """The sweep: for each C/H table, a list scope's count of rows with the
    OTHER client's id is zero. Rows are not seeded per table (each has its own
    NOT NULL columns); instead the predicate is checked directly, which is what
    the policy evaluates: `its_client_visible(<other client>)` must be false
    under the list scope and the policy must reference it (catalog test 2)."""
    org, x, y, *_ = await _two_clients(admin_engine)
    async with org_session(org, clients=ClientScope.only([x]), engine=app_engine) as s:
        assert (await s.execute(text("SELECT its_client_visible(:y)"), {"y": y})).scalar() is not True
        assert (await s.execute(text("SELECT its_client_visible(:x)"), {"x": x})).scalar() is True
        await s.execute(text(f"SELECT count(*) FROM {table}"))  # the table is readable under RLS
```

Check the column names (`device_tags.tag`, `software_ignore.name`) against their models before running (`grep -n "class DeviceTag\|class SoftwareIgnore" -A12 app/models/core.py app/modules/licenses/models.py`). Use the real NOT NULL columns, keeping the assertion.

- [ ] **Step 2: Ejecutar**

Run: `.venv/bin/pytest -q tests/test_client_isolation.py`
Expected: PASS.

- [ ] **Step 3: Mutación — comprobar que las pruebas detectan una política borrada**

```bash
sudo -u postgres psql -At -c "SELECT datname FROM pg_database WHERE datname LIKE 'its_test_%' ORDER BY oid DESC LIMIT 1"
```

The pytest harness drops its database on exit, so this is done by editing the migration instead:
1. Comment out `_policy(table, "C")` for `devices` only (`if table != "devices"`).
2. Assert the edit applied: `git diff --stat` shows `0231` changed.
3. Run `pytest -q tests/test_client_isolation.py::test_a_list_scope_sees_only_its_clients_devices tests/test_client_catalog.py::test_phase_1_tables_carry_the_restrictive_policy`.

Expected: both FAIL. Revert with `git checkout backend/migrations/versions/0231_client_scope_rls.py`, then re-run them and expect PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/test_client_isolation.py
git commit -m "test(clients): isolation sweep over every phase-1 table (spec §5 tests 3-8)"
```

---

### Task 9: API de clientes, sedes y contactos

**Files:**
- Create: `backend/app/services/clients.py`
- Create: `backend/app/api/v1/clients.py`
- Modify: `backend/app/main.py` (include the router; follow how other `api/v1` routers are included: `grep -n "include_router" app/main.py | head`)
- Modify: `backend/app/core/rbac.py` (`PERMISSIONS`: `clients.read`, `clients.manage`, `sites.manage`) and a migration `backend/migrations/versions/0232_client_permissions.py` seeding them into `permissions` and granting them to `org-admin`. Follow the latest migration that seeded a permission: `grep -ln "INSERT INTO permissions" migrations/versions | tail -1`.
- Modify: `backend/app/i18n/` (or wherever `tr()` catalogues live: `grep -rn "def tr(" app/core/i18n*`), Spanish strings for every new message
- Test: `backend/tests/test_clients_api.py`

**Interfaces:**
- Produces the routes of spec §3.2:
  - `GET /api/v1/clients`, `POST /api/v1/clients`, `GET/PATCH /api/v1/clients/{id}`, `POST /api/v1/clients/{id}/archive`;
  - `GET/POST /api/v1/clients/{id}/sites`, `PATCH/DELETE /api/v1/sites/{id}`;
  - `GET/POST /api/v1/clients/{id}/contacts`, `PATCH /api/v1/contacts/{id}`.
- Response model `ClientOut{id, name, kind, status, email_domains, phone, address, website, notes, enabled_modules, device_count, site_count}`.

- [ ] **Step 1: Escribir las pruebas que fallan**

```python
# backend/tests/test_clients_api.py
async def _admin(env):
    org = await env.create_org("cli")
    sub = await env.create_user(org, "org-admin")
    return org, await env.headers(org, sub)


async def test_create_list_and_principal_site(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    r = await env.client.post("/api/v1/clients", json={"name": "Acme"}, headers=h)
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    sites = (await env.client.get(f"/api/v1/clients/{cid}/sites", headers=h)).json()
    assert [s["name"] for s in sites] == ["Principal"]
    names = {c["name"] for c in (await env.client.get("/api/v1/clients", headers=h)).json()}
    assert {"Acme", "Sin asignar"} <= names


async def test_duplicate_email_domain_is_409(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    assert (await env.client.post("/api/v1/clients", json={"name": "A", "email_domains": ["a.co"]}, headers=h)).status_code == 201
    r = await env.client.post("/api/v1/clients", json={"name": "B", "email_domains": ["A.co"]}, headers=h)
    assert r.status_code == 409


async def test_cannot_archive_system_clients(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    assert (await env.client.post(f"/api/v1/clients/{org}/archive", headers=h)).status_code == 409


async def test_cannot_delete_a_site_with_devices(api_env) -> None:
    env = api_env
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.models import Device
    org, h = await _admin(env)
    async with AsyncSession(env.admin_engine) as s:
        s.add(Device(org_id=org, client_id=org, site_id=org, hostname="pc1"))
        await s.commit()
    assert (await env.client.delete(f"/api/v1/sites/{org}", headers=h)).status_code == 409


async def test_a_restricted_technician_gets_404_for_another_client(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    x = (await env.client.post("/api/v1/clients", json={"name": "X"}, headers=h)).json()["id"]
    y = (await env.client.post("/api/v1/clients", json={"name": "Y"}, headers=h)).json()["id"]
    sub = await env.create_user(org)  # no roles yet
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.models import Role, User, UserRole
    async with AsyncSession(env.admin_engine) as s:
        user = (await s.execute(select(User).where(User.external_subject == sub))).scalar_one()
        role = (await s.execute(select(Role).where(Role.name == "org-admin", Role.org_id.is_(None)))).scalar_one()
        s.add(UserRole(org_id=org, user_id=user.id, role_id=role.id, client_id=x))
        await s.commit()
    hx = await env.headers(org, sub)
    assert (await env.client.get(f"/api/v1/clients/{y}", headers=hx)).status_code == 404
    assert (await env.client.get(f"/api/v1/clients/{x}", headers=hx)).status_code == 200
    listed = {c["id"] for c in (await env.client.get("/api/v1/clients", headers=hx)).json()}
    assert listed == {x}


async def test_x_client_id_narrows_and_never_widens(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    x = (await env.client.post("/api/v1/clients", json={"name": "X"}, headers=h)).json()["id"]
    narrowed = (await env.client.get("/api/v1/clients", headers={**h, "X-Client-Id": x})).json()
    assert [c["id"] for c in narrowed] == [x]
    assert (await env.client.get("/api/v1/clients", headers={**h, "X-Client-Id": "nope"})).status_code == 400


async def test_contacts_are_users_of_kind_contact(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    x = (await env.client.post("/api/v1/clients", json={"name": "X"}, headers=h)).json()["id"]
    r = await env.client.post(f"/api/v1/clients/{x}/contacts",
                              json={"email": "ana@x.co", "display_name": "Ana"}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "contact"
```

- [ ] **Step 2: Ejecutar y ver que fallan**

Run: `.venv/bin/pytest -q tests/test_clients_api.py`
Expected: FAIL with 404 on `/api/v1/clients`.

- [ ] **Step 3: Implementar el servicio**

```python
# backend/app/services/clients.py
"""Clients, sites and contacts (MSP clients spec §1, §3.2).

Business rules live here; `api/v1/clients.py` maps `ClientError` to HTTP.
Every function takes a session already scoped by the caller's `ClientScope`:
a client outside it is simply not found (404 by RLS, spec §3.2).
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.models import Client, Device, Site, User
from app.models.clients import CLIENT_KIND_CUSTOMER, CLIENT_KIND_UNASSIGNED, OPTIONAL_MODULES


@dataclass
class ClientError(Exception):
    status_code: int
    message: str


def _norm_domains(domains: list[str]) -> list[str]:
    return sorted({d.strip().lower() for d in domains if d.strip()})


async def _domain_taken(session: AsyncSession, org_id: UUID, domains: list[str], exclude: UUID | None) -> bool:
    if not domains:
        return False
    q = select(Client.id).where(Client.org_id == org_id, Client.email_domains.overlap(domains))
    if exclude is not None:
        q = q.where(Client.id != exclude)
    return (await session.execute(q.limit(1))).scalar_one_or_none() is not None


async def create_client(session: AsyncSession, *, org_id: UUID, actor_id: UUID, name: str,
                        email_domains: list[str], enabled_modules: list[str]) -> Client:
    domains = _norm_domains(email_domains)
    if set(enabled_modules) - set(OPTIONAL_MODULES):
        raise ClientError(422, "unknown optional module")
    if await _domain_taken(session, org_id, domains, None):
        raise ClientError(409, "an email domain already belongs to another client")
    client = Client(org_id=org_id, name=name.strip(), kind=CLIENT_KIND_CUSTOMER,
                    email_domains=domains, enabled_modules=sorted(set(enabled_modules)))
    session.add(client)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ClientError(409, "a client with that name already exists") from exc
    session.add(Site(org_id=org_id, client_id=client.id, name="Principal"))
    await session.flush()
    await audit_service.record(session, org_id=org_id, actor_type="user", actor_id=actor_id,
                               action="client.created", target_type="client",
                               target_id=str(client.id), metadata={"name": client.name})
    return client


async def archive_client(session: AsyncSession, *, org_id: UUID, actor_id: UUID, client_id: UUID) -> Client:
    client = await session.get(Client, client_id)
    if client is None:
        raise ClientError(404, "client not found")
    if client.kind != CLIENT_KIND_CUSTOMER:
        raise ClientError(409, "system clients cannot be archived")
    active = (await session.execute(
        select(func.count()).select_from(Device).where(Device.client_id == client_id,
                                                       Device.status != "retired")
    )).scalar_one()
    if active:
        raise ClientError(409, "retire this client's devices before archiving it")
    client.status = "archived"
    await audit_service.record(session, org_id=org_id, actor_type="user", actor_id=actor_id,
                               action="client.archived", target_type="client",
                               target_id=str(client_id), metadata={})
    return client


async def delete_site(session: AsyncSession, *, org_id: UUID, actor_id: UUID, site_id: UUID) -> None:
    site = await session.get(Site, site_id)
    if site is None:
        raise ClientError(404, "site not found")
    used = (await session.execute(
        select(func.count()).select_from(Device).where(Device.site_id == site_id)
    )).scalar_one()
    if used:
        raise ClientError(409, "move this site's devices to another site first")
    await session.delete(site)
    await audit_service.record(session, org_id=org_id, actor_type="user", actor_id=actor_id,
                               action="site.deleted", target_type="site",
                               target_id=str(site_id), metadata={"client_id": str(site.client_id)})


async def create_contact(session: AsyncSession, *, org_id: UUID, actor_id: UUID, client_id: UUID,
                         email: str, display_name: str) -> User:
    client = await session.get(Client, client_id)
    if client is None:
        raise ClientError(404, "client not found")
    if client.kind == CLIENT_KIND_UNASSIGNED:
        raise ClientError(422, "the unassigned client cannot have contacts")
    # NO external_subject YET: how a contact signs in is project 2 (spec §1.3).
    # A placeholder subject keeps the existing NOT NULL / unique column valid
    # and can never match a real token's `sub`.
    user = User(org_id=org_id, kind="contact", client_id=client_id, email=email.strip().lower(),
                display_name=display_name.strip(), external_subject=f"contact-pending:{email.strip().lower()}")
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ClientError(409, "that contact already exists") from exc
    await audit_service.record(session, org_id=org_id, actor_type="user", actor_id=actor_id,
                               action="contact.created", target_type="user",
                               target_id=str(user.id), metadata={"client_id": str(client_id)})
    return user
```

Before writing `create_contact`, check `User.external_subject`'s constraints with `grep -n "external_subject" app/models/core.py`. If it is nullable, drop the placeholder and leave it `None`. Also confirm the `Device.status` values with `grep -n "status" app/models/core.py | sed -n 1,20p`, and use the real "retired" value.

`update_client` (PATCH), `list_clients` (with `device_count`/`site_count` via grouped counts) and `create_site`/`update_site` follow the same shape:
- domain check through `_domain_taken(..., exclude=client_id)`;
- an audit event per write (`client.updated`, `site.created`, `site.updated`);
- `ClientError` for every refusal of spec §4.

- [ ] **Step 4: Implementar las rutas**

`backend/app/api/v1/clients.py` has one handler per route in the **Interfaces** list. Each one:
1. depends on `require_permission("clients.read" | "clients.manage" | "sites.manage")`;
2. opens `org_session(ctx.org_id, clients=ctx.clients, engine=...)`;
3. calls the service;
4. maps `ClientError` **outside** the session block to `HTTPException(e.status_code, tr(e.message))`. This is the existing convention (`app/modules/ipam/api.py` header).

`POST /clients` and `POST /clients/{id}/archive` also require `ctx.clients.is_all`, or they answer 403 `tr("only a technician with access to every client can do this")` (spec §2.1).

Add the Spanish translation of every new message to the catalogue `tr()` reads.

- [ ] **Step 5: Ejecutar**

Run: `.venv/bin/pytest -q tests/test_clients_api.py tests/test_rbac.py && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: PASS and clean. `test_rbac.py`'s route-completeness test proves every new route carries a permission.

- [ ] **Step 6: Commit**

```bash
git add -A backend/app backend/migrations/versions/0232_client_permissions.py backend/tests/test_clients_api.py
git commit -m "feat(clients): clients, sites and contacts API (spec §3.2)"
```

---

### Task 10: Canal en vivo por cliente

**Files:**
- Modify: `backend/app/gateway/dispatch.py:63-75` (`ui_channel(org_id, client_id)`; the publisher passes the device's client)
- Modify: every other `ui_channel(` publisher (`grep -rn "ui_channel(" app`)
- Modify: `backend/app/api/v1/ws.py:68-160` (subscribe per scope)
- Test: `backend/tests/test_ui_ws.py` (add a case)

- [ ] **Step 1: Prueba que falla**

Add to `tests/test_ui_ws.py`, following that file's handshake helper:
- a technician granted only on client X connects;
- an event published for a device of client Y is not received within 1 s;
- one for X is received.

Run it and confirm it fails: the current channel is per org, so Y's event arrives.

- [ ] **Step 2: Implementar**

```python
def ui_channel(org_id: UUID, client_id: UUID) -> str:
    return f"ui:{org_id}:{client_id}"


def ui_channel_pattern(org_id: UUID) -> str:
    return f"ui:{org_id}:*"
```

The publisher at `dispatch.py:74` reads the device's `client_id`. It already loads the device or job; add `client_id` to that select.

In `ws.py`:
1. Compute the scope the same way `auth_context` does: `clients_holding(session, user.id, None)`, narrowed by a `client` query parameter. A UUID only; anything else closes with 4400.
2. If `scope.is_all`, call `await pubsub.psubscribe(ui_channel_pattern(org_id))`.
3. Otherwise, `await pubsub.subscribe(*[ui_channel(org_id, c) for c in scope.ids])`.
4. With an empty list, close the socket with code 4403 and do not subscribe.

- [ ] **Step 3: Ejecutar y commit**

Run: `.venv/bin/pytest -q tests/test_ui_ws.py tests/test_gateway*.py`
Expected: PASS.

```bash
git add -A backend/app/gateway backend/app/api/v1/ws.py backend/tests/test_ui_ws.py
git commit -m "feat(clients): live console channel per client (spec §2.6)"
```

---

### Task 11: Enmienda 5a, documentación y verificación final

**Files:**
- Modify: `CLAUDE.md` (invariant 5a, text of spec §2.7, after invariant 6's paragraph, with the line "*(Approved by the operator 2026-10-23.)*")
- Modify: `docs/04-data-model.md` (clients and sites, one paragraph and a reference to the spec)
- Modify: `docs/superpowers/specs/2026-10-23-msp-clientes-design.md` (state the two refinements, under "Refinamientos durante la implementación"):
  1. the request scope is the clients holding the route's code (Task 4);
  2. the internal client and its Principal site share the org's id (Global Constraints).

- [ ] **Step 1: Suite completa, tipos y lint**

Run: `.venv/bin/pytest -q -p no:cacheprovider 2>&1 | tail -5 && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `0 failed`, `Success: no issues found`, and clean ruff. Copy the summary line into the PR description.

- [ ] **Step 2: Frontend sin cambios de comportamiento**

Run (desde `frontend/`): `npm ci && npx vitest run 2>&1 | tail -3 && npm run build 2>&1 | tail -3`
Expected: green. The API changes are additive for the console, with one exception: `POST /enrollment-tokens` now requires `site_id`. If a frontend test posts a token, it fails here. Fix the call in `frontend/src/modules/enrollment/` to send the internal Principal site, which is the org's id (`GET /api/v1/me` returns `org_id`). Phase 3 replaces that with the site picker.

- [ ] **Step 3: Commit y push**

```bash
git add CLAUDE.md docs/04-data-model.md docs/superpowers/specs/2026-10-23-msp-clientes-design.md frontend
git commit -m "docs(clients): invariant 5a and the phase-1 refinements"
git push -u origin claude/compassionate-bardeen-vgtwus
```
