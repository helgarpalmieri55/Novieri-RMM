# End-user Portal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the person who reported a problem a place to raise it, watch it, and reply to it — without any view of the fleet, of other people's tickets, or of internal staff discussion.

**Architecture:** A second, tightly-scoped surface onto the ticket core. Row-level tenancy is a *database* guarantee: a new `portal_user` Postgres role carries policies keyed on both an org GUC and a requester GUC, fail-closed when either is unset. The module adds no tables, no columns, and no ticket rules — it calls ticket-core services under a portal-scoped session.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x (typed), Pydantic v2, Alembic, PostgreSQL RLS. Frontend: React + TypeScript (strict), Vite, TanStack Query.

## Global Constraints

- **Migration head is `0027`.** This module's migration is `0028`. Task 1 confirms the head before writing it. Migrations are forward-only — never edit a shipped one.
- **Backend suite is at 2070 passing; frontend at 199.** Every task ends green. Gates: `.venv/bin/python -m pytest tests/ -q`, `.venv/bin/python -m mypy app/`, `.venv/bin/python -m ruff check app/ tests/`, and in `frontend/`: `npx tsc --noEmit`, `npm test`.
- **Environment first, every session:** `pg_ctlcluster 16 main start ; redis-server --daemonize yes`
- **Run gates in the FOREGROUND** as plain blocking commands with an explicit long timeout. The backend suite takes ~5 minutes. Do not background it, sleep-poll, or launch monitors — three agents on the previous sub-project lost hours that way.
- **Use `.venv/bin/python`, never bare `python`.** Run backend commands from `backend/`. Use absolute paths.
- **Type hints mandatory**; mypy strict. No `any` in TypeScript.
- **This module adds NO tables and NO columns.** If you find yourself writing `op.create_table`, stop — you have misread the spec.
- **`app.modules.portal` calls ticket-core services; it never reimplements them.** Ticket creation goes through `tickets.create_ticket`, comments through `threads.add_comment`, attachments through `threads.add_attachment`.
- **`requester_id`/`created_by` come from `PortalContext`, never from a request body** — the same rule staff routes follow for `org_id`/`actor_id`.
- **A ticket that is not the requester's returns 404, never 403.** A 403 confirms the id exists, which is a cross-requester existence oracle.
- **Real table names, all singular:** `ticket`, `ticket_comment`, `ticket_attachment`, `ticket_status_history`, `ticket_approval`, `ticket_category`, `ticket_number_counter`, `ticket_queue`. There is no `tickets`, no `ticket_queues`. Getting this wrong cost the previous sub-project a broken migration.
- **Comment visibility column is `visibility`, `String(16)`, default `"public"`.** Not `is_internal`.
- **The tenancy GUC is `app.current_org`**, set by `org_session` via `set_config(..., true)` (transaction-local). The portal adds `app.current_requester` the same way. There is no `app.org_id`.
- **`ticket_number_counter` needs INSERT as well as SELECT/UPDATE.** `numbering.py` creates the counter row lazily with `on_conflict_do_nothing` before its atomic `UPDATE ... RETURNING`; without INSERT the first portal ticket in an org fails.
- **Test fixture pattern:** `async def test_x(api_env)` with `async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:`, plus `await api_env.create_org("prefix")` and `api_env.create_user(org_id, *role_names)`. `asyncio_mode` is auto — do NOT add `@pytest.mark.asyncio`.
- **`backend/tests/test_rls.py` auto-covers every table with an `org_id` column** for `ENABLE + FORCE` and a `tenant_isolation` policy. The portal's policies are *additional* policies on existing tables and must not disturb it.

### The portal's policies MUST be `AS RESTRICTIVE` — this is not optional

The existing `tenant_isolation` policy is `PERMISSIVE` and its `roles` column is
`{public}`, so it applies to **every** role including `portal_user`. Postgres
combines permissive policies with **OR**. A permissive `portal_isolation` policy
would therefore be OR'd with `tenant_isolation`, `portal_user` would satisfy the
org-only predicate, and **the portal would see every ticket in the org**.

This was verified empirically against the real database before this plan was
written: with a permissive portal policy the portal role saw 2 of 2 rows; with
`AS RESTRICTIVE TO portal_user` it saw 1 of 2, staff still saw 2, and a session
that never set the requester GUC saw 0.

Two consequences the implementer must not undo:

- Every portal policy is declared `AS RESTRICTIVE TO portal_user`. Restrictive
  policies are AND'd with the permissive result and apply only to the roles they
  name, which is exactly the required semantics and is why staff behaviour is
  untouched.
- A restrictive policy therefore only needs the **requester** half of the
  predicate — `tenant_isolation` already supplies org scope. Repeating the org
  check is harmless but redundant.

**Why this warning is here at all:** a `pg_policies`-shape test cannot catch the
mistake. With a permissive policy every assertion about the policy's existence
and its predicate text still passes, and the portal still leaks the whole org.
Only a behavioural test with two requesters catches it — Task 1 asserts
`permissive = 'RESTRICTIVE'` explicitly, and Task 2's isolation test is the real
gate.

### A pre-existing deployment gap this plan works around

`deploy/postgres/init-app-user.sh` grants `app_user` only `CONNECT`. The table-level `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public` exists **only in `tests/conftest.py`**, applied after migrations during test-cluster setup. Verified against the migrated dev database: only `its_admin` holds privileges on `ticket`.

That means a real deployment has an `app_user` with no table privileges, and the test suite masks it. **This is pre-existing and out of scope here** — do not fix `app_user` in this plan.

