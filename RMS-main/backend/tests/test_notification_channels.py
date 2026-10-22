"""Channel CRUD and vault-held credentials
(docs/modules/notification/spec.md §2.2, §5, §8).

Covers: `create_channel` stores a supplied secret in the vault and keeps only
`credential_id` on the row -- never the plaintext, in `config`, in a log
line, or on the row itself (invariant #3); a create with no secret leaves
`credential_id` NULL; every mutation writes exactly the audit event the spec
names (invariant #4); `get_channel`/`list_channels` are org-scoped -- a
channel in another org is indistinguishable from a nonexistent one
(invariant #5, the exact `default_queue_id` bug class from
`service_desk.refdata`); `update_channel` applies only the supplied fields,
can rotate the secret in place, and reports only changed field names;
`delete_channel` audits before removing the row.

ENGINE CHOICE. Every test uses a raw `AsyncSession(api_env.admin_engine, ...)`
(BYPASSRLS), never `org_session`/`app_engine` -- mirroring
`test_service_desk_refdata.py`'s cross-org `resolve_*` tests: RLS would mask
a foreign org's row and prove nothing about `get_channel`/`list_channels`'
own explicit `org_id` predicate, which is what these tests must pin down.
"""

from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.vault import LocalVaultService
from app.models import AuditEvent, Credential, NotificationChannel, User
from app.modules.notification.channels import (
    create_channel,
    delete_channel,
    get_channel,
    list_channels,
    update_channel,
)
from app.modules.notification.errors import (
    NotificationNotFoundError,
    NotificationValidationError,
)
from app.modules.notification.schema import ChannelCreate, ChannelUpdate


async def _org_with_user(api_env) -> tuple[UUID, UUID]:
    """Create a throwaway org plus an `org-admin` user in it; return
    `(org_id, user_id)`. Mirrors `test_service_desk_refdata.py`'s helper of
    the same name/shape -- not imported across test modules, per convention."""
    org_id = await api_env.create_org("notif-channels")
    sub = await api_env.create_user(org_id, "org-admin")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return org_id, user_id


def _vault(tmp_path: Path) -> LocalVaultService:
    return LocalVaultService(tmp_path / "root.key")


async def _events(session: AsyncSession, org_id: UUID, action: str) -> list[AuditEvent]:
    return (
        (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id, AuditEvent.action == action
                )
            )
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# create_channel
# ---------------------------------------------------------------------------


async def test_create_channel_stores_the_secret_in_the_vault(api_env, tmp_path: Path) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await create_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=ChannelCreate(
                kind="webhook", name="ops", config={"url": "https://h.example.com/x"}
            ),
            secret="bearer-token-value",
            vault=vault,
        )
        assert channel.credential_id is not None

        # Invariant #3: the plaintext is nowhere in the row or its config.
        assert "bearer-token-value" not in str(channel.config)
        assert channel.config == {"url": "https://h.example.com/x"}

        # Defense in depth: not even ciphertext-adjacent columns on the
        # underlying Credential row carry the plaintext.
        credential = (
            await session.execute(
                select(Credential).where(Credential.id == channel.credential_id)
            )
        ).scalar_one()
        assert "bearer-token-value" not in (credential.ciphertext or "")
        assert credential.ciphertext is not None
        assert credential.wrapped_dek is not None

        # And the vault actually round-trips the original secret.
        secret = await vault.checkout(
            session, org_id=org_id, credential_id=channel.credential_id
        )
        assert secret == "bearer-token-value"


async def test_create_channel_without_a_secret_leaves_credential_null(
    api_env, tmp_path: Path
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await create_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=ChannelCreate(
                kind="webhook", name="open", config={"url": "https://h.example.com/x"}
            ),
            secret=None,
            vault=vault,
        )
    assert channel.credential_id is None


