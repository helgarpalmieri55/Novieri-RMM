"""agent distribution: `agent.release.manage` belongs to the operator, not to every tenant

WHAT WAS WRONG. `agent_releases` is a GLOBAL catalogue: it carries no `org_id`
(spec §4.2, and `tests/test_tenant_column_sweep.py` lists it under
`GLOBAL_BY_NATURE`), so one row is one artefact for every tenant on the
deployment, and no RLS policy sits underneath the routes that write it. 0060
seeded `agent.release.manage` to `org-admin`. 0060's OWN docstring says what
that code does -- it "writes the bytes every endpoint in the fleet will
execute ... the closest thing in this platform to arbitrary code execution on
every managed machine, gated only by whoever holds the code". Put those two
sentences beside each other and the defect is plain: on a multi-tenant
deployment, ANY customer's administrator could publish, replace or withdraw the
binary EVERY OTHER customer's endpoints run. That is a wider blast radius than
the cross-tenant read `platform.errors.*` had (0068): a read discloses
identifiers, this executes code on every managed machine on the platform.

`agent.release.read` IS DELIBERATELY LEFT WITH `org-admin`. 0060 is explicit
that read "is needed by anyone who installs an agent, because generating an
install command is a read of the catalogue plus a token mint", and the console
page that mints those tokens is gated on it. Revoking read would stop every
tenant onboarding a machine without the operator and would close nothing: the
bytes are published by the operator either way after this migration. The two
codes were split by 0060 precisely so they could move apart, and this is the
migration that moves one of them.

WHO GETS IT. `super-admin`, which 0003 describes as "Cross-org platform
administration (SaaS operator)" and which no org-scoped surface can grant,
clone, edit or assign (`app/core/rbac_admin.py`; 0065's `set_role_permissions`
refuses it by name). 0003's `ALL` was computed from 0003's own ten codes, so
`super-admin` never received any later code -- including these two -- and this
migration is the grant.

BOTH CODES GO TO `super-admin`, NOT JUST THE ONE THAT MOVED. A publisher who
cannot list the catalogue cannot see what they published, cannot find the id
the unpublish route takes, and cannot open the console page at all
(`frontend/src/shell/nav.ts` gates Agent Packages on `agent.release.read`).
Granting read to the operator takes nothing from anybody -- it is additive, and
`org-admin` keeps its own grant untouched.

WHAT A DEPLOYMENT THAT HAS ALREADY RUN 0060 LOSES, PLAINLY. Every current
holder of `org-admin` -- on the single-tenant on-prem install, that is the
operator's own everyday account -- stops being able to publish agent artefacts.
Concretely, after `alembic upgrade head`:

  * `POST /api/v1/agent/releases` (register a version),
    `POST /api/v1/agent/releases/{id}/artifact` (upload the bytes) and
    `DELETE /api/v1/agent/releases/{id}` (unpublish) answer
    `403 {"detail": "requires agent.release.manage"}`;
  * the Platform -> Agent Packages page KEEPS WORKING and the nav entry stays,
    because both are gated on `agent.release.read`, which does not move. Listing
    releases and generating an install command are unaffected;
  * nothing else changes -- no schema, no data, no enrollment behaviour.

To publish again, assign the global `super-admin` role to the operator's user.
That cannot be done from the console by design; it is a direct `user_roles`
insert, and it is deliberately the same act it was before this migration --
somebody with database access deciding who runs the platform. It is also worth
saying out loud that on a SINGLE-tenant deployment this migration protects
nothing that is not already the same person; its purpose is that the grant is
correct before a second tenant exists, not after.

REVOKED FROM EVERY ROLE, NOT FROM `org-admin` BY NAME. A deployment that has
run 0060 may have had an administrator clone `org-admin` into an org-local role
of the same name (0065's clone-on-write does exactly that on the first matrix
edit, keeping the name and setting `org_id`), or tick the box on a bespoke
role. Revoking only the built-in grant would fix this on a fresh database and
nowhere else.

AND THE MATRIX SURFACE IS CLOSED BEHIND IT. Without the function change below
this migration would be cosmetic: `org-admin` holds `roles.manage` and
`users.manage`, so the same administrator could create a role carrying
`agent.release.manage`, assign it to themselves, and publish -- two console
calls and no SQL. `app/core/rbac.py`'s `PLATFORM_OPERATOR_CODES` gains the
code, which is what keeps it out of the permission matrix
`/api/v1/permissions` serves and makes `rbac_admin._validate_codes` refuse it
on write; this replaces `set_role_permissions` so the rail also holds below the
service layer, for a caller who bypasses that module entirely. It is 0068's
rail with one more code in the array, not a second mechanism.

`agent.release.read` IS NOT IN THAT ARRAY, and that is the same distinction
0068 drew for `platform.status.read`: the rail is an explicit code list rather
than a prefix, because an org may legitimately grant read to a technician and
the console posts back every code a role already holds on an ordinary edit.

Revision ID: 0070
Revises: 0069
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0070"
down_revision: str | None = "0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The code this migration moves, and the one it deliberately does not.
_MOVED = "agent.release.manage"
_READ = "agent.release.read"

# Mirrors `app.core.rbac.PLATFORM_OPERATOR_CODES` in full -- the whole set, not
# this migration's addition to it, because the function below is REPLACED
# rather than patched and must refuse everything the set names. Two copies,
# because a migration must not import application code that will have moved on
# by the time it runs; `tests/test_rbac.py::
# test_the_migration_and_the_application_name_the_same_operator_codes` holds
# the two equal, and now points here rather than at 0068.
_OPERATOR_CODES = (
    "platform.errors.read",
    "platform.errors.write",
    "agent.release.manage",
)

_SQL_ARRAY = "ARRAY[" + ", ".join(f"'{c}'" for c in _OPERATOR_CODES) + "]"
_SQL_ARRAY_0068 = "ARRAY['platform.errors.read', 'platform.errors.write']"


def _function_sql(operator_array: str, provenance: str) -> str:
    """0065's function, plus 0068's operator rail, keyed on `operator_array`.

    Reproduced in full rather than patched because `CREATE OR REPLACE FUNCTION`
    has no other form: a replacement is the whole body or nothing. Built from
    one template so the upgrade and the downgrade cannot drift into being two
    different functions that happen to share a name -- the only difference
    between them is which codes the rail refuses.

    `provenance` is the one comment line that must differ with the array. The
    body is stored verbatim in `pg_proc.prosrc`, so a comment naming
    `agent.release.manage` inside the function a DOWNGRADED database is running
    would describe a rail that database does not have -- and the next person to
    read it out of Postgres would believe it.
    """
    return f"""
