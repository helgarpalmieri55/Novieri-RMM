"""Push install inside the client (plan 2026-10-26 Task 12).

A push credential has exactly one client (Annex A; `push_credential_scope`
and `push_credential_realm` are H from `credentials` and NOT NULL). Existing
push-type credentials with no client are filed under the internal client
first, so their scope and realm rows can inherit one. So is any other
credential a scope or realm row already names: only a push credential can
have one through the API, but a child cannot inherit "no client", and one
such row would otherwise abort this migration at the NOT NULL.

`push_authorization` is C (a prefix means something only inside one
client's network). `push_run` is C and its client IS its credential's
(`its_push_run_client_guard`; the credential may later go to NULL, the one
change 0200's `push_run_fixed` allows). One active run per client (D3):
`uq_push_run_active` gains `client_id`. A run's target device is of the
run's client (`its_push_run_target_device_guard`).

Existing rows: every authorization takes the internal client, whose id is
the organisation's; a run takes its credential's client when the credential
is still there AND of the run's own organisation, the internal client
otherwise (a subselect that could name another organisation's credential
would abort `fk_push_run_client_org`). The keys cannot collide on the way
in: until now one prefix was authorized once per organisation, and one run
was active per organisation, so one per client is already true.

DOWNGRADE restores the per-organisation keys exactly as 0077 and 0200 wrote
them. It fails if two clients of one organisation now authorize one prefix,
or each have an active run, as 0231's own key restores do: downgrade is
CI's round trip, never production's rollback (restore-from-backup,
CLAUDE.md). The credentials filed under the internal client stay there.

Revision ID: 0241
Revises: 0240
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0241"
down_revision: str | None = "0240"
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


PUSH_TYPES = "('push_install', 'windows_domain')"
ACTIVE = "state IN ('running', 'paused', 'stopped_credential')"

# A run's client is its credential's, read only from a credential of the
# run's own organisation; a run whose credential is gone (or another
# organisation's) takes the internal client.
RUN_BACKFILL = (
    "COALESCE((SELECT c.client_id FROM credentials c WHERE c.id = push_run.credential_id "
    "AND c.org_id = push_run.org_id), push_run.org_id)"
)


def upgrade() -> None:
    op.execute(
        "UPDATE credentials SET client_id = org_id WHERE client_id IS NULL AND ("
        f"type IN {PUSH_TYPES} "
        "OR EXISTS (SELECT 1 FROM push_credential_scope s WHERE s.credential_id = credentials.id) "
        "OR EXISTS (SELECT 1 FROM push_credential_realm r WHERE r.credential_id = credentials.id))"
    )
    _add_c("push_authorization")
    _add_c("push_run", RUN_BACKFILL)
    _add_h("push_credential_scope", "credentials", "credential_id")
    _add_h("push_credential_realm", "credentials", "credential_id")
    _add_h("push_run_target", "push_run", "run_id")

    op.execute("DROP INDEX uq_push_authorization_active")
    op.execute(
        "CREATE UNIQUE INDEX uq_push_authorization_client_active "
        "ON push_authorization (org_id, client_id, prefix) WHERE revoked_at IS NULL"
    )
    op.execute("DROP INDEX uq_push_run_active")
    op.execute(
        f"CREATE UNIQUE INDEX uq_push_run_active ON push_run (org_id, client_id) WHERE {ACTIVE}"
    )

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
