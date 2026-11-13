"""The OAuth server's rows in the daily retention sweep (autenticación
propia plan 2026-11-06, Task 13; phase 2's `app/core/auth/retention.py`).

- a code goes ten minutes after it expired, used or not; a live one stays;
- a grant goes a day after it was revoked or lapsed, its refresh tokens with
  it; a live one, and one revoked an hour ago, stay;
- an app unused for 30 days goes, its grants and codes with it; a used one
  stays;
- the sweep's one audit row counts each step (`auth.retention_applied`);
- every step names its organization: on a session RLS does not filter,
  another organization's rows stay.

And what the coordinator's integration notes for Task 13 add, each pair of
the lock map met deterministically (`tests/race_helpers.py`):

- THE SWEEP WAITS ON NO GRANT ROW. Its codes and grants steps lock what they
  delete FOR UPDATE SKIP LOCKED, leaves first: a revocation across two ended
  grants, a refresh holding its token, an exchange holding its spent code and
  an idle app's purge leave it nothing to wait for, so it closes no cycle
  with them; what they hold is left to the next sweep. The other way round
  they may wait on the open sweep, and then find nothing (a refresh:
  `invalid_grant`; a purge: the rest of the app);
- THE APPS STEP IS THE REGISTRATION'S PURGE (`clients.purge_idle`), outside
  the sweep's transaction: a held app is given way on and the rest of the
  sweep goes on, and the step is bounded by the purge's own budget.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import retention
from app.core.auth.guards import lock_user_row
from app.core.auth.oauth import clients, grants
from app.core.auth.oauth.clients import Purged
from app.core.auth.oauth.errors import BAD_REFRESH, OAuthError
from app.core.auth.oauth.store import oauth_session
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent
from app.models.oauth import OAuthClient, OAuthCode, OAuthRefreshFamily, OAuthRefreshToken
from tests.oauth_support import RESOURCE, oauth_org, pkce_pair, seed_client, seed_code
from tests.race_helpers import backend_pid, normalised, until_blocked_by
from tests.session_support import user_id_of
from tests.test_oauth_schema import NO_WAITING

NOW = datetime.now(UTC)
#: Lapsed, unrevoked, two days ago: an ended grant the sweep takes.
LAPSED = {"expires_at": NOW - timedelta(days=2)}


async def _open(
    env: Any, org_id: UUID, user_id: UUID, app: OAuthClient, **ended: Any
) -> tuple[UUID, str]:
    """A grant opened 40 days ago, used today unless `ended` says otherwise;
    its id and its refresh token."""
    opened = NOW - timedelta(days=40)
    async with oauth_session(org_id, env.app_engine) as session:
        family, secret = await grants.open_family(
            session,
            org_id=org_id,
            user_id=user_id,
            oauth_client_id=app.id,
            resource=RESOURCE,
            now=opened,
        )
        values: dict[str, Any] = {"expires_at": NOW + timedelta(days=30), **ended}
        await session.execute(
            update(OAuthRefreshFamily).where(OAuthRefreshFamily.id == family.id).values(**values)
        )
        family_id: UUID = family.id
    return family_id, secret


async def _family(env: Any, org_id: UUID, sub: str, app: OAuthClient, **ended: Any) -> UUID:
    """A grant opened 40 days ago, used today unless `ended` says otherwise."""
    family_id, _ = await _open(env, org_id, await user_id_of(env, sub), app, **ended)
    return family_id


async def _ids(env: Any, model: Any, org_id: UUID) -> set[UUID]:
    async with AsyncSession(env.admin_engine) as session:
        return set(
            (await session.execute(select(model.id).where(model.org_id == org_id))).scalars()
        )


async def _world(env: Any, prefix: str) -> dict[str, Any]:
    org_id, _ = await oauth_org(env, prefix)
    sub = await env.create_user(org_id, "org-admin")
    app = await seed_client(env, org_id)
    idle = await seed_client(env, org_id, last_used_at=NOW - timedelta(days=31))
    _, challenge = pkce_pair()
    await seed_code(env, org_id, sub, app=app, challenge=challenge, now=NOW - timedelta(minutes=12))
    await seed_code(env, org_id, sub, app=app, challenge=challenge, now=NOW)
    await seed_code(env, org_id, sub, app=idle, challenge=challenge, now=NOW)
    revoked = {"revoked_reason": "revoked_in_settings"}
    return {
        "org_id": org_id,
        "app": app.id,
        "idle": idle.id,
        "live": await _family(env, org_id, sub, app),
        "revoked_old": await _family(
            env, org_id, sub, app, revoked_at=NOW - timedelta(days=2), **revoked
        ),
        "revoked_recent": await _family(
            env, org_id, sub, app, revoked_at=NOW - timedelta(hours=1), **revoked
        ),
        "lapsed_old": await _family(env, org_id, sub, app, expires_at=NOW - timedelta(days=2)),
        "of_the_idle_app": await _family(env, org_id, sub, idle),
    }


async def test_the_sweep_removes_what_can_no_longer_serve_anyone(api_env) -> None:
    w = await _world(api_env, "oauth-ret")
    org_id = w["org_id"]

    swept = await retention.sweep_org(org_id, api_env.app_engine, now=NOW)

    # One expired code, two ended grants, one idle app (whose grant and code
    # went with it, uncounted).
    assert swept == 4
    assert await _ids(api_env, OAuthClient, org_id) == {w["app"]}
    assert await _ids(api_env, OAuthRefreshFamily, org_id) == {w["live"], w["revoked_recent"]}
    async with AsyncSession(api_env.admin_engine) as session:
        codes = (
            (await session.execute(select(OAuthCode.expires_at).where(OAuthCode.org_id == org_id)))
            .scalars()
            .all()
        )
        tokens = set(
            (
                await session.execute(
                    select(OAuthRefreshToken.family_id).where(OAuthRefreshToken.org_id == org_id)
                )
            ).scalars()
        )
        (event,) = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id, AuditEvent.action == "auth.retention_applied"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(codes) == 1 and codes[0] > NOW  # the live one; the idle app's went with it
    assert tokens == {w["live"], w["revoked_recent"]}  # the ended grants' tokens cascaded
    assert (event.actor_type, event.actor_id) == ("system", None)
    assert (event.meta["oauth_codes"], event.meta["oauth_grants"], event.meta["oauth_apps"]) == (
        1,
        2,
        1,
    )


async def test_every_oauth_step_names_its_organization(api_env, bypassrls_session) -> None:
    """Phase 2's decision 3: `sweep` takes any session, and on one RLS does
    not filter, the predicate is what keeps one organization's sweep off
    another's rows. The apps step opens its own transactions on the engine it
    is given (`APART_STEPS`): given the BYPASSRLS one, its predicate is what
    keeps it off the other organization's idle app."""
    mine = await _world(api_env, "oauth-ret-a")
    theirs = await _world(api_env, "oauth-ret-b")
    # An ended grant whose tokens an earlier sweep already took (it left the
    # grant, which a held code still named): nothing but its `org_id` keeps
    # another organization's grants step off it.
    async with AsyncSession(api_env.admin_engine) as session:
        await session.execute(
            delete(OAuthRefreshToken).where(OAuthRefreshToken.family_id == theirs["revoked_old"])
        )
        await session.commit()
    before = {
        model: await _ids(api_env, model, theirs["org_id"])
        for model in (OAuthClient, OAuthCode, OAuthRefreshFamily, OAuthRefreshToken)
    }

    swept = await retention.sweep(bypassrls_session, mine["org_id"], now=NOW)
    await bypassrls_session.commit()
    for _name, step in retention.APART_STEPS:
        await step(api_env.admin_engine, mine["org_id"], NOW)

    for model, ids in before.items():
        assert await _ids(api_env, model, theirs["org_id"]) == ids, model
    assert (swept.counts["oauth_codes"], swept.counts["oauth_grants"]) == (1, 2)
    assert mine["idle"] not in await _ids(api_env, OAuthClient, mine["org_id"])