async def test_create_channel_writes_an_audit_event(api_env, tmp_path: Path) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await create_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=ChannelCreate(
                kind="webhook", name="ops", config={"url": "https://h.example.com/x"}
            ),
            secret=None,
            vault=vault,
        )
        events = await _events(session, org_id, "notification.channel_created")
    assert len(events) == 1
    event = events[0]
    assert event.target_type == "notification_channel"
    assert event.target_id == str(channel.id)
    assert event.actor_type == "user"
    assert event.actor_id == user_id


async def test_create_channel_audit_metadata_carries_no_secret(
    api_env, tmp_path: Path
) -> None:
    """Invariant #3, the audit-metadata half: a secret must not leak into
    the immutable log either."""
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        await create_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=ChannelCreate(
                kind="webhook", name="ops", config={"url": "https://h.example.com/x"}
            ),
            secret="bearer-token-value",
            vault=vault,
        )
        events = await _events(session, org_id, "notification.channel_created")
    assert "bearer-token-value" not in str(events[0].meta)


async def test_duplicate_channel_name_in_one_org_is_rejected(api_env, tmp_path: Path) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        await create_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=ChannelCreate(kind="webhook", name="ops", config={"url": "https://h.example.com/a"}),
            secret=None,
            vault=vault,
        )
        with pytest.raises(NotificationValidationError):
            await create_channel(
                session,
                org_id=org_id,
                actor_id=user_id,
                data=ChannelCreate(
                    kind="webhook", name="ops", config={"url": "https://h.example.com/b"}
                ),
                secret=None,
                vault=vault,
            )


