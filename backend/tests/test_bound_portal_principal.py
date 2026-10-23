"""`app.modules.portal.principal.bound_portal_principal` -- portal routes bind
org_id and user_id as contextvars, exactly as staff routes do.

THE GAP THIS CLOSES. `app.api.deps.bound_auth_context` gave every *staff*
route its correlation ids, and `tests/test_bound_auth_context.py`'s sweep
stops a new staff route from rejoining the gap. The portal was outside both:
`resolve_portal_principal` awaits `auth_context` DIRECTLY (it must -- it is
still establishing the identity that `portal_session` needs), which is not the
`Depends(auth_context)` pattern that sweep looks for, and nothing anywhere in
`app/modules/portal/` called `bind()`. Every log line emitted while handling
any of the seven portal routes therefore carried no org_id and no user_id --
including the AI resolver's loopback calls, which are the platform's most
attacker-adjacent request path and the one where "which tenant was this?"
matters most.

The binding lives in `bound_portal_principal`, once, and
`require_portal_permission` depends on it rather than on the bare resolver --
the same shape as `require_permission` depending on `bound_auth_context`.
"""

import json
import logging
from types import SimpleNamespace
from uuid import uuid4

from app.core.client_scope import ClientScope
from app.core.logging import JsonFormatter, org_id_var, user_id_var
from app.modules.portal.principal import (
    PortalContext,
    bound_portal_principal,
    resolve_portal_principal,
)


def _patch_resolver(monkeypatch, ctx: PortalContext) -> None:
    import app.modules.portal.principal as principal

    async def _fake(request: object) -> PortalContext:
        return ctx

    monkeypatch.setattr(principal, "resolve_portal_principal", _fake)


async def test_it_binds_org_and_requester_and_unbinds_after(monkeypatch) -> None:
    """Bound inside, absent again afterwards.

    The unbind half matters as much as the bind: a contextvar left set leaks
    one requester's identity into whatever the worker handles next, which for
    a portal principal -- an end user, not staff -- is worse than having none.
    """
    ctx = PortalContext(org_id=uuid4(), requester_user_id=uuid4(), clients=ClientScope.all())
    _patch_resolver(monkeypatch, ctx)

    assert org_id_var.get() is None
    assert user_id_var.get() is None

    agen = bound_portal_principal(SimpleNamespace())
    bound = await agen.__anext__()

    assert bound is ctx
    assert org_id_var.get() == str(ctx.org_id), "org_id was not bound for the request"
    assert user_id_var.get() == str(ctx.requester_user_id), (
        "the requester's id was not bound as user_id"
    )

    try:
        await agen.__anext__()
    except StopAsyncIteration:
        pass

    assert org_id_var.get() is None, "org_id leaked past the request"
    assert user_id_var.get() is None, "user_id leaked past the request"


async def test_a_log_line_emitted_inside_carries_both_ids(monkeypatch) -> None:
    """The point of the binding, end to end: a line FORMATTED while the
    dependency is active carries org_id and user_id.

    Formatted, not merely emitted -- `JsonFormatter` reads the contextvars at
    format time and writes them into the payload; it does not stamp them onto
    the `LogRecord`. Asserting `record.org_id` here would pass for a binding
    that never happened, which is the mistake the staff-side version of this
    test made first.
    """
    ctx = PortalContext(org_id=uuid4(), requester_user_id=uuid4(), clients=ClientScope.all())
    _patch_resolver(monkeypatch, ctx)

    record = logging.LogRecord(
        name="its.test.portal",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="inside the bound portal request",
        args=(),
        exc_info=None,
    )

    agen = bound_portal_principal(SimpleNamespace())
    await agen.__anext__()
    inside = json.loads(JsonFormatter().format(record))
    try:
        await agen.__anext__()
    except StopAsyncIteration:
        pass
    outside = json.loads(JsonFormatter().format(record))

    assert inside["org_id"] == str(ctx.org_id)
    assert inside["user_id"] == str(ctx.requester_user_id)
    # NON-VACUITY: the same record formatted after the dependency closed must
    # NOT carry them, or the assertions above would pass for a binding that
    # never happened.
    assert "org_id" not in outside or outside["org_id"] is None
    assert "user_id" not in outside or outside["user_id"] is None


def test_every_portal_route_reaches_the_bound_resolver() -> None:
    """THE STRUCTURAL GUARD, on the real router rather than on source text.

    Walks each portal route's dependant tree and requires
    `bound_portal_principal` in it -- and requires the bare
    `resolve_portal_principal` NOT to appear as a route dependency, since a
    route wired to that one authenticates fine, passes its own tests, and
    silently emits log lines with no tenant.
    """
    from fastapi.dependencies.models import Dependant

    from app.modules.portal.api import router

    def _calls(dependant: Dependant) -> set[object]:
        found: set[object] = set()
        for sub in dependant.dependencies:
            if sub.call is not None:
                found.add(sub.call)
            found |= _calls(sub)
        return found

    routes = [route for route in router.routes if hasattr(route, "dependant")]
    assert routes, "no portal routes found -- this guard would be vacuous"

    unbound = []
    for route in routes:
        calls = _calls(route.dependant)
        if bound_portal_principal not in calls or resolve_portal_principal in calls:
            unbound.append(f"{sorted(route.methods)[0]} {route.path}")

    assert unbound == [], (
        "these portal routes do not reach bound_portal_principal, so their log "
        f"lines carry no org_id/user_id: {unbound}"
    )


def test_no_module_depends_on_the_unbound_portal_resolver() -> None:
    """The source-text half of the guard, mirroring
    `tests/test_bound_auth_context.py`'s sweep: catches the pattern being
    *written* anywhere in `app/`, including in a module whose router this
    test does not import.
    """
    from pathlib import Path

    app_dir = Path(__file__).resolve().parent.parent / "app"
    offenders = [
        f"{path.relative_to(app_dir.parent)}:{i}"
        for path in app_dir.rglob("*.py")
        for i, line in enumerate(path.read_text().splitlines(), start=1)
        if "Depends(resolve_portal_principal)" in line and not line.lstrip().startswith("#")
    ]
    assert offenders == [], (
        "these depend on the UNBOUND portal resolver, so their log lines carry "
        f"no org_id/user_id -- use bound_portal_principal: {offenders}"
    )
