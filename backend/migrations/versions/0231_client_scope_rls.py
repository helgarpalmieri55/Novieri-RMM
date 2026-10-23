"""Client scope at the database (MSP clients spec §2.2-2.4, plan Task 6).

1. `its_client_visible(client)`: the scope predicate over the two GUCs that
   `org_session` pins. Unset GUCs make it NULL -- no row, no write. A NULL
   client (O tables: "every client") is readable only under a pinned scope,
   `all` or `list`: a session with no scope sees ZERO rows, global ones
   included (spec §2.2, plan Global Constraints: RLS fails closed).
2. `client_id` on every phase-1 table (C, H, O), backfilled for existing rows
   (C: the organization's internal client, whose id IS the org id; H: from
   the parent), then NOT NULL for C and H.
3. `its_client_from_parent`: H tables and O tables with a parent take the
   parent's client; a different explicit value raises. SECURITY INVOKER, so a
   parent the session cannot see yields NULL and NOT NULL refuses the write.
   It fires on the fk AND on `client_id` itself, so an UPDATE that rewrites
   `client_id` alone meets the same check as an INSERT does (spec §2.4).
4. A RESTRICTIVE `client_isolation` policy on each, ANDed with the existing
   permissive `tenant_isolation`, which is not touched.

ORG CONSISTENCY IS A FOREIGN KEY, NOT A POLICY. A foreign key check ignores
RLS: under scope `all` the client policy passes any client id and the tenant
policy looks only at `org_id`, so a plain `REFERENCES clients(id)` would let
org A's row name org B's client. Every table where the APPLICATION writes
`client_id` therefore also gets `(client_id, org_id) REFERENCES clients (id,
org_id)` (`fk_<table>_client_org`, over `uq_clients_id_org`): `users` and
`user_roles` (columns from 0230), the C tables but `sites` (0230's
`its_site_client_check` already holds it there), and every O table. H tables
and `jobs` need none: their trigger copies the client of a parent that tenant
RLS already confines to the org.

SITE CONSISTENCY IS A FOREIGN KEY TOO. `devices`, `enrollment_tokens` and
`install_links` name a site AND a client, and `(site_id, client_id) REFERENCES
sites (id, client_id)` (`fk_<table>_site_client`, over `uq_sites_id_client`)
makes the site one of that client's. It also pins the site: a site that holds
a device cannot change client, because the device's key would dangle (0230's
`its_site_client_check` allows the UPDATE on its own). The devices guard
trigger stays for its clearer message.

TWO TABLES HAVE TWO PARENTS. `doc_versions` hangs from an article XOR a record
(`doc_versions_one_parent`), `health_profile_assignment` from a device or a
group. Each gets a second trigger for its second parent -- otherwise those
rows carry NULL, which an O table reads as "every client".

The table list is a LITERAL copy of `app/core/client_scope_catalog.py`'s
phase-1 rows: migrations never import application code, and
`tests/test_client_catalog.py` proves the two agree through `pg_policies` and
through each trigger's arguments.

Revision ID: 0231
Revises: 0230
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0231"
down_revision: str | None = "0230"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

C_TABLES = ["sites", "device_groups", "devices", "enrollment_tokens", "install_links"]

H_TABLES: list[tuple[str, str, str]] = [  # (table, parent, fk)
    ("suggested_asset_match", "devices", "device_id"),
    ("device_tags", "devices", "device_id"),
    ("agents", "devices", "device_id"),
    ("job_logs", "jobs", "job_id"),
    ("ai_action", "devices", "target_device_id"),
    ("inventory_snapshots", "devices", "device_id"),
    ("installed_software", "devices", "device_id"),
    ("inventory_changes", "devices", "device_id"),
    ("hardware_components", "devices", "device_id"),
    ("security_events", "devices", "device_id"),
    ("push_batch_target", "jobs", "job_id"),
    ("agent_update_targets", "devices", "device_id"),
    ("backup_runs", "devices", "device_id"),
    ("backup_points", "devices", "device_id"),
    ("restore_jobs", "devices", "device_id"),
    ("backup_point_copies", "backup_points", "point_id"),
    ("deployment_results", "devices", "device_id"),
    ("script_runs", "devices", "device_id"),
    ("script_policies", "device_groups", "device_group_id"),
    ("health_alert", "devices", "device_id"),
    ("device_health_check_state", "devices", "device_id"),
    ("health_flap_state", "devices", "device_id"),
    ("device_health_policy_ack", "devices", "device_id"),
    ("health_alert_action", "health_alert", "alert_id"),
    ("device_patch_status", "devices", "device_id"),
    ("patch_reboots", "devices", "device_id"),
    ("patch_runs", "devices", "device_id"),
    ("device_person", "devices", "device_id"),
    ("remote_sessions", "devices", "device_id"),
    ("site_server", "devices", "device_id"),
]

O_TABLES: list[tuple[str, str | None, str | None]] = [  # (table, parent, fk)
    ("users", None, None),
    ("domain_events", None, None),
    ("policies", "device_groups", "device_group_id"),
    ("credentials", "device_groups", "device_group_id"),
    ("doc_articles", None, None),
    ("doc_records", None, None),
    ("doc_documents", None, None),
    ("doc_links", "doc_articles", "article_id"),
    ("doc_versions", "doc_articles", "article_id"),
    ("doc_document_versions", "doc_documents", "document_id"),
    ("doc_document_links", "doc_documents", "document_id"),
    ("push_host_key", "devices", "device_id"),
    ("agent_update_rings", "device_groups", "device_group_id"),
    ("backup_policies", "device_groups", "device_group_id"),
    ("backup_offsite_targets", None, None),
    ("backup_recovery_keys", None, None),
    ("change_gates", "device_groups", "device_group_id"),
    ("inbound_message", None, None),
    ("health_profile_assignment", "devices", "device_id"),
    ("software_ignore", None, None),
    ("patch_policies", "device_groups", "device_group_id"),
    ("patch_approvals", None, None),
    ("provision_run", "devices", "device_id"),
    ("remote_policies", "device_groups", "device_group_id"),
    ("sla_policy", None, None),
]

# The second parent of the O tables that have two (see the module docstring):
# (table, trigger, parent, fk).
SECOND_PARENTS: list[tuple[str, str, str, str]] = [
    ("doc_versions", "its_client_from_record", "doc_records", "record_id"),
    ("health_profile_assignment", "its_client_from_group", "device_groups", "device_group_id"),
]

# Tables whose `client_id` the application writes, and that therefore carry
# the org-consistency foreign key (module docstring). `user_roles` is G -- no
# client policy -- but its `client_id` is a grant's scope, written by the API.
CLIENT_ORG_FK_TABLES = ["users", "user_roles"] + C_TABLES[1:] + [
    t for t, _, _ in O_TABLES if t != "users"
]

# Tables that name a site, and so carry the site-consistency foreign key.
SITE_TABLES = ["devices", "enrollment_tokens", "install_links"]

VISIBLE = "its_client_visible(client_id)"
# A NULL client is "every client" (O tables). It is readable -- and, where a
# table allows it, writable -- only under a PINNED scope: with no scope GUC
# `IN` is NULL, so a scopeless session sees no global row either.
NULL_PINNED = "(client_id IS NULL AND current_setting('app.client_scope', true) IN ('all', 'list'))"
# Writing a global row is a change to every client's configuration: scope all.
NULL_ALL = "(client_id IS NULL AND current_setting('app.client_scope', true) = 'all')"

# O tables whose NULL rows any pinned scope may WRITE (see `_policy`).
NULL_WRITABLE_FROM_ANY_SCOPE = {"users", "domain_events"}


def _policy(table: str, kind: str) -> None:
    if table in NULL_WRITABLE_FROM_ANY_SCOPE:
        # Two O tables whose NULL rows any PINNED scope may write; with no
        # scope, still nothing.
        #
        # `users`: a staff row (client_id NULL) is written under NARROWED
        # scopes -- `PATCH /me` and the login bookkeeping run with the
        # person's own scope -- so the generic O check, which reserves NULL
        # rows to scope `all`, would refuse a technician's own profile. Admin
        # writes to staff rows already require a scope of every client at the
        # API (`users.manage`, plan Task 4). A contact row (client_id set)
        # needs its client to be visible.
        #
        # `domain_events`: `app/core/events.publish` cannot yet name the
        # client, so in phase 1 an event is written with NULL client from any
        # scope -- under the generic check every route that publishes would
        # fail for a narrowed technician. Phase 2 makes publish set client_id
        # from the event's subject and returns `domain_events` to the generic
        # O check.
        using = check = f"{NULL_PINNED} OR {VISIBLE}"
    elif kind == "O":
        using = f"{NULL_PINNED} OR {VISIBLE}"
        check = f"{NULL_ALL} OR {VISIBLE}"
    else:
        using = check = VISIBLE
    op.execute(
        f"CREATE POLICY client_isolation ON {table} AS RESTRICTIVE "
        f"USING ({using}) WITH CHECK ({check})"
    )


def _inherit(table: str, trigger: str, parent: str, fk: str) -> None:
    op.execute(
        f"CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE OF {fk}, client_id ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION its_client_from_parent('{parent}', '{fk}')"
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION its_client_visible(c uuid) RETURNS boolean
        LANGUAGE sql STABLE AS $$
            SELECT current_setting('app.client_scope', true) = 'all'
                OR c = ANY (string_to_array(
                       nullif(current_setting('app.current_clients', true), ''), ','
                   )::uuid[])
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION its_client_from_parent() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE parent_id uuid; parent_client uuid;
        BEGIN
            EXECUTE format('SELECT ($1).%I', TG_ARGV[1]) USING NEW INTO parent_id;
            IF parent_id IS NULL THEN
                RETURN NEW;
            END IF;
            EXECUTE format('SELECT client_id FROM %I WHERE id = $1', TG_ARGV[0])
                USING parent_id INTO parent_client;
            IF NEW.client_id IS NOT NULL AND parent_client IS NOT NULL
               AND NEW.client_id <> parent_client THEN
                RAISE EXCEPTION '%.client_id % differs from its %''s client %',
                    TG_TABLE_NAME, NEW.client_id, TG_ARGV[0], parent_client
                    USING ERRCODE = 'check_violation';
            END IF;
            NEW.client_id := parent_client;
            RETURN NEW;
        END $$
        """
    )
    # `device` is the only target the job engine writes (`JobEngine.create`);
    # `group` is the model's other documented value. Anything else is refused
    # by name rather than looked up in `devices`, where it would find nothing
    # and fail on NOT NULL with a message about the wrong thing.
    op.execute(
        """
        CREATE FUNCTION its_job_client() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE target_client uuid;
        BEGIN
            IF NEW.target_type = 'device' THEN
                SELECT client_id INTO target_client FROM devices WHERE id = NEW.target_id;
            ELSIF NEW.target_type = 'group' THEN
                SELECT client_id INTO target_client FROM device_groups WHERE id = NEW.target_id;
            ELSE
                RAISE EXCEPTION 'jobs.target_type % has no client: only device or group',
                    NEW.target_type
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.client_id IS NOT NULL AND target_client IS NOT NULL
               AND NEW.client_id <> target_client THEN
                RAISE EXCEPTION 'jobs.client_id differs from its target''s client'
                    USING ERRCODE = 'check_violation';
            END IF;
            NEW.client_id := target_client;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION its_device_client_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE s_client uuid; g_client uuid;
        BEGIN
            IF TG_OP = 'UPDATE' AND NEW.client_id <> OLD.client_id THEN
                RAISE EXCEPTION 'a device cannot move to another client (spec §1.5)'
                    USING ERRCODE = 'check_violation';
            END IF;
            SELECT client_id INTO s_client FROM sites WHERE id = NEW.site_id;
            IF s_client IS DISTINCT FROM NEW.client_id THEN
                RAISE EXCEPTION 'device site belongs to another client'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.device_group_id IS NOT NULL THEN
                SELECT client_id INTO g_client FROM device_groups WHERE id = NEW.device_group_id;
                IF g_client IS DISTINCT FROM NEW.client_id THEN
                    RAISE EXCEPTION 'device group belongs to another client'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION its_group_client_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE p_client uuid;
        BEGIN
            IF TG_OP = 'UPDATE' AND NEW.client_id <> OLD.client_id THEN
                RAISE EXCEPTION 'a device group cannot move to another client'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.parent_group_id IS NOT NULL THEN
                SELECT client_id INTO p_client FROM device_groups WHERE id = NEW.parent_group_id;
                IF p_client IS DISTINCT FROM NEW.client_id THEN
                    RAISE EXCEPTION 'parent group belongs to another client'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END $$
        """
    )

    # The target of every org-consistency foreign key below.
    op.execute("ALTER TABLE clients ADD CONSTRAINT uq_clients_id_org UNIQUE (id, org_id)")

    # C tables. `sites` already has client_id (0230).
    for table in C_TABLES[1:]:
        op.execute(f"ALTER TABLE {table} ADD COLUMN client_id uuid REFERENCES clients(id)")
        op.execute(f"UPDATE {table} SET client_id = org_id")  # the internal client
        op.execute(f"ALTER TABLE {table} ALTER COLUMN client_id SET NOT NULL")
        op.execute(f"CREATE INDEX ix_{table}_client_id ON {table} (client_id)")
    for table in SITE_TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN site_id uuid REFERENCES sites(id)")
        op.execute(f"UPDATE {table} SET site_id = org_id")  # the internal Principal site
        op.execute(f"ALTER TABLE {table} ALTER COLUMN site_id SET NOT NULL")
    op.execute("CREATE INDEX ix_devices_site_id ON devices (site_id)")
    # The target of every site-consistency foreign key (module docstring).
    op.execute("ALTER TABLE sites ADD CONSTRAINT uq_sites_id_client UNIQUE (id, client_id)")
    for table in SITE_TABLES:
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_site_client "
            "FOREIGN KEY (site_id, client_id) REFERENCES sites (id, client_id)"
        )
    op.execute("ALTER TABLE install_links DROP CONSTRAINT uq_install_links_org")
    op.execute("ALTER TABLE install_links ADD CONSTRAINT uq_install_links_site UNIQUE (site_id)")
    op.execute(
        "CREATE TRIGGER its_device_client_guard BEFORE INSERT OR UPDATE ON devices "
        "FOR EACH ROW EXECUTE FUNCTION its_device_client_guard()"
    )
    op.execute(
        "CREATE TRIGGER its_group_client_guard BEFORE INSERT OR UPDATE ON device_groups "
        "FOR EACH ROW EXECUTE FUNCTION its_group_client_guard()"
    )
    # A group-scoped grant migrated by 0230 to the internal client keeps it:
    # every existing group is the internal client's now.

    # jobs (H, special)
    op.execute("ALTER TABLE jobs ADD COLUMN client_id uuid REFERENCES clients(id)")
    op.execute(
        "UPDATE jobs j SET client_id = COALESCE("
        "(SELECT client_id FROM devices WHERE id = j.target_id),"
        "(SELECT client_id FROM device_groups WHERE id = j.target_id), j.org_id)"
    )
    op.execute("ALTER TABLE jobs ALTER COLUMN client_id SET NOT NULL")
    op.execute("CREATE INDEX ix_jobs_client_id ON jobs (client_id)")
    op.execute(
        "CREATE TRIGGER its_job_client BEFORE INSERT OR UPDATE OF target_id, target_type, "
        "client_id ON jobs FOR EACH ROW EXECUTE FUNCTION its_job_client()"
    )

    for table, parent, fk in H_TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN client_id uuid REFERENCES clients(id)")
        op.execute(
            f"UPDATE {table} t SET client_id = p.client_id FROM {parent} p WHERE p.id = t.{fk}"
        )
        op.execute(f"ALTER TABLE {table} ALTER COLUMN client_id SET NOT NULL")
        op.execute(f"CREATE INDEX ix_{table}_client_id ON {table} (client_id)")
        _inherit(table, "its_client_from_parent", parent, fk)

    for table, parent, fk in O_TABLES:
        if table != "users":  # users.client_id exists since 0230
            op.execute(f"ALTER TABLE {table} ADD COLUMN client_id uuid REFERENCES clients(id)")
        if parent is not None and fk is not None:
            op.execute(
                f"UPDATE {table} t SET client_id = p.client_id FROM {parent} p "
                f"WHERE p.id = t.{fk}"
            )
            _inherit(table, "its_client_from_parent", parent, fk)
        op.execute(f"CREATE INDEX ix_{table}_client_id ON {table} (client_id)")

    for table, trigger, parent, fk in SECOND_PARENTS:
        op.execute(
            f"UPDATE {table} t SET client_id = p.client_id FROM {parent} p "
            f"WHERE p.id = t.{fk}"
        )
        _inherit(table, trigger, parent, fk)

    for table in CLIENT_ORG_FK_TABLES:
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_client_org "
            "FOREIGN KEY (client_id, org_id) REFERENCES clients (id, org_id)"
        )

    for table in C_TABLES + ["jobs"] + [t for t, _, _ in H_TABLES]:
        _policy(table, "C")
    for table, _, _ in O_TABLES:
        _policy(table, "O")

    # `clients` itself: a list scope sees its own clients; never `unassigned`
    # unless the scope is all (spec §1.1).
    op.execute(
        "CREATE POLICY client_isolation ON clients AS RESTRICTIVE "
        "USING (its_client_visible(id) AND (kind <> 'unassigned' "
        "OR current_setting('app.client_scope', true) = 'all')) "
        "WITH CHECK (current_setting('app.client_scope', true) = 'all' OR its_client_visible(id))"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS client_isolation ON clients")
    for table, trigger, _, _ in SECOND_PARENTS:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    for table in CLIENT_ORG_FK_TABLES:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS fk_{table}_client_org")
    op.execute("ALTER TABLE clients DROP CONSTRAINT uq_clients_id_org")
    for table, parent, _ in O_TABLES:
        op.execute(f"DROP POLICY IF EXISTS client_isolation ON {table}")
        if parent is not None:
            op.execute(f"DROP TRIGGER IF EXISTS its_client_from_parent ON {table}")
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_client_id")
        if table != "users":
            op.execute(f"ALTER TABLE {table} DROP COLUMN client_id")
    for table, _, _ in H_TABLES:
        op.execute(f"DROP POLICY IF EXISTS client_isolation ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS its_client_from_parent ON {table}")
        op.execute(f"ALTER TABLE {table} DROP COLUMN client_id")
    op.execute("DROP POLICY IF EXISTS client_isolation ON jobs")
    op.execute("DROP TRIGGER IF EXISTS its_job_client ON jobs")
    op.execute("ALTER TABLE jobs DROP COLUMN client_id")
    op.execute("DROP TRIGGER IF EXISTS its_device_client_guard ON devices")
    op.execute("DROP TRIGGER IF EXISTS its_group_client_guard ON device_groups")
    for table in SITE_TABLES:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS fk_{table}_site_client")
    op.execute("ALTER TABLE sites DROP CONSTRAINT uq_sites_id_client")
    op.execute("DROP INDEX IF EXISTS ix_devices_site_id")
    op.execute("ALTER TABLE install_links DROP CONSTRAINT uq_install_links_site")
    op.execute("ALTER TABLE install_links ADD CONSTRAINT uq_install_links_org UNIQUE (org_id)")
    for table in SITE_TABLES:
        op.execute(f"ALTER TABLE {table} DROP COLUMN site_id")
    for table in C_TABLES:
        op.execute(f"DROP POLICY IF EXISTS client_isolation ON {table}")
    for table in C_TABLES[1:]:
        op.execute(f"ALTER TABLE {table} DROP COLUMN client_id")
    for fn in (
        "its_group_client_guard()",
        "its_device_client_guard()",
        "its_job_client()",
        "its_client_from_parent()",
        "its_client_visible(uuid)",
    ):
        op.execute(f"DROP FUNCTION {fn}")
