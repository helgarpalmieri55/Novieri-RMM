"""The install link's settings API (spec 2026-09-28 §1.1)."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.models import AuditEvent, InstallLink
from tests.install_link_fixtures import switch_on


async def _admin(api_env, org_id):
    sub = await api_env.create_user(org_id, "org-admin")
    return await api_env.headers(org_id, sub)


async def test_the_settings_are_admin_tier(api_env) -> None:
    org_id = await api_env.create_org("il")
    tech = await api_env.headers(org_id, await api_env.create_user(org_id, "technician"))
    assert (await api_env.client.get("/api/v1/install-link", headers=tech)).status_code == 403
    put = await api_env.client.put("/api/v1/install-link", json={"enabled": False}, headers=tech)
    assert put.status_code == 403
    regenerate = await api_env.client.post("/api/v1/install-link/regenerate", headers=tech)
    assert regenerate.status_code == 403


async def test_a_new_org_reads_a_switched_off_link_with_defaults(api_env) -> None:
    org_id = await api_env.create_org("il")
    body = (
        await api_env.client.get("/api/v1/install-link", headers=await _admin(api_env, org_id))
    ).json()
    assert body == {
        "enabled": False,
        "has_code": False,
        "networks": [],
        "disable_at": None,
        "hourly_limit": 50,
        "enabled_at": None,
        "joined_since_enabled": 0,
    }


async def test_it_cannot_be_switched_on_with_no_network(api_env) -> None:
    org_id = await api_env.create_org("il")
    response = await api_env.client.put(
        "/api/v1/install-link",
        json={"enabled": True, "networks": []},
        headers=await _admin(api_env, org_id),
    )
    assert response.status_code == 422
    assert "at least one network" in response.text


async def test_switching_on_mints_a_code_shown_once_and_stored_as_a_hash(api_env) -> None:
    org_id = await api_env.create_org("il")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    assert len(code) == 32 and all(c.isalnum() or c in "-_" for c in code)

    async with AsyncSession(api_env.admin_engine) as session:
        row = (
            await session.execute(select(InstallLink).where(InstallLink.org_id == org_id))
        ).scalar_one()
    assert row.code_hash == hashlib.sha256(code.encode()).hexdigest()
    assert code not in repr(row.__dict__)

    headers = await _admin(api_env, org_id)
    got = (await api_env.client.get("/api/v1/install-link", headers=headers)).json()
    assert got["enabled"] is True and got["has_code"] is True
    assert "url" not in got and code not in str(got)

    again = await api_env.client.put(
        "/api/v1/install-link",
        json={"enabled": True, "networks": ["10.50.0.0/16", "fd00::/8"], "hourly_limit": 20},
        headers=headers,
    )
    assert again.status_code == 200 and again.json()["url"] is None


async def test_the_link_url_uses_the_configured_console(api_env) -> None:
    org_id = await api_env.create_org("il")
    api_env.settings.public_console_url = "https://itms.example.com/"
    headers = await _admin(api_env, org_id)
    response = await api_env.client.put(
        "/api/v1/install-link", json={"enabled": True, "networks": ["10.0.0.0/8"]}, headers=headers
    )
    assert response.json()["url"].startswith("https://itms.example.com/i/")


async def test_regenerate_replaces_the_code(api_env) -> None:
    org_id = await api_env.create_org("il")
    old = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    response = await api_env.client.post(
        "/api/v1/install-link/regenerate", headers=await _admin(api_env, org_id)
    )
    assert response.status_code == 200
    new = response.json()["url"].rsplit("/i/", 1)[1]
    assert new != old
    async with AsyncSession(api_env.admin_engine) as session:
        stored = (
            await session.execute(select(InstallLink.code_hash).where(InstallLink.org_id == org_id))
        ).scalar_one()
    assert stored == hashlib.sha256(new.encode()).hexdigest()


async def test_bad_settings_are_refused_with_a_sentence(api_env) -> None:
    """The console's Install link panel shows a 422's `detail` VERBATIM, so it
    must be one plain sentence -- never Pydantic's list of error objects, and
    never a field's wire name."""
    org_id = await api_env.create_org("il")
    headers = await _admin(api_env, org_id)
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    for body, fragment in [
        ({"enabled": True, "networks": ["10.0.0.1/24"]}, "not a network"),
        ({"enabled": True, "networks": ["10.0.0.0/24"], "disable_at": past}, "future"),
        ({"enabled": True, "networks": ["10.0.0.0/24"], "hourly_limit": 0}, "hourly limit"),
        ({"enabled": True, "networks": ["10.0.0.0/24"], "hourly_limit": 501}, "hourly limit"),
        (
            {"enabled": True, "networks": [f"10.{i}.0.0/16" for i in range(65)]},
            "at most 64 networks",
        ),
        # A time with no zone is refused as input, not compared and 500'd.
        (
            {"enabled": True, "networks": ["10.0.0.0/24"], "disable_at": "2099-01-01T00:00:00"},
            "time zone",
        ),
        # "Anywhere" is not a network; the list is where the link answers.
        ({"enabled": True, "networks": ["0.0.0.0/0"]}, "every address"),
        ({"enabled": True, "networks": ["::/0"]}, "every address"),
        # Behind Docker's userland proxy every client can appear to come from
        # the bridge gateway: a network overlapping the trusted proxies would
        # admit everyone.
        ({"enabled": True, "networks": ["172.19.0.0/16"]}, "proxy"),
        ({"enabled": True, "networks": ["172.19.240.128/25"]}, "proxy"),
    ]:
        response = await api_env.client.put("/api/v1/install-link", json=body, headers=headers)
        assert response.status_code == 422, body
        detail = response.json()["detail"]
        assert isinstance(detail, str), detail
        assert fragment in detail, detail
        assert "hourly_limit" not in detail and "disable_at" not in detail, detail


