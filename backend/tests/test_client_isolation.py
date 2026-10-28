"""Spec §5 tests 3-8, over every phase-1 table, through the real RLS.

Seeding is through the ADMIN engine (no RLS), asserting is through
`org_session` (RLS on) as `app_user`. The fixture makes the client the ONLY
discriminator: X and Y are two customers of one org, each with a site, a
device, a group, a job, a health alert and a documentation record, alike in
everything but their client -- the two groups even share a name. Any other
column that separated them would let a missing predicate hide (HANDOFF.md §4).

WHERE EACH SPEC §5 TEST LIVES. Names alone are in this file; `rls::` is
`tests/test_client_scope_rls.py`, which pins what migration 0231 adds beyond
the catalog's literal copy.

3. Isolation over the whole catalog.
   - EVERY phase-1 table, and `clients`, structurally:
     `test_every_scoped_table_hides_other_clients_rows` -- RLS enabled and
     forced, not owned by `app_user`, and exactly spec §2.2's client policies,
     expression by expression; with `test_the_app_role_is_held_by_rls`.
   - Behaviourally, one table per kind and inheritance path (C; H through
     devices, jobs, device_groups and two levels; O with no parent, through a
     parent and through a second parent; users' contacts):
     `test_another_clients_row_is_invisible_and_untouchable` -- Y's row is not
     read, changed or deleted, X's is, and nothing is inserted into Y. And
     `clients` itself: `test_a_list_scope_cannot_change_or_delete_another_client`
     with rls::test_the_clients_table_shows_a_list_scope_its_own_clients_only.
   - O tables' global rows are read but not changed:
     `test_a_global_row_is_read_but_not_changed`,
     `test_a_restricted_scope_reads_global_config_but_cannot_write_it`,
     rls::test_a_narrowed_scope_cannot_change_or_delete_global_rows,
     rls::test_a_narrowed_scope_cannot_change_or_delete_a_global_event,
     rls::test_a_narrowed_scope_writes_no_staff_row_and_not_another_clients_contact,
     rls::test_a_narrowed_scope_cannot_insert_a_staff_row.
4. The application's own client predicate, which RLS would mask, through the
   admin engine: `test_the_codes_own_client_predicate_holds_without_rls`.
5. No scope, zero rows and no write: the unscoped leg of
   `test_another_clients_row_is_invisible_and_untouchable` and of
   `test_a_global_row_is_read_but_not_changed`;
   rls::test_no_scope_guc_sees_zero_rows, rls::test_no_scope_guc_cannot_write,
   rls::test_no_scope_guc_hides_global_rows_and_staff_too,
   rls::test_an_empty_scope_sees_no_global_row.
6. `X-Client-Id` cannot widen the scope, through the API:
   `test_a_client_header_outside_the_scope_lists_nothing_and_404s`.
7. Inheritance: `test_a_child_inherits_and_cannot_contradict` (the INSERT, an
   alert of X's device among them); rls::test_an_inherited_client_cannot_be_rewritten
   (the UPDATE), rls::test_an_alerts_actions_follow_its_device,
   rls::test_a_retargeted_jobs_logs_follow_it. Moving a ticket is phase 2.
8. Immutability: `test_a_device_cannot_change_client`.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.client_scope_catalog import PHASE_1
from app.core.tenancy import org_session
from app.models import (
    BcpPlan,
    BcpTest,
    BcpTestStep,
    Budget,
    BudgetLine,
    ChangeRequest,
    Client,
    Deployment,
    Device,
    DeviceGroup,
    DeviceTag,
    DocArticle,
    DocRecord,
    DocVersion,
    EnrollmentToken,
    ExpenseCategory,
    HealthAlert,
    HealthAlertAction,
    HealthProfileAssignment,
    InstallLink,
    Job,
    JobLog,
    License,
    Organization,
    Package,
    Person,
    PersonLogin,
    Policy,
    Script,
    ScriptPolicy,
    Site,
    SoftwareProduct,
    Subscription,
    SubscriptionEvent,
    Ticket,
    TicketCategory,
    TicketComment,
    TicketQueue,
    User,
)
from app.services.device_groups import DeviceGroupError, create_device_group
from tests.client_scope_support import (
    deparse_policies,
    everything_on,
    expected_client_policies,
)
from tests.conftest import APP_ROLE, ApiEnv
from tests.health_fixtures import seed_check, seed_profile

#: What `app_user` meets writing a row its scope may not hold. Postgres checks
#: the policies' WITH CHECK before NOT NULL and before unique indexes, so it
#: is this message for every kind -- an H row whose parent is invisible gets a
#: NULL client from its trigger and fails here too.
REFUSED = "new row violates row-level security policy"

#: C tables whose BEFORE guard refuses first. Each reads the row's site,
#: client or target under the session's own RLS, finds nothing -- Y is
#: invisible -- and raises before the policy is checked. Refused all the same.
#: (`sites`'s message, from 0230, predates clients sharing an organisation;
#: a deployment's client is its target's, 0239.)
GUARDED = {
    "C-devices": "device site belongs to another client",
    "C-sites": "names a client of another organization",
    "C-deployments": "deployment target is not visible",
}


@dataclass(frozen=True)
class Side:
    """One client's rows: everything a child in the catalog can hang from."""

    client: UUID
    site: UUID
    device: UUID
    group: UUID
    job: UUID
    alert: UUID
    record: UUID
    ticket: UUID
    person: UUID
    subscription: UUID
    plan: UUID  # a BCP plan, and a test of it
    test: UUID
    budget: UUID  # a budget, and an expense category its lines can use
    expense_category: UUID


