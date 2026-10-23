"""The operator error-groups API: list, detail, resolve, reopen
(docs/superpowers/specs/2026-08-15-error-monitoring-design.md §5; Task 5).

TWO PROPERTIES MATTER MOST HERE:

1. NO EXCEPTION TEXT REACHES A RESPONSE (spec §0.1 falsifier #1). Proven with
   a raise whose `str()` is a DSN carrying a password -- and the raise is
   checked to actually carry that text BEFORE it is asserted absent
   downstream, so a raise that never held the secret cannot make this test
   pass vacuously.
2. TWO DISTINCT CODES. `platform.errors.read` gates the two GET routes;
   `platform.errors.write` gates resolve. A caller holding only the read code
   must get 403 from resolve, not merely "succeed without needing write" --
   the two are asserted as separate permissions, not one renamed.

SESSION-SCOPED DATABASE, NOT PER-TEST: `pg_cluster` (`tests/conftest.py`) is
one throwaway Postgres for the whole run, and `platform_error_group`/`_event`
carry no tenant column to filter by (spec §4.1) -- so other test files'
captures land in the same tables this file reads. Every assertion below
either looks up ITS OWN row by a fingerprint-identifying field (`exc_type`
paired with a nonce baked into the raise site) rather than assuming position,
or compares two groups THIS TEST drained itself, in the order it drained
them.
"""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.errors.capture import capture_exception, drained
from app.main import drain_platform_errors_once
from app.models import Permission, Role, RolePermission, User, UserRole

# --------------------------------------------------------------------------- #
# Seeding helpers.
# --------------------------------------------------------------------------- #


async def _holder(api_env: Any, org_id: UUID, *codes: str) -> dict[str, str]:
    """Headers for a user holding EXACTLY `codes` and nothing else -- so a
    route gated on the wrong code, or on "any of", is observable rather than
    hidden behind an org-admin fixture that holds everything."""
    sub = f"sub-{uuid4().hex[:10]}"
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user = User(org_id=org_id, external_subject=sub, email=f"{sub}@test.local")
        role = Role(org_id=org_id, name=f"role-{uuid4().hex[:8]}")
        session.add_all([user, role])
        await session.flush()
        for code in codes:
            permission_id = (
                await session.execute(select(Permission.id).where(Permission.code == code))
            ).scalar_one()
            session.add(RolePermission(role_id=role.id, permission_id=permission_id))
        session.add(UserRole(org_id=org_id, user_id=user.id, role_id=role.id))
        await session.commit()
    return await api_env.headers(org_id, sub)


@pytest.fixture()
async def _clean_queue() -> AsyncIterator[None]:
    """The capture queue is process-global (spec §3.1) -- start and end each
    test with it empty so one test's drops/leftovers cannot leak into the
    next."""
    drained()
    yield
    drained()


def _raise_and_capture(exc: BaseException, *, where: str = "api") -> None:
    try:
        raise exc
    except type(exc) as caught:
        capture_exception(caught, where=where)


class _LeakyErr(Exception):
    """Its `str()` is a DSN with a password -- the shape this project's two
    real leaks had (matches `tests/test_error_capture.py::_LeakyErr`)."""

    def __str__(self) -> str:
        return "could not connect: postgres://its:S3CRET-PW@db.internal/itsuite"


# --------------------------------------------------------------------------- #
# Permissions: seeded, and granted to the platform operator -- NOT to every
# tenant's own administrator.
# --------------------------------------------------------------------------- #


