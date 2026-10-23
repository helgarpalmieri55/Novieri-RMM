# Patch Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the control-plane server slice of fleet patch management — scan ingestion + patch catalog, per-device status, approval workflow, deployment rings + maintenance windows, signed `patch.apply` dispatch gated on approval, reboot policy, and compliance reporting — all test-first with the agent per-OS engines mocked.

**Architecture:** A new `app/modules/patch/` package registers into the kernel exactly as Inventory and Remote Control do: two signed job types with result handlers (`patch.scan`, `patch.apply`), API routes, migration 0011, a Celery worker sweep, and a frontend folder. Approval is the server-side security gate — `patch.apply` can only ever dispatch `(device, patch)` pairs already `approved` for the device's ring, within an open maintenance window.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 (async/asyncpg), Alembic, Pydantic v2, Celery; React 19 + TS strict + Vite + TanStack Query frontend. No new backend/frontend dependencies.

## Global Constraints

- Python 3.12, FastAPI, SQLAlchemy 2.x typed, Pydantic v2, async throughout. `mypy app` must pass; `ruff check app tests` must pass.
- Every `org_id` table: `ENABLE` + `FORCE ROW LEVEL SECURITY` + a `tenant_isolation` policy `USING (org_id = current_setting('app.current_org', true)::uuid)` and matching `WITH CHECK`. The Phase-0 RLS-completeness test requires it.
- Migrations forward-only; never edit a shipped migration. Next revision is **0011**, `down_revision = "0010"`.
- Every privileged route uses `Depends(require_permission("<code>"))`; RBAC is server-side only. Device scope enforced as `jobs.dispatch` does (inside `org_session`, RLS-scoped).
- **Approval is the security gate:** `patch.apply` dispatch selects only `(device, patch)` pairs whose `device_patch_status.state == "approved"` AND an un-revoked `patch_approval` covers the device's ring AND the device's maintenance window is open — enforced server-side at dispatch, independent of caller.
- Business logic in services, not route handlers. Async engine sessions via `org_session(org_id, engine=...)`.
- Audit every privileged action via `audit_service.record(session, org_id=…, actor_type=…, actor_id=…, action=…, target_type=…, target_id=…, metadata=…)`. Never log/return secrets (this module handles none — agent uses local privilege).
- Severity vocabulary: `critical|important|moderate|low|unknown`. Security-severity = `critical` or `important`. Patch state vocabulary: `missing|approved|installed|failed|not_applicable`. Reboot policy: `never|if_required|forced`.
- Sentinel is NOT integrated: `patches.priority` is a nullable seam field; v1 orders by severity. Do not build a Sentinel client.

## Interfaces already in the codebase (consume, do not redefine)

- `app.jobs.registry`: `JobType(name, params_model, result_handler=None)`, `register(job_type)`, `get_job_type(name)`. `result_handler` is `async (session, job, result: dict) -> None`, invoked by `JobEngine.complete()` inside the job's tenant transaction; a raising handler fails the job.
- `app.jobs.engine.JobEngine`: `create(session, *, org_id, type, target_device_id, params, requested_by=None, ttl_s=None) -> Job`; `signed_envelope(job) -> dict`. Available at `app.state.job_engine`.
- `app.gateway.dispatch`: `publish_job(redis, device_id, envelope)`, `queue_ui_event(session, org_id, event)` (post-commit UI event drain).
- `app.api.deps`: `AuthContext(org_id, user_id, claims)`, `require_permission(code)`.
- `app.core.tenancy.org_session(org_id, *, engine)`; `app.core.audit.audit_service.record(...)`.
- Models (import from `app.models`): `Device` (`.device_group_id`, `.os_family`, `.os_version`), `DeviceGroup`, `Job`, `User`, `Organization`.
- Reference implementations to copy patterns from: `app/modules/inventory/handler.py` (result-handler registration), `app/modules/inventory/ingest.py` (ingest service), `app/modules/remote/policy.py` (device-group policy resolution robust to duplicate NULL org-defaults), `app/modules/remote/models.py` (`Base`/`TimestampMixin`/`uuid7_pk` + `__table_args__`), migration `0010_remote_control.py` (RLS helper + RBAC seed), `app/worker.py` (`retention_all_orgs`/`sweep_all_orgs` + beat), `app/modules/remote/api.py` (routes), `app/modules/__init__.py` (`register_all`), `app/gateway/main.py` (register module job types in the gateway process too).

---

### Task 1: Migration 0011 — tables, RLS, RBAC seeds

**Files:**
- Create: `backend/migrations/versions/0011_patch.py`
- Test: `backend/tests/test_patch_rbac.py`

**Interfaces:**
- Produces tables `patches`, `device_patch_status`, `patch_policies`, `patch_approvals`, `patch_runs`; permissions `patch.read`, `patch.scan`, `patch.approve`, `patch.apply`, `patch.policy.manage` with role grants.

- [ ] **Step 1: Write the failing test** — `backend/tests/test_patch_rbac.py`

```python
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import Permission, Role, RolePermission


async def _perms_for(session, role_name: str) -> set[str]:
    rows = (
        await session.execute(
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, Role.id == RolePermission.role_id)
            .where(Role.name == role_name, Role.org_id.is_(None))
        )
    ).scalars()
    return set(rows)


async def test_patch_permissions_seeded_and_granted(admin_engine: AsyncEngine) -> None:
    async with AsyncSession(admin_engine) as session:
        codes = set((await session.execute(select(Permission.code))).scalars())
        assert {"patch.read", "patch.scan", "patch.approve", "patch.apply",
                "patch.policy.manage"} <= codes

        tech = await _perms_for(session, "technician")
        assert {"patch.read", "patch.scan", "patch.apply"} <= tech
        assert "patch.approve" not in tech            # approve is privileged
        assert "patch.policy.manage" not in tech

        auditor = await _perms_for(session, "auditor")
        assert "patch.read" in auditor
        assert "patch.apply" not in auditor and "patch.approve" not in auditor

        readonly = await _perms_for(session, "read-only")
        assert readonly & {"patch.read"} == {"patch.read"}
        assert not (readonly & {"patch.scan", "patch.apply", "patch.approve"})

        for role in ("super-admin", "org-admin"):
            assert {"patch.read", "patch.scan", "patch.approve", "patch.apply",
                    "patch.policy.manage"} <= await _perms_for(session, role)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/test_patch_rbac.py -v`