@dataclass(frozen=True)
class World:
    org: UUID
    actor: UUID  # a staff user, for the `created_by` columns
    script: UUID  # G: scripts are the organisation's
    profile: UUID  # G: health profiles too
    check: UUID
    queue: UUID  # G: ticket queues and categories too
    category: UUID
    product: UUID  # G: the software catalogue too
    package: UUID  # G: deployable packages too
    x: Side
    y: Side


def _now() -> datetime:
    return datetime.now(UTC)


def _job(org: UUID, device: UUID) -> Job:
    now = _now()
    return Job(
        org_id=org, type="inventory.collect", target_type="device", target_id=device,
        params={}, nonce=uuid4().hex, issued_at=now, expires_at=now + timedelta(hours=1),
    )


def _subscription(org: UUID, client: UUID, actor: UUID) -> Subscription:
    return Subscription(
        org_id=org, client_id=client, service_name="s", billing_cycle="monthly",
        cost_per_cycle=1, currency="USD", created_by=actor,
    )


def _plan(org: UUID, client: UUID, actor: UUID) -> BcpPlan:
    return BcpPlan(org_id=org, client_id=client, name="p", description="d", created_by=actor)


def _budget(org: UUID, client: UUID, actor: UUID) -> Budget:
    return Budget(
        org_id=org, client_id=client, fiscal_year=2027, name=f"b-{uuid4().hex[:6]}",
        granularity="month", created_by=actor,
    )


def _expense_category(org: UUID, client: UUID, actor: UUID) -> ExpenseCategory:
    return ExpenseCategory(
        org_id=org, client_id=client, key=f"k-{uuid4().hex[:6]}", name="n", created_by=actor
    )


def _ticket(org: UUID, client: UUID, queue: UUID, category: UUID, actor: UUID) -> Ticket:
    return Ticket(
        org_id=org, client_id=client, number=f"T-{uuid4().hex[:8]}", type="incident",
        category_id=category, queue_id=queue, status="new", subject="s",
        requester_id=actor, origin="technician", created_by=actor,
    )