def test_the_oauth_steps_close_the_registries() -> None:
    """Codes and grants close `STEPS`, in the sweep's transaction; the idle
    apps are `APART_STEPS`' one step, outside it (coordinator's notes for
    Task 13: the purge's shape)."""
    assert [name for name, _ in retention.STEPS][-2:] == ["oauth_codes", "oauth_grants"]
    assert [name for name, _ in retention.APART_STEPS] == ["oauth_apps"]
    assert retention.OAUTH_CODE_RETENTION == timedelta(minutes=10)
    assert retention.OAUTH_GRANT_RETENTION == timedelta(days=1)


# --------------------------------------------------------------------------- #
# The lock map (`guards.lock_user_row`): each pair it names, met
# --------------------------------------------------------------------------- #


async def _person(env: Any, prefix: str) -> tuple[UUID, str, UUID, OAuthClient]:
    """An organization, a technician in it (subject and id) and a live app."""
    org_id, _ = await oauth_org(env, prefix)
    sub = await env.create_user(org_id, "org-admin")
    return org_id, sub, await user_id_of(env, sub), await seed_client(env, org_id)


@asynccontextmanager
async def _sweeping(env: Any, org_id: UUID) -> AsyncIterator[AsyncSession]:
    """The sweep's own transaction, as `sweep_org` opens it, held open by the
    test; a statement in it that would wait on a lock fails instead."""
    async with org_session(org_id, engine=env.app_engine, clients=ClientScope.all()) as session:
        await session.execute(text(NO_WAITING))
        yield session


