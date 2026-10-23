"""Push runs through HTTP (design §1.1, §1.3, §1.5, §1.7; CLAUDE.md
invariants 4, 5, 6; 2a(e) as amended 2026-09-30).

Envelopes are published AFTER the transaction commits. The vault and the
object store are doubles, so no test needs OpenBao or S3.
"""

import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Permission, Role, RolePermission, User, UserRole
from app.modules.agent_push import runs
from app.modules.agent_push.models import PushRun, PushRunTarget
from tests.push_run_fixtures import (
    CONSOLE,
    CRED_JSON,
    FakeStore,
    FakeVault,
    make_dispatcher,
    run_world,
)

PUSH = "/api/v1/agent-push"


@pytest.fixture(autouse=True)
def _register_push_job_types() -> None:
    from app.modules.agent_push.jobtypes import register_job_types

    register_job_types()


@pytest.fixture()
def published(api_env, monkeypatch) -> list[tuple[UUID, dict[str, Any]]]:
    from app.modules.agent_push import api as push_api

    sent: list[tuple[UUID, dict[str, Any]]] = []

    async def _publish(redis: Any, device_id: UUID, envelope: dict[str, Any]) -> None:
        sent.append((device_id, envelope))

    monkeypatch.setattr(push_api, "publish_job", _publish)
    monkeypatch.setattr(push_api, "_object_store", lambda request: FakeStore())
    monkeypatch.setattr(api_env.app.state, "vault", FakeVault())
    monkeypatch.setattr(api_env.app.state.settings, "public_console_url", CONSOLE)
    return sent


async def _person(api_env, org_id: UUID, codes: list[str] | None = None) -> tuple[dict, UUID]:
    """A user holding `org-admin`, or exactly `codes`; returns (headers, user id)."""
    if codes is None:
        sub = await api_env.create_user(org_id, "org-admin")
    else:
        sub = f"sub-{uuid4().hex[:10]}"
        async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
            role = Role(org_id=org_id, name=f"r-{uuid4().hex[:6]}")
            session.add(role)
            await session.flush()
            for code in codes:
                permission = (
                    await session.execute(select(Permission).where(Permission.code == code))
                ).scalar_one()
                session.add(RolePermission(role_id=role.id, permission_id=permission.id))
            user = User(org_id=org_id, external_subject=sub, email=f"{sub}@test.local")
            session.add(user)
            await session.flush()
            session.add(UserRole(org_id=org_id, user_id=user.id, role_id=role.id))
            await session.commit()
    async with AsyncSession(api_env.admin_engine) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return await api_env.headers(org_id, sub), user_id


async def _preview(api_env, headers, world) -> dict[str, Any]:
    response = await api_env.client.get(
        f"{PUSH}/runs/preview",
        params={"prefix": world.prefix, "credential_id": str(world.credential_id)},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _start(api_env, headers, world, digest: str):
    return await api_env.client.post(
        f"{PUSH}/runs",
        json={
            "prefix": world.prefix,
            "credential_id": str(world.credential_id),
            "list_digest": digest,
        },
        headers=headers,
    )


async def test_pick_preview_confirm_and_the_first_jobs_are_published_after_commit(
    api_env, published
) -> None:
    world = await run_world(api_env, "ra-go", octet=131, count=2)
    headers, user_id = await _person(api_env, world.org.org_id)

    prefixes = (await api_env.client.get(f"{PUSH}/runs/prefixes", headers=headers)).json()
    assert [p["prefix"] for p in prefixes] == [world.prefix]
    assert prefixes[0]["address_count"] == 256

    preview = await _preview(api_env, headers, world)
    assert preview["ready_count"] == 2 and preview["blocked_count"] == 0
    assert preview["unvouched_count"] == 0 and preview["awaiting_review_count"] == 0
    assert [r["address"] for r in preview["rows"]] == list(world.addresses)
    assert preview["run_max"] == 1024 and preview["concurrency"] == 50

    response = await _start(api_env, headers, world, preview["list_digest"])
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["state"] == "running" and run["target_count"] == 2
    # One first (spec §1.4a): one target until a sign-in has got through.
    assert run["counts"]["in_flight"] == 1 and run["counts"]["waiting"] == 1
    assert run["signed_in"] is False and run["auth_failure_limit"] == 3
    assert [device for device, _e in published] == [world.site_server_device_id]

    async with AsyncSession(api_env.admin_engine) as session:
        (started,) = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == world.org.org_id,
                    AuditEvent.action == "push.run.started",
                )
            )
        ).scalars().all()
    assert started.actor_id == user_id
    assert "hunter2" not in response.text and "enroll_token" not in response.text


