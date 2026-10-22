"""platform: `platform.errors.*` belongs to the platform operator, not to every tenant's admin

Closes F1 of `docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md`.

WHAT WAS WRONG. `GET /api/v1/platform/errors/{group_id}` returns
`platform_error_event` rows, and those rows carry `org_id`, `user_id`,
`job_id` and `device_id` for EVERY tenant. Nothing else stands between them
and the caller: the route reads on a bare `AsyncSession(app_engine)` with no
`app.current_org` pinned (`app/api/v1/errors.py`), and the table's only RLS
policy is `USING (true) WITH CHECK (true)` (0056) -- the one table in this
schema carrying `org_id` whose policy predicate never mentions it. So the
permission code gating that route IS the tenant boundary, entire. 0057 granted
it to `org-admin`, the role every tenant's own administrator holds, and the
audit reproduced one org's administrator reading another org's identifiers
through it.

WHY THIS SHAPE OF FIX. Two other fixes were considered and rejected: scoping
the route to the caller's org, and nulling the tenant-bearing columns for a
caller whose org does not match. Both remove the cross-org view that is the
feature's entire purpose -- "this line has failed 4,000 times, across these
orgs, these users and these devices" is what an operator triages with. The
tables are platform-scoped by design (spec §4.1) and stay that way; what
moves is who may read them.

`super-admin` is the role for it, and 0003 already describes it as exactly
that: "Cross-org platform administration (SaaS operator)". It is also
unreachable from every org-scoped surface -- `app/core/rbac_admin.py` will not
list, clone, edit or delete it, `app/modules/user_admin/service.py` will not
grant or revoke it, and 0065's `set_role_permissions` refuses it by name -- so
a tenant administrator cannot mint themselves into it. 0003's `ALL` was
computed from 0003's own ten codes, so `super-admin` never received the later
`platform.errors.*` codes; this migration is the grant.

WHAT A DEPLOYMENT THAT HAS ALREADY RUN 0057 LOSES, PLAINLY. Every current
holder of `org-admin` -- on the single-tenant on-prem install, that is the
operator's own everyday account -- stops being able to reach the error
monitor. Concretely, after `alembic upgrade head`:

  * `GET /api/v1/platform/errors` and `GET /api/v1/platform/errors/{id}`
    answer `403 {"detail": "requires platform.errors.read"}`;
  * `POST .../resolve` and `POST .../unresolve` answer
    `403 {"detail": "requires platform.errors.write"}`;
  * the console's "Error Monitor" nav entry disappears on its own --
    `frontend/src/shell/nav.ts` gates it on `platform.errors.read` read from
    `/me`, so no frontend change is needed and none was made.

To restore access, assign the global `super-admin` role to the operator's
user. That cannot be done from the console by design; it is a direct
`user_roles` insert, and it is deliberately the same act as before this
migration -- somebody with database access deciding who runs the platform.

REVOKED FROM EVERY ROLE, NOT FROM `org-admin` BY NAME. A deployment that has
run 0057 may have had an administrator clone `org-admin` into an org-local
role (0065's clone-on-write does exactly that on the first matrix edit) or
tick these boxes on a bespoke role. Revoking only the built-in grant would fix
the finding on a fresh database and nowhere else.

AND THE MATRIX SURFACE IS CLOSED BEHIND IT. Without the function change below,
this migration would be cosmetic: a holder of `roles.manage` -- which is
`org-admin` -- could create a role carrying `platform.errors.read`, assign it
to themselves with `users.manage`, and be back where 0057 left them, in two
console calls and no SQL. `app/core/rbac_admin.py` now keeps the codes out of
the permission matrix and refuses them on write; this replaces
`set_role_permissions` so the rail also holds below the service layer, for a
caller who bypasses that module entirely. It is the same rail 0065 built for
`super-admin`, applied to a set of codes rather than to a role name, and it is
an explicit code list rather than a `platform.%` prefix because
`platform.status.read` is a code an org may legitimately hold and the console
posts every held code back on an ordinary role edit.

`platform.status.read` IS DELIBERATELY LEFT ALONE. It was seeded to
`org-admin` by 0054 for the same mechanical reason 0057 seeded these (a test
asserting org-admin holds every code), but it is not the same defect: it reads
probe results, process heartbeats and the module registry, none of which come
from a table carrying `org_id`, so no tenant row is disclosed by it. Revoking
it would take a working page away from the on-prem administrator and close
nothing. Whether it should move before this platform goes multi-tenant is
recorded in the audit, not decided here.

Revision ID: 0068
Revises: 0067
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0068"
down_revision: str | None = "0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirrors `app.core.rbac.PLATFORM_OPERATOR_CODES`. Two copies, because a
# migration must not import application code that will have moved on by the
# time it runs -- `tests/test_rbac.py::
# test_the_migration_and_the_application_name_the_same_operator_codes holds
# the two copies equal.
_CODES = ("platform.errors.read", "platform.errors.write")

_CODE_LIST = ", ".join(f"'{c}'" for c in _CODES)
_SQL_ARRAY = "ARRAY[" + ", ".join(f"'{c}'" for c in _CODES) + "]"

# 0065's function, with one added rail. Reproduced in full rather than patched
# because `CREATE OR REPLACE FUNCTION` has no other form: a replacement is the
# whole body or nothing.
_SET_ROLE_PERMISSIONS_WITH_OPERATOR_RAIL = f"""
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

    -- ADDED IN 0068, AND IT IS WHAT MAKES 0068 MEAN ANYTHING. The platform
    -- operator's codes read every tenant's error correlation ids across the
    -- tenant boundary. Revoking them from `org-admin` while leaving this
    -- function able to write them onto any org-scoped role would be a
    -- revocation an org-admin holding `roles.manage` could undo from the
    -- console. Refused by CODE rather than by role name, because the escalation
    -- path is a brand-new role nobody has named yet.
    SELECT string_agg(c, ', ') INTO v_operator
    FROM unnest(p_codes) AS c
    WHERE c = ANY({_SQL_ARRAY});
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