async def test_create_channel_concurrent_duplicate_raises_typed_error_not_integrity_error(
    api_env, admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Two concurrent `create_channel` calls for the same `(org_id, name)`
    can both miss the in-memory pre-check before either has written. The fix
    mirrors `service_desk.refdata.create_queue`'s own conflict-safe write:
    the actual insert goes through `ON CONFLICT DO NOTHING` against
    `uq_channel_org_name` -- but this is a single-caller CREATE, not a
    get-or-create, so the race loser must raise the SAME typed
    `NotificationValidationError` the pre-check raises, never a raw
    `IntegrityError`, and never end up holding the winner's row.

    Deterministic interleave, following
    `test_service_desk_refdata.py`'s
    `test_create_queue_concurrent_duplicate_raises_typed_error_not_integrity_error`:
    session A's pre-check SELECT against `notification_channels` is
    intercepted, and right after it returns (a miss -- nothing exists yet),
    session B runs a full, independent `create_channel` for the EXACT same
    name and commits before session A's own insert proceeds."""
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session_a:
        original_execute = session_a.execute
        precheck_seen = False

        async def interleaving_execute(statement, *args, **kwargs):
            nonlocal precheck_seen
            result = await original_execute(statement, *args, **kwargs)
            if (
                not precheck_seen
                and getattr(statement, "is_select", False)
                and "notification_channels.id" in str(statement).lower()
            ):
                precheck_seen = True
                async with AsyncSession(admin_engine, expire_on_commit=False) as session_b:
                    await create_channel(
                        session_b,
                        org_id=org_id,
                        actor_id=user_id,
                        data=ChannelCreate(
                            kind="webhook",
                            name="ops",
                            config={"url": "https://h.example.com/winner"},
                        ),
                        secret=None,
                        vault=vault,
                    )
                    await session_b.commit()
            return result

        session_a.execute = interleaving_execute  # type: ignore[method-assign]
        with pytest.raises(NotificationValidationError, match="channel name already exists"):
            await create_channel(
                session_a,
                org_id=org_id,
                actor_id=user_id,
                data=ChannelCreate(
                    kind="webhook", name="ops", config={"url": "https://h.example.com/loser"}
                ),
                secret=None,
                vault=vault,
            )
        await session_a.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rows = (
            (
                await session.execute(
                    select(NotificationChannel).where(
                        NotificationChannel.org_id == org_id,
                        NotificationChannel.name == "ops",
                    )
                )
            )
            .scalars()
            .all()
        )
    # Exactly one channel landed -- the WINNER's -- never both, never a
    # second row from the loser retrying past its own typed error.
    assert len(rows) == 1
    assert rows[0].config == {"url": "https://h.example.com/winner"}


async def test_same_channel_name_in_two_orgs_is_fine(api_env, tmp_path: Path) -> None:
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel_a = await create_channel(
            session,
            org_id=org_a,
            actor_id=user_a,
            data=ChannelCreate(kind="webhook", name="ops", config={"url": "https://h.example.com/a"}),
            secret=None,
            vault=vault,
        )
        channel_b = await create_channel(
            session,
            org_id=org_b,
            actor_id=user_b,
            data=ChannelCreate(kind="webhook", name="ops", config={"url": "https://h.example.com/b"}),
            secret=None,
            vault=vault,
        )
    assert channel_a.name == channel_b.name == "ops"
    assert channel_a.id != channel_b.id


# ---------------------------------------------------------------------------
# get_channel / list_channels -- tenancy (spec §12.10)
# ---------------------------------------------------------------------------


async def test_get_channel_is_org_scoped(api_env, tmp_path: Path) -> None:
    """Spec §12.10 -- this assertion must fail if the org_id predicate in
    get_channel is deleted. Run on a raw BYPASSRLS admin session, so RLS is
    not the thing making this pass: `get_channel`'s own `org_id` predicate
    is the sole guard under test (see module docstring)."""
    org_id, user_id = await _org_with_user(api_env)
    other_org_id, other_user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await create_channel(
            session,
            org_id=other_org_id,
            actor_id=other_user_id,
            data=ChannelCreate(
                kind="webhook", name="theirs", config={"url": "https://h.example.com/x"}
            ),
            secret=None,
            vault=vault,
        )
        with pytest.raises(NotificationNotFoundError):
            await get_channel(session, org_id=org_id, channel_id=channel.id)


async def test_get_channel_raises_the_identical_error_for_a_nonexistent_id(
    api_env,
) -> None:
    """The other half of invariant #5's "indistinguishable" requirement: a
    nonexistent id must raise the SAME error type as a cross-org id, not a
    different one that would let a caller tell the two cases apart."""
    from uuid_extensions import uuid7

    org_id, _user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        with pytest.raises(NotificationNotFoundError):
            await get_channel(session, org_id=org_id, channel_id=uuid7())


async def test_list_channels_excludes_other_orgs(api_env, tmp_path: Path) -> None:
    """Spec §12.10 -- must fail if the org_id predicate in list_channels is
    deleted, for the same reason as test_get_channel_is_org_scoped above."""
    org_id, user_id = await _org_with_user(api_env)
    other_org_id, other_user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        await create_channel(
            session,
            org_id=other_org_id,
            actor_id=other_user_id,
            data=ChannelCreate(
                kind="webhook", name="theirs", config={"url": "https://h.example.com/x"}
            ),
            secret=None,
            vault=vault,
        )
        assert await list_channels(session, org_id=org_id) == []


async def test_list_channels_returns_only_this_orgs_channels(api_env, tmp_path: Path) -> None:
    org_id, user_id = await _org_with_user(api_env)
    other_org_id, other_user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        mine = await create_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=ChannelCreate(kind="webhook", name="mine", config={"url": "https://h.example.com/m"}),
            secret=None,
            vault=vault,
        )
        await create_channel(
            session,
            org_id=other_org_id,
            actor_id=other_user_id,
            data=ChannelCreate(
                kind="webhook", name="theirs", config={"url": "https://h.example.com/x"}
            ),
            secret=None,
            vault=vault,
        )
        seen = await list_channels(session, org_id=org_id)
    assert [c.id for c in seen] == [mine.id]


# ---------------------------------------------------------------------------
# update_channel
# ---------------------------------------------------------------------------


