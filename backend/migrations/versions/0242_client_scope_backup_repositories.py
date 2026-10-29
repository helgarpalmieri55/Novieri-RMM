"""A backup repository serves one client (plan 2026-10-26 Task 13, C1).

A repository's master key travels in the signed parameters of every device
that backs up into it, and its chunks are deduplicated per repository: shared
by two clients, a device of Y would hold X's key and could probe X's content
(research-5 §E1). So `backup_repositories` is C, `backup_chunks` and
`backup_chunk_copies` are H from it, and all three are under the client
policy. A repository's name is unique per client.

Existing repositories go to the internal client, and so do the group-less,
client-less ("every client") policies that named one: a policy for every
client can no longer name a repository, which belongs to one. Customer
clients then get no backup until a person gives each its own repository and
default policy -- refused by silence, the right direction (no data to keep,
spec P7). A group policy of a customer client's group keeps the repository it
names, which is now another client's: the guard below refuses that pairing
the next time it is written, and the dispatcher refuses it every time and
records why (`backup.dispatch_refused`) -- once per device and policy
version, asked through `ix_audit_backup_dispatch_refused`, a partial index of
`audit_events (org_id, target_id)` holding only those rows (the 0220
precedent). It is built inside this transaction: `audit_events` is
append-only by trigger and not partitioned, so a plain CREATE INDEX is
correct. Its SHARE lock holds every audit append until this migration
commits -- and in an upgrade the OLD api and worker are still running while
`migrate` runs (the deploy note's step 6), so their audit writes, and the
requests and sweeps waiting on them, stall for the build's duration: seconds
at this table's size.

A policy names only a repository of its own client
(`its_backup_policy_repository_guard`). Its trigger is named
`its_zz_backup_policy_repository_guard` so it fires after 0231's
`its_client_from_parent` (Postgres fires BEFORE triggers in name order), which
has by then copied the group's client into the row. A policy with no client
names no repository.

Runs and points carry their device's client (H, phase 1); the repository they
name must be the same client's: `(repository_id, client_id) REFERENCES
backup_repositories (id, client_id)`, added NOT VALID, deliberately. Before
this migration a device of a customer client could back up into an
organisation repository, which this migration files under the internal
client; such a run or point cannot satisfy the key, and validating it would
abort this migration on any host where that happened. Those rows are history
and are not re-judged. Every new row is checked: NOT VALID skips only the
initial scan, and the key's insert and update triggers exist all the same.
One consequence to keep: an UPDATE that leaves the key alone is not checked,
UNLESS the same transaction has already written that row -- Postgres then
re-checks the key, and a row from before 0242 fails it. So a transaction
writes such a row at most once (`backup.dispatch.ingest_backup_result` asks
its question before touching the run, for exactly this), and
`tests/test_client_scope_backup.py::test_every_writer_writes_a_row_from_before_0242_once`
drives every writer of runs and points through such a row.

Backfills, each scoped to the row's own organisation: a repository takes the
internal client of its organisation (`org_id`, the id convention); a chunk
and a chunk copy take their repository's client (`_add_h`); the policy UPDATE
writes `client_id = org_id`, only where the row has neither a client nor a
group (a group policy's client is its group's, which 0231 already wrote).

DOWNGRADE restores the per-organisation name key exactly as 0014 wrote it
(`backup_repositories_org_id_name_key`). It fails if two clients of one
organisation now share a repository name, as the earlier tasks' key restores
do: downgrade is CI's round trip, never production's rollback
(restore-from-backup, CLAUDE.md). The policies filed under the internal client
stay there.

Revision ID: 0242
Revises: 0241
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0242"
down_revision: str | None = "0241"
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
    ("backup_chunks", "backup_repositories", "repository_id"),
    ("backup_chunk_copies", "backup_repositories", "repository_id"),
]
RUN_TABLES = ["backup_runs", "backup_points"]


def upgrade() -> None:
    _add_c("backup_repositories")
    for table, parent, fk in H:
        _add_h(table, parent, fk)
    op.execute(
        "ALTER TABLE backup_repositories DROP CONSTRAINT backup_repositories_org_id_name_key"
    )
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
    # The repository is read through the caller's RLS, so one the session
    # cannot see -- another organisation's, which the bare foreign key on
    # `repository_id` allows -- reads as no client and is refused like
    # another client's (`r_client IS NULL`). Every repository has a client,
    # so a policy with none is always refused. Client equality is also
    # organisation equality: both rows' clients are pinned to their own
    # organisation (`fk_<table>_client_org`).
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
            IF r_client IS NULL OR r_client IS DISTINCT FROM NEW.client_id THEN
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
    op.execute(
        "CREATE INDEX ix_audit_backup_dispatch_refused ON audit_events (org_id, target_id) "
        "WHERE action = 'backup.dispatch_refused'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_audit_backup_dispatch_refused")
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