Expected: FAIL (permissions not seeded).

- [ ] **Step 3: Write the migration** — `backend/migrations/versions/0011_patch.py` (copy the `_rls` helper and the permission/role-grant seed loop verbatim from `0010_remote_control.py`)

```python
"""Patch management: catalog, status, policies, approvals, runs, RBAC.

Revision ID: 0011
Revises: 0010
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = [
    ("patch.read", "patch", "read"),
    ("patch.scan", "patch", "scan"),
    ("patch.approve", "patch", "approve"),
    ("patch.apply", "patch", "apply"),
    ("patch.policy.manage", "patch", "policy.manage"),
]
ROLE_GRANTS = {
    "super-admin": ["patch.read", "patch.scan", "patch.approve", "patch.apply", "patch.policy.manage"],
    "org-admin": ["patch.read", "patch.scan", "patch.approve", "patch.apply", "patch.policy.manage"],
    "technician": ["patch.read", "patch.scan", "patch.apply"],
    "auditor": ["patch.read"],
    "read-only": ["patch.read"],
}


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        "USING (org_id = current_setting('app.current_org', true)::uuid) "
        "WITH CHECK (org_id = current_setting('app.current_org', true)::uuid)"
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE patches (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            os_family varchar(8) NOT NULL,
            source varchar(20) NOT NULL,
            identifier varchar(400) NOT NULL,
            title text NOT NULL,
            severity varchar(12) NOT NULL,
            kb varchar(40),
            cve text[],
            superseded_by uuid REFERENCES patches(id),
            priority integer,
            first_seen_at timestamptz NOT NULL DEFAULT now(),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (org_id, os_family, source, identifier)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE device_patch_status (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            device_id uuid NOT NULL REFERENCES devices(id),
            patch_id uuid NOT NULL REFERENCES patches(id),
            state varchar(16) NOT NULL,
            detected_at timestamptz NOT NULL DEFAULT now(),
            applied_at timestamptz,
            last_error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (device_id, patch_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_dps_state ON device_patch_status (org_id, state)")
    op.execute("CREATE INDEX ix_dps_patch ON device_patch_status (org_id, patch_id)")
    op.execute(
        """
        CREATE TABLE patch_policies (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            device_group_id uuid REFERENCES device_groups(id),
            ring_name varchar(40) NOT NULL DEFAULT 'broad',
            ring_order integer NOT NULL DEFAULT 100,
            maintenance_window varchar(120),
            auto_approve_security boolean NOT NULL DEFAULT false,
            reboot varchar(12) NOT NULL DEFAULT 'if_required',
            reboot_deferral_hours integer NOT NULL DEFAULT 24,
            scan_interval_hours integer NOT NULL DEFAULT 24,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (org_id, device_group_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE patch_approvals (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            patch_id uuid NOT NULL REFERENCES patches(id),
            ring_name varchar(40) NOT NULL,
            approved_by uuid NOT NULL REFERENCES users(id),
            approved_at timestamptz NOT NULL DEFAULT now(),
            revoked boolean NOT NULL DEFAULT false,
            revoked_by uuid REFERENCES users(id),
            revoked_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (org_id, patch_id, ring_name)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE patch_runs (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            job_id uuid NOT NULL REFERENCES jobs(id),
            device_id uuid NOT NULL REFERENCES devices(id),
            results jsonb NOT NULL DEFAULT '{}'::jsonb,
            reboot_required boolean NOT NULL DEFAULT false,
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_patch_runs_device ON patch_runs (org_id, device_id, id)")

    for t in ("patches", "device_patch_status", "patch_policies", "patch_approvals", "patch_runs"):
        _rls(t)

    for code, module, action in PERMISSIONS:
        op.execute(
            "INSERT INTO permissions (id, code, module, action) "
            f"VALUES (gen_random_uuid(), '{code}', '{module}', '{action}')"
        )
    for name, codes in ROLE_GRANTS.items():
        code_list = ", ".join(f"'{c}'" for c in codes)
        op.execute(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            f"WHERE r.name = '{name}' AND r.org_id IS NULL AND p.code IN ({code_list})"
        )


def downgrade() -> None:
    op.execute("DELETE FROM role_permissions WHERE permission_id IN "
               "(SELECT id FROM permissions WHERE module = 'patch')")
    op.execute("DELETE FROM permissions WHERE module = 'patch'")
    for t in ("patch_runs", "patch_approvals", "patch_policies", "device_patch_status", "patches"):
        op.execute(f"DROP TABLE IF EXISTS {t}")
```

> The seeded role grants also change the `PERMISSIONS` registry in `app/core/rbac.py` and two hardcoded-set tests (`test_rbac.py`, `test_api_routes.py`) — follow the exact precedent from the docs/remote modules: add the five `patch.*` codes to `app/core/rbac.py`'s `PERMISSIONS` list and update the expected sets in those two tests. Do this in THIS task so the suite stays green.

- [ ] **Step 4: Run migration + tests**