async def _side(
    admin_engine: AsyncEngine,
    org: UUID,
    profile: UUID,
    check: UUID,
    *,
    actor: UUID,
    queue: UUID,
    category: UUID,
) -> Side:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        client = Client(org_id=org, name=f"Cliente {uuid4().hex[:6]}", kind="customer")
        s.add(client)
        await s.flush()
        site = Site(org_id=org, client_id=client.id, name="Sede")
        # The same name on both sides: unique per client since 0231.
        group = DeviceGroup(org_id=org, client_id=client.id, name="Servidores")
        record = DocRecord(org_id=org, client_id=client.id, record_type="service", name="r")
        s.add_all([site, group, record])
        await s.flush()
        device = Device(
            org_id=org, client_id=client.id, site_id=site.id, hostname="h", os_family="linux"
        )
        s.add(device)
        await s.flush()
        job = _job(org, device.id)
        alert = HealthAlert(
            org_id=org, device_id=device.id, check_id=check, profile_id=profile,
            check_type="cpu.busy_pct", severity="warning",
        )
        ticket = _ticket(org, client.id, queue, category, actor)
        person = Person(org_id=org, client_id=client.id, display_name="Persona")
        subscription = _subscription(org, client.id, actor)
        plan = _plan(org, client.id, actor)
        budget = _budget(org, client.id, actor)
        expense_category = _expense_category(org, client.id, actor)
        s.add_all([job, alert, ticket, person, subscription, plan, budget, expense_category])
        await s.flush()
        test = BcpTest(org_id=org, plan_id=plan.id, type="tabletop", created_by=actor)
        s.add(test)
        await s.commit()
    return Side(
        client.id, site.id, device.id, group.id, job.id, alert.id, record.id, ticket.id,
        person.id, subscription.id, plan.id, test.id, budget.id, expense_category.id,
    )


async def _two_clients(admin_engine: AsyncEngine) -> World:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="Iso", slug=f"iso-{uuid4().hex[:8]}")
        s.add(org)
        await s.flush()
        actor = User(org_id=org.id, external_subject=uuid4().hex, email="actor@test.local")
        script = Script(
            org_id=org.id, name="noop", os_family="linux", interpreter="bash", body="true"
        )
        s.add_all([actor, script])
        await s.flush()
        queue = TicketQueue(org_id=org.id, key="q", name="Q", created_by=actor.id)
        s.add(queue)
        await s.flush()
        category = TicketCategory(
            org_id=org.id, key="c", name="C", type="incident", default_queue_id=queue.id,
            created_by=actor.id,
        )
        product = SoftwareProduct(org_id=org.id, name="Office", created_by=actor.id)
        package = Package(
            org_id=org.id, name="htop", version="3", os_family="linux", installer_type="deb"
        )
        s.add_all([category, product, package])
        await s.commit()
    profile = await seed_profile(admin_engine, org.id)
    check = await seed_check(admin_engine, org.id, profile)
    tickets = {"actor": actor.id, "queue": queue.id, "category": category.id}
    x = await _side(admin_engine, org.id, profile, check, **tickets)
    y = await _side(admin_engine, org.id, profile, check, **tickets)
    return World(
        org.id, actor.id, script.id, profile, check, queue.id, category.id, product.id,
        package.id, x, y,
    )


async def _seed(admin_engine: AsyncEngine, *rows: Any) -> list[UUID]:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        s.add_all(rows)
        await s.commit()
    return [row.id for row in rows]


async def _ids(s: AsyncSession, sql: str, **params: Any) -> set[UUID]:
    return set((await s.execute(text(sql), params)).scalars())


async def _rowcount(s: AsyncSession, sql: str, **params: Any) -> int:
    result = await s.execute(text(sql), params)
    return int(result.rowcount)  # type: ignore[attr-defined]


async def _exists(admin_engine: AsyncEngine, table: str, row: UUID) -> bool:
    async with AsyncSession(admin_engine) as s:
        return await _ids(s, f"SELECT id FROM {table} WHERE id = :i", i=row) == {row}


@asynccontextmanager
async def _unscoped(org: UUID, app_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """The tenant GUC and NOT the client scope: the state a path that pinned
    only `app.current_org` would leave a session in (spec §2.2)."""
    async with AsyncSession(app_engine) as s, s.begin():
        await s.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(org)})
        yield s


# --------------------------------------------------------------------------- #
# Spec §5 test 3, structurally: every phase-1 table.
# --------------------------------------------------------------------------- #

#: Every phase-1 table (C, H and O), and `clients`, which is G in the catalog
#: but carries a client policy of its own (0231).
SWEPT = [*sorted(PHASE_1), "clients"]


async def test_the_app_role_is_held_by_rls(app_engine: AsyncEngine) -> None:
    """Once, not per table: the role every request runs as is neither a
    superuser nor BYPASSRLS -- either skips every policy the sweep checks --
    and the app engine really connects as it."""
    async with app_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT current_user AS who, rolsuper, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
    assert (row.who, row.rolsuper, row.rolbypassrls) == (APP_ROLE, False, False)


