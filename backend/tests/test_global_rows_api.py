"""Refused global writes are refused LOUDLY, and `/me` is personal (Task 6b).

Migration 0231 made the database the binding guard: a global row -- an O-table
row with `client_id` NULL, "every client" -- is readable under a pinned
non-empty scope, but only scope `all` may change or delete it (spec §2.2). The
database refuses quietly, though: a DELETE that RLS filters matches zero rows
and SQLAlchemy only warns, so `DELETE /credentials/{id}` answered 200, wrote a
`credential.deleted` audit event and left the row where it was -- the audit log
recording a privileged act that never happened (invariant 4). A refused ORM
UPDATE raised `StaleDataError`, a 500.

`app.core.global_rows` refuses such a write BEFORE any SQL is sent, so the
transaction -- and the audit row in it -- rolls back, and the API answers 403
with the every-client refusal of Task 4. Statements the ORM guard cannot see
(bulk INSERT/UPDATE/DELETE) check for themselves; each is pinned below.

The actor is the one `tests/test_global_writes.py` uses: EVERY permission
code, held on one client (the org's internal client X, whose id is the org's)
or on every client.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.global_rows import GlobalRowNotWritable
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    Credential,
    DeviceGroup,
    DocArticle,
    DocLink,
    DocVersion,
    Permission,
    Policy,
    Role,
    RolePermission,
    User,
    UserRole,
)
from tests.health_fixtures import seed_assignment, seed_profile

EVERY_CLIENT = "only a technician with access to every client can do this"


async def _actor(env: Any, org_id: UUID, client_id: UUID | None) -> dict[str, str]:
    """Every permission code on one client, or on every client (None)."""
    sub = await env.create_user(org_id)
    async with AsyncSession(env.admin_engine) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
        role = Role(org_id=org_id, name=f"everything-{uuid4().hex[:8]}")
        session.add(role)
        await session.flush()
        for permission_id in (await session.execute(select(Permission.id))).scalars():
            session.add(RolePermission(role_id=role.id, permission_id=permission_id))
        session.add(UserRole(org_id=org_id, user_id=user_id, role_id=role.id, client_id=client_id))
        await session.commit()
    return dict(await env.headers(org_id, sub))


async def _seed(engine: AsyncEngine, *rows: Any) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add_all(rows)
        await session.commit()


async def _actions(engine: AsyncEngine, org_id: UUID, action: str, target: UUID) -> int:
    async with AsyncSession(engine) as session:
        return len(
            (
                await session.execute(
                    select(AuditEvent.id).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == action,
                        AuditEvent.target_id == str(target),
                    )
                )
            ).all()
        )


async def _exists(engine: AsyncEngine, model: Any, row_id: UUID) -> bool:
    async with AsyncSession(engine) as session:
        found = (await session.execute(select(model.id).where(model.id == row_id))).first()
        return found is not None


def _refused(response: Any) -> bool:
    return bool(response.status_code == 403 and response.json().get("detail") == EVERY_CLIENT)


def _credential(org: UUID, client: UUID | None, name: str) -> Credential:
    return Credential(org_id=org, client_id=client, name=name, type="ssh_key", vault_ref="v")


# --------------------------------------------------------------------------- #
# Item 1, through the API
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("who", ["per-client", "narrowed by X-Client-Id"])
async def test_a_limited_technician_cannot_delete_a_global_credential(
    api_env: Any, who: str
) -> None:
    """403, the row survives, and NO `credential.deleted` is written -- while
    the same person deletes X's own credential, audited."""
    env = api_env
    org = await env.create_org("g6b")
    if who == "per-client":
        headers = await _actor(env, org, org)
    else:
        headers = {**await _actor(env, org, None), "X-Client-Id": str(org)}
    shared, own = _credential(org, None, "global"), _credential(org, org, "x")
    await _seed(env.admin_engine, shared, own)

    response = await env.client.delete(f"/api/v1/credentials/{shared.id}", headers=headers)
    assert _refused(response), (response.status_code, response.text)
    assert await _exists(env.admin_engine, Credential, shared.id)
    assert await _actions(env.admin_engine, org, "credential.deleted", shared.id) == 0

    response = await env.client.delete(f"/api/v1/credentials/{own.id}", headers=headers)
    assert response.status_code == 200, response.text
    assert not await _exists(env.admin_engine, Credential, own.id)
    assert await _actions(env.admin_engine, org, "credential.deleted", own.id) == 1