def _verbs(blocked: list[str]) -> list[str]:
    return [normalised(query).split(" ")[0] for query in blocked]


async def test_a_revocation_across_two_ended_grants_meets_the_sweep_without_a_cycle(
    api_env,
) -> None:
    """A revocation (`grants.revoke_all_for_user`: one UPDATE of a person's
    families WHERE `revoked_at IS NULL`) matches a grant that LAPSED
    unrevoked, as the sweep's grants step does. Two of them, the UPDATE
    reaching the second first: a plain DELETE would take the first and wait
    on the second, while the UPDATE, reaching the first, waited on the
    DELETE -- 40P01. The sweep skips the grant the revocation holds and takes
    the other; the revocation that then reaches it waits on the sweep and
    finds it gone."""
    org_id, _sub, user_id, app = await _person(api_env, "oauth-ret-revoke")
    first, _ = await _open(api_env, org_id, user_id, app, **LAPSED)
    second, _ = await _open(api_env, org_id, user_id, app, **LAPSED)
    revoke = update(OAuthRefreshFamily).values(revoked_at=NOW, revoked_reason="disabled")

    async with oauth_session(org_id, api_env.app_engine) as revocation:
        await lock_user_row(revocation, user_id)
        await revocation.execute(revoke.where(OAuthRefreshFamily.id == second))
        async with _sweeping(api_env, org_id) as sweeping:
            swept = await retention.sweep(sweeping, org_id, now=NOW)
            assert swept.counts["oauth_grants"] == 1
            reaching = asyncio.create_task(
                revocation.execute(revoke.where(OAuthRefreshFamily.id == first))
            )
            blocked = await until_blocked_by(api_env.app_engine, await backend_pid(sweeping))
            assert _verbs(blocked) == ["UPDATE"]
        assert (await reaching).rowcount == 0  # the sweep took it
    # Left to the next sweep: still ended.
    assert await _ids(api_env, OAuthRefreshFamily, org_id) == {second}


async def test_a_refresh_holding_its_token_meets_the_sweep_without_a_cycle(api_env) -> None:
    """A refresh presenting an ended grant's token locks the token, then its
    family (`grants._refresh`, step 3: one SELECT ... FOR UPDATE, the
    tokens' table first). A plain DELETE of the family between the two would
    cascade into the token and wait on the refresh, which then waits on the
    family: a cycle. The sweep skips a token another transaction holds, and
    a family any of whose tokens is left, so it waits on neither; the
    refresh then takes the family without waiting."""
    org_id, _sub, user_id, app = await _person(api_env, "oauth-ret-refresh")
    family_id, secret = await _open(api_env, org_id, user_id, app, **LAPSED)

    async with oauth_session(org_id, api_env.app_engine) as refresh:
        await lock_user_row(refresh, user_id)
        assert await clients.hold(refresh, org_id=org_id, app_id=app.id, now=NOW)
        await refresh.execute(
            select(OAuthRefreshToken.id)
            .where(OAuthRefreshToken.token_hash == grants.hash_secret(secret))
            .with_for_update()
        )
        async with _sweeping(api_env, org_id) as sweeping:
            swept = await retention.sweep(sweeping, org_id, now=NOW)
        assert swept.counts["oauth_grants"] == 0
        await refresh.execute(text(NO_WAITING))
        await refresh.execute(
            select(OAuthRefreshFamily.id)
            .where(OAuthRefreshFamily.id == family_id)
            .with_for_update()
        )
    assert await _ids(api_env, OAuthRefreshFamily, org_id) == {family_id}
    assert len(await _ids(api_env, OAuthRefreshToken, org_id)) == 1


