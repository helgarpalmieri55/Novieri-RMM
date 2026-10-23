"""The two kernel `approved_action` tables against a real database
(docs/modules/endpoint-health-alerting/spec.md §2.9, §2.10, §8.3).

READ THIS BEFORE ADDING A TEST HERE.

1. The RLS tests are BEHAVIOURAL. A test asserting `relrowsecurity` is true
   passes against a policy whose predicate was deleted, so the enable/force
   check below is paired with cross-org read and insert tests that actually
   move rows.

2. For an `ALL`-command policy Postgres reuses `USING` as `WITH CHECK` when the
   latter is omitted, so DELETING `WITH CHECK` falsifies nothing. The mutations
   that actually falsify these two are `WITH CHECK (true)` for the insert test
   and `USING (true)` for the read test.

3. RLS MASKS APPLICATION PREDICATES. A cross-org test driven through
   `org_session` passes even when a service's own `org_id` predicate has been
   deleted, because RLS rejects first. `get_approved_action`'s explicit
   predicate is therefore pinned on the ADMIN engine, where RLS is not the thing
   doing the rejecting.

4. The autonomy gate is a DATABASE constraint, so every gate test goes in as
   `its_admin` with raw SQL: no Pydantic validator, no service, nothing but
   Postgres left to refuse the row. Each carries a positive control -- the same
   row with one field changed -- so a NOT NULL or FK failure can never be
   mistaken for the gate working.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as sqltext
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.approved_actions import (
    ApprovedAction,
    ApprovedActionScriptExemption,
    get_approved_action,
    get_script_exemption,
)
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session

TABLES = ("approved_action", "approved_action_script_exemption")


class _Fixture:
    def __init__(self, org_id: UUID, user_id: UUID, script_id: UUID) -> None:
        self.org_id = org_id
        self.user_id = user_id
        self.script_id = script_id


async def _seed_org(admin_engine: AsyncEngine, tag: str = "aa") -> _Fixture:
    from app.models import Organization, Script, User

    slug = f"{tag}-{uuid4().hex[:8]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@test.local")
        session.add(user)
        await session.flush()
        script = Script(
            org_id=org.id,
            name=f"restart-{slug}",
            os_family="windows",
            interpreter="powershell",
            body="Restart-Service Spooler",
            version=3,
            created_by=user.id,
        )
        session.add(script)
        await session.commit()
        return _Fixture(org.id, user.id, script.id)


def _action(fx: _Fixture, **overrides: object) -> ApprovedAction:
    kwargs: dict[str, object] = {
        "org_id": fx.org_id,
        "key": f"k-{uuid4().hex[:8]}",
        "name": "Restart the print spooler",
        "description": "",
        "job_type": "inventory.collect",
        "params_template": {},
        "risk_class": "low",
        "destructive": False,
        "max_target_scope": "device",
        "autonomy": "require_approval",
        "personas": ["health"],
        "created_by": fx.user_id,
    }
    kwargs.update(overrides)
    return ApprovedAction(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tenancy (invariant #5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", TABLES)
async def test_rls_is_enabled_and_forced(admin_engine: AsyncEngine, table: str) -> None:
    """The weakest of the tenancy tests, and it is here only as a smoke check --
    the two behavioural ones below are what actually hold. FORCE matters
    separately from ENABLE: without it the table owner bypasses the policy, and
    migrations run as the owner."""
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                sqltext(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = :t"
                ),
                {"t": table},
            )
        ).one()
    assert (row.relrowsecurity, row.relforcerowsecurity) == (True, True)


async def test_approved_action_cross_org_read_returns_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Falsified by `USING (true)`, which would return org A's row here."""
    a = await _seed_org(admin_engine, "aa-read-a")
    b = await _seed_org(admin_engine, "aa-read-b")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(_action(a, key="secret-remediation"))
        await session.commit()

    async with org_session(b.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        rows = (await session.execute(sqltext("SELECT key FROM approved_action"))).all()
    assert [r.key for r in rows] == []

    async with org_session(a.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        rows = (await session.execute(sqltext("SELECT key FROM approved_action"))).all()
    assert [r.key for r in rows] == ["secret-remediation"]


async def test_approved_action_cross_org_insert_is_rejected(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Falsified by `WITH CHECK (true)`, which would let org B write a command
    into org A's fleet."""
    a = await _seed_org(admin_engine, "aa-ins-a")
    b = await _seed_org(admin_engine, "aa-ins-b")
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with org_session(b.org_id, engine=app_engine, clients=ClientScope.all()) as session:
            session.add(_action(a, key="planted"))
            await session.flush()


async def test_exemption_cross_org_read_returns_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    a = await _seed_org(admin_engine, "aa-ex-a")
    b = await _seed_org(admin_engine, "aa-ex-b")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            ApprovedActionScriptExemption(
                org_id=a.org_id,
                script_id=a.script_id,
                script_version=3,
                reason="reviewed by security",
                granted_by=a.user_id,
            )
        )
        await session.commit()

    async with org_session(b.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        rows = (
            await session.execute(
                sqltext("SELECT script_version FROM approved_action_script_exemption")
            )
        ).all()
    assert rows == []

    async with org_session(a.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        rows = (
            await session.execute(
                sqltext("SELECT script_version FROM approved_action_script_exemption")
            )
        ).all()
    assert [r.script_version for r in rows] == [3]


async def test_exemption_cross_org_insert_is_rejected(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A cross-tenant exemption would be a cross-tenant path to `autonomous`
    execution of arbitrary code (§2.10)."""
    a = await _seed_org(admin_engine, "aa-exi-a")
    b = await _seed_org(admin_engine, "aa-exi-b")
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with org_session(b.org_id, engine=app_engine, clients=ClientScope.all()) as session:
            session.add(
                ApprovedActionScriptExemption(
                    org_id=a.org_id,
                    script_id=a.script_id,
                    script_version=3,
                    reason="planted",
                    granted_by=a.user_id,
                )
            )
            await session.flush()


async def test_reads_carry_their_own_org_predicate_on_the_admin_engine(
    admin_engine: AsyncEngine,
) -> None:
    """THE ONE THAT RLS CANNOT MASK.

    Driven through `org_session` this would pass with the `org_id` predicate
    deleted from `get_approved_action`, because RLS would have removed the other
    org's row before the query ran. On the admin engine RLS is bypassed, so the
    predicate is the only thing separating two same-keyed rows: delete it and
    the query matches BOTH and `scalar_one_or_none` raises
    `MultipleResultsFound`.
    """
    a = await _seed_org(admin_engine, "aa-pred-a")
    b = await _seed_org(admin_engine, "aa-pred-b")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(_action(a, key="shared-key", name="belongs to A"))
        session.add(_action(b, key="shared-key", name="belongs to B"))
        session.add(
            ApprovedActionScriptExemption(
                org_id=a.org_id,
                script_id=a.script_id,
                script_version=3,
                reason="A",
                granted_by=a.user_id,
            )
        )
        session.add(
            ApprovedActionScriptExemption(
                org_id=b.org_id,
                script_id=b.script_id,
                script_version=3,
                reason="B",
                granted_by=b.user_id,
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        found = await get_approved_action(session, org_id=a.org_id, key="shared-key")
        assert found is not None
        assert found.name == "belongs to A"

        other = await get_approved_action(session, org_id=b.org_id, key="shared-key")
        assert other is not None
        assert other.name == "belongs to B"

        exemption = await get_script_exemption(
            session, org_id=b.org_id, script_id=b.script_id, script_version=3
        )
        assert exemption is not None
        assert exemption.reason == "B"

        # And the pair is exact: version 2 is a different fact from version 3.
        assert (
            await get_script_exemption(
                session, org_id=b.org_id, script_id=b.script_id, script_version=2
            )
            is None
        )


async def test_approved_action_has_no_global_rows(admin_engine: AsyncEngine) -> None:
    """`org_id` NOT NULL (§2.9). A global approved action would be a
    cross-tenant command path into every fleet, and a nullable `org_id` plus an
    `IS NULL` convention is one forgotten predicate away from being one."""
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                sqltext(
                    "SELECT table_name, is_nullable FROM information_schema.columns "
                    "WHERE table_name IN ('approved_action', "
                    "'approved_action_script_exemption') AND column_name = 'org_id'"
                )
            )
        ).all()
    assert {r.table_name: r.is_nullable for r in rows} == {
        "approved_action": "NO",
        "approved_action_script_exemption": "NO",
    }


# ---------------------------------------------------------------------------
# The autonomy gate (spec §8.3) -- a CHECK, not application logic
# ---------------------------------------------------------------------------

_INSERT_ACTION = sqltext(
    "INSERT INTO approved_action "
    "(id, org_id, key, name, description, job_type, params_template, script_id, "
    " script_version, risk_class, destructive, max_target_scope, autonomy, personas, "
    " cooldown_s, max_per_device_per_day, max_per_org_per_hour, active, created_by, "
    " created_at, updated_at) "
    "VALUES (:id, :org, :key, 'n', '', :job_type, '{}'::jsonb, :script_id, "
    " :script_version, :risk_class, :destructive, :scope, :autonomy, '[\"health\"]'::jsonb, "
    " 3600, 3, 20, true, :u, now(), now())"
)


async def _insert_action_raw(admin_engine: AsyncEngine, fx: _Fixture, **overrides: object) -> str:
    """Insert as `its_admin` and reduce the outcome to a STRING: either the
    constraint that refused it, or the row read back out of the table. Both
    worlds are then comparable, so a deleted CHECK produces a visibly different
    value rather than merely "no exception"."""
    params: dict[str, object] = {
        "id": uuid4(),
        "org": fx.org_id,
        "key": f"k-{uuid4().hex[:8]}",
        "job_type": "inventory.collect",
        "script_id": None,
        "script_version": None,
        "risk_class": "low",
        "destructive": False,
        "scope": "device",
        "autonomy": "require_approval",
        "u": fx.user_id,
    }
    params.update(overrides)
    async with admin_engine.connect() as conn:
        try:
            async with conn.begin():
                await conn.execute(_INSERT_ACTION, params)
        except (IntegrityError, ProgrammingError, DBAPIError) as exc:
            message = str(exc)
            for name in (
                "ck_approved_action_autonomous_is_bounded",
                "ck_approved_action_max_target_scope",
                "ck_approved_action_script_pin",
                "ck_approved_action_risk_class",
                "ck_approved_action_autonomy",
                "uq_approved_action_org_key",
            ):
                if name in message:
                    return f"refused by {name}"
            return f"refused: {message[:200]}"
        stored = (
            await conn.execute(
                sqltext(
                    "SELECT autonomy, destructive, risk_class, max_target_scope "
                    "FROM approved_action WHERE id = :id"
                ),
                {"id": params["id"]},
            )
        ).all()
        return f"ACCEPTED and readable: {stored}"


async def test_a_bounded_autonomous_action_is_accepted(admin_engine: AsyncEngine) -> None:
    """POSITIVE CONTROL for every gate test below. `autonomous` is a legal
    value; the gate refuses only the combinations that make it dangerous. If
    this went red, the gate tests would be passing for the wrong reason."""
    fx = await _seed_org(admin_engine, "aa-gate-ok")
    outcome = await _insert_action_raw(
        admin_engine,
        fx,
        autonomy="autonomous",
        destructive=False,
        risk_class="low",
        scope="device",
    )
    assert outcome.startswith("ACCEPTED and readable:")


async def test_autonomous_plus_destructive_is_refused_by_the_database(
    admin_engine: AsyncEngine,
) -> None:
    """The brief's "nothing destructive auto-fires", as a CHECK. Application
    logic would not survive a future persona, a service refactor, or a direct
    SQL fixup; relaxing a CHECK takes a migration, which a reviewer sees."""
    fx = await _seed_org(admin_engine, "aa-gate-d")
    outcome = await _insert_action_raw(
        admin_engine, fx, autonomy="autonomous", destructive=True
    )
    assert outcome == "refused by ck_approved_action_autonomous_is_bounded"


async def test_autonomous_plus_high_risk_is_refused_by_the_database(
    admin_engine: AsyncEngine,
) -> None:
    fx = await _seed_org(admin_engine, "aa-gate-h")
    outcome = await _insert_action_raw(
        admin_engine, fx, autonomy="autonomous", risk_class="high"
    )
    assert outcome == "refused by ck_approved_action_autonomous_is_bounded"


async def test_a_non_device_target_scope_is_refused(admin_engine: AsyncEngine) -> None:
    """`max_target_scope IN ('device')` in v1.

    NOTE FOR A FUTURE READER: the gate's third clause
    (`max_target_scope <> 'device'`) cannot fire today, because this narrower
    CHECK already forbids the only value that would trigger it. That clause is
    not dead weight -- it is what must already be in place on the day Phase 17
    widens this CHECK by a reviewed migration, so widening the scope does not
    silently re-open autonomous fleet-wide execution. Either constraint
    refusing this row is the correct outcome, which is why the assertion accepts
    both names.
    """
    fx = await _seed_org(admin_engine, "aa-gate-s")
    outcome = await _insert_action_raw(admin_engine, fx, scope="group")
    assert outcome in {
        "refused by ck_approved_action_max_target_scope",
        "refused by ck_approved_action_autonomous_is_bounded",
    }


async def test_require_approval_may_be_destructive_and_high_risk(
    admin_engine: AsyncEngine,
) -> None:
    """The gate is about `autonomous` ONLY. A destructive, high-risk action
    behind a human approval is the normal case -- v1 seeds everything at
    `require_approval` -- and a gate that also refused this would make the
    registry useless and would be turned off."""
    fx = await _seed_org(admin_engine, "aa-gate-ra")
    outcome = await _insert_action_raw(
        admin_engine,
        fx,
        autonomy="require_approval",
        destructive=True,
        risk_class="high",
        job_type="backup.restore",
    )
    assert outcome.startswith("ACCEPTED and readable:")


# ---------------------------------------------------------------------------
# The script pin (spec §2.9)
# ---------------------------------------------------------------------------


async def test_script_run_requires_a_pinned_script_and_version(
    admin_engine: AsyncEngine,
) -> None:
    """Without the pin, approving a script approves its future contents:
    `update_script` bumps `version` in place on the same row and the old body is
    not retained anywhere."""
    fx = await _seed_org(admin_engine, "aa-pin-a")
    assert (
        await _insert_action_raw(
            admin_engine, fx, job_type="script.run", destructive=True
        )
        == "refused by ck_approved_action_script_pin"
    )
    assert (
        await _insert_action_raw(
            admin_engine,
            fx,
            job_type="script.run",
            destructive=True,
            script_id=fx.script_id,
            script_version=None,
        )
        == "refused by ck_approved_action_script_pin"
    )
    assert (
        await _insert_action_raw(
            admin_engine,
            fx,
            job_type="script.run",
            destructive=True,
            script_id=fx.script_id,
            script_version=3,
        )
    ).startswith("ACCEPTED and readable:")


async def test_a_pin_on_a_non_script_job_type_is_refused(admin_engine: AsyncEngine) -> None:
    """Forbidden, not merely ignored: a pin on a `patch.apply` action would read
    as a version guarantee that nothing enforces."""
    fx = await _seed_org(admin_engine, "aa-pin-b")
    outcome = await _insert_action_raw(
        admin_engine,
        fx,
        job_type="inventory.collect",
        script_id=fx.script_id,
        script_version=3,
    )
    assert outcome == "refused by ck_approved_action_script_pin"


# ---------------------------------------------------------------------------
# Uniqueness
# ---------------------------------------------------------------------------


async def test_key_is_unique_per_org_and_free_across_orgs(admin_engine: AsyncEngine) -> None:
    a = await _seed_org(admin_engine, "aa-uq-a")
    b = await _seed_org(admin_engine, "aa-uq-b")
    assert (
        await _insert_action_raw(admin_engine, a, key="restart-spooler")
    ).startswith("ACCEPTED and readable:")
    assert (
        await _insert_action_raw(admin_engine, a, key="restart-spooler")
        == "refused by uq_approved_action_org_key"
    )
    # Another org may use the same key: the registry is org-scoped, and seeds
    # are copied per org (§11.2).
    assert (
        await _insert_action_raw(admin_engine, b, key="restart-spooler")
    ).startswith("ACCEPTED and readable:")


async def test_one_exemption_per_script_version(admin_engine: AsyncEngine) -> None:
    fx = await _seed_org(admin_engine, "aa-uqex")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            ApprovedActionScriptExemption(
                org_id=fx.org_id,
                script_id=fx.script_id,
                script_version=3,
                reason="first",
                granted_by=fx.user_id,
            )
        )
        await session.commit()

    with pytest.raises(IntegrityError):
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            session.add(
                ApprovedActionScriptExemption(
                    org_id=fx.org_id,
                    script_id=fx.script_id,
                    script_version=3,
                    reason="duplicate",
                    granted_by=fx.user_id,
                )
            )
            await session.commit()

    # A DIFFERENT version is a different fact and must be storable -- both
    # halves, or the constraint could be on `(org_id, script_id)` alone and this
    # test would not notice.
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            ApprovedActionScriptExemption(
                org_id=fx.org_id,
                script_id=fx.script_id,
                script_version=4,
                reason="the edited body, re-reviewed",
                granted_by=fx.user_id,
            )
        )
        await session.commit()


async def test_deleting_a_script_cascades_its_exemptions(admin_engine: AsyncEngine) -> None:
    """ON DELETE CASCADE (§2.10): deleting a script must not leave an exemption
    a later script could inherit by id reuse."""
    fx = await _seed_org(admin_engine, "aa-casc")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            ApprovedActionScriptExemption(
                org_id=fx.org_id,
                script_id=fx.script_id,
                script_version=3,
                reason="x",
                granted_by=fx.user_id,
            )
        )
        await session.commit()

    async with admin_engine.begin() as conn:
        await conn.execute(
            sqltext("DELETE FROM scripts WHERE id = :id"), {"id": fx.script_id}
        )
    async with admin_engine.connect() as conn:
        remaining = (
            await conn.execute(
                sqltext(
                    "SELECT id FROM approved_action_script_exemption WHERE script_id = :id"
                ),
                {"id": fx.script_id},
            )
        ).all()
    assert remaining == []