async def test_permissions_seeded_and_granted_to_super_admin_not_org_admin(
    admin_engine: Any,
) -> None:
    """Migration 0068. This assertion used to name `org-admin` on both rows,
    and that grant is what audit 2026-08-21-tenancy-test-efficacy F1 was: these
    routes read `platform_error_event` -- which carries `org_id`, `user_id`,
    `job_id` and `device_id` for EVERY tenant and whose only RLS policy is
    `USING (true)` -- so the code that gates them decides whether a tenant
    administrator can enumerate other tenants' identifiers. It could.

    `super-admin` is "Cross-org platform administration (SaaS operator)"
    (migration 0003) and no org-scoped console surface can grant, clone or
    assign it (`app/core/rbac_admin.py`, migration 0065's database function).
    That is the role a cross-tenant read belongs to."""
    async with AsyncSession(admin_engine) as session:
        codes = set((await session.execute(select(Permission.code))).scalars())
        assert {"platform.errors.read", "platform.errors.write"} <= codes

        rows = (
            await session.execute(
                select(Role.name, Permission.code)
                .join(RolePermission, RolePermission.role_id == Role.id)
                .join(Permission, Permission.id == RolePermission.permission_id)
                .where(Permission.code.in_(["platform.errors.read", "platform.errors.write"]))
                .where(Role.org_id.is_(None))
            )
        ).all()
        granted = {(name, code) for name, code in rows}
        assert granted == {
            ("super-admin", "platform.errors.read"),
            ("super-admin", "platform.errors.write"),
        }


async def test_an_org_admin_is_refused_and_a_super_admin_is_not(
    api_env: Any, _clean_queue: None
) -> None:
    """THE REGRESSION TEST FOR F1, ASSERTED FROM BOTH SIDES.

    One side alone would be worthless. "org-admin gets 403" passes just as
    happily if the routes were deleted, or gated on a code nobody holds, or
    broken; "super-admin gets 200" passes just as happily if the grant were
    left on org-admin too. Together they say the boundary MOVED rather than
    closed: the tenant administrator is out and the platform operator is in.

    Both GET routes, because both read `platform_error_event` and the detail
    route is the one the audit reproduced the disclosure through -- it is the
    route that returns the events, and with them the four correlation ids.
    """
    org_id = await api_env.create_org("err-su-boundary")
    _raise_and_capture(RuntimeError("boundary check: a real group to fetch"))
    assert await drain_platform_errors_once(api_env.app.state.app_engine) == 1

    tenant_admin = await api_env.headers(
        org_id, await api_env.create_user(org_id, "org-admin")
    )
    operator = await api_env.headers(
        org_id, await api_env.create_user(org_id, "super-admin")
    )

    listing = await api_env.client.get("/api/v1/platform/errors", headers=operator)
    assert listing.status_code == 200, listing.text
    group_id = next(
        g["id"] for g in listing.json()["groups"] if g["exc_type"] == "RuntimeError"
    )
    detail = await api_env.client.get(
        f"/api/v1/platform/errors/{group_id}", headers=operator
    )
    assert detail.status_code == 200, detail.text

    refused_list = await api_env.client.get("/api/v1/platform/errors", headers=tenant_admin)
    assert refused_list.status_code == 403, refused_list.text
    assert refused_list.json()["detail"] == "requires platform.errors.read"

    refused_detail = await api_env.client.get(
        f"/api/v1/platform/errors/{group_id}", headers=tenant_admin
    )
    assert refused_detail.status_code == 403, refused_detail.text
    assert refused_detail.json()["detail"] == "requires platform.errors.read"


async def test_an_org_admin_is_refused_both_triage_writes(api_env: Any, _clean_queue: None) -> None:
    """The write code moved with the read one, and for the same reason: an
    operator's triage queue is not a thing one tenant's administrator should be
    able to reach into and mark resolved for everybody else."""
    org_id = await api_env.create_org("err-su-writes")
    _raise_and_capture(RuntimeError("boundary check: a group to resolve"))
    assert await drain_platform_errors_once(api_env.app.state.app_engine) == 1

    tenant_admin = await api_env.headers(
        org_id, await api_env.create_user(org_id, "org-admin")
    )
    operator = await api_env.headers(
        org_id, await api_env.create_user(org_id, "super-admin")
    )

    listing = await api_env.client.get("/api/v1/platform/errors", headers=operator)
    group_id = next(
        g["id"] for g in listing.json()["groups"] if g["exc_type"] == "RuntimeError"
    )

    for route in (f"/api/v1/platform/errors/{group_id}/resolve",
                  f"/api/v1/platform/errors/{group_id}/unresolve"):
        refused = await api_env.client.post(route, headers=tenant_admin)
        assert refused.status_code == 403, refused.text
        assert refused.json()["detail"] == "requires platform.errors.write"
        allowed = await api_env.client.post(route, headers=operator)
        assert allowed.status_code == 200, allowed.text


