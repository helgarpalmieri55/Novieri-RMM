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

A VLAN THAT LOSES ITS SITE CAN COLLIDE, and is folded into one row first.
Until now VLAN 10 could exist once per IPAM site and once org-wide. With the
sites gone every existing VLAN is site-less and of the internal client, whose
VLAN 10 is one row (`uq_ipam_vlan_client_vid`) -- so an organisation that had
VLAN 10 at two sites would abort this migration at that index. Before the
site column goes, each organisation's VLANs that share a number are folded
into one: the one that was already site-less, else the oldest. The subnets
that named the others name it instead, and the others are deleted with their
names and descriptions -- the same loss the sites themselves take (P7: there
is no data to keep). Where no number repeats, nothing is touched.

Existing rows take the internal client, whose id is the organisation's: C
from `org_id`, H from the parent the row already names (0231's backfills, the
common block's).

DOWNGRADE recreates `ipam_site` and the three old columns exactly as 0043
and 0050 created them -- the earlier migrations' own downgrades drop them by
name -- and loses the new `site_id` values (CI's round trip only). It also
fails if two clients of one organisation now share a VRF name or a site-less
VLAN number, as 0231's own key restores do: downgrade is CI's round trip,
never production's rollback (restore-from-backup, CLAUDE.md). 0043 granted
`ipam_site` nothing by name; `app_user`'s privileges come from the
deployment's default privileges, as for every table.

Revision ID: 0240
Revises: 0239
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0240"
down_revision: str | None = "0239"
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

_ORG_GUC = "current_setting('app.current_org', true)::uuid"

# Each VLAN, and the VLAN of its organisation and number that survives the
# fold (module docstring): the site-less one first, then the oldest.
_VLAN_SURVIVOR = (
    "SELECT v.id AS dup, first_value(v.id) OVER ("
    "PARTITION BY v.org_id, v.vid ORDER BY (v.site_id IS NOT NULL), v.created_at, v.id"
    ") AS keep FROM ipam_vlan v"
)


def upgrade() -> None:
    # A VLAN that loses its site may collide with another of its number: fold
    # them while the site still says which one was site-less.
    op.execute(
        f"UPDATE ipam_subnet s SET vlan_id = m.keep FROM ({_VLAN_SURVIVOR}) m "
        "WHERE s.vlan_id = m.dup AND m.dup <> m.keep"
    )
    op.execute(
        f"DELETE FROM ipam_vlan v USING ({_VLAN_SURVIVOR}) m "
        "WHERE v.id = m.dup AND m.dup <> m.keep"
    )

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
        "ALTER TABLE ipam_vrf ADD CONSTRAINT uq_ipam_vrf_client_name "
        "UNIQUE (org_id, client_id, name)"
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
    # tenant policy (0043's loop over `_TABLES`).
    op.execute(
        """
        CREATE TABLE ipam_site (
            id uuid NOT NULL,
            org_id uuid NOT NULL,
            name varchar(200) NOT NULL,
            doc_record_id uuid,
            description text DEFAULT '' NOT NULL,
            created_at timestamp with time zone NOT NULL,
            updated_at timestamp with time zone NOT NULL,
            CONSTRAINT ipam_site_pkey PRIMARY KEY (id),
            CONSTRAINT ipam_site_org_id_fkey FOREIGN KEY (org_id) REFERENCES organizations(id),
            CONSTRAINT ipam_site_doc_record_id_fkey
                FOREIGN KEY (doc_record_id) REFERENCES doc_records(id),
            CONSTRAINT uq_ipam_site_org_name UNIQUE (org_id, name)
        )
        """
    )
    op.execute("ALTER TABLE ipam_site ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ipam_site FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON ipam_site "
        f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
    )
    op.execute("ALTER TABLE ipam_vlan ADD COLUMN site_id uuid REFERENCES ipam_site(id)")
    op.execute("ALTER TABLE ipam_subnet ADD COLUMN site_id uuid REFERENCES ipam_site(id)")
    op.execute("ALTER TABLE site_server ADD COLUMN ipam_site_id uuid REFERENCES ipam_site(id)")
    op.execute("CREATE INDEX ix_ipam_subnet_org_site ON ipam_subnet (org_id, site_id)")
    op.execute(
        "CREATE UNIQUE INDEX uq_ipam_vlan_org_site_vid ON ipam_vlan (org_id, site_id, vid)"
        " WHERE site_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_ipam_vlan_org_vid_global ON ipam_vlan (org_id, vid)"
        " WHERE site_id IS NULL"
    )
