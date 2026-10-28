"""A deployment belongs to its target's client (plan 2026-10-26 Task 9).

`deployments` is C, but its client is never chosen: it is the target's -- one
device or one group, and a group is one client's (0231). Like `jobs` (0231's
`its_job_client`), a trigger derives it from `target_type` + `target_id`,
which has no foreign key. SECURITY INVOKER: a target the session cannot see
reads as NULL and the write is refused by name.

Existing rows take their target's client: a device or a group of the row's
own organisation, of the type the row names. Until now `create_deployment`
never resolved `target_id` (research-3 §E5), so a row may name a target that
is gone, another organisation's, or of the other type; each of those takes
the internal client, whose id is the organisation's, instead of a client the
`fk_deployments_client_org` key would refuse mid-upgrade. The trigger fires
only on the columns that name the target or the client, so such a row's
state still changes; retargeting it needs a real target.

Of the common block, `_add_c`, `_isolate` and `_drop_c` are used here; the
block is copied whole all the same, as the plan requires.

Revision ID: 0239
Revises: 0238
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0239"
down_revision: str | None = "0238"
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


BACKFILL = (
    "COALESCE("
    "(SELECT d.client_id FROM devices d WHERE d.id = deployments.target_id "
    "AND d.org_id = deployments.org_id AND deployments.target_type = 'device'), "
    "(SELECT g.client_id FROM device_groups g WHERE g.id = deployments.target_id "
    "AND g.org_id = deployments.org_id AND deployments.target_type = 'group'), "
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
