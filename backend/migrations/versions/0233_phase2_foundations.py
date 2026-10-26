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