The consequence for this plan is a decision, not a workaround: **`portal_user`'s role creation and its table GRANTs go in migration `0028`**, not in conftest and not in the deploy script. A migration always runs wherever the schema does, so the portal is correctly provisioned in every environment. Task 1 also adds the role to `tests/conftest.py`'s grant block *and* to `deploy/postgres/init-app-user.sh` for the LOGIN/password part (a migration cannot know a deployment's password), and the report must state plainly that `app_user`'s gap remains open.

---

## File structure

**Backend:**

| File | Responsibility |
|---|---|
| `backend/migrations/versions/0028_portal.py` | `portal_user` role, its GRANTs, its RLS policies, `requester` role + two permission codes |
| `backend/app/core/tenancy.py` (modify) | add `portal_session()` beside `org_session()` |
| `backend/app/core/db.py` (modify) | add `get_portal_engine()` beside `get_engine()`/`get_admin_engine()` |
| `backend/app/core/config.py` (modify) | add `database_portal_url` setting |
| `backend/app/modules/portal/principal.py` | `PortalContext`, `resolve_portal_principal` — the auth seam, one function |
| `backend/app/modules/portal/schema.py` | portal-specific narrow response models |
| `backend/app/modules/portal/tickets.py` | portal read/write services; call ticket-core services |
| `backend/app/modules/portal/api.py` | the 7 routes, registration, error translation |

**Frontend** (`frontend/src/modules/portal/`): `types.ts`, `api.ts`, `MyTicketsPage.tsx`, `PortalTicketDetailPage.tsx`, `NewTicketPage.tsx`, `PortalNav.tsx`, `__tests__/portal.test.tsx`.

Split rationale: `principal.py` is separate from `api.py` because it is the documented extension point for an external credential store (spec §0.1) — a future implementer should find one file, not a function buried in a router. `tickets.py` is separate from `api.py` so the 404-not-403 rule and the service-reuse rule live in one testable place rather than in route handlers.

---

## Task 1: Migration 0028 — the `portal_user` role, its policies, and the requester tier

This task is the security foundation. Everything else depends on getting it right.

**Files:**
- Create: `backend/migrations/versions/0028_portal.py`
- Modify: `backend/tests/conftest.py` (add `portal_user` to the grant block and expose a portal URL)
- Modify: `deploy/postgres/init-app-user.sh` (create the role with a password in real deployments)
- Modify: `backend/app/core/rbac.py` (add the two permission codes)
- Test: `backend/tests/test_portal_migration.py`

**Interfaces:**
- Consumes: nothing.
- Produces: DB role `portal_user`; policies named `portal_isolation` on seven tables; role `requester`; permissions `portal.ticket.read`, `portal.ticket.write`.

- [ ] **Step 1: Confirm the migration head**

```bash
cd /home/user/Toeshee-RMS/backend && .venv/bin/python -m alembic heads
```

Expected: `0027 (head)`. If it is anything else, stop — renumber before continuing.

- [ ] **Step 2: Write the failing test**

```python
# backend/tests/test_portal_migration.py
"""Migration 0028: the portal_user role, its policies, and the requester tier.

The privilege tests here are the point of this file. A policy cannot restrict a
table the role has no grant on, and a grant with no policy exposes the whole
org -- both layers are required and both are asserted.
"""

import pytest
from sqlalchemy import text

# Spec §2.1. Note what is ABSENT: ticket_queue. A requester never chooses a
# queue, so portal_user gets no access to it at all.
PORTAL_READABLE = [
    "ticket",
    "ticket_comment",
    "ticket_attachment",
    "ticket_status_history",
    "ticket_approval",
    "ticket_category",
    "ticket_number_counter",
]


async def test_portal_user_role_exists(db_session) -> None:
    got = await db_session.execute(
        text("SELECT 1 FROM pg_roles WHERE rolname = 'portal_user'")
    )
    assert got.scalar() == 1


async def test_portal_user_is_not_bypassrls(db_session) -> None:
    """The whole design rests on RLS applying to this role."""
    got = await db_session.execute(
        text("SELECT rolbypassrls FROM pg_roles WHERE rolname = 'portal_user'")
    )
    assert got.scalar() is False


@pytest.mark.parametrize("table", PORTAL_READABLE)
async def test_portal_user_can_select_the_tables_it_needs(db_session, table: str) -> None:
    got = await db_session.execute(
        text("SELECT has_table_privilege('portal_user', :t, 'SELECT')"), {"t": table}
    )
    assert got.scalar() is True, f"portal_user cannot SELECT {table}"


async def test_portal_user_can_read_but_not_write_ticket_queue(db_session) -> None:
    """AMENDED DURING EXECUTION (migration 0029). The original plan asserted
    NO access here, reasoning that a requester never chooses a queue. That is
    true of the API surface and false of the implementation: `ticket.queue_id`
    is NOT NULL and `tickets.create_ticket` resolves it via
    `refdata.resolve_queue`, which issues `select(TicketQueue)` -- with no grant
    that raises `permission denied for table ticket_queue` and EVERY portal
    ticket creation fails with a 500.

    Read-only, and no portal_isolation policy: a queue is org refdata, and a
    restrictive requester predicate on it would make resolution impossible. A
    requester still never SEES a queue, because the portal's response models
    carry no queue field.
    """
    got = await db_session.execute(
        text("SELECT has_table_privilege('portal_user', 'ticket_queue', 'SELECT')")
    )
    assert got.scalar() is True
    for priv in ("INSERT", "UPDATE", "DELETE"):
        got = await db_session.execute(
            text("SELECT has_table_privilege('portal_user', 'ticket_queue', :p)"), {"p": priv}
        )
        assert got.scalar() is False, f"portal_user can {priv} ticket_queue"


async def test_portal_user_can_insert_where_it_must(db_session) -> None:
    for table in ("ticket", "ticket_comment", "ticket_attachment", "ticket_number_counter"):
        got = await db_session.execute(
            text("SELECT has_table_privilege('portal_user', :t, 'INSERT')"), {"t": table}
        )
        assert got.scalar() is True, f"portal_user cannot INSERT {table}"


async def test_portal_user_can_update_the_number_counter_only(db_session) -> None:
    """numbering.py does INSERT ... ON CONFLICT DO NOTHING then UPDATE ...
    RETURNING. Both are required, and UPDATE is required NOWHERE else -- a
    requester never edits a ticket row after creating it."""
    got = await db_session.execute(
        text("SELECT has_table_privilege('portal_user', 'ticket_number_counter', 'UPDATE')")
    )
    assert got.scalar() is True
    got = await db_session.execute(
        text("SELECT has_table_privilege('portal_user', 'ticket', 'UPDATE')")
    )
    assert got.scalar() is False, "a requester must not be able to edit a ticket row"


async def test_portal_user_cannot_delete_anything(db_session) -> None:
    for table in PORTAL_READABLE:
        got = await db_session.execute(
            text("SELECT has_table_privilege('portal_user', :t, 'DELETE')"), {"t": table}
        )
        assert got.scalar() is False, f"portal_user can DELETE {table}"


# Only these five carry a portal policy. ticket_category and
# ticket_number_counter deliberately do not -- see the migration's comment.
PORTAL_POLICIED = [
    "ticket",
    "ticket_comment",
    "ticket_attachment",
    "ticket_status_history",
    "ticket_approval",
]


@pytest.mark.parametrize("table", PORTAL_POLICIED)
async def test_every_portal_table_has_a_portal_policy(db_session, table: str) -> None:
    got = await db_session.execute(
        text(
            "SELECT COUNT(*) FROM pg_policies "
            "WHERE tablename = :t AND policyname = 'portal_isolation'"
        ),
        {"t": table},
    )
    assert got.scalar() == 1, f"{table} has no portal_isolation policy"


@pytest.mark.parametrize("table", PORTAL_POLICIED)
async def test_portal_policies_are_restrictive(db_session, table: str) -> None:
    """THE MOST IMPORTANT ASSERTION IN THIS FILE.

    tenant_isolation is PERMISSIVE with roles={public}, so it also covers
    portal_user, and Postgres OR's permissive policies. A PERMISSIVE portal
    policy would be OR'd with the org-only predicate and portal_user would see
    EVERY ticket in the org -- while every other assertion in this file still
    passed. Verified empirically: permissive -> 2 of 2 rows; restrictive -> 1.
    """
    got = await db_session.execute(
        text(
            "SELECT permissive FROM pg_policies "
            "WHERE tablename = :t AND policyname = 'portal_isolation'"
        ),
        {"t": table},
    )
    assert got.scalar() == "RESTRICTIVE", f"{table}'s portal policy is not RESTRICTIVE"


@pytest.mark.parametrize("table", PORTAL_POLICIED)
async def test_portal_policies_apply_only_to_portal_user(db_session, table: str) -> None:
    """A restrictive policy with no TO clause would apply to app_user too and
    break every staff route on these tables."""
    got = await db_session.execute(
        text(
            "SELECT roles FROM pg_policies "
            "WHERE tablename = :t AND policyname = 'portal_isolation'"
        ),
        {"t": table},
    )
    assert got.scalar() == ["portal_user"]


async def test_ticket_portal_policy_scopes_by_requester(db_session) -> None:
    """The restrictive policy supplies the REQUESTER half; org scope comes from
    the permissive tenant_isolation policy it is AND'd with."""
    got = await db_session.execute(
        text(
            "SELECT qual FROM pg_policies "
            "WHERE tablename = 'ticket' AND policyname = 'portal_isolation'"
        )
    )
    assert "app.current_requester" in (got.scalar() or "")


async def test_comment_portal_policy_filters_visibility(db_session) -> None:
    """Spec §2.3 -- the internal-note guard lives in the POLICY, so no future
    portal query can expose one."""
    got = await db_session.execute(
        text(
            "SELECT qual FROM pg_policies "
            "WHERE tablename = 'ticket_comment' AND policyname = 'portal_isolation'"
        )
    )
    assert "public" in (got.scalar() or "")


async def test_comment_insert_is_constrained_to_public(db_session) -> None:
    """Spec §2.2 -- WITH CHECK, so a requester cannot WRITE an internal note
    even if application code were wrong."""
    got = await db_session.execute(
        text(
            "SELECT with_check FROM pg_policies "
            "WHERE tablename = 'ticket_comment' AND policyname = 'portal_isolation'"
        )
    )
    assert "public" in (got.scalar() or "")


async def test_ticket_insert_forces_the_requester(db_session) -> None:
    """Spec §2.2 -- a requester cannot file a ticket naming somebody else."""
    got = await db_session.execute(
        text(
            "SELECT with_check FROM pg_policies "
            "WHERE tablename = 'ticket' AND policyname = 'portal_isolation'"
        )
    )
    assert "app.current_requester" in (got.scalar() or "")


async def test_requester_role_exists_and_holds_only_portal_permissions(db_session) -> None:
    """Spec §4 -- ticket.read is ORG-WIDE. A requester holding it would defeat
    the module regardless of RLS, because it also gates staff routes."""
    got = await db_session.execute(
        text(
            "SELECT p.code FROM roles r "
            "JOIN role_permissions rp ON rp.role_id = r.id "
            "JOIN permissions p ON p.id = rp.permission_id "
            "WHERE r.name = 'requester' AND r.org_id IS NULL"
        )
    )
    codes = sorted(row[0] for row in got.all())
    assert codes == ["portal.ticket.read", "portal.ticket.write"]
```

`db_session` here is whatever fixture the existing migration tests use — read `backend/tests/test_notification_migration.py` and match it exactly rather than inventing one.

- [ ] **Step 3: Run it to verify it fails**

Run: `cd /home/user/Toeshee-RMS/backend && .venv/bin/python -m pytest tests/test_portal_migration.py -q`
Expected: FAIL — no `portal_user` role.

- [ ] **Step 4: Write the migration**

```python
# backend/migrations/versions/0028_portal.py
"""end-user portal: portal_user role, row-scoped policies, requester tier

Revision ID: 0028
Revises: 0027

WHY THE ROLE AND ITS GRANTS LIVE IN A MIGRATION. `app_user`'s table privileges
are granted in `tests/conftest.py` and nowhere else -- `deploy/postgres/
init-app-user.sh` grants it only CONNECT -- so a real deployment has an
app_user with no table privileges and the test suite masks it. That gap is
pre-existing and not fixed here, but this module must not replicate it: a
migration runs wherever the schema does, so portal_user is correctly
provisioned in every environment. The deploy script still creates the role
with a LOGIN password, which a migration cannot know.
"""

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

# Spec §2.1, amended by migration 0029: ticket_queue gets SELECT (read-only,
# no portal policy). A requester never CHOOSES a queue, but create_ticket must
# RESOLVE one -- queue_id is NOT NULL -- and resolve_queue reads the table.
_ORG_GUC = "current_setting('app.current_org', true)::uuid"
_REQ_GUC = "current_setting('app.current_requester', true)::uuid"

# EVERY policy below is `AS RESTRICTIVE TO portal_user`. The existing
# tenant_isolation policy is PERMISSIVE with roles={public}, so it also covers
# portal_user, and Postgres OR's permissive policies together -- a permissive
# portal policy would be OR'd with the org-only predicate and the portal would
# see EVERY ticket in the org. Verified empirically: permissive -> 2 of 2 rows
# visible; restrictive -> 1 of 2, staff unaffected at 2, requester-GUC-unset 0.
#
# Because tenant_isolation already supplies org scope and restrictive policies
# are AND'd, these predicates carry only the REQUESTER half.
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
        "DELETE FROM role_permissions WHERE role_id IN "
        "(SELECT id FROM roles WHERE name = 'requester' AND org_id IS NULL)"
    )
    op.execute("DELETE FROM roles WHERE name = 'requester' AND org_id IS NULL")
    codes = ", ".join(f"'{c}'" for c, _, _ in PERMISSIONS)
    op.execute(f"DELETE FROM permissions WHERE code IN ({codes})")
```

Read `backend/migrations/versions/0025_notification_core.py` first and match its `revision`/`down_revision` style exactly (bare numbers) and its permission-seeding shape.

- [ ] **Step 5: Add the permission codes to `rbac.py`**

```python
    "portal.ticket.read": "Portal: view your OWN tickets and their public history",
    "portal.ticket.write": (
        "Portal: file a ticket, comment on your own, attach to your own. "
        "END-USER TIER: deliberately NOT `ticket.write`, which is org-wide and "
        "gates staff routes (spec §4)."
    ),
```

- [ ] **Step 6: Wire the role into the test cluster and the deploy script**

In `backend/tests/conftest.py`'s grant block, create `portal_user` with a login password and grant `CONNECT`, then expose `portal_url` on `PgCluster` alongside `app_url`/`admin_url`. The migration does the table GRANTs; conftest must not duplicate them.

In `deploy/postgres/init-app-user.sh`, add the role with a password from the environment, mirroring `app_user`:

```bash
    CREATE ROLE portal_user LOGIN PASSWORD '${PORTAL_DB_PASSWORD:?PORTAL_DB_PASSWORD must be set}'
        NOSUPERUSER NOBYPASSRLS;
    GRANT CONNECT ON DATABASE itsuite TO portal_user;
```

Add `PORTAL_DB_PASSWORD` to `deploy/.env.example` and note it in `deploy/README.md`.

- [ ] **Step 7: Migrate and run the tests**

```bash
cd /home/user/Toeshee-RMS/backend
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m pytest tests/test_portal_migration.py tests/test_rls.py tests/test_rbac.py -q
```

Expected: PASS. `test_rls.py` must still pass — the new policies are additional and must not disturb its `ENABLE + FORCE` / `tenant_isolation` coverage.

- [ ] **Step 8: Commit**

```bash
git add backend/migrations/versions/0028_portal.py backend/app/core/rbac.py \
        backend/tests/conftest.py backend/tests/test_portal_migration.py \
        deploy/postgres/init-app-user.sh deploy/.env.example deploy/README.md
git commit -m "feat(portal): migration 0028 -- portal_user role, row-scoped policies, requester tier"
```

---

## Task 2: `portal_session` and the portal engine

**Files:**
- Modify: `backend/app/core/config.py` (add `database_portal_url`)
- Modify: `backend/app/core/db.py` (add `get_portal_engine`)
- Modify: `backend/app/core/tenancy.py` (add `portal_session`)
- Test: `backend/tests/test_portal_session.py`

**Interfaces:**
- Consumes: Task 1's role and policies.
- Produces: `async def portal_session(org_id: UUID, requester_id: UUID, *, engine: AsyncEngine | None = None) -> AsyncIterator[AsyncSession]` (an `@asynccontextmanager`), and `db.get_portal_engine() -> AsyncEngine`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_portal_session.py
"""portal_session: the ONLY way a portal request reaches the database.

The fail-closed test is the point of this file. If portal_session sets the org
GUC but not the requester GUC, a portal user sees every ticket in the org --
and every other test here would still pass.
"""

from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import org_session, portal_session
from app.models import Ticket


async def test_portal_session_sets_both_gucs(api_env, portal_engine) -> None:
    org_id = await api_env.create_org("portal-guc")
    requester_id = uuid4()
    async with portal_session(org_id, requester_id, engine=portal_engine) as s:
        got = await s.execute(
            text(
                "SELECT current_setting('app.current_org', true), "
                "       current_setting('app.current_requester', true)"
            )
        )
        org_guc, req_guc = got.one()
    assert org_guc == str(org_id)
    assert req_guc == str(requester_id)


async def test_portal_session_sees_only_the_requesters_own_tickets(
    api_env, portal_engine, a_ticket_for
) -> None:
    """Spec §9.2 -- cross-requester isolation. Must fail if the policy's
    requester predicate is removed."""
    org_id = await api_env.create_org("portal-iso")
    mine = await a_ticket_for(org_id, "me")
    theirs = await a_ticket_for(org_id, "them")

    async with portal_session(org_id, mine.requester_id, engine=portal_engine) as s:
        rows = (await s.execute(text("SELECT id FROM ticket"))).scalars().all()
    ids = {str(r) for r in rows}
    assert str(mine.id) in ids
    assert str(theirs.id) not in ids, "saw another requester's ticket"


async def test_a_portal_session_with_no_requester_sees_nothing(
    api_env, portal_engine, a_ticket_for
) -> None:
    """Spec §9.1 -- FAIL-CLOSED, the claim the whole design rests on.

    Connect as portal_user and set ONLY the org GUC, simulating a bug that
    forgot the requester context. The result must be zero rows, not the org's
    tickets.
    """
    org_id = await api_env.create_org("portal-failclosed")
    await a_ticket_for(org_id, "me")

    async with AsyncSession(portal_engine, expire_on_commit=False) as s:
        async with s.begin():
            await s.execute(
                text("SELECT set_config('app.current_org', :o, true)"), {"o": str(org_id)}
            )
            rows = (await s.execute(text("SELECT id FROM ticket"))).scalars().all()
    assert rows == [], "portal_user saw rows with no requester context set"


async def test_staff_sessions_are_unaffected(api_env, app_engine, a_ticket_for) -> None:
    """Spec §9.10 -- the new policies are TO portal_user, so app_user still
    sees every ticket in its org."""
    org_id = await api_env.create_org("portal-staff")
    await a_ticket_for(org_id, "me")
    await a_ticket_for(org_id, "them")
    async with org_session(org_id, engine=app_engine) as s:
        rows = (await s.execute(text("SELECT id FROM ticket"))).scalars().all()
    assert len(rows) == 2
```

You will need a `portal_engine` fixture (an `AsyncEngine` on `PgCluster.portal_url`) and an `a_ticket_for(org_id, who)` helper that creates a user and a ticket for them via the admin engine. Add both to `conftest.py` following the shape of the existing `app_engine`/`admin_engine` fixtures.

- [ ] **Step 2: Run to verify failure**

Run: `cd /home/user/Toeshee-RMS/backend && .venv/bin/python -m pytest tests/test_portal_session.py -q`
Expected: FAIL — `ImportError: cannot import name 'portal_session'`.

- [ ] **Step 3: Add the setting and the engine**

In `app/core/config.py`, beside `database_url`/`database_admin_url`:

```python
    database_portal_url: str
```

In `app/core/db.py`, mirroring `get_admin_engine`:

```python
_portal_engine: AsyncEngine | None = None


def get_portal_engine() -> AsyncEngine:
    """The RLS-constrained, row-scoped role used ONLY by portal requests.

    A third engine rather than a reused one: portal_user's policies are keyed
    on a requester GUC that staff sessions never set, so the two connection
    pools must not be shared or a portal request could land on a staff
    connection.
    """
    global _portal_engine
    if _portal_engine is None:
        _portal_engine = create_async_engine(
            get_settings().database_portal_url, pool_pre_ping=True
        )
    return _portal_engine
```

- [ ] **Step 4: Add `portal_session`**

In `app/core/tenancy.py`, beside `org_session`:

```python
@asynccontextmanager
async def portal_session(
    org_id: UUID, requester_id: UUID, *, engine: AsyncEngine | None = None
) -> AsyncIterator[AsyncSession]:
    """The ONLY way a portal request may reach the database.

    Connects as `portal_user` and pins BOTH tenant GUCs with SET LOCAL, so the
    database itself constrains every statement in the block to one
    organization AND one requester.

    Both GUCs are load-bearing. `portal_user`'s policies compare against each
    of them; if either is unset, `current_setting(..., true)` returns NULL, the
    comparison is NULL rather than true, and no row matches. A bug that forgets
    the requester context therefore yields an EMPTY portal, never another
    person's tickets.

    A portal route that used `org_session` instead would connect as `app_user`
    and see the whole org -- `tests/test_portal_api.py` enforces that no portal
    route does this.
    """
    engine = engine or db.get_portal_engine()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.current_org', :org, true)"),
                {"org": str(org_id)},
            )
            await session.execute(
                text("SELECT set_config('app.current_requester', :req, true)"),
                {"req": str(requester_id)},
            )
            yield session
