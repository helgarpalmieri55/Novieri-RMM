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

TWO GROUP-SCOPED GRANTS OF THE SAME ROLE TO THE SAME USER COLLAPSE. Until 0231
gives groups a client, every group-scoped grant maps to the internal client, so
a user holding one role on two groups would hold two identical
`(user_id, role_id, client_id)` rows and the new UNIQUE would refuse to build.
The earliest of each such set is kept and the rest deleted; they were already
the same grant on the same client, so no access changes. Org-wide grants are
untouched (their `client_id` stays NULL).

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
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
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
    # One statement per call: the asyncpg driver prepares each `op.execute`,
    # and a prepared statement cannot hold several commands.
    op.execute(
        "INSERT INTO clients (id, org_id, name, kind) "
        "SELECT id, id, name, 'internal' FROM organizations"
    )
    op.execute(
        "INSERT INTO clients (id, org_id, name, kind) "
        "SELECT gen_random_uuid(), id, 'Sin asignar', 'unassigned' FROM organizations"
    )
    op.execute(
        "INSERT INTO sites (id, org_id, client_id, name) "
        "SELECT id, id, id, 'Principal' FROM organizations"
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
    # See the module docstring: several groups now share one client, so the
    # grants that differed only by group are one grant. Keep the earliest.
    op.execute(
        """
        DELETE FROM user_roles AS ur
        USING (
            SELECT id, row_number() OVER (
                PARTITION BY user_id, role_id, client_id ORDER BY id
            ) AS n
            FROM user_roles
            WHERE client_id IS NOT NULL
        ) AS ranked
        WHERE ur.id = ranked.id AND ranked.n > 1
        """
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
    # A client-scoped grant has no group to go back to; keeping it would turn
    # it into an org-wide one, so a downgrade drops it rather than widen access.
    op.execute("DELETE FROM user_roles WHERE client_id IS NOT NULL")
    op.execute(
        "ALTER TABLE user_roles ADD COLUMN device_group_id uuid REFERENCES device_groups(id)"
    )
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
