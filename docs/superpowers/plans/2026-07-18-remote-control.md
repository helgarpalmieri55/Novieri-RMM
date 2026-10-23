# Remote Control Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the control-plane server slice of clientless browser remote control (RDP/VNC/SSH via Guacamole) with unattended vault-credential admin access, agent reverse-tunnel reachability, RBAC/consent policy, audit, and recorded sessions with 90-day retention — all test-first with guacd mocked.

**Architecture:** A new `app/modules/remote/` package registers into the kernel (one signed job type `remote.session.open`, no result handler; a retention worker task). Session setup rides the existing signed-job gateway; the media path rides a new relay that glues an agent-initiated mTLS reverse tunnel to a per-session loopback socket guacd connects to. A FastAPI WebSocket broker bridges the browser to guacd; recordings finalize to per-tenant object storage.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 (async/asyncpg), Alembic, Pydantic v2, Celery, existing `ObjectStore` (MinIO/S3), Guacamole protocol (text instructions) against guacd; React 19 + TS strict + guacamole-common-js (Apache-2.0) frontend.

## Global Constraints

- Python 3.12, FastAPI, SQLAlchemy 2.x typed, Pydantic v2, async throughout. Type hints mandatory; `mypy` must pass.
- Every `org_id` table: `ENABLE` + `FORCE ROW LEVEL SECURITY` + `tenant_isolation` policy `USING (org_id = current_setting('app.current_org', true)::uuid)` and matching `WITH CHECK`. The Phase-0 RLS-completeness test requires it.
- Migrations forward-only; never edit a shipped migration. Next revision is **0010**, `down_revision = "0009"`.
- Every privileged route uses `Depends(require_permission("<code>"))`; RBAC is server-side only. Device scope enforced as `jobs.dispatch` does.
- Business logic in services, not route handlers. Async engine sessions via `org_session(org_id, engine=...)`.
- Secrets (relay token, checked-out credentials) are NEVER logged, returned in responses, or stored in plaintext. Only `relay_token_hash` (sha256) and `credential_id` are persisted.
- Canonical JSON / signing already handled by `JobEngine`; do not reimplement.
- Audit every privileged action via `audit_service.record(session, org_id=…, actor_type=…, actor_id=…, action=…, target_type=…, target_id=…, metadata=…)`.
- `ruff check` and `mypy app` clean before each commit; run `pytest` for the touched tests.
- Guacamole and guacamole-common-js are Apache-2.0 — the only new deps.

## Interfaces already in the codebase (consume, do not redefine)

- `app.jobs.registry`: `JobType(name, params_model, result_handler=None)`, `register(job_type)`, `get_job_type(name)`.
- `app.jobs.engine.JobEngine`: `create(session, *, org_id, type, target_device_id, params, requested_by=None, ttl_s=None) -> Job`; `signed_envelope(job) -> dict`.
- `app.gateway.dispatch.publish_job(redis, device_id, envelope)`.
- `app.api.deps`: `AuthContext(org_id, user_id, claims)`, `require_permission(code)`.
- `app.core.tenancy.org_session(org_id, *, engine)`.
- `app.core.audit.audit_service.record(...)`.
- `app.core.vault` via `app.state.vault`: `checkout(session, *, org_id, credential_id) -> str`.
- `app.core.storage.ObjectStore`: `put_job_artifact`/`get_job_artifact` (mirror for recordings). `app.state` exposes `app_engine`, `admin_engine`, `redis`, `job_engine`, `vault`, `settings`; add `object_store` if not already present (check `app/main.py`).
- Module registration: add `remote` to `app.modules.register_all`.

---

### Task 1: Migration 0010 — tables, RLS, and RBAC seeds

**Files:**
- Create: `backend/migrations/versions/0010_remote_control.py`
- Test: `backend/tests/test_migrations.py` (existing RLS-completeness test covers new tables; add a targeted seed test in `backend/tests/test_remote_rbac.py`)

**Interfaces:**
- Produces tables `remote_sessions`, `remote_policies`; permissions `remote.control`, `remote.recordings.read`, `remote.policy.manage`; role grants.

- [ ] **Step 1: Write the failing test** — `backend/tests/test_remote_rbac.py`

```python
import pytest
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


async def test_remote_permissions_seeded_and_granted(admin_engine: AsyncEngine) -> None:
    async with AsyncSession(admin_engine) as session:
        codes = set(
            (await session.execute(select(Permission.code))).scalars()
        )
        assert {"remote.control", "remote.recordings.read", "remote.policy.manage"} <= codes

        technician = await _perms_for(session, "technician")
        assert "remote.control" in technician
        assert "remote.recordings.read" not in technician  # techs operate, not audit

        auditor = await _perms_for(session, "auditor")
        assert "remote.recordings.read" in auditor  # read privileged-access evidence
        assert "remote.control" not in auditor

        for role in ("super-admin", "org-admin"):
            assert {"remote.control", "remote.recordings.read", "remote.policy.manage"} <= await _perms_for(session, role)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/test_remote_rbac.py -v`
Expected: FAIL (permissions not seeded).

- [ ] **Step 3: Write the migration** — `backend/migrations/versions/0010_remote_control.py`

