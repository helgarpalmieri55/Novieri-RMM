"""Service Desk migration (docs/modules/service-desk/spec.md §2, §8.2)."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_TABLES = [
    "ticket_queue",
    "ticket_category",
    "ticket_number_counter",
    "ticket",
    "ticket_comment",
    "ticket_attachment",
    "ticket_approval",
    "ticket_status_history",
]

_PERMISSIONS = [
    "ticket.read",
    "ticket.write",
    "ticket.assign",
    "ticket.approve",
    "ticket.admin",
]


async def test_all_tables_exist_with_forced_rls(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        for table in _TABLES:
            row = (
                await conn.execute(
                    text(
                        "SELECT c.relrowsecurity, c.relforcerowsecurity FROM pg_class c"
                        " JOIN pg_namespace n ON n.oid = c.relnamespace"
                        " WHERE n.nspname = 'public' AND c.relname = :t"
                    ),
                    {"t": table},
                )
            ).one_or_none()
            assert row is not None, f"{table} does not exist"
            assert row == (True, True), f"{table} RLS not enabled+forced: {row}"


async def test_permissions_seeded(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        codes = {
            r.code
            for r in await conn.execute(
                text("SELECT code FROM permissions WHERE code LIKE 'ticket.%'")
            )
        }
    assert codes == set(_PERMISSIONS)


async def test_technician_holds_read_write_assign_only(admin_engine: AsyncEngine) -> None:
    """Spec §0.6: the inverse of Budget. Service Desk is a technician's primary
    tool, so technician gets read/write/assign -- and deliberately NOT approve
    (a requester must not authorize their own request) or admin."""
    async with admin_engine.connect() as conn:
        codes = {
            r.code
            for r in await conn.execute(
                text(
                    "SELECT p.code FROM permissions p"
                    " JOIN role_permissions rp ON rp.permission_id = p.id"
                    " JOIN roles r ON r.id = rp.role_id"
                    " WHERE r.name = 'technician' AND p.code LIKE 'ticket.%'"
                )
            )
        }
    assert codes == {"ticket.read", "ticket.write", "ticket.assign"}


async def test_auditor_holds_read_only(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        codes = {
            r.code
            for r in await conn.execute(
                text(
                    "SELECT p.code FROM permissions p"
                    " JOIN role_permissions rp ON rp.permission_id = p.id"
                    " JOIN roles r ON r.id = rp.role_id"
                    " WHERE r.name = 'auditor' AND p.code LIKE 'ticket.%'"
                )
            )
        }
    assert codes == {"ticket.read"}


async def test_ticket_origin_check_constraint_pins_all_five_values(
    admin_engine: AsyncEngine,
) -> None:
    """Migration 0024 (whole-branch security review, finding A3): `origin`
    had no CHECK constraint enumerating its legal values before this
    migration -- `schema.py`'s `TicketOrigin` `Literal` was the only
    enforcement. `ck_ticket_origin_valid` must exist and must include
    `"integration"`, the new value `push_change` now uses, alongside the
    four pre-existing ones."""
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) AS constraint_def FROM pg_constraint"
                    " WHERE conname = 'ck_ticket_origin_valid'"
                )
            )
        ).one_or_none()
    assert row is not None, "ck_ticket_origin_valid constraint is missing"
    for origin in ("technician", "alert", "portal", "email", "integration"):
        assert origin in row.constraint_def, f"{origin!r} missing from {row.constraint_def!r}"