async def test_every_change_is_audited_and_no_audit_row_carries_the_code(api_env) -> None:
    org_id = await api_env.create_org("il")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    headers = await _admin(api_env, org_id)
    await api_env.client.put(
        "/api/v1/install-link",
        json={"enabled": True, "networks": ["10.60.0.0/16"], "hourly_limit": 10},
        headers=headers,
    )
    regenerated = await api_env.client.post("/api/v1/install-link/regenerate", headers=headers)
    new_code = regenerated.json()["url"].rsplit("/i/", 1)[1]
    await api_env.client.put(
        "/api/v1/install-link",
        json={"enabled": False, "networks": ["10.60.0.0/16"], "hourly_limit": 10},
        headers=headers,
    )
    async with AsyncSession(api_env.admin_engine) as session:
        rows = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.org_id == org_id).order_by(AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )
    actions = [r.action for r in rows if r.action.startswith("enrollment.link")]
    assert actions == [
        "enrollment.link_created",
        "enrollment.link_settings_changed",
        "enrollment.link_enabled",
        "enrollment.link_settings_changed",
        "enrollment.link_regenerated",
        "enrollment.link_disabled",
    ]
    disabled = [r for r in rows if r.action == "enrollment.link_disabled"][0]
    assert disabled.meta["reason"] == "operator"
    for row in rows:
        assert code not in str(row.meta) and new_code not in str(row.meta)


async def test_the_view_counts_machines_that_joined_since_it_was_switched_on(api_env) -> None:
    from app.models import Device

    org_id = await api_env.create_org("il")
    await switch_on(api_env, org_id, ["10.50.0.0/16"])
    async with AsyncSession(api_env.admin_engine) as session:
        session.add_all(
            [
                Device(
                    org_id=org_id,
                    hostname=f"pc{i}",
                    link_joined_at=datetime.now(UTC),
                    link_joined_from="10.50.0.9",
                    review_state="pending",
                )
                for i in range(3)
            ]
        )
        await session.commit()
    body = (
        await api_env.client.get("/api/v1/install-link", headers=await _admin(api_env, org_id))
    ).json()
    assert body["joined_since_enabled"] == 3
    assert body["enabled_at"] is not None