@pytest.mark.parametrize("table", SWEPT)
async def test_every_scoped_table_hides_other_clients_rows(
    table: str, admin_engine: AsyncEngine
) -> None:
    """THE SWEEP: what the database evaluates for every statement on `table`,
    one test per table so a regression names its table.

    RLS enabled AND forced (so the owner is held as well), the table not owned
    by `app_user` (an owner is exempt unless forced), and exactly the client
    policies of spec §2.2 -- the full set by name, each restrictive, for every
    role, with its command and both expressions as Postgres deparses them. The
    expected policies are written once, in
    `tests/client_scope_support.py::expected_client_policies`; a dropped,
    loosened or extra client policy turns this table's row red. What the
    expressions DO is the behavioural tests below."""
    expected = expected_client_policies(table)
    async with admin_engine.connect() as conn:
        rel = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity, "
                    "pg_get_userbyid(relowner) AS owner "
                    "FROM pg_class WHERE oid = to_regclass(:t)"
                ),
                {"t": f"public.{table}"},
            )
        ).one()
        live = {
            r.policyname: r
            for r in (
                await conn.execute(
                    text(
                        "SELECT policyname, permissive, roles, cmd, qual, with_check "
                        "FROM pg_policies WHERE schemaname = 'public' AND tablename = :t "
                        "AND policyname LIKE 'client\\_isolation%'"
                    ),
                    {"t": table},
                )
            ).all()
        }
        deparsed = await deparse_policies(conn, set(expected.values()))
        await conn.rollback()

    assert (rel.relrowsecurity, rel.relforcerowsecurity) == (True, True), f"{table}: RLS off"
    assert rel.owner != APP_ROLE, f"{table}: owned by {APP_ROLE}"
    assert set(live) == set(expected), f"{table}: client policies {sorted(live)}"
    for name, shape in expected.items():
        row = live[name]
        got = (row.permissive, list(row.roles), row.cmd)
        assert got == ("RESTRICTIVE", ["public"], shape[0]), (table, name, got)
        assert (row.qual, row.with_check) == deparsed[shape], (
            f"{table}.{name}: live\n  USING {row.qual}\n  CHECK {row.with_check}\n"
            f"expected\n  USING {deparsed[shape][0]}\n  CHECK {deparsed[shape][1]}"
        )


# --------------------------------------------------------------------------- #
# Spec §5 tests 3 and 5, behaviourally: one table per kind and inheritance path.
# --------------------------------------------------------------------------- #

Make = Callable[[World, Side], Any]

