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