```

- [ ] **Step 5: Run the tests**

Run: `cd /home/user/Toeshee-RMS/backend && .venv/bin/python -m pytest tests/test_portal_session.py -q`
Expected: PASS (4 tests).

- [ ] **Step 6: Mutation-check the fail-closed claim**

The fail-closed property is the design's foundation. Verify the test actually holds it:

```bash
cd /home/user/Toeshee-RMS/backend
cp migrations/versions/0028_portal.py /tmp/0028.bak
# Weaken the ticket policy to org-only, as a forgotten requester predicate would.
sed -i 's|_MINE = f"org_id = {_ORG_GUC} AND requester_id = {_REQ_GUC}"|_MINE = f"org_id = {_ORG_GUC}"|' migrations/versions/0028_portal.py
.venv/bin/python -m pytest tests/test_portal_session.py -q
```

Expected: **FAIL** on both `test_portal_session_sees_only_the_requesters_own_tickets` and `test_a_portal_session_with_no_requester_sees_nothing`. If either passes, that test is not doing its job — fix the test before restoring.

```bash
cp /tmp/0028.bak migrations/versions/0028_portal.py && rm /tmp/0028.bak
.venv/bin/python -m pytest tests/test_portal_session.py -q   # back to PASS
```

Note: the migration must be re-applied to a fresh test database for the mutation to take effect — the test fixture recreates the cluster per run, so this works as written. Confirm the failure is the assertion, not a setup error.

- [ ] **Step 7: Commit**

```bash
git add backend/app/core/config.py backend/app/core/db.py backend/app/core/tenancy.py \
        backend/tests/conftest.py backend/tests/test_portal_session.py