async def test_an_exchange_holding_its_spent_code_meets_the_sweep_without_a_cycle(
    api_env,
) -> None:
    """An exchange presenting a spent code locks it, then ends the grant the
    code opened (`grants._exchange`: a reuse, `revoke_family`). The sweep
    skips the code, and a grant a remaining code still names: deleting it
    would empty that code's `family_id` (ON DELETE SET NULL) and wait on the
    exchange, which would then wait on the grant. So the exchange ends the
    grant without waiting, and both are left to the next sweep."""
    org_id, sub, user_id, app = await _person(api_env, "oauth-ret-exchange")
    family_id, _ = await _open(api_env, org_id, user_id, app, **LAPSED)
    _, challenge = pkce_pair()
    opened = NOW - timedelta(days=40)
    code = await seed_code(api_env, org_id, sub, app=app, challenge=challenge, now=opened)
    digest = grants.hash_secret(code)
    async with oauth_session(org_id, api_env.app_engine) as session:
        await session.execute(
            update(OAuthCode)
            .where(OAuthCode.code_hash == digest)
            .values(used_at=opened, family_id=family_id)
        )

    async with oauth_session(org_id, api_env.app_engine) as exchange:
        await lock_user_row(exchange, user_id)
        assert await clients.hold(exchange, org_id=org_id, app_id=app.id, now=NOW)
        await exchange.execute(
            select(OAuthCode.id).where(OAuthCode.code_hash == digest).with_for_update()
        )
        async with _sweeping(api_env, org_id) as sweeping:
            swept = await retention.sweep(sweeping, org_id, now=NOW)
        assert (swept.counts["oauth_codes"], swept.counts["oauth_grants"]) == (0, 0)
        await exchange.execute(text(NO_WAITING))
        assert await grants.revoke_family(exchange, family_id, "code_reused", NOW)
    assert await _ids(api_env, OAuthRefreshFamily, org_id) == {family_id}
    assert len(await _ids(api_env, OAuthCode, org_id)) == 1


async def test_a_refresh_of_a_grant_the_sweep_took_waits_on_it_and_is_refused(api_env) -> None:
    """The other way round, through the token endpoint's own code: a refresh
    of a grant the open sweep has deleted waits on the sweep -- which waits
    on nothing -- and, once it commits, finds no token: `invalid_grant`,
    neither a 500 nor 40P01."""
    org_id, _sub, user_id, app = await _person(api_env, "oauth-ret-late")
    _, secret = await _open(api_env, org_id, user_id, app, **LAPSED)

    async with _sweeping(api_env, org_id) as sweeping:
        swept = await retention.sweep(sweeping, org_id, now=NOW)
        assert swept.counts["oauth_grants"] == 1
        refreshing = asyncio.create_task(
            grants.refresh_grant(
                api_env.app, org_id=org_id, client_id=app.client_id, refresh_token=secret
            )
        )
        blocked = await until_blocked_by(api_env.app_engine, await backend_pid(sweeping))
        assert _verbs(blocked) == ["SELECT"]
    with pytest.raises(OAuthError) as refused:
        await refreshing
    assert refused.value.problem == BAD_REFRESH


async def _idle_with_dead_rows(env: Any, org_id: UUID, sub: str, user_id: UUID) -> OAuthClient:
    """An app idle for 31 days, with a spent code and an ended grant."""
    idle = await seed_client(env, org_id, last_used_at=NOW - timedelta(days=31))
    await _open(env, org_id, user_id, idle, **LAPSED)
    _, challenge = pkce_pair()
    await seed_code(env, org_id, sub, app=idle, challenge=challenge, now=NOW - timedelta(days=31))
    return idle


async def test_the_sweep_waits_on_no_purge_of_an_idle_app(api_env) -> None:
    """A registration's purge of an idle app (`clients._purge_one`: its
    DELETE's cascade holds the app's codes, grants and tokens) runs while the
    sweep does: the sweep skips every row the cascade holds and commits
    without waiting; the purge then commits."""
    org_id, sub, user_id, app = await _person(api_env, "oauth-ret-purging")
    idle = await _idle_with_dead_rows(api_env, org_id, sub, user_id)

    async with oauth_session(org_id, api_env.app_engine) as purge:
        await purge.execute(
            delete(OAuthClient).where(OAuthClient.id == idle.id, clients.idle_at(NOW))
        )
        async with _sweeping(api_env, org_id) as sweeping:
            swept = await retention.sweep(sweeping, org_id, now=NOW)
        assert (swept.counts["oauth_codes"], swept.counts["oauth_grants"]) == (0, 0)
    assert await _ids(api_env, OAuthClient, org_id) == {app.id}
    assert await _ids(api_env, OAuthCode, org_id) == set()
    assert await _ids(api_env, OAuthRefreshFamily, org_id) == set()


