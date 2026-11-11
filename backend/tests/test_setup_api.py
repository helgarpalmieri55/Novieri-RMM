"""First boot: `GET /api/v1/setup/status` and `POST /api/v1/setup/claim`.

`docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §3, and
falsifier 2: "the setup screen is reachable on a populated instance, or without
the install code. Either is a takeover path."

THESE TWO ROUTES ARE UNGATED BY NECESSITY. They answer before the platform has
a single user, so there is no token to check and no permission to require. What
stands between a stranger and ownership of the fleet is exactly two facts:

    1. the platform has zero users
    2. the caller knows the install code the installer printed

Neither is sufficient alone. Zero-users alone means whoever reaches the URL of a
freshly racked appliance first becomes its administrator. The code alone would
let somebody who once read a log line re-claim a running platform. So the
refusals are tested in both directions -- a wrong code on an EMPTY platform, and
the RIGHT code on a populated one -- because a check that is only ever exercised
when the other one also fails is a check nobody has tested.

A DEDICATED, GENUINELY EMPTY DATABASE PER TEST, and it is not a nicety. The
session's shared cluster has users in it by the time this file runs, so
"claim succeeds on an empty platform" could not be written against it, and
"claim is refused on a populated one" would pass no matter what the route did.
The template is migrated once (~3s) and copied per test (~0.15s), which is what
makes a real empty platform affordable.

EVERY ASSERTION ABOUT WHAT THE DATABASE HOLDS READS THROUGH THE ADMIN
(BYPASSRLS) ENGINE. The claim runs before any tenant exists, so a check that
read through a tenant session would be answered by RLS -- returning "no users"
for a populated platform, which is the exact bug these tests exist to catch.

NO IDENTITY PROVIDER IS CALLED. The fake from `test_user_admin_api` stays on
`httpx` so that a call would be seen; `kc.seen == []` holds it.
"""

import os
import secrets as _secrets
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.audit import audit_service
from app.core.auth.passwords import STAFF_MINIMUM, PasswordRejected
from app.core.client_address import parse_trusted_proxies
from app.core.setup_code import SETUP_CODE_PATH_ENV, ensure_setup_code
from app.models import AuditEvent, Organization, Role, User, UserRole
from app.models.auth import AuthPassword
from tests.auth_support import CONSOLE, browser
from tests.conftest import PgCluster, _psql, _run_migrations
from tests.test_user_admin_api import _REAL_CLIENT, FakeKeycloak

REPO = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO / "deploy" / ".env.example"

# --------------------------------------------------------------------------- #
# an empty platform, per test
# --------------------------------------------------------------------------- #


def _rebase(url: str, dbname: str) -> str:
    return url.rsplit("/", 1)[0] + "/" + dbname


