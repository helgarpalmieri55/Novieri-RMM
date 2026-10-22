"""`push_credential_scope` and the dedicated `push_install` credential type
(spec §6.2, §6.3, §9; falsifiers 15 and 16).

**THE PROPERTY UNDER TEST IS "THE CREDENTIAL ITSELF CARRIES WHERE IT MAY BE
PRESENTED".** That is the module's answer to *how does one stored secret not
get sprayed at the whole estate*, and it is the third of invariant 2a clause
(d)'s three authorizations.

WHAT THESE FIXTURES DELIBERATELY VARY:

* **THE TENANCY TESTS RUN ON THE BYPASSRLS ADMIN ENGINE**, for the reason
  `tests/test_agent_push_authorization.py`'s docstring sets out in full:
  through `org_session` a deleted `WHERE org_id` changes nothing observable,
  because RLS rejects first.
* **THE TYPE TESTS SEED A CREDENTIAL OF EACH TYPE IN THE SAME ORG.** A fixture
  holding only a `push_install` credential cannot tell "the type is checked"
  from "any credential is accepted"; a fixture holding only a `windows_admin`
  one cannot tell "the type is checked" from "nothing is accepted".
* **THE CROSS-CREDENTIAL TEST BINDS A DIFFERENT CREDENTIAL TO THE SAME
  PREFIX.** Without the `credential_id` predicate in the scope lookup, any
  credential in the org being bound anywhere satisfies every target -- and a
  test that seeds one credential and one scope passes against that defect.

**NOTHING HERE DECRYPTS ANYTHING AND NO FIXTURE STORES A PLAINTEXT**, because
cycle B1 has no point of use for one (spec §14, invariant #3).
`tests/test_agent_push_dispatches_nothing.py` asserts the module cannot.
"""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import AuditEvent, PushCredentialScope
from app.modules.agent_push.credential_scope import (
    PUSH_CREDENTIAL_TYPE,
    bind_scope,
    list_scopes,
    refuse_push_install_credential,
    resolve_push_credential,
    unbind_scope,
)
from app.modules.agent_push.errors import (
    PushConflictError,
    PushNotFoundError,
    PushValidationError,
)
from tests.agent_push_fixtures import (
    seed_credential,
    seed_org,
    seed_push_credential_scope,
)

# --------------------------------------------------------------------------- #
# The dedicated type (spec §6.2, falsifier 15)
# --------------------------------------------------------------------------- #


