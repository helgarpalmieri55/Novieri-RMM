"""Tests for the change-management ticket port (Odoo Helpdesk contract, deferred).

v1 ships only the protocol + a null adapter: no Odoo client, no network. The
NullChangeTicketAdapter must be inert -- push_change returns an empty ref and
writes nothing to change_external_ref; update_change/close_change are no-ops.
"""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.config import Settings
from app.core.tenancy import org_session
from app.models import ChangeExternalRef, ChangeRequest, Organization, User
from app.modules.change_mgmt.ticketing import (
    ChangeTicketPort,
    NullChangeTicketAdapter,
    get_ticket_adapter,
)

# Static conformance check: NullChangeTicketAdapter structurally satisfies
# ChangeTicketPort (mypy verifies this line; no isinstance check needed).
_: ChangeTicketPort = NullChangeTicketAdapter()


async def _org_user_change(admin_engine: AsyncEngine):
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="tix", slug=f"tix-{uuid4().hex[:6]}")
        s.add(org)
        await s.flush()
        usr = User(org_id=org.id, external_subject=f"s{uuid4().hex[:6]}", email="a@b.c")
        s.add(usr)
        await s.flush()
        change = ChangeRequest(
            org_id=org.id,
            type="normal",
            title="Test change",
            description="desc",
            risk="low",
            created_by=usr.id,
        )
        s.add(change)
        await s.commit()
        return org.id, usr.id, change


def test_get_ticket_adapter_null_backend_returns_null_adapter() -> None:
    """`null` is the explicit opt-out; the default is service_desk."""
    settings = Settings(
        database_url="postgresql+asyncpg://app_user:app@localhost/x",
        database_admin_url="postgresql+asyncpg://admin:a@localhost/x",
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir="/tmp/ca",
        change_ticket_backend="null",
    )
    adapter = get_ticket_adapter(settings)
    assert isinstance(adapter, NullChangeTicketAdapter)


def test_get_ticket_adapter_unknown_backend_raises() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://app_user:app@localhost/x",
        database_admin_url="postgresql+asyncpg://admin:a@localhost/x",
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir="/tmp/ca",
        change_ticket_backend="odoo",
    )
    with pytest.raises(ValueError, match="odoo"):
        get_ticket_adapter(settings)


async def test_null_adapter_push_change_returns_empty_ref_and_writes_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _user_id, change = await _org_user_change(admin_engine)
    adapter = NullChangeTicketAdapter()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        result = await adapter.push_change(s, org_id=org_id, change=change)
        assert result == ""

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        rows = (
            await s.execute(select(ChangeExternalRef).where(ChangeExternalRef.org_id == org_id))
        ).scalars().all()
        assert rows == []


async def test_null_adapter_update_and_close_are_noop(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _user_id, change = await _org_user_change(admin_engine)
    adapter = NullChangeTicketAdapter()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        update_result = await adapter.update_change(
            s, org_id=org_id, external_id="ext-1", change=change
        )
        assert update_result is None
        close_result = await adapter.close_change(s, org_id=org_id, external_id="ext-1")
        assert close_result is None

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        rows = (
            await s.execute(select(ChangeExternalRef).where(ChangeExternalRef.org_id == org_id))
        ).scalars().all()
        assert rows == []