# --------------------------------------------------------------------------- #
# Gating: two codes, not one.
# --------------------------------------------------------------------------- #


async def test_list_requires_platform_errors_read(api_env: Any) -> None:
    org_id = await api_env.create_org("err-gate-list")
    headers = await _holder(api_env, org_id)  # no codes at all
    response = await api_env.client.get("/api/v1/platform/errors", headers=headers)
    assert response.status_code == 403


async def test_detail_requires_platform_errors_read(api_env: Any) -> None:
    org_id = await api_env.create_org("err-gate-detail")
    headers = await _holder(api_env, org_id)
    response = await api_env.client.get(
        f"/api/v1/platform/errors/{uuid4()}", headers=headers
    )
    assert response.status_code == 403


async def test_resolve_requires_platform_errors_write_not_read(api_env: Any) -> None:
    """THE CENTRAL GATING CLAIM: a caller holding ONLY the read code is
    refused resolve. If `platform.errors.write` were merely a synonym for
    `platform.errors.read` this would be a 404 (permitted, group missing),
    not a 403."""
    org_id = await api_env.create_org("err-gate-resolve")
    headers = await _holder(api_env, org_id, "platform.errors.read")
    response = await api_env.client.post(
        f"/api/v1/platform/errors/{uuid4()}/resolve", headers=headers
    )
    assert response.status_code == 403


async def test_read_alone_cannot_reach_resolve_even_for_a_real_group(
    api_env: Any, _clean_queue: None
) -> None:
    """Same claim as above, against a group that genuinely exists -- so a 403
    here cannot be explained away as "would have 404'd anyway"."""
    org_id = await api_env.create_org("err-gate-resolve-real")
    _raise_and_capture(RuntimeError("gate check: real group"))
    written = await drain_platform_errors_once(api_env.app.state.app_engine)
    assert written == 1

    read_headers = await _holder(api_env, org_id, "platform.errors.read")
    listing = await api_env.client.get("/api/v1/platform/errors", headers=read_headers)
    assert listing.status_code == 200
    group_id = next(
        g["id"] for g in listing.json()["groups"] if g["exc_type"] == "RuntimeError"
    )

    response = await api_env.client.post(
        f"/api/v1/platform/errors/{group_id}/resolve", headers=read_headers
    )
    assert response.status_code == 403

    write_headers = await _holder(api_env, org_id, "platform.errors.write")
    response = await api_env.client.post(
        f"/api/v1/platform/errors/{group_id}/resolve", headers=write_headers
    )
    assert response.status_code == 200
    assert response.json()["resolved_at"] is not None


# --------------------------------------------------------------------------- #
# No exception text in any response -- proven, not assumed.
# --------------------------------------------------------------------------- #


async def test_no_exception_text_in_list_or_detail_response(
    api_env: Any, _clean_queue: None
) -> None:
    exc = _LeakyErr()
    # THE SANITY CHECK the brief demands: confirm the raise genuinely carries
    # the secret before relying on its absence proving anything downstream.
    assert "S3CRET-PW" in str(exc)
    assert "postgres://" in str(exc)

    _raise_and_capture(exc)
    written = await drain_platform_errors_once(api_env.app.state.app_engine)
    assert written == 1

    org_id = await api_env.create_org("err-no-leak")
    headers = await _holder(api_env, org_id, "platform.errors.read")

    listing = await api_env.client.get("/api/v1/platform/errors", headers=headers)
    assert listing.status_code == 200
    assert "S3CRET-PW" not in listing.text
    assert "postgres://" not in listing.text
    assert "could not connect" not in listing.text

    group_id = next(
        g["id"] for g in listing.json()["groups"] if g["exc_type"] == "_LeakyErr"
    )

    detail = await api_env.client.get(f"/api/v1/platform/errors/{group_id}", headers=headers)
    assert detail.status_code == 200
    assert "S3CRET-PW" not in detail.text
    assert "postgres://" not in detail.text
    assert "could not connect" not in detail.text
    assert detail.json()["group"]["exc_type"] == "_LeakyErr"