Run: `cd backend && .venv/bin/python -m pytest tests/test_patch_rbac.py tests/test_rbac.py tests/test_api_routes.py tests/test_migrations.py tests/test_rls.py -v`
Expected: PASS (RLS-completeness auto-covers the five new tables).

- [ ] **Step 5: Commit**

```bash
git add backend/migrations/versions/0011_patch.py backend/tests/test_patch_rbac.py backend/app/core/rbac.py backend/tests/test_rbac.py backend/tests/test_api_routes.py
git commit -m "feat(patch): migration 0011 — catalog/status/policy/approval/run tables, RLS, RBAC"
```

---

### Task 2: ORM models

**Files:**
- Create: `backend/app/modules/patch/__init__.py` (empty package marker; `register` added in Task 10)
- Create: `backend/app/modules/patch/models.py`
- Modify: `backend/app/models/__init__.py` (re-export the five models + add to `__all__`, matching how `remote` models are surfaced)
- Test: `backend/tests/test_patch_models.py`

**Interfaces:**
- Produces `Patch`, `DevicePatchStatus`, `PatchPolicy`, `PatchApproval`, `PatchRun` SQLAlchemy models whose columns/types/nullability/defaults/`__table_args__` match migration 0011 exactly. Reuse `Base`, `TimestampMixin`, `uuid7_pk` from `app.models.base` (see `app/modules/remote/models.py` for the exact imports and the `Text`/`BigInteger`/`Index`/`UniqueConstraint` usage — match migration column types precisely: `text`→`Text`, `text[]`→`ARRAY(Text)`, `jsonb`→ mapped `dict`, `integer`→`Integer`).

- [ ] **Step 1: Write the failing test**

```python
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import (Device, Organization, Patch, PatchApproval, PatchPolicy,
                        PatchRun, DevicePatchStatus, User)


async def _org_device_user(admin_engine: AsyncEngine):
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="pt", slug=f"pt-{uuid4().hex[:6]}"); s.add(org); await s.flush()
        dev = Device(org_id=org.id, hostname="h", os_family="windows"); s.add(dev)
        usr = User(org_id=org.id, external_subject=f"s{uuid4().hex[:6]}", email="a@b.c"); s.add(usr)
        await s.commit()
        return org.id, dev.id, usr.id


async def test_patch_and_status_roundtrip(admin_engine, app_engine) -> None:
    org_id, device_id, _ = await _org_device_user(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        p = Patch(org_id=org_id, os_family="windows", source="windows_update",
                  identifier="KB5034123", title="Cumulative Update", severity="critical")
        s.add(p); await s.flush()
        st = DevicePatchStatus(org_id=org_id, device_id=device_id, patch_id=p.id, state="missing")
        s.add(st); await s.flush()
        assert p.priority is None and p.cve is None
    async with org_session(org_id, engine=app_engine) as s:
        got = (await s.execute(select(DevicePatchStatus).where(DevicePatchStatus.patch_id == p.id))).scalar_one()
        assert got.state == "missing" and got.applied_at is None


async def test_policy_defaults(admin_engine, app_engine) -> None:
    org_id, _, _ = await _org_device_user(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        pol = PatchPolicy(org_id=org_id, device_group_id=None); s.add(pol); await s.flush()
        assert pol.ring_name == "broad" and pol.ring_order == 100
        assert pol.auto_approve_security is False and pol.reboot == "if_required"
        assert pol.reboot_deferral_hours == 24 and pol.scan_interval_hours == 24
```

- [ ] **Step 2: Run test to verify it fails** — Run: `cd backend && .venv/bin/python -m pytest tests/test_patch_models.py -v` → FAIL (ImportError).

- [ ] **Step 3: Write `models.py`** — define the five models. Use `from sqlalchemy import ARRAY, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, Index` and `from sqlalchemy.dialects.postgresql import JSONB` (or `mapped_column(JSONB)`); `id: Mapped[UUID] = uuid7_pk()`; `TimestampMixin, Base` from `app.models.base`. Columns and `__table_args__` MUST mirror migration 0011 (unique + index names identical). `Patch.cve: Mapped[list[str] | None] = mapped_column(ARRAY(Text), default=None)`; `Patch.priority: Mapped[int | None]`; `PatchRun.results: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)`. Re-export all five from `app/models/__init__.py`.

- [ ] **Step 4: Run test to verify it passes** — Run: `cd backend && .venv/bin/python -m pytest tests/test_patch_models.py -v && .venv/bin/mypy app` → PASS; mypy clean.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/patch/ backend/app/models/__init__.py backend/tests/test_patch_models.py
git commit -m "feat(patch): ORM models (Patch, DevicePatchStatus, PatchPolicy, PatchApproval, PatchRun)"
```

---

### Task 3: Policy resolution

**Files:**
- Create: `backend/app/modules/patch/policy.py`
- Test: `backend/tests/test_patch_policy.py`

**Interfaces:**
- Produces `ResolvedPatchPolicy` (frozen dataclass: `ring_name: str`, `ring_order: int`, `maintenance_window: str | None`, `auto_approve_security: bool`, `reboot: str`, `reboot_deferral_hours: int`, `scan_interval_hours: int`) and `async def resolve_patch_policy(session, org_id, device_id) -> ResolvedPatchPolicy`.
- Resolution order: device's group policy → org-default (`device_group_id IS NULL`, `order_by(id).limit(1)` — robust to duplicate NULL rows, mirroring `app/modules/remote/policy.py`) → hardcoded defaults (`ring_name="broad"`, `ring_order=100`, `maintenance_window=None`, `auto_approve_security=False`, `reboot="if_required"`, `reboot_deferral_hours=24`, `scan_interval_hours=24`).

- [ ] **Step 1: Write the failing test** (mirror `tests/test_remote_policy.py` structure)

```python
from uuid import uuid4
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from app.core.tenancy import org_session
from app.models import Device, DeviceGroup, Organization, PatchPolicy
from app.modules.patch.policy import resolve_patch_policy