git commit -m "feat(portal): portal_session and the row-scoped portal engine"
```

---

## Task 3: `principal.py` — the auth seam

**Files:**
- Create: `backend/app/modules/portal/__init__.py`
- Create: `backend/app/modules/portal/principal.py`
- Test: `backend/tests/test_portal_principal.py`

**Interfaces:**
- Consumes: `app.api.deps.auth_context`, `app.core.rbac`.
- Produces:
  - `@dataclass(frozen=True) class PortalContext: org_id: UUID; requester_user_id: UUID`
  - `async def resolve_portal_principal(request: Request) -> PortalContext`
  - `def require_portal_permission(code: str) -> Callable[..., Coroutine[Any, Any, PortalContext]]`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_portal_principal.py
"""The portal auth seam (spec §0.1, §3).

PortalContext is deliberately NOT an AuthContext: a portal principal must not
be passable to a staff dependency by mistake.
"""

import pytest

from app.modules.portal.principal import PortalContext


def test_portal_context_is_not_an_auth_context() -> None:
    """Spec §3 -- distinct types, so the two surfaces cannot be crossed by a
    careless dependency signature."""
    from app.api.deps import AuthContext

    assert not issubclass(PortalContext, AuthContext)
    assert "claims" not in PortalContext.__dataclass_fields__


def test_portal_context_carries_only_org_and_requester() -> None:
    assert sorted(PortalContext.__dataclass_fields__) == ["org_id", "requester_user_id"]


async def test_resolve_refuses_a_principal_without_a_portal_permission(api_env) -> None:
    """A technician-tier user is authenticated but is NOT a portal user. Call
    resolve_portal_principal directly -- no route needed."""
    import pytest
    from fastapi import HTTPException

    org_id = await api_env.create_org("portal-nrole")
    sub = await api_env.create_user(org_id, "technician")
    request = _fake_request(api_env, org_id, sub)  # build with api_env's headers
    with pytest.raises(HTTPException) as exc:
        await resolve_portal_principal(request)
    assert exc.value.status_code == 403


async def test_resolve_admits_a_requester(api_env) -> None:
    org_id = await api_env.create_org("portal-role")
    sub = await api_env.create_user(org_id, "requester")
    request = _fake_request(api_env, org_id, sub)
    ctx = await resolve_portal_principal(request)
    assert ctx.org_id == org_id
```

