# Asset inventory, part 1: agentless assets — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** An operator can add an asset by hand — name, description, IP, type, lifecycle — and see it in the fleet list beside agent-managed machines, with a device page that omits what an agentless asset cannot have rather than showing it empty.

**Architecture:** No new table. `agents.device_id` is already a unique FK to `devices.id`, so *managed* is already a join and a `devices` row with no agent is already structurally legal (`docs/modules/asset-inventory/spec.md` §1). This plan adds the columns `devices` has no room for, drops two `NOT NULL`s, and audits the read paths that assumed them — §2 is explicit that the migration is trivial and **the audit is the work**.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x (typed), Alembic, Pydantic v2, pytest against a real Postgres; React + TypeScript (strict), TanStack Query, vitest.

## Why this is part 1 of three

`docs/modules/asset-inventory/spec.md` covers three deliverables that depend on each other in one direction only:

1. **Agentless assets** (this plan) — schema, manual entry, honest UI.
2. **Discovery → candidates → promote** (§3) — needs part 1's `source` column and its promote target.
3. **Enrolment matching and the suggestion queue** (§4) — needs part 1's rows to match *against*.

Each produces working software on its own; each is its own spec → plan → build cycle. **Do not build 2 or 3 here.**

## Global Constraints

- **`lifecycle` is stored; *managed* is a join.** Never add a `management_state` column. "Does this record have an agent" is `EXISTS (SELECT 1 FROM agents WHERE device_id = devices.id)`; storing it beside the `agents` row guarantees they eventually disagree, silently (spec §2).
- **`lifecycle` vocabulary:** `in_service | stock | unmanageable | retired`. `asset_type`: `computer | server | network | printer | phone | appliance | other`. `source`: `agent | manual | discovery`. Exact strings — the API, the migration's CHECK constraints and the UI all use these.
- **"unmanaged" and "unmanageable" are different words for different facts.** Unmanaged = no `agents` row, and belongs on a to-do list. Unmanageable = the operator said a switch will never have an agent, and must **never** appear on one.
- **A screen that looks finished and lies is worse than one that is visibly incomplete** — `DevicesPage.test.tsx`'s standard, and spec §5 applies it here: Patch, Deploy, Backup, Provisioning and Health are **not rendered** for an agentless asset. A zeroed Patch card is the lie.
- **`stock` is not offline.** A boxed laptop must not raise an offline alert; that trains operators to ignore the alert (spec §5, falsifier 7).
- **Tenancy:** every new route is `org_id`-scoped at the database layer, and the cross-org test asserts on the **BYPASSRLS admin engine** — RLS masks a missing application predicate otherwise (falsifier 9).
- **Migrations are forward-only.** New revision, never edit a shipped one.
- **Permissions:** `assets.read` is **not added** — `devices.read` already covers it; a second read gate is a second thing to grant. `assets.manage` is new. `assets.discover` belongs to part 2 and is **not** seeded here.

## The spec's §9 open question, answered here

> Does an agentless asset consume a licence seat, and does it count in "Managed endpoints"?

The commercial half is not this plan's to decide. The console half is, and the spec's own recommendation is implemented: **"Managed endpoints" counts records with an agent**, and a second card counts **total assets**. Today's card counts rows, so the moment warehouse stock lands it silently stops meaning what an operator reads it to mean. Task 6 makes the split, and its test is what stops the two numbers drifting back together.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/migrations/versions/0061_asset_columns.py` | The seven columns, the two `NOT NULL` drops, CHECK constraints |
| `backend/migrations/versions/0062_asset_permissions.py` | Seeds `assets.manage` |
| `backend/app/models/core.py` | `Device` gains the columns; `hostname`/`os_family` become nullable |
| `backend/app/core/rbac.py` | `assets.manage` in `PERMISSIONS` |
| `backend/app/api/v1/devices.py` | `DeviceOut` carries the new fields and nullable identity; `POST`/`PATCH` for manual assets |
| `backend/app/modules/health/sweep.py` | Offline sweep skips non-`in_service` lifecycles |
| `backend/tests/test_asset_columns.py` | Model + migration shape |
| `backend/tests/test_asset_api.py` | Create, edit, tenancy, audit, validation |
| `backend/tests/test_asset_read_paths.py` | The `NOT NULL`-drop audit (falsifier 5) |
| `backend/tests/test_health_offline_lifecycle.py` | Falsifier 7 |
| `frontend/src/lib/api.ts` | `Device` type gains the fields; `createAsset`, `updateAsset` |
| `frontend/src/modules/devices/DevicesPage.tsx` | Two stat cards, an Assets tab, the Add-asset dialog |
| `frontend/src/modules/inventory/DeviceDetailPage.tsx` | Agentless adaptation |
| `docs/modules/asset-inventory/spec.md` | Status: part 1 built |

---

### Task 1: The columns, and the two `NOT NULL` drops

**Files:**
- Create: `backend/migrations/versions/0061_asset_columns.py`
- Modify: `backend/app/models/core.py:113-135` (`Device`)
- Create: `backend/tests/test_asset_columns.py`

**Interfaces:**
- Produces: `Device.lifecycle`, `.asset_type`, `.description`, `.primary_ip`, `.owner`, `.location`, `.source`, with `hostname: Mapped[str | None]` and `os_family: Mapped[str | None]`. Tasks 2–8 all consume these names.

- [x] **Step 1: Write the failing test**

`backend/tests/test_asset_columns.py`:

```python
"""`devices` becomes an ASSET table (docs/modules/asset-inventory/spec.md §2).

THE RISKY PART IS NOT THE MIGRATION. Adding seven columns is trivial; dropping
`NOT NULL` from `hostname` and `os_family` changes what every existing read path
may receive, and none of them was written expecting `None`. That audit is
`tests/test_asset_read_paths.py`. This file pins the SHAPE.

WHY THERE IS NO `management_state` COLUMN, asserted rather than remembered:
"does this record have an agent" is a join (`agents.device_id` is a unique FK to
`devices.id`), and storing it beside the `agents` row guarantees the two
eventually disagree with nothing to notice.
"""

from app.models.core import Device

LIFECYCLES = {"in_service", "stock", "unmanageable", "retired"}
ASSET_TYPES = {
    "computer",
    "server",
    "network",
    "printer",
    "phone",
    "appliance",
    "other",
}
SOURCES = {"agent", "manual", "discovery"}


def test_the_asset_columns_exist() -> None:
    columns = Device.__table__.columns
    for name in (
        "lifecycle",
        "asset_type",
        "description",
        "primary_ip",
        "owner",
        "location",
        "source",
    ):
        assert name in columns, sorted(columns.keys())


def test_identity_is_optional_because_a_boxed_laptop_has_none() -> None:
    """A switch has a name but no hostname in this sense, and no `os_family` at
    all; a laptop in a box has neither. Both were NOT NULL."""
    assert Device.__table__.columns["hostname"].nullable is True
    assert Device.__table__.columns["os_family"].nullable is True


def test_lifecycle_and_source_default_to_todays_meaning() -> None:
    """Every row that exists today came from an agent and is in service. A
    default that said otherwise would silently reclassify the whole fleet."""
    assert Device.__table__.columns["lifecycle"].default.arg == "in_service"
    assert Device.__table__.columns["source"].default.arg == "agent"
    assert Device.__table__.columns["asset_type"].default.arg == "computer"


def test_there_is_no_stored_management_state() -> None:
    """Spec §2: managed is a JOIN. This is the column that must never appear."""
    assert "management_state" not in Device.__table__.columns
    assert "is_managed" not in Device.__table__.columns
```