#: (table, a row of the given side's client). The paths
#: `tests/test_client_scope_rls.py` covers only in part -- it reads devices
#: and record versions, and follows jobs, alerts and articles to a new client
#: -- are here in full: read, change, delete and insert.
PATHS: dict[str, tuple[str, Make]] = {
    "C-devices": ("devices", lambda w, s: Device(
        org_id=w.org, client_id=s.client, site_id=s.site, hostname="h", os_family="linux"
    )),
    "C-device_groups": ("device_groups", lambda w, s: DeviceGroup(
        org_id=w.org, client_id=s.client, name=f"g-{uuid4().hex[:6]}"
    )),
    "C-sites": ("sites", lambda w, s: Site(org_id=w.org, client_id=s.client, name="Sede 2")),
    "C-enrollment_tokens": ("enrollment_tokens", lambda w, s: EnrollmentToken(
        org_id=w.org, client_id=s.client, site_id=s.site, token_hash=uuid4().hex,
        expires_at=_now() + timedelta(hours=1),
    )),
    "C-install_links": ("install_links", lambda w, s: InstallLink(
        org_id=w.org, client_id=s.client, site_id=s.site
    )),
    "H-jobs-via-target": ("jobs", lambda w, s: _job(w.org, s.device)),
    "H-device_tags-via-devices": ("device_tags", lambda w, s: DeviceTag(
        org_id=w.org, device_id=s.device, key=f"k-{uuid4().hex[:6]}"
    )),
    "H-job_logs-via-jobs": ("job_logs", lambda w, s: JobLog(
        org_id=w.org, job_id=s.job, ts=_now(), message="m"
    )),
    "H-script_policies-via-device_groups": ("script_policies", lambda w, s: ScriptPolicy(
        org_id=w.org, name=f"p-{uuid4().hex[:6]}", script_id=w.script, device_group_id=s.group
    )),
    "H-health_alert_action-via-health_alert-via-devices": (
        "health_alert_action",
        lambda w, s: HealthAlertAction(org_id=w.org, alert_id=s.alert, kind="notify"),
    ),
    "C-ticket": ("ticket", lambda w, s: _ticket(w.org, s.client, w.queue, w.category, w.actor)),
    "H-ticket_comment-via-ticket": ("ticket_comment", lambda w, s: TicketComment(
        org_id=w.org, ticket_id=s.ticket, author_id=w.actor, author_type="user", body="b",
        visibility="public",
    )),
    "C-person": ("person", lambda w, s: Person(
        org_id=w.org, client_id=s.client, display_name="p"
    )),
    "H-person_login-via-person": ("person_login", lambda w, s: PersonLogin(
        org_id=w.org, person_id=s.person, login=f"corp\\{uuid4().hex[:8]}"
    )),
    "C-license": ("license", lambda w, s: License(
        org_id=w.org, client_id=s.client, product_id=w.product, type="perpetual",
        entitlements=1, created_by=w.actor,
    )),
    "C-subscription": ("subscription", lambda w, s: _subscription(w.org, s.client, w.actor)),
    "H-subscription_event-via-subscription": ("subscription_event", lambda w, s: (
        SubscriptionEvent(
            org_id=w.org, subscription_id=s.subscription, type="created", created_by=w.actor
        )
    )),
    "C-change_request": ("change_request", lambda w, s: ChangeRequest(
        org_id=w.org, client_id=s.client, type="normal", title="t", description="d",
        risk="low", affected=[], created_by=w.actor,
    )),
    "C-bcp_plan": ("bcp_plan", lambda w, s: _plan(w.org, s.client, w.actor)),
    "H-bcp_test_step-via-bcp_test": ("bcp_test_step", lambda w, s: BcpTestStep(
        org_id=w.org, test_id=s.test, ordinal=1, description="d"
    )),
    "C-budget": ("budget", lambda w, s: _budget(w.org, s.client, w.actor)),
    "C-expense_category": ("expense_category", lambda w, s: _expense_category(
        w.org, s.client, w.actor
    )),
    "H-budget_line-via-budget": ("budget_line", lambda w, s: BudgetLine(
        org_id=w.org, budget_id=s.budget, category_id=s.expense_category,
        period_index=1, planned_amount=1, created_by=w.actor,
    )),
    # The client is never sent: the trigger takes it from the target (0239).
    "C-deployments": ("deployments", lambda w, s: Deployment(
        org_id=w.org, package_id=w.package, action="install", target_type="device",
        target_id=s.device, state="pending",
    )),
    "O-doc_articles-no-parent": ("doc_articles", lambda w, s: DocArticle(
        org_id=w.org, client_id=s.client, title="t"
    )),
    "O-policies-via-device_groups": ("policies", lambda w, s: Policy(
        org_id=w.org, module="patch", config={}, device_group_id=s.group
    )),
    "O-doc_versions-via-record_id": ("doc_versions", lambda w, s: DocVersion(
        org_id=w.org, record_id=s.record, version=1, ts=_now()
    )),
    "O-health_profile_assignment-via-device_group_id": (
        "health_profile_assignment",
        lambda w, s: HealthProfileAssignment(
            org_id=w.org, profile_id=w.profile, device_group_id=s.group
        ),
    ),
    # ONE ADDRESS PER CONTACT (migration 0232's `its_contact_email_unique`,
    # Task 9 review 1): X's and Y's contacts cannot share one. The address is
    # never what a statement here filters on -- every probe names its row by
    # id -- so the client is still the only discriminator that matters.
    "O-users-contacts": ("users", lambda w, s: User(
        org_id=w.org, external_subject=uuid4().hex, email=f"c-{uuid4().hex[:8]}@test.local",
        kind="contact", client_id=s.client,
    )),
}


