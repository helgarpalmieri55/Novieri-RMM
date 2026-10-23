"""The preview digest (docs/modules/ai-automation.md §6.4).

**WHAT THE DIGEST IS FOR, AND WHAT IT IS NOT FOR.** It is NOT proof that a
preview happened -- the persisted `ai_action` row is that, and §0.6 records why
IPAM's import digest, which has only the digest half, cannot make the claim. The
digest's one job is to catch the stored row having changed underneath the human
between the moment they were shown a preview and the moment they confirmed it.

So the tests here are about CHANGE, and every one of them asserts a VALUE:
"two digests are equal" and "two digests differ" print differently, where "it
raised" prints nothing about either.

`approved_action_updated_at` IS THE FIELD UNDER TEST. Drop it and a catalogue
edit between preview and confirm leaves the digest identical, so §6.4's "editing
the catalogue invalidates the confirmation" stops being true while every other
test in this layer stays green.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai.act import compute_preview_digest, digest_fields, propose
from app.core.ai.executor_staff import invoke_tool
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.jobs.canonical import canonical_bytes
from app.models import AiAction, ApprovedAction
from tests.ai_act_fixtures import build_world


async def _propose(api_env: Any, world: Any, **kw: Any) -> Any:
    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        return await propose(
            session,
            org_id=world.org_id,
            binding=world.binding,
            ctx=world.ctx,
            executor=invoke_tool,
            action_key=world.action_key,
            device_id=world.device_id,
            **kw,
        )


async def _row_and_action(api_env: Any, world: Any) -> tuple[AiAction, ApprovedAction]:
    async with AsyncSession(api_env.admin_engine) as session:
        row = (
            await session.execute(select(AiAction).where(AiAction.org_id == world.org_id))
        ).scalar_one()
        action = (
            await session.execute(
                select(ApprovedAction).where(ApprovedAction.id == world.action_id)
            )
        ).scalar_one()
        return row, action


# --------------------------------------------------------------------------- #
# 9. Determinism.
# --------------------------------------------------------------------------- #


async def test_two_computations_over_one_stored_row_are_equal(api_env: Any) -> None:
    """Recomputed at confirm from the same row, in a different request and a
    different session, and it must be the same string."""
    world = await build_world(api_env, tag="digest-stable")
    preview = await _propose(api_env, world)
    row, action = await _row_and_action(api_env, world)

    first = compute_preview_digest(row, action)
    second = compute_preview_digest(row, action)
    assert first == second == preview.preview_digest
    assert len(first) == 64


# --------------------------------------------------------------------------- #
# 10. A catalogue edit changes it. INEQUALITY, never "it raised".
# --------------------------------------------------------------------------- #


async def test_touching_the_catalogue_rows_updated_at_changes_the_digest(
    api_env: Any,
) -> None:
    """**THE MUTATION THIS TEST EXISTS FOR: drop `approved_action_updated_at`
    from `digest_fields`.** Then this observes two EQUAL digests across a
    catalogue edit -- and §6.4's binding between a preview and what it described
    is gone, silently."""
    world = await build_world(api_env, tag="digest-edit")
    await _propose(api_env, world)
    row, action = await _row_and_action(api_env, world)
    before = compute_preview_digest(row, action)

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        stored = await session.get(ApprovedAction, world.action_id)
        assert stored is not None
        stored.updated_at = datetime.now(UTC).replace(year=2030)
        await session.commit()

    _, edited = await _row_and_action(api_env, world)
    assert compute_preview_digest(row, edited) != before


async def test_changing_the_params_template_changes_the_digest(api_env: Any) -> None:
    """The other half of the same claim, through a field an admin actually
    edits. `rendered_params` is on the row rather than on the catalogue, so this
    exercises the row side of the map."""
    world = await build_world(api_env, tag="digest-params")
    await _propose(api_env, world)
    row, action = await _row_and_action(api_env, world)
    before = compute_preview_digest(row, action)

    row.rendered_params = {"changed": True}
    assert compute_preview_digest(row, action) != before


@pytest.mark.parametrize(
    "field",
    ["job_type", "risk_class", "effective_autonomy", "destructive", "script_version"],
)
async def test_every_row_field_in_the_map_moves_the_digest(api_env: Any, field: str) -> None:
    """A field listed in §6.4 that the hash does not actually read would make the
    map a description rather than a definition. Parametrized so each one is
    named on failure."""
    world = await build_world(api_env, tag="digest-fields")
    await _propose(api_env, world)
    row, action = await _row_and_action(api_env, world)
    before = compute_preview_digest(row, action)

    replacement: Any = {
        "job_type": "patch.scan",
        "risk_class": "high",
        "effective_autonomy": "suggest",
        "destructive": not row.destructive,
        "script_version": 99,
    }[field]
    setattr(row, field, replacement)
    assert compute_preview_digest(row, action) != before, field


# --------------------------------------------------------------------------- #
# 11. Every uuid and datetime is stringified BEFORE `canonical_bytes` sees it.
# --------------------------------------------------------------------------- #


def test_canonical_bytes_rejects_a_raw_uuid_and_a_raw_datetime() -> None:
    """**THIS IS WHAT PROVES THE GUARD IS `canonical_bytes`** and not a `str()`
    somewhere incidental.

    `app/jobs/canonical.py`'s `default=` handler rejects anything `json` cannot
    serialize BY DESIGN -- "passing a datetime raises so a formatting ambiguity
    can never silently change signed bytes". A builder who canonicalizes a UUID
    by accident gets a `TypeError` at write time, not a digest that differs
    between two Python versions.
    """
    with pytest.raises(TypeError, match="UUID"):
        canonical_bytes({"ai_action_id": uuid4()})
    with pytest.raises(TypeError, match="datetime"):
        canonical_bytes({"approved_action_updated_at": datetime.now(UTC)})


async def test_the_field_map_contains_no_uuid_and_no_datetime(api_env: Any) -> None:
    """Asserted over the MAP, not over one call site, so a field added later
    that carries a raw type fails here rather than at 03:00."""
    world = await build_world(api_env, tag="digest-types")
    await _propose(api_env, world)
    row, action = await _row_and_action(api_env, world)

    fields = digest_fields(row, action)
    for key, value in fields.items():
        assert not isinstance(value, UUID), f"{key} is a raw UUID"
        assert not isinstance(value, datetime), f"{key} is a raw datetime"
    # And the whole map really does canonicalize.
    assert canonical_bytes(fields)

    # The ten §6.4 names plus the row's own id, exactly -- so a field added to
    # the hash without being added to the spec is visible.
    assert set(fields) == {
        "ai_action_id",
        "approved_action_id",
        "approved_action_updated_at",
        "job_type",
        "rendered_params",
        "script_id",
        "script_version",
        "target_device_id",
        "risk_class",
        "destructive",
        "effective_autonomy",
    }


# --------------------------------------------------------------------------- #
# 12. `redact_secret_values` on the preview read path.
# --------------------------------------------------------------------------- #


async def test_a_legacy_credential_named_param_is_redacted_in_the_preview(
    api_env: Any,
) -> None:
    """§6.2. The registry's write gate refuses a `params_template` naming a
    credential and does NOTHING about rows written before it shipped, so a
    legacy `repo_key` would otherwise be rendered in full on a confirmation
    screen -- invariant #3 violated on both halves at once.

    `script.run` is used because it is approvable AND `SUBSTITUTABLE_PARAM_KEYS`
    permits no token in it, so the template renders as a constant. The raw value
    must appear NOWHERE in the preview, not merely be marked.
    """
    world = await build_world(
        api_env,
        tag="digest-redact",
        action_kw={
            "job_type": "app.deploy",
            "destructive": True,
            "params_template": {"artifact": "pkg-1.0", "api_token": "hunter2"},
        },
    )
    preview = await _propose(api_env, world)

    assert preview.rendered_params["api_token"] == "[redacted: stored credential]"
    assert preview.rendered_params["artifact"] == "pkg-1.0"
    assert "api_token" in preview.redacted_param_paths
    assert "hunter2" not in str(preview)


async def test_the_digest_is_over_the_stored_params_not_the_redacted_ones(
    api_env: Any,
) -> None:
    """The redaction is a READ-PATH concern. If it leaked into the digest, the
    server's recomputation at confirm would depend on which rendering path ran
    -- two truths again, one of them a security control."""
    world = await build_world(
        api_env,
        tag="digest-redact3",
        action_kw={
            "job_type": "app.deploy",
            "destructive": True,
            "params_template": {"api_token": "hunter2"},
        },
    )
    preview = await _propose(api_env, world)
    row, action = await _row_and_action(api_env, world)

    assert row.rendered_params["api_token"] == "hunter2"
    assert digest_fields(row, action)["rendered_params"] == {"api_token": "hunter2"}
    assert compute_preview_digest(row, action) == preview.preview_digest


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _preview(api_env: Any, world: Any) -> Any:
    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        return await propose(
            session,
            org_id=world.org_id,
            binding=world.binding,
            ctx=world.ctx,
            executor=invoke_tool,
            action_key=world.action_key,
            device_id=world.device_id,
        )


async def test_a_catalogue_edit_inside_one_second_still_moves_the_digest(
    api_env: Any,
) -> None:
    """**THE TRUNCATION THAT MADE §6.4 TRUE ONLY TO THE NEAREST SECOND.**

    `approved_action_updated_at` was formatted by `app/jobs/canonical.py::
    rfc3339`, which renders `%H:%M:%SZ` -- whole seconds. `ApprovedAction.
    updated_at` carries `onupdate`, so an edit landing in the SAME wall-clock
    second as the row's previous update produced an identical digest, and the
    confirm proceeded against the edited row. §6.4's claim -- "editing the
    catalogue invalidates the confirmation" -- was true only at one-second
    granularity.

    The window is small and nobody can steer it, which is why this went unnoticed
    until `test_ai_act_confirm.py`'s fuse test flaked under full-suite load. But a
    digest whose whole job is to detect an edit must detect EVERY edit.

    **THE TWO TIMESTAMPS HERE DIFFER BY ONE MICROSECOND AND SHARE A SECOND**, so
    this fails against `rfc3339` formatting and passes against `isoformat()`. It
    is asserted on the two digests being DIFFERENT -- a wrong VALUE -- rather than
    on anything raising.
    """
    world = await build_world(api_env, tag="digest-subsecond")
    preview = await _preview(api_env, world)

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        action = await session.get(ApprovedAction, world.action_id)
        assert action is not None
        base = _aware(action.updated_at)
        action.description = "edited within the same second"
        action.updated_at = base + timedelta(microseconds=1)
        await session.commit()

    async with AsyncSession(api_env.admin_engine) as session:
        action = await session.get(ApprovedAction, world.action_id)
        row = await session.get(AiAction, preview.ai_action_id)
        assert action is not None and row is not None
        assert _aware(action.updated_at).replace(microsecond=0) == base.replace(microsecond=0), (
            "the fixture must keep both timestamps inside ONE second or this proves nothing"
        )
        after = compute_preview_digest(row, action)

    assert after != preview.preview_digest, (
        "a catalogue edit inside one second did not move the digest; §6.4's "
        "'editing the catalogue invalidates the confirmation' is then true only to "
        "the nearest second"
    )