- [x] **Step 2: Run to verify it fails**

Run: `cd backend && .venv/bin/pytest tests/test_asset_columns.py -q`
Expected: FAIL — `lifecycle` not in columns; `hostname.nullable is False`.

- [x] **Step 3: The migration**

`backend/migrations/versions/0061_asset_columns.py`:

```python
"""asset inventory: agentless assets in `devices`

docs/modules/asset-inventory/spec.md §2.

WHY NO `assets` TABLE. `agents.device_id` is a unique FK to `devices.id`, so
"managed" is already a join and a `devices` row with no agent is already legal.
A separate table would duplicate hostname, serial, group, tags and tenancy, and
then need a link table to reunite them -- and every fleet query would have to
remember to consult both.

WHY THE DEFAULTS ARE WHAT THEY ARE. Every row that exists when this runs came
from an enrolling agent and is in service, so `source='agent'` and
`lifecycle='in_service'` describe the existing fleet correctly. A default of
anything else would silently reclassify every machine in the estate.

THE `NOT NULL` DROPS ARE THE RISK, and not because of this migration. Postgres
applies them instantly; what changes is that every read path may now receive
`None` where it has always had a string. `tests/test_asset_read_paths.py` is
that audit, and it is the reason this migration is not the whole task.

Revision ID: 0061
Revises: 0060
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("lifecycle", sa.String(20), nullable=False, server_default="in_service"),
    )
    op.add_column(
        "devices",
        sa.Column("asset_type", sa.String(20), nullable=False, server_default="computer"),
    )
    op.add_column(
        "devices",
        sa.Column("source", sa.String(12), nullable=False, server_default="agent"),
    )
    op.add_column("devices", sa.Column("description", sa.String(1000), nullable=True))
    op.add_column("devices", sa.Column("primary_ip", postgresql.INET(), nullable=True))
    op.add_column("devices", sa.Column("owner", sa.String(200), nullable=True))
    op.add_column("devices", sa.Column("location", sa.String(200), nullable=True))

    op.create_check_constraint(
        "ck_devices_lifecycle",
        "devices",
        "lifecycle IN ('in_service', 'stock', 'unmanageable', 'retired')",
    )
    op.create_check_constraint(
        "ck_devices_asset_type",
        "devices",
        "asset_type IN ('computer', 'server', 'network', 'printer', 'phone', "
        "'appliance', 'other')",
    )
    op.create_check_constraint(
        "ck_devices_source",
        "devices",
        "source IN ('agent', 'manual', 'discovery')",
    )

    op.alter_column("devices", "hostname", existing_type=sa.String(255), nullable=True)
    op.alter_column("devices", "os_family", existing_type=sa.String(20), nullable=True)


def downgrade() -> None:
    # Re-imposing NOT NULL would fail on any row this feature created, which is
    # correct: a downgrade that silently deleted agentless assets to make the
    # constraint fit would destroy the operator's inventory. Rollback here is
    # restore-from-backup (docs/superpowers/specs/2026-08-15-deploy-rollback-design.md
    # D1); this exists so `downgrade base` round-trips in CI on a schema with
    # no such rows.
    op.alter_column("devices", "os_family", existing_type=sa.String(20), nullable=False)
    op.alter_column("devices", "hostname", existing_type=sa.String(255), nullable=False)
    op.drop_constraint("ck_devices_source", "devices", type_="check")
    op.drop_constraint("ck_devices_asset_type", "devices", type_="check")
    op.drop_constraint("ck_devices_lifecycle", "devices", type_="check")
    for column in (
        "location",
        "owner",
        "primary_ip",
        "description",
        "source",
        "asset_type",
        "lifecycle",
    ):
        op.drop_column("devices", column)
```

- [x] **Step 4: The model**

In `backend/app/models/core.py`, `Device`:

```python
    hostname: Mapped[str | None] = mapped_column(String(255), default=None)
    fqdn: Mapped[str | None] = mapped_column(String(255), default=None)
    # windows | linux | macos, or NULL: a switch is none of those, and a boxed
    # laptop has not told us yet (spec §1).
    os_family: Mapped[str | None] = mapped_column(String(20), default=None)
```

and, after `last_seen_at`:

```python
    #: WHAT THE OPERATOR INTENDS THIS ASSET TO BE -- stored, because nothing can
    #: derive it. `in_service | stock | unmanageable | retired`.
    #:
    #: NOT to be confused with whether the record has an agent, which is a JOIN
    #: (`agents.device_id` is a unique FK here) and is never stored. "Unmanaged"
    #: means no `agents` row and belongs on a to-do list; "unmanageable" means
    #: the operator said a switch will never have one, and must never appear on
    #: that list. Spec §2.
    lifecycle: Mapped[str] = mapped_column(String(20), default="in_service")
    asset_type: Mapped[str] = mapped_column(String(20), default="computer")
    #: How this record came to exist -- `agent | manual | discovery` -- which is
    #: the first question an operator asks when a row looks wrong.
    source: Mapped[str] = mapped_column(String(12), default="agent")
    description: Mapped[str | None] = mapped_column(String(1000), default=None)
    primary_ip: Mapped[str | None] = mapped_column(INET, default=None)
    owner: Mapped[str | None] = mapped_column(String(200), default=None)
    location: Mapped[str | None] = mapped_column(String(200), default=None)
```

`INET` comes from `sqlalchemy.dialects.postgresql` — add it to that import if it is not already there.

- [x] **Step 5: Run the tests**

Run: `cd backend && .venv/bin/pytest tests/test_asset_columns.py tests/test_schema_drift.py -q`
Expected: PASS. `test_schema_drift` compares the model against the live schema, so a column in one and not the other fails here rather than in production.

- [x] **Step 6: Round-trip the migration against a real Postgres**

```bash
cd backend
export ITS_ALEMBIC_URL="postgresql+asyncpg://its_admin:its_admin_test@127.0.0.1:5432/<scratch db>"
.venv/bin/alembic upgrade head && .venv/bin/alembic downgrade 0060 && .venv/bin/alembic upgrade head
```
Expected: three exit-0 runs. **A `downgrade` that leaves a constraint or column behind makes the next `upgrade head` fail** — that is the 0001 defect `CLAUDE.md` records, and this is the check for it.