async def _setup(admin_engine):
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="pp", slug=f"pp-{uuid4().hex[:6]}"); s.add(org); await s.flush()
        grp = DeviceGroup(org_id=org.id, name="servers"); s.add(grp); await s.flush()
        dev = Device(org_id=org.id, hostname="h", os_family="linux", device_group_id=grp.id)
        s.add(dev); await s.commit()
        return org.id, grp.id, dev.id


async def test_defaults_when_no_policy(admin_engine, app_engine) -> None:
    org_id, _, device_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        pol = await resolve_patch_policy(s, org_id, device_id)
    assert pol.ring_name == "broad" and pol.auto_approve_security is False
    assert pol.reboot == "if_required" and pol.scan_interval_hours == 24


async def test_group_policy_wins_and_dup_org_default_safe(admin_engine, app_engine) -> None:
    org_id, group_id, device_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        s.add(PatchPolicy(org_id=org_id, device_group_id=None, ring_name="broad"))
        s.add(PatchPolicy(org_id=org_id, device_group_id=None, ring_name="broad2"))  # duplicate NULL
        s.add(PatchPolicy(org_id=org_id, device_group_id=group_id, ring_name="test",
                          ring_order=0, auto_approve_security=True))
        await s.flush()
        pol = await resolve_patch_policy(s, org_id, device_id)
    assert pol.ring_name == "test" and pol.ring_order == 0 and pol.auto_approve_security is True
```

- [ ] **Step 2: Run to verify it fails** — `pytest tests/test_patch_policy.py -v` → ImportError.

- [ ] **Step 3: Write `policy.py`** — copy the exact shape of `app/modules/remote/policy.py`: `_from_row(row) -> ResolvedPatchPolicy`; look up `Device.device_group_id`; group query `.scalar_one_or_none()`; org-default query `.where(device_group_id.is_(None)).order_by(PatchPolicy.id).limit(1).scalar_one_or_none()`; fall back to `ResolvedPatchPolicy()` defaults.

- [ ] **Step 4: Run to verify it passes** — `pytest tests/test_patch_policy.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/patch/policy.py backend/tests/test_patch_policy.py
git commit -m "feat(patch): device-group patch policy resolution with safe defaults"
```

---

### Task 4: Result document contracts + job-type params + wire schema export

**Files:**
- Create: `backend/app/modules/patch/schema.py`
- Modify: `scripts/export_wire_schema.py` (repo root — add the new models to the import + export loop; run `backend/.venv/bin/python scripts/export_wire_schema.py` from repo root)
- Create (generated): `docs/kernel/wire-protocol/PatchScanDocument.schema.json`, `PatchApplyResult.schema.json`, `PatchScanParams.schema.json`, `PatchApplyParams.schema.json`
- Test: `backend/tests/test_patch_schema.py`

**Interfaces:**
- Produces (all `model_config = ConfigDict(extra="forbid")` where a wire contract): `PatchEntry` (`source: str`, `identifier: str`, `title: str`, `severity: Literal["critical","important","moderate","low","unknown"]`, `kb: str | None = None`, `cve: list[str] | None = None`, `superseded_by: str | None = None`); `PatchScanDocument` (`os_family: Literal["windows","linux","macos"]`, `patches: list[PatchEntry]`, `installed_identifiers: list[str] = []`, `scanned_at: str`); `PatchApplyOutcome` (`source: str`, `identifier: str`, `state: Literal["installed","failed"]`, `error: str | None = None`); `PatchApplyResult` (`results: list[PatchApplyOutcome]`, `reboot_required: bool = False`, `applied_at: str`); `PatchScanParams` (empty, extra=forbid); `PatchApplyParams` (`patches: list[dict[str,str]]` as `[{source, identifier}]`, `reboot: Literal["never","if_required","forced"]`, `deferral_hours: int = 24`).

- [ ] **Step 1: Write the failing test**

```python
import pytest
from pydantic import ValidationError
from app.modules.patch.schema import (PatchApplyParams, PatchApplyResult,
                                       PatchScanDocument)


def test_scan_document_validates_and_rejects_bad_severity() -> None:
    doc = PatchScanDocument(os_family="windows", scanned_at="2026-07-19T00:00:00Z",
        patches=[{"source": "windows_update", "identifier": "KB1", "title": "x",
                  "severity": "critical"}])
    assert doc.patches[0].severity == "critical"
    with pytest.raises(ValidationError):
        PatchScanDocument(os_family="windows", scanned_at="t", patches=[
            {"source": "s", "identifier": "i", "title": "t", "severity": "nope"}])


def test_apply_result_and_params() -> None:
    r = PatchApplyResult(applied_at="t", reboot_required=True, results=[
        {"source": "apt", "identifier": "openssl", "state": "installed"}])
    assert r.reboot_required is True
    p = PatchApplyParams(patches=[{"source": "apt", "identifier": "openssl"}], reboot="if_required")
    assert p.deferral_hours == 24