@pytest.fixture(scope="session")
def unclaimed_template(pg_cluster: PgCluster) -> Any:
    """A migrated database with NO users in it, used only as a copy source.

    Depends on `pg_cluster` for the roles and the DSN shape, and for nothing
    else -- in particular not for its data, which is the whole point.
    """
    dbname = f"its_unclaimed_{_secrets.token_hex(4)}"
    _psql(f'CREATE DATABASE "{dbname}" OWNER its_admin')
    _psql(f'GRANT CONNECT ON DATABASE "{dbname}" TO app_user', dbname)
    _psql(f'GRANT CONNECT ON DATABASE "{dbname}" TO portal_user', dbname)
    _run_migrations(
        PgCluster(
            dbname=dbname,
            app_url=_rebase(pg_cluster.app_url, dbname),
            admin_url=_rebase(pg_cluster.admin_url, dbname),
            portal_url=_rebase(pg_cluster.portal_url, dbname),
            app_url_sync=_rebase(pg_cluster.app_url_sync, dbname),
            admin_url_sync=_rebase(pg_cluster.admin_url_sync, dbname),
        )
    )
    # Non-vacuity: if migrations ever seeded a user, every "empty platform"
    # test below would be measuring nothing.
    assert _psql("SELECT count(*) FROM users", dbname) == "0"
    yield dbname
    _psql(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


class SetupEnv:
    def __init__(
        self,
        app: Any,
        client: httpx.AsyncClient,
        settings: Any,
        admin_engine: AsyncEngine,
        kc: FakeKeycloak,
        code_path: Path,
    ) -> None:
        self.app = app
        self.client = client
        self.settings = settings
        self.admin_engine = admin_engine
        self.kc = kc
        self.code_path = code_path

    @property
    def code(self) -> str:
        """The install code this deployment would have printed."""
        return ensure_setup_code(self.code_path)

    async def orgs(self) -> list[Organization]:
        async with AsyncSession(self.admin_engine) as session:
            return list((await session.execute(select(Organization))).scalars())

    async def users(self) -> list[User]:
        async with AsyncSession(self.admin_engine) as session:
            return list((await session.execute(select(User))).scalars())

    async def seed_user(self, email: str = "someone@already.here") -> UUID:
        """A user in some organization, created WITHOUT going through the
        claim -- the "this platform is already somebody's" state."""
        async with AsyncSession(self.admin_engine, expire_on_commit=False) as session:
            org = Organization(name="Existing", slug=f"existing-{uuid4().hex[:8]}")
            session.add(org)
            await session.flush()
            user = User(
                org_id=org.id,
                external_subject=f"sub-{uuid4().hex[:10]}",
                email=email,
                display_name="Already Here",
            )
            session.add(user)
            await session.commit()
            return user.id


@pytest.fixture()
async def setup_env(
    unclaimed_template: str,
    pg_cluster: PgCluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    from app.core.config import Settings
    from app.main import create_app
    from tests.fake_idp import ISSUER, JWKS_URL, FakeIdp, jwks_client

    dbname = f"its_claim_{_secrets.token_hex(4)}"
    _psql(f'CREATE DATABASE "{dbname}" TEMPLATE "{unclaimed_template}" OWNER its_admin')
    # Database-level ACLs live in `pg_database`, not in the copied database, so
    # CONNECT has to be granted again on every copy.
    _psql(f'GRANT CONNECT ON DATABASE "{dbname}" TO app_user', dbname)
    _psql(f'GRANT CONNECT ON DATABASE "{dbname}" TO portal_user', dbname)

    code_path = tmp_path / "secrets" / "setup-code"
    monkeypatch.setenv(SETUP_CODE_PATH_ENV, str(code_path))

    settings = Settings(
        database_url=_rebase(pg_cluster.app_url, dbname),
        database_admin_url=_rebase(pg_cluster.admin_url, dbname),
        database_portal_url=_rebase(pg_cluster.portal_url, dbname),
        oidc_issuer=ISSUER,
        oidc_jwks_url=JWKS_URL,
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        default_org_slug="toeshee",
        # The sign-in test below reaches the per-IP limiter: the suite's Redis
        # database, as `tests/conftest.py::api_env` uses, never the default.
        redis_url=os.environ.get("ITS_TEST_REDIS_URL", "redis://localhost:6379/9"),
    )
    app = create_app(settings, jwks_http_client=jwks_client(FakeIdp()))
    _include_setup_router(app)

    # THE APP'S OWN TWO ENGINES, REPLACED WITH UNPOOLED ONES. `create_app`
    # builds them with `pool_pre_ping=True` and no pool hook -- correct for a
    # long-lived server, and wrong for a fixture that builds a fresh app per
    # test. Swapped here rather than parameterised in production, because the
    # pooling this defeats is exactly what a real deployment wants.
    for attr in ("app_engine", "admin_engine"):
        await getattr(app.state, attr).dispose()
    app.state.app_engine = create_async_engine(
        _rebase(pg_cluster.app_url, dbname), poolclass=NullPool
    )
    app.state.admin_engine = create_async_engine(
        _rebase(pg_cluster.admin_url, dbname), poolclass=NullPool
    )

    kc = FakeKeycloak()

    def factory(**kw: Any) -> httpx.AsyncClient:
        return _REAL_CLIENT(transport=httpx.MockTransport(kc.handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)

    # NullPool, and it is not an optimisation -- it is what stops this file
    # from failing the WHOLE SUITE intermittently.
    #
    # Every test here gets its OWN DATABASE, because a setup test needs a
    # genuinely empty platform and "zero users" cannot be shared between two
    # tests running against one schema. That is right and stays. What is NOT
    # free is the pooling: three engines per test (this one plus the app's two)
    # each holding a default pool of idle connections, across 23 tests, against
    # a Postgres whose `max_connections` is 100. Alone the file passes; inside
    # the full run it exhausted the server and the failure surfaced as
    # `TooManyConnectionsError` in an UNRELATED test, which is the worst shape
    # a resource leak can take -- it accuses the wrong code.
    #
    # NullPool holds nothing open between statements, so the peak is what is
    # actually in flight rather than what has ever been opened.
    admin_engine = create_async_engine(
        _rebase(pg_cluster.admin_url, dbname), poolclass=NullPool
    )
    async with _REAL_CLIENT(
        transport=httpx.ASGITransport(app=app), base_url="http://api.test"
    ) as client:
        yield SetupEnv(app, client, settings, admin_engine, kc, code_path)

    await admin_engine.dispose()
    await app.state.app_engine.dispose()
    await app.state.admin_engine.dispose()
    # The sign-in test opens the app's Redis pool; release it as `api_env` does.
    aclose = getattr(app.state.redis, "aclose", None)
    if aclose is not None:
        await aclose()
    _psql(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


def _include_setup_router(app: Any) -> None:
    """Wire the router if `app/main.py` has not yet.

    This workstream owns `app/api/v1/setup.py` and not `app/main.py`; the
    controller wires the router in afterwards, exactly as `test_user_admin_api`
    describes for its own surface. Including it only when absent means these
    tests pass on both sides of that change and never register it twice.
    """
    from app.api.v1.setup import router as setup_router
    from app.core.router_inspect import flatten

    paths = {path for path, _route in flatten(app.router)}
    if "/api/v1/setup/status" not in paths:
        app.include_router(setup_router, prefix="/api/v1")


def _body(name: str = "Toeshee IT", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": "",
        "email": "owner@toeshee.test",
        "display_name": "The Owner",
        "organization_name": name,
        "password": "Owner-Passphrase-2026",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


async def test_an_unclaimed_platform_says_it_needs_setting_up(setup_env: Any) -> None:
    response = await setup_env.client.get("/api/v1/setup/status")

    assert response.status_code == 200
    assert response.json() == {"needed": True}


async def test_status_never_reveals_the_code(setup_env: Any) -> None:
    """The route that tells an anonymous caller "this box is unclaimed" is one
    HTTP response away from telling them how to claim it.

    Asserted against the RAW BODY and against the code that actually exists, so
    this cannot pass by there being no code to leak.
    """
    response = await setup_env.client.get("/api/v1/setup/status")
    code = setup_env.code

    assert code and code not in response.text
    assert response.json() == {"needed": True}, "the status body grew a field"


async def test_a_platform_with_a_user_needs_no_setup(setup_env: Any) -> None:
    await setup_env.seed_user()

    response = await setup_env.client.get("/api/v1/setup/status")

    assert response.json() == {"needed": False}


async def test_status_still_answers_when_the_code_cannot_be_written(
    setup_env: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secrets volume that failed to mount must not take the setup screen
    down with it -- the operator needs to see the screen to be told anything at
    all. The claim still refuses, because there is no code to match."""
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    monkeypatch.setenv(SETUP_CODE_PATH_ENV, str(blocker / "setup-code"))

    response = await setup_env.client.get("/api/v1/setup/status")

    assert response.status_code == 200
    assert response.json() == {"needed": True}


# --------------------------------------------------------------------------- #
# claim -- the happy path
# --------------------------------------------------------------------------- #


async def test_the_right_code_on_an_empty_platform_claims_it(setup_env: Any) -> None:
    response = await setup_env.client.post(
        "/api/v1/setup/claim", json=_body(code=setup_env.code)
    )

    assert response.status_code == 201, response.text
    payload = response.json()

    orgs = await setup_env.orgs()
    users = await setup_env.users()
    assert [o.name for o in orgs] == ["Toeshee IT"]
    assert [u.email for u in users] == ["owner@toeshee.test"]
    assert payload["organization_id"] == str(orgs[0].id)
    assert payload["user_id"] == str(users[0].id)
    assert users[0].display_name == "The Owner"
    # No identity provider was asked for anything (P2 phase 1): the row's
    # subject is the platform's own placeholder, and the password is stored.
    assert users[0].external_subject.startswith("itms-local:")
    assert setup_env.kc.seen == []
    async with AsyncSession(setup_env.admin_engine) as session:
        assert await session.get(AuthPassword, users[0].id) is not None


async def test_the_claim_closes_the_door_behind_itself(setup_env: Any) -> None:
    await setup_env.client.post("/api/v1/setup/claim", json=_body(code=setup_env.code))

    assert (await setup_env.client.get("/api/v1/setup/status")).json() == {"needed": False}


async def test_the_first_administrator_is_an_org_admin(setup_env: Any) -> None:
    """Not merely "a user". An installation whose only account cannot manage
    users, roles or settings is an installation nobody can administer, and the
    recovery is SQL against production -- which is the runbook step this whole
    cycle exists to delete.
    """
    await setup_env.client.post("/api/v1/setup/claim", json=_body(code=setup_env.code))

    async with AsyncSession(setup_env.admin_engine) as session:
        rows = list(
            (
                await session.execute(
                    select(Role.name, Role.org_id, UserRole.client_id).join(
                        UserRole, UserRole.role_id == Role.id
                    )
                )
            ).all()
        )

    assert [r[0] for r in rows] == ["org-admin"]
    # The seeded platform-wide role, granted on every client (MSP clients spec
    # §2.1): the first administrator must also be able to reach the clients
    # created after them, and the unassigned one.
    assert rows[0][1] is None
    assert rows[0][2] is None


async def test_the_organization_takes_the_slug_this_deployment_signs_in_with(
    setup_env: Any,
) -> None:
    """`ITS_DEFAULT_ORG_SLUG` is what `app/core/security.py` resolves a token to
    when the org claim is absent, which is every token on a single-org install.
    An organization created under any other slug is one the first administrator
    cannot sign in to: `auth_context` answers "unknown organization" and the
    setup screen's whole promise -- claim it, then use it -- breaks at the
    first redirect.
    """
    await setup_env.client.post("/api/v1/setup/claim", json=_body(code=setup_env.code))

    orgs = await setup_env.orgs()
    assert orgs[0].slug == "toeshee"


async def test_the_claim_carries_no_credential_of_any_kind(setup_env: Any) -> None:
    """Invariant 3, and the same scan `test_user_admin_api` runs over its own
    surface: no password enters or leaves this process."""
    response = await setup_env.client.post(
        "/api/v1/setup/claim", json=_body(code=setup_env.code)
    )

    lowered = response.text.lower()
    for word in ("password", "credential", "secret", "token"):
        assert word not in lowered, f"the claim response mentions {word}"


async def test_no_response_on_this_surface_names_the_identity_vendor(
    setup_env: Any,
) -> None:
    """THE BUYER TEST, on the first screen a buyer ever sees.

    The interesting bodies are the FAILURES: a refusal forwarded verbatim from a
    lower layer is how the word would reach the setup screen.
    """
    bodies = [(await setup_env.client.get("/api/v1/setup/status")).text]
    bodies.append(
        (await setup_env.client.post("/api/v1/setup/claim", json=_body(code="WRONG"))).text
    )
    bodies.append(
        (
            await setup_env.client.post(
                "/api/v1/setup/claim", json=_body(code=setup_env.code, password="short")
            )
        ).text
    )
    # And the success, which the old version drove too.
    bodies.append(
        (
            await setup_env.client.post("/api/v1/setup/claim", json=_body(code=setup_env.code))
        ).text
    )

    for body in bodies:
        assert "keycloak" not in body.lower(), body


# --------------------------------------------------------------------------- #
# claim -- the refusals, which are the feature
# --------------------------------------------------------------------------- #


async def test_a_wrong_code_is_refused_and_leaves_nothing_behind(setup_env: Any) -> None:
    response = await setup_env.client.post("/api/v1/setup/claim", json=_body(code="NOTTHECODE"))

    assert response.status_code == 403
    assert await setup_env.orgs() == []
    assert await setup_env.users() == []
    assert setup_env.kc.seen == [], "a refused claim reached the identity provider"


async def test_an_empty_code_is_refused(setup_env: Any) -> None:
    """The blank-field case, spelled out because it is the one an attacker
    tries first and the one a helper substituting a default would let through.
    """
    response = await setup_env.client.post("/api/v1/setup/claim", json=_body(code=""))

    assert response.status_code in (403, 422)
    assert await setup_env.users() == []


async def test_a_populated_platform_refuses_the_right_code(setup_env: Any) -> None:
    """FALSIFIER 2, and the half that a zero-users check alone cannot survive.

    The code here is CORRECT. What refuses the request is that the platform
    already belongs to somebody -- so a leaked install code, a rebuilt
    container, or an operator who ran the setup screen twice cannot mint a
    second administrator nobody asked for.
    """
    await setup_env.seed_user()

    response = await setup_env.client.post(
        "/api/v1/setup/claim", json=_body(code=setup_env.code)
    )

    assert response.status_code == 409
    assert len(await setup_env.users()) == 1
    assert [o.name for o in await setup_env.orgs()] == ["Existing"]


async def test_the_second_submission_of_the_same_form_is_refused(setup_env: Any) -> None:
    """A double-clicked button must not produce two organizations."""
    first = await setup_env.client.post("/api/v1/setup/claim", json=_body(code=setup_env.code))
    second = await setup_env.client.post("/api/v1/setup/claim", json=_body(code=setup_env.code))

    assert first.status_code == 201
    assert second.status_code == 409
    assert len(await setup_env.orgs()) == 1


async def test_a_deleted_user_does_not_reopen_the_setup_screen(setup_env: Any) -> None:
    """`users` rows are RETAINED on delete (`user_admin/service.py` rule 2), so
    "any row at all" is the right question. Counting only active users would
    make deleting the last account -- an ordinary, reversible administrative
    act -- reopen an unauthenticated route that mints an org-admin."""
    user_id = await setup_env.seed_user()
    async with AsyncSession(setup_env.admin_engine) as session:
        row = await session.get(User, user_id)
        assert row is not None
        row.status = "deleted"
        await session.commit()

    assert (await setup_env.client.get("/api/v1/setup/status")).json() == {"needed": False}
    response = await setup_env.client.post(
        "/api/v1/setup/claim", json=_body(code=setup_env.code)
    )
    assert response.status_code == 409


# --------------------------------------------------------------------------- #
# the halves that must not be half-done
# --------------------------------------------------------------------------- #


async def test_the_claim_needs_no_identity_provider_settings(setup_env: Any) -> None:
    """P2 phase 1: there is no provisioning credential to wait for. With no
    `ITS_KEYCLOAK_ADMIN_*` at all the claim makes the owner."""
    assert setup_env.app.state.settings.keycloak_admin_url is None

    response = await setup_env.client.post(
        "/api/v1/setup/claim", json=_body(code=setup_env.code)
    )

    assert response.status_code == 201, response.text
    assert setup_env.kc.seen == []


@pytest.mark.parametrize(
    ("password", "reason"),
    [("Short-1", "too_short"), ("owner@toeshee.test-is-mine", "context")],
)
async def test_a_password_the_rule_refuses_leaves_nothing_behind(
    setup_env: Any, password: str, reason: Literal["too_short", "context"]
) -> None:
    """Design §2.1, checked after the code and before the first write: the
    retry the 422 asks for actually works, and the reason is said (Task 2's
    `PasswordRejected.message`, in the request's language -- English here)."""
    response = await setup_env.client.post(
        "/api/v1/setup/claim", json=_body(code=setup_env.code, password=password)
    )

    assert response.status_code == 422, response.text
    assert response.json() == {
        "code": "password_rejected",
        "detail": PasswordRejected(reason=reason, minimum=STAFF_MINIMUM).message,
    }
    assert await setup_env.orgs() == []
    assert await setup_env.users() == []
    assert (await setup_env.client.get("/api/v1/setup/status")).json() == {"needed": True}


async def test_an_account_that_cannot_be_created_leaves_no_organization(
    setup_env: Any,
) -> None:
    """The org row and the first user are ONE transaction or they are a broken
    install: an address the service refuses rolls the organization back."""
    response = await setup_env.client.post(
        "/api/v1/setup/claim", json=_body(code=setup_env.code, email="not@an@address")
    )

    assert response.status_code == 422
    assert await setup_env.orgs() == []
    assert await setup_env.users() == []
    assert (await setup_env.client.get("/api/v1/setup/status")).json() == {"needed": True}


async def test_the_claim_answers_with_the_way_in_not_an_invitation(setup_env: Any) -> None:
    """The owner chose their password in the form: there is no invitation to
    report, and the answer says what was made and nothing else."""
    response = await setup_env.client.post(
        "/api/v1/setup/claim", json=_body(code=setup_env.code)
    )

    assert response.status_code == 201
    assert set(response.json()) == {"organization_id", "organization_slug", "user_id", "email"}


async def test_the_owner_signs_in_with_the_password_the_claim_set(setup_env: Any) -> None:
    await setup_env.client.post("/api/v1/setup/claim", json=_body(code=setup_env.code))
    setup_env.settings.public_console_url = CONSOLE
    setup_env.app.state.trusted_proxies = parse_trusted_proxies("127.0.0.1/32")

    response = await setup_env.client.post(
        "/api/v1/auth/login",
        headers=browser(),
        json={"email": "owner@toeshee.test", "password": "Owner-Passphrase-2026"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"state": "enroll_required"}


# --------------------------------------------------------------------------- #
# one way to create an administrator, not two
# --------------------------------------------------------------------------- #


async def test_the_claim_goes_through_the_same_service_the_users_tab_uses(
    setup_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FALSIFIER 6. Two ways of creating an administrator is two sets of rails.

    `user_admin.service.create_user` is where the domain rule, the role rails
    and the audit record live. A claim route that wrote its own `User(...)`
    would pass every other test in this file and quietly skip all three.

    The spy DELEGATES, so the claim still really happens and the assertions
    about its result stay true.
    """
    from app.modules.user_admin import service as user_admin_service

    real = user_admin_service.create_user
    seen: list[dict[str, Any]] = []

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return await real(*args, **kwargs)

    monkeypatch.setattr(user_admin_service, "create_user", spy)

    response = await setup_env.client.post(
        "/api/v1/setup/claim", json=_body(code=setup_env.code)
    )

    assert response.status_code == 201
    assert len(seen) == 1, "the claim did not go through the shared create path"
    assert seen[0]["email"] == "owner@toeshee.test"
    assert seen[0]["display_name"] == "The Owner"
    assert [g.client_id for g in seen[0]["grants"]] == [None]


def test_the_claim_route_does_not_build_users_of_its_own() -> None:
    """The structural half of the assertion above.

    A spy proves the shared function was called; it does not prove nothing else
    was. This reads the source and refuses the two ways the surfaces would
    drift: a hand-built `User` row, or a direct call to the identity client.
    """
    source = Path("app/api/v1/setup.py").read_text()

    assert "User(" not in source, "the claim route builds a user row of its own"
    assert "httpx" not in source, "the claim route talks to the identity provider itself"
    assert "kc_client.create_user" not in source, (
        "the claim route reaches past the service layer into the identity client"
    )
    assert "AuthPassword(" not in source, (
        "the claim route stores a password of its own instead of `staff_password.store`"
    )


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #


async def test_everything_the_claim_did_is_in_the_hash_chain(setup_env: Any) -> None:
    """Invariant 4. The first administrator of a fleet appearing from nowhere,
    with no record of who asked for it, is the one event this log most needs.
    """
    await setup_env.client.post("/api/v1/setup/claim", json=_body(code=setup_env.code))

    org = (await setup_env.orgs())[0]
    async with AsyncSession(setup_env.admin_engine) as session:
        events = list(
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.org_id == org.id).order_by(AuditEvent.id)
                )
            ).scalars()
        )
        result = await audit_service.verify(session, org.id)

    assert result.ok and result.count == len(events)
    # The org's default health baseline is seeded first, in the same chain.
    assert [e.action for e in events] == [
        "health.profile_created",
        "health.checks_replaced",
        "health.assignment_created",
        "health.settings_updated",
        "health.baseline_seeded",
        "user.created",
        "auth.password_changed",
        "setup.claimed",
    ]
    claimed = events[-1]
    assert claimed.actor_type == "system" and claimed.actor_id is None
    assert claimed.target_type == "organization" and claimed.target_id == str(org.id)


async def test_the_claim_seeds_the_default_health_baseline(setup_env: Any) -> None:
    from app.models import HealthBaselineSeed, HealthProfile, HealthProfileAssignment

    await setup_env.client.post("/api/v1/setup/claim", json=_body(code=setup_env.code))

    org = (await setup_env.orgs())[0]
    async with AsyncSession(setup_env.admin_engine) as session:
        profiles = list(
            (
                await session.execute(
                    select(HealthProfile).where(HealthProfile.org_id == org.id)
                )
            ).scalars()
        )
        assignment = (
            await session.execute(
                select(HealthProfileAssignment).where(HealthProfileAssignment.org_id == org.id)
            )
        ).scalar_one()
        marker = (
            await session.execute(
                select(HealthBaselineSeed).where(HealthBaselineSeed.org_id == org.id)
            )
        ).scalar_one()
    assert [p.name for p in profiles] == ["Workstation baseline"]
    assert assignment.profile_id == profiles[0].id
    assert assignment.device_id is None and assignment.device_group_id is None
    assert marker.outcome == "seeded"


async def test_the_install_code_is_never_written_to_the_audit_log(setup_env: Any) -> None:
    code = setup_env.code
    await setup_env.client.post("/api/v1/setup/claim", json=_body(code=code))

    async with AsyncSession(setup_env.admin_engine) as session:
        events = list((await session.execute(select(AuditEvent))).scalars())

    assert events
    for event in events:
        assert code not in str(event.meta), "the install code is in the audit metadata"


async def test_a_refused_claim_writes_no_audit_event(setup_env: Any) -> None:
    """A log saying an organization was claimed when it was not is worse than
    no log: the refusals here are the ones an operator would investigate."""
    await setup_env.client.post("/api/v1/setup/claim", json=_body(code="NOTTHECODE"))

    async with AsyncSession(setup_env.admin_engine) as session:
        count = (await session.execute(select(func.count()).select_from(AuditEvent))).scalar_one()

    assert count == 0


# --------------------------------------------------------------------------- #
# the shipped defaults, which are the only ones this route ever runs on
# --------------------------------------------------------------------------- #


def _shipped_env() -> dict[str, str]:
    """`deploy/.env.example` as an operator's `.env` starts out.

    A COMMENTED-OUT LINE IS NOT A SETTING, and that distinction is the whole
    point of reading the file this way rather than grepping it: a value the
    operator has to find and uncomment is a value a fresh installation does not
    have.
    """
    values: dict[str, str] = {}
    for raw in ENV_EXAMPLE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def test_the_shipped_defaults_name_the_organization_the_console_signs_in_to() -> None:
    """SETUP RUNS ONCE, ON THE SHIPPED DEFAULTS, and then the owner signs in.
    `POST /auth/login` looks the address up in `ITS_DEFAULT_ORG_SLUG`'s
    organization (P2 phase 1 plan decision 5), and the claim names the
    organization with that same slug (`_org_slug`); a `.env.example` without
    it ships a product whose owner cannot sign in after setting it up."""
    env = _shipped_env()
    assert env.get("ITS_DEFAULT_ORG_SLUG"), (
        "ITS_DEFAULT_ORG_SLUG ships empty: the first administrator could not sign in"
    )


async def test_a_refused_claim_answers_in_the_requests_language_and_logs_nothing(
    setup_env: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The refusals left are about the request, so they are said in its
    language; none of them is a server fault worth a log line."""
    import logging

    with caplog.at_level(logging.WARNING, logger="app.api.v1.setup"):
        response = await setup_env.client.post(
            "/api/v1/setup/claim",
            json=_body(code=setup_env.code, password="short"),
            headers={"Accept-Language": "es"},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "Usa al menos 12 caracteres."
    assert [r for r in caplog.records if r.name == "app.api.v1.setup"] == []


async def test_an_over_long_password_is_refused_without_repeating_it(setup_env: Any) -> None:
    """P2 Task 7's decision 16: `SetupClaim.password` has no Pydantic bound,
    because a field failure repeats its INPUT in the 422 and the input is the
    owner's password. The rule's `too_long` refuses it after the install code,
    the body never repeats it, and nothing is made. A `max_length` back on the
    field turns this red."""
    long_password = "Owner-Passphrase-2026" * 100

    response = await setup_env.client.post(
        "/api/v1/setup/claim", json=_body(code=setup_env.code, password=long_password)
    )

    assert response.status_code == 422, response.text
    assert response.json() == {
        "code": "password_rejected",
        "detail": PasswordRejected(reason="too_long", minimum=STAFF_MINIMUM).message,
    }
    assert long_password not in response.text
    assert await setup_env.orgs() == []
    assert await setup_env.users() == []


async def test_a_password_built_on_the_organizations_name_is_refused(setup_env: Any) -> None:
    """Task 2's decision 5: the organization being set up is context. Drop
    `org_name=` from `claim_platform` and this password -- 16 characters, on
    no list, built on nothing but that name -- is taken: red."""
    response = await setup_env.client.post(
        "/api/v1/setup/claim",
        json=_body(name="Quokkaworks Ltd", code=setup_env.code, password="Quokkaworks 2026"),
    )

    assert response.status_code == 422, response.text
    assert response.json() == {
        "code": "password_rejected",
        "detail": PasswordRejected(reason="context", minimum=STAFF_MINIMUM).message,
    }
    assert await setup_env.orgs() == []


async def test_the_password_rule_is_not_learnt_without_the_install_code(
    setup_env: Any,
) -> None:
    """The order of the refusals (`claim_platform`'s docstring): the install
    code first, so a caller without it never learns even the password rule. A
    weak password with a wrong code is the 403, not the 422."""
    response = await setup_env.client.post(
        "/api/v1/setup/claim", json=_body(code="NOTTHECODE", password="short")
    )

    assert response.status_code == 403, response.text
    assert "password_rejected" not in response.text
