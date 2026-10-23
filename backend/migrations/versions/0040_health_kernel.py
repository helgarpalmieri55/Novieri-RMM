"""endpoint health kernel: permission codes and staff-only rule guards

docs/modules/endpoint-health-alerting/spec.md §9.2, §11.1.

Two things, both kernel-owned, and neither of them a health table (those come
later): the five `health.*` permission codes with their role grants, and one
`notification_rules` CHECK per new staff-only event type.

WHY FOUR SEPARATE CHECKS AND NOT ONE `LIKE 'health.%'` PREDICATE. A prefix
match would silently cover every future `health.*` event type, including one
that is deliberately NOT staff-only -- and the point of
`app.core.events.STAFF_ONLY_EVENT_TYPES` is that membership is a decision
someone made per type. SQL cannot read that dict, so the set is restated here
literally, exactly as `ck_rule_no_internal_to_requester` (0025) and
`ck_rule_no_sla_breach_to_requester` (0034) restate theirs. Adding a fifth
staff-only type means a fifth CHECK in a new migration, which a reviewer sees.

An alert has no requester, so this is not a live hole today. It is added
anyway because it costs one CHECK per type, and because the structural
argument for skipping it -- "the payload carries no `requester_id`, so the
pairing is unroutable" -- is a statement about a payload convention, not about
the schema. Health payloads (spec §9.1) in fact carry `requester_id`
present-and-null, so the day a publisher populates it the pairing would
become routable with nothing to stop it. See the corrected comment in
`app/core/events.py`.

Revision ID: 0040
Revises: 0039
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# NOTE: the live `permissions` table is (id, code, module, action) -- there is
# NO `resource` and NO `description` column. The human-readable description
# lives only in `app/core/rbac.py::PERMISSIONS`, matching every other module's
# migration.
PERMISSIONS = [
    ("health.read", "health", "read"),
    ("health.alert.manage", "health", "alert.manage"),
    ("health.admin", "health", "admin"),
    ("health.remediate", "health", "remediate"),
    ("health.action.admin", "health", "action.admin"),
]

# Spec §11.1. `health.admin`, `health.remediate` and `health.action.admin` are
# admin tier and go to super-admin/org-admin ONLY -- the same posture
# `notification.admin` and `sla.admin` take. `health.alert.manage` is the
# technician day-job (acknowledge, mute, close); it changes no configuration
# and dispatches nothing. Everything below the technician tier is read-only.
#
# super-admin/org-admin receive all five so the full-permission invariant
# `test_rbac.py::test_org_admin_has_all_permissions` continues to hold.
_ALL = [c for c, _, _ in PERMISSIONS]
ROLE_GRANTS: dict[str, list[str]] = {
    "super-admin": _ALL,
    "org-admin": _ALL,
    "technician": ["health.read", "health.alert.manage"],
    "auditor": ["health.read"],
    "read-only": ["health.read"],
}

STAFF_ONLY_RULE_CHECKS = {
    "ck_rule_no_health_alert_raised_to_requester": "health.alert_raised",
    "ck_rule_no_health_alert_cleared_to_requester": "health.alert_cleared",
    "ck_rule_no_health_alert_escalated_to_requester": "health.alert_escalated",
    "ck_rule_no_health_remediation_requires_approval_to_requester": (
        "health.remediation_requires_approval"
    ),
}


def upgrade() -> None:
    for constraint, event_type in STAFF_ONLY_RULE_CHECKS.items():
        op.execute(
            f"ALTER TABLE notification_rules ADD CONSTRAINT {constraint} "
            f"CHECK (NOT (event_type = '{event_type}' AND recipient_kind = 'requester'))"
        )

    for code, module, action in PERMISSIONS:
        op.execute(
            "INSERT INTO permissions (id, code, module, action) "
            f"VALUES (gen_random_uuid(), '{code}', '{module}', '{action}')"
        )
    for role, codes in ROLE_GRANTS.items():
        code_list = ", ".join(f"'{c}'" for c in codes)
        op.execute(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            f"WHERE r.name = '{role}' AND r.org_id IS NULL AND p.code IN ({code_list})"
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE module = 'health')"
    )
    op.execute("DELETE FROM permissions WHERE module = 'health'")
    for constraint in STAFF_ONLY_RULE_CHECKS:
        op.execute(f"ALTER TABLE notification_rules DROP CONSTRAINT IF EXISTS {constraint}")
