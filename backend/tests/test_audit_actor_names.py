"""Naming the actor in the audit log (audit
`docs/superpowers/audits/2026-08-21-ux-remediation-closeout.md`, rec. 6 —
finding B64, "the audit log identifies every actor and target by raw UUID
only, you cannot tell who did what").

WHY THE RESPONSE IS ENRICHED RATHER THAN A LOOKUP ROUTE ADDED. The closeout
searched `app/api/v1/` and reported there was no users route; there is one, it
just lives in a module — `GET /api/v1/users` (`app/modules/user_admin/api.py`),
gated on `users.read`. It is the wrong instrument for this page twice over:

  * it is gated on a code the reader of an audit log need not hold. Migration
    0003 gives `auditor` exactly `devices.read`, `jobs.read`, `audit.read` and
    `credentials.read` — NOT `users.read`. So the one role whose entire job is
    reading this log is the one role a client-side lookup would leave staring
    at UUIDs, and closing that by granting `auditor` `users.read` would widen
    a privileged code to fix a display defect;
  * it returns the whole directory — every email, every role assignment. The
    tool-surface census calls that set "precisely the reconnaissance an
    attacker would want first". Resolving forty names on a page of audit
    events should not require handing over the staff list.

WHAT IS STORED DOES NOT MOVE, AND THE CHAIN DOES NOT CARE. `AuditService.
record` hashes `_canonical_fields`, which is built from the columns and does
not include any name; the name is joined at READ time and never written. The
test below asserts both halves — the row's `hash` is byte-identical before and
after, and `GET /audit/verify` still verifies.

A NAME IS THE DIRECTORY'S CURRENT ANSWER, NOT WHAT WAS RECORDED. The log
stores an id on purpose; a person renamed after the fact is still the person
who acted. So the field is additive — `actor_id` stays exactly where it was —
and it is null rather than guessed whenever the directory has no answer.
"""

from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.models import AuditEvent, Organization, User
from app.services.user_names import resolve_display_names


async def _named_user(api_env, org_id: UUID, display_name: str, *roles: str) -> tuple[str, UUID]:
    sub = await api_env.create_user(org_id, *roles)
    async with AsyncSession(api_env.admin_engine) as session:
        await session.execute(
            update(User)
            .where(User.org_id == org_id, User.external_subject == sub)
            .values(display_name=display_name)
        )
        await session.commit()
        user_id = (
            await session.execute(
                select(User.id).where(User.org_id == org_id, User.external_subject == sub)
            )
        ).scalar_one()
    return sub, user_id


async def _record(api_env, org_id: UUID, **kwargs) -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        event = await audit_service.record(session, org_id=org_id, **kwargs)
        await session.commit()
        return event.id


# --------------------------------------------------------------------------- #
# What B64 asked for
# --------------------------------------------------------------------------- #


async def test_the_log_names_the_actor_instead_of_only_its_uuid(api_env) -> None:
    org_id = await api_env.create_org("auditname")
    _, actor_id = await _named_user(api_env, org_id, "Ada Lovelace")
    await _record(
        api_env,
        org_id,
        actor_type="user",
        actor_id=actor_id,
        action="credential.checked_out",
        target_type="credential",
        target_id="db-root",
    )

    auditor = await api_env.create_user(org_id, "auditor")
    headers = await api_env.headers(org_id, auditor)
    body = (await api_env.client.get("/api/v1/audit", headers=headers)).json()
    event = next(e for e in body["items"] if e["action"] == "credential.checked_out")

    assert event["actor_display_name"] == "Ada Lovelace"
    # ADDITIVE. The id is what the log actually stores and it stays.
    assert event["actor_id"] == str(actor_id)


async def test_a_user_shaped_target_is_named_too(api_env) -> None:
    """`target_type="user"` is written by eleven call sites — every
    `user_admin` mutation, the Keycloak event sweep, `PATCH /me` and first-login
    admission — and in every one `target_id` is `str(user.id)`. That is the one
    target type in the schema that resolves to a person, so it is the one that
    is resolved; the rest are hostnames, job ids and codes, which no generic
    resolver could name without inventing a type-to-table map.
    """
    org_id = await api_env.create_org("auditname")
    _, actor_id = await _named_user(api_env, org_id, "Grace Hopper")
    _, target_id = await _named_user(api_env, org_id, "Katherine Johnson")
    await _record(
        api_env,
        org_id,
        actor_type="user",
        actor_id=actor_id,
        action="user.role_assigned",
        target_type="user",
        target_id=str(target_id),
    )

    auditor = await api_env.create_user(org_id, "auditor")
    headers = await api_env.headers(org_id, auditor)
    body = (await api_env.client.get("/api/v1/audit", headers=headers)).json()
    event = next(e for e in body["items"] if e["action"] == "user.role_assigned")

    assert event["actor_display_name"] == "Grace Hopper"
    assert event["target_display_name"] == "Katherine Johnson"