Build `_fake_request` from `api_env`'s app and headers — read how existing tests that exercise a dependency directly (rather than through a route) construct a `Request`, and follow that. If no such precedent exists, drive these two through the app's dependency-override mechanism instead. **Do NOT write tests here that depend on Task 5's routes** — this task must end with the full suite green, per the plan's Global Constraints, and a task that ends red breaks the review gate.

- [ ] **Step 2: Run to verify failure**

Expected: FAIL — `ModuleNotFoundError: app.modules.portal.principal`.

- [ ] **Step 3: Implement**

```python
# backend/app/modules/portal/principal.py
"""The portal's auth seam (spec §0.1, §3).

THIS FILE IS THE EXTENSION POINT. Portal users in v1 are staff already in the
org's OIDC IdP, holding the `requester` role. External end users -- a managed
provider's clients, who are not in that IdP -- are a real future requirement,
and adding them means writing a SECOND implementation of
`resolve_portal_principal` here. That is the whole seam: no pluggable-auth
framework is built for a case that does not yet exist, and the tenancy design
(spec §2) is deliberately independent of how a principal authenticated.

`PortalContext` is intentionally NOT an `AuthContext`. Distinct types mean a
portal principal cannot be handed to a staff dependency by mistake, which a
shared type would make a one-line error.
"""

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, Request, status

from app.api.deps import auth_context
from app.core.rbac import has_permission
from app.core.tenancy import org_session


@dataclass(frozen=True)
class PortalContext:
    org_id: UUID
    requester_user_id: UUID


async def resolve_portal_principal(request: Request) -> PortalContext:
    """Resolve an authenticated portal principal.

    v1 reuses the OIDC path, then requires a portal permission. Auto-
    provisioning of an unknown `sub` is kept deliberately: an unrecognised
    person gets a `User` row with no roles and therefore no access, which is a
    clean 403 rather than a 500. Having a row is not access; the role grant is.
    """
    ctx = await auth_context(request)
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        allowed = await has_permission(session, ctx.user_id, "portal.ticket.read")
    if not allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not a portal user")
    return PortalContext(org_id=ctx.org_id, requester_user_id=ctx.user_id)
```