- [x] **Step 7: Commit**

```bash
git add backend/migrations/versions/0061_asset_columns.py backend/app/models/core.py backend/tests/test_asset_columns.py
git commit -m "asset-inventory: devices gains asset columns, and optional identity"
```

---

### Task 2: The read-path audit (falsifier 5)

**Files:**
- Create: `backend/tests/test_asset_read_paths.py`
- Modify: `backend/app/api/v1/devices.py:16-40` (`DeviceOut`)
- Modify: whatever the audit finds

**Interfaces:**
- Consumes: Task 1's nullable `hostname`/`os_family`.
- Produces: `DeviceOut.hostname: str | None`, `DeviceOut.os_family: str | None`, plus the new fields — the shape Task 6's frontend types mirror.

- [x] **Step 1: Find every read path that assumes identity**

This is a survey, and it is the actual work of this task. Run all three, and read the results rather than skimming:

```bash
cd backend
grep -rn "\.hostname" app/ --include=*.py | grep -v "device\.hostname is not None" | wc -l
grep -rn "os_family" app/ --include=*.py
grep -rn "hostname" app/modules/*/[a-z]*.py | grep -v "^.*#" | head -60
```

Classify each hit:

- **Serializers** (`DeviceOut` and friends) → the field becomes optional. A Pydantic model declaring `hostname: str` **raises on serialization** when the column is NULL — a 500 on the device list, which is falsifier 5's exact failure.
- **f-strings and `.format`** → `None` renders as the literal `"None"`, which reads as fact. Use an explicit fallback.
- **Comparisons and joins** (`Device.hostname == x`) → SQL handles NULL correctly; leave them.
- **`.lower()`, `.strip()`, `.split()`** → `AttributeError` on `None`. These are the crashes.

- [x] **Step 2: Write the failing test**

`backend/tests/test_asset_read_paths.py`:

```python
"""An asset with no hostname and no OS must not break a read path (§6.5).

WHY THIS FILE EXISTS SEPARATELY FROM THE API TESTS. Spec §2 says the migration
is trivial and "the audit of read paths is the work". Dropping two `NOT NULL`s
changes what EVERY existing consumer may receive, and none of them was written
expecting `None`. The failure modes are not symmetrical:

  * a Pydantic model declaring `hostname: str` raises at SERIALIZATION time, so
    the device list 500s for the whole org because ONE row is an asset;
  * an f-string renders `None` as the four characters "None", which an operator
    reads as a hostname;
  * `.lower()` on `None` raises inside a filter.

The first is the dangerous one: one warehouse laptop takes out the fleet list.
"""

from uuid import UUID

from sqlalchemy import select

from app.models import Device
from app.core.tenancy import org_session
from tests.conftest import ApiEnv


async def _bare_asset(api_env: ApiEnv, org_id: UUID) -> Device:
    """A switch: no hostname, no OS, no agent. The row this feature exists for."""
    async with org_session(org_id, engine=api_env.app_engine) as session:
        device = Device(
            org_id=org_id,
            hostname=None,
            os_family=None,
            asset_type="network",
            lifecycle="unmanageable",
            source="manual",
            description="core switch, rack 3",
            primary_ip="172.22.104.1",
        )
        session.add(device)
        await session.commit()
        return device


async def test_the_device_list_survives_an_asset_with_no_identity(
    api_env: ApiEnv,
) -> None:
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    await _bare_asset(api_env, org_id)

    resp = await api_env.client.get(
        "/api/v1/devices", headers=await api_env.headers(org_id, admin)
    )
    assert resp.status_code == 200, resp.text
    row = resp.json()["items"][0]
    assert row["hostname"] is None
    assert row["os_family"] is None
    # NOT the string "None", and not "" -- both read as a fact the platform does
    # not have. The console decides how to render an absence; the API's job is
    # to say it is one.
    assert row["hostname"] != "None"


async def test_the_device_detail_route_survives_it_too(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    device = await _bare_asset(api_env, org_id)

    resp = await api_env.client.get(
        f"/api/v1/devices/{device.id}", headers=await api_env.headers(org_id, admin)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["asset_type"] == "network"
    assert resp.json()["lifecycle"] == "unmanageable"


async def test_an_asset_row_is_still_tenant_scoped(api_env: ApiEnv) -> None:
    """Falsifier 9. Asserted through the API AND on the admin engine, because
    RLS would mask a missing application-level predicate on the first alone."""
    org_a = await api_env.create_org("assets-a")
    org_b = await api_env.create_org("assets-b")
    admin_b = await api_env.create_user(org_b, "org-admin")
    device = await _bare_asset(api_env, org_a)

    resp = await api_env.client.get(
        f"/api/v1/devices/{device.id}", headers=await api_env.headers(org_b, admin_b)
    )
    assert resp.status_code == 404, resp.text

    async with org_session(org_b, engine=api_env.app_engine) as session:
        rows = (await session.execute(select(Device.id))).scalars().all()
    assert device.id not in rows
```

- [x] **Step 3: Run to verify it fails**

Run: `cd backend && .venv/bin/pytest tests/test_asset_read_paths.py -q`
Expected: FAIL — a `ResponseValidationError` (500) from `DeviceOut.hostname: str` receiving `None`, plus `KeyError: 'asset_type'`.

- [x] **Step 4: Make `DeviceOut` honest**

In `backend/app/api/v1/devices.py`:

```python
class DeviceOut(BaseModel):
    id: UUID
    # NULLABLE since 0061. A switch has no hostname in this sense and no OS
    # family at all; a boxed laptop has told us neither yet. Declaring these
    # `str` makes ONE asset row 500 the whole device list at serialization
    # time -- see tests/test_asset_read_paths.py.
    hostname: str | None
    fqdn: str | None
    os_family: str | None
    os_version: str
    architecture: str
    device_group_id: UUID | None
    status: str
    agent_status: str | None
    last_seen_at: datetime | None
    cert_not_after: datetime | None = None
    cert_state: str | None = None
    # Asset fields (docs/modules/asset-inventory/spec.md §2). `lifecycle` is
    # what the operator INTENDS; whether a record is managed is `agent_status`
    # being non-null, which is a join and is never stored.
    lifecycle: str = "in_service"
    asset_type: str = "computer"
    source: str = "agent"
    description: str | None = None
    primary_ip: str | None = None
    owner: str | None = None
    location: str | None = None
```

and carry them in `_row_to_out`. Order by `Device.hostname` still works — Postgres sorts NULLs last by default on `ASC`, which puts unnamed assets after named ones rather than at the top of the fleet list.

- [x] **Step 5: Fix whatever else the survey found**

For each hit classified as an f-string or a `.lower()`, use the row's own best label:

```python
def device_label(device: Device) -> str:
    """What to call a device in a message: hostname, else IP, else the id.

    `str(None)` is the four characters "None", and an operator reading a job
    log cannot tell that from a machine actually named None.
    """
    return device.hostname or device.primary_ip or str(device.id)
```

Put it in `app/models/core.py` beside `Device` if more than one module needs it; otherwise inline the `or` chain. **Do not** add it speculatively — YAGNI applies to the audit's findings too.

- [x] **Step 6: Run the tests, then the whole backend suite**

Run: `cd backend && .venv/bin/pytest tests/test_asset_read_paths.py -q && .venv/bin/pytest -q`
Expected: both green. **The full suite is the point of this task** — a nullable column breaks tests far from the module that changed it, and that is exactly the audit §2 asks for.

- [x] **Step 7: Commit**

```bash
git add backend/app/api/v1/devices.py backend/tests/test_asset_read_paths.py
git commit -m "asset-inventory: the read paths survive an asset with no identity"
```

---

### Task 3: `assets.manage`

**Files:**
- Create: `backend/migrations/versions/0062_asset_permissions.py`
- Modify: `backend/app/core/rbac.py:20` (`PERMISSIONS`)
- Modify: `backend/tests/test_asset_columns.py` (one test)

- [x] **Step 1: Write the failing test**

Append to `backend/tests/test_asset_columns.py`:

```python
def test_the_manage_code_exists_and_read_is_deliberately_not_added() -> None:
    """`assets.read` is NOT a code (spec §7).

    `devices.read` already answers "may this principal see the fleet list", and
    an asset IS a fleet row. A second read gate would mean every existing
    read-only role stops seeing part of the list the day this ships, with
    nothing to say why.
    """
    from app.core.rbac import PERMISSIONS

    assert "assets.manage" in PERMISSIONS
    assert "assets.read" not in PERMISSIONS
    # Part 2's code. Not seeded here -- it dispatches a job, and a permission
    # that exists before the thing it guards is a permission nobody can test.
    assert "assets.discover" not in PERMISSIONS
```

- [x] **Step 2: Run to verify it fails**

Run: `cd backend && .venv/bin/pytest tests/test_asset_columns.py -q -k manage_code`
Expected: FAIL — `assets.manage` not in PERMISSIONS.

- [x] **Step 3: Add the code and seed it**

`app/core/rbac.py`, in `PERMISSIONS` beside the `devices.*` entries:

```python
    "assets.manage": "Create and edit assets, including records with no agent",
```

`backend/migrations/versions/0062_asset_permissions.py`:

```python
"""asset inventory: seed `assets.manage`

docs/modules/asset-inventory/spec.md §7. Same shape as 0060: no schema, so the
RBAC change can be reviewed and reverted without touching a table.

ONE CODE, NOT THREE. `assets.read` is deliberately absent -- `devices.read`
already gates the fleet list and an asset is a fleet row; adding a second read
gate would blind every existing read-only role to part of that list on the day
this ships. `assets.discover` belongs to part 2, where the job it dispatches
exists.

WHO GETS IT. `org-admin`, because `tests/test_rbac.py::
test_org_admin_has_all_permissions` asserts an org-admin's codes equal
set(PERMISSIONS) -- a code in the dict and unseeded here turns that red. Also
`technician`: adding the switch you are standing in front of is the ordinary
work of the role, and unlike `agent.release.manage` this code publishes no
executable and mints no credential.

Revision ID: 0062
Revises: 0061
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CODE = "assets.manage"


def upgrade() -> None:
    op.execute(
        "INSERT INTO permissions (id, code, module, action) "
        f"VALUES (gen_random_uuid(), '{_CODE}', 'assets', 'manage')"
    )
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id FROM roles r, permissions p "
        f"WHERE r.name IN ('org-admin', 'technician') AND p.code = '{_CODE}'"
    )


def downgrade() -> None:
    # role_permissions first -- it references permissions, and dropping those
    # rows out from under it is what leaves a downgrade half-applied.
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code = '{_CODE}')"
    )
    op.execute(f"DELETE FROM permissions WHERE code = '{_CODE}'")
```

- [x] **Step 4: Run the RBAC tests**

Run: `cd backend && .venv/bin/pytest tests/test_asset_columns.py tests/test_rbac.py -q`
Expected: PASS, including `test_org_admin_has_all_permissions`.

- [x] **Step 5: Falsify the seeding**

Comment the `role_permissions` INSERT out of 0062, re-run `tests/test_rbac.py`, and confirm `test_org_admin_has_all_permissions` goes **red**. Restore it. (Deleting a grant by hand from a local database proves nothing — the suite builds its own database per run.)

- [x] **Step 6: Commit**

```bash
git add backend/migrations/versions/0062_asset_permissions.py backend/app/core/rbac.py backend/tests/test_asset_columns.py
git commit -m "asset-inventory: assets.manage, and no second read gate"
```

---

### Task 4: Creating and editing an asset

**Files:**
- Modify: `backend/app/api/v1/devices.py`
- Create: `backend/tests/test_asset_api.py`

**Interfaces:**
- Consumes: `assets.manage` (Task 3), the columns (Task 1), `DeviceOut` (Task 2).
- Produces: `POST /api/v1/devices` and `PATCH /api/v1/devices/{device_id}`, both returning `DeviceOut`. Task 6's `createAsset`/`updateAsset` call exactly these.

**Why these live on `/devices` and not a new `/assets` prefix:** the record IS a device row, the list route is `/devices`, and a second prefix for the same table means every future reader has to learn which half of the fleet lives where.

- [x] **Step 1: Write the failing tests**

`backend/tests/test_asset_api.py`:

```python
"""Manual asset entry (docs/modules/asset-inventory/spec.md §2, §7).

THE OPERATOR'S ASK, VERBATIM: "I should be able to manually add an asset with
name, description, IP address" -- for stock in the warehouse and for devices
that can never run an agent, like networking gear. So the create route must
accept a record with NO hostname and NO OS, which is precisely what 0061's
`NOT NULL` drops made expressible.

WHAT THESE TESTS REFUSE TO LET HAPPEN: a manual row that claims to be
agent-managed. `source` and `agent_status` are not settable here; the first is
stamped by the route, the second is a join.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.core.tenancy import org_session
from app.models import AuditEvent, Device
from tests.conftest import ApiEnv

_SWITCH: dict[str, Any] = {
    "hostname": "core-sw-01",
    "description": "core switch, rack 3",
    "primary_ip": "172.22.104.1",
    "asset_type": "network",
    "lifecycle": "unmanageable",
}


async def test_creating_an_asset_requires_manage(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("assets")
    reader = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.post(
        "/api/v1/devices", json=_SWITCH, headers=await api_env.headers(org_id, reader)
    )
    assert resp.status_code == 403, resp.text


async def test_an_operator_can_add_a_switch_with_no_hostname_at_all(
    api_env: ApiEnv,
) -> None:
    """The warehouse case: a record whose identity is a description and an IP."""
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    resp = await api_env.client.post(
        "/api/v1/devices",
        json={"description": "boxed laptop, PO 4471", "lifecycle": "stock"},
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["hostname"] is None
    assert body["os_family"] is None
    assert body["lifecycle"] == "stock"


async def test_a_manual_asset_is_marked_manual_and_cannot_claim_otherwise(
    api_env: ApiEnv,
) -> None:
    """`source` answers "why does this row look wrong", so it must be stamped
    by the route rather than accepted from the caller. A manual row claiming
    `source='agent'` is a lie an operator has no way to detect."""
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    resp = await api_env.client.post(
        "/api/v1/devices",
        json={**_SWITCH, "source": "agent"},
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 422, resp.text

    ok = await api_env.client.post(
        "/api/v1/devices", json=_SWITCH, headers=await api_env.headers(org_id, admin)
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["source"] == "manual"
    assert ok.json()["agent_status"] is None


async def test_an_unknown_lifecycle_is_refused_where_the_operator_can_see_it(
    api_env: ApiEnv,
) -> None:
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    resp = await api_env.client.post(
        "/api/v1/devices",
        json={**_SWITCH, "lifecycle": "decommissioned"},
        headers=await api_env.headers(org_id, admin),
    )
    # 422, not a 500 from the CHECK constraint: the operator typed this.
    assert resp.status_code == 422, resp.text


async def test_editing_an_asset_writes_an_audit_event(api_env: ApiEnv) -> None:
    """Invariant 4. Changing what an asset IS -- retiring it, marking it
    unmanageable -- changes what every sweep and alert does with it."""
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    created = (
        await api_env.client.post("/api/v1/devices", json=_SWITCH, headers=headers)
    ).json()

    resp = await api_env.client.patch(
        f"/api/v1/devices/{created['id']}",
        json={"lifecycle": "retired", "location": "rack 3"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["lifecycle"] == "retired"

    async with org_session(org_id, engine=api_env.app_engine) as session:
        actions = (
            await session.execute(select(AuditEvent.action).order_by(AuditEvent.id))
        ).scalars().all()
    assert "asset.updated" in actions
    assert "asset.created" in actions


async def test_an_agent_managed_device_cannot_be_renamed_by_hand(
    api_env: ApiEnv,
) -> None:
    """A hostname on a managed device is REPORTED BY THE MACHINE, and the next
    inventory run overwrites whatever a human typed. Accepting the edit and
    silently reverting it hours later is worse than refusing it."""
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    device_id = await _enrolled_device(api_env, org_id)

    resp = await api_env.client.patch(
        f"/api/v1/devices/{device_id}",
        json={"hostname": "renamed-by-hand"},
        headers=headers,
    )
    assert resp.status_code == 409, resp.text

    # ... but the operator-owned fields are still editable on a managed device:
    # owner and location are facts about the ORGANISATION, not the machine, and
    # no inventory run will ever overwrite them.
    ok = await api_env.client.patch(
        f"/api/v1/devices/{device_id}",
        json={"owner": "helpdesk", "location": "HQ floor 2"},
        headers=headers,
    )
    assert ok.status_code == 200, ok.text


async def test_creating_an_asset_is_tenant_scoped(api_env: ApiEnv) -> None:
    """Falsifier 9, on the write path. Asserted on the ADMIN engine, which
    bypasses RLS -- through a tenant-scoped session, a row in the wrong org is
    invisible rather than absent, and the two look identical from the API."""
    org_a = await api_env.create_org("assets-a")
    org_b = await api_env.create_org("assets-b")
    admin_a = await api_env.create_user(org_a, "org-admin")
    created = (
        await api_env.client.post(
            "/api/v1/devices", json=_SWITCH, headers=await api_env.headers(org_a, admin_a)
        )
    ).json()

    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(api_env.admin_engine) as session:
        org_id = (
            await session.execute(select(Device.org_id).where(Device.id == UUID(created["id"])))
        ).scalar_one()
    assert org_id == org_a
    assert org_id != org_b
```

`_enrolled_device` is a helper this file needs; write it as the smallest thing that produces a `Device` with an `Agent` row:

```python
async def _enrolled_device(api_env: ApiEnv, org_id: UUID) -> UUID:
    from datetime import UTC, datetime, timedelta

    from app.models import Agent

    async with org_session(org_id, engine=api_env.app_engine) as session:
        device = Device(org_id=org_id, hostname="managed-01", os_family="linux")
        session.add(device)
        await session.flush()
        session.add(
            Agent(
                org_id=org_id,
                device_id=device.id,
                cert_fingerprint=f"fp-{device.id.hex[:16]}",
                cert_serial="01",
                cert_not_after=datetime.now(UTC) + timedelta(days=365),
            )
        )
        await session.commit()
        return device.id
```

- [x] **Step 2: Run to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_asset_api.py -q`
Expected: FAIL — 405 Method Not Allowed on both routes.

- [x] **Step 3: Implement**

In `backend/app/api/v1/devices.py`:

```python
_LIFECYCLES = ("in_service", "stock", "unmanageable", "retired")
_ASSET_TYPES = (
    "computer",
    "server",
    "network",
    "printer",
    "phone",
    "appliance",
    "other",
)


class AssetCreateRequest(BaseModel):
    """A record an operator types. `extra="forbid"` so `source` or
    `agent_status` in the body is a 422 rather than a silently ignored field --
    an operator who thinks they set one and did not is worse off than one who
    got an error."""

    model_config = ConfigDict(extra="forbid")

    hostname: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    primary_ip: IPvAnyAddress | None = None
    asset_type: str = Field(default="computer", pattern="^(" + "|".join(_ASSET_TYPES) + ")$")
    lifecycle: str = Field(default="in_service", pattern="^(" + "|".join(_LIFECYCLES) + ")$")
    owner: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    os_family: str | None = Field(default=None, pattern="^(windows|linux|macos)$")
    serial: str | None = Field(default=None, max_length=100)
    asset_tag: str | None = Field(default=None, max_length=100)


class AssetUpdateRequest(AssetCreateRequest):
    """Every field optional; only what is sent is written."""

    asset_type: str | None = Field(default=None, pattern="^(" + "|".join(_ASSET_TYPES) + ")$")
    lifecycle: str | None = Field(default=None, pattern="^(" + "|".join(_LIFECYCLES) + ")$")