async def test_an_auditor_who_may_not_read_the_directory_still_gets_names(api_env) -> None:
    """The whole reason this is a joined field and not a client-side lookup.
    The same request that is refused `GET /users` comes back with names."""
    org_id = await api_env.create_org("auditname")
    _, actor_id = await _named_user(api_env, org_id, "Radia Perlman")
    await _record(
        api_env,
        org_id,
        actor_type="user",
        actor_id=actor_id,
        action="enrollment.token_created",
        target_type="enrollment_token",
        target_id="t1",
    )

    auditor = await api_env.create_user(org_id, "auditor")
    headers = await api_env.headers(org_id, auditor)

    refused = await api_env.client.get("/api/v1/users", headers=headers)
    assert refused.status_code == 403, "an auditor holding users.read would void this test"

    body = (await api_env.client.get("/api/v1/audit", headers=headers)).json()
    event = next(e for e in body["items"] if e["action"] == "enrollment.token_created")
    assert event["actor_display_name"] == "Radia Perlman"


# --------------------------------------------------------------------------- #
# Where there is no name, nothing is invented
# --------------------------------------------------------------------------- #


async def test_a_non_user_actor_is_unnamed_rather_than_borrowing_one(api_env) -> None:
    """`actor_type` is `user`, `agent` or `system`. Only the first names a row
    in `users`, and a system append carries no `actor_id` at all."""
    org_id = await api_env.create_org("auditname")
    await _record(
        api_env,
        org_id,
        actor_type="system",
        actor_id=None,
        action="retention.applied",
        target_type="org",
        target_id=str(org_id),
    )

    auditor = await api_env.create_user(org_id, "auditor")
    headers = await api_env.headers(org_id, auditor)
    body = (await api_env.client.get("/api/v1/audit", headers=headers)).json()
    event = next(e for e in body["items"] if e["action"] == "retention.applied")

    assert event["actor_id"] is None
    assert event["actor_display_name"] is None
    assert event["target_display_name"] is None


async def test_an_empty_directory_name_is_null_and_never_the_email(api_env) -> None:
    """`users.display_name` defaults to `""`, so "has a row" is not "has a
    name". Falling back to the email address was the obvious filler and is
    refused: an address is the directory route's disclosure, gated on
    `users.read`, and `audit.read` is not that gate. A null sends the console
    back to the id it was already showing, which is a worse label and not a
    leak.
    """
    org_id = await api_env.create_org("auditname")
    sub = await api_env.create_user(org_id)
    async with AsyncSession(api_env.admin_engine) as session:
        user_id = (
            await session.execute(
                select(User.id).where(User.org_id == org_id, User.external_subject == sub)
            )
        ).scalar_one()
    await _record(
        api_env,
        org_id,
        actor_type="user",
        actor_id=user_id,
        action="auth.login",
        target_type="user",
        target_id=str(user_id),
    )

    auditor = await api_env.create_user(org_id, "auditor")
    headers = await api_env.headers(org_id, auditor)
    body = (await api_env.client.get("/api/v1/audit", headers=headers)).json()
    event = next(e for e in body["items"] if e["action"] == "auth.login")

    assert event["actor_display_name"] is None
    whole_page = await api_env.client.get("/api/v1/audit", headers=headers)
    assert f"{sub}@test.local" not in whole_page.text


async def test_a_target_id_that_is_not_a_uuid_does_not_break_the_page(api_env) -> None:
    """`target_id` is a free string — `AuditEvent.target_id` is `str`, and the
    fixture the closeout complained about holds a HOSTNAME. A `target_type` of
    `user` carrying an unparseable id is not a thing any call site writes, but
    the log is append-only and nine days of history cannot be corrected, so the
    resolver has to survive one rather than 500."""
    org_id = await api_env.create_org("auditname")
    await _record(
        api_env,
        org_id,
        actor_type="system",
        actor_id=None,
        action="legacy.import",
        target_type="user",
        target_id="not-a-uuid",
    )

    auditor = await api_env.create_user(org_id, "auditor")
    headers = await api_env.headers(org_id, auditor)
    response = await api_env.client.get("/api/v1/audit", headers=headers)
    assert response.status_code == 200
    event = next(e for e in response.json()["items"] if e["action"] == "legacy.import")
    assert event["target_display_name"] is None