# --------------------------------------------------------------------------- #
# Content: ordering, counts, detail events, resolve, 404, dropped.
# --------------------------------------------------------------------------- #


async def test_list_orders_newest_last_seen_first(api_env: Any, _clean_queue: None) -> None:
    org_id = await api_env.create_org("err-order")
    headers = await _holder(api_env, org_id, "platform.errors.read")

    def _older() -> None:
        _raise_and_capture(ValueError("order: older"))

    def _newer() -> None:
        _raise_and_capture(TypeError("order: newer"))

    _older()
    assert await drain_platform_errors_once(api_env.app.state.app_engine) == 1
    _newer()
    assert await drain_platform_errors_once(api_env.app.state.app_engine) == 1

    response = await api_env.client.get("/api/v1/platform/errors", headers=headers)
    assert response.status_code == 200
    groups = response.json()["groups"]
    older_idx = next(i for i, g in enumerate(groups) if g["exc_type"] == "ValueError")
    newer_idx = next(i for i, g in enumerate(groups) if g["exc_type"] == "TypeError")
    assert newer_idx < older_idx, "newest last_seen must come first"


async def test_list_reports_event_counts(api_env: Any, _clean_queue: None) -> None:
    org_id = await api_env.create_org("err-counts")
    headers = await _holder(api_env, org_id, "platform.errors.read")

    def _raise_site() -> None:
        _raise_and_capture(KeyError("counts: repeat"))

    _raise_site()
    _raise_site()
    _raise_site()
    written = await drain_platform_errors_once(api_env.app.state.app_engine)
    assert written == 3

    response = await api_env.client.get("/api/v1/platform/errors", headers=headers)
    group = next(g for g in response.json()["groups"] if g["exc_type"] == "KeyError")
    assert group["event_count"] >= 3


async def test_list_reports_dropped_count(api_env: Any, _clean_queue: None) -> None:
    from app.core.errors.capture import QUEUE_MAX

    for _ in range(QUEUE_MAX + 7):
        _raise_and_capture(OSError("overflow"))
    # Drain what fit; the rest were dropped and counted (spec §3.1).
    await drain_platform_errors_once(api_env.app.state.app_engine)

    org_id = await api_env.create_org("err-dropped")
    headers = await _holder(api_env, org_id, "platform.errors.read")
    response = await api_env.client.get("/api/v1/platform/errors", headers=headers)
    assert response.status_code == 200
    assert response.json()["dropped"] >= 7


