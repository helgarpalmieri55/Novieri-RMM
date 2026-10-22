"""access control: the codes that gate the new surface, and the one way to write the matrix

`docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §1.2, §2.

ONE MIGRATION FOR THE WHOLE CYCLE, DELIBERATELY. Four separate workstreams each
independently planned a `0065`. Alembic accepts several files sharing
`down_revision` without complaint and only fails later, at `upgrade head`, with
"multiple heads" -- at which point every database-backed test dies in fixture
setup pointing at nobody's code in particular. So the cycle gets one migration
and one author.

### The SECURITY DEFINER function is the interesting half

`deploy/postgres/grant-app-privileges.sql` REVOKES INSERT/UPDATE/DELETE on
`permissions` and `role_permissions` from `app_user`, and its own rationale
block explains why: without it, `app_user` could write the RBAC catalog
directly, "a way to self-grant permissions that bypasses the API's own RBAC
checks entirely". That control is load-bearing and it STAYS.

But a permission-matrix UI has to write `role_permissions` somehow. The three
options were: grant the writes (deletes the control), write through the
BYPASSRLS admin engine (makes tenant scoping application-code-only, which
invariant 5 calls a security bug), or this -- a function owned by `its_admin`
that is the ONLY writable surface, validates its own arguments, and can enforce
in the DATABASE what would otherwise be enforceable only in Python.

Two things it refuses that Python alone could not guarantee:

* **`super-admin` is untouchable.** It is cross-org platform administration; an
  org-scoped console that could edit it is a tenant-escalation path. The
  function refuses that role by name, so the rail holds even for a caller that
  bypasses the service layer entirely.
* **An unknown code is an error, not a silent no-op.** `INSERT ... SELECT`
  against a misspelled code inserts zero rows and returns success, which would
  present in the console as a permission that will not stick.

Revision ID: 0065
Revises: 0064
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Gating the new surface. NOT `orgs.manage`, which today gates only the org
# name/settings PATCH and remote-tunnel authorization -- attaching identity
# lifecycle to it would silently upgrade every current holder from "can rename
# the org" to "can mint a fleet admin", a privilege escalation delivered by a
# description string. Two codes rather than one because "can add a user" and
# "can redefine what every role means" are different levels of trust.
_CODES = [
    ("roles.read", "roles", "read"),
    ("roles.manage", "roles", "manage"),
    ("users.read", "users", "read"),
    ("users.manage", "users", "manage"),
]

# `super-admin` gets them because it holds every code by construction
# (0003_rbac_seed.py grants it ALL); `org-admin` because this is org
# administration. Nothing else -- a technician who could mint a fleet admin
# makes every other permission boundary decorative.
_GRANT_TO = ("super-admin", "org-admin")

_SET_ROLE_PERMISSIONS = """
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


def upgrade() -> None:
    for code, module, action in _CODES:
        op.execute(
            "INSERT INTO permissions (id, code, module, action) "
            f"VALUES (gen_random_uuid(), '{code}', '{module}', '{action}')"
        )
        role_list = ", ".join(f"'{r}'" for r in _GRANT_TO)
        op.execute(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            f"WHERE r.name IN ({role_list}) AND r.org_id IS NULL AND p.code = '{code}'"
        )

    op.execute(_SET_ROLE_PERMISSIONS)
    # EXECUTE is granted; the underlying REVOKE on role_permissions is
    # untouched. That pairing is the whole design: the only write path is one
    # that validates first.
    op.execute("GRANT EXECUTE ON FUNCTION set_role_permissions(uuid, text[]) TO app_user")

    # `users.status` has existed since 0001 and NOTHING has ever read it, so
    # nothing has ever constrained it either. The disable and delete paths give
    # it meaning, and a CHECK is what stops a fourth value appearing later and
    # being silently treated as "not active" by a gate that tests equality.
    op.execute(
        "ALTER TABLE users ADD CONSTRAINT ck_users_status "
        "CHECK (status IN ('active', 'disabled', 'deleted'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_status")
    op.execute("DROP FUNCTION IF EXISTS set_role_permissions(uuid, text[])")
    # role_permissions first: it references permissions, and dropping those
    # rows out from under it is what leaves a downgrade half-applied.
    codes = ", ".join(f"'{c}'" for c, _m, _a in _CODES)
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code IN ({codes}))"
    )
    op.execute(f"DELETE FROM permissions WHERE code IN ({codes})")
