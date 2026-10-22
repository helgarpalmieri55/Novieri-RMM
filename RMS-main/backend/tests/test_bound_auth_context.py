"""`app.api.deps.bound_auth_context` -- authenticated routes bind org_id and
user_id as contextvars, whether or not they are permission-gated.

THE GAP THIS CLOSES. `require_permission` bound the correlation ids around
its `yield`, so log lines from a gated route carried them. Five routes that
authenticate but need no permission code depended on the bare `auth_context`
instead, which stashes `request.state.org_id`/`user_id` -- enough for the
middleware's capture -- but binds nothing. Ordinary log lines emitted while
handling `GET /me` and the reporting/remote routes therefore had no org_id
or user_id at all, and nothing in the suite said so.

The binding now lives in `bound_auth_context`, once, and `require_permission`
depends on it rather than carrying its own copy.
"""

import logging
from types import SimpleNamespace
from uuid import uuid4

from app.api.deps import AuthContext, bound_auth_context
from app.core.logging import org_id_var, user_id_var


class _Claims:
    org_slug = "acme"
    sub = "sub-1"
    email = "a@example.com"
    name = "A"


async def test_it_binds_org_and_user_for_the_duration_and_unbinds_after(
    monkeypatch,
) -> None:
    """Bound inside, absent again afterwards.

    The unbind half matters as much as the bind: a contextvar left set leaks
    one request's identity into whatever the worker thread handles next,
    which for correlation ids is worse than having none.
    """
    import app.api.deps as deps

    org_id, user_id = uuid4(), uuid4()

    async def _fake_auth_context(request: object) -> AuthContext:
        return AuthContext(org_id=org_id, user_id=user_id, claims=_Claims())

    monkeypatch.setattr(deps, "auth_context", _fake_auth_context)

    assert org_id_var.get() is None
    assert user_id_var.get() is None

    agen = bound_auth_context(SimpleNamespace())
    ctx = await agen.__anext__()

    assert ctx.org_id == org_id
    assert org_id_var.get() == str(org_id), "org_id was not bound for the request"
    assert user_id_var.get() == str(user_id), "user_id was not bound for the request"

    # Closing the generator is what FastAPI does when the endpoint returns.
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
    the `LogRecord`. An earlier version of this test asserted
    `record.org_id` and failed for that reason, which is the difference
    between testing the kernel's actual contract and testing a guess about
    it.
    """
    import json

    import app.api.deps as deps
    from app.core.logging import JsonFormatter

    org_id, user_id = uuid4(), uuid4()

    async def _fake_auth_context(request: object) -> AuthContext:
        return AuthContext(org_id=org_id, user_id=user_id, claims=_Claims())

    monkeypatch.setattr(deps, "auth_context", _fake_auth_context)

    record = logging.LogRecord(
        name="its.test.bound",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="inside the bound request",
        args=(),
        exc_info=None,
    )

    agen = bound_auth_context(SimpleNamespace())
    await agen.__anext__()
    inside = json.loads(JsonFormatter().format(record))
    try:
        await agen.__anext__()
    except StopAsyncIteration:
        pass
    outside = json.loads(JsonFormatter().format(record))

    assert inside["org_id"] == str(org_id)
    assert inside["user_id"] == str(user_id)
    # NON-VACUITY: the same record formatted after the dependency closed must
    # NOT carry them, or the assertion above would pass for a binding that
    # never happened.
    assert "org_id" not in outside or outside["org_id"] is None
    assert "user_id" not in outside or outside["user_id"] is None


def test_no_route_depends_on_the_unbound_auth_context() -> None:
    """THE REGRESSION GUARD. A new authenticated route written with
    `Depends(auth_context)` would silently rejoin the gap above -- it would
    work, pass its own tests, and quietly emit log lines with no org_id.

    Sweeps source text rather than the dependant tree deliberately: the point
    is to catch the pattern being *written*, wherever it is written, without
    needing every module's router imported here.
    """
    from pathlib import Path

    app_dir = Path(__file__).resolve().parent.parent / "app"
    offenders = [
        f"{path.relative_to(app_dir.parent)}:{i}"
        for path in app_dir.rglob("*.py")
        for i, line in enumerate(path.read_text().splitlines(), start=1)
        if "Depends(auth_context)" in line and not line.lstrip().startswith("#")
    ]
    assert offenders == [], (
        "these depend on the UNBOUND auth_context, so their log lines carry no "
        f"org_id/user_id -- use bound_auth_context: {offenders}"
    )