async def test_update_channel_applies_only_supplied_fields_and_audits_changed_names(
    api_env, tmp_path: Path
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await create_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=ChannelCreate(kind="webhook", name="ops", config={"url": "https://h.example.com/a"}),
            secret=None,
            vault=vault,
        )
        updated = await update_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            channel_id=channel.id,
            data=ChannelUpdate(active=False),
            secret=None,
            vault=vault,
        )
        events = await _events(session, org_id, "notification.channel_updated")

    assert updated.active is False
    assert updated.name == "ops"  # untouched
    assert len(events) == 1
    assert events[0].meta["changed_fields"] == ["active"]


async def test_update_channel_rejects_null_name(api_env, tmp_path: Path) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await create_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=ChannelCreate(kind="webhook", name="ops", config={"url": "https://h.example.com/a"}),
            secret=None,
            vault=vault,
        )
        with pytest.raises(NotificationValidationError):
            await update_channel(
                session,
                org_id=org_id,
                actor_id=user_id,
                channel_id=channel.id,
                data=ChannelUpdate(name=None),
                secret=None,
                vault=vault,
            )


async def test_update_channel_rejects_a_config_that_no_longer_matches_kind(
    api_env, tmp_path: Path
) -> None:
    """Whole-branch review finding C1(a) -- `ChannelUpdate` carries no `kind`
    field (it is immutable), so this is validated SERVICE-SIDE, against the
    channel's EXISTING kind, once `update_channel` has loaded the row.
    `ChannelUpdate(config={})` used to be accepted outright for an email
    channel; `_send_email` would then raise an uncaught `KeyError` at send
    time on whichever delivery used it next."""
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await create_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=ChannelCreate(
                kind="email",
                name="mail",
                config={"host": "mail.example.com", "port": 587, "from_address": "a@b.c"},
            ),
            secret=None,
            vault=vault,
        )
        with pytest.raises(NotificationValidationError, match="email config requires"):
            await update_channel(
                session,
                org_id=org_id,
                actor_id=user_id,
                channel_id=channel.id,
                data=ChannelUpdate(config={}),
                secret=None,
                vault=vault,
            )
        with pytest.raises(NotificationValidationError, match="email config requires"):
            await update_channel(
                session,
                org_id=org_id,
                actor_id=user_id,
                channel_id=channel.id,
                data=ChannelUpdate(config={"from_address": "ops@corp.example"}),
                secret=None,
                vault=vault,
            )

        # Untouched by either rejected attempt.
        reloaded = await get_channel(session, org_id=org_id, channel_id=channel.id)
    assert reloaded.config == {
        "host": "mail.example.com",
        "port": 587,
        "from_address": "a@b.c",
    }


async def test_update_channel_rejects_a_non_integer_port(api_env, tmp_path: Path) -> None:
    """The `_config_matches_kind` (create) fix and this update-path fix share
    ONE validator (`schema.validate_channel_config`) -- this pins that the
    update path gets the same port-type coercion the create path does, not a
    weaker copy of it."""
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await create_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=ChannelCreate(
                kind="email",
                name="mail",
                config={"host": "mail.example.com", "port": 587, "from_address": "a@b.c"},
            ),
            secret=None,
            vault=vault,
        )
        with pytest.raises(NotificationValidationError, match="port"):
            await update_channel(
                session,
                org_id=org_id,
                actor_id=user_id,
                channel_id=channel.id,
                data=ChannelUpdate(
                    config={
                        "host": "mail.example.com",
                        "port": "not-a-number",
                        "from_address": "a@b.c",
                    }
                ),
                secret=None,
                vault=vault,
            )


async def test_update_channel_allows_a_config_replacement_that_matches_kind(
    api_env, tmp_path: Path
) -> None:
    """Positive control: the fix above must not block a legitimate config
    replacement that DOES match the channel's kind."""
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await create_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=ChannelCreate(
                kind="email",
                name="mail",
                config={"host": "mail.example.com", "port": 587, "from_address": "a@b.c"},
            ),
            secret=None,
            vault=vault,
        )
        updated = await update_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            channel_id=channel.id,
            data=ChannelUpdate(
                config={"host": "mail2.example.com", "port": 25, "from_address": "b@b.c"}
            ),
            secret=None,
            vault=vault,
        )
    assert updated.config == {
        "host": "mail2.example.com",
        "port": 25,
        "from_address": "b@b.c",
    }