async def test_an_every_client_technician_deletes_the_global_credential(api_env: Any) -> None:
    env = api_env
    org = await env.create_org("g6b")
    headers = await _actor(env, org, None)
    shared = _credential(org, None, "global")
    await _seed(env.admin_engine, shared)

    response = await env.client.delete(f"/api/v1/credentials/{shared.id}", headers=headers)
    assert response.status_code == 200, response.text
    assert not await _exists(env.admin_engine, Credential, shared.id)
    assert await _actions(env.admin_engine, org, "credential.deleted", shared.id) == 1


async def test_a_limited_technician_cannot_edit_a_global_credential(api_env: Any) -> None:
    """The refused UPDATE was a `StaleDataError`, a 500."""
    env = api_env
    org = await env.create_org("g6b")
    headers = await _actor(env, org, org)
    shared = _credential(org, None, "global")
    await _seed(env.admin_engine, shared)

    response = await env.client.put(
        f"/api/v1/credentials/{shared.id}", headers=headers, json={"name": "renamed"}
    )
    assert _refused(response), (response.status_code, response.text)
    async with AsyncSession(env.admin_engine) as session:
        name = (
            await session.execute(select(Credential.name).where(Credential.id == shared.id))
        ).scalar_one()
    assert name == "global"


async def test_a_limited_technician_cannot_create_a_global_credential(api_env: Any) -> None:
    """No group, so no client: a new global row. Refused before the INSERT,
    not by the database after it."""
    env = api_env
    org = await env.create_org("g6b")
    headers = await _actor(env, org, org)
    response = await env.client.post(
        "/api/v1/credentials",
        headers=headers,
        json={"name": "new", "type": "ssh_key", "secret": "s3cret"},
    )
    assert _refused(response), (response.status_code, response.text)


async def test_a_limited_technician_cannot_archive_a_global_article(api_env: Any) -> None:
    """The documentation API has no hard delete; archiving is its delete."""
    env = api_env
    org = await env.create_org("g6b")
    headers = await _actor(env, org, org)
    article = DocArticle(org_id=org, title="global runbook")
    await _seed(env.admin_engine, article)

    response = await env.client.post(
        f"/api/v1/docs/articles/{article.id}/archive", headers=headers
    )
    assert _refused(response), (response.status_code, response.text)
    async with AsyncSession(env.admin_engine) as session:
        archived = (
            await session.execute(select(DocArticle.archived).where(DocArticle.id == article.id))
        ).scalar_one()
    assert archived is False
    assert await _actions(env.admin_engine, org, "docs.article_archived", article.id) == 0


async def test_a_limited_technician_cannot_delete_a_global_articles_link(api_env: Any) -> None:
    """A real DELETE on the documentation family: the link of a global
    article is itself global (it takes the article's client)."""
    env = api_env
    org = await env.create_org("g6b")
    headers = await _actor(env, org, org)
    article = DocArticle(org_id=org, title="global runbook")
    await _seed(env.admin_engine, article)
    link = DocLink(org_id=org, article_id=article.id, target_type="device", target_id=uuid4())
    await _seed(env.admin_engine, link)

    response = await env.client.delete(
        f"/api/v1/docs/articles/{article.id}/links/{link.id}", headers=headers
    )
    assert _refused(response), (response.status_code, response.text)
    assert await _exists(env.admin_engine, DocLink, link.id)
    assert await _actions(env.admin_engine, org, "docs.link_removed", article.id) == 0


# --------------------------------------------------------------------------- #
# Item 1, the bulk statements the ORM guard cannot see
# --------------------------------------------------------------------------- #


async def test_a_limited_technician_cannot_delete_the_org_default_health_assignment(
    api_env: Any,
) -> None:
    """`delete_assignment` is a bulk `DELETE ... RETURNING`: RLS made it
    match nothing and the route said 404 about a row the person can read."""
    env = api_env
    org = await env.create_org("g6b")
    headers = await _actor(env, org, org)
    profile = await seed_profile(env, org)
    assignment = await seed_assignment(env, org, profile)  # no device, no group: global

    response = await env.client.delete(
        f"/api/v1/health/assignments/{assignment}", headers=headers
    )
    assert _refused(response), (response.status_code, response.text)
    async with AsyncSession(env.admin_engine) as session:
        left = (
            await session.execute(
                text("SELECT count(*) FROM health_profile_assignment WHERE id = :i"),
                {"i": assignment},
            )
        ).scalar_one()
    assert left == 1

    every = await _actor(env, org, None)
    response = await env.client.delete(f"/api/v1/health/assignments/{assignment}", headers=every)
    assert response.status_code == 204, response.text


