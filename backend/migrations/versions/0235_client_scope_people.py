"""People belong to a client (plan 2026-10-26 Task 5).

`person` is C. A person tied to an account follows the account: a contact's
person is its client's, a staff member's is the internal client's (C4) --
`its_person_account_guard` holds that for every later write. Its logins and
checklists are H.

`device_person` (phase 1, H from devices) also names a person, and the two
must agree: `(person_id, client_id) REFERENCES person (id, client_id)`, the
0231 technique for sites -- a foreign key ignores RLS, and a NULL person_id
skips the check. Existing links that disagree are UNLINKED (person_id set to
NULL, and handed back to inventory: `source = 'inventory'`, `set_by` NULL)
before the key is added: there is no data to keep (spec P7), and the
alternative is a migration that fails on a populated database.

Natural keys become per client: a person's e-mail and a login are unique
within their client. `person.user_id` stays unique across the organisation
(an account is one person), named in `ORG_WIDE_KEYS`.

`its_person_account_guard` is SECURITY INVOKER, like 0234's ticket guard: the
account is read under the session's own RLS. An account the session cannot
see -- a contact of a client outside a narrowed technician's scope -- is
refused rather than waved through, so a narrowed session cannot tie a person
to an account it could not have named (the ticket guard's "requester is not
visible"). Under scope all every account is visible and the rule is exact.

Revision ID: 0235
Revises: 0234
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0235"
down_revision: str | None = "0234"
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
    # Unlinked AND handed back to inventory: an operator override left with
    # no person reads as "an operator said nobody uses this", and inventory
    # never re-resolves an override (`people.service.observe_login`).
    op.execute(
        "UPDATE device_person dp SET person_id = NULL, source = 'inventory', set_by = NULL, "
        "updated_at = now() FROM person p "
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
        DECLARE u_found boolean; u_kind text; u_client uuid;
        BEGIN
            IF NEW.user_id IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT true, kind, client_id INTO u_found, u_kind, u_client
                FROM users WHERE id = NEW.user_id;
            IF u_found IS NULL THEN
                RAISE EXCEPTION 'a person''s account is not visible'
                    USING ERRCODE = 'check_violation';
            END IF;
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