```

- [ ] **Step 2: Run to verify it fails** — `pytest tests/test_patch_schema.py -v` → ImportError.

- [ ] **Step 3: Write `schema.py`** with the Pydantic models above.

- [ ] **Step 4: Run + export** — `pytest tests/test_patch_schema.py -v`; then add `from app.modules.patch.schema import PatchApplyParams, PatchApplyResult, PatchScanDocument, PatchScanParams` (with `# noqa: E402`) to `scripts/export_wire_schema.py` and include them in the `for model in (...)` tuple; run `backend/.venv/bin/python scripts/export_wire_schema.py` from repo root → 4 new `.schema.json` files under `docs/kernel/wire-protocol/`. `ruff check` the edited exporter.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/patch/schema.py backend/tests/test_patch_schema.py scripts/export_wire_schema.py docs/kernel/wire-protocol/Patch*.schema.json
git commit -m "feat(patch): scan/apply wire contracts + exported schemas"
```

---

### Task 5: Scan ingestion (`ingest.py`)

**Files:**
- Create: `backend/app/modules/patch/ingest.py`
- Test: `backend/tests/test_patch_ingest.py`

**Interfaces:**
- Consumes: `resolve_patch_policy` (Task 3), `PatchScanDocument` (Task 4), models (Task 2).
- Produces: `class PatchValidationError(Exception)`; `@dataclass IngestStats(patches_seen, missing, installed, auto_approved)`; `async def ingest_scan(session, device, doc: PatchScanDocument) -> IngestStats`.
- Behavior: (1) upsert each `patches` row on `(org_id, os_family, source, identifier)` — update title/severity/kb/cve/superseded_by, never touch `priority`. (2) Reconcile `device_patch_status`: for each scanned patch not in `installed_identifiers` → ensure a row `missing` (unless it's already `approved`/`installed` — do not downgrade `approved`); for identifiers in `installed_identifiers` (format `"<source>:<identifier>"`) → set matching status `installed` + `applied_at=now` if newly installed; existing `missing`/`approved` rows for patches no longer reported and not installed → `not_applicable`. (3) Auto-approve (PD-02): resolve the device policy; if `auto_approve_security` and a newly-`missing` patch has severity in `{critical, important}`, create a `patch_approvals` row for the **`test`** ring (if none) and set that device's status to `approved`; count in `auto_approved`; audit `patch.auto_approved`. (4) Do NOT publish here — the handler (Task 10) queues the UI event.

- [ ] **Step 1: Write the failing test**

```python
from datetime import UTC, datetime
from uuid import uuid4
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from app.core.tenancy import org_session
from app.models import (Device, DeviceGroup, Organization, Patch, PatchApproval,
                        PatchPolicy, DevicePatchStatus)
from app.modules.patch.ingest import ingest_scan
from app.modules.patch.schema import PatchScanDocument


async def _dev(admin_engine, *, auto=False):
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="pi", slug=f"pi-{uuid4().hex[:6]}"); s.add(org); await s.flush()
        grp = DeviceGroup(org_id=org.id, name="g"); s.add(grp); await s.flush()
        dev = Device(org_id=org.id, hostname="h", os_family="windows", device_group_id=grp.id)
        s.add(dev)
        if auto:
            s.add(PatchPolicy(org_id=org.id, device_group_id=grp.id, ring_name="test",
                              auto_approve_security=True))
        await s.commit()
        return org.id, dev.id


def _doc(sev="critical", installed=None):
    return PatchScanDocument(os_family="windows", scanned_at=datetime.now(UTC).isoformat(),
        installed_identifiers=installed or [],
        patches=[{"source": "windows_update", "identifier": "KB1", "title": "Cum Update",
                  "severity": sev}])