async def test_a_purge_of_an_idle_app_may_wait_on_the_sweep_and_then_takes_it(
    api_env, monkeypatch
) -> None:
    """The other way round: the open sweep holds the idle app's spent code and
    ended grant; the purge's cascade waits on the sweep -- at most its own
    bounded wait, lengthened here -- and, once the sweep commits, takes the
    app."""
    monkeypatch.setattr(clients, "purge_lock_wait_ms", lambda _deadlock_ms: 30_000)
    org_id, sub, user_id, _app = await _person(api_env, "oauth-ret-purged")
    idle = await _idle_with_dead_rows(api_env, org_id, sub, user_id)

    async with _sweeping(api_env, org_id) as sweeping:
        swept = await retention.sweep(sweeping, org_id, now=NOW)
        assert (swept.counts["oauth_codes"], swept.counts["oauth_grants"]) == (1, 1)
        purging = asyncio.create_task(
            clients.purge_idle(api_env.app_engine, org_id=org_id, now=NOW)
        )
        blocked = await until_blocked_by(api_env.app_engine, await backend_pid(sweeping))
        assert _verbs(blocked) == ["DELETE"]
    assert await purging == Purged(removed=1, gave_way=0, not_tried=0)
    assert idle.id not in await _ids(api_env, OAuthClient, org_id)


async def test_the_apps_step_gives_way_on_a_held_app_and_the_sweep_goes_on(api_env) -> None:
    """THE APPS STEP RUNS OUTSIDE THE SWEEP'S TRANSACTION, one app per
    transaction (`clients.purge_idle`): an idle app a token request holds
    (`clients.hold`, FOR KEY SHARE) is waited on at most the purge's bounded
    wait and given way on; the grants are still swept, and audited with
    `oauth_apps` 0. The next sweep takes the app."""
    org_id, _sub, user_id, app = await _person(api_env, "oauth-ret-held")
    idle = await seed_client(api_env, org_id, last_used_at=NOW - timedelta(days=31))
    await _open(api_env, org_id, user_id, app, **LAPSED)

    async with oauth_session(org_id, api_env.app_engine) as holding:
        # FOR KEY SHARE, re-checked against a clock 30 days back: held.
        assert await clients.hold(
            holding, org_id=org_id, app_id=idle.id, now=NOW - timedelta(days=30)
        )
        swept = await asyncio.wait_for(
            retention.sweep_org(org_id, api_env.app_engine, now=NOW), timeout=30
        )
    assert swept == 1
    assert idle.id in await _ids(api_env, OAuthClient, org_id)
    assert await retention.sweep_org(org_id, api_env.app_engine, now=NOW) == 1
    assert await _ids(api_env, OAuthClient, org_id) == {app.id}
    async with AsyncSession(api_env.admin_engine) as session:
        events = (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.org_id == org_id, AuditEvent.action == "auth.retention_applied")
                .order_by(AuditEvent.id)
            )
        ).scalars()
        counted = [(event.meta["oauth_grants"], event.meta["oauth_apps"]) for event in events]
    assert counted == [(1, 0), (0, 1)]


async def test_the_apps_step_is_bounded_by_the_purges_budget(api_env, monkeypatch) -> None:
    """The sweep's apps step is the registration's purge, with its batch and
    its budget (`PURGE_BATCH`, `PURGE_BUDGET_S`): once the budget is spent it
    tries no app, and the rest of the sweep goes on."""
    monkeypatch.setattr(clients, "PURGE_BUDGET_S", 0.0)
    org_id, _sub, user_id, app = await _person(api_env, "oauth-ret-budget")
    idle = await seed_client(api_env, org_id, last_used_at=NOW - timedelta(days=31))
    await _open(api_env, org_id, user_id, app, **LAPSED)
    assert await retention.sweep_org(org_id, api_env.app_engine, now=NOW) == 1
    assert idle.id in await _ids(api_env, OAuthClient, org_id)