# 0065's function verbatim, for the downgrade. Restoring the previous
# definition is the only honest inverse of replacing it: leaving 0068's body in
# place would mean a database downgraded to 0067 refused codes 0067 grants.
_SET_ROLE_PERMISSIONS_0065 = """
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
    v_count integer;
BEGIN
    SELECT name, org_id INTO v_name, v_org FROM roles WHERE id = p_role_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no such role: %', p_role_id USING ERRCODE = '23503';
    END IF;

    IF v_name = 'super-admin' THEN
        RAISE EXCEPTION 'super-admin is not editable' USING ERRCODE = '42501';
    END IF;

    IF v_org IS NULL THEN
        RAISE EXCEPTION 'global role % must be cloned before editing', v_name
            USING ERRCODE = '42501';
    END IF;

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


def upgrade() -> None:
    # 1. Off every role that holds them -- built-in, cloned or bespoke.
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code IN ({_CODE_LIST}))"
    )
    # 2. On to the platform operator. `org_id IS NULL` because `super-admin` is
    #    a global role and an org-scoped row of the same name would be a
    #    different role that this migration must not invent.
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id FROM roles r, permissions p "
        f"WHERE r.name = 'super-admin' AND r.org_id IS NULL AND p.code IN ({_CODE_LIST})"
    )
    # 3. And shut the door the console could have walked back through.
    op.execute(_SET_ROLE_PERMISSIONS_WITH_OPERATOR_RAIL)


def downgrade() -> None:
    op.execute(_SET_ROLE_PERMISSIONS_0065)
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code IN ({_CODE_LIST}))"
    )
    # Back to 0057's state exactly: both codes on `org-admin`, and nothing on
    # `super-admin`. A downgrade that left the operator grant behind would make
    # `alembic downgrade 0067` a different database from one that had never run
    # 0068.
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id FROM roles r, permissions p "
        f"WHERE r.name = 'org-admin' AND r.org_id IS NULL AND p.code IN ({_CODE_LIST})"
    )