```python
"""Remote control: sessions, policies, RBAC (docs/modules/remote-control/spec.md).

Revision ID: 0010
Revises: 0009
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = [
    ("remote.control", "remote", "control"),
    ("remote.recordings.read", "remote", "recordings.read"),
    ("remote.policy.manage", "remote", "policy.manage"),
]
ROLE_GRANTS = {
    "super-admin": ["remote.control", "remote.recordings.read", "remote.policy.manage"],
    "org-admin": ["remote.control", "remote.recordings.read", "remote.policy.manage"],
    "technician": ["remote.control"],
    "auditor": ["remote.recordings.read"],
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
        CREATE TABLE remote_sessions (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            device_id uuid NOT NULL REFERENCES devices(id),
            technician_id uuid NOT NULL REFERENCES users(id),
            protocol varchar(8) NOT NULL,
            consent_mode varchar(12) NOT NULL,
            credential_id uuid REFERENCES credentials(id),
            state varchar(20) NOT NULL,
            relay_token_hash varchar(64),
            recording_ref text,
            recording_bytes bigint,
            started_at timestamptz,
            ended_at timestamptz,
            end_reason varchar(20),
            audit_event_id uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_remote_sessions_device ON remote_sessions (org_id, device_id, id)")
    op.execute("CREATE INDEX ix_remote_sessions_state ON remote_sessions (org_id, state)")
    op.execute(
        "CREATE INDEX ix_remote_sessions_recording ON remote_sessions (org_id, ended_at) "
        "WHERE recording_ref IS NOT NULL"
    )
    op.execute(
        """
        CREATE TABLE remote_policies (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            device_group_id uuid REFERENCES device_groups(id),
            allow_unattended boolean NOT NULL DEFAULT false,
            record_sessions boolean NOT NULL DEFAULT true,
            allow_file_transfer boolean NOT NULL DEFAULT false,
            allow_clipboard boolean NOT NULL DEFAULT false,
            recording_retention_days integer NOT NULL DEFAULT 90,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (org_id, device_group_id)
        )
        """
    )
    _rls("remote_sessions")
    _rls("remote_policies")

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
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE module = 'remote')"
    )
    op.execute("DELETE FROM permissions WHERE module = 'remote'")
    op.execute("DROP TABLE IF EXISTS remote_sessions")
    op.execute("DROP TABLE IF EXISTS remote_policies")
```

