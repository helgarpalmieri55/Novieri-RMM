"""Change-ticket port: the external-ticketing contract for change management.

v1 shipped a deferred contract only, with `NullChangeTicketAdapter` as the
only adapter -- completely inert, no network calls, never writes to
`change_external_ref`. docs/modules/change-mgmt/spec.md §5 originally pinned
an `OdooChangeTicketAdapter` as the eventual real backend behind this same
port; docs/modules/service-desk/spec.md §7.1 reverses that (Odoo is no
longer this platform's ticketing target at all, §0.1) and Service Desk now
ships that real backend instead:
`app.modules.service_desk.adapters.ServiceDeskChangeTicketAdapter`, selected
by `ITS_CHANGE_TICKET_BACKEND=service_desk`, which is the default since
2026-09-23: a change with no ticket was a change nobody outside the module
saw. `ITS_CHANGE_TICKET_BACKEND=null` is the explicit opt-out.
"""

from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import ChangeRequest


class ChangeTicketPort(Protocol):
    async def push_change(
        self, session: AsyncSession, *, org_id: UUID, change: ChangeRequest
    ) -> str: ...

    async def update_change(
        self, session: AsyncSession, *, org_id: UUID, external_id: str, change: ChangeRequest
    ) -> None: ...

    async def close_change(
        self, session: AsyncSession, *, org_id: UUID, external_id: str
    ) -> None: ...


class NullChangeTicketAdapter:
    """v1 no-op adapter -- the Odoo JSON-RPC adapter is a later phase behind this
    same port. Every method is inert: no network, and push_change returns an
    empty external ref and writes NOTHING to change_external_ref."""

    async def push_change(
        self, session: AsyncSession, *, org_id: UUID, change: ChangeRequest
    ) -> str:
        return ""

    async def update_change(
        self, session: AsyncSession, *, org_id: UUID, external_id: str, change: ChangeRequest
    ) -> None:
        return None

    async def close_change(
        self, session: AsyncSession, *, org_id: UUID, external_id: str
    ) -> None:
        return None


def get_ticket_adapter(settings: Settings) -> ChangeTicketPort:
    backend = settings.change_ticket_backend
    if backend == "null":
        return NullChangeTicketAdapter()
    if backend == "service_desk":
        # Local import: change_mgmt has no other reason to import
        # service_desk, and every other module boundary in this platform
        # defers a cross-module import until the branch that actually needs
        # it runs (mirrors `app.modules.register_all`'s per-call imports).
        from app.modules.service_desk.adapters import ServiceDeskChangeTicketAdapter

        return ServiceDeskChangeTicketAdapter()
    raise ValueError(  # i18n-exempt: bad server setting, a logged 500
        f"unsupported change ticket backend: {backend!r}"
    )