async def test_a_changed_list_is_a_409_the_console_can_recognise(api_env, published) -> None:
    world = await run_world(api_env, "ra-chg", octet=132, count=1)
    headers, _user = await _person(api_env, world.org.org_id)
    response = await _start(api_env, headers, world, "0" * 64)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "list_changed"
    assert published == []
    async with AsyncSession(api_env.admin_engine) as session:
        count = (
            await session.execute(
                select(func.count()).select_from(PushRun).where(PushRun.org_id == world.org.org_id)
            )
        ).scalar_one()
    assert count == 0


async def test_a_second_active_run_is_a_409(api_env, published) -> None:
    world = await run_world(api_env, "ra-two", octet=133, count=1)
    headers, _user = await _person(api_env, world.org.org_id)
    digest = (await _preview(api_env, headers, world))["list_digest"]
    assert (await _start(api_env, headers, world, digest)).status_code == 201
    second = await _start(api_env, headers, world, digest)
    assert second.status_code == 409 and "one active run" in second.json()["detail"]


async def test_preview_start_and_resume_need_credentials_checkout_but_pause_does_not(
    api_env, published
) -> None:
    world = await run_world(api_env, "ra-conj", octet=134, count=1)
    admin, _user = await _person(api_env, world.org.org_id)
    runner, _runner_id = await _person(api_env, world.org.org_id, ["agent.push.run"])

    preview = await api_env.client.get(
        f"{PUSH}/runs/preview",
        params={"prefix": world.prefix, "credential_id": str(world.credential_id)},
        headers=runner,
    )
    assert preview.status_code == 403 and "credentials.checkout" in preview.json()["detail"]
    assert (await _start(api_env, runner, world, "a" * 64)).status_code == 403

    digest = (await _preview(api_env, admin, world))["list_digest"]
    run_id = (await _start(api_env, admin, world, digest)).json()["id"]
    # Stopping is always safe: the run code alone may pause.
    paused = await api_env.client.post(f"{PUSH}/runs/{run_id}/pause", headers=runner)
    assert paused.status_code == 200
    resumed = await api_env.client.post(f"{PUSH}/runs/{run_id}/resume", headers=runner)
    assert resumed.status_code == 403