async def test_ingest_creates_catalog_and_missing_status(admin_engine, app_engine) -> None:
    org_id, device_id = await _dev(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        stats = await ingest_scan(s, await s.get(Device, device_id), _doc())
        assert stats.missing == 1 and stats.auto_approved == 0
        st = (await s.execute(select(DevicePatchStatus))).scalar_one()
        assert st.state == "missing"


async def test_auto_approve_security_into_test_ring(admin_engine, app_engine) -> None:
    org_id, device_id = await _dev(admin_engine, auto=True)
    async with org_session(org_id, engine=app_engine) as s:
        stats = await ingest_scan(s, await s.get(Device, device_id), _doc(sev="critical"))
        assert stats.auto_approved == 1
        st = (await s.execute(select(DevicePatchStatus))).scalar_one()
        assert st.state == "approved"
        appr = (await s.execute(select(PatchApproval))).scalar_one()
        assert appr.ring_name == "test" and appr.revoked is False


async def test_auto_approve_skips_non_security(admin_engine, app_engine) -> None:
    org_id, device_id = await _dev(admin_engine, auto=True)
    async with org_session(org_id, engine=app_engine) as s:
        stats = await ingest_scan(s, await s.get(Device, device_id), _doc(sev="low"))
        assert stats.auto_approved == 0
        assert (await s.execute(select(DevicePatchStatus))).scalar_one().state == "missing"


async def test_installed_identifier_marks_installed(admin_engine, app_engine) -> None:
    org_id, device_id = await _dev(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        await ingest_scan(s, await s.get(Device, device_id),
                          _doc(installed=["windows_update:KB1"]))
        st = (await s.execute(select(DevicePatchStatus))).scalar_one()
        assert st.state == "installed" and st.applied_at is not None
```

- [ ] **Step 2: Run to verify it fails** — `pytest tests/test_patch_ingest.py -v` → ImportError.

- [ ] **Step 3: Write `ingest.py`** implementing the four steps. Use `audit_service.record(session, org_id=device.org_id, actor_type="system", actor_id=None, action="patch.auto_approved", target_type="patch", target_id=str(patch.id), metadata={"device_id": str(device.id), "ring": "test"})` for each auto-approval. Upsert catalog via select-then-insert-or-update on the unique key.

- [ ] **Step 4: Run to verify it passes** — `pytest tests/test_patch_ingest.py -v && .venv/bin/mypy app` → PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/patch/ingest.py backend/tests/test_patch_ingest.py
git commit -m "feat(patch): scan ingestion — catalog upsert, status reconcile, test-ring auto-approve"
```

---

### Task 6: Approval service (`approve.py`)

**Files:**
- Create: `backend/app/modules/patch/approve.py`
- Test: `backend/tests/test_patch_approve.py`

**Interfaces:**
- Produces `async def approve_patch(session, *, org_id, actor_id, patch_id, ring_name) -> PatchApproval` and `async def revoke_approval(session, *, org_id, actor_id, approval_id) -> bool`.
- `approve_patch`: create (or un-revoke) a `patch_approvals` row for `(patch, ring)`; flip every `device_patch_status` for devices whose resolved ring == `ring_name` and current state == `missing` to `approved`; audit `patch.approved` (actor=user, target_type="patch", metadata `{ring_name, devices_flipped}`). Idempotent (existing un-revoked approval returns it).
- `revoke_approval`: set `revoked`/`revoked_by`/`revoked_at`; flip `approved` (not yet `installed`) statuses for that patch+ring back to `missing`; audit `patch.revoked`. Returns False if not found.
- To decide a device's ring, reuse `resolve_patch_policy(session, org_id, device.id).ring_name`.

- [ ] **Step 1: Write the failing test** — create org+group(policy ring="test")+device+a `Patch`+a `missing` status; `approve_patch(ring_name="test")` → status flips to `approved`, an un-revoked approval exists, audit `patch.approved` present. A device in a DIFFERENT ring (`broad`) is NOT flipped. `revoke_approval` flips back to `missing` and audits `patch.revoked`. (Write full test bodies mirroring Task 5's setup helpers.)

- [ ] **Step 2: Run to verify it fails** → ImportError.

- [ ] **Step 3: Write `approve.py`.** For the flip, iterate devices in the org, resolve each device's ring, and update matching statuses (v1 fleet sizes make a per-device resolve acceptable; add a `# NOTE:` that a denormalized device→ring column is a later optimization).

- [ ] **Step 4: Run to verify it passes** — `pytest tests/test_patch_approve.py -v && .venv/bin/mypy app` → PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/patch/approve.py backend/tests/test_patch_approve.py
git commit -m "feat(patch): approval service — approve/revoke flips device status per ring, audited"
```

---

### Task 7: Maintenance-window evaluation + apply dispatch (`apply.py`)

**Files:**
- Create: `backend/app/modules/patch/apply.py`
- Test: `backend/tests/test_patch_apply.py`

**Interfaces:**
- Produces `def window_open(cron: str | None, now: datetime) -> bool` (None = always open; a 5-field cron matched to the minute — implement a small matcher supporting `*`, `*/n`, comma lists, and ranges on minute/hour/dom/month/dow; do NOT add a cron dependency), and `async def dispatch_apply(session, job_engine, redis, *, org_id, now, device_ids=None) -> list[UUID]`.
- `dispatch_apply`: select `device_patch_status` rows where `state == "approved"`; group by device; for each device whose resolved policy window `window_open(policy.maintenance_window, now)` is true (and, if `device_ids` given, in that set), build ONE signed `patch.apply` job (`job_engine.create(..., type="patch.apply", target_device_id=device_id, params=PatchApplyParams(patches=[{source,identifier}...], reboot=policy.reboot, deferral_hours=policy.reboot_deferral_hours).model_dump())`), `publish_job(redis, device_id, envelope)`, create a `PatchRun(job_id, device_id)`, audit `patch.apply_dispatched`. Returns the dispatched job ids. A device outside its window is skipped (no job). **Guard:** re-verify each selected patch has an un-revoked `patch_approval` for the device's ring before including it (approval is the gate).

- [ ] **Step 1: Write the failing test** — cover: `window_open(None, now) is True`; `window_open("*/5 * * * *", ...)` true/false by minute; a device with an `approved` status + open window + valid approval → exactly one `patch.apply` job created (fake job_engine/redis like `tests/test_remote_service.py`) + a `PatchRun` row + audit `patch.apply_dispatched`; a device whose window is CLOSED → no job; an `approved` status whose approval was revoked → NOT dispatched (negative gate test).

- [ ] **Step 2: Run to verify it fails** → ImportError.

- [ ] **Step 3: Write `apply.py`** (`window_open` matcher + `dispatch_apply`). Reuse the `_FakeJobEngine`/`_FakeRedis` pattern from `tests/test_remote_service.py` in the test.

- [ ] **Step 4: Run to verify it passes** — `pytest tests/test_patch_apply.py -v && .venv/bin/mypy app` → PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/patch/apply.py backend/tests/test_patch_apply.py
git commit -m "feat(patch): apply dispatch gated on approval + open window; cron window matcher"
```

---

### Task 8: Apply-result ingestion + job-type registration (`handler.py`)

**Files:**
- Create: `backend/app/modules/patch/handler.py`
- Modify: `backend/app/modules/patch/apply.py` (add `async def ingest_apply_result(session, job, result: PatchApplyResult) -> None`)
- Test: `backend/tests/test_patch_result.py`

**Interfaces:**
- Produces `register_job_types()` that registers BOTH `JobType("patch.scan", PatchScanParams, result_handler=<scan handler>)` and `JobType("patch.apply", PatchApplyParams, result_handler=<apply handler>)`. The scan handler validates `PatchScanDocument(**result)`, loads the `Device`, calls `ingest_scan`, and `queue_ui_event(session, job.org_id, {"kind":"patch_status_updated","device_id":...,"job_id":...})`. The apply handler validates `PatchApplyResult(**result)`, calls `ingest_apply_result`, and queues the same UI event kind.
- `ingest_apply_result(session, job, result)`: for each outcome, update the matching `device_patch_status` (by resolving the `patches` row for `(source, identifier)`) to `installed` (set `applied_at`) or `failed` (set `last_error`); update the `PatchRun` for `job.id` with `results=result.model_dump()`, `reboot_required`, `finished_at=now`; audit `patch.applied` with an outcome summary `{installed: n, failed: m, reboot_required: bool}`.

- [ ] **Step 1: Write the failing test** — a `PatchRun` + a `Patch` + an `approved` `device_patch_status`; feed a `PatchApplyResult` with one `installed` and (separately) one `failed`; assert status → `installed`/`failed` with `applied_at`/`last_error`, the run gets `results`+`reboot_required`+`finished_at`, and audit `patch.applied`. Plus: `register_job_types()` registers both types with non-None handlers (`get_job_type("patch.scan").result_handler is not None`).

- [ ] **Step 2: Run to verify it fails** → ImportError.

- [ ] **Step 3: Write `handler.py` + `ingest_apply_result`.**

- [ ] **Step 4: Run to verify it passes** — `pytest tests/test_patch_result.py -v && .venv/bin/mypy app` → PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/patch/handler.py backend/app/modules/patch/apply.py backend/tests/test_patch_result.py
git commit -m "feat(patch): apply-result ingestion + patch.scan/patch.apply job-type registration"
```

---

### Task 9: Reporting (`reporting.py`)

**Files:**
- Create: `backend/app/modules/patch/reporting.py`
- Test: `backend/tests/test_patch_reporting.py`

**Interfaces:**
- Produces `async def device_report(session, org_id, device_id) -> dict` (counts by state + by severity, pending reboots from latest run) and `async def fleet_report(session, org_id) -> dict` (compliant % by os_family and by severity, exposed-device list ordered by `priority` desc-nulls-last then severity) and `async def export_rows(session, org_id) -> list[dict]` (flat device×patch rows for evidence export). Pure aggregation over the tables; no new writes.

- [ ] **Step 1: Write the failing test** — seed a couple devices with mixed `missing`/`installed` statuses across severities; assert `fleet_report` compliant-% math and that `device_report` returns per-state counts; `export_rows` returns one row per `(device, patch)` with device hostname, patch identifier, severity, state.

- [ ] **Step 2–5:** RED → implement `reporting.py` → GREEN (`pytest tests/test_patch_reporting.py -v`) → commit `feat(patch): per-device + fleet compliance reporting and export rows`.

---

### Task 10: API routes + module registration

**Files:**
- Create: `backend/app/modules/patch/api.py`
- Modify: `backend/app/modules/patch/__init__.py` (`register(app)`), `backend/app/modules/__init__.py` (`register_all` → `patch.register(app)`), `backend/app/gateway/main.py` (call `register_job_types()` in gateway startup next to inventory/remote)
- Test: `backend/tests/test_patch_api.py`

**Interfaces:**
- Routes under `/api/v1/patch` (§12 of the spec): `GET /patches` (`patch.read`), `GET /devices/{device_id}/status` (`patch.read`), `POST /scan` (`patch.scan`), `POST /approvals` (`patch.approve`), `DELETE /approvals/{id}` (`patch.approve`), `POST /apply` (`patch.apply`), `GET|PUT /policies` (`patch.read`/`patch.policy.manage`; PUT upserts a single org-default like `remote` `PUT /policies`), `GET /report` (`patch.read`), `GET /report/export` (`patch.read`, streams; audit `patch.report_exported`). Pydantic response models; no secret fields (module has none). `register(app)` calls `register_job_types()` + mounts the router; construct nothing stateful beyond what the handlers need. `POST /scan` and `POST /apply` build/dispatch via `app.state.job_engine` + `app.state.redis`; `POST /apply` calls `dispatch_apply(...)` with `now=datetime.now(UTC)` computed in the handler (NOT via a blocked clock).

- [ ] **Step 1: Write the failing test** (use `api_env` like `tests/test_remote_api.py`) — assert: `GET /patches` allowed for a `read-only` user, `POST /approvals` FORBIDDEN (403) for a technician but OK for org-admin, `POST /apply` allowed for technician, `POST /scan` dispatches (fake redis sees a publish), `PUT /policies` upserts a single org-default (second PUT updates, not inserts). Mirror helpers from `tests/test_remote_api.py` (enrolled/created device via admin engine).

- [ ] **Step 2: Run to verify it fails** → FAIL.

- [ ] **Step 3: Implement `api.py`** + wire `register(app)`, `register_all`, and the gateway `register_job_types()` call.

- [ ] **Step 4: Run to verify it passes** — `pytest tests/test_patch_api.py -v && .venv/bin/mypy app && .venv/bin/ruff check app tests` → PASS, clean. Then run the FULL suite (`.venv/bin/python -m pytest -q`) — this task touches shared wiring.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/patch/api.py backend/app/modules/patch/__init__.py backend/app/modules/__init__.py backend/app/gateway/main.py backend/tests/test_patch_api.py
git commit -m "feat(patch): API routes, module + gateway registration"
```

---

### Task 11: Worker sweeps (scan cadence + window apply)

**Files:**
- Modify: `backend/app/worker.py` (add two async sweeps + Celery task shells + beat entries)
- Test: `backend/tests/test_patch_worker.py`

**Interfaces:**
- Produces `async def patch_scan_sweep_all_orgs(now) -> int` (per org, dispatch `patch.scan` to devices whose latest scan — max `device_patch_status.detected_at` for the device, or never — is older than the resolved policy `scan_interval_hours`; returns count dispatched) and `async def patch_apply_sweep_all_orgs(now) -> int` (per org, call `dispatch_apply(session, job_engine, redis, org_id=…, now=now)`; returns total dispatched). Beat entries: `patch-scan-sweep` (`3600.0`) and `patch-apply-sweep` (`900.0`); task names `app.worker.patch_scan_sweep` / `app.worker.patch_apply_sweep`. Mirror `retention_all_orgs`/`sweep_all_orgs` (engine construction, org iteration, `finally` dispose). `now` is injected for tests (the Celery shell passes `datetime.now(UTC)`).

- [ ] **Step 1: Write the failing test** — monkeypatch `dispatch_apply` to record calls; build two orgs; assert `patch_apply_sweep_all_orgs(now)` visits both and sums counts. For scan: a device with no status (never scanned) is dispatched; a device scanned within its interval is not.

- [ ] **Step 2–5:** RED → implement in `worker.py` → GREEN (`pytest tests/test_patch_worker.py -v`) + full suite → commit `feat(patch): scan-cadence and window-apply worker sweeps`.

---

### Task 12: Frontend feature folder

**Files:**
- Create: `frontend/src/modules/patch/FleetPatchPage.tsx`, `DevicePatchTab.tsx`, `PatchPolicyEditor.tsx`, `api.ts`, `types.ts`
- Modify: `frontend/src/App.tsx` (nav item gated on `permissions.includes("patch.read")` + routes), the device-detail page (add the patch tab)
- Test: `frontend/src/modules/patch/__tests__/patch.test.tsx`

**Interfaces:**
- `api.ts` types mirror the backend response models exactly (no secret fields). Hooks via TanStack Query; invalidate the patch queries after scan/approve/apply mutations (the codebase has no live patch event — follow the Phase-3 lesson: refetch after mutation, do NOT subscribe to a non-existent event). `FleetPatchPage`: compliance summary + catalog table with an **Approve for ring** action gated on `patch.approve` and a **Scan now** button (`patch.scan`). `DevicePatchTab`: per-device status. `PatchPolicyEditor`: rings/windows/posture/reboot (`patch.policy.manage`). Strict TS, no `any`. Nav gating is cosmetic; backend RBAC is authoritative.

- [ ] **Step 1: Write the failing test** — vitest render of `FleetPatchPage` with a mocked api asserting it lists patches and the Approve action calls the approve mutation with `{patch_id, ring_name}`; the Approve control is hidden without `patch.approve`. Follow the existing frontend test setup.

- [ ] **Step 2–5:** RED → implement pages + nav/routes → `npm test`, `npm run typecheck`, `npm run build` all green → commit `feat(patch): fleet compliance + approvals UI, device patch tab, policy editor`.

---

### Task 13: Deploy / docs

**Files:**
- Modify: `deploy/.env.example` (document default scan cadence + the macOS Apple-Silicon MDM-enforcement caveat), `deploy/README.md` (patch sweeps are Celery-beat-driven; approval is the apply gate)
- Modify (if a config knob is wanted): `backend/app/core/config.py` — only if the sweep intervals should be settings; otherwise the policy fields own cadence and no config change is needed (prefer NO new settings — cadence lives in `patch_policies`). If nothing in `config.py` changes, this task is docs-only.
- Test: `cd deploy && docker compose config` (with a `.env` present) prints OK.

- [ ] **Step 1:** Update `.env.example` + `README.md`. No new compose services (patch needs none). If you add any `config.py` field, add a `tests/test_config.py` assertion.
- [ ] **Step 2:** Validate: `cd deploy && cp .env.example .env && docker compose config >/dev/null && echo OK && rm -f .env`.
- [ ] **Step 3:** Commit `docs(patch): deploy notes — scan cadence, macOS MDM caveat, beat sweeps`.

---

## Self-Review

**Spec coverage:**
- §4 data model → Task 1 (migration) + Task 2 (models) ✓
- §5 result contracts → Task 4 ✓
- §6 ingestion (catalog/reconcile/auto-approve) → Task 5 ✓
- §7 approvals/rings/windows → Task 6 (approve) + Task 7 (window eval) ✓
- §8 apply dispatch + result → Task 7 (dispatch) + Task 8 (result + registration) ✓
- §9 reporting/export → Task 9 (rows) + Task 10 (routes) ✓
- §10 wire contracts → Task 4 (exported) ✓
- §11 RBAC → Task 1 ✓
- §12 API → Task 10 ✓
- §13 worker → Task 11 ✓
- §14 frontend → Task 12 ✓
- §15 security invariants → approval-gate enforced in Task 7 dispatch (negative test) + signed jobs (existing engine) + audit across Tasks 5/6/7/8/10 ✓
- §16 testing → each task is test-first ✓
- §17 deploy → Task 13 ✓
- Sentinel seam (`priority` nullable, severity ordering) → Task 2 (column) + Task 9 (ordering) ✓

**Placeholder scan:** Tasks 6, 9, 11, 12 reference "mirror Task N's setup helpers" / "follow the existing frontend test setup" — pointers to concrete existing code, acceptable. All novel logic (ingest reconciliation, auto-approve, approval flip, window matcher + apply gate, result ingestion) has full test code or an exact behavioral spec.

**Type consistency:** `resolve_patch_policy`/`ResolvedPatchPolicy` (Tasks 3,5,6,7,11) consistent. `ingest_scan`/`IngestStats` (Tasks 5,8). `PatchScanDocument`/`PatchApplyResult`/`PatchApplyParams`/`PatchScanParams` (Tasks 4,7,8,10). `approve_patch`/`revoke_approval` (Tasks 6,10). `window_open`/`dispatch_apply` (Tasks 7,11). `ingest_apply_result` (Task 8). `register_job_types` (Tasks 8,10, gateway). State/severity/reboot vocabularies are fixed in Global Constraints and used verbatim throughout.

**Note for the implementer:** confirm `app.state.job_engine` and `app.state.redis` exist (used by Tasks 10/11) — they do (see `app/main.py`); no new `app.state` wiring is required for this module.