@pytest.mark.parametrize("path", list(PATHS))
async def test_another_clients_row_is_invisible_and_untouchable(
    path: str, admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Spec §5 test 3 for one path: under a list scope of X, Y's row is not
    read, not changed and not deleted -- each statement matches nothing --
    while X's row is read and changed (the positive control, which proves the
    statement itself is sound). A row of Y is not inserted either. With no
    scope at all (test 5) neither row is read nor changed. Raw SQL, so the
    DATABASE is what refuses, not the ORM guard."""
    table, make = PATHS[path]
    w = await _two_clients(admin_engine)
    x_row, y_row = await _seed(admin_engine, make(w, w.x), make(w, w.y))
    both = f"SELECT id FROM {table} WHERE id IN (:a, :b)"
    touch = f"UPDATE {table} SET org_id = org_id WHERE id = :i"

    async with org_session(w.org, clients=ClientScope.only([w.x.client]), engine=app_engine) as s:
        seen = await _ids(s, both, a=x_row, b=y_row)
        changed = await _rowcount(s, touch, i=y_row)
        deleted = await _rowcount(s, f"DELETE FROM {table} WHERE id = :i", i=y_row)
        own = await _rowcount(s, touch, i=x_row)
    assert seen == {x_row}
    assert (changed, deleted, own) == (0, 0, 1)
    assert await _exists(admin_engine, table, y_row)

    async with _unscoped(w.org, app_engine) as s:
        unscoped = await _ids(s, both, a=x_row, b=y_row)
        unscoped_change = await _rowcount(s, touch, i=x_row)
    assert (unscoped, unscoped_change) == (set(), 0)

    with pytest.raises(DBAPIError, match=GUARDED.get(path, REFUSED)):
        async with org_session(
            w.org, clients=ClientScope.only([w.x.client]), engine=app_engine
        ) as s:
            s.add(make(w, w.y))
            await s.flush()


async def test_a_list_scope_cannot_change_or_delete_another_client(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`clients` itself. What a list scope SEES of it is
    rls::test_the_clients_table_shows_a_list_scope_its_own_clients_only; this
    is what it may not do to Y, nor to a client it creates."""
    w = await _two_clients(admin_engine)
    async with org_session(w.org, clients=ClientScope.only([w.x.client]), engine=app_engine) as s:
        changed = await _rowcount(
            s, "UPDATE clients SET name = 'renamed' WHERE id = :i", i=w.y.client
        )
        deleted = await _rowcount(s, "DELETE FROM clients WHERE id = :i", i=w.y.client)
        own = await _rowcount(s, "UPDATE clients SET name = name WHERE id = :i", i=w.x.client)
    assert (changed, deleted, own) == (0, 0, 1)
    async with AsyncSession(admin_engine) as s:
        name = (await s.execute(select(Client.name).where(Client.id == w.y.client))).scalar_one()
    assert name != "renamed"
    with pytest.raises(DBAPIError, match=REFUSED):
        async with org_session(
            w.org, clients=ClientScope.only([w.x.client]), engine=app_engine
        ) as s:
            s.add(Client(org_id=w.org, name="Nuevo", kind="customer"))
            await s.flush()


MakeGlobal = Callable[[World], list[Any]]


def _global_record_version(w: World) -> list[Any]:
    record = DocRecord(id=uuid4(), org_id=w.org, record_type="service", name="global")
    return [record, DocVersion(org_id=w.org, record_id=record.id, version=1, ts=_now())]


#: (table, rows to seed, the global one last). The O paths
#: `tests/test_client_scope_rls.py` does not cover: there, articles,
#: credentials, domain events and staff rows.
GLOBAL_ROWS: dict[str, tuple[str, MakeGlobal]] = {
    "O-policies-no-group": ("policies", lambda w: [
        Policy(org_id=w.org, module="patch", config={})
    ]),
    "O-doc_versions-of-a-global-record": ("doc_versions", _global_record_version),
    "O-health_profile_assignment-org-default": ("health_profile_assignment", lambda w: [
        HealthProfileAssignment(org_id=w.org, profile_id=w.profile)
    ]),
}


