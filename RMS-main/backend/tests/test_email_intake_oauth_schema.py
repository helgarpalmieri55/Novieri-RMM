"""The schema half of delegated Microsoft sign-in for email intake.

`credential_id` becomes nullable, and `ck_email_mailbox_credential` keeps the
property the NOT NULL protected: a mailbox with no credential can never be
active, so it can never be polled. Design:
docs/superpowers/specs/2026-09-11-mail-oauth-design.md §4.7.
"""

from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.test_email_intake_migration import _seed_category, _seed_user

_INSERT = text(
    "INSERT INTO email_mailbox (id, org_id, address, host, port, username, "
    "credential_id, default_category_id, active, auth_kind, credential_state, "
    "created_by, created_at, updated_at) VALUES (gen_random_uuid(), :org, :addr, "
    "'h', 993, 'u', NULL, :cat, :active, :kind, :state, :usr, now(), now())"
)


async def _seeded(api_env) -> tuple[UUID, UUID, UUID]:
    org_id = await api_env.create_org()
    return org_id, await _seed_category(api_env, org_id), await _seed_user(api_env, org_id)


async def _insert(api_env, *, active: bool, kind: str = "microsoft_oauth", state: str = "ok"):
    org_id, category_id, user_id = await _seeded(api_env)
    async with AsyncSession(api_env.admin_engine) as session:
        await session.execute(
            _INSERT,
            {
                "org": org_id,
                "addr": "a@b.c",
                "cat": category_id,
                "usr": user_id,
                "active": active,
                "kind": kind,
                "state": state,
            },
        )
        await session.commit()


async def test_existing_mailboxes_default_to_the_password_path(api_env) -> None:
    async with AsyncSession(api_env.admin_engine) as session:
        rows = dict(
            (
                await session.execute(
                    text(
                        "SELECT column_name, column_default FROM information_schema.columns "
                        "WHERE table_name='email_mailbox' "
                        "AND column_name IN ('auth_kind', 'credential_state')"
                    )
                )
            ).all()
        )
    assert "password" in rows["auth_kind"]
    assert "ok" in rows["credential_state"]


async def test_a_credential_less_mailbox_cannot_be_active(api_env) -> None:
    with pytest.raises(IntegrityError, match="ck_email_mailbox_credential"):
        await _insert(api_env, active=True)


async def test_an_unconnected_oauth_mailbox_is_legal_when_inactive(api_env) -> None:
    await _insert(api_env, active=False)


@pytest.mark.parametrize(
    ("kind", "state", "constraint"),
    [
        ("google_oauth", "ok", "ck_email_mailbox_auth_kind"),
        ("microsoft_oauth", "broken", "ck_email_mailbox_credential_state"),
    ],
)
async def test_the_vocabularies_are_closed(api_env, kind: str, state: str, constraint: str) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        await _insert(api_env, active=False, kind=kind, state=state)


async def test_both_new_tables_are_rls_protected(api_env) -> None:
    async with AsyncSession(api_env.admin_engine) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                    "(SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) "
                    "FROM pg_class c "
                    "WHERE c.relname IN ('email_oauth_app', 'email_oauth_authorization')"
                )
            )
        ).all()
    assert len(rows) == 2
    for name, enabled, forced, policies in rows:
        assert enabled and forced and policies >= 1, name


async def test_one_state_per_org_is_unique(api_env) -> None:
    async with AsyncSession(api_env.admin_engine) as session:
        definitions = (
            (
                await session.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes "
                        "WHERE tablename='email_oauth_authorization'"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert any("UNIQUE" in d and "(org_id, state)" in d for d in definitions)
