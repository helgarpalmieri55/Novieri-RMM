"""Primary user from inventory (docs/modules/people/spec.md §2).

Registered into inventory's post-ingest extension point, which runs this
inside its own savepoint: a failure here never fails the inventory job.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device
from app.modules.inventory.schema import InventoryDocument
from app.modules.people.service import observe_login


async def inventory_post_ingest(
    session: AsyncSession, device: Device, doc: InventoryDocument
) -> None:
    login = doc.facts.get("console_user")
    # Absent means nobody is signed in, which says nothing about who uses it.
    if not isinstance(login, str) or not login.strip():
        return
    await observe_login(
        session,
        org_id=device.org_id,
        device_id=device.id,
        # The sign-in names one of the device's client's people, or nobody.
        client_id=device.client_id,
        login=login,
    )