async def test_only_a_push_install_credential_may_be_bound(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """BOTH CREDENTIALS EXIST IN THE SAME ORG, so the observable difference is
    which one was accepted rather than whether anything was."""
    org = await seed_org(admin_engine, "push-type")
    push_cred = await seed_credential(admin_engine, org)
    remote_cred = await seed_credential(
        admin_engine, org, credential_type="windows_admin"
    )

    async with org_session(org.org_id, engine=app_engine) as session:
        await bind_scope(
            session,
            org.org_id,
            actor_id=org.user_id,
            credential_id=push_cred,
            prefix="10.40.0.0/24",
        )
    async with org_session(org.org_id, engine=app_engine) as session:
        with pytest.raises(PushValidationError) as exc:
            await bind_scope(
                session,
                org.org_id,
                actor_id=org.user_id,
                credential_id=remote_cred,
                prefix="10.40.0.0/24",
            )
    assert "windows_admin" in str(exc.value)
    assert PUSH_CREDENTIAL_TYPE in str(exc.value)


async def test_the_type_is_rechecked_on_resolve_not_only_at_bind(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A credential whose type is edited AFTER its scopes were bound must stop
    working immediately, not at the next bind. Falsified by checking the type
    only inside `bind_scope`: the scope row survives the edit and every later
    push resolves happily against a credential that is no longer dedicated."""
    org = await seed_org(admin_engine, "push-type-drift")
    credential = await seed_credential(admin_engine, org)
    await seed_push_credential_scope(admin_engine, org, credential, "10.41.0.0/24")

    async with AsyncSession(admin_engine) as session:
        from sqlalchemy import text as sqltext

        await session.execute(
            sqltext("UPDATE credentials SET type = 'windows_admin' WHERE id = :i"),
            {"i": credential},
        )
        await session.commit()

    async with org_session(org.org_id, engine=app_engine) as session:
        with pytest.raises(PushValidationError):
            await resolve_push_credential(session, org.org_id, credential)


async def test_a_push_credential_is_refused_by_remote_control(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """THE MIRROR RULE, falsifier 15's other direction. Both credentials exist,
    so the test observes WHICH one was refused rather than that something
    was."""
    org = await seed_org(admin_engine, "push-mirror")
    push_cred = await seed_credential(admin_engine, org)
    remote_cred = await seed_credential(
        admin_engine, org, credential_type="windows_admin"
    )

    async with org_session(org.org_id, engine=app_engine) as session:
        # The ordinary remote-control credential passes straight through.
        await refuse_push_install_credential(
            session, org_id=org.org_id, credential_id=remote_cred
        )
        with pytest.raises(PushValidationError) as exc:
            await refuse_push_install_credential(
                session, org_id=org.org_id, credential_id=push_cred
            )
    assert "dedicated to agent push install" in str(exc.value)


async def test_the_mirror_rule_is_wired_into_the_remote_session_path() -> None:
    """A guard nobody calls is not a guard. `remote/service.py` is the only
    other place in this repository that presents a stored credential to a
    machine, and the refusal must sit BEFORE the vault checkout there -- after
    it, the plaintext has already been fetched."""
    import inspect

    from app.modules.remote import service as remote_service

    source = inspect.getsource(remote_service.RemoteSessionService.start_session)
    assert "refuse_push_install_credential" in source
    guard = source.index("refuse_push_install_credential(")
    checkout = source.index("self._vault.checkout(")
    assert guard < checkout, (
        "the push-credential refusal runs after the vault checkout: the "
        "plaintext has already been decrypted by the time it refuses"
    )


async def test_a_credential_from_another_org_is_not_found_not_forbidden(
    bypassrls_session: AsyncSession, admin_engine: AsyncEngine
) -> None:
    """404 AND NEVER 403. A 403 confirms the id exists, which over
    `credentials` is an existence oracle on the vault's own key space.

    FALSIFIED BY DELETING `Credential.org_id == org_id` from
    `resolve_push_credential`: org A then binds a scope to org B's credential,
    and a later push presents org B's secret on org A's authorization.
    """
    a = await seed_org(admin_engine, "push-cred-a")
    b = await seed_org(admin_engine, "push-cred-b")
    b_cred = await seed_credential(admin_engine, b)

    with pytest.raises(PushNotFoundError):
        await resolve_push_credential(bypassrls_session, a.org_id, b_cred)


async def test_a_credential_that_does_not_exist_is_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await seed_org(admin_engine, "push-cred-missing")
    async with org_session(org.org_id, engine=app_engine) as session:
        with pytest.raises(PushNotFoundError):
            await resolve_push_credential(session, org.org_id, uuid4())


# --------------------------------------------------------------------------- #
# Binding, unbinding, listing
# --------------------------------------------------------------------------- #


async def test_binding_is_audited_with_the_name_and_never_the_secret(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Invariant #3: a credential is never written to an audit metadata dict.
    What an auditor needs is the id and the label a human chose."""
    org = await seed_org(admin_engine, "push-bind-audit")
    credential = await seed_credential(admin_engine, org, name="branch-installer")
    async with org_session(org.org_id, engine=app_engine) as session:
        await bind_scope(
            session,
            org.org_id,
            actor_id=org.user_id,
            credential_id=credential,
            prefix="10.42.0.0/24",
            note="branch onboarding",
        )

    async with AsyncSession(admin_engine) as session:
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org.org_id,
                    AuditEvent.action == "push.credential_scope_bound",
                )
            )
        ).scalar_one()
    assert event.meta["credential_name"] == "branch-installer"
    assert event.meta["prefix"] == "10.42.0.0/24"
    assert event.meta["address_count"] == 256
    assert "secret" not in event.meta
    assert "vault_ref" not in event.meta
    assert "ciphertext" not in event.meta