```

The two handlers:

```python
@router.post("", status_code=status.HTTP_201_CREATED, response_model=DeviceOut)
async def create_asset(
    body: AssetCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("assets.manage")),
) -> DeviceOut:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        device = Device(
            org_id=ctx.org_id,
            # STAMPED, never taken from the body: `source` is the first thing an
            # operator checks when a row looks wrong, and a manual record
            # claiming `agent` would send them looking at the wrong thing.
            source="manual",
            **body.model_dump(exclude_none=True) | {"primary_ip": str(body.primary_ip)
                                                     if body.primary_ip else None},
        )
        session.add(device)
        await session.flush()
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="asset.created",
            target_type="device",
            target_id=str(device.id),
            metadata={"asset_type": device.asset_type, "lifecycle": device.lifecycle},
        )
        return _row_to_out(device, None, None)


@router.patch("/{device_id}", response_model=DeviceOut)
async def update_asset(
    device_id: UUID,
    body: AssetUpdateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("assets.manage")),
) -> DeviceOut:
    changes = body.model_dump(exclude_unset=True)
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        row = (
            await session.execute(
                select(Device, Agent.status, Agent.cert_not_after)
                .join(Agent, isouter=True)
                .where(Device.id == device_id)
            )
        ).first()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such device")
        device, agent_status, not_after = row

        # MACHINE-REPORTED FIELDS ARE NOT EDITABLE ON A MANAGED DEVICE. The next
        # inventory run overwrites them, so accepting the edit and reverting it
        # silently hours later is worse than refusing it now. `owner` and
        # `location` are facts about the ORGANISATION and stay editable.
        machine_owned = {"hostname", "os_family", "serial"} & changes.keys()
        if agent_status is not None and machine_owned:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{', '.join(sorted(machine_owned))} is reported by the agent on "
                "this device and would be overwritten by the next inventory run",
            )

        for field, value in changes.items():
            setattr(device, field, str(value) if field == "primary_ip" and value else value)
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="asset.updated",
            target_type="device",
            target_id=str(device.id),
            metadata={"fields": sorted(changes)},
        )
        return _row_to_out(device, agent_status, not_after)
```

Imports to add: `ConfigDict`, `Field`, `IPvAnyAddress` from `pydantic`; `HTTPException`, `status` from `fastapi`; `audit_service` from `app.core.audit`.

- [x] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/pytest tests/test_asset_api.py -q`
Expected: PASS (7 tests)

- [x] **Step 5: The route sweep must still see every route gated**

Run: `cd backend && .venv/bin/pytest tests/test_rbac.py -q`
Expected: PASS. If this repository has a per-module completeness sweep for `/devices`, the two new routes must appear in it; add them if the sweep asserts an exact count.

- [x] **Step 6: mypy and ruff**