async def test_pause_resume_cancel_through_http_are_audited_as_the_person(
    api_env, published
) -> None:
    world = await run_world(api_env, "ra-ctl", octet=135, count=1)
    headers, user_id = await _person(api_env, world.org.org_id)
    digest = (await _preview(api_env, headers, world))["list_digest"]
    run_id = (await _start(api_env, headers, world, digest)).json()["id"]

    for verb, state in (("pause", "paused"), ("resume", "running"), ("cancel", "cancelled")):
        response = await api_env.client.post(f"{PUSH}/runs/{run_id}/{verb}", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == state
    again = await api_env.client.post(f"{PUSH}/runs/{run_id}/resume", headers=headers)
    assert again.status_code == 409

    async with AsyncSession(api_env.admin_engine) as session:
        actions = [
            (e.action, e.actor_id)
            for e in (
                await session.execute(
                    select(AuditEvent)
                    .where(
                        AuditEvent.org_id == world.org.org_id,
                        AuditEvent.action.like("push.run.%"),
                    )
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        ]
    assert actions == [
        ("push.run.started", user_id),
        ("push.run.paused", user_id),
        ("push.run.resumed", user_id),
        ("push.run.cancelled", user_id),
    ]


async def test_a_run_stopped_after_sign_in_failures_is_shown_and_a_person_resumes_it(
    api_env, published
) -> None:
    world = await run_world(api_env, "ra-stop", octet=140, count=2)
    headers, user_id = await _person(api_env, world.org.org_id)
    digest = (await _preview(api_env, headers, world))["list_digest"]
    run_id = (await _start(api_env, headers, world, digest)).json()["id"]
    async with AsyncSession(api_env.admin_engine) as session:
        await session.execute(
            text(
                "UPDATE push_run SET state = 'stopped_credential', stopped_at = now()"
                " WHERE id = :r"
            ),
            {"r": UUID(run_id)},
        )
        await session.commit()

    shown = (await api_env.client.get(f"{PUSH}/runs/{run_id}", headers=headers)).json()
    assert shown["state"] == "stopped_credential" and shown["stopped_at"]
    assert shown["auth_failure_limit"] == 3 and shown["signed_in"] is False
    # Stopped is not paused: Pause is refused, Resume is a person's way out.
    paused = await api_env.client.post(f"{PUSH}/runs/{run_id}/pause", headers=headers)
    assert paused.status_code == 409
    resumed = await api_env.client.post(f"{PUSH}/runs/{run_id}/resume", headers=headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "running" and resumed.json()["stopped_at"] is None


async def test_the_run_page_reads_counts_and_a_page_of_targets(api_env, published) -> None:
    world = await run_world(api_env, "ra-read", octet=136, count=3)
    headers, _user = await _person(api_env, world.org.org_id)
    digest = (await _preview(api_env, headers, world))["list_digest"]
    run_id = (await _start(api_env, headers, world, digest)).json()["id"]

    listed = (await api_env.client.get(f"{PUSH}/runs", headers=headers)).json()
    assert [r["id"] for r in listed] == [run_id]
    page = (
        await api_env.client.get(
            f"{PUSH}/runs/{run_id}/targets", params={"offset": 1, "limit": 1}, headers=headers
        )
    ).json()
    assert page["total"] == 3 and [t["position"] for t in page["items"]] == [1]
    # One first (spec §1.4a): position 0 is in flight, position 1 still waits.
    assert page["items"][0]["state"] == "waiting" and page["items"][0]["job_id"] is None
    assert "credential_missing" in page["reason_help"]


async def test_another_orgs_run_is_a_404_on_every_run_route(api_env, published) -> None:
    owner = await run_world(api_env, "ra-own", octet=137, count=1)
    other = await run_world(api_env, "ra-oth", octet=138, count=1)
    owner_headers, _u = await _person(api_env, owner.org.org_id)
    other_headers, _v = await _person(api_env, other.org.org_id)
    digest = (await _preview(api_env, owner_headers, owner))["list_digest"]
    run_id = (await _start(api_env, owner_headers, owner, digest)).json()["id"]

    for method, path in (
        ("GET", f"{PUSH}/runs/{run_id}"),
        ("GET", f"{PUSH}/runs/{run_id}/targets"),
        ("POST", f"{PUSH}/runs/{run_id}/pause"),
        ("POST", f"{PUSH}/runs/{run_id}/resume"),
        ("POST", f"{PUSH}/runs/{run_id}/cancel"),
    ):
        response = await api_env.client.request(method, path, headers=other_headers)
        assert response.status_code == 404, (method, path, response.text)


async def test_a_scope_manager_alone_may_not_touch_a_run(api_env, published) -> None:
    world = await run_world(api_env, "ra-scope", octet=139, count=1)
    headers, _user = await _person(api_env, world.org.org_id, ["agent.push.scope.manage"])
    for method, path in (
        ("GET", f"{PUSH}/runs/prefixes"),
        ("GET", f"{PUSH}/runs"),
        ("POST", f"{PUSH}/runs"),
        ("POST", f"{PUSH}/runs/{uuid4()}/pause"),
    ):
        response = await api_env.client.request(method, path, headers=headers, json={})
        assert response.status_code == 403, (method, path)


def test_no_ai_tool_reaches_a_push_route() -> None:
    """CLAUDE.md 2a(e): never reachable by an AI-driven actor. The AI read
    tools are an explicit registry; none may point under /agent-push."""
    from app.core.ai.tools.registry import REGISTRY

    assert not [t.name for t in REGISTRY.values() if t.path.startswith(PUSH)]


# --------------------------------------------------------------------------- #
# The lockout guard follows the SECRET, not only the window (spec §1.4a)
# --------------------------------------------------------------------------- #


async def _finish_in_flight(api_env, run_id: UUID, outcome: str) -> int:
    """The site server reports `outcome` for every install of this run in flight."""
    async with AsyncSession(api_env.admin_engine) as session:
        job_ids = list(
            (
                await session.execute(
                    select(PushRunTarget.job_id).where(
                        PushRunTarget.run_id == run_id, PushRunTarget.state == "in_flight"
                    )
                )
            ).scalars()
        )
        for job_id in job_ids:
            await session.execute(
                text(
                    "UPDATE jobs SET status = 'succeeded', result = CAST(:r AS jsonb),"
                    " finished_at = now() WHERE id = :j"
                ),
                {"r": json.dumps({"outcome": outcome}), "j": job_id},
            )
        await session.commit()
    return len(job_ids)


async def _advance(api_env, world, run_id: UUID, dispatcher: Any) -> list[Any]:
    async with org_session(
        world.org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        return await runs.advance_run(
            session, world.org.org_id, run_id, dispatcher=dispatcher
        )


async def test_a_secret_changed_mid_run_is_proven_again_by_one_machine_first(
    api_env, published, tmp_path, monkeypatch
) -> None:
    """One-first exists to prove the SECRET before it is fanned out. A sign-in
    proven with the old password says nothing about the new one, so after the
    credential is rotated through the real `PUT /credentials/{id}` the next
    dispatch is ONE again, until the new password has got through."""
    monkeypatch.setattr(runs, "PUSH_RUN_CONCURRENCY", 3)
    world = await run_world(api_env, "ra-rot", octet=143, count=9)
    headers, _user = await _person(api_env, world.org.org_id)
    digest = (await _preview(api_env, headers, world))["list_digest"]
    run_id = UUID((await _start(api_env, headers, world, digest)).json()["id"])
    dispatcher = make_dispatcher(tmp_path, vault=api_env.app.state.vault)

    # The first machine proves the old password; the run opens to three.
    assert await _finish_in_flight(api_env, run_id, "installed") == 1
    assert len(await _advance(api_env, world, run_id, dispatcher)) == 3

    rotated = await api_env.client.put(
        f"/api/v1/credentials/{world.credential_id}",
        json={"name": "push-rotated", "secret": CRED_JSON.replace("hunter2", "hunter3")},
        headers=headers,
    )
    assert rotated.status_code == 200, rotated.text
    assert api_env.app.state.vault.stored == 1

    # Three more sign-ins got through -- with the OLD password. One, not three.
    assert await _finish_in_flight(api_env, run_id, "installed") == 3
    assert len(await _advance(api_env, world, run_id, dispatcher)) == 1
    assert await _advance(api_env, world, run_id, dispatcher) == []

    # The new password got through: now it fans out again.
    assert await _finish_in_flight(api_env, run_id, "installed") == 1
    assert len(await _advance(api_env, world, run_id, dispatcher)) == 3

    # THE FINGERPRINT IS COMPARED, NEVER SHOWN. Two versions were presented;
    # neither is derivable from the secret, and neither leaves the database.
    async with AsyncSession(api_env.admin_engine) as session:
        fingerprints = set(
            (
                await session.execute(
                    select(PushRunTarget.credential_fingerprint).where(
                        PushRunTarget.run_id == run_id,
                        PushRunTarget.credential_fingerprint.is_not(None),
                    )
                )
            ).scalars()
        )
        audits = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.org_id == world.org.org_id)
            )
        ).scalars().all()
    assert len(fingerprints) == 2
    for plaintext in (CRED_JSON, CRED_JSON.replace("hunter2", "hunter3"), "hunter2", "hunter3"):
        assert hashlib.sha256(plaintext.encode()).hexdigest() not in fingerprints
    shown = [
        (await api_env.client.get(f"{PUSH}/runs/{run_id}", headers=headers)).text,
        (await api_env.client.get(f"{PUSH}/runs/{run_id}/targets", headers=headers)).text,
        (await api_env.client.get(f"{PUSH}/runs", headers=headers)).text,
        *(json.dumps(event.meta, default=str) for event in audits),
    ]
    assert not [fp for fp in fingerprints for text_ in shown if fp in text_]
    assert "fingerprint" not in shown[0] and "fingerprint" not in shown[1]


async def test_the_subnet_list_needs_credentials_checkout_as_every_use_of_it_does(
    api_env, published
) -> None:
    """Review item 5. The subnets a run may be built from are only ever used
    to preview and start a run, and both need `credentials.checkout` beside
    `agent.push.run`. The list is held to the same conjunction."""
    world = await run_world(api_env, "ra-pfx", octet=146, count=1)
    admin, _admin_id = await _person(api_env, world.org.org_id)
    runner, _runner_id = await _person(api_env, world.org.org_id, ["agent.push.run"])

    refused = await api_env.client.get(f"{PUSH}/runs/prefixes", headers=runner)
    assert refused.status_code == 403, refused.text
    assert "credentials.checkout" in refused.json()["detail"]
    assert world.prefix not in refused.text

    allowed = await api_env.client.get(f"{PUSH}/runs/prefixes", headers=admin)
    assert allowed.status_code == 200
    assert [p["prefix"] for p in allowed.json()] == [world.prefix]