async def test_binding_the_same_prefix_twice_is_a_conflict(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await seed_org(admin_engine, "push-bind-dupe")
    credential = await seed_credential(admin_engine, org)
    async with org_session(org.org_id, engine=app_engine) as session:
        await bind_scope(
            session,
            org.org_id,
            actor_id=org.user_id,
            credential_id=credential,
            prefix="10.43.0.0/24",
        )
    async with org_session(org.org_id, engine=app_engine) as session:
        with pytest.raises(PushConflictError):
            await bind_scope(
                session,
                org.org_id,
                actor_id=org.user_id,
                credential_id=credential,
                prefix="10.43.0.0/24",
            )


async def test_two_credentials_may_be_bound_to_the_same_prefix(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The uniqueness is per (credential, prefix), not per prefix. Two
    installers for one segment -- one being rotated in -- is an ordinary
    state, and a per-prefix constraint would make rotation impossible."""
    org = await seed_org(admin_engine, "push-bind-two")
    first = await seed_credential(admin_engine, org)
    second = await seed_credential(admin_engine, org)
    for credential in (first, second):
        async with org_session(org.org_id, engine=app_engine) as session:
            await bind_scope(
                session,
                org.org_id,
                actor_id=org.user_id,
                credential_id=credential,
                prefix="10.44.0.0/24",
            )
    async with org_session(org.org_id, engine=app_engine) as session:
        rows = await list_scopes(session, org.org_id)
    assert {r.credential_id for r in rows} == {first, second}


async def test_unbinding_stamps_and_the_row_stays(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """"Which addresses was this credential bound to when it was used in
    March" must survive somebody narrowing the scope, which a DELETE
    destroys."""
    org = await seed_org(admin_engine, "push-unbind")
    credential = await seed_credential(admin_engine, org)
    scope_id = await seed_push_credential_scope(
        admin_engine, org, credential, "10.45.0.0/24"
    )
    async with org_session(org.org_id, engine=app_engine) as session:
        await unbind_scope(
            session, org.org_id, actor_id=org.user_id, scope_id=scope_id
        )

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(PushCredentialScope).where(PushCredentialScope.id == scope_id)
            )
        ).scalar_one()
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org.org_id,
                    AuditEvent.action == "push.credential_scope_unbound",
                )
            )
        ).scalar_one()
    assert row.unbound_at is not None
    assert row.unbound_by == org.user_id
    assert event.meta["prefix"] == "10.45.0.0/24"


async def test_unbinding_twice_is_a_conflict(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await seed_org(admin_engine, "push-unbind2")
    credential = await seed_credential(admin_engine, org)
    scope_id = await seed_push_credential_scope(
        admin_engine, org, credential, "10.46.0.0/24", unbound=True
    )
    async with org_session(org.org_id, engine=app_engine) as session:
        with pytest.raises(PushConflictError):
            await unbind_scope(
                session, org.org_id, actor_id=org.user_id, scope_id=scope_id
            )


async def test_listing_filters_by_credential_and_by_bound_state(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """THREE ROWS, DIFFERING ONLY IN WHAT SHOULD HAPPEN TO THEM."""
    org = await seed_org(admin_engine, "push-scope-list")
    first = await seed_credential(admin_engine, org)
    second = await seed_credential(admin_engine, org)
    await seed_push_credential_scope(admin_engine, org, first, "10.47.0.0/24")
    await seed_push_credential_scope(
        admin_engine, org, first, "10.48.0.0/24", unbound=True
    )
    await seed_push_credential_scope(admin_engine, org, second, "10.49.0.0/24")

    async with org_session(org.org_id, engine=app_engine) as session:
        active = {str(r.prefix) for r in await list_scopes(session, org.org_id)}
        for_first = {
            str(r.prefix)
            for r in await list_scopes(session, org.org_id, credential_id=first)
        }
        everything = {
            str(r.prefix)
            for r in await list_scopes(session, org.org_id, include_unbound=True)
        }
    assert active == {"10.47.0.0/24", "10.49.0.0/24"}
    assert for_first == {"10.47.0.0/24"}
    assert everything == {"10.47.0.0/24", "10.48.0.0/24", "10.49.0.0/24"}


# --------------------------------------------------------------------------- #
# Tenancy, ON THE ADMIN ENGINE
# --------------------------------------------------------------------------- #


async def test_listing_does_not_return_another_orgs_scopes_on_the_admin_engine(
    bypassrls_session: AsyncSession, admin_engine: AsyncEngine
) -> None:
    """FALSIFIED BY DELETING `PushCredentialScope.org_id == org_id` from
    `list_scopes`. Through `org_session` that deletion changes nothing."""
    a = await seed_org(admin_engine, "push-scope-ten-a")
    b = await seed_org(admin_engine, "push-scope-ten-b")
    a_cred = await seed_credential(admin_engine, a)
    b_cred = await seed_credential(admin_engine, b)
    await seed_push_credential_scope(admin_engine, a, a_cred, "10.50.0.0/24")
    await seed_push_credential_scope(admin_engine, b, b_cred, "10.51.0.0/24")

    seen = {str(r.prefix) for r in await list_scopes(bypassrls_session, a.org_id)}
    assert seen == {"10.50.0.0/24"}


async def test_unbinding_another_orgs_scope_is_not_found_on_the_admin_engine(
    bypassrls_session: AsyncSession, admin_engine: AsyncEngine
) -> None:
    """FALSIFIED BY DELETING `PushCredentialScope.org_id == org_id` from
    `unbind_scope`'s lookup: org A then narrows org B's credential scope, a
    silent cross-tenant change to where org B's secret may be presented."""
    a = await seed_org(admin_engine, "push-scope-un-a")
    b = await seed_org(admin_engine, "push-scope-un-b")
    b_cred = await seed_credential(admin_engine, b)
    b_scope = await seed_push_credential_scope(
        admin_engine, b, b_cred, "10.52.0.0/24"
    )

    with pytest.raises(PushNotFoundError):
        await unbind_scope(
            bypassrls_session, a.org_id, actor_id=a.user_id, scope_id=b_scope
        )

    row = (
        await bypassrls_session.execute(
            select(PushCredentialScope).where(PushCredentialScope.id == b_scope)
        )
    ).scalar_one()
    assert row.unbound_at is None, "org A unbound org B's credential scope"