> Note: `remote_sessions.id` is a uuid7 PK generated app-side (see Task 2's model default), matching the codebase's `uuid7_pk()` pattern; the migration declares a plain `uuid PRIMARY KEY` (no server default) exactly like the other core tables.

- [ ] **Step 4: Run migration + tests**

Run: `cd backend && .venv/bin/python -m pytest tests/test_remote_rbac.py tests/test_migrations.py -v`
Expected: PASS (the migration harness applies to head; RLS-completeness includes the two new tables).

- [ ] **Step 5: Commit**

```bash
git add backend/migrations/versions/0010_remote_control.py backend/tests/test_remote_rbac.py
git commit -m "feat(remote): migration 0010 — sessions/policies tables, RLS, RBAC seeds"
```

---

### Task 2: ORM models — RemoteSession, RemotePolicy

**Files:**
- Create: `backend/app/modules/remote/__init__.py` (empty package marker for now; `register` added in Task 10)
- Create: `backend/app/modules/remote/models.py`
- Modify: `backend/app/models/__init__.py` (re-export the two models so `from app.models import RemoteSession` works, matching how other module models are exposed — check the existing pattern first)
- Test: `backend/tests/test_remote_models.py`

**Interfaces:**
- Produces `RemoteSession`, `RemotePolicy` SQLAlchemy models with columns matching migration 0010. Reuse the codebase's `Base`, `TimestampMixin`, `uuid7_pk()` (see `app/models/core.py`).

- [ ] **Step 1: Write the failing test**

```python
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import Device, Organization, RemotePolicy, RemoteSession, User


async def _org_device_user(admin_engine: AsyncEngine):
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="rc", slug=f"rc-{uuid4().hex[:6]}")
        s.add(org); await s.flush()
        dev = Device(org_id=org.id, hostname="h", os_family="linux"); s.add(dev)
        usr = User(org_id=org.id, external_subject=f"s{uuid4().hex[:6]}", email="a@b.c"); s.add(usr)
        await s.commit()
        return org.id, dev.id, usr.id


async def test_remote_session_roundtrip(admin_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    org_id, device_id, user_id = await _org_device_user(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        row = RemoteSession(
            org_id=org_id, device_id=device_id, technician_id=user_id,
            protocol="rdp", consent_mode="attended", state="requested",
        )
        s.add(row); await s.flush()
        sid = row.id
    async with org_session(org_id, engine=app_engine) as s:
        got = (await s.execute(select(RemoteSession).where(RemoteSession.id == sid))).scalar_one()
        assert got.protocol == "rdp" and got.state == "requested" and got.recording_ref is None


async def test_remote_policy_defaults(admin_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    org_id, _, _ = await _org_device_user(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        p = RemotePolicy(org_id=org_id, device_group_id=None)
        s.add(p); await s.flush()
        assert p.allow_unattended is False and p.record_sessions is True
        assert p.recording_retention_days == 90
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/test_remote_models.py -v`
Expected: FAIL (`ImportError: RemoteSession`).

- [ ] **Step 3: Write `models.py`** (match `app/models/core.py` conventions — inspect it for `uuid7_pk`, `TimestampMixin`, `Base` imports)

```python
from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.core import Base, TimestampMixin, uuid7_pk


class RemoteSession(TimestampMixin, Base):
    __tablename__ = "remote_sessions"

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"))
    technician_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    protocol: Mapped[str] = mapped_column(String(8))
    consent_mode: Mapped[str] = mapped_column(String(12))
    credential_id: Mapped[UUID | None] = mapped_column(ForeignKey("credentials.id"), default=None)
    state: Mapped[str] = mapped_column(String(20))
    relay_token_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    recording_ref: Mapped[str | None] = mapped_column(default=None)
    recording_bytes: Mapped[int | None] = mapped_column(default=None)
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    ended_at: Mapped[datetime | None] = mapped_column(default=None)
    end_reason: Mapped[str | None] = mapped_column(String(20), default=None)
    audit_event_id: Mapped[UUID | None] = mapped_column(default=None)


class RemotePolicy(TimestampMixin, Base):
    __tablename__ = "remote_policies"

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    device_group_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("device_groups.id"), default=None
    )
    allow_unattended: Mapped[bool] = mapped_column(Boolean, default=False)
    record_sessions: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_file_transfer: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_clipboard: Mapped[bool] = mapped_column(Boolean, default=False)
    recording_retention_days: Mapped[int] = mapped_column(Integer, default=90)
```

Then re-export in `app/models/__init__.py` following the existing per-module pattern (e.g. add `from app.modules.remote.models import RemotePolicy, RemoteSession` and include in `__all__`). If the codebase instead centralizes models in `app/models/`, place the file there per the established convention.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/test_remote_models.py -v && .venv/bin/mypy app`
Expected: PASS; mypy clean.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/remote/ backend/app/models/__init__.py backend/tests/test_remote_models.py
git commit -m "feat(remote): RemoteSession + RemotePolicy ORM models"
```

---

### Task 3: Policy resolution

**Files:**
- Create: `backend/app/modules/remote/policy.py`
- Test: `backend/tests/test_remote_policy.py`

**Interfaces:**
- Produces `ResolvedPolicy` (frozen dataclass: `allow_unattended: bool`, `record_sessions: bool`, `allow_file_transfer: bool`, `allow_clipboard: bool`, `recording_retention_days: int`) and `async def resolve_policy(session, org_id, device_id) -> ResolvedPolicy`.
- Consumes `RemoteSession`/`RemotePolicy` (Task 2), `Device.device_group_id`.

Resolution order: device's group policy → org-default policy (`device_group_id IS NULL`) → hardcoded defaults (unattended False, record True, transfer/clipboard False, retention 90).

- [ ] **Step 1: Write the failing test**

```python
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import Device, DeviceGroup, Organization, RemotePolicy
from app.modules.remote.policy import resolve_policy


async def _setup(admin_engine):
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="rp", slug=f"rp-{uuid4().hex[:6]}"); s.add(org); await s.flush()
        grp = DeviceGroup(org_id=org.id, name="servers"); s.add(grp); await s.flush()
        dev = Device(org_id=org.id, hostname="h", os_family="linux", device_group_id=grp.id)
        s.add(dev); await s.commit()
        return org.id, grp.id, dev.id


async def test_defaults_when_no_policy(admin_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    org_id, _, device_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        pol = await resolve_policy(s, org_id, device_id)
    assert pol.allow_unattended is False and pol.record_sessions is True
    assert pol.recording_retention_days == 90


async def test_group_policy_wins_over_org_default(admin_engine, app_engine) -> None:
    org_id, group_id, device_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        s.add(RemotePolicy(org_id=org_id, device_group_id=None, allow_unattended=False))
        s.add(RemotePolicy(org_id=org_id, device_group_id=group_id, allow_unattended=True,
                           recording_retention_days=30))
        await s.flush()
        pol = await resolve_policy(s, org_id, device_id)
    assert pol.allow_unattended is True and pol.recording_retention_days == 30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/test_remote_policy.py -v`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Write `policy.py`**

```python
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, RemotePolicy


@dataclass(frozen=True)
class ResolvedPolicy:
    allow_unattended: bool = False
    record_sessions: bool = True
    allow_file_transfer: bool = False
    allow_clipboard: bool = False
    recording_retention_days: int = 90


def _from_row(row: RemotePolicy) -> ResolvedPolicy:
    return ResolvedPolicy(
        allow_unattended=row.allow_unattended,
        record_sessions=row.record_sessions,
        allow_file_transfer=row.allow_file_transfer,
        allow_clipboard=row.allow_clipboard,
        recording_retention_days=row.recording_retention_days,
    )


async def resolve_policy(session: AsyncSession, org_id: UUID, device_id: UUID) -> ResolvedPolicy:
    group_id = (
        await session.execute(select(Device.device_group_id).where(Device.id == device_id))
    ).scalar_one_or_none()
    if group_id is not None:
        row = (
            await session.execute(
                select(RemotePolicy).where(
                    RemotePolicy.org_id == org_id, RemotePolicy.device_group_id == group_id
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            return _from_row(row)
    org_default = (
        await session.execute(
            select(RemotePolicy).where(
                RemotePolicy.org_id == org_id, RemotePolicy.device_group_id.is_(None)
            )
        )
    ).scalar_one_or_none()
    return _from_row(org_default) if org_default is not None else ResolvedPolicy()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/test_remote_policy.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/remote/policy.py backend/tests/test_remote_policy.py
git commit -m "feat(remote): device-group remote policy resolution with safe defaults"
```

---

### Task 4: Register `remote.session.open` job type + wire schema

**Files:**
- Create: `backend/app/modules/remote/jobtypes.py`
- Modify: `backend/scripts/export_wire_schema.py` (add the new params model to the export set) OR create the schema file directly if the exporter enumerates registered types — inspect it first.
- Create: `docs/kernel/wire-protocol/RemoteSessionOpenJob.schema.json` (generated)
- Test: `backend/tests/test_remote_jobtype.py`

**Interfaces:**
- Produces `RemoteSessionOpenParams(BaseModel)` with fields: `session_id: str`, `protocol: Literal["rdp","vnc","ssh"]`, `local_port: int`, `relay_url: str`, `relay_token: str`, `consent_mode: Literal["attended","unattended"]`, `consent_prompt: dict[str, str] | None = None`. `model_config = ConfigDict(extra="forbid")`.
- Produces `register_job_type()` that calls `register(JobType("remote.session.open", RemoteSessionOpenParams))` (no result handler).

- [ ] **Step 1: Write the failing test**

```python
import pytest
from pydantic import ValidationError

from app.jobs.registry import get_job_type
from app.modules.remote.jobtypes import RemoteSessionOpenParams, register_job_type


def test_params_validate_and_reject_extra() -> None:
    p = RemoteSessionOpenParams(
        session_id="s", protocol="rdp", local_port=3389,
        relay_url="wss://gw/relay", relay_token="t", consent_mode="unattended",
    )
    assert p.protocol == "rdp"
    with pytest.raises(ValidationError):
        RemoteSessionOpenParams(
            session_id="s", protocol="telnet", local_port=3389,  # bad protocol
            relay_url="x", relay_token="t", consent_mode="unattended",
        )


def test_job_type_registered() -> None:
    register_job_type()
    jt = get_job_type("remote.session.open")
    assert jt.params_model is RemoteSessionOpenParams and jt.result_handler is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/test_remote_jobtype.py -v`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Write `jobtypes.py`**

```python
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.jobs.registry import JobType, register


class RemoteSessionOpenParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    protocol: Literal["rdp", "vnc", "ssh"]
    local_port: int
    relay_url: str
    relay_token: str
    consent_mode: Literal["attended", "unattended"]
    consent_prompt: dict[str, str] | None = None


def register_job_type() -> None:
    register(JobType("remote.session.open", RemoteSessionOpenParams))
```

- [ ] **Step 4: Run test + regenerate wire schema**

Run: `cd backend && .venv/bin/python -m pytest tests/test_remote_jobtype.py -v`
Then wire the params model into `scripts/export_wire_schema.py` (follow how it emits `JobEnvelope`/`InventoryDocument`) and run `.venv/bin/python scripts/export_wire_schema.py` to produce `docs/kernel/wire-protocol/RemoteSessionOpenJob.schema.json`.
Expected: PASS; schema file created.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/remote/jobtypes.py backend/tests/test_remote_jobtype.py backend/scripts/export_wire_schema.py docs/kernel/wire-protocol/RemoteSessionOpenJob.schema.json
git commit -m "feat(remote): remote.session.open signed job type + exported wire schema"
```

---

### Task 5: Relay — per-session token + byte glue

**Files:**
- Create: `backend/app/modules/remote/relay.py`
- Test: `backend/tests/test_remote_relay.py`

**Interfaces:**
- Produces `mint_relay_token() -> tuple[str, str]` returning `(token, token_sha256_hex)`.
- Produces `token_hash(token: str) -> str`.
- Produces `class SessionRelay` with an in-memory registry keyed by `session_id`:
  - `register(session_id: UUID, device_id: UUID, token_hash: str) -> None`
  - `async def attach_agent(session_id: UUID, device_id: UUID, token: str, agent_reader, agent_writer) -> bool` — validates token_hash + device, marks the session connectable, returns False (and does not attach) on mismatch.
  - `async def pump(session_id: UUID, guacd_reader, guacd_writer) -> None` — bidirectional copy between the attached agent stream and the guacd stream until either closes.
  - `unregister(session_id)`.
- The relay never inspects payload bytes. Uses asyncio stream reader/writer duck-typing so tests pass `asyncio.StreamReader`/`Writer` from `asyncio.open_connection` over a socketpair, or simple in-memory fakes.

- [ ] **Step 1: Write the failing test**

```python
import asyncio
from uuid import uuid4

import pytest

from app.modules.remote.relay import SessionRelay, mint_relay_token, token_hash


def test_token_mint_and_hash() -> None:
    token, h = mint_relay_token()
    assert len(token) > 20 and h == token_hash(token) and len(h) == 64


async def test_attach_rejects_wrong_token_or_device() -> None:
    relay = SessionRelay()
    sid, did = uuid4(), uuid4()
    token, h = mint_relay_token()
    relay.register(sid, did, h)
    r, w = _fake_stream()
    assert await relay.attach_agent(sid, did, "wrong-token", r, w) is False
    assert await relay.attach_agent(sid, uuid4(), token, r, w) is False  # wrong device
    assert await relay.attach_agent(sid, did, token, r, w) is True


async def test_pump_relays_bytes_bidirectionally() -> None:
    # agent side and guacd side are socketpairs; the relay glues them
    relay = SessionRelay()
    sid, did = uuid4(), uuid4()
    token, h = mint_relay_token()
    relay.register(sid, did, h)

    agent_a, agent_b = await _connected_pair()      # relay holds agent_b
    guacd_a, guacd_b = await _connected_pair()       # relay holds guacd_b
    assert await relay.attach_agent(sid, did, token, agent_b[0], agent_b[1]) is True
    task = asyncio.create_task(relay.pump(sid, guacd_b[0], guacd_b[1]))

    guacd_a[1].write(b"select rdp;")               # browser->guacd->endpoint
    await guacd_a[1].drain()
    assert await agent_a[0].readexactly(len(b"select rdp;")) == b"select rdp;"

    agent_a[1].write(b"0.;")                        # endpoint->guacd->browser
    await agent_a[1].drain()
    assert await guacd_a[0].readexactly(3) == b"0.;"

    agent_a[1].close(); guacd_a[1].close()
    await asyncio.wait_for(task, timeout=2)
```

Add helpers `_connected_pair()` (via `asyncio.open_connection`/`start_server` on `127.0.0.1:0`, or `socket.socketpair()` wrapped with `asyncio.open_connection(sock=...)`) and `_fake_stream()` returning a minimal reader/writer. Keep them in the test file.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/test_remote_relay.py -v`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Write `relay.py`** — token helpers + the glue. Implement `attach_agent` to compare `token_hash(token)` with the registered hash using `hmac.compare_digest` and match `device_id`; store the agent `(reader, writer)` on the session entry. Implement `pump` as two `asyncio.create_task` copy loops (agent→guacd, guacd→agent) joined with `asyncio.gather`, each reading up to 64 KiB and writing/draining until EOF, then closing the peer writer. On any mismatch, audit is the caller's job (Task 8) — the relay just returns False.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/test_remote_relay.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/remote/relay.py backend/tests/test_remote_relay.py
git commit -m "feat(remote): session relay — per-session token + bidirectional byte glue"
```

---

### Task 6: guacd protocol client

**Files:**
- Create: `backend/app/modules/remote/guacd.py`
- Test: `backend/tests/test_remote_guacd.py`

**Interfaces:**
- Produces `encode_instruction(*args: str) -> bytes` — Guacamole wire format: each element `LENGTH.VALUE`, comma-separated, terminated with `;` (e.g. `encode_instruction("select", "rdp") == b"6.select,3.rdp;"`).
- Produces `async def read_instruction(reader) -> list[str]` — parse one instruction from an asyncio reader.
- Produces `class GuacdClient` with `async def connect(reader, writer, *, protocol, params: dict[str,str]) -> str` performing the handshake (`select` → receive `args` → send `size`/`audio`/`video`/`image` → send `connect` with params in the server's arg order) and returning the connection id from the `ready` instruction.
- guacd is mocked: the test provides an in-process server speaking the protocol.

- [ ] **Step 1: Write the failing test**

```python
import asyncio

import pytest

from app.modules.remote.guacd import GuacdClient, encode_instruction, read_instruction


def test_encode_instruction_matches_guac_wire_format() -> None:
    assert encode_instruction("select", "rdp") == b"6.select,3.rdp;"
    assert encode_instruction("size", "1024", "768", "96") == b"4.size,4.1024,3.768,2.96;"


async def test_read_instruction_parses_elements() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"4.args,8.hostname,4.port;")
    reader.feed_eof()
    assert await read_instruction(reader) == ["args", "hostname", "port"]