Run: `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: clean

- [x] **Step 7: Commit**

```bash
git add backend/app/api/v1/devices.py backend/tests/test_asset_api.py
git commit -m "asset-inventory: create and edit an asset by hand"
```

---

### Task 5: Stock is not offline (falsifier 7)

**Files:**
- Modify: `backend/app/modules/health/sweep.py:458` (`run_offline_sweep`)
- Create: `backend/tests/test_health_offline_lifecycle.py`

- [x] **Step 1: Write the failing test**

`backend/tests/test_health_offline_lifecycle.py`:

```python
"""A boxed laptop is not offline (docs/modules/asset-inventory/spec.md §5).

WHAT IS ALREADY SAFE, so the test says what it actually covers.
`run_offline_sweep` joins `agents`, so a record with NO agent can never reach
it -- an asset the operator typed in cannot raise an offline alert today. The
reachable case is narrower and real: a machine that WAS deployed, still has its
agent installed, and has gone back to the warehouse. Its lifecycle says `stock`
and it will never answer again until somebody unboxes it.

WHY IT MATTERS MORE THAN ONE ALERT. An alert an operator learns to ignore is
worse than no alert: the next one that matters is ignored the same way. Spec §5
calls this the first thing the feature would do wrong.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.core.tenancy import org_session
from app.models import Agent, Device
from app.modules.health.sweep import run_offline_sweep
from tests.conftest import ApiEnv


async def _stale_device(api_env: ApiEnv, org_id: UUID, lifecycle: str) -> UUID:
    long_ago = datetime.now(UTC) - timedelta(days=30)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        device = Device(
            org_id=org_id,
            hostname=f"{lifecycle}-01",
            os_family="linux",
            lifecycle=lifecycle,
        )
        session.add(device)
        await session.flush()
        session.add(
            Agent(
                org_id=org_id,
                device_id=device.id,
                cert_fingerprint=f"fp-{device.id.hex[:16]}",
                cert_serial="01",
                cert_not_after=datetime.now(UTC) + timedelta(days=365),
                last_seen_at=long_ago,
            )
        )
        await session.commit()
        return device.id


async def test_a_stock_machine_raises_no_offline_alert(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("assetlc")
    await _stale_device(api_env, org_id, "stock")
    async with org_session(org_id, engine=api_env.app_engine) as session:
        result = await run_offline_sweep(session, org_id)
    assert result["opened"] == 0, result


async def test_an_unmanageable_or_retired_record_raises_none_either(
    api_env: ApiEnv,
) -> None:
    org_id = await api_env.create_org("assetlc")
    await _stale_device(api_env, org_id, "unmanageable")
    await _stale_device(api_env, org_id, "retired")
    async with org_session(org_id, engine=api_env.app_engine) as session:
        result = await run_offline_sweep(session, org_id)
    assert result["opened"] == 0, result
```

**Non-vacuity is required here and is not optional.** The sweep returns `opened: 0` for an org with no health profile, so all three tests above pass over a sweep that does nothing at all. Whatever fixture this suite already uses to give an org an `agent.offline` check (see `tests/test_health_sweep*.py`) must be applied, plus this test:

```python
async def test_an_in_service_machine_DOES_raise_one(api_env: ApiEnv) -> None:
    """Non-vacuity for both tests above. Without this, a sweep that opened
    nothing for any reason -- no profile, no check, an exception swallowed --
    would report them green while covering nothing."""
    org_id = await api_env.create_org("assetlc")
    await _stale_device(api_env, org_id, "in_service")
    async with org_session(org_id, engine=api_env.app_engine) as session:
        result = await run_offline_sweep(session, org_id)
    assert result["opened"] == 1, result
```

- [x] **Step 2: Run to verify the in-service case passes and the stock case fails**

Run: `cd backend && .venv/bin/pytest tests/test_health_offline_lifecycle.py -q`
Expected: `test_a_stock_machine_raises_no_offline_alert` FAILS with `opened == 1`; the non-vacuity test PASSES. **If the non-vacuity test fails, fix the fixture before touching the sweep** — otherwise the next step is verified by nothing.

- [x] **Step 3: Add the predicate**

In `run_offline_sweep`'s query:

```python
            .where(
                Device.org_id == org_id,
                Agent.org_id == org_id,
                Device.status == "active",
                # ONLY `in_service` RECORDS CAN BE OFFLINE. A machine in the
                # warehouse is not down, a switch was never expected to check
                # in, and a retired record is not a to-do item. Alerting on any
                # of the three trains an operator to ignore this alert, which
                # costs the next real one (asset-inventory spec §5).
                Device.lifecycle == "in_service",
                (Agent.last_seen_at.is_(None)) | (Agent.last_seen_at < cutoff),
            )
```

- [x] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/pytest tests/test_health_offline_lifecycle.py tests/test_health_sweep.py -q`
Expected: PASS, including the existing health sweep suite.

- [x] **Step 5: Commit**

```bash
git add backend/app/modules/health/sweep.py backend/tests/test_health_offline_lifecycle.py
git commit -m "asset-inventory: a boxed laptop is not offline"
```

---

### Task 6: The fleet list becomes an asset list

**Files:**
- Modify: `frontend/src/lib/api.ts:303-327` (`Device`), plus two client methods
- Modify: `frontend/src/modules/devices/DevicesPage.tsx`
- Modify: `frontend/src/modules/devices/DevicesPage.test.tsx`

**Interfaces:**
- Consumes: `POST /api/v1/devices`, `PATCH /api/v1/devices/{id}`, and `DeviceOut`'s new fields (Tasks 2, 4).
- Produces: `api.createAsset(body)` and `api.updateAsset(id, body)`.

- [x] **Step 1: Write the failing tests**

Append to `frontend/src/modules/devices/DevicesPage.test.tsx`:

```tsx
it("counts managed endpoints and total assets as different numbers", async () => {
  // The spec's §9 question, answered in the console: "Managed endpoints" must
  // mean "has an agent". Counting rows was correct until rows could be
  // warehouse stock, and the card would then quietly mean something else while
  // reading the same.
  renderPage(() =>
    Promise.resolve({
      items: [
        device({ id: "1", agent_status: "online" }),
        device({ id: "2", hostname: "core-sw-01", agent_status: null, source: "manual" }),
      ],
      total: 2,
    }),
  );
  await screen.findByText("core-sw-01");
  const values = [...document.querySelectorAll(".stat-value")].map((n) => n.textContent);
  expect(values[0]).toBe("1"); // managed: one agent
  expect(values[1]).toBe("2"); // assets: both rows
});

it("says an asset has no agent rather than showing it as offline", async () => {
  // "Offline" is a claim about a machine that should be answering. A switch is
  // not offline; a boxed laptop is not offline. Rendering them in the same
  // column as a crashed server is the lie this page already refuses elsewhere.
  renderPage(() =>
    Promise.resolve({
      items: [device({ id: "2", hostname: "core-sw-01", agent_status: null })],
      total: 1,
    }),
  );
  await screen.findByText("core-sw-01");
  expect(screen.getByText(/no agent/i)).toBeInTheDocument();
  expect(screen.queryByText(/^offline$/i)).not.toBeInTheDocument();
});

it("renders an asset with no hostname without inventing one", async () => {
  renderPage(() =>
    Promise.resolve({
      items: [
        device({
          id: "3",
          hostname: null,
          os_family: null,
          description: "boxed laptop, PO 4471",
          primary_ip: "172.22.104.55",
        }),
      ],
      total: 1,
    }),
  );
  // The IP is the identity this row actually has; "None" and "" are not.
  expect(await screen.findByText("172.22.104.55")).toBeInTheDocument();
  expect(screen.queryByText("None")).not.toBeInTheDocument();
});
```

The file's `device()` helper needs the new optional fields; give them today's values as defaults so every existing test is unaffected.

- [x] **Step 2: Run to verify they fail**

Run: `cd frontend && npx vitest run src/modules/devices`
Expected: FAIL — one stat card, and `hostname: null` is a type error plus a blank cell.

- [x] **Step 3: Implement**

`frontend/src/lib/api.ts`, on `Device`:

```ts
  /** NULL for an asset with no agent: a switch, or hardware still in a box. */
  hostname: string | null;
  os_family: "windows" | "linux" | "macos" | null;
  lifecycle: "in_service" | "stock" | "unmanageable" | "retired";
  asset_type:
    | "computer"
    | "server"
    | "network"
    | "printer"
    | "phone"
    | "appliance"
    | "other";
  source: "agent" | "manual" | "discovery";
  description: string | null;
  primary_ip: string | null;
  owner: string | null;
  location: string | null;
```

and the two methods, beside the existing device calls:

```ts
  createAsset(body: AssetInput): Promise<Device> {
    return this.request<Device>("POST", "/devices", body);
  }

  updateAsset(id: string, body: Partial<AssetInput>): Promise<Device> {
    return this.request<Device>("PATCH", `/devices/${id}`, body);
  }
```

(Match the surrounding file's own request helper; the names above are illustrative of the shape, not of a helper this codebase necessarily has.)

`DevicesPage.tsx`:

```tsx
// One name for the row, in the order an operator would recognise it. `??` is
// wrong here: an empty hostname is as unhelpful as a missing one.
function assetLabel(device: Device): string {
  return device.hostname || device.primary_ip || device.description || "Unnamed asset";
}
```

- Stat cards become `<StatsGrid columns={4}>` with **Managed endpoints** = `items.filter(d => d.agent_status !== null).length`, and a new **Assets** card = `devices.data?.total`.
- The Agent column renders `No agent` for `agent_status === null`, not `offline`.
- The device cell uses `assetLabel(device)`, with `description` as the sub-line when there is no FQDN.
- Add an **Add asset** action to `PageHead`, opening a dialog with name, description, IP, type and lifecycle, calling `createAsset` and invalidating the devices query. Gate it on `assets.manage` from `me.permissions`, the same way this page already gates other actions.

- [x] **Step 4: Run the tests**

Run: `cd frontend && npx vitest run src/modules/devices && npx tsc --noEmit`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add frontend/src/lib/api.ts frontend/src/modules/devices/DevicesPage.tsx frontend/src/modules/devices/DevicesPage.test.tsx
git commit -m "asset-inventory: the fleet list holds assets, and counts them separately"
```

---

### Task 7: The device page stops offering what an asset cannot have

**Files:**
- Modify: `frontend/src/modules/inventory/DeviceDetailPage.tsx:39-56, 108-186`
- Create: `frontend/src/modules/inventory/DeviceDetailPage.assets.test.tsx`

**Interfaces:**
- Consumes: `Device.agent_status`, `.lifecycle` (Task 6's types).

- [x] **Step 1: Write the failing test**

`frontend/src/modules/inventory/DeviceDetailPage.assets.test.tsx`:

```tsx
/**
 * An agentless asset's device page (asset-inventory spec §5).
 *
 * THIS IS THE SAME RULE `DevicesPage.test.tsx` ALREADY ENFORCES for columns,
 * applied to tabs: "a screen that looks finished and lies is worse than one
 * that is visibly incomplete, and it lies most convincingly on the demo where
 * somebody decides to trust it."
 *
 * A Patch tab on a switch can never populate. Rendering it -- even empty, even
 * with a zero -- tells an operator this asset is patch-compliant, which is not
 * a weaker claim than a wrong number. It is the same claim.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// ... mock useApi as DeviceDetailPage.test.tsx does, returning a device with
// agent_status: null and lifecycle as each test needs.

describe("a device page for an asset with no agent", () => {
  it("does not offer Patch, Deploy, Backup, Provisioning or Health", async () => {
    renderPage({ agent_status: null, lifecycle: "in_service" });
    await screen.findByText("core-sw-01");
    for (const label of ["Patch", "Deploy", "Backup", "Provisioning", "Health"]) {
      expect(screen.queryByRole("tab", { name: label })).not.toBeInTheDocument();
    }
  });

  it("says why, and offers the install command", async () => {
    renderPage({ agent_status: null, lifecycle: "in_service" });
    expect(await screen.findByText(/no agent on this asset/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /agent packages/i })).toBeInTheDocument();
  });

  it("does not offer an agent for a switch", async () => {
    // `unmanageable` means the operator said this will never have an agent.
    // Offering one is noise on every row of a network estate.
    renderPage({ agent_status: null, lifecycle: "unmanageable" });
    await screen.findByText(/no agent on this asset/i);
    expect(screen.queryByRole("link", { name: /agent packages/i })).not.toBeInTheDocument();
  });

  it("still offers everything for a managed device", async () => {
    // Non-vacuity: a page that hid the tabs unconditionally would pass all
    // three tests above and remove Patch from the entire fleet.
    renderPage({ agent_status: "online", lifecycle: "in_service" });
    expect(await screen.findByRole("tab", { name: "Health" })).toBeInTheDocument();
  });
});
```

- [x] **Step 2: Run to verify it fails**

Run: `cd frontend && npx vitest run src/modules/inventory`
Expected: FAIL — every tab renders regardless.

- [x] **Step 3: Implement**

The page needs the device record itself, not only its inventory snapshot (an agentless asset has no snapshot):

```tsx
  const device = useQuery<Device>({
    queryKey: ["device", deviceId],
    queryFn: () => api.device(deviceId as string),
    enabled: deviceId != null,
  });
  // MANAGED IS A JOIN, and this is the console's view of it: `agent_status` is
  // null exactly when no `agents` row exists (asset-inventory spec §2).
  const managed = device.data?.agent_status != null;
```

`tabItems` becomes:

```tsx
  const tabItems = [
    ...CORE_TABS,
    // Patch, Deploy, Backup, Provisioning and Health CANNOT populate without an
    // agent. Rendering them empty is the "looks finished and lies" failure this
    // page's sibling test file names -- so they are absent, not zeroed.
    ...(managed && canReadPatch ? [{ key: "patch" as const, label: "Patch" }] : []),
    ...(managed && canReadDeploy ? [{ key: "deploy" as const, label: "Deploy" }] : []),
    ...(managed && canReadBackup ? [{ key: "backup" as const, label: "Backup" }] : []),
    ...(managed && canReadProvisioning
      ? [{ key: "provisioning" as const, label: "Provisioning" }]
      : []),
    ...(managed ? [{ key: "health" as const, label: "Health" }] : []),
  ];
```

and above the tabs, when `!managed`:

```tsx
        <Panel>
          <h2 className="text-base font-semibold">No agent on this asset</h2>
          <p className="mt-1 text-sm text-[var(--muted)]">
            {device.data?.lifecycle === "unmanageable"
              ? "This asset is marked unmanageable — it is recorded here for inventory, and no agent will ever run on it."
              : "Patch, deployment, backup, provisioning and health need an agent on the machine. Install one to see them."}
          </p>
          {device.data?.lifecycle !== "unmanageable" && (
            <Link className="btn mt-3 inline-block" to="/agent-packages">
              Agent packages
            </Link>
          )}
        </Panel>
```

Guard the stat cards too: `Last sweep` on an asset that never had one should read `never`, which it already does, but `CPU`/`Memory`/`Serial` come from the inventory snapshot and will read `—`. That is honest and needs no change.

- [x] **Step 4: Run the tests and the whole suite**

Run: `cd frontend && npx vitest run && npx tsc --noEmit`
Expected: green

- [x] **Step 5: Commit**

```bash
git add frontend/src/modules/inventory/DeviceDetailPage.tsx frontend/src/modules/inventory/DeviceDetailPage.assets.test.tsx
git commit -m "asset-inventory: an agentless asset's page omits what it cannot have"
```

---

### Task 8: Close part 1 in the docs

**Files:**
- Modify: `docs/modules/asset-inventory/spec.md`
- Modify: `docs/06-roadmap.md`

- [x] **Step 1: Update the status block**

Say which falsifiers this part covers and which belong to parts 2 and 3, in the shape the agent-distribution spec uses. Honest accounting:

- **Covered here:** 5 (read paths), 7 (stock is not offline), 9 (tenancy), and 6 (the tabs) — with 6 covered in the console only.
- **Parts 2 and 3:** 1, 2, 3, 4 and 8 are about discovery and enrolment matching. **None of them is tested, because none of the code exists.** Say so explicitly rather than leaving a §6 that reads as a checklist somebody might assume was worked through.
- **Not verified anywhere:** nothing in part 1 has run against a real deployment.

- [x] **Step 2: Add the roadmap row**

`docs/06-roadmap.md`, Phase 1 (Inventory) already exists — add a line naming asset inventory as an extension of it, with the same reasoning the agent-distribution entry uses. `backend/tests/test_roadmap_status.py` maps phases to backend packages; this feature adds no package (the code lives in `api/v1/devices.py` and `models/core.py`), so **no change to that test is needed** — verify by running it.

- [x] **Step 3: Run the roadmap test**

Run: `cd backend && .venv/bin/pytest tests/test_roadmap_status.py -q`
Expected: PASS unchanged. If it goes red, the change added a package and the set in that file needs a decision, not a silent edit.

- [x] **Step 4: Commit**

```bash
git add docs/modules/asset-inventory/spec.md docs/06-roadmap.md
git commit -m "asset-inventory: part 1 status, with parts 2 and 3 named as unbuilt"
```

---

## Verification before claiming completion

- `cd backend && .venv/bin/pytest -q` — green. **The whole suite, not this module's tests**: Task 1 makes two long-standing `NOT NULL` columns nullable, and the consumers that breaks are in other modules.
- `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests` — clean
- `alembic upgrade head → downgrade 0060 → upgrade head` — three exit-0 runs against a real Postgres
- `cd frontend && npx vitest run && npx tsc --noEmit` — green
- Falsifiers 5, 6, 7 and 9 each map to a named test above; 1, 2, 3, 4 and 8 are recorded in the spec status as belonging to parts 2 and 3 and **untested today**
- Every guard added was falsified by planting the regression it exists for, with the plant confirmed on disk before the result was read
