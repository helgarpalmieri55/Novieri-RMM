"""Phase 16 kernel registry: four event types, five permission codes, and the
four `notification_rules` CHECK constraints that make a health alert routed to
a ticket requester unrepresentable.

docs/modules/endpoint-health-alerting/spec.md §9, §9.1, §9.2, §11.1.

WHY THE DB-LAYER TESTS RUN ON `admin_engine`. The application already refuses
these pairings in two places (`RuleCreate`'s validator and
`rules.create_rule`'s service check, both reading
`app.core.events.staff_only_rejection`). A test driven through the API would
therefore stay green with the CHECK constraint deleted -- it would be pinning
the Python guard, not the database one. Going in as `its_admin` with raw SQL
removes both application guards from the path, so the only thing left that can
refuse the row is Postgres. Each such test also carries a POSITIVE CONTROL
insert (same row, `recipient_kind='queue_members'`) so that a failure caused by
a missing FK or a NOT NULL can never be mistaken for the guard working, and a
read-back that asserts the rejected row is absent -- with the CHECK deleted the
insert succeeds and that read-back returns the row.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as sqltext
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.events import EVENT_TYPES, STAFF_ONLY_EVENT_TYPES, staff_only_rejection
from app.core.rbac import PERMISSIONS

HEALTH_EVENT_TYPES = (
    "health.alert_raised",
    "health.alert_cleared",
    "health.alert_escalated",
    "health.remediation_requires_approval",
)

# Constraint name per event type (spec §9.2). The suffix is the event type's
# local part; SQL cannot read `STAFF_ONLY_EVENT_TYPES`, so the pairing is
# restated per type in the migration and pinned here.
HEALTH_RULE_CHECKS = {
    "health.alert_raised": "ck_rule_no_health_alert_raised_to_requester",
    "health.alert_cleared": "ck_rule_no_health_alert_cleared_to_requester",
    "health.alert_escalated": "ck_rule_no_health_alert_escalated_to_requester",
    "health.remediation_requires_approval": (
        "ck_rule_no_health_remediation_requires_approval_to_requester"
    ),
}

HEALTH_PERMISSIONS = (
    "health.read",
    "health.alert.manage",
    "health.admin",
    "health.remediate",
    "health.action.admin",
)

# Spec §11.1: the three grants that put configuration of the whole fleet's
# alarms, or code execution as SYSTEM/root, in one hand.
HIGH_PRIVILEGE_PERMISSIONS = ("health.admin", "health.remediate", "health.action.admin")


# ---------------------------------------------------------------------------
# app/core/events.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("event_type", HEALTH_EVENT_TYPES)
def test_health_event_type_is_registered(event_type: str) -> None:
    assert event_type in EVENT_TYPES


@pytest.mark.parametrize("event_type", HEALTH_EVENT_TYPES)
def test_health_event_type_is_staff_only(event_type: str) -> None:
    entry = STAFF_ONLY_EVENT_TYPES[event_type]
    assert entry.noun
    assert "endpoint health spec" in entry.spec_ref


@pytest.mark.parametrize("event_type", HEALTH_EVENT_TYPES)
def test_staff_only_rejection_refuses_requester_and_names_the_type(event_type: str) -> None:
    reason = staff_only_rejection(event_type, "requester")
    assert reason is not None
    # The rejection must name THIS event type's noun, not merely be non-None:
    # a rejection that said "internal notes" for a health alert would be a
    # single shared string, and the guard would be one `dict.get` away from
    # answering for a type it does not cover.
    assert STAFF_ONLY_EVENT_TYPES[event_type].noun in reason


@pytest.mark.parametrize("event_type", HEALTH_EVENT_TYPES)
@pytest.mark.parametrize("recipient_kind", ["queue_members", "assignee", "channel_default"])
def test_staff_only_rejection_permits_every_other_recipient_kind(
    event_type: str, recipient_kind: str
) -> None:
    assert staff_only_rejection(event_type, recipient_kind) is None


# ---------------------------------------------------------------------------
# app/core/rbac.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", HEALTH_PERMISSIONS)
def test_health_permission_code_is_registered(code: str) -> None:
    assert code in PERMISSIONS
    assert PERMISSIONS[code].strip()


@pytest.mark.parametrize("code", HIGH_PRIVILEGE_PERMISSIONS)
def test_high_privilege_health_permissions_say_so(code: str) -> None:
    """Spec §11.1 requires the HIGH PRIVILEGE wording on these three, matching
    `notification.admin` and `sla.admin`. The description is what a reviewer
    granting a role actually reads."""
    assert "HIGH PRIVILEGE" in PERMISSIONS[code]


async def _role_grants(admin_engine: AsyncEngine, role: str) -> set[str]:
    """The permission codes granted to a built-in (org_id IS NULL) role, read
    from the real `role_permissions` rows a migration seeded."""
    async with admin_engine.connect() as conn:
        rows = await conn.execute(
            sqltext(
                "SELECT p.code FROM role_permissions rp "
                "JOIN permissions p ON p.id = rp.permission_id "
                "JOIN roles r ON r.id = rp.role_id "
                "WHERE r.name = :role AND r.org_id IS NULL"
            ),
            {"role": role},
        )
        return {r.code for r in rows}


@pytest.mark.parametrize("role", ["super-admin", "org-admin"])
async def test_admin_roles_hold_every_health_permission(
    admin_engine: AsyncEngine, role: str
) -> None:
    assert set(HEALTH_PERMISSIONS) <= await _role_grants(admin_engine, role)


async def test_technician_tier_roles_hold_no_high_privilege_health_grant(
    admin_engine: AsyncEngine,
) -> None:
    """Spec §11.1: `health.admin`, `health.remediate` and `health.action.admin`
    are admin tier, never technician -- one decides what the whole fleet alarms
    on, the other two put signed code on endpoints as SYSTEM/root.

    Asserted against the seeded `role_permissions` rows rather than against a
    Python dict, because grants in this codebase ARE rows: a later migration
    adding `technician` to a ROLE_GRANTS map for convenience is exactly what
    this test exists to fail on."""
    for role in ("technician", "auditor", "read-only"):
        granted = await _role_grants(admin_engine, role)
        assert granted & set(HIGH_PRIVILEGE_PERMISSIONS) == set(), role


async def test_technician_holds_the_alert_day_job_only(admin_engine: AsyncEngine) -> None:
    """Spec §11.1 calls `health.alert.manage` "the day-job" for the technician
    tier. Pinning both halves -- what technician HAS and what it has NOT --
    stops this from degrading into "technician has nothing", which would pass
    the guard test above while making the module unusable."""
    granted = await _role_grants(admin_engine, "technician")
    assert granted & set(HEALTH_PERMISSIONS) == {"health.read", "health.alert.manage"}


async def test_health_remediate_is_not_implied_by_health_admin(
    admin_engine: AsyncEngine,
) -> None:
    """Spec §11.1: "Deliberately NOT bundled into `health.admin`". There is no
    implication machinery in this RBAC -- codes are flat rows -- so the thing
    to pin is that they are two distinct `permissions` rows with two distinct
    ids, and that a role can hold one without the other."""
    async with admin_engine.connect() as conn:
        rows = await conn.execute(
            sqltext(
                "SELECT code, id FROM permissions "
                "WHERE code IN ('health.admin', 'health.remediate')"
            )
        )
        by_code = {r.code: r.id for r in rows}
    assert set(by_code) == {"health.admin", "health.remediate"}
    assert by_code["health.admin"] != by_code["health.remediate"]

    # And a role holding health.admin does not thereby hold health.remediate:
    # build one and read the grants back.
    org_id = await _seed_org(admin_engine)
    role_id = uuid4()
    async with admin_engine.begin() as conn:
        await conn.execute(
            sqltext(
                "INSERT INTO roles (id, org_id, name, description) "
                "VALUES (:id, :org, 'health-config-only', 'x')"
            ),
            {"id": role_id, "org": org_id},
        )
        await conn.execute(
            sqltext(
                "INSERT INTO role_permissions (role_id, permission_id) "
                "SELECT :role, id FROM permissions WHERE code = 'health.admin'"
            ),
            {"role": role_id},
        )
    async with admin_engine.connect() as conn:
        codes = {
            r.code
            for r in await conn.execute(
                sqltext(
                    "SELECT p.code FROM role_permissions rp "
                    "JOIN permissions p ON p.id = rp.permission_id "
                    "WHERE rp.role_id = :role"
                ),
                {"role": role_id},
            )
        }
    assert codes == {"health.admin"}


# ---------------------------------------------------------------------------
# The four notification_rules CHECK constraints (spec §9.2)
# ---------------------------------------------------------------------------


async def _seed_org(admin_engine: AsyncEngine) -> UUID:
    from app.models import Organization

    slug = f"health-ck-{uuid4().hex[:8]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org.id


async def _seed_rule_prerequisites(admin_engine: AsyncEngine) -> tuple[UUID, UUID, UUID]:
    """An org, a user, and a webhook channel -- everything a `notification_rules`
    row needs besides the two columns under test. Built through the ORM on the
    admin engine so the row shapes come from the models rather than from a
    hand-written INSERT that would drift."""
    from app.models import NotificationChannel, Organization, User

    slug = f"health-ck-{uuid4().hex[:8]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@test.local")
        session.add(user)
        await session.flush()
        channel = NotificationChannel(
            org_id=org.id,
            kind="webhook",
            name="ops",
            config={"url": "https://hooks.example.com/x"},
            created_by=user.id,
        )
        session.add(channel)
        await session.commit()
        return org.id, user.id, channel.id


_INSERT_RULE = sqltext(
    "INSERT INTO notification_rules "
    "(id, org_id, event_type, channel_id, queue_id, recipient_kind, active, "
    " created_by, created_at) "
    "VALUES (:id, :org, :event_type, :channel, NULL, :kind, true, :u, now())"
)


@pytest.mark.parametrize("event_type", HEALTH_EVENT_TYPES)
async def test_health_event_paired_with_requester_is_refused_by_the_database(
    admin_engine: AsyncEngine, event_type: str
) -> None:
    org_id, user_id, channel_id = await _seed_rule_prerequisites(admin_engine)

    # POSITIVE CONTROL: the identical row with a permitted recipient_kind must
    # land. Without this, a NOT NULL or FK failure below would look exactly
    # like the guard working.
    control_id = uuid4()
    async with admin_engine.begin() as conn:
        await conn.execute(
            _INSERT_RULE,
            {
                "id": control_id,
                "org": org_id,
                "event_type": event_type,
                "channel": channel_id,
                "kind": "queue_members",
                "u": user_id,
            },
        )
    async with admin_engine.connect() as conn:
        landed = (
            await conn.execute(
                sqltext("SELECT recipient_kind FROM notification_rules WHERE id = :id"),
                {"id": control_id},
            )
        ).scalar_one()
    assert landed == "queue_members"

    # The assertion below is deliberately NOT `pytest.raises`. It reduces both
    # possible worlds to a STRING and compares them, so the guarded and the
    # unguarded path produce different observable values: with the CHECK in
    # place the outcome names the constraint; with the CHECK deleted the insert
    # succeeds and the outcome is the stored row read back out of the table,
    # which is what the failure message then prints.
    forbidden_id = uuid4()
    outcome: str
    async with admin_engine.connect() as conn:
        try:
            async with conn.begin():
                await conn.execute(
                    _INSERT_RULE,
                    {
                        "id": forbidden_id,
                        "org": org_id,
                        "event_type": event_type,
                        "channel": channel_id,
                        "kind": "requester",
                        "u": user_id,
                    },
                )
        except (IntegrityError, ProgrammingError) as exc:
            constraint = HEALTH_RULE_CHECKS[event_type]
            outcome = f"refused by {constraint}" if constraint in str(exc) else f"refused: {exc}"
        else:
            stored = (
                await conn.execute(
                    sqltext(
                        "SELECT event_type, recipient_kind FROM notification_rules "
                        "WHERE id = :id"
                    ),
                    {"id": forbidden_id},
                )
            ).all()
            outcome = f"ACCEPTED and readable: {stored}"

    assert outcome == f"refused by {HEALTH_RULE_CHECKS[event_type]}"