# --------------------------------------------------------------------------- #
# The immutable half: nothing stored moves
# --------------------------------------------------------------------------- #


async def test_naming_the_actor_writes_nothing_and_the_chain_still_verifies(
    api_env,
) -> None:
    """Invariant #4's log is append-only and hash-chained. The name is joined
    at read time from `users`, is not one of `_canonical_fields`' inputs, and
    therefore cannot move a link. Both halves asserted: the stored `hash` is
    unchanged by a read, and `GET /audit/verify` passes after it."""
    org_id = await api_env.create_org("auditname")
    _, actor_id = await _named_user(api_env, org_id, "Barbara Liskov")
    event_id = await _record(
        api_env,
        org_id,
        actor_type="user",
        actor_id=actor_id,
        action="policy.changed",
        target_type="org",
        target_id=str(org_id),
    )

    async with AsyncSession(api_env.admin_engine) as session:
        before = (
            await session.execute(select(AuditEvent.hash).where(AuditEvent.id == event_id))
        ).scalar_one()

    auditor = await api_env.create_user(org_id, "auditor")
    headers = await api_env.headers(org_id, auditor)
    listed = (await api_env.client.get("/api/v1/audit", headers=headers)).json()
    event = next(e for e in listed["items"] if e["id"] == str(event_id))
    assert event["actor_display_name"] == "Barbara Liskov"
    assert event["hash"] == before

    async with AsyncSession(api_env.admin_engine) as session:
        after = (
            await session.execute(select(AuditEvent.hash).where(AuditEvent.id == event_id))
        ).scalar_one()
    assert after == before

    verify = await api_env.client.get("/api/v1/audit/verify", headers=headers)
    assert verify.status_code == 200
    assert verify.json()["ok"] is True


# --------------------------------------------------------------------------- #
# Tenancy, where RLS is not the thing rejecting
# --------------------------------------------------------------------------- #


async def test_a_neighbours_user_id_resolves_to_nothing(admin_engine: AsyncEngine) -> None:
    """The resolver's OWN `org_id` predicate, pinned against an engine that
    ignores RLS.

    Everything the route does runs through `org_session` on a NOBYPASSRLS
    role, where Postgres rejects a cross-tenant row whether or not the query
    asked it to — so deleting `WHERE org_id` here turns no route test red
    (`docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md`). `its_admin`
    carries BYPASSRLS, so below the SQL is the only thing scoping anything.

    The id being asked about is real and belongs to somebody else, which is
    exactly the shape a forgotten predicate leaks: `audit_events.actor_id` is a
    plain UUID column with no cross-org constraint, so nothing in the schema
    stops a row naming a foreign id, and a resolver without the predicate would
    answer it with that tenant's staff name. `mine` is asserted to still
    resolve, so the test cannot pass by resolving nothing at all.
    """
    orgs: dict[str, UUID] = {}
    users: dict[str, UUID] = {}
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        for key in ("mine", "theirs"):
            org = Organization(name=key, slug=f"an-{uuid4().hex[:8]}")
            session.add(org)
            await session.flush()
            orgs[key] = org.id
            user = User(
                org_id=org.id,
                external_subject=f"sub-{uuid4().hex[:10]}",
                email=f"{key}@test.local",
                display_name=f"{key.title()} Person",
            )
            session.add(user)
            await session.flush()
            users[key] = user.id
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        resolved = await resolve_display_names(
            session, org_id=orgs["mine"], user_ids=[users["mine"], users["theirs"]]
        )

    assert resolved == {users["mine"]: "Mine Person"}


async def test_the_resolver_asks_for_nothing_when_there_is_nothing_to_ask(
    admin_engine: AsyncEngine,
) -> None:
    """A page of system-actor events must not issue a `WHERE id IN ()`."""
    async with AsyncSession(admin_engine) as session:
        assert await resolve_display_names(session, org_id=uuid4(), user_ids=[]) == {}