async def test_two_first_saves_at_once_do_not_collide(admin_engine, app_engine) -> None:
    """Two first-ever saves race: neither sees the other's uncommitted row, so
    both INSERT. The INSERT is idempotent (ON CONFLICT DO NOTHING on org_id), so
    the second waits for the first to commit and then locks ITS row -- no
    IntegrityError on `uq_install_links_org`, and only the first mints a code."""
    from app.core.tenancy import org_session
    from app.services import install_link as svc
    from tests.ipam_fixtures import seed_org

    org = await seed_org(admin_engine, "il-race")
    now = datetime.now(UTC)
    kwargs = {
        "org_id": org.org_id,
        "actor_id": org.user_id,
        "enabled": True,
        "networks": ["10.50.0.0/16"],
        "disable_at": None,
        "hourly_limit": 50,
        "now": now,
    }

    async def second() -> str | None:
        async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
            return await svc.save(session, **kwargs)

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        first = await svc.save(session, **kwargs)
        racing = asyncio.create_task(second())
        # Let the second save reach its INSERT and block on the first's row.
        await asyncio.sleep(0.5)
        assert not racing.done()
    minted_second = await racing
    assert first is not None and minted_second is None
    async with AsyncSession(admin_engine) as session:
        rows = (
            (await session.execute(select(InstallLink).where(InstallLink.org_id == org.org_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1 and rows[0].code_hash == hashlib.sha256(first.encode()).hexdigest()


def test_a_hostile_host_header_cannot_reach_the_script() -> None:
    from app.services.install_link import console_base

    assert console_base(None, "itms.example.com:443") == "https://itms.example.com:443"
    assert console_base(None, "evil'; touch /tmp/x; '") == "https://localhost"
    assert console_base(None, "itms.example.com/i/x") == "https://localhost"
    assert console_base("https://itms.example.com/", "ignored") == "https://itms.example.com"


async def test_after_the_automatic_switch_off_the_settings_save_again(api_env) -> None:
    """Final review M2. The beat switched the link off but left the (now past)
    date in place, so every later save echoing it back was refused as "must
    be in the future" until someone cleared the field. The switch-off clears
    the date; the audit row keeps the value it had."""
    from app.core.tenancy import org_session
    from app.services import install_link as svc

    org_id = await api_env.create_org("il")
    when = datetime.now(UTC) + timedelta(days=1)
    await switch_on(api_env, org_id, ["10.50.0.0/16"], disable_at=when.isoformat())
    passed = datetime.now(UTC) - timedelta(minutes=1)
    async with AsyncSession(api_env.admin_engine) as session:
        row = (
            await session.execute(select(InstallLink).where(InstallLink.org_id == org_id))
        ).scalar_one()
        row.disable_at = passed
        await session.commit()

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert await svc.switch_off_due(session, org_id, datetime.now(UTC))

    async with AsyncSession(api_env.admin_engine) as session:
        row = (
            await session.execute(select(InstallLink).where(InstallLink.org_id == org_id))
        ).scalar_one()
        audit = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "enrollment.link_disabled"
                )
            )
        ).scalar_one()
    assert row.enabled is False and row.disable_at is None
    assert audit.meta == {"reason": "scheduled", "disable_at": passed.isoformat()}

    headers = await _admin(api_env, org_id)
    view = (await api_env.client.get("/api/v1/install-link", headers=headers)).json()
    assert view["disable_at"] is None
    saved = await api_env.client.put(
        "/api/v1/install-link",
        json={
            "enabled": view["enabled"],
            "networks": [*view["networks"], "10.60.0.0/16"],
            "disable_at": view["disable_at"],
            "hourly_limit": 20,
        },
        headers=headers,
    )
    assert saved.status_code == 200, saved.text
