"""Post-ingest extension point for the `inventory.collect` result path.

**INVENTORY OWNS THIS FILE.** It exists so another module can observe an
inventory result without re-registering `inventory.collect` or being imported by
inventory: the dependency arrow points OTHER MODULE -> inventory, never the
reverse. `app.jobs.registry.register` overwrites by name, so a module CAN take
over `inventory.collect` with its own handler -- and that "works" only until
someone reorders `register_all`, and it forks this module's ingest for whatever
inventory does next.

The extension point is deliberately generic: it takes the device and the
validated document, not anything any particular module is shaped like.

**EVERY HOOK RUNS INSIDE `session.begin_nested()` -- A SAVEPOINT -- WITH
`except Exception` LOGGED. The SAVEPOINT is not belt-and-braces around the
`except`; the `except` alone cannot deliver the guarantee.** If a hook raises a
DATABASE error -- an integrity violation, a bad cast, a deadlock -- Postgres
puts the enclosing transaction into an aborted state at the moment the statement
fails. Catching the Python exception does not undo that: every subsequent
statement in that transaction fails with "current transaction is aborted", so
inventory's own already-written device, software and `hardware_components` rows
are lost with it, and `JobEngine.complete` (`app/jobs/engine.py:145-180`, which
wraps the registered `result_handler` in `try/except Exception` and flips the
job to `failed` with `{"error": ...}`) marks the `inventory.collect` job failed.
A `try/except` around a database error is a comment, not a control. The
SAVEPOINT is what bounds the rollback to the hook.

One ordering detail that makes this correct rather than merely hopeful:
`Session.begin_nested()` autoflushes already-pending dirty state the instant it
opens the subtransaction (verified empirically against this exact ordering bug
at `app/modules/notification/channels.py:288`). Because hooks run AFTER
`ingest_inventory`, that autoflush lands inventory's writes OUTSIDE the
savepoint, before it opens -- so rolling the savepoint back discards the hook's
work and keeps inventory's. Precedents for the pattern:
`app/modules/notification/channels.py:325`,
`app/modules/email_intake/mailboxes.py:277`, `app/modules/sla/policies.py:499`.

A hook that fails does so QUIETLY, leaving a log line. That is the correct
trade: the alternative is failing a fleet-wide inventory job for a side effect.
"""

import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device
from app.modules.inventory.schema import InventoryDocument

logger = logging.getLogger(__name__)

PostIngestHook = Callable[[AsyncSession, Device, InventoryDocument], Awaitable[None]]

_HOOKS: list[PostIngestHook] = []

__all__ = [
    "PostIngestHook",
    "clear_post_ingest_hooks",
    "post_ingest_hooks",
    "register_post_ingest_hook",
    "run_post_ingest_hooks",
]


def register_post_ingest_hook(hook: PostIngestHook) -> None:
    """Append `hook`, once.

    IDEMPOTENT BY IDENTITY, because registration is not a one-time event in this
    codebase: the API app registers its modules through `register_all` and the
    gateway process registers them again for itself, and a test suite does both
    repeatedly in one process. Appending unconditionally would run one module's
    hook N times per ingest, which for an upsert-based hook is invisible until
    it is a `sighting_count` nobody can explain.
    """
    if hook not in _HOOKS:
        _HOOKS.append(hook)


def post_ingest_hooks() -> tuple[PostIngestHook, ...]:
    """The registered hooks, as an immutable snapshot."""
    return tuple(_HOOKS)


def clear_post_ingest_hooks() -> None:
    """Drop every registered hook. For tests that register a deliberately
    misbehaving one and must restore the process afterwards."""
    _HOOKS.clear()


async def run_post_ingest_hooks(
    session: AsyncSession, device: Device, doc: InventoryDocument
) -> None:
    """Run every registered hook. Returns nothing and NEVER raises.

    Each hook gets its own SAVEPOINT, so one failing hook rolls back only its
    own writes and the hooks after it still run.
    """
    for hook in tuple(_HOOKS):
        try:
            async with session.begin_nested():
                await hook(session, device, doc)
        except Exception:  # noqa: BLE001 - containment is the entire contract
            logger.exception(
                "post-ingest hook %s failed for device %s",
                getattr(hook, "__qualname__", hook),
                device.id,
            )
