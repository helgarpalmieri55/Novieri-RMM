"""The `credentials.checkout` conjunction every push path checks (spec §2.5,
§8; R10), in its two halves -- written once, called from the push request
(`preflight.request_push_batch`), the run list (`run_list.authorized_prefixes`,
`run_list.expand_prefix`) and a person's Resume (`runs.resume_run`).

A push presents a stored secret to a machine, so the route's `agent.push.run`
is never enough: the caller must also hold `credentials.checkout`.

1. **SOMEWHERE FIRST** (`require_checkout_somewhere`): in some client, before
   the request is parsed or the credential resolved. It is a fact about the
   caller, and a caller who may decrypt nothing anywhere is told nothing about
   which credentials, machines or subnets exist.
2. **THEN IN THE CLIENT OF THE ACT** (`require_checkout_in`): the credential's,
   or the run's (plan 2026-10-26 Task 12). A technician holding both codes on
   one client pushes there, and nowhere else. `client_id=None` asks for an
   every-client grant, which is what a list over every client needs.

Each caller keeps its own refusal text; only the checks are shared.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rbac import codes_held_anywhere, has_permission
from app.modules.agent_push.errors import PushPermissionError

__all__ = ["require_checkout_in", "require_checkout_somewhere"]

_CODE = "credentials.checkout"


async def require_checkout_somewhere(
    session: AsyncSession, actor_id: UUID, refusal: str
) -> None:
    """`PushPermissionError(refusal)` unless `actor_id` holds
    `credentials.checkout` in at least one client."""
    if _CODE not in await codes_held_anywhere(session, actor_id):
        raise PushPermissionError(refusal)


async def require_checkout_in(
    session: AsyncSession, actor_id: UUID, client_id: UUID | None, refusal: str
) -> None:
    """`PushPermissionError(refusal)` unless `actor_id` holds
    `credentials.checkout` in `client_id` -- or, for `None`, in every client."""
    if not await has_permission(session, actor_id, _CODE, client_id=client_id):
        raise PushPermissionError(refusal)