Then `require_portal_permission(code)`, mirroring `app/api/deps.py::require_permission`'s shape so `app/core/rbac.py::route_permission` can recover the code from its closure — read that function before writing, because the notification module's RBAC completeness test depends on the shape.

- [ ] **Step 4: Run the tests**

Expected: PASS (4 tests). If any test here needs a portal route to exist, you have written a Task 5 test in Task 3 — move it.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/portal/ backend/tests/test_portal_principal.py
git commit -m "feat(portal): PortalContext and the single-function auth seam"
```

---

## Task 4: `schema.py` and `tickets.py` — narrow models and the service layer

**Files:**
- Create: `backend/app/modules/portal/schema.py`
- Create: `backend/app/modules/portal/tickets.py`
- Test: `backend/tests/test_portal_tickets.py`

**Interfaces:**
- Consumes: Task 2's `portal_session`, Task 3's `PortalContext`, and the ticket core's `tickets.create_ticket`, `threads.add_comment`, `threads.add_attachment`.
- Produces: `PortalTicketSummary`, `PortalTicketDetail`, `PortalCommentOut`, `PortalTicketCreate`, `PortalCommentCreate`, `PortalCategoryOut`; and `list_my_tickets`, `get_my_ticket`, `create_my_ticket`, `add_my_comment`, `add_my_attachment`, `list_categories`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_portal_tickets.py
"""Portal services (spec §5, §6).

The 404-not-403 test and the internal-note test are the two that matter: one
prevents a cross-requester existence oracle, the other prevents disclosing
staff discussion about a person to that person.
"""

import pytest

from app.modules.portal.errors import PortalNotFoundError
from app.modules.portal.schema import PortalTicketDetail


def test_portal_detail_exposes_no_staff_fields() -> None:
    """Spec §5 -- a structural guarantee, not a runtime filter. The portal must
    not reuse the staff TicketOut."""
    fields = set(PortalTicketDetail.model_fields)
    for forbidden in ("queue_id", "assignee_id", "assigned_to", "internal_notes"):
        assert forbidden not in fields, f"portal detail exposes {forbidden}"


async def test_a_ticket_that_is_not_mine_is_not_found(api_env, portal_engine, a_ticket_for) -> None:
    """Spec §5 -- 404, never 403. A 403 confirms the id exists, which is a
    cross-requester existence oracle."""
    org_id = await api_env.create_org("portal-404")
    theirs = await a_ticket_for(org_id, "them")
    me = await a_ticket_for(org_id, "me")
    from app.modules.portal.tickets import get_my_ticket
    from app.core.tenancy import portal_session

    async with portal_session(org_id, me.requester_id, engine=portal_engine) as s:
        with pytest.raises(PortalNotFoundError):
            await get_my_ticket(s, org_id=org_id, requester_id=me.requester_id, ticket_id=theirs.id)


async def test_internal_notes_are_absent_from_the_thread(
    api_env, portal_engine, a_ticket_for, add_comment_as_staff
) -> None:
    """Spec §9.3 -- MUST fail if the POLICY's visibility filter is removed, not
    only if the query filter is. Assert on the returned thread."""
    org_id = await api_env.create_org("portal-internal")
    mine = await a_ticket_for(org_id, "me")
    await add_comment_as_staff(mine, body="visible to you", internal=False)
    await add_comment_as_staff(mine, body="STAFF ONLY do not disclose", internal=True)

    from app.modules.portal.tickets import get_my_ticket
    from app.core.tenancy import portal_session

    async with portal_session(org_id, mine.requester_id, engine=portal_engine) as s:
        detail = await get_my_ticket(
            s, org_id=org_id, requester_id=mine.requester_id, ticket_id=mine.id
        )
    bodies = [c.body for c in detail.comments]
    assert "visible to you" in bodies
    assert all("STAFF ONLY" not in b for b in bodies)


async def test_creating_a_ticket_forces_me_as_requester(api_env, portal_engine) -> None:
    """Spec §6.2 -- requester_id comes from PortalContext, never the body."""
    org_id = await api_env.create_org("portal-create")
    sub = await api_env.create_user(org_id, "requester")
    # resolve the user id for `sub`, create a category, then create via the
    # portal service and assert the row's requester_id is this user.
```

Complete that last test against the real fixtures; the assertion is that the created row's `requester_id` equals the portal context's user, regardless of anything in the input model.

- [ ] **Step 2: Run to verify failure**

Expected: FAIL — module not found.

- [ ] **Step 3: Implement `errors.py` and `schema.py`**

`PortalValidationError` and `PortalNotFoundError` (a subclass), mirroring the notification and service-desk modules' `errors.py`.