@pytest.mark.parametrize("path", list(GLOBAL_ROWS))
async def test_a_global_row_is_read_but_not_changed(
    path: str, admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Spec §5 test 3's O half: a global row (no client, "every client") is
    READ by a list scope and changed or deleted only under scope all -- a
    DELETE checks no WITH CHECK, which is why 0231 gives O tables policies of
    their own. With no scope (test 5) it is not read at all."""
    table, make = GLOBAL_ROWS[path]
    w = await _two_clients(admin_engine)
    rows = make(w)
    *_, row = await _seed(admin_engine, *rows)
    one = f"SELECT id FROM {table} WHERE id = :i"
    touch = f"UPDATE {table} SET org_id = org_id WHERE id = :i"

    async with org_session(w.org, clients=ClientScope.only([w.x.client]), engine=app_engine) as s:
        seen = await _ids(s, one, i=row)
        changed = await _rowcount(s, touch, i=row)
        deleted = await _rowcount(s, f"DELETE FROM {table} WHERE id = :i", i=row)
    assert seen == {row}
    assert (changed, deleted) == (0, 0)
    async with _unscoped(w.org, app_engine) as s:
        assert await _ids(s, one, i=row) == set()
    async with AsyncSession(admin_engine) as s:
        client = (
            await s.execute(text(f"SELECT client_id FROM {table} WHERE id = :i"), {"i": row})
        ).scalar_one()
    assert client is None  # still there, still global
    async with org_session(w.org, clients=ClientScope.all(), engine=app_engine) as s:
        assert await _rowcount(s, touch, i=row) == 1


async def test_a_restricted_scope_reads_global_config_but_cannot_write_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """An O table with no parent, by raw INSERT: the ORM guard
    (`app.core.global_rows`) would refuse first and hide a missing policy."""
    w = await _two_clients(admin_engine)
    insert = (
        "INSERT INTO software_ignore (id, org_id, name, created_by) "
        "VALUES (gen_random_uuid(), :o, :n, :u)"
    )
    async with AsyncSession(admin_engine) as s:
        await s.execute(text(insert), {"o": w.org, "n": "g", "u": w.actor})
        await s.commit()
    async with org_session(w.org, clients=ClientScope.only([w.x.client]), engine=app_engine) as s:
        assert (await s.execute(text("SELECT count(*) FROM software_ignore"))).scalar() == 1
    with pytest.raises(DBAPIError, match=REFUSED):
        async with org_session(
            w.org, clients=ClientScope.only([w.x.client]), engine=app_engine
        ) as s:
            await s.execute(text(insert), {"o": w.org, "n": "g2", "u": w.actor})
    async with org_session(w.org, clients=ClientScope.all(), engine=app_engine) as s:
        await s.execute(text(insert), {"o": w.org, "n": "g2", "u": w.actor})


# --------------------------------------------------------------------------- #
# Spec §5 test 4: the code's own predicate, with no RLS to mask it.
# --------------------------------------------------------------------------- #


async def test_the_codes_own_client_predicate_holds_without_rls(
    admin_engine: AsyncEngine,
) -> None:
    """RLS masks a missing APPLICATION predicate (HANDOFF.md §4): under a list
    scope of X, Y's group is invisible, so a name check that forgot its client
    clause would pass anyway. Through the ADMIN engine -- no RLS at all -- the
    code's own predicate is all there is: a name Y uses does not block X, and
    does block Y."""
    w = await _two_clients(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        s.add(DeviceGroup(org_id=w.org, client_id=w.y.client, name="Solo de Y"))
        await s.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as s, s.begin():
        made = await create_device_group(
            s, org_id=w.org, client_id=w.x.client, actor_id=w.actor, name="Solo de Y"
        )
    assert made.client_id == w.x.client
    with pytest.raises(DeviceGroupError) as taken:
        async with AsyncSession(admin_engine) as s, s.begin():
            await create_device_group(
                s, org_id=w.org, client_id=w.y.client, actor_id=w.actor, name="Solo de Y"
            )
    assert taken.value.status_code == 409


# --------------------------------------------------------------------------- #
# Spec §5 test 6: the header narrows, never widens.
# --------------------------------------------------------------------------- #


async def test_a_client_header_outside_the_scope_lists_nothing_and_404s(
    api_env: ApiEnv,
) -> None:
    """A technician holding every code on X asks for Y with `X-Client-Id`: the
    list is empty and the detail of EITHER client's device is a 404 -- the
    empty scope, never the person's own. Asking for X shows X's device only."""
    w = await _two_clients(api_env.admin_engine)
    tech = await everything_on(api_env, w.org, w.x.client)

    async def get(path: str, client: UUID) -> Any:
        return await api_env.client.get(path, headers={**tech, "X-Client-Id": str(client)})

    listed = await get("/api/v1/devices", w.y.client)
    assert listed.status_code == 200, listed.text
    assert (listed.json()["items"], listed.json()["total"]) == ([], 0)
    for device in (w.x.device, w.y.device):
        assert (await get(f"/api/v1/devices/{device}", w.y.client)).status_code == 404

    listed = await get("/api/v1/devices", w.x.client)
    assert [d["id"] for d in listed.json()["items"]] == [str(w.x.device)]
    assert (await get(f"/api/v1/devices/{w.x.device}", w.x.client)).status_code == 200
    assert (await get(f"/api/v1/devices/{w.y.device}", w.x.client)).status_code == 404


# --------------------------------------------------------------------------- #
# Spec §5 tests 7 and 8: inheritance and immutability.
# --------------------------------------------------------------------------- #

MakeChild = Callable[[World, "UUID | None"], Any]

#: Children of X's device, with the client the statement claims (None: none).
CHILDREN: dict[str, MakeChild] = {
    "device_tags": lambda w, claimed: DeviceTag(
        org_id=w.org, device_id=w.x.device, key=f"k-{uuid4().hex[:6]}", client_id=claimed
    ),
    # "La alerta de un equipo de X queda en X."
    "health_alert": lambda w, claimed: HealthAlert(
        org_id=w.org, device_id=w.x.device, check_id=w.check, profile_id=w.profile,
        check_type="cpu.busy_pct", severity="warning", selector=uuid4().hex,
        client_id=claimed,
    ),
}


@pytest.mark.parametrize("table", list(CHILDREN))
async def test_a_child_inherits_and_cannot_contradict(
    table: str, admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Spec §5 test 7 and §2.4, on the INSERT (the UPDATE is
    rls::test_an_inherited_client_cannot_be_rewritten): a child of X's device
    is X's without being told, and one that claims Y is refused -- under
    scope all, where the client policy would let Y through."""
    make = CHILDREN[table]
    w = await _two_clients(admin_engine)
    async with org_session(w.org, clients=ClientScope.all(), engine=app_engine) as s:
        child = make(w, None)
        s.add(child)
        await s.flush()
        await s.refresh(child, ["client_id"])
        assert child.client_id == w.x.client
    with pytest.raises(DBAPIError, match="differs"):
        async with org_session(w.org, clients=ClientScope.all(), engine=app_engine) as s:
            s.add(make(w, w.y.client))
            await s.flush()


async def test_a_device_cannot_change_client(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Spec §5 test 8 and §1.5, under scope all: a device's client never
    changes; its site may change within that client, never to another's."""
    w = await _two_clients(admin_engine)
    (second,) = await _seed(admin_engine, Site(org_id=w.org, client_id=w.x.client, name="Sede 2"))
    async with org_session(w.org, clients=ClientScope.all(), engine=app_engine) as s:
        moved = await _rowcount(
            s, "UPDATE devices SET site_id = :site WHERE id = :d", site=second, d=w.x.device
        )
    assert moved == 1
    refusals = [
        ("cannot move to another client", "client_id = :y", {"y": w.y.client}),
        ("cannot move to another client", "client_id = :y, site_id = :s",
         {"y": w.y.client, "s": w.y.site}),
        ("device site belongs to another client", "site_id = :s", {"s": w.y.site}),
    ]
    for message, assignment, params in refusals:
        with pytest.raises(DBAPIError, match=message):
            async with org_session(w.org, clients=ClientScope.all(), engine=app_engine) as s:
                await s.execute(
                    text(f"UPDATE devices SET {assignment} WHERE id = :d"),
                    {**params, "d": w.x.device},
                )
    async with AsyncSession(admin_engine) as s:
        placed = (
            await s.execute(
                select(Device.client_id, Device.site_id).where(Device.id == w.x.device)
            )
        ).one()
    assert tuple(placed) == (w.x.client, second)