async def test_update_channel_is_org_scoped(api_env, tmp_path: Path) -> None:
    """A channel id from another org raises NotificationNotFoundError, not a
    silent no-op update of a foreign row."""
    org_id, user_id = await _org_with_user(api_env)
    other_org_id, other_user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        foreign = await create_channel(
            session,
            org_id=other_org_id,
            actor_id=other_user_id,
            data=ChannelCreate(
                kind="webhook", name="theirs", config={"url": "https://h.example.com/x"}
            ),
            secret=None,
            vault=vault,
        )
        with pytest.raises(NotificationNotFoundError):
            await update_channel(
                session,
                org_id=org_id,
                actor_id=user_id,
                channel_id=foreign.id,
                data=ChannelUpdate(active=False),
                secret=None,
                vault=vault,
            )


async def test_update_channel_concurrent_rename_to_duplicate_raises_typed_error_not_integrity_error(
    api_env, admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The rename-time counterpart of the create-time race above: two
    concurrent `update_channel` calls that both rename a DIFFERENT existing
    channel to the SAME target name can both miss `_reject_duplicate_name`'s
    pre-check before either has written. `update_channel`'s fix is a
    SAVEPOINT around the rename's flush (there is no `ON CONFLICT DO
    NOTHING` for an UPDATE) that catches the `uq_channel_org_name`
    violation and re-raises the identical typed `NotificationValidationError`
    the pre-check raises -- never a raw `IntegrityError`, and the loser's
    channel keeps its ORIGINAL name rather than silently becoming a second
    "winner" row.

    Same deterministic-interleave shape as the create-time test: session A's
    pre-check SELECT is intercepted, and right after it returns (a miss),
    session B renames its OWN channel to the exact same target name and
    commits before session A's own update proceeds.

    The hook keys on `notification_channels.name =` (an equality PREDICATE)
    rather than the broader `notification_channels.id` used in the
    create-time test: `update_channel` issues `get_channel`'s full-row SELECT
    first, which also projects an `id` column and would otherwise match too
    early, before `_reject_duplicate_name`'s own SELECT (the one this test
    needs to interleave around) ever runs. `get_channel`'s SELECT lists
    `name` only as a projected column, never as a `WHERE ... name = `
    predicate, so it never matches this narrower marker."""
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        channel_a = await create_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=ChannelCreate(
                kind="webhook", name="alpha", config={"url": "https://h.example.com/a"}
            ),
            secret=None,
            vault=vault,
        )
        channel_b = await create_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=ChannelCreate(
                kind="webhook", name="bravo", config={"url": "https://h.example.com/b"}
            ),
            secret=None,
            vault=vault,
        )
        await session.commit()
    channel_a_id, channel_b_id = channel_a.id, channel_b.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session_a:
        original_execute = session_a.execute
        precheck_seen = False

        async def interleaving_execute(statement, *args, **kwargs):
            nonlocal precheck_seen
            result = await original_execute(statement, *args, **kwargs)
            if (
                not precheck_seen
                and getattr(statement, "is_select", False)
                and "notification_channels.name =" in str(statement).lower()
            ):
                precheck_seen = True
                async with AsyncSession(admin_engine, expire_on_commit=False) as session_b:
                    await update_channel(
                        session_b,
                        org_id=org_id,
                        actor_id=user_id,
                        channel_id=channel_b_id,
                        data=ChannelUpdate(name="shared"),
                        secret=None,
                        vault=vault,
                    )
                    await session_b.commit()
            return result

        session_a.execute = interleaving_execute  # type: ignore[method-assign]
        with pytest.raises(NotificationValidationError, match="channel name already exists"):
            await update_channel(
                session_a,
                org_id=org_id,
                actor_id=user_id,
                channel_id=channel_a_id,
                data=ChannelUpdate(name="shared"),
                secret=None,
                vault=vault,
            )
        await session_a.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        shared_rows = (
            (
                await session.execute(
                    select(NotificationChannel).where(
                        NotificationChannel.org_id == org_id,
                        NotificationChannel.name == "shared",
                    )
                )
            )
            .scalars()
            .all()
        )
        loser = (
            await session.execute(
                select(NotificationChannel).where(NotificationChannel.id == channel_a_id)
            )
        ).scalar_one()
    # Exactly one channel holds "shared" -- the WINNER's (channel_b). The
    # loser (channel_a) kept its original name rather than the flush leaving
    # it half-applied or somehow also landing on "shared".
    assert len(shared_rows) == 1
    assert shared_rows[0].id == channel_b_id
    assert loser.name == "alpha"


async def test_update_channel_rotates_the_secret_in_place_without_leaking_plaintext(
    api_env, tmp_path: Path
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await create_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=ChannelCreate(kind="webhook", name="ops", config={"url": "https://h.example.com/a"}),
            secret="first-secret",
            vault=vault,
        )
        original_credential_id = channel.credential_id

        updated = await update_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            channel_id=channel.id,
            data=ChannelUpdate(),
            secret="second-secret",
            vault=vault,
        )

        assert "second-secret" not in str(updated.config)
        rotated_secret = await vault.checkout(
            session, org_id=org_id, credential_id=updated.credential_id
        )
        assert rotated_secret == "second-secret"

        events = await _events(session, org_id, "notification.channel_updated")
    assert updated.credential_id is not None
    # Rotated in place -- the SAME credential id, not a newly minted one, so
    # no orphaned Credential row is left behind by a rotation.
    assert updated.credential_id == original_credential_id
    assert "credential_id" in events[-1].meta["changed_fields"]


async def test_update_channel_can_add_a_secret_where_none_existed(
    api_env, tmp_path: Path
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await create_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=ChannelCreate(kind="webhook", name="ops", config={"url": "https://h.example.com/a"}),
            secret=None,
            vault=vault,
        )
        assert channel.credential_id is None

        updated = await update_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            channel_id=channel.id,
            data=ChannelUpdate(),
            secret="added-later",
            vault=vault,
        )
    assert updated.credential_id is not None


# ---------------------------------------------------------------------------
# delete_channel
# ---------------------------------------------------------------------------


async def test_delete_channel_removes_the_row_and_audits(api_env, tmp_path: Path) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await create_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=ChannelCreate(kind="webhook", name="ops", config={"url": "https://h.example.com/a"}),
            secret=None,
            vault=vault,
        )
        channel_id = channel.id

        await delete_channel(session, org_id=org_id, actor_id=user_id, channel_id=channel_id)

        events = await _events(session, org_id, "notification.channel_deleted")
        with pytest.raises(NotificationNotFoundError):
            await get_channel(session, org_id=org_id, channel_id=channel_id)
    assert len(events) == 1
    assert events[0].target_id == str(channel_id)


async def test_delete_channel_is_org_scoped(api_env, tmp_path: Path) -> None:
    org_id, user_id = await _org_with_user(api_env)
    other_org_id, other_user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        foreign = await create_channel(
            session,
            org_id=other_org_id,
            actor_id=other_user_id,
            data=ChannelCreate(
                kind="webhook", name="theirs", config={"url": "https://h.example.com/x"}
            ),
            secret=None,
            vault=vault,
        )
        with pytest.raises(NotificationNotFoundError):
            await delete_channel(session, org_id=org_id, actor_id=user_id, channel_id=foreign.id)

        # Untouched -- still resolvable in its own org.
        still_there = await get_channel(session, org_id=other_org_id, channel_id=foreign.id)
    assert still_there.id == foreign.id