Response models are **narrow and portal-specific**: `PortalTicketDetail` carries number, type, status, subject, description, created/updated timestamps, public comments, status history, and approval outcome when present. It carries **no** queue, **no** assignee, and no field whose value is staff-only. Do not import or subclass the staff `TicketOut`.

- [ ] **Step 4: Implement `tickets.py`**

Each function takes the `portal_session`-opened session plus explicit `org_id` and `requester_id`, and carries both predicates in its queries **in addition to** the RLS policy — belt and braces, matching this project's discipline that RLS is the backstop rather than the only guard.

Writes delegate: `create_my_ticket` calls `tickets.create_ticket`; `add_my_comment` calls `threads.add_comment` with visibility forced to `public`; `add_my_attachment` calls `threads.add_attachment`. Raise `PortalNotFoundError` for a ticket that is absent **or** not the requester's — the two must be indistinguishable.

- [ ] **Step 5: Run the tests**

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/modules/portal/ backend/tests/test_portal_tickets.py
git commit -m "feat(portal): narrow response models and portal-scoped ticket services"
```

---

## Task 5: `api.py` — the seven routes

**Files:**
- Create: `backend/app/modules/portal/api.py`
- Modify: `backend/app/modules/__init__.py` (register the router)
- Test: `backend/tests/test_portal_api.py`

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces: the seven routes in spec §5.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_portal_api.py
"""Portal API (spec §5, §9).

The org_session test is the structural one: a portal route that opened an
org_session would connect as app_user and see the whole org, and no
behavioural test would necessarily catch it.
"""

import pathlib

import pytest


async def test_every_portal_route_requires_a_permission(app) -> None:
    """Router-derived, as in the notification module -- a portal route added
    later without a permission fails this test."""
    from app.core.rbac import route_permission

    routes = [r for r in app.routes if getattr(r, "path", "").startswith("/api/v1/portal")]
    assert routes, "no portal routes registered"
    for route in routes:
        assert route_permission(route) is not None, f"{route.path} has no permission"


def test_no_portal_route_opens_an_org_session() -> None:
    """Spec §9.7 -- structural. `org_session` connects as app_user, which sees
    the whole org; a portal route must only ever use `portal_session`.

    `principal.py` is exempt: it deliberately uses org_session to read the
    caller's permissions BEFORE a portal context exists.
    """
    root = pathlib.Path("app/modules/portal")
    for path in root.rglob("*.py"):
        if path.name == "principal.py":
            continue
        assert "org_session" not in path.read_text(), path


async def test_a_technician_cannot_reach_the_portal(api_env, client) -> None:
    org_id = await api_env.create_org("portal-tech")
    sub = await api_env.create_user(org_id, "technician")
    got = await client.get(
        "/api/v1/portal/tickets", headers=await api_env.headers(org_id, sub)
    )
    assert got.status_code == 403


async def test_a_requester_cannot_reach_a_staff_route(api_env, client) -> None:
    """The other direction, and the more important one: the requester tier must
    not open the org-wide ticket API."""
    org_id = await api_env.create_org("portal-cross")
    sub = await api_env.create_user(org_id, "requester")
    got = await client.get(
        "/api/v1/service-desk/tickets", headers=await api_env.headers(org_id, sub)
    )
    assert got.status_code == 403


async def test_another_requesters_ticket_is_404(api_env, client, a_ticket_for) -> None:
    org_id = await api_env.create_org("portal-api-404")
    theirs = await a_ticket_for(org_id, "them")
    sub = await api_env.create_user(org_id, "requester")
    got = await client.get(
        f"/api/v1/portal/tickets/{theirs.id}", headers=await api_env.headers(org_id, sub)
    )
    assert got.status_code == 404, "403 would confirm the id exists"
```

- [ ] **Step 2: Run to verify failure**

Expected: FAIL — no portal routes registered.

- [ ] **Step 3: Implement**

Seven routes per spec §5, each gated with `require_portal_permission`. Literal segments before `{id}` parameters. Handlers translate errors and nothing more; every one opens a `portal_session` from `PortalContext` and passes the session to Task 4's services. The attachment upload route reuses the ticket core's ordering exactly: bound the declared `Content-Length`, verify the ticket is in scope, **then** read the bounded body — read `app/modules/service_desk/api.py::upload_attachment_route` and mirror it.

Register via `app/modules/__init__.py::register_all`, following how `notification` and `service_desk` do it. Note that `app/api/__init__.py` is empty and unused.

- [ ] **Step 4: Run the tests, then the full suite**

```bash
cd /home/user/Toeshee-RMS/backend
.venv/bin/python -m pytest tests/test_portal_api.py tests/test_portal_principal.py -q
.venv/bin/python -m pytest tests/ -q
```

Expected: both PASS. The full suite matters here — this task registers routes into the shared app.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/portal/api.py backend/app/modules/__init__.py \
        backend/tests/test_portal_api.py
git commit -m "feat(portal): REST API with router-derived permission coverage"
```

---

## Task 6: Fix the notification email copy

**Files:**
- Modify: `backend/app/modules/notification/delivery.py`
- Modify: `docs/modules/notification/spec.md` (note the amended copy)
- Test: `backend/tests/test_notification_delivery.py` (extend)

Spec §0.4. The resolution email currently says *"If you have further questions, reply to this ticket to reopen it."* Inbound email intake is sub-project 4 and does not exist, so a requester who replies gets silence — the message instructs an action the system cannot perform.

- [ ] **Step 1: Write the failing test**

```python
def test_resolution_email_does_not_promise_reply_to_reopen() -> None:
    """Portal spec §0.4 -- inbound email intake is sub-project 4 and does not
    exist. Telling a requester to reply is an instruction the system cannot
    honour."""
    body = _render_email_body(  # use the real renderer's name
        event_type="ticket.resolved",
        payload={"ticket_number": "INC-1", "subject": "VPN will not connect"},
    )
    assert "reply to this ticket" not in body.lower()
    assert "portal" in body.lower(), "point the requester somewhere that works"
```

- [ ] **Step 2: Run to verify failure**

Expected: FAIL — the current copy contains the promise.

- [ ] **Step 3: Change the copy**

Point the requester at the portal instead. Keep it one short sentence; do not invent a URL — the portal path is a deployment concern, so refer to it by name rather than hard-coding a host.

- [ ] **Step 4: Run the notification suite**

Run: `cd /home/user/Toeshee-RMS/backend && .venv/bin/python -m pytest tests/test_notification_delivery.py -q`
Expected: PASS. If another test asserted the old copy, that assertion is now wrong and correcting it is part of this task — say which in your report.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/notification/delivery.py docs/modules/notification/spec.md \
        backend/tests/test_notification_delivery.py
git commit -m "fix(notification): stop telling requesters to reply-to-reopen, which does nothing"
```

