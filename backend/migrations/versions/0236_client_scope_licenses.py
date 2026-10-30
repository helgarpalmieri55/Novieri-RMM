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

The guard is a BEFORE trigger, like the inheritance trigger `_add_h` puts on
the same table, and Postgres fires BEFORE triggers in name order:
`its_client_from_parent` < `its_license_allocation_target_guard`. So the
guard compares the client already copied from the license, also on an INSERT
that names none (`tests/test_client_scope_licenses.py` inserts one).

Existing allocations are backfilled with their license's client (the internal
one, like every license here) and their targets are NOT re-checked: the guard
holds every later write that names a target or a client, and a release
touches neither.

Existing `license_compliance_snapshot` rows were org-wide positions; the
backfill files them under the internal client, so the internal client's series
steps down at deploy, from the whole organisation's figures to its own.

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
