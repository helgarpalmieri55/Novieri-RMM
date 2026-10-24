"""Client scope at the database (MSP clients spec §2.2-2.4, plan Task 6).

1. `its_client_visible(client)`: the scope predicate over the two GUCs that
   `org_session` pins. Unset GUCs make it NULL -- no row, no write. A NULL
   client (O tables: "every client") is readable only under a scope that
   names a client -- `all`, or a NON-EMPTY `list`: a session with no scope,
   or with the empty intersection a refused `X-Client-Id` yields, sees ZERO
   rows, global ones included (spec §2.2; plan Global Constraints).
2. `client_id` on every phase-1 table (C, H, O), backfilled for existing rows
   (C: the organization's internal client, whose id IS the org id; H: from
   the parent), then NOT NULL for C and H.
3. `its_client_from_parent`: H tables and O tables with a parent take the
   parent's client; a different explicit value raises. SECURITY INVOKER, so a
   parent the session cannot see yields NULL and NOT NULL refuses the write.
   It fires on the fk AND on `client_id` itself, so an UPDATE that rewrites
   `client_id` alone meets the same check as an INSERT does (spec §2.4). A
   `client_id` merely carried over from the old row is not a claim: a row
   moved to another parent takes the new parent's client.
4. `its_client_to_children`, the other direction of every edge: when a
   parent's client changes (an article moved, a job retargeted, an alert's
   device corrected), an AFTER UPDATE trigger on the parent moves its
   children in the same statement. Without it they keep the old client and
   stay readable where the parent no longer is (spec §2.4).
5. A RESTRICTIVE `client_isolation` policy on each, ANDed with the existing
   permissive `tenant_isolation`, which is not touched. On O tables a NULL
   row is global configuration: readable by a limited scope, but changed or
   deleted only under scope all (spec §2.2) -- hence `client_isolation_update`
   and `client_isolation_delete`, because a DELETE checks no WITH CHECK and a
   FOR ALL policy's USING has to admit the NULL rows a limited scope reads.
   `users` is held like every other O table -- a staff row is created,
   changed and deleted only under scope all -- and names the creation in a
   `client_isolation_insert` of its own (`_policies`).

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
SCOPE = "current_setting('app.client_scope', true)"
# A scope that names at least one client: `all`, or a non-empty `list`. With
# no scope GUC every comparison is NULL; an empty list is the intersection a
# refused `X-Client-Id` yields, and it sees nothing either (spec §2.2).
SCOPE_PINNED = (
    f"({SCOPE} = 'all' OR ({SCOPE} = 'list' AND "
    "nullif(current_setting('app.current_clients', true), '') IS NOT NULL))"
)
# A NULL client is "every client" (O tables): readable under a pinned scope.
NULL_PINNED = f"(client_id IS NULL AND {SCOPE_PINNED})"
# Writing, changing or deleting a global row is a change to every client's
# configuration: scope all.
NULL_ALL = f"(client_id IS NULL AND {SCOPE} = 'all')"

# O tables whose NULL rows any pinned scope may INSERT (see `_policies`).
# `users` was here until Task 8; no narrowed path writes a staff row.
NULL_WRITABLE_FROM_ANY_SCOPE = {"domain_events"}


def _create_policy(
    table: str, name: str, cmd: str, using: str | None, check: str | None
) -> None:
    using_ = f" USING ({using})" if using is not None else ""  # INSERT has none
    with_check = f" WITH CHECK ({check})" if check is not None else ""
    op.execute(f"CREATE POLICY {name} ON {table} AS RESTRICTIVE FOR {cmd}{using_}{with_check}")


def _policies(table: str, kind: str) -> None:
    if kind in ("C", "H"):
        _create_policy(table, "client_isolation", "ALL", VISIBLE, VISIBLE)
        return
    read = f"{NULL_PINNED} OR {VISIBLE}"
    modify = f"{NULL_ALL} OR {VISIBLE}"
    if table in NULL_WRITABLE_FROM_ANY_SCOPE:
        # The one O table whose NULL rows any PINNED scope may WRITE; with no
        # scope, still nothing. Changing or deleting one is still scope all
        # (the policies below).
        #
        # `domain_events`: `app/core/events.publish` cannot yet name the
        # client, so in phase 1 an event is written with NULL client from any
        # scope -- under the generic check every route that publishes would
        # fail for a narrowed technician. Phase 2 makes publish set client_id
        # from the event's subject and returns `domain_events` to the generic
        # O check.
        write = read
    else:
        write = modify
    _create_policy(table, "client_isolation", "ALL", read, write)
    # The FOR ALL policy's USING admits the global rows a limited scope may
    # READ, and a DELETE checks USING only -- so changing or deleting a global
    # row needs these too.
    #
    # `users` included (Task 8): a staff row is a global row, and no path a
    # narrowed scope reaches changes one -- `PATCH /me` and the login
    # bookkeeping (`app/api/deps.py::admit`) run under scope all since Task
    # 6b, and user administration (`users.manage`) requires every client at
    # the API (plan Task 4). So a narrowed scope may not UPDATE any column of
    # a staff row; a contact row (client_id set) needs its client visible.
    _create_policy(table, "client_isolation_update", "UPDATE", modify, None)
    _create_policy(table, "client_isolation_delete", "DELETE", modify, None)
    if table == "users":
        # A staff row is a global row: it is CREATED only under scope all --
        # first-login provisioning (`app/api/deps.py::admit`) and `POST
        # /users`, both every-client. `client_isolation`'s WITH CHECK says the
        # same since `users` left `NULL_WRITABLE_FROM_ANY_SCOPE`; this one
        # names the rule for the table where it matters most, so it does not
        # hang on that set's membership.
        _create_policy(table, "client_isolation_insert", "INSERT", None, modify)


# (parent, child, fk): every inheritance edge above. A parent's change of
# client reaches its children through `its_client_to_children` (spec §2.4).
EDGES: list[tuple[str, str, str]] = (
    [(parent, table, fk) for table, parent, fk in H_TABLES]
    + [(p, table, f) for table, p, f in O_TABLES if p is not None and f is not None]
    + [(parent, table, fk) for table, _, parent, fk in SECOND_PARENTS]
)


def _to_children(parent: str, child: str, fk: str) -> str:
    twice = sum(1 for p, c, _ in EDGES if (p, c) == (parent, child)) > 1
    return f"its_client_to_{child}" + (f"_{fk}" if twice else "")


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
        DECLARE parent_id uuid; parent_client uuid; claimed boolean;
        BEGIN
            EXECUTE format('SELECT ($1).%I', TG_ARGV[1]) USING NEW INTO parent_id;
            IF parent_id IS NULL THEN
                RETURN NEW;
            END IF;
            EXECUTE format('SELECT client_id FROM %I WHERE id = $1', TG_ARGV[0])
                USING parent_id INTO parent_client;
            -- A client the statement supplies: any on INSERT, a CHANGED one on
            -- UPDATE. One carried over from the old row claims nothing.
            IF TG_OP = 'INSERT' THEN
                claimed := NEW.client_id IS NOT NULL;
            ELSE
                claimed := NEW.client_id IS NOT NULL
                    AND NEW.client_id IS DISTINCT FROM OLD.client_id;
            END IF;
            IF claimed AND parent_client IS NOT NULL
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
        DECLARE target_client uuid; claimed boolean;
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
            -- As in its_client_from_parent: a carried-over client claims nothing.
            IF TG_OP = 'INSERT' THEN
                claimed := NEW.client_id IS NOT NULL;
            ELSE
                claimed := NEW.client_id IS NOT NULL
                    AND NEW.client_id IS DISTINCT FROM OLD.client_id;
            END IF;
            IF claimed AND target_client IS NOT NULL
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

    # SECURITY INVOKER, like the trigger it feeds: it moves the children the
    # session can see. Every child of a parent shares the parent's old client,
    # which the session could see -- it just changed that parent.
    op.execute(
        """
        CREATE FUNCTION its_client_to_children() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            EXECUTE format('UPDATE %I SET client_id = $1 WHERE %I = $2', TG_ARGV[0], TG_ARGV[1])
                USING NEW.client_id, NEW.id;
            RETURN NULL;
        END $$
        """
    )
    # What a `users` row IS -- staff or contact (and with it, through
    # `ck_users_kind_client`, its client), the identity it logs in as, and
    # whether it may log in at all (`status`, the admission gate, spec §3) --
    # changes only under scope all: the auth door and the every-client user
    # administration (`users.manage`: disable, enable, delete). A narrowed
    # scope reaches no staff row at all (`client_isolation_update`, Task 8),
    # so what this holds there is a CONTACT of a client in the scope, whose
    # other columns that scope may write. A role that bypasses RLS (the
    # admin session) is not held to a client scope here either.
    op.execute(
        """
        CREATE FUNCTION its_user_identity_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF (NEW.kind IS DISTINCT FROM OLD.kind
                OR NEW.external_subject IS DISTINCT FROM OLD.external_subject
                OR NEW.status IS DISTINCT FROM OLD.status)
               AND current_setting('app.client_scope', true) IS DISTINCT FROM 'all'
               AND NOT (SELECT rolbypassrls OR rolsuper FROM pg_roles
                        WHERE rolname = current_user) THEN
                RAISE EXCEPTION
                    'users.kind, users.external_subject and users.status change only '
                    'under the every-client scope'
                    USING ERRCODE = 'check_violation';
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
    op.execute("CREATE INDEX ix_enrollment_tokens_site_id ON enrollment_tokens (site_id)")
    # The target of every site-consistency foreign key (module docstring).
    op.execute("ALTER TABLE sites ADD CONSTRAINT uq_sites_id_client UNIQUE (id, client_id)")
    for table in SITE_TABLES:
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_site_client "
            "FOREIGN KEY (site_id, client_id) REFERENCES sites (id, client_id)"
        )
    op.execute("ALTER TABLE install_links DROP CONSTRAINT uq_install_links_org")
    op.execute("ALTER TABLE install_links ADD CONSTRAINT uq_install_links_site UNIQUE (site_id)")
    # A group organises ONE client's devices (spec §1.5), so its live name is
    # unique within that client, not across the organisation. Per organisation
    # (0072), a technician limited to client X could not see that client Y
    # already uses a name -- the application's check runs under their scope --
    # and the INSERT died on this index: a 500 where an every-client admin got
    # a 409, which told the technician Y's group names one guess at a time.
    op.execute("DROP INDEX uq_device_groups_org_live_name")
    op.execute(
        "CREATE UNIQUE INDEX uq_device_groups_client_live_name "
        "ON device_groups (org_id, client_id, name) WHERE archived_at IS NULL"
    )
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

    # AFTER UPDATE with no column list: a parent's client often changes inside
    # its own BEFORE trigger (a job retargeted, an alert's device corrected),
    # and `UPDATE OF client_id` fires only when the statement names it.
    for parent, child, fk in EDGES:
        op.execute(
            f"CREATE TRIGGER {_to_children(parent, child, fk)} AFTER UPDATE ON {parent} "
            "FOR EACH ROW WHEN (OLD.client_id IS DISTINCT FROM NEW.client_id) "
            f"EXECUTE FUNCTION its_client_to_children('{child}', '{fk}')"
        )

    op.execute(
        "CREATE TRIGGER its_user_identity_guard BEFORE UPDATE OF kind, external_subject, "
        "status ON users FOR EACH ROW EXECUTE FUNCTION its_user_identity_guard()"
    )

    for table in CLIENT_ORG_FK_TABLES:
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_client_org "
            "FOREIGN KEY (client_id, org_id) REFERENCES clients (id, org_id)"
        )

    for table in C_TABLES + ["jobs"] + [t for t, _, _ in H_TABLES]:
        _policies(table, "C")
    for table, _, _ in O_TABLES:
        _policies(table, "O")

    # `clients` itself: a list scope sees its own clients; never `unassigned`
    # unless the scope is all (spec §1.1).
    op.execute(
        "CREATE POLICY client_isolation ON clients AS RESTRICTIVE "
        "USING (its_client_visible(id) AND (kind <> 'unassigned' "
        "OR current_setting('app.client_scope', true) = 'all')) "
        "WITH CHECK (current_setting('app.client_scope', true) = 'all' OR its_client_visible(id))"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS its_user_identity_guard ON users")
    for parent, child, fk in EDGES:
        op.execute(f"DROP TRIGGER IF EXISTS {_to_children(parent, child, fk)} ON {parent}")
    op.execute("DROP POLICY IF EXISTS client_isolation ON clients")
    for table, trigger, _, _ in SECOND_PARENTS:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    for table in CLIENT_ORG_FK_TABLES:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS fk_{table}_client_org")
    op.execute("ALTER TABLE clients DROP CONSTRAINT uq_clients_id_org")
    for table, parent, _ in O_TABLES:
        op.execute(f"DROP POLICY IF EXISTS client_isolation ON {table}")
        op.execute(f"DROP POLICY IF EXISTS client_isolation_update ON {table}")
        op.execute(f"DROP POLICY IF EXISTS client_isolation_delete ON {table}")
        op.execute(f"DROP POLICY IF EXISTS client_isolation_insert ON {table}")
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
    op.execute("DROP INDEX IF EXISTS ix_enrollment_tokens_site_id")
    op.execute("ALTER TABLE install_links DROP CONSTRAINT uq_install_links_site")
    op.execute("ALTER TABLE install_links ADD CONSTRAINT uq_install_links_org UNIQUE (org_id)")
    # Fails if two clients of one organisation share a live group name, as
    # 0231's own `uq_install_links_org` restore fails with several links per
    # organisation: downgrade is CI's round trip, never production's rollback
    # (restore-from-backup, CLAUDE.md).
    op.execute("DROP INDEX uq_device_groups_client_live_name")
    op.execute(
        "CREATE UNIQUE INDEX uq_device_groups_org_live_name "
        "ON device_groups (org_id, name) WHERE archived_at IS NULL"
    )
    for table in SITE_TABLES:
        op.execute(f"ALTER TABLE {table} DROP COLUMN site_id")
    for table in C_TABLES:
        op.execute(f"DROP POLICY IF EXISTS client_isolation ON {table}")
    for table in C_TABLES[1:]:
        op.execute(f"ALTER TABLE {table} DROP COLUMN client_id")
    for fn in (
        "its_user_identity_guard()",
        "its_client_to_children()",
        "its_group_client_guard()",
        "its_device_client_guard()",
        "its_job_client()",
        "its_client_from_parent()",
        "its_client_visible(uuid)",
    ):
        op.execute(f"DROP FUNCTION {fn}")