async def test_handshake_sends_connect_in_arg_order() -> None:
    # a mock guacd: expects select, replies args(hostname,port,username,password),
    # then expects size/audio/video/image and a connect, replies ready
    async def mock_guacd(reader, writer):
        assert await read_instruction(reader) == ["select", "rdp"]
        writer.write(encode_instruction("args", "hostname", "port", "username", "password"))
        await writer.drain()
        seen = []
        while True:
            instr = await read_instruction(reader)
            seen.append(instr[0])
            if instr[0] == "connect":
                assert instr[1:] == ["127.0.0.1", "9999", "admin", "s3cret"]
                break
        writer.write(encode_instruction("ready", "$conn-1"))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(mock_guacd, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        conn_id = await GuacdClient().connect(
            reader, writer, protocol="rdp",
            params={"hostname": "127.0.0.1", "port": "9999", "username": "admin", "password": "s3cret"},
        )
        assert conn_id == "$conn-1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/test_remote_guacd.py -v`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Write `guacd.py`** implementing the codec and handshake. `encode_instruction` joins `f"{len(a)}.{a}"` with `,` + `;`. `read_instruction` reads until `;`, splitting on `.`/`,` per the length prefixes. `GuacdClient.connect` sends `select`, reads the `args` reply, sends `size 1024 768 96`, `audio`/`video`/`image` (empty), then `connect` with the received arg names mapped from `params` (empty string for unknown), and reads until `ready`, returning its first element.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/test_remote_guacd.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/remote/guacd.py backend/tests/test_remote_guacd.py
git commit -m "feat(remote): Guacamole-protocol client (codec + handshake), guacd mockable"
```

---

### Task 7: Recording finalize + retention

**Files:**
- Create: `backend/app/modules/remote/recording.py`
- Modify: `backend/app/core/storage.py` (add `put_remote_recording`/`get_remote_recording`/`delete_object` mirroring `put_job_artifact`)
- Test: `backend/tests/test_remote_recording.py`

**Interfaces:**
- Produces `ObjectStore.put_remote_recording(org_id, session_id, data) -> str` (key `{org_id}/remote-recordings/{session_id}.guac`), `get_remote_recording(ref) -> bytes`, `delete_object(ref) -> None`.
- Produces `async def finalize_recording(session, store, *, org_id, session_id, data) -> tuple[str, int]` — uploads, sets `recording_ref`/`recording_bytes` on the RemoteSession row, returns `(ref, size)`.
- Produces `async def apply_recording_retention(org_id, admin_engine, app_engine, store) -> int` — deletes objects + nulls `recording_ref` for sessions whose `ended_at` older than the org's `recording_retention_days`; audits `remote.recording_expired`; returns count.

- [ ] **Step 1: Write the failing test** (uses the real `ObjectStore` against the test MinIO, like `test_storage.py`)

```python
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.models import Device, Organization, RemotePolicy, RemoteSession, User
from app.modules.remote.recording import apply_recording_retention, finalize_recording


async def test_finalize_uploads_and_links(store: ObjectStore, admin_engine, app_engine) -> None:
    org_id, device_id, user_id = await _org_device_user(admin_engine)  # helper as in Task 2
    async with org_session(org_id, engine=app_engine) as s:
        row = RemoteSession(org_id=org_id, device_id=device_id, technician_id=user_id,
                            protocol="rdp", consent_mode="attended", state="active")
        s.add(row); await s.flush(); sid = row.id
        ref, size = await finalize_recording(s, store, org_id=org_id, session_id=sid, data=b"GUAC-REC")
        assert size == 8 and ref.endswith(f"{sid}.guac")
    assert await store.get_remote_recording(ref) == b"GUAC-REC"


async def test_retention_deletes_expired(store, admin_engine, app_engine) -> None:
    org_id, device_id, user_id = await _org_device_user(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        s.add(RemotePolicy(org_id=org_id, device_group_id=None, recording_retention_days=30))
        old = RemoteSession(org_id=org_id, device_id=device_id, technician_id=user_id,
                            protocol="rdp", consent_mode="attended", state="ended",
                            ended_at=datetime.now(UTC) - timedelta(days=40))
        s.add(old); await s.flush(); sid = old.id
        ref, _ = await finalize_recording(s, store, org_id=org_id, session_id=sid, data=b"OLD")
    deleted = await apply_recording_retention(org_id, admin_engine, app_engine, store)
    assert deleted == 1
    async with org_session(org_id, engine=app_engine) as s:
        assert (await s.execute(select(RemoteSession.recording_ref).where(RemoteSession.id == sid))).scalar_one() is None
```

Add a `store` fixture in the test (or `conftest.py`) building `ObjectStore(api_env.settings)` — reuse the pattern in `test_storage.py`. If MinIO is unavailable in the environment, mark these with the same skip guard `test_storage.py` uses.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/test_remote_recording.py -v`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Implement** the storage methods and `recording.py`. `finalize_recording` calls `store.put_remote_recording`, updates the row, flushes. `apply_recording_retention` resolves retention from the org-default `RemotePolicy` (default 90), selects sessions with `recording_ref IS NOT NULL AND ended_at < cutoff` via the app engine, deletes each object with `store.delete_object`, nulls `recording_ref`/`recording_bytes`, and audits once with the deleted count.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/test_remote_recording.py -v`
Expected: PASS (or SKIP if MinIO absent — verify at least import + logic via a mocked store variant if skipped).

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/remote/recording.py backend/app/core/storage.py backend/tests/test_remote_recording.py
git commit -m "feat(remote): recording finalize to object storage + per-org retention sweep"
```

---

### Task 8: Session service — lifecycle state machine

**Files:**
- Create: `backend/app/modules/remote/service.py`
- Test: `backend/tests/test_remote_service.py`

**Interfaces:**
- Produces `class RemoteSessionService` constructed with `(job_engine, relay: SessionRelay, vault)`.
- `async def start_session(session, redis, *, org_id, technician_id, device_id, protocol, requested_mode, credential_id, reason) -> RemoteSession` — resolves policy; picks `consent_mode` (unattended only if policy `allow_unattended` and `requested_mode=="unattended"`); for unattended requires `credential_id` and checks it out (secret used only to stash on an in-memory session-params map, never on the row); mints the relay token; registers the session with the relay; creates + dispatches the signed `remote.session.open` job via `job_engine.create` + `publish_job`; audits `remote.session.requested`; sets state `awaiting_consent`. Returns the row.
- `async def on_consent(session, *, session_id, accepted) -> None` — attended accept → state `connecting`; reject → `ended (denied_consent)` + audit `remote.session.denied`.
- `async def mark_active(session, *, session_id, audit_ref) -> None` — state `active`, `started_at`, audit `remote.session.started`, store `audit_event_id`.
- `async def end_session(session, *, session_id, reason) -> None` — state `ended`, `ended_at`, audit `remote.session.ended` with duration; unregister relay.
- Legal transitions enforced; illegal raises `InvalidSessionState`.

Test with a fake `job_engine` (records `create`/`signed_envelope`), a real `SessionRelay`, and a fake `vault` whose `checkout` returns a sentinel secret; assert the secret never lands on the row and audit actions are written.

- [ ] **Step 1: Write the failing test**

```python
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import org_session
from app.models import AuditEvent, RemotePolicy, RemoteSession
from app.modules.remote.relay import SessionRelay
from app.modules.remote.service import InvalidSessionState, RemoteSessionService


class _FakeJobEngine:
    def __init__(self): self.created = []
    async def create(self, session, **kw): 
        from app.models import Job
        job = Job(org_id=kw["org_id"], type=kw["type"], target_type="device",
                  target_id=kw["target_device_id"], params=kw["params"], nonce="n",
                  issued_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
                  expires_at=__import__("datetime").datetime.now(__import__("datetime").UTC))
        session.add(job); await session.flush(); self.created.append(kw); return job
    async def signed_envelope(self, job): return {"v":1,"kind":"job","job":{"id":str(job.id)},"sig":"x"}


class _FakeVault:
    async def checkout(self, session, *, org_id, credential_id): return "SUPER-SECRET"


class _FakeRedis:
    def __init__(self): self.published = []
    async def publish(self, ch, data): self.published.append((ch, data))


async def test_unattended_requires_policy_and_never_stores_secret(admin_engine, app_engine) -> None:
    org_id, device_id, user_id = await _org_device_user(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        s.add(RemotePolicy(org_id=org_id, device_group_id=None, allow_unattended=True))
        await s.flush()
    svc = RemoteSessionService(_FakeJobEngine(), SessionRelay(), _FakeVault())
    async with org_session(org_id, engine=app_engine) as s:
        row = await svc.start_session(
            s, _FakeRedis(), org_id=org_id, technician_id=user_id, device_id=device_id,
            protocol="rdp", requested_mode="unattended", credential_id=uuid4(), reason="fix",
        )
        assert row.consent_mode == "unattended" and row.state == "awaiting_consent"
        assert row.relay_token_hash and "SUPER-SECRET" not in str(row.__dict__)
        actions = {a.action for a in (await s.execute(select(AuditEvent).where(AuditEvent.org_id==org_id))).scalars()}
    assert "remote.session.requested" in actions


async def test_unattended_downgraded_when_policy_forbids(admin_engine, app_engine) -> None:
    org_id, device_id, user_id = await _org_device_user(admin_engine)
    svc = RemoteSessionService(_FakeJobEngine(), SessionRelay(), _FakeVault())
    async with org_session(org_id, engine=app_engine) as s:
        row = await svc.start_session(
            s, _FakeRedis(), org_id=org_id, technician_id=user_id, device_id=device_id,
            protocol="rdp", requested_mode="unattended", credential_id=uuid4(), reason="x")
        assert row.consent_mode == "attended"  # policy default forbids unattended
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/test_remote_service.py -v`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Write `service.py`** implementing the state machine, transition guard (`_ALLOWED` dict), policy resolution call, credential checkout (only when the resolved mode is unattended), relay token mint + `relay.register`, job create + `publish_job`, and audit calls. Keep the checked-out secret in a local variable / an in-memory `dict[UUID, dict]` session-params map on the service instance — never on the ORM row.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/test_remote_service.py -v && .venv/bin/mypy app`
Expected: PASS; mypy clean.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/remote/service.py backend/tests/test_remote_service.py
git commit -m "feat(remote): session lifecycle service (consent/checkout/dispatch/audit)"
```

---

### Task 9: Browser WebSocket broker

**Files:**
- Create: `backend/app/modules/remote/broker.py`
- Test: `backend/tests/test_remote_broker.py`

**Interfaces:**
- Produces `async def bridge(ws_send, ws_recv, guacd_reader, guacd_writer, *, record_sink=None) -> None` — after `GuacdClient.connect` has produced a ready connection, copy Guacamole instructions browser↔guacd; when `record_sink` is set, tee guacd→browser bytes into it (recording capture). Decoupled from FastAPI so it's testable with plain callables/streams.
- Consumes `guacd.py` codec.

- [ ] **Step 1: Write the failing test**

```python
import asyncio

from app.modules.remote.broker import bridge
from app.modules.remote.guacd import encode_instruction


async def test_bridge_copies_both_ways_and_records() -> None:
    to_browser = []
    from_browser = asyncio.Queue()
    await from_browser.put(b"5.mouse,3.100,3.200;")
    await from_browser.put(None)  # browser closes

    guacd_reader = asyncio.StreamReader()
    guacd_reader.feed_data(encode_instruction("sync", "12345"))
    guacd_reader.feed_eof()

    class _W:
        def __init__(self): self.buf = b""
        def write(self, d): self.buf += d
        async def drain(self): ...
        def close(self): ...
        async def wait_closed(self): ...

    guacd_writer = _W()
    sink = bytearray()

    async def ws_send(data: bytes): to_browser.append(data)
    async def ws_recv(): return await from_browser.get()

    await bridge(ws_send, ws_recv, guacd_reader, guacd_writer, record_sink=sink)

    assert b"5.mouse,3.100,3.200;" in guacd_writer.buf          # browser -> guacd
    assert any(b"sync" in m for m in to_browser)                # guacd -> browser
    assert b"sync" in bytes(sink)                                # recorded
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/test_remote_broker.py -v`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Write `broker.py`** — two copy loops joined with `asyncio.gather`: browser→guacd (read from `ws_recv`, write to `guacd_writer`, stop on `None`) and guacd→browser (read raw chunks from `guacd_reader`, `ws_send`, and if `record_sink is not None` append to it). Close cleanly when either side ends.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/test_remote_broker.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/remote/broker.py backend/tests/test_remote_broker.py
git commit -m "feat(remote): browser<->guacd WebSocket bridge with recording tee"
```

---

### Task 10: API routes + module registration

**Files:**
- Create: `backend/app/modules/remote/api.py`
- Modify: `backend/app/modules/remote/__init__.py` (add `register(app)`)
- Modify: `backend/app/modules/__init__.py` (call `remote.register(app)`)
- Modify: `backend/app/gateway/main.py` (register the remote job type in the gateway process too, mirroring inventory — the job type must exist where jobs are created/validated)
- Test: `backend/tests/test_remote_api.py`

**Interfaces:**
- Routes under `/api/v1/remote` per spec §11: `POST /sessions`, `GET /sessions`, `GET /sessions/{id}`, `POST /sessions/{id}/stop`, `GET /sessions/{id}/recording`, `GET|PUT /policies`. (The tunnel `WS /sessions/{id}/tunnel` is wired here using `broker.bridge` + `GuacdClient` + `relay.pump`; in tests guacd is mocked via a settings-injected connector factory on `app.state`.)
- Response models never carry secrets or the relay token.

- [ ] **Step 1: Write the failing test** (uses `api_env` like `test_enrollment.py`)

```python
from uuid import uuid4


async def _enrolled_device(api_env, org_id):  # create a Device + Agent row directly via admin engine
    ...  # mirror helpers in test_dispatch.py


async def test_start_session_requires_permission(api_env) -> None:
    org_id = await api_env.create_org("rc")
    ro = await api_env.create_user(org_id, "read-only")
    headers = await api_env.headers(org_id, ro)
    resp = await api_env.client.post("/api/v1/remote/sessions",
        json={"device_id": str(uuid4()), "protocol": "rdp", "reason": "x"}, headers=headers)
    assert resp.status_code == 403


async def test_start_session_creates_and_dispatches(api_env) -> None:
    org_id = await api_env.create_org("rc")
    tech = await api_env.create_user(org_id, "technician")
    device_id = await _enrolled_device(api_env, org_id)
    headers = await api_env.headers(org_id, tech)
    resp = await api_env.client.post("/api/v1/remote/sessions",
        json={"device_id": str(device_id), "protocol": "rdp", "reason": "helpdesk"}, headers=headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["state"] == "awaiting_consent" and "relay_token" not in body and "tunnel_url" in body


async def test_recording_read_forbidden_for_technician_but_ok_for_auditor(api_env) -> None:
    org_id = await api_env.create_org("rc")
    tech = await api_env.create_user(org_id, "technician")
    aud = await api_env.create_user(org_id, "auditor")
    # a session with no recording yet -> 404 for auditor, 403 for technician
    from uuid import uuid4 as u
    sid = u()
    assert (await api_env.client.get(f"/api/v1/remote/sessions/{sid}/recording",
        headers=await api_env.headers(org_id, tech))).status_code == 403
    assert (await api_env.client.get(f"/api/v1/remote/sessions/{sid}/recording",
        headers=await api_env.headers(org_id, aud))).status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/test_remote_api.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `api.py`** with Pydantic request/response models (no secret fields), `Depends(require_permission(...))` on every route, `org_session` for DB work, and the `RemoteSessionService` from `app.state` (construct it in `register(app)` and stash on `app.state.remote_service`). Wire `register(app)` to call `register_job_type()` + `app.include_router(router, prefix="/api/v1")`. Add `remote.register(app)` to `register_all`, and register the job type in `app/gateway/main.py` startup next to the inventory registration.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/test_remote_api.py -v && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: PASS; mypy + ruff clean.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/remote/api.py backend/app/modules/remote/__init__.py backend/app/modules/__init__.py backend/app/gateway/main.py backend/tests/test_remote_api.py
git commit -m "feat(remote): API routes, tunnel endpoint, module + gateway registration"
```

---

### Task 11: Retention worker task

**Files:**
- Modify: `backend/app/worker.py` (add `remote_recording_retention_all_orgs()` async fn + a Celery task + a daily beat entry)
- Test: `backend/tests/test_remote_retention_worker.py`

**Interfaces:**
- Produces `async def remote_recording_retention_all_orgs() -> int` iterating orgs (admin engine) and calling `apply_recording_retention` per org with an `ObjectStore`. Beat schedule key `remote-recording-retention` at `86400.0`, task name `app.worker.apply_remote_recording_retention`.

- [ ] **Step 1: Write the failing test**

```python
async def test_retention_worker_sweeps_all_orgs(monkeypatch) -> None:
    from app import worker
    calls = []
    async def fake_apply(org_id, admin_engine, app_engine, store): calls.append(org_id); return 2
    monkeypatch.setattr(worker, "apply_recording_retention", fake_apply, raising=False)
    # ... build two orgs via admin_engine, call remote_recording_retention_all_orgs(),
    # assert it returns 4 and visited both orgs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/test_remote_retention_worker.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement** the async sweep in `worker.py` (mirror `retention_all_orgs`), import `apply_recording_retention` and `ObjectStore`, add the beat entry + task shell.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/test_remote_retention_worker.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/worker.py backend/tests/test_remote_retention_worker.py
git commit -m "feat(remote): daily recording-retention worker task"
```

---

### Task 12: Frontend feature folder

**Files:**
- Create: `frontend/src/modules/remote/SessionsPage.tsx`, `SessionViewer.tsx`, `RecordingPlayer.tsx`, `api.ts`, `types.ts`
- Modify: nav/route registration + `package.json` (add `guacamole-common-js`)
- Test: `frontend/src/modules/remote/__tests__/sessions.test.tsx` (vitest)

**Interfaces:**
- `api.ts`: `listSessions()`, `startSession(body)`, `stopSession(id)`, `getRecordingUrl(id)` via the shared fetch client + TanStack Query hooks. Types in `types.ts` mirror the API response models (no secret fields).
- Nav item gated on `me.permissions` including `remote.control` or `remote.recordings.read` (existing permission-gated nav pattern).

- [ ] **Step 1: Write the failing test** — a vitest test rendering `SessionsPage` with a mocked query client asserting it lists sessions and the Start dialog calls `startSession`. (Follow the existing frontend test setup used by other modules.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test -- sessions`
Expected: FAIL.

- [ ] **Step 3: Implement** the pages: `SessionsPage` (table + Start dialog), `SessionViewer` (guacamole-common-js `Guacamole.Client` over a `Guacamole.WebSocketTunnel` pointed at the tunnel URL, mounting the display canvas, with an End button), `RecordingPlayer` (guacamole-common-js `Guacamole.SessionRecording` over the recording bytes). Strict TS, no `any`. Gate the nav item.

- [ ] **Step 4: Run test + typecheck + build**

Run: `cd frontend && npm test -- sessions && npx tsc --noEmit && npm run build`
Expected: PASS; tsc clean; build succeeds.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/modules/remote frontend/package.json frontend/package-lock.json
git commit -m "feat(remote): sessions list, live session viewer, recording player (frontend)"
```

---

### Task 13: Deploy — guacd service + relay port

**Files:**
- Modify: `deploy/docker-compose.yml` (add `guacd` service on the internal network; expose the relay port alongside the agent gateway)
- Modify: `deploy/.env.example` (guacd host/port, relay bind)
- Modify: `deploy/README.md` (document the media path + that recordings retention is app-driven, not bucket lifecycle)
- Test: `cd deploy && docker compose config` (validates compose)

- [ ] **Step 1: Add the guacd service** using the official `guacamole/guacd` (Apache-2.0) image, no published ports (internal only), on the same network as the API/gateway. Add relay env (`ITS_RELAY_HOST`, `ITS_RELAY_PORT`) and guacd env (`ITS_GUACD_HOST=guacd`, `ITS_GUACD_PORT=4822`) to `.env.example` and `Settings` (add fields in `app/core/config.py` with sensible defaults + a config test in `tests/test_config.py`).

- [ ] **Step 2: Validate**

Run: `cd deploy && docker compose config >/dev/null && echo OK`
Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add deploy/docker-compose.yml deploy/.env.example deploy/README.md backend/app/core/config.py backend/tests/test_config.py
git commit -m "feat(remote): deploy guacd + relay wiring and config"
```

---

## Self-Review

**Spec coverage:**
- §2 architecture → Tasks 5 (relay), 6 (guacd), 9 (broker), 8 (service) ✓
- §4 data model → Tasks 1 (migration) + 2 (models) ✓
- §5 state machine → Task 8 ✓
- §6 relay + token → Task 5 ✓
- §7 credential checkout → Task 8 (unattended path; secret-never-stored asserted) ✓
- §8 recording + retention → Tasks 7 + 11 ✓
- §9 agent wire contract → Task 4 (schema exported; agent impl is the documented fast-follow under `agent/`, out of this cycle and still unbuilt; it was scoped to the separate `it-suite-agent` repo when this plan was written) ✓
- §10 RBAC → Task 1 ✓
- §11 API → Task 10 ✓
- §12 audit events → emitted across Tasks 7, 8, 10 ✓
- §13 frontend → Task 12 ✓
- §16 deploy → Task 13 ✓

**Placeholder scan:** Tasks 10 and 12 reference "mirror helpers in test_dispatch.py" and the existing frontend test setup rather than repeating boilerplate — acceptable since those are pointers to concrete existing code, not undefined behavior. All service/relay/guacd/recording code is fully specified.

**Type consistency:** `mint_relay_token`/`token_hash`/`SessionRelay.attach_agent`/`pump` names are consistent across Tasks 5, 8, 10. `resolve_policy`/`ResolvedPolicy` consistent (Tasks 3, 8). `finalize_recording`/`apply_recording_retention` consistent (Tasks 7, 11). `RemoteSessionOpenParams` consistent (Tasks 4, 8/10). `GuacdClient.connect`/`encode_instruction`/`read_instruction` consistent (Tasks 6, 9, 10).

**Note for the implementer:** confirm `app.state.object_store` exists (used by Tasks 7/10/13); if not, add it in `app/main.py` next to `vault`/`signer` in the same style, as a tiny preliminary step folded into Task 7.
