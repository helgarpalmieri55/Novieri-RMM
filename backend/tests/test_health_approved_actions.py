"""The health module's service layer over the kernel approved-action registry
(`docs/modules/endpoint-health-alerting/spec.md` §8.2, §8.3, §8.4, §11.3).

THE AUTONOMY GATE IS TESTED AT BOTH LAYERS, SEPARATELY. The service refuses
`autonomous` + destructive with a typed 400 naming which clause tripped; the
CHECK refuses it as a fact of the database, tested with RAW SQL ON THE ADMIN
ENGINE so there is no Pydantic model, no service and no ORM default left between
the statement and Postgres. A test that only exercised the service would pass
with the constraint dropped, which is the whole thing §8.3 says takes a
migration and a reviewer.

WHAT THESE FIXTURES HOLD CONSTANT: `inventory.collect` is the default job type
because it is the one approvable type that is neither destructive by name, nor
in need of a script pin, nor carrying a single required params field -- so an
action can legitimately be `autonomous` with an empty `params_template`, and the
refusals below are visibly caused by what each test VARIES (the job type, the
risk class, the exemption, the template, the fuse) rather than by the fixture.
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.approved_actions import SECRET_PARAM_KEY_MARKERS as _SECRET_MARKERS
from app.modules.health.approved_actions import (
    create_approved_action,
    grant_script_exemption,
    revoke_script_exemption,
    update_approved_action,
)
from app.modules.health.errors import HealthValidationError
from tests.health_action_fixtures import seed_script
from tests.health_fixtures import seed_org

pytestmark = pytest.mark.usefixtures("admin_engine")


@pytest.fixture(autouse=True)
def _job_types_registered() -> None:
    """§8.2 validates `job_type` against the LIVE registry, so the registry has
    to be populated in this process.

    `register_worker_job_types()` rather than a hand-written list: that is the
    same call the worker makes, so a job type this module can name is exactly a
    job type a sweep could dispatch. A test that stubbed the registry would let
    an approved action name something the worker cannot run.
    """
    from app.worker import register_worker_job_types

    register_worker_job_types()


def _fields(**kw):
    base = {
        "key": f"act-{uuid4().hex[:8]}",
        "name": "Collect inventory",
        "description": "",
        "job_type": "inventory.collect",
        "params_template": {},
        "risk_class": "low",
        "autonomy": "require_approval",
        "personas": ["health"],
    }
    base.update(kw)
    return base


async def _org(admin_engine):
    return await seed_org(admin_engine, "aa")


# ---------------------------------------------------------------------------
# §8.2's write-time properties
# ---------------------------------------------------------------------------


async def test_an_unregistered_job_type_is_refused_at_write_time(admin_engine) -> None:
    """§8.2: "an approved action naming a job type that does not exist is a
    configuration bug that must surface when someone writes it, NOT at 03:00
    when an alert fires."

    Validated against the LIVE registry, so renaming a job type breaks this
    rather than silently disarming the gate.
    """
    from app.models import ApprovedAction

    org = await _org(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(HealthValidationError, match="not a registered job type"):
            await create_approved_action(
                session, org.org_id, org.user_id, _fields(job_type="service.restart")
            )
        await session.rollback()
    async with AsyncSession(admin_engine) as session:
        rows = (
            await session.execute(
                select(ApprovedAction.id).where(ApprovedAction.org_id == org.org_id)
            )
        ).all()
    assert rows == [], "the refused action must not have been stored"


# FULLY VALID params for each refused job type, not `{}`. This is the point of
# the fixture: an empty template is already refused by the params-model check, so
# a test that used one would pass with the allowlist deleted and prove nothing.
# Each of these passes `get_job_type(...).params_model` and would be SIGNED AND
# DISPATCHED. `net.sweep`'s is the shape that was verified live against the API.
_VALID_PARAMS_FOR_REFUSED_TYPES: dict[str, dict] = {
    "net.sweep": {
        "subnet_id": "0d9d0c9e-1f1a-4b6a-9d4a-6a0d2a5f7c31",
        "sweep_run_id": "6f1f0a1e-2b3c-4d5e-8f90-1a2b3c4d5e6f",
        "targets": ["8.8.8.0/24"],
        "scope_digest": "0" * 64,
    },
    "health.policy.apply": {
        # `checks: []` means "run nothing" and `policy_version: 999999` makes the
        # reconcile sweep believe the device converged: a monitoring kill switch
        # that the device's own agent verifies and applies.
        "policy_version": 999999,
        "tick_interval_s": 3600,
        "posture_interval_s": 900,
        "summary_interval_s": 86400,
        "checks": [],
    },
    "backup.run": {
        "repository_id": "r-1",
        "storage_ref": "s3://bucket/repo",
        "repo_key": "SUPER-SECRET-REPO-KEY",
        "snapshot": "vss",
    },
    "backup.restore": {
        "repository_id": "r-1",
        "storage_ref": "s3://bucket/repo",
        "manifest_ref": "m-1",
        "selection": ["C:\\"],
        "target": {"path": "C:\\restore"},
        "mode": "restore",
        "repo_key": "SUPER-SECRET-REPO-KEY",
    },
    "provision.enroll": {
        "provision_run_id": "p-1",
        "os_family": "windows",
        "hostname": "WS-001",
        "post_config": {},
    },
}


@pytest.mark.parametrize(
    ("job_type", "needle"),
    [
        ("net.sweep", "ipam"),
        ("health.policy.apply", "monitoring kill switch"),
        ("backup.run", "repository encryption key"),
        ("backup.restore", "repository encryption key"),
        ("provision.enroll", "provision_run_id"),
    ],
)
async def test_a_job_type_outside_the_allowlist_cannot_be_stored_at_all(
    admin_engine, job_type, needle
) -> None:
    """Being REGISTERED means an agent can run the job type. It does not mean a
    machine may decide to, and this facility reaches `JobEngine.create` with the
    operator's own frozen template and nothing else.

    `net.sweep` is the case that was verified live against the API: an approved
    action naming it was accepted (201, `destructive: false`,
    `autonomy: "autonomous"`) and dispatched with ZERO `ipam_scan_authorization`
    rows in the org -- no `ipam.scope.manage` check, no `ipam_sweep_run` row, no
    `ipam.sweep_dispatched` audit event, and the kernel's 3600s job TTL instead
    of ipam's 600s clamp. The result handler rejects the document afterwards
    because `sweep_run_id` does not resolve, but the packets are already on
    somebody else's wire. `app/modules/ipam/authorization.py` says "nothing in
    this repository may dispatch a sweep without going through it"; this test is
    what makes that sentence true.

    THE TEMPLATES ARE FULLY VALID, WHICH IS THE WHOLE FIXTURE. An empty
    `params_template` is already refused by the params-model check, so a test
    that used one would pass with the allowlist deleted. Every template here
    passes the job type's own params model and would be signed and sent.

    EACH TYPE IS PINNED SEPARATELY AND ON THE REASON IT NAMES, not on a shared
    "refused": a parametrisation that only asserted the raise would pass with
    the allowlist replaced by a denylist of one, which is the shape that put
    these here.

    ASSERTED ON THE STORED ROWS TOO. The refusal is only worth anything if no
    row exists afterwards -- a stored row is what the sweep reads.
    """
    from app.models import ApprovedAction

    org = await _org(admin_engine)
    fields = _fields(
        job_type=job_type, params_template=_VALID_PARAMS_FOR_REFUSED_TYPES[job_type]
    )
    raised: Exception | None = None
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        try:
            await create_approved_action(session, org.org_id, org.user_id, dict(fields))
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - inspected below
            raised = exc
            await session.rollback()

    async with AsyncSession(admin_engine) as session:
        stored = list(
            (
                await session.execute(
                    select(ApprovedAction.job_type, ApprovedAction.autonomy).where(
                        ApprovedAction.org_id == org.org_id
                    )
                )
            ).all()
        )
    assert stored == [], (
        f"an approved action naming {job_type} was STORED with params its job type "
        f"accepts; the sweep signs and dispatches that row. Rows: {stored}"
    )
    assert isinstance(raised, HealthValidationError), (
        f"{job_type} must not be nameable by an approved action; got {raised!r}"
    )
    assert "may not be named by an approved action" in str(raised), (
        f"the refusal must say the job type is not approvable at all, not merely that "
        f"something about this particular row is wrong; got {raised!r}"
    )
    assert needle in str(raised), (
        f"the refusal for {job_type} must name the authorization it protects, or an "
        f"operator reads it as an arbitrary 'no'; got {raised!r}"
    )


async def test_destructive_is_derived_and_a_request_body_cannot_set_it(
    admin_engine,
) -> None:
    """§8.4: "derived and stored, never accepted from a request body."

    TWO ASSERTIONS, and the second is the one that matters. Sending
    `destructive` is REFUSED rather than ignored -- silently dropping it returns
    200 for a body that changed nothing, and the caller then believes they set
    it. And the STORED value for a `patch.apply` action is `True` even though
    nobody asked for it, which is `is_destructive` clause 1 doing the deriving.
    """
    from app.models import ApprovedAction

    org = await _org(admin_engine)
    # THE SMUGGLING ATTEMPT, ASSERTED ON THE STORED ROW. `destructive: false` on
    # a `patch.apply` is the attack: paired with `autonomy: autonomous` it
    # would walk straight past the CHECK, which reads the STORED column. So the
    # row is read back and its derived value asserted -- a bare `pytest.raises`
    # here prints only "DID NOT RAISE" on mutation and never shows that a
    # caller's `false` became the stored truth.
    #
    # `patch.apply` rather than `backup.restore`: both derive True by clause 1,
    # and `backup.restore` is no longer nameable by an approved action at all
    # (its params carry a repository encryption key), so it would be refused for
    # a reason that has nothing to do with what this test is about.
    destructive_type = {
        "job_type": "patch.apply",
        "params_template": {"patches": [], "reboot": "never"},
    }
    smuggled = f"smuggle-{uuid4().hex[:8]}"
    raised: Exception | None = None
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        try:
            await create_approved_action(
                session,
                org.org_id,
                org.user_id,
                _fields(key=smuggled, destructive=False, **destructive_type),
            )
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - the type is asserted below
            raised = exc
            await session.rollback()

    async with AsyncSession(admin_engine) as session:
        stored_smuggle = (
            await session.execute(
                select(ApprovedAction.destructive).where(
                    ApprovedAction.org_id == org.org_id, ApprovedAction.key == smuggled
                )
            )
        ).scalar_one_or_none()
    assert stored_smuggle in (None, True), (
        "a request body's `destructive: false` reached the stored column on a "
        "patch.apply action; the autonomy CHECK reads that column"
    )
    assert isinstance(raised, HealthValidationError), (
        f"sending `destructive` must be REFUSED, not ignored -- silently dropping it "
        f"returns 200 for a body that changed nothing; got {raised!r}"
    )

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        action = await create_approved_action(
            session,
            org.org_id,
            org.user_id,
            _fields(autonomy="require_approval", **destructive_type),
        )
        await session.commit()
        action_id = action.id

    async with AsyncSession(admin_engine) as session:
        stored = (
            await session.execute(
                select(ApprovedAction.destructive).where(ApprovedAction.id == action_id)
            )
        ).scalar_one()
    assert stored is True


async def test_script_run_requires_a_pin_and_other_types_refuse_one(admin_engine) -> None:
    """§8.2: "without the version, approving a script approves its future
    contents." Both directions, because the CHECK is an equivalence
    (`(job_type = 'script.run') = (script_id IS NOT NULL AND script_version IS
    NOT NULL)`) and a one-directional test cannot see the other half."""
    org = await _org(admin_engine)
    script_id = await seed_script(admin_engine, org.org_id, org.user_id)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(HealthValidationError, match="must pin both"):
            await create_approved_action(
                session, org.org_id, org.user_id, _fields(job_type="script.run")
            )
        await session.rollback()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(HealthValidationError, match="only valid for"):
            await create_approved_action(
                session,
                org.org_id,
                org.user_id,
                _fields(script_id=script_id, script_version=1),
            )
        await session.rollback()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        action = await create_approved_action(
            session,
            org.org_id,
            org.user_id,
            _fields(job_type="script.run", script_id=script_id, script_version=1),
        )
        await session.commit()
        assert action.destructive is True, "arbitrary code is destructive by default"


# ---------------------------------------------------------------------------
# §8.3's autonomy gate, at BOTH layers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        (
            {"job_type": "patch.apply", "params_template": {"patches": [], "reboot": "never"}},
            "destructive",
        ),
        ({"risk_class": "high"}, "risk_class"),
    ],
)
async def test_the_service_refuses_autonomous_and_names_the_clause(
    admin_engine, overrides, needle
) -> None:
    """Each clause separately: a single "it refused" test cannot tell which of
    the three tripped, and a clause that never fires is a clause that can be
    deleted unnoticed."""
    from app.models import ApprovedAction

    org = await _org(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(HealthValidationError, match=needle):
            await create_approved_action(
                session, org.org_id, org.user_id, _fields(autonomy="autonomous", **overrides)
            )
        await session.rollback()
    async with AsyncSession(admin_engine) as session:
        assert (
            await session.execute(
                select(ApprovedAction.id).where(ApprovedAction.org_id == org.org_id)
            )
        ).all() == []


async def test_postgres_refuses_autonomous_plus_destructive_with_no_service_involved(
    admin_engine,
) -> None:
    """`ck_approved_action_autonomous_is_bounded`, RAW SQL, ADMIN ENGINE.

    §8.3: "a future persona cannot relax it in application code; it takes a
    migration, which a reviewer sees." A test that went through the service
    would pass with the constraint dropped.

    GUARD OBSERVABILITY: without the CHECK this observes a STORED row with
    `autonomy='autonomous', destructive=true` -- a row you can then SELECT back
    and that the sweep would dispatch with no human. The positive control below
    is the same statement with `destructive = false`, which succeeds, so the
    rejection is the GATE and not the statement.
    """
    from app.models import ApprovedAction

    org = await _org(admin_engine)
    insert = text(
        "INSERT INTO approved_action (id, org_id, key, name, description, job_type, "
        "params_template, risk_class, destructive, max_target_scope, autonomy, personas, "
        "cooldown_s, max_per_device_per_day, max_per_org_per_hour, active, created_by, "
        "created_at, updated_at) VALUES (gen_random_uuid(), :org, :key, 'n', '', "
        "'inventory.collect', '{}'::jsonb, 'low', :destructive, 'device', 'autonomous', "
        "'[\"health\"]'::jsonb, 0, 3, 20, true, :user, now(), now())"
    )
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                insert, {"org": org.org_id, "user": org.user_id, "key": "boom", "destructive": True}
            )
        await session.rollback()

    async with AsyncSession(admin_engine) as session:
        await session.execute(
            insert, {"org": org.org_id, "user": org.user_id, "key": "fine", "destructive": False}
        )
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        keys = list(
            (
                await session.execute(
                    select(ApprovedAction.key).where(ApprovedAction.org_id == org.org_id)
                )
            )
            .scalars()
            .all()
        )
    assert keys == ["fine"], "only the non-destructive autonomous row may exist"


@pytest.mark.parametrize(
    ("column", "value", "needle"),
    [
        ("max_per_org_per_hour", 1_000_000, "at most 500"),
        ("max_per_device_per_day", 1_000_000, "at most 100"),
        ("cooldown_s", 10**12, "at most 2592000"),
    ],
)
async def test_the_rate_fuses_are_bounded_above_and_not_only_below(
    admin_engine, column, value, needle
) -> None:
    """§8.4's "fleet-wide is structurally impossible" was resting on an unbounded
    operator integer.

    `_dispatch_remediation` takes a single device `UUID`, which makes ONE JOB
    device-scoped and says nothing about HOW MANY. `max_per_org_per_hour =
    1_000_000` was storable -- both the CHECK (`> 0`) and the service (`<= 0`)
    tested only the floor -- and the sweep dispatches up to 500 rows every 30s,
    so that is 60,000 machine-decided jobs an hour for one tenant with the
    structural claim still technically true.

    EACH FUSE SEPARATELY, and each asserted on the message rather than on "it
    raised": a shared assertion would pass with two of the three ceilings
    deleted.
    """
    from app.models import ApprovedAction

    org = await _org(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        raised: Exception | None = None
        try:
            await create_approved_action(
                session, org.org_id, org.user_id, _fields(**{column: value})
            )
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - asserted below
            raised = exc
            await session.rollback()
    assert isinstance(raised, HealthValidationError), (
        f"{column}={value} was accepted; the fuse an operator sets is the only thing "
        f"bounding how much of a fleet a machine may reach. Got {raised!r}"
    )
    assert needle in str(raised)

    async with AsyncSession(admin_engine) as session:
        assert (
            await session.execute(
                select(ApprovedAction.id).where(ApprovedAction.org_id == org.org_id)
            )
        ).all() == []


async def test_postgres_refuses_an_out_of_range_fuse_with_no_service_involved(
    admin_engine,
) -> None:
    """`ck_approved_action_fuses_bounded`, RAW SQL, ADMIN ENGINE -- the same
    shape the autonomy gate is pinned with, and for the same reason: a test that
    went through the service would pass with the constraint dropped, and the
    constraint is what makes relaxing this take a migration a reviewer sees.

    THE POSITIVE CONTROL IS THE SAME STATEMENT AT THE CEILING, so the rejection
    is the BOUND and not the statement.
    """
    from app.models import ApprovedAction

    org = await _org(admin_engine)
    insert = text(
        "INSERT INTO approved_action (id, org_id, key, name, description, job_type, "
        "params_template, risk_class, destructive, max_target_scope, autonomy, personas, "
        "cooldown_s, max_per_device_per_day, max_per_org_per_hour, active, created_by, "
        "created_at, updated_at) VALUES (gen_random_uuid(), :org, :key, 'n', '', "
        "'inventory.collect', '{}'::jsonb, 'low', false, 'device', 'require_approval', "
        "'[\"health\"]'::jsonb, 0, 3, :per_org, true, :user, now(), now())"
    )
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                insert,
                {"org": org.org_id, "user": org.user_id, "key": "boom", "per_org": 1_000_000},
            )
        await session.rollback()

    async with AsyncSession(admin_engine) as session:
        await session.execute(
            insert, {"org": org.org_id, "user": org.user_id, "key": "fine", "per_org": 500}
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        keys = list(
            (
                await session.execute(
                    select(ApprovedAction.key).where(ApprovedAction.org_id == org.org_id)
                )
            )
            .scalars()
            .all()
        )
    assert keys == ["fine"], "only the row inside the ceiling may exist"


async def test_an_autonomy_change_is_audited_with_its_old_and_new_value(
    admin_engine,
) -> None:
    """§11.3. "Who set this action to autonomous" is only answerable if the
    PREVIOUS value is in the audit row -- the new one is already in the table,
    so a record carrying only the new value answers a question nobody asked."""
    from app.models import AuditEvent

    org = await _org(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        action = await create_approved_action(
            session, org.org_id, org.user_id, _fields(autonomy="suggest")
        )
        await session.commit()
        action_id = action.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await update_approved_action(
            session, org.org_id, action_id, org.user_id, {"autonomy": "autonomous"}
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        record = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org.org_id,
                    AuditEvent.action == "health.action_autonomy_changed",
                )
            )
        ).scalar_one()
    assert record.meta["previous"] == "suggest"
    assert record.meta["new"] == "autonomous"


# ---------------------------------------------------------------------------
# §8.4 clause 2 -- the script exemption, keyed to the exact version
# ---------------------------------------------------------------------------


async def test_granting_an_exemption_makes_the_action_non_destructive(
    admin_engine,
) -> None:
    """§8.4 clause 2, end to end: the stored `destructive` is RE-DERIVED, which
    is what makes the exemption mean anything -- a stored column that disagrees
    with the derivation is exactly what §8.4 exists to make impossible.

    The audit record carries `(script_id, script_version)` per §11.3, because an
    exemption outlives the action that motivated it.
    """
    from app.models import ApprovedAction, AuditEvent

    org = await _org(admin_engine)
    script_id = await seed_script(admin_engine, org.org_id, org.user_id)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        action = await create_approved_action(
            session,
            org.org_id,
            org.user_id,
            _fields(job_type="script.run", script_id=script_id, script_version=1),
        )
        await session.commit()
        action_id = action.id
        assert action.destructive is True

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await grant_script_exemption(
            session, org.org_id, action_id, actor_id=org.user_id, reason="reviewed line by line"
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        stored = (
            await session.execute(
                select(ApprovedAction.destructive).where(ApprovedAction.id == action_id)
            )
        ).scalar_one()
        record = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org.org_id,
                    AuditEvent.action == "health.action.destructive_exempted",
                )
            )
        ).scalar_one()
    assert stored is False
    assert record.meta["script_id"] == str(script_id)
    assert record.meta["script_version"] == 1


async def test_an_exemption_is_refused_once_the_script_has_been_edited(
    admin_engine,
) -> None:
    """§10: refused "when the action's pinned `script_version` no longer matches
    the live `scripts.version` -- exempting a version that has already been
    edited away is a no-op that READS AS A GRANT", which is the worst of both.
    """
    from app.models import ApprovedActionScriptExemption, Script

    org = await _org(admin_engine)
    script_id = await seed_script(admin_engine, org.org_id, org.user_id)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        action = await create_approved_action(
            session,
            org.org_id,
            org.user_id,
            _fields(job_type="script.run", script_id=script_id, script_version=1),
        )
        await session.commit()
        action_id = action.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        script = (
            await session.execute(select(Script).where(Script.id == script_id))
        ).scalar_one()
        script.version = 2
        session.add(script)
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(HealthValidationError, match="edited away"):
            await grant_script_exemption(
                session, org.org_id, action_id, actor_id=org.user_id
            )
        await session.rollback()

    async with AsyncSession(admin_engine) as session:
        rows = (
            await session.execute(
                select(ApprovedActionScriptExemption.id).where(
                    ApprovedActionScriptExemption.org_id == org.org_id
                )
            )
        ).all()
    assert rows == [], "no exemption row may exist for a version nobody can run"


async def test_revoking_an_exemption_re_derives_destructive_and_demotes_autonomy(
    admin_engine,
) -> None:
    """The revoke path. An exemption removed WITHOUT re-deriving leaves the
    action stored as non-destructive and therefore still eligible for
    `autonomous` -- a standing grant nobody can see, which is the failure §2.10
    says visibility is the mitigation for."""
    from app.models import ApprovedAction

    org = await _org(admin_engine)
    script_id = await seed_script(admin_engine, org.org_id, org.user_id)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        action = await create_approved_action(
            session,
            org.org_id,
            org.user_id,
            _fields(job_type="script.run", script_id=script_id, script_version=1),
        )
        await session.commit()
        action_id = action.id
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await grant_script_exemption(session, org.org_id, action_id, actor_id=org.user_id)
        await update_approved_action(
            session, org.org_id, action_id, org.user_id, {"autonomy": "autonomous"}
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        assert (
            await session.execute(
                select(ApprovedAction.autonomy).where(ApprovedAction.id == action_id)
            )
        ).scalar_one() == "autonomous"

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await revoke_script_exemption(session, org.org_id, action_id, actor_id=org.user_id)
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(ApprovedAction.destructive, ApprovedAction.autonomy).where(
                    ApprovedAction.id == action_id
                )
            )
        ).one()
    assert row == (True, "require_approval"), (
        "revoking the grant must make the action destructive again AND demote it, or the "
        "CHECK would refuse the flush and the revoke would be a 500"
    )


async def test_granting_a_shared_exemption_re_derives_every_action_pinning_the_pair(
    admin_engine,
) -> None:
    """The exemption is keyed `(script_id, script_version)` and is EXPLICITLY
    SHARED (§2.10: "two approved actions naming the same pinned version share
    one exemption rather than needing two identical grants").

    THE SIBLING IS ASSERTED, NOT THE ACTION THE CALL NAMES. A test that only
    read back the granted action passes with the sweep over the pair deleted,
    because that action is re-derived either way -- it is the OTHER action, the
    one nobody mentioned, whose stored column is the finding.
    """
    from app.models import ApprovedAction

    org = await _org(admin_engine)
    script_id = await seed_script(admin_engine, org.org_id, org.user_id)
    pin = {"job_type": "script.run", "script_id": script_id, "script_version": 1}
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        granted = await create_approved_action(session, org.org_id, org.user_id, _fields(**pin))
        sibling = await create_approved_action(session, org.org_id, org.user_id, _fields(**pin))
        await session.commit()
        granted_id, sibling_id = granted.id, sibling.id
        assert (granted.destructive, sibling.destructive) == (True, True)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await grant_script_exemption(session, org.org_id, granted_id, actor_id=org.user_id)
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        stored = dict(
            (
                await session.execute(
                    select(ApprovedAction.id, ApprovedAction.destructive).where(
                        ApprovedAction.id.in_([granted_id, sibling_id])
                    )
                )
            ).all()
        )
    assert stored[sibling_id] is False, (
        "the exemption is shared by construction, so the sibling pinning the same "
        f"(script_id, script_version) must be re-derived too; it is stored "
        f"destructive={stored[sibling_id]} with a grant standing for its exact body"
    )
    assert stored[granted_id] is False


async def test_revoking_a_shared_exemption_re_derives_and_demotes_every_sibling(
    admin_engine,
) -> None:
    """THE FINDING, in its worst direction. The revoke deletes ONE SHARED ROW.

    An implementation that re-derives only the action the call names leaves the
    sibling stored `destructive=False, autonomy='autonomous'` -- an
    arbitrary-code `script.run` with NO exemption behind it, which the sweep
    signs and dispatches as SYSTEM/root with no human. Nothing downstream
    notices: `ck_approved_action_autonomous_is_bounded` reads the stored column,
    and the dispatcher branches on `autonomy` and never reads `destructive`.

    BOTH COLUMNS ARE ASSERTED SEPARATELY. `destructive` alone would pass with
    the demotion deleted, and the CHECK would then turn the next write to that
    row into a 500 instead of a refusal; `autonomy` alone would pass with the
    derivation deleted.
    """
    from app.models import ApprovedAction

    org = await _org(admin_engine)
    script_id = await seed_script(admin_engine, org.org_id, org.user_id)
    pin = {"job_type": "script.run", "script_id": script_id, "script_version": 1}
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        revoked_through = await create_approved_action(
            session, org.org_id, org.user_id, _fields(**pin)
        )
        sibling = await create_approved_action(session, org.org_id, org.user_id, _fields(**pin))
        await session.commit()
        revoked_id, sibling_id = revoked_through.id, sibling.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await grant_script_exemption(session, org.org_id, revoked_id, actor_id=org.user_id)
        await update_approved_action(
            session, org.org_id, sibling_id, org.user_id, {"autonomy": "autonomous"}
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        assert (
            await session.execute(
                select(ApprovedAction.destructive, ApprovedAction.autonomy).where(
                    ApprovedAction.id == sibling_id
                )
            )
        ).one() == (False, "autonomous"), "the sibling must start where the finding starts"

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await revoke_script_exemption(session, org.org_id, revoked_id, actor_id=org.user_id)
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(ApprovedAction.destructive, ApprovedAction.autonomy).where(
                    ApprovedAction.id == sibling_id
                )
            )
        ).one()
    assert row == (True, "require_approval"), (
        "the shared exemption is gone, so the sibling is arbitrary code with nothing "
        f"backing it; it is stored destructive={row[0]} autonomy={row[1]!r}, which the "
        "sweep dispatches as SYSTEM/root with no human"
    )


async def test_every_action_the_exemption_touched_gets_its_own_audit_record(
    admin_engine,
) -> None:
    """§11.3. "Which approved actions did this revoke make destructive again" is
    only answerable if each of them is in the log -- one record naming the
    action the caller happened to call the endpoint through answers a question
    nobody asked."""
    from app.models import AuditEvent

    org = await _org(admin_engine)
    script_id = await seed_script(admin_engine, org.org_id, org.user_id)
    pin = {"job_type": "script.run", "script_id": script_id, "script_version": 1}
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        first = await create_approved_action(session, org.org_id, org.user_id, _fields(**pin))
        second = await create_approved_action(session, org.org_id, org.user_id, _fields(**pin))
        await session.commit()
        ids = {str(first.id), str(second.id)}

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await grant_script_exemption(session, org.org_id, first.id, actor_id=org.user_id)
        await revoke_script_exemption(session, org.org_id, first.id, actor_id=org.user_id)
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        records = list(
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org.org_id,
                        AuditEvent.action == "health.action.destructive_exemption_revoked",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert {record.target_id for record in records} == ids, (
        "both actions pinning the pair were re-derived, so both must be in the audit log"
    )


async def test_a_params_template_with_an_unrecognised_brace_token_is_a_400(
    admin_engine,
) -> None:
    """§8.2's third reason for refusing `str.format`: "it fails at the WRONG
    TIME -- `KeyError` at dispatch, 03:00, alert already open -- where a
    structural walk raises at write time and the API returns 400."

    Rendered here against placeholder values so the failure is a write-time 400.
    """
    from app.models import ApprovedAction

    org = await _org(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(HealthValidationError, match="brace token"):
            await create_approved_action(
                session,
                org.org_id,
                org.user_id,
                _fields(params_template={"note": "{whatever}"}),
            )
        await session.rollback()
    async with AsyncSession(admin_engine) as session:
        assert (
            await session.execute(
                select(ApprovedAction.id).where(ApprovedAction.org_id == org.org_id)
            )
        ).all() == []


async def test_a_params_template_the_job_type_refuses_is_a_400_and_never_stored(
    admin_engine,
) -> None:
    """§8.2's posture applied to the params themselves: "a configuration bug
    that must surface when someone writes it, not at 03:00".

    THE FAILURE THIS PREVENTS IS ORG-WIDE, NOT PER-ACTION. `JobEngine.create`
    raises a bare `ValueError` for params its model refuses --
    `execute_pending_actions` does not catch it, so it escapes into
    `sweep._for_each_org`'s broad `except Exception`, which rolls back the whole
    org's transaction and skips its cycle. One badly written approved action
    stops every remediation, ticket and notify for that tenant, every 30s,
    indefinitely.

    BOTH SHAPES ARE PINNED, because they fail differently and a single case
    would let half the check be deleted:

    * a key the model does not have (`patch.scan` has NO fields and forbids
      extras) -- the shape reproduced in the report;
    * a REQUIRED key that is missing (`patch.apply.patches`) -- which renders
      and reads perfectly well and is caught only by the model.
    """
    from app.models import ApprovedAction

    org = await _org(admin_engine)
    cases = [
        ("patch.scan", {"targets": ["everything"]}),
        ("patch.apply", {"reboot": "never"}),
    ]
    for job_type, template in cases:
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            raised: Exception | None = None
            try:
                await create_approved_action(
                    session,
                    org.org_id,
                    org.user_id,
                    _fields(job_type=job_type, params_template=template),
                )
                await session.commit()
            except Exception as exc:  # noqa: BLE001 - the type is asserted below
                raised = exc
                await session.rollback()
        assert isinstance(raised, HealthValidationError), (
            f"{job_type} with params_template={template} was ACCEPTED at write time; "
            f"it becomes a ValueError inside the org's sweep transaction instead. Got "
            f"{raised!r}"
        )
        assert "params_template is not valid" in str(raised)

    async with AsyncSession(admin_engine) as session:
        assert (
            await session.execute(
                select(ApprovedAction.id).where(ApprovedAction.org_id == org.org_id)
            )
        ).all() == [], "a poison-pill row must not exist for the sweep to find"


_DEPLOY = {"action": "install", "installer_type": "msi"}


@pytest.mark.parametrize(
    ("job_type", "template"),
    [
        ("inventory.collect", {"repo_key": "SUPER-SECRET-REPO-KEY"}),
        ("inventory.collect", {"api_token": "t0ken"}),
        # THESE TWO ARE THE ONES THAT WOULD OTHERWISE BE STORED. `app.deploy` is
        # approvable and its `detection_rule`/`uninstall_rule` are free-form
        # `dict[str, Any]`, so the params model accepts a credential nested
        # inside them and only this check refuses it.
        ("app.deploy", {**_DEPLOY, "detection_rule": {"admin_password": "hunter2"}}),
        ("app.deploy", {**_DEPLOY, "uninstall_rule": {"keys": [{"api_key": "k"}]}}),
    ],
    ids=["top-level", "token", "nested-in-a-free-form-dict", "nested-inside-a-list"],
)
async def test_a_params_template_naming_a_credential_is_refused(
    admin_engine, job_type, template
) -> None:
    """Invariant #3: stored credentials live encrypted in the vault and are
    decrypted only at point of use -- never in the application database in
    plaintext, never returned in an API response.

    A `params_template` is BOTH of those at once: plaintext JSONB on a config
    row, returned verbatim by `_approved_action_out` to every holder of
    `health.read`. `BackupRunParams.repo_key` is a REQUIRED `str`, so a
    dispatchable `backup.run` approved action had no way to exist WITHOUT the
    repository encryption key in the clear.

    THE NESTED CASES ARE NOT PADDING, and they are the ones that would actually
    be STORED without this check. `app.deploy` is approvable, and its
    `detection_rule`/`uninstall_rule` are free-form `dict[str, Any]` -- so the
    params model accepts a credential nested inside one, and a top-level-only
    scan would too. The list case is the same argument one level further: a walk
    that recursed into dicts but not lists still misses it.

    THE STORED ROWS ARE ASSERTED, not just the raise. A refusal that still left
    the row behind would be worse than no refusal, because the operator would
    believe the secret was rejected.
    """
    from app.models import ApprovedAction

    org = await _org(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        raised: Exception | None = None
        try:
            await create_approved_action(
                session,
                org.org_id,
                org.user_id,
                _fields(job_type=job_type, params_template=template),
            )
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - the message is asserted below
            raised = exc
            await session.rollback()

    async with AsyncSession(admin_engine) as session:
        stored = list(
            (
                await session.execute(
                    select(ApprovedAction.params_template).where(
                        ApprovedAction.org_id == org.org_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert stored == [], (
        f"{template} was STORED for {job_type}; the secret is now plaintext JSONB on a "
        f"config row that every health.read holder can read back out of the API. Rows: "
        f"{stored}"
    )
    assert isinstance(raised, HealthValidationError), (
        f"{template} was accepted for {job_type}. Got {raised!r}"
    )
    assert "may not carry a credential" in str(raised), (
        "the refusal must name the problem as a credential -- refused as 'extra fields not "
        "permitted' reads as 'rename the field', and the next attempt puts the same secret "
        f"in a key the model accepts. Got {raised!r}"
    )


def test_the_credential_check_finds_the_key_at_every_depth() -> None:
    """`secret_bearing_keys` on its own, without a database, so the paths it
    reports are pinned rather than inferred from a refusal message."""
    from app.core.approved_actions import secret_bearing_keys

    assert secret_bearing_keys(
        {
            "repository_id": "r1",
            "repo_key": "k",
            "post_config": {"join": {"admin_password": "p"}, "hostname": "h"},
            "sources": [{"paths": ["C:\\"]}, {"unlock_token": "t"}],
        }
    ) == ["post_config.join.admin_password", "repo_key", "sources.unlock_token"]


def test_the_credential_check_does_not_fire_on_ordinary_params() -> None:
    """The other half. Without it the walk could return every key it sees and
    every test above would still pass -- with the whole facility refusing every
    template."""
    from app.core.approved_actions import secret_bearing_keys

    assert secret_bearing_keys({"patches": [{"id": "KB1"}], "reboot": "never"}) == []


# ---------------------------------------------------------------------------
# The READ side of invariant #3: rows written BEFORE the write gate shipped
# ---------------------------------------------------------------------------


def test_the_secret_values_are_redacted_at_every_depth_the_check_finds_them() -> None:
    """`redact_secret_values` on its own.

    The write gate cannot reach a row that already exists, so the read path
    needs its own answer -- and it has to reach exactly as deep, because the
    nested and in-a-list cases are the ones a legacy row actually carries
    (`app.deploy`'s free-form rules, `backup.run`'s `sources`).

    THE ASSERTION IS ON THE RETURNED DOCUMENT, not on a boolean: what matters is
    that no secret VALUE survives, and that every non-secret value does. A test
    that only checked the path list would pass against a function that returned
    the template untouched.
    """
    from app.core.approved_actions import REDACTED_PLACEHOLDER, redact_secret_values

    template = {
        "repository_id": "r1",
        "repo_key": "SUPERSECRET",
        "post_config": {"join": {"admin_password": "hunter2"}, "hostname": "h"},
        "sources": [{"paths": ["C:\\"]}, {"unlock_token": "TOKVALUE"}],
    }
    out, paths = redact_secret_values(template)

    assert out == {
        "repository_id": "r1",
        "repo_key": REDACTED_PLACEHOLDER,
        "post_config": {"join": {"admin_password": REDACTED_PLACEHOLDER}, "hostname": "h"},
        "sources": [{"paths": ["C:\\"]}, {"unlock_token": REDACTED_PLACEHOLDER}],
    }
    assert paths == ["post_config.join.admin_password", "repo_key", "sources.unlock_token"]
    assert "SUPERSECRET" not in str(out)
    assert "hunter2" not in str(out)
    assert "TOKVALUE" not in str(out)
    # The input is not mutated: the caller is handing us an ORM attribute, and a
    # redaction that edited it in place would write the placeholder BACK to the
    # database on the next flush and destroy the value it was protecting.
    assert template["repo_key"] == "SUPERSECRET"


def test_an_ordinary_template_survives_redaction_unchanged() -> None:
    """The other half, and the one that keeps the gate from being "return
    nothing". Every test above passes against a function that redacts
    everything."""
    from app.core.approved_actions import redact_secret_values

    template = {"patches": [{"id": "KB1"}], "reboot": "never"}
    out, paths = redact_secret_values(template)
    assert out == template
    assert paths == []


@pytest.mark.parametrize("marker", sorted(_SECRET_MARKERS))
def test_the_two_sides_agree_on_every_marker_in_the_live_set(marker: str) -> None:
    """THE DRIFT GUARD, over the LIVE marker set rather than a copied list.

    A key the write gate refuses and the read path serves is the hole this
    finding was: two predicates that were once the same and stopped being. Both
    functions read `SECRET_PARAM_KEY_MARKERS`, so adding a marker must make BOTH
    fire -- and this is parametrized over the set itself, so a marker added
    without a matching redaction breaks a test instead of shipping a leak.
    """
    from app.core.approved_actions import (
        REDACTED_PLACEHOLDER,
        redact_secret_values,
        secret_bearing_keys,
    )

    key = f"device_{marker}"
    template = {key: "the-secret-value"}
    assert secret_bearing_keys(template) == [key]
    out, paths = redact_secret_values(template)
    assert paths == [key]
    assert out == {key: REDACTED_PLACEHOLDER}


def test_a_secret_named_key_is_redacted_whole_and_covers_what_the_check_finds() -> None:
    """A credential-named key holding an OBJECT loses the object, not merely its
    innocently-named leaves.

    That makes the redacted paths a PREFIX-COVER of `secret_bearing_keys`'
    paths rather than an exact match, and the cover is the property that
    matters: every path the write gate would refuse is at or under a path the
    read path withheld. Asserted as a cover rather than as equality, because
    equality would be the WRONG direction -- it would require the read path to
    leave `credentials.username` in place.
    """
    from app.core.approved_actions import redact_secret_values, secret_bearing_keys

    template = {"credentials": {"username": "svc", "value": "s3cr3t"}, "target": "d1"}
    out, paths = redact_secret_values(template)
    assert "s3cr3t" not in str(out)
    assert "svc" not in str(out)
    assert out["target"] == "d1"
    for found in secret_bearing_keys(template):
        assert any(
            found == path or found.startswith(f"{path}.") for path in paths
        ), f"{found} is refused on write but was not covered by any redacted path {paths}"


async def test_the_write_time_check_is_the_same_model_the_dispatcher_validates_against(
    admin_engine,
) -> None:
    """The check is only worth anything if it is the SAME model
    `JobEngine.create` uses. Asserted by construction: the template that passed
    the write-time check is fed to `get_job_type(...).params_model` exactly as
    the engine does, and the template that failed it is shown to raise there.

    Without this, `_validate_params_against_job_type` could validate against
    something adjacent -- a hand-written key list, a superset -- and drift from
    the engine silently, which is how the gap being closed here opened.
    """
    from app.jobs.registry import get_job_type
    from app.worker import register_worker_job_types

    register_worker_job_types()
    org = await _org(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        accepted = await create_approved_action(
            session,
            org.org_id,
            org.user_id,
            _fields(job_type="patch.apply", params_template={"patches": [], "reboot": "never"}),
        )
        await session.commit()
        stored_template = dict(accepted.params_template)

    model = get_job_type("patch.apply").params_model
    model.model_validate(stored_template)  # the engine's own gate, unchanged

    with pytest.raises(ValidationError):
        model.model_validate({"reboot": "never"})


async def test_a_script_run_params_template_must_be_empty(admin_engine) -> None:
    """The dispatcher builds `ScriptRunParams` from the PINNED `scripts` row and
    never from the template (§8.2: "duplicating the pin into the template would
    create two truths that can disagree").

    So a non-empty template on a `script.run` action is config an operator can
    read, believe and edit, and that nothing will ever execute -- including an
    `interpreter`/`body` pair that looks exactly like the thing that runs. It is
    refused rather than ignored, for the same reason a `destructive` field in the
    body is.
    """
    org = await _org(admin_engine)
    script_id = await seed_script(admin_engine, org.org_id, org.user_id)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(HealthValidationError, match="must be empty"):
            await create_approved_action(
                session,
                org.org_id,
                org.user_id,
                _fields(
                    job_type="script.run",
                    script_id=script_id,
                    script_version=1,
                    params_template={"interpreter": "powershell", "body": "Remove-Item C:\\"},
                ),
            )
        await session.rollback()


async def test_creating_an_approved_action_is_audited(admin_engine) -> None:
    from app.models import AuditEvent

    org = await _org(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        action = await create_approved_action(session, org.org_id, org.user_id, _fields())
        await session.commit()
        action_id = action.id
    async with AsyncSession(admin_engine) as session:
        record = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org.org_id,
                    AuditEvent.action == "health.action_created",
                    AuditEvent.target_id == str(action_id),
                )
            )
        ).scalar_one()
    assert record.meta["job_type"] == "inventory.collect"
    assert record.meta["destructive"] is False


async def test_another_orgs_approved_action_is_a_404(admin_engine) -> None:
    """404, never 403 -- pinned on the admin engine where RLS is not the thing
    filtering."""
    from app.modules.health.approved_actions import get_action
    from app.modules.health.errors import HealthNotFoundError

    a = await _org(admin_engine)
    b = await _org(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        action = await create_approved_action(session, b.org_id, b.user_id, _fields())
        await session.commit()
        action_id = action.id

    leaked = None
    raised: Exception | None = None
    async with AsyncSession(admin_engine) as session:
        try:
            leaked = await get_action(session, a.org_id, action_id)
        except Exception as exc:  # noqa: BLE001 - the type is asserted below
            raised = exc
    assert leaked is None, (
        f"get_action returned org {getattr(leaked, 'org_id', None)}'s action to org {a.org_id}"
    )
    assert isinstance(raised, HealthNotFoundError)