async def test_detail_lists_recent_events_correlation_ids(
    api_env: Any, _clean_queue: None
) -> None:
    from app.core.logging import bind

    with bind(request_id="req-detail-abc123"):
        _raise_and_capture(LookupError("detail: correlation"))
    written = await drain_platform_errors_once(api_env.app.state.app_engine)
    assert written == 1

    org_id = await api_env.create_org("err-detail")
    headers = await _holder(api_env, org_id, "platform.errors.read")
    listing = await api_env.client.get("/api/v1/platform/errors", headers=headers)
    group_id = next(
        g["id"] for g in listing.json()["groups"] if g["exc_type"] == "LookupError"
    )

    detail = await api_env.client.get(f"/api/v1/platform/errors/{group_id}", headers=headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["group"]["id"] == group_id
    assert any(e["request_id"] == "req-detail-abc123" for e in body["events"])


async def test_detail_404_for_an_unknown_group(api_env: Any) -> None:
    org_id = await api_env.create_org("err-404")
    headers = await _holder(api_env, org_id, "platform.errors.read")
    response = await api_env.client.get(f"/api/v1/platform/errors/{uuid4()}", headers=headers)
    assert response.status_code == 404


async def test_resolve_404_for_an_unknown_group(api_env: Any) -> None:
    org_id = await api_env.create_org("err-resolve-404")
    headers = await _holder(api_env, org_id, "platform.errors.write")
    response = await api_env.client.post(
        f"/api/v1/platform/errors/{uuid4()}/resolve", headers=headers
    )
    assert response.status_code == 404


async def test_resolve_sets_resolved_at_and_is_reflected_on_relist(
    api_env: Any, _clean_queue: None
) -> None:
    _raise_and_capture(ArithmeticError("resolve: relist"))
    written = await drain_platform_errors_once(api_env.app.state.app_engine)
    assert written == 1

    org_id = await api_env.create_org("err-resolve-relist")
    read_headers = await _holder(api_env, org_id, "platform.errors.read")
    write_headers = await _holder(api_env, org_id, "platform.errors.write")

    listing = await api_env.client.get("/api/v1/platform/errors", headers=read_headers)
    group = next(
        g for g in listing.json()["groups"] if g["exc_type"] == "ArithmeticError"
    )
    assert group["resolved_at"] is None

    resolved = await api_env.client.post(
        f"/api/v1/platform/errors/{group['id']}/resolve", headers=write_headers
    )
    assert resolved.status_code == 200
    assert resolved.json()["resolved_at"] is not None

    relisted = await api_env.client.get("/api/v1/platform/errors", headers=read_headers)
    relisted_group = next(
        g for g in relisted.json()["groups"] if g["id"] == group["id"]
    )
    assert relisted_group["resolved_at"] is not None


# --------------------------------------------------------------------------- #
# Reopen: the inverse of resolve (recommendation 8, unblocking B76).
# --------------------------------------------------------------------------- #


async def _audit_rows(api_env: Any, org_id: UUID) -> list[Any]:
    """Every audit event in ONE org's chain, oldest first.

    Read through `org_session`, not the admin engine, so a row written under
    the wrong `org_id` is invisible here rather than merely mis-labelled --
    which is what makes the cross-org assertion below mean something.
    """
    from app.core.tenancy import org_session
    from app.models import AuditEvent

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        return list(
            (await session.execute(select(AuditEvent).order_by(AuditEvent.id))).scalars()
        )


async def _a_group(api_env: Any, label: str) -> str:
    """Capture one failure, drain it, and return the id of the group it made.

    The exception TYPE is minted fresh per call, because the database is
    session-scoped (see this file's docstring) and every capture routed
    through `_raise_and_capture` shares a raise site -- so `exc_type` is the
    only thing separating one group from another, and a name another test
    file might also raise would make `scalar_one()` a coin toss.
    """
    from app.models import PlatformErrorGroup

    exc_type = type(f"Reopen{label}{uuid4().hex[:8]}", (Exception,), {})
    _raise_and_capture(exc_type("reopen fixture"))
    written = await drain_platform_errors_once(api_env.app.state.app_engine)
    assert written == 1
    async with AsyncSession(api_env.app_engine, expire_on_commit=False) as session:
        group_id = (
            await session.execute(
                select(PlatformErrorGroup.id).where(
                    PlatformErrorGroup.exc_type == exc_type.__name__
                )
            )
        ).scalar_one()
    return str(group_id)


async def test_unresolve_requires_platform_errors_write_not_read(api_env: Any) -> None:
    """Reopening is not a lesser act than resolving, so it takes the same code.

    A caller holding only `platform.errors.read` must be refused. If the route
    were gated on the read code -- or on nothing -- this would be a 404
    (permitted, group missing), not a 403.
    """
    org_id = await api_env.create_org("err-gate-unresolve")
    headers = await _holder(api_env, org_id, "platform.errors.read")
    response = await api_env.client.post(
        f"/api/v1/platform/errors/{uuid4()}/unresolve", headers=headers
    )
    assert response.status_code == 403


async def test_unresolve_404_for_an_unknown_group(api_env: Any) -> None:
    org_id = await api_env.create_org("err-unresolve-404")
    headers = await _holder(api_env, org_id, "platform.errors.write")
    response = await api_env.client.post(
        f"/api/v1/platform/errors/{uuid4()}/unresolve", headers=headers
    )
    assert response.status_code == 404


async def test_unresolve_clears_resolved_at_and_is_reflected_on_relist(
    api_env: Any, _clean_queue: None
) -> None:
    """THE FINDING (B76): a group resolved by mistake can be put back.

    Asserted on the relist as well as on the route's own answer, because the
    console reads the list -- a route that answered `null` without persisting
    it would leave the operator looking at a group that is still resolved.
    """
    group_id = await _a_group(api_env, "Relist")

    org_id = await api_env.create_org("err-reopen-relist")
    read_headers = await _holder(api_env, org_id, "platform.errors.read")
    write_headers = await _holder(api_env, org_id, "platform.errors.write")

    resolved = await api_env.client.post(
        f"/api/v1/platform/errors/{group_id}/resolve", headers=write_headers
    )
    assert resolved.status_code == 200
    assert resolved.json()["resolved_at"] is not None

    reopened = await api_env.client.post(
        f"/api/v1/platform/errors/{group_id}/unresolve", headers=write_headers
    )
    assert reopened.status_code == 200
    assert reopened.json()["resolved_at"] is None

    relisted = await api_env.client.get("/api/v1/platform/errors", headers=read_headers)
    relisted_group = next(g for g in relisted.json()["groups"] if g["id"] == group_id)
    assert relisted_group["resolved_at"] is None


async def test_reopening_records_the_timestamp_it_cleared_in_the_audit_log(
    api_env: Any, _clean_queue: None
) -> None:
    """`resolved_at` IS the record of the resolution -- there is no
    `resolved_by`, no note, nothing else -- so clearing it destroys the only
    trace that anybody ever resolved this group.

    That is acceptable only because the audit log keeps what the column gave
    up: who resolved it, who reopened it, and the exact instant that was
    blanked. Without this the undo would trade one defect (no way back) for a
    worse one (a state change with no history).
    """
    group_id = await _a_group(api_env, "Audit")

    org_id = await api_env.create_org("err-reopen-audit")
    headers = await _holder(api_env, org_id, "platform.errors.write")

    resolved = await api_env.client.post(
        f"/api/v1/platform/errors/{group_id}/resolve", headers=headers
    )
    assert resolved.status_code == 200
    resolved_at = resolved.json()["resolved_at"]

    reopened = await api_env.client.post(
        f"/api/v1/platform/errors/{group_id}/unresolve", headers=headers
    )
    assert reopened.status_code == 200

    rows = [r for r in await _audit_rows(api_env, org_id) if r.target_id == group_id]
    assert [r.action for r in rows] == [
        "platform_error.resolved",
        "platform_error.unresolved",
    ]
    assert {r.target_type for r in rows} == {"platform_error_group"}
    assert all(r.actor_type == "user" and r.actor_id is not None for r in rows)

    cleared = rows[1].meta["previous_resolved_at"]
    assert cleared is not None
    assert datetime.fromisoformat(cleared) == datetime.fromisoformat(resolved_at)


async def test_resolving_is_audited_too_so_the_reopen_record_has_a_counterpart(
    api_env: Any, _clean_queue: None
) -> None:
    """A reopen event with no matching resolve event would say a resolution was
    undone that the log never shows happening. Both halves, or neither."""
    group_id = await _a_group(api_env, "ResolveAudit")

    org_id = await api_env.create_org("err-resolve-audit")
    headers = await _holder(api_env, org_id, "platform.errors.write")

    response = await api_env.client.post(
        f"/api/v1/platform/errors/{group_id}/resolve", headers=headers
    )
    assert response.status_code == 200

    rows = [r for r in await _audit_rows(api_env, org_id) if r.target_id == group_id]
    assert [r.action for r in rows] == ["platform_error.resolved"]
    assert datetime.fromisoformat(rows[0].meta["resolved_at"]) == datetime.fromisoformat(
        response.json()["resolved_at"]
    )


async def test_the_audit_chain_still_verifies_after_a_resolve_and_a_reopen(
    api_env: Any, _clean_queue: None
) -> None:
    """These two appends go through `audit_service.record` like every other
    privileged action, so the chain they extend must still verify."""
    from app.core.audit import audit_service
    from app.core.tenancy import org_session

    group_id = await _a_group(api_env, "Chain")

    org_id = await api_env.create_org("err-reopen-chain")
    headers = await _holder(api_env, org_id, "platform.errors.write")

    await api_env.client.post(
        f"/api/v1/platform/errors/{group_id}/resolve", headers=headers
    )
    await api_env.client.post(
        f"/api/v1/platform/errors/{group_id}/unresolve", headers=headers
    )

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        result = await audit_service.verify(session, org_id)
    assert result.ok is True
    assert result.count >= 2


async def test_reopening_an_open_group_is_a_no_op_that_still_leaves_a_record(
    api_env: Any, _clean_queue: None
) -> None:
    """Idempotent, matching resolve (which simply moves the timestamp
    forward). It is still audited, with `previous_resolved_at` null: the log
    records what an operator DID, and "tried to reopen a group nobody had
    resolved" is a fact about the operator, not a state delta to be optimised
    away."""
    group_id = await _a_group(api_env, "Idempotent")

    org_id = await api_env.create_org("err-reopen-idem")
    headers = await _holder(api_env, org_id, "platform.errors.write")

    first = await api_env.client.post(
        f"/api/v1/platform/errors/{group_id}/unresolve", headers=headers
    )
    assert first.status_code == 200
    assert first.json()["resolved_at"] is None

    rows = [r for r in await _audit_rows(api_env, org_id) if r.target_id == group_id]
    assert [r.action for r in rows] == ["platform_error.unresolved"]
    assert rows[0].meta["previous_resolved_at"] is None


async def test_a_group_is_platform_scoped_but_the_audit_row_belongs_to_the_actor(
    api_env: Any, _clean_queue: None
) -> None:
    """THE TENANCY CLAIM, both halves.

    The group is deliberately NOT tenant-scoped (spec §4.1, and
    `tests/test_tenant_column_sweep.py::TENANTLESS_BY_DESIGN`): a broken code
    path is platform information, and authorisation is by the platform-wide
    `platform.errors.write` code. So an operator in org B reopening a group
    whose events correlate to org A is CORRECT, and asserted here so that the
    tenant-pinned session the audit append needs cannot quietly grow into a
    row filter nobody decided on.

    What IS tenant-scoped is the audit row: it lands in the acting operator's
    chain and nowhere else. A record of org B's action appearing in org A's
    log would be a tenancy leak in the direction that actually matters.
    """
    from app.core.logging import bind

    org_a = await api_env.create_org("err-scope-a")
    org_b = await api_env.create_org("err-scope-b")

    with bind(org_id=str(org_a)):
        group_id = await _a_group(api_env, "CrossOrg")

    b_headers = await _holder(api_env, org_b, "platform.errors.write")
    resolved = await api_env.client.post(
        f"/api/v1/platform/errors/{group_id}/resolve", headers=b_headers
    )
    assert resolved.status_code == 200
    reopened = await api_env.client.post(
        f"/api/v1/platform/errors/{group_id}/unresolve", headers=b_headers
    )
    assert reopened.status_code == 200, "the group is platform-scoped, not org A's"

    assert [
        r.action for r in await _audit_rows(api_env, org_b) if r.target_id == group_id
    ] == ["platform_error.resolved", "platform_error.unresolved"]
    assert [
        r.action for r in await _audit_rows(api_env, org_a) if r.target_id == group_id
    ] == []
