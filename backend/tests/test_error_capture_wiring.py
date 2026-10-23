"""Task 3, API side: `request_context_middleware` actually calls
`capture_exception`, and org_id/user_id -- unwired since sub-project 1,
measured before this task started -- now reach it too.

`api_env` (tests/conftest.py) builds a real `create_app()` FastAPI app over
`httpx.AsyncClient(transport=httpx.ASGITransport(app=app))`.
`raise_app_exceptions` defaults True on that transport, so an unhandled
exception still propagates OUT of `client.get(...)` after
`ServerErrorMiddleware` builds (and this middleware sends) the 500 -- every
test below wraps the request in `pytest.raises` for that reason, not because
the capture itself fails.

Routes are added directly to `api_env.app` per test (`app.get(...)` after
`create_app()` has already returned) rather than adding permanent
`/__test/...` routes to the production router -- the point of each test is
the MIDDLEWARE's behaviour on an unhandled exception, not any real route's.
"""

import pytest
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.deps import AuthContext, auth_context
from app.core.errors.capture import drained


async def test_an_unauthenticated_requests_capture_carries_no_org_or_user(api_env) -> None:  # noqa: ANN001
    """No auth dependency runs on this route at all, so nothing ever calls
    `bind(org_id=..., user_id=...)` -- the captured record's two fields must
    be `None`, not empty strings (spec's own absent-correlation-ids rule)."""
    drained()  # start empty

    @api_env.app.get("/__test/boom-anon")
    async def _boom_anon() -> None:
        raise RuntimeError("boom-anon")

    with pytest.raises(RuntimeError, match="boom-anon"):
        await api_env.client.get("/__test/boom-anon")

    captured = drained()
    assert len(captured) == 1
    record = captured[0]
    assert record.where == "api"
    assert record.exc_type == "RuntimeError"
    assert record.org_id is None
    assert record.user_id is None


async def test_an_authenticated_requests_capture_carries_org_and_user(
    api_env, admin_engine: AsyncEngine  # noqa: ANN001
) -> None:
    """`app.api.deps.auth_context` stashes org_id/user_id on `request.state`
    once authentication resolves; `request_context_middleware`'s `except`
    clause reads them back and binds them (via `bind()`) around the
    `capture_exception` call itself -- see that middleware's own comment for
    why a contextvar set inside the route's own dependency graph cannot
    reach this middleware directly (an asyncio Task boundary, not a timing
    one)."""
    from app.models import User

    drained()  # start empty
    org_id = await api_env.create_org()
    sub = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, sub)

    async with AsyncSession(admin_engine) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()

    @api_env.app.get("/__test/boom-authed")
    async def _boom_authed(ctx: AuthContext = Depends(auth_context)) -> None:  # noqa: B008
        raise RuntimeError("boom-authed")

    with pytest.raises(RuntimeError, match="boom-authed"):
        await api_env.client.get("/__test/boom-authed", headers=headers)

    captured = drained()
    assert len(captured) == 1
    record = captured[0]
    assert record.where == "api"
    assert record.org_id == str(org_id)
    assert record.user_id == str(user_id)


async def test_a_request_that_fails_authentication_captures_nothing_bound(
    api_env,  # noqa: ANN001
) -> None:
    """A request with no/garbage credentials never reaches `auth_context`'s
    binding line at all -- `validate_token` raises first, which FastAPI's
    own `HTTPException` handling turns into an ordinary 401 response, never
    an unhandled exception this middleware's `except` clause sees. Nothing
    is captured, and nothing leaks into whatever runs next."""
    drained()  # start empty

    @api_env.app.get("/__test/needs-auth-only")
    async def _needs_auth(ctx: AuthContext = Depends(auth_context)) -> dict[str, str]:  # noqa: B008
        return {"ok": "yes"}

    resp = await api_env.client.get(
        "/__test/needs-auth-only", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert resp.status_code == 401
    assert drained() == []