CREATE OR REPLACE FUNCTION set_role_permissions(p_role_id uuid, p_codes text[])
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_name text;
    v_org  uuid;
    v_missing text;
    v_operator text;
    v_count integer;
BEGIN
    SELECT name, org_id INTO v_name, v_org FROM roles WHERE id = p_role_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no such role: %', p_role_id USING ERRCODE = '23503';
    END IF;

    -- THE RAIL THAT MUST HOLD BELOW THE SERVICE LAYER. `super-admin` is
    -- cross-org platform administration; nothing reachable from an org-scoped
    -- console may reshape it, whatever the calling code believes.
    IF v_name = 'super-admin' THEN
        RAISE EXCEPTION 'super-admin is not editable' USING ERRCODE = '42501';
    END IF;

    -- A GLOBAL role (org_id IS NULL) is one row shared by every tenant, so
    -- editing it in place would reshape another org's `technician`. The
    -- service layer clones such a role into the caller's org first and passes
    -- the clone's id; being handed a global role here means that step was
    -- skipped, and silently editing it would be a cross-tenant write.
    IF v_org IS NULL THEN
        RAISE EXCEPTION 'global role % must be cloned before editing', v_name
            USING ERRCODE = '42501';
    END IF;

    -- {provenance}
    -- These codes act on a resource with no `org_id`, so whoever holds one
    -- acts on every tenant at once. Revoking them from `org-admin` while
    -- leaving this function able to write them onto any org-scoped role would
    -- be a revocation an org-admin holding `roles.manage` could undo from the
    -- console. Refused by CODE rather than by role name, because the
    -- escalation path is a brand-new role nobody has named yet.
    SELECT string_agg(c, ', ') INTO v_operator
    FROM unnest(p_codes) AS c
    WHERE c = ANY({operator_array});
    IF v_operator IS NOT NULL THEN
        RAISE EXCEPTION 'platform operator permission(s) are not grantable here: %',
            v_operator USING ERRCODE = '42501';
    END IF;

    -- An unknown code must be an error. `INSERT ... SELECT` would insert zero
    -- rows and report success, which reaches the operator as a permission that
    -- will not stick and no explanation anywhere.
    SELECT string_agg(c, ', ') INTO v_missing
    FROM unnest(p_codes) AS c
    WHERE NOT EXISTS (SELECT 1 FROM permissions p WHERE p.code = c);
    IF v_missing IS NOT NULL THEN
        RAISE EXCEPTION 'unknown permission code(s): %', v_missing
            USING ERRCODE = '23503';
    END IF;

    DELETE FROM role_permissions WHERE role_id = p_role_id;
    INSERT INTO role_permissions (role_id, permission_id)
    SELECT p_role_id, p.id FROM permissions p WHERE p.code = ANY(p_codes);
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$;
"""


_SET_ROLE_PERMISSIONS_WITH_OPERATOR_RAIL = _function_sql(
    _SQL_ARRAY,
    "ADDED IN 0068, WIDENED IN 0070: `platform.errors.*` reads every tenant's\n"
    "    -- error correlation ids; `agent.release.manage` publishes the bytes\n"
    "    -- every tenant's endpoints execute.",
)
_SET_ROLE_PERMISSIONS_0068 = _function_sql(
    _SQL_ARRAY_0068,
    "ADDED IN 0068: `platform.errors.*` reads every tenant's error\n"
    "    -- correlation ids.",
)


def upgrade() -> None:
    # 1. Off every role that holds it -- built-in, cloned or bespoke. ONLY the
    #    manage code: `agent.release.read` is not touched anywhere in this
    #    migration's upgrade, and `org-admin` keeps it.
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code = '{_MOVED}')"
    )
    # 2. On to the platform operator, BOTH codes. `org_id IS NULL` because
    #    `super-admin` is a global role and an org-scoped row of the same name
    #    would be a different role this migration must not invent.
    #    `ON CONFLICT DO NOTHING` because the read grant is additive and this
    #    must not fail on a database where somebody has already made it.
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id FROM roles r, permissions p "
        "WHERE r.name = 'super-admin' AND r.org_id IS NULL "
        f"AND p.code IN ('{_MOVED}', '{_READ}') "
        "ON CONFLICT DO NOTHING"
    )
    # 3. And shut the door the console could have walked back through.
    op.execute(_SET_ROLE_PERMISSIONS_WITH_OPERATOR_RAIL)


def downgrade() -> None:
    # Back to 0068's function first: a database downgraded past this
    # migration that kept 0070's body would refuse a code 0068 permits. 0069
    # does not touch this function, so 0068's is the definition below it.
    op.execute(_SET_ROLE_PERMISSIONS_0068)
    # Undo step 2 -- both codes, off `super-admin` only. Nothing else was given
    # them by this migration.
    op.execute(
        "DELETE FROM role_permissions WHERE "
        "role_id IN (SELECT id FROM roles WHERE name = 'super-admin' AND org_id IS NULL) "
        f"AND permission_id IN (SELECT id FROM permissions WHERE code IN ('{_MOVED}', '{_READ}'))"
    )
    # And restore 0060's grant. `WHERE r.name = 'org-admin'` with no `org_id`
    # predicate is 0060's own clause, verbatim: this restores the state 0060
    # left, not a tidier one. Step 1 above removed the code from every role, so
    # there is nothing to conflict with; the clause is there for the same
    # reason the comment is.
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id FROM roles r, permissions p "
        f"WHERE r.name = 'org-admin' AND p.code = '{_MOVED}' "
        "ON CONFLICT DO NOTHING"
    )
