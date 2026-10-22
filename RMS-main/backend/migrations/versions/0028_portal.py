"""end-user portal: portal_user role, row-scoped policies, requester tier

docs/modules/portal/spec.md (Task 1: security foundation).

WHY THE ROLE AND ITS GRANTS LIVE IN A MIGRATION. `app_user`'s table privileges
are granted in `tests/conftest.py` and nowhere else -- `deploy/postgres/
init-app-user.sh` grants it only CONNECT -- so a real deployment has an
app_user with no table privileges and the test suite masks it. That gap is
pre-existing and NOT fixed here, but this module must not replicate it: a
migration runs wherever the schema does, so portal_user is correctly
provisioned in every environment. The deploy script still creates the role
with a LOGIN password, which a migration cannot know.

Revision ID: 0028
Revises: 0027
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spec §2.1. ticket_queue is deliberately ABSENT -- a requester never chooses
# a queue, so portal_user gets no grant on it at all.
_ORG_GUC = "current_setting('app.current_org', true)::uuid"
_REQ_GUC = "current_setting('app.current_requester', true)::uuid"

# EVERY policy below is `AS RESTRICTIVE TO portal_user`. The existing
# tenant_isolation policy is PERMISSIVE with roles={public}, so it also covers
# portal_user, and Postgres OR's permissive policies together -- a permissive
# portal policy would be OR'd with the org-only predicate and the portal
# would see EVERY ticket in the org. Verified empirically: permissive -> 2 of
# 2 rows visible; restrictive -> 1 of 2, staff unaffected at 2,
# requester-GUC-unset -> 0.
#
# Because tenant_isolation already supplies org scope and restrictive
# policies are AND'd, these predicates carry only the REQUESTER half.
#
# If the requester GUC is unset, current_setting(..., true) returns NULL, the
# comparison is NULL rather than true, and no row matches -- an empty portal
# instead of someone else's tickets.
_MINE = f"requester_id = {_REQ_GUC}"
_PARENT_IS_MINE = (
    f"ticket_id IN (SELECT id FROM ticket "
    f"WHERE org_id = {_ORG_GUC} AND requester_id = {_REQ_GUC})"
)

PERMISSIONS = [
    ("portal.ticket.read", "portal", "read"),
    ("portal.ticket.write", "portal", "write"),
]
_ALL = [c for c, _, _ in PERMISSIONS]
# Every other module's migration in this repo grants its new permissions to
# super-admin/org-admin (0023_service_desk.py, 0025_notification_core.py) so
# that the full-permission invariant `test_org_admin_has_all_permissions`
# holds; the portal codes are no exception. This is IN ADDITION to the
# `requester` role below, which is the actual portal-facing tier and holds
# ONLY these two codes (test_requester_role_exists_and_holds_only_portal_
# permissions asserts that about `requester` specifically, not about
# super-admin/org-admin).
ROLE_GRANTS: dict[str, list[str]] = {
    "super-admin": _ALL,
    "org-admin": _ALL,
}


def upgrade() -> None:
    # The role may already exist (a deployment's init script creates it with a
    # password before migrations run), so this is idempotent.
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'portal_user') "
        "THEN CREATE ROLE portal_user NOSUPERUSER NOBYPASSRLS NOLOGIN; END IF; END $$;"
    )
    op.execute("GRANT USAGE ON SCHEMA public TO portal_user")

    # Least privilege, table by table. No DELETE anywhere; no UPDATE except the
    # number counter, which numbering.py must increment.
    op.execute("GRANT SELECT, INSERT ON ticket TO portal_user")
    op.execute("GRANT SELECT, INSERT ON ticket_comment TO portal_user")
    op.execute("GRANT SELECT, INSERT ON ticket_attachment TO portal_user")
    op.execute("GRANT SELECT ON ticket_status_history TO portal_user")
    op.execute("GRANT SELECT ON ticket_approval TO portal_user")
    op.execute("GRANT SELECT ON ticket_category TO portal_user")
    # INSERT is required: numbering.py creates the counter row lazily with
    # on_conflict_do_nothing before its atomic UPDATE ... RETURNING.
    op.execute("GRANT SELECT, INSERT, UPDATE ON ticket_number_counter TO portal_user")

    # Row-scoped policies. These are ADDITIONAL to the existing
    # tenant_isolation policies (which apply to app_user); nothing existing is
    # altered. `TO portal_user` is what keeps staff behaviour untouched.
    op.execute(
        f"CREATE POLICY portal_isolation ON ticket AS RESTRICTIVE TO portal_user "
        f"USING ({_MINE}) WITH CHECK ({_MINE})"
    )
    op.execute(
        f"CREATE POLICY portal_isolation ON ticket_comment AS RESTRICTIVE TO portal_user "
        f"USING ({_PARENT_IS_MINE} AND visibility = 'public') "
        f"WITH CHECK ({_PARENT_IS_MINE} AND visibility = 'public')"
    )
    op.execute(
        f"CREATE POLICY portal_isolation ON ticket_attachment AS RESTRICTIVE TO portal_user "
        f"USING ({_PARENT_IS_MINE}) WITH CHECK ({_PARENT_IS_MINE})"
    )
    op.execute(
        f"CREATE POLICY portal_isolation ON ticket_status_history AS RESTRICTIVE TO portal_user "
        f"USING ({_PARENT_IS_MINE})"
    )
    op.execute(
        f"CREATE POLICY portal_isolation ON ticket_approval AS RESTRICTIVE TO portal_user "
        f"USING ({_PARENT_IS_MINE})"
    )
    # ticket_category and ticket_number_counter get NO portal policy. They are
    # org refdata a requester legitimately needs org-wide access to (a category
    # to file under; the counter to allocate a number), and the existing
    # PERMISSIVE tenant_isolation policy already scopes both to the org. Adding
    # a restrictive policy here would only narrow them further with no security
    # benefit -- and a restrictive `active = true` on categories would silently
    # break filing under a category an admin had temporarily deactivated,
    # which belongs in the query, not the policy.

    for code, module, action in PERMISSIONS:
        op.execute(
            "INSERT INTO permissions (id, code, module, action) "
            f"VALUES (gen_random_uuid(), '{code}', '{module}', '{action}')"
        )
    op.execute(
        "INSERT INTO roles (id, org_id, name, description) VALUES "
        "(gen_random_uuid(), NULL, 'requester', "
        "'End user: raise and follow up on their OWN tickets via the portal only')"
    )
    codes = ", ".join(f"'{c}'" for c, _, _ in PERMISSIONS)
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id FROM roles r, permissions p "
        f"WHERE r.name = 'requester' AND r.org_id IS NULL AND p.code IN ({codes})"
    )
    for role, role_codes in ROLE_GRANTS.items():
        role_code_list = ", ".join(f"'{c}'" for c in role_codes)
        op.execute(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            f"WHERE r.name = '{role}' AND r.org_id IS NULL AND p.code IN ({role_code_list})"
        )


def downgrade() -> None:
    for table in (
        "ticket_number_counter",
        "ticket_category",
        "ticket_approval",
        "ticket_status_history",
        "ticket_attachment",
        "ticket_comment",
        "ticket",
    ):
        op.execute(f"DROP POLICY IF EXISTS portal_isolation ON {table}")
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE module = 'portal')"
    )
    op.execute("DELETE FROM roles WHERE name = 'requester' AND org_id IS NULL")
    op.execute("DELETE FROM permissions WHERE module = 'portal'")
