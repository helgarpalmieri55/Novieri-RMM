"""Change management and BCP belong to a client (plan 2026-10-26 Task 7).

`change_request` and `bcp_plan` are C; their children H. No natural key
changes: `uq_change_link` holds `change_id`, the inheritance fk (research-5
§A). Existing rows go to the internal client, which holds both modules
since 0233.

What a row POINTS AT is not re-checked here: a plan's `scope` and
`procedure_refs`, a finding's `change_ref` and a link's `action_id` have no
foreign key (JSONB entries and bare UUIDs). The application holds every
later write that names one to the row's client (`bcp/service.py`,
`change_mgmt/links.py`); existing rows keep what they name, as Task 6 kept
existing allocations' targets.

Revision ID: 0237
Revises: 0236
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0237"
down_revision: str | None = "0236"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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