async def test_a_limited_technician_cannot_create_the_org_default_health_assignment(
    api_env: Any,
) -> None:
    """A bulk `INSERT ... ON CONFLICT`: the database refused it with a 500."""
    env = api_env
    org = await env.create_org("g6b")
    headers = await _actor(env, org, org)
    profile = await seed_profile(env, org)
    response = await env.client.post(
        "/api/v1/health/assignments", headers=headers, json={"profile_id": str(profile)}
    )
    assert _refused(response), (response.status_code, response.text)


async def test_a_limited_technician_cannot_create_an_sla_policy(api_env: Any) -> None:
    """`sla_policy` rows carry no client in phase 1: every one is global."""
    env = api_env
    org = await env.create_org("g6b")
    headers = await _actor(env, org, org)
    response = await env.client.post(
        "/api/v1/sla/policies",
        headers=headers,
        json={
            "calendar_id": str(uuid4()),
            "response_target_minutes": 60,
            "resolution_target_minutes": 480,
        },
    )
    assert _refused(response), (response.status_code, response.text)


async def test_a_limited_scope_cannot_approve_a_patch(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`patch_approvals` rows carry no client in phase 1: the approval is
    global, inserted by a bulk `INSERT ... ON CONFLICT`."""
    from app.models import Organization
    from app.modules.patch.approve import approve_patch
    from app.modules.patch.models import Patch

    org = Organization(name="p", slug=f"p-{uuid4().hex[:8]}")
    await _seed(admin_engine, org)
    actor = User(org_id=org.id, external_subject=uuid4().hex, email="a@test.local")
    patch = Patch(
        org_id=org.id, os_family="windows", source="wu", identifier="KB1", title="t",
        severity="important",
    )
    await _seed(admin_engine, actor, patch)
    with pytest.raises(GlobalRowNotWritable):
        async with org_session(org.id, clients=ClientScope.only([org.id]), engine=app_engine) as s:
            await approve_patch(
                s, org_id=org.id, actor_id=actor.id, patch_id=patch.id, ring_name="pilot"
            )
    async with org_session(org.id, clients=ClientScope.all(), engine=app_engine) as s:
        approval = await approve_patch(
            s, org_id=org.id, actor_id=actor.id, patch_id=patch.id, ring_name="pilot"
        )
        assert approval.client_id is None


# --------------------------------------------------------------------------- #
# Item 1, the guard itself (no HTTP)
# --------------------------------------------------------------------------- #


async def _org_with_group(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    from app.models import Organization

    org = Organization(name="g", slug=f"g-{uuid4().hex[:8]}")
    await _seed(admin_engine, org)
    group = DeviceGroup(org_id=org.id, client_id=org.id, name="gx")
    await _seed(admin_engine, group)
    return org.id, group.id


async def test_the_guard_refuses_global_policies_and_passes_a_clients(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """An O table WITH a parent: `policies` takes its client from its device
    group. A policy with no group is global; X's group's policy is X's."""
    org, group = await _org_with_group(admin_engine)
    shared = Policy(org_id=org, module="patch", config={})
    own = Policy(org_id=org, module="patch", config={}, device_group_id=group)
    await _seed(admin_engine, shared, own)
    x = ClientScope.only([org])

    async def attempt(change: Any) -> None:
        async with org_session(org, clients=x, engine=app_engine) as s:
            row = (await s.execute(select(Policy).where(Policy.id == change[0]))).scalar_one()
            change[1](s, row)
            await s.flush()

    def edit(s: Any, row: Policy) -> None:
        row.config = {"edited": True}

    def claim(s: Any, row: Policy) -> None:
        row.device_group_id = group  # "claim a global row into my client"

    async def remove(s: Any, row: Policy) -> None:
        await s.delete(row)

    for refused in (edit, claim):
        with pytest.raises(GlobalRowNotWritable) as caught:
            await attempt((shared.id, refused))
        assert caught.value.table == "policies"
    with pytest.raises(GlobalRowNotWritable):
        async with org_session(org, clients=x, engine=app_engine) as s:
            row = (await s.execute(select(Policy).where(Policy.id == shared.id))).scalar_one()
            await remove(s, row)
            await s.flush()
    with pytest.raises(GlobalRowNotWritable):
        async with org_session(org, clients=x, engine=app_engine) as s:
            s.add(Policy(org_id=org, module="patch", config={}))
            await s.flush()

    # X's own: edited, created through its group, and deleted.
    await attempt((own.id, edit))
    async with org_session(org, clients=x, engine=app_engine) as s:
        made = Policy(org_id=org, module="patch", config={}, device_group_id=group)
        s.add(made)
        await s.flush()
        await s.refresh(made, ["client_id"])
        assert made.client_id == org
        row = (await s.execute(select(Policy).where(Policy.id == own.id))).scalar_one()
        await s.delete(row)
        await s.flush()

    # Scope all changes the global row.
    async with org_session(org, clients=ClientScope.all(), engine=app_engine) as s:
        row = (await s.execute(select(Policy).where(Policy.id == shared.id))).scalar_one()
        row.config = {"edited": True}
        await s.flush()


async def test_the_guard_reads_an_unloaded_client_from_the_database(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`client_id` is expired after the trigger may have changed it
    (`server_onupdate`): the guard asks the database, it does not assume."""
    org, _ = await _org_with_group(admin_engine)
    shared = Policy(org_id=org, module="patch", config={})
    await _seed(admin_engine, shared)
    with pytest.raises(GlobalRowNotWritable):
        async with org_session(org, clients=ClientScope.only([org]), engine=app_engine) as s:
            row = (await s.execute(select(Policy).where(Policy.id == shared.id))).scalar_one()
            s.expire(row, ["client_id"])
            row.config = {"edited": True}
            await s.flush()


async def test_the_guard_follows_a_child_to_its_global_parent(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A link or version of a GLOBAL article is global: the trigger copies the
    article's NULL. One of X's article is X's."""
    org, _ = await _org_with_group(admin_engine)
    shared = DocArticle(org_id=org, title="global")
    own = DocArticle(org_id=org, client_id=org, title="x")
    await _seed(admin_engine, shared, own)
    x = ClientScope.only([org])
    for child in (
        lambda a: DocLink(org_id=org, article_id=a, target_type="device", target_id=uuid4()),
        lambda a: DocVersion(
            org_id=org, article_id=a, version=9, snapshot={}, ts=datetime.now(UTC)
        ),
    ):
        with pytest.raises(GlobalRowNotWritable):
            async with org_session(org, clients=x, engine=app_engine) as s:
                s.add(child(shared.id))
                await s.flush()
        async with org_session(org, clients=x, engine=app_engine) as s:
            s.add(child(own.id))
            await s.flush()


async def test_the_guard_refuses_releasing_a_row_to_every_client(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The other direction of a claim: X's credential made global."""
    org, _ = await _org_with_group(admin_engine)
    own = _credential(org, org, "x")
    await _seed(admin_engine, own)
    with pytest.raises(GlobalRowNotWritable):
        async with org_session(org, clients=ClientScope.only([org]), engine=app_engine) as s:
            row = (await s.execute(select(Credential).where(Credential.id == own.id))).scalar_one()
            row.client_id = None
            await s.flush()


async def test_the_guard_holds_staff_rows_and_passes_contacts(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`users`: a staff row (client NULL) is changed, deleted or created only
    under scope all; X's contact is X's. Since this task the database agrees
    on the INSERT (`client_isolation_insert`)."""
    org, _ = await _org_with_group(admin_engine)
    staff = User(org_id=org, external_subject=uuid4().hex, email="s@test.local")
    await _seed(admin_engine, staff)
    x = ClientScope.only([org])
    with pytest.raises(GlobalRowNotWritable):
        async with org_session(org, clients=x, engine=app_engine) as s:
            row = (await s.execute(select(User).where(User.id == staff.id))).scalar_one()
            row.display_name = "edited"
            await s.flush()
    with pytest.raises(GlobalRowNotWritable):
        async with org_session(org, clients=x, engine=app_engine) as s:
            s.add(User(org_id=org, external_subject=uuid4().hex, email="n@test.local"))
            await s.flush()
    async with org_session(org, clients=x, engine=app_engine) as s:
        contact = User(
            org_id=org, external_subject=uuid4().hex, email="c@test.local",
            kind="contact", client_id=org,
        )
        s.add(contact)
        await s.flush()
        contact.display_name = "edited"
        await s.flush()


def test_the_guards_second_parents_are_the_migrations() -> None:
    """The guard derives a child's client from its parent; the two O tables
    with a second parent must be the ones 0231 gives a second trigger."""
    from app.core.global_rows import SECOND_PARENTS
    from tests.test_client_catalog import EXTRA_PARENT_TRIGGERS

    assert {(t, p, fk) for t, (p, fk) in SECOND_PARENTS.items()} == {
        (t, p, fk) for (t, _), (p, fk) in EXTRA_PARENT_TRIGGERS.items()
    }


# --------------------------------------------------------------------------- #
# Item 2: /me is personal
# --------------------------------------------------------------------------- #


async def test_a_person_with_no_grants_reads_and_writes_their_own_profile(api_env: Any) -> None:
    """No grant is the EMPTY scope, and 0231 hides staff rows from it. `/me`
    is about the caller's own row, so it reads and writes it anyway."""
    env = api_env
    org = await env.create_org("me6b")
    sub = await env.create_user(org)
    headers = await env.headers(org, sub)

    assert (await env.client.get("/api/v1/me", headers=headers)).status_code == 200
    response = await env.client.patch(
        "/api/v1/me", headers=headers, json={"display_name": "Nadie", "locale": "es"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["roles"] == []
    async with AsyncSession(env.admin_engine) as session:
        row = (
            await session.execute(select(User).where(User.external_subject == sub))
        ).scalar_one()
    assert (row.display_name, row.locale) == ("Nadie", "es")


async def test_a_per_client_technician_writes_their_own_profile(api_env: Any) -> None:
    """Scope [X] reads the staff row but may not change a global row: `/me`
    is not a change to global configuration, and runs under every client."""
    env = api_env
    org = await env.create_org("me6b")
    headers = await _actor(env, org, org)
    response = await env.client.patch("/api/v1/me", headers=headers, json={"phone": "0101"})
    assert response.status_code == 200, response.text
    assert response.json()["phone"] == "0101"


async def test_a_narrowed_header_does_not_hide_the_callers_own_profile(api_env: Any) -> None:
    """An X-Client-Id the person may not use narrows to the EMPTY scope."""
    env = api_env
    org = await env.create_org("me6b")
    headers = {**await _actor(env, org, org), "X-Client-Id": str(uuid4())}
    assert (await env.client.get("/api/v1/me", headers=headers)).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "contact"},
        {"client_id": "00000000-0000-0000-0000-000000000001"},
        {"external_subject": "someone-else"},
        {"status": "active"},
        {"display_name": "Fine", "kind": "staff"},
    ],
)
async def test_patch_me_cannot_set_who_the_caller_is(api_env: Any, body: dict[str, Any]) -> None:
    """Under scope all the database no longer holds a narrowed scope back,
    so the body schema is what must: `extra="forbid"`, a 422, nothing written."""
    env = api_env
    org = await env.create_org("me6b")
    sub = await env.create_user(org, "technician")
    headers = await env.headers(org, sub)
    async with AsyncSession(env.admin_engine) as session:
        before = (
            await session.execute(select(User).where(User.external_subject == sub))
        ).scalar_one()
    response = await env.client.patch("/api/v1/me", headers=headers, json=body)
    assert response.status_code == 422, response.text
    async with AsyncSession(env.admin_engine) as session:
        after = (
            await session.execute(select(User).where(User.external_subject == sub))
        ).scalar_one()
    assert (after.kind, after.client_id, after.external_subject, after.status) == (
        before.kind, before.client_id, before.external_subject, before.status,
    )
    assert after.display_name == before.display_name


async def test_the_refusal_is_spoken_in_the_requests_language(api_env: Any) -> None:
    """The 403 reuses Task 4's refusal, Spanish included -- and the MCP
    channel passes a 403's detail through as it is."""
    from app.core.i18n import CATALOG_ES
    from app.mcp.invoke import error_text

    env = api_env
    org = await env.create_org("g6b")
    headers = {**await _actor(env, org, org), "Accept-Language": "es"}
    shared = _credential(org, None, "global")
    await _seed(env.admin_engine, shared)
    response = await env.client.delete(f"/api/v1/credentials/{shared.id}", headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"] == CATALOG_ES[EVERY_CLIENT] != EVERY_CLIENT
    assert error_text(403, EVERY_CLIENT) == EVERY_CLIENT