---

## Task 7: The portal frontend

**Files:**
- Create: `frontend/src/modules/portal/types.ts`, `api.ts`, `MyTicketsPage.tsx`, `PortalTicketDetailPage.tsx`, `NewTicketPage.tsx`, `PortalNav.tsx`
- Create: `frontend/src/modules/portal/__tests__/portal.test.tsx`
- Modify: the app router and nav

- [ ] **Step 1: Write the failing tests**

```tsx
// frontend/src/modules/portal/__tests__/portal.test.tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

describe("portal", () => {
  it("shows my tickets with status", async () => {
    renderPortal({ tickets: [{ number: "INC-1", subject: "VPN", status: "in_progress" }] });
    expect(await screen.findByText("INC-1")).toBeInTheDocument();
  });

  it("never renders a queue or assignee", async () => {
    renderPortal({ tickets: [{ number: "INC-1", subject: "VPN", status: "new" }] });
    await waitFor(() => expect(screen.queryByText(/queue/i)).toBeNull());
    expect(screen.queryByText(/assignee/i)).toBeNull();
  });

  it("shows the approval outcome when there is one", async () => {
    renderPortal({ detail: { approval: { decision: "rejected", reason: "no budget" } } });
    expect(await screen.findByText(/rejected/i)).toBeInTheDocument();
    expect(await screen.findByText(/no budget/i)).toBeInTheDocument();
  });

  it("degrades quietly when a secondary call 403s", async () => {
    api.listCategories = vi.fn().mockRejectedValue(new ApiError(403, "forbidden"));
    renderPortal({});
    await waitFor(() => expect(screen.queryByText(/error/i)).toBeNull());
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /home/user/Toeshee-RMS/frontend && npm test -- portal`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Three pages per spec §7. Strict TypeScript, no `any`; TanStack Query; badge and empty-state conventions carried from the service-desk and notification modules. Read `frontend/src/modules/service-desk/` first and match it. The portal surface shows nothing about queues, assignees, or internal notes.

- [ ] **Step 4: Run the gates**

```bash
cd /home/user/Toeshee-RMS/frontend && npx tsc --noEmit && npm test
```

Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/modules/portal/ frontend/src/
git commit -m "feat(portal): end-user surface -- my tickets, detail, and new ticket"
```

---

## Final gate

```bash
cd /home/user/Toeshee-RMS/backend
.venv/bin/python -m pytest tests/ -q          # expect > 2070 passing, 0 failed
.venv/bin/python -m mypy app/                 # expect: Success
.venv/bin/python -m ruff check app/ tests/    # expect: All checks passed
cd ../frontend
npx tsc --noEmit                              # expect: no output
npm test                                      # expect: > 199 passing
```

Then confirm `git status --porcelain` is empty and every task is committed.

**Run the portal end to end before declaring done.** This module's whole claim is a database guarantee, and the previous sub-project shipped two defects that only a real run surfaced. Create two requesters in one org, file a ticket as each, and confirm through the real API that each sees exactly one ticket — then confirm a staff user still sees both.

---

## Self-review

**Spec coverage.** §0.1 seam → Task 3. §0.2 role/policies → Tasks 1, 2. §0.3 scope → Tasks 4, 5, 7. §0.4 email → Task 6. §1 boundary → enforced by Task 5's `org_session` test and Task 4's delegation. §2.1 per-table access → Task 1. §2.2 `WITH CHECK` → Task 1. §2.3 policy-level visibility → Tasks 1, 4. §2.4 no new tables → Global Constraints. §3 auth seam → Task 3. §4 permission tier → Task 1. §5 API → Task 5. §6 reuse → Task 4. §7 frontend → Task 7. §8 audit → inherited from the ticket-core services Task 4 calls. §9's ten tests → 9.1/9.2 Task 2; 9.3/9.4/9.5 Tasks 1, 4; 9.6 Tasks 4, 5; 9.7 Task 5; 9.8 Task 1; 9.9 Task 5; 9.10 Task 2 plus the full suite at every task. §10 migration → Task 1.

**Fatal flaw found and closed before writing a line of code.** The first draft
declared the portal policies PERMISSIVE. Verified against the real database:
`tenant_isolation` is PERMISSIVE with `roles={public}`, Postgres OR's permissive
policies, and the portal role saw **2 of 2** rows — the entire design defeated,
with every `pg_policies`-shape assertion still passing. Corrected to
`AS RESTRICTIVE TO portal_user` (1 of 2 visible, staff still 2, requester-GUC-
unset 0), and Task 1 now asserts both `permissive = 'RESTRICTIVE'` and
`roles = ['portal_user']` so the mistake cannot be reintroduced silently.

**Second gap found and closed.** §9.4 ("a requester cannot write an internal note, refused by the database") needed the `WITH CHECK` half of the comment policy, which an earlier draft of Task 1 only had on SELECT. A policy with `USING` but no `WITH CHECK` filters reads while permitting writes — the same defect the notification module's spec shipped and had to fix. Task 1's `portal_isolation` on `ticket_comment` now carries both, and `test_comment_insert_is_constrained_to_public` asserts it.

**Placeholder scan.** No TBD/TODO. Tasks 4, 5 and 7 describe some implementations in prose rather than full code, because each follows a pattern already established in `app/modules/service_desk/` or `app/modules/notification/` that the implementer is directed to read; every one has complete test code, which is what pins behaviour. Task 4's last test and Task 6's renderer name are deliberately left to be completed against the real fixture/function names rather than guessed here — both are called out in their steps.

**Type consistency.** `PortalContext` (Task 3) is consumed by Tasks 4 and 5 with the same two fields. `portal_session(org_id, requester_id, *, engine)` is defined in Task 2 and called with that signature in Tasks 2, 4, 5. `PortalNotFoundError` is defined in Task 4 and asserted in Tasks 4 and 5. Policy name `portal_isolation` is identical in the migration and in every test that queries `pg_policies`.

**Known risk carried forward.** `app_user`'s missing table privileges in `deploy/postgres/init-app-user.sh` are pre-existing and deliberately not fixed here. Task 1's report must state that the gap remains, so it is not mistaken for something this module introduced.
