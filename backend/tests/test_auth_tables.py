"""Migration 0245: the identity core's tables and the two reads before the
organization is known (own authentication design §6.1, §6.2; plan Task 1).

What is held here, each by a test that goes red when the protection goes:

- every auth table (and `account_mail_settings`, plan Task 3) is
  tenant-scoped, and with no organization pinned the application role reads
  NOTHING from any of them (invariant 5);
- a technician's rows are global (client NULL) and a contact's are its
  client's, copied by the trigger -- a narrowed scope reads the first, never
  changes it, and never sees another client's contact (invariant 5a);
- `its_auth_principal` and `its_auth_session` DO read before the organization
  is known, answer only their few columns, are SECURITY DEFINER with a pinned
  `search_path`, and only `app_user` may call them;
- they are, with the four that existed before, the whole reviewed list of
  SECURITY DEFINER functions;
- two technicians of one organization never share an address (D2);
- the stored secrets have the shape of a hash, the settings stay in bounds,
  and a session cannot name another person's remembered device.

The rows are seeded through the BYPASSRLS admin engine; every read that
matters runs as `app_user`, the role every request uses.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    AccountMailSettings,
    AuthActionToken,
    AuthFailure,
    AuthOrgSettings,
    AuthPasskey,
    AuthPassword,
    AuthRecoveryCode,
    AuthSession,
    AuthTotp,
    AuthTrustedDevice,
    AuthWebauthnChallenge,
    Client,
    Organization,
    User,
)

AUTH_TABLES = (
    "auth_passwords",
    "auth_totp",
    "auth_passkeys",
    "auth_recovery_codes",
    "auth_trusted_devices",
    "auth_sessions",
    "auth_failures",
    "auth_action_tokens",
    "auth_webauthn_challenges",
    "auth_org_settings",
    "account_mail_settings",
)

SESSION_COLUMNS = [
    "session_id",
    "user_id",
    "org_id",
    "kind",
    "status",
    "last_seen_at",
    "expires_at",
    "factor_at",
    "step_up_at",
    "ended_at",
]


def _digest() -> bytes:
    return hashlib.sha256(secrets.token_bytes(32)).digest()


def _later(hours: int = 1) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)


async def _org(admin_engine: AsyncEngine) -> tuple[UUID, str]:
    slug = f"auth-{uuid4().hex[:8]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.commit()
        return org.id, slug


async def _customer(admin_engine: AsyncEngine, org: UUID) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        client = Client(org_id=org, name=f"Cliente {uuid4().hex[:6]}", kind="customer")
        s.add(client)
        await s.commit()
        return client.id


async def _person(
    admin_engine: AsyncEngine, org: UUID, email: str, *, client: UUID | None = None
) -> UUID:
    """A technician, or a contact of `client`."""
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        user = User(
            org_id=org,
            external_subject=uuid4().hex,
            email=email,
            kind="contact" if client else "staff",
            client_id=client,
        )
        s.add(user)
        await s.commit()
        return user.id


async def _add(admin_engine: AsyncEngine, *rows: Any) -> None:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        s.add_all(rows)
        await s.commit()


async def _session(
    admin_engine: AsyncEngine, org: UUID, user: UUID, *, kind: str = "staff"
) -> tuple[AuthSession, bytes]:
    token_hash = _digest()
    row = AuthSession(
        org_id=org, user_id=user, token_hash=token_hash, kind=kind, expires_at=_later(12)
    )
    await _add(admin_engine, row)
    return row, token_hash


def _one_of_each(org: UUID, user: UUID) -> list[Any]:
    """A row in every table of 0245, for one person of `org`."""
    device = AuthTrustedDevice(org_id=org, user_id=user, token_hash=_digest(), expires_at=_later())
    return [
        AuthPassword(org_id=org, user_id=user, hash="$argon2id$x", pepper_version=1),
        AuthTotp(org_id=org, user_id=user, secret_sealed=b"sealed"),
        AuthPasskey(
            org_id=org,
            user_id=user,
            credential_id=secrets.token_bytes(16),
            public_key=b"cose",
            backup_eligible=True,
            backup_state=False,
            name="Portátil",
        ),
        AuthRecoveryCode(org_id=org, user_id=user, code_hash=_digest()),
        device,
        AuthSession(
            org_id=org, user_id=user, token_hash=_digest(), kind="staff", expires_at=_later()
        ),
        AuthFailure(
            org_id=org, user_id=user, factor="password", consecutive=1,
            last_failed_at=datetime.now(UTC),
        ),
        AuthActionToken(
            org_id=org, user_id=user, purpose="reset", token_hash=_digest(), expires_at=_later()
        ),
        AuthWebauthnChallenge(
            org_id=org, user_id=user, purpose="register", challenge=secrets.token_bytes(32),
            expires_at=_later(),
        ),
        AuthOrgSettings(org_id=org),
        AccountMailSettings(
            org_id=org, host="smtp.acme.test", port=587, from_address="it@acme.test",
            encryption="starttls",
        ),
    ]


async def test_with_no_organization_pinned_no_auth_table_shows_a_row(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Forced `tenant_isolation` on all eleven tables: before the organization is
    known, the application role reads nothing -- which is why the two
    functions below exist at all."""
    org, _ = await _org(admin_engine)
    user = await _person(admin_engine, org, f"t-{uuid4().hex[:8]}@acme.test")
    await _add(admin_engine, *_one_of_each(org, user))

    async with app_engine.connect() as conn:
        for table in AUTH_TABLES:
            count = (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
            assert count == 0, table


async def test_an_organization_reads_only_its_own_auth_rows(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, _ = await _org(admin_engine)
    org_b, _ = await _org(admin_engine)
    for org in (org_a, org_b):
        user = await _person(admin_engine, org, f"t-{uuid4().hex[:8]}@acme.test")
        await _add(admin_engine, *_one_of_each(org, user))

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        for table in AUTH_TABLES:
            orgs = set((await s.execute(text(f"SELECT DISTINCT org_id FROM {table}"))).scalars())
            assert orgs == {org_a}, table


async def test_a_technicians_rows_are_global_and_a_contacts_are_its_clients(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The trigger copies `users.client_id`; the O policies of 0231 do the
    rest. A technician limited to client Y reads a technician's session (a
    global row) but cannot change it, and never sees X's contact's."""
    org, _ = await _org(admin_engine)
    x, y = await _customer(admin_engine, org), await _customer(admin_engine, org)
    staff = await _person(admin_engine, org, f"s-{uuid4().hex[:8]}@acme.test")
    contact = await _person(admin_engine, org, f"c-{uuid4().hex[:8]}@x.test", client=x)
    staff_row, _ = await _session(admin_engine, org, staff)
    contact_row, _ = await _session(admin_engine, org, contact, kind="contact")

    assert staff_row.client_id is None
    assert contact_row.client_id == x

    def ids(s: AsyncSession) -> Any:
        return s.execute(text("SELECT id FROM auth_sessions"))

    async with org_session(org, engine=app_engine, clients=ClientScope.only([y])) as s:
        assert set((await ids(s)).scalars()) == {staff_row.id}
        changed = await s.execute(
            text("UPDATE auth_sessions SET ip = 'x' WHERE id = :id"), {"id": staff_row.id}
        )
        assert changed.rowcount == 0  # type: ignore[attr-defined]
    async with org_session(org, engine=app_engine, clients=ClientScope.only([x])) as s:
        assert set((await ids(s)).scalars()) == {staff_row.id, contact_row.id}
    async with org_session(org, engine=app_engine, clients=ClientScope.only([])) as s:
        assert set((await ids(s)).scalars()) == set()
    async with org_session(org, engine=app_engine, clients=ClientScope.all()) as s:
        changed = await s.execute(
            text("UPDATE auth_sessions SET ip = 'x' WHERE id = :id"), {"id": staff_row.id}
        )
        assert changed.rowcount == 1  # type: ignore[attr-defined]


async def test_the_door_finds_a_session_before_the_organization_is_known(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`its_auth_session` as `app_user` on a connection with NO GUC at all --
    the state of a request whose cookie has not been resolved yet. As SECURITY
    INVOKER it would find nothing (the first test)."""
    org, _ = await _org(admin_engine)
    user = await _person(admin_engine, org, f"t-{uuid4().hex[:8]}@acme.test")
    row, token_hash = await _session(admin_engine, org, user)

    async with app_engine.connect() as conn:
        found = await conn.execute(
            text("SELECT * FROM its_auth_session(:h)"), {"h": token_hash}
        )
        assert list(found.keys()) == SESSION_COLUMNS
        got = found.mappings().one()
        assert (got["session_id"], got["user_id"], got["org_id"]) == (row.id, user, org)
        assert (got["kind"], got["status"], got["ended_at"], got["factor_at"]) == (
            "staff", "active", None, None,
        )
        nothing = await conn.execute(
            text("SELECT * FROM its_auth_session(:h)"), {"h": _digest()}
        )
        assert nothing.all() == []


async def test_a_session_whose_kind_is_not_its_persons_is_no_session(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A contact's row marked `staff` would be a contact admitted at the
    console's door; the function answers it as no session at all."""
    org, _ = await _org(admin_engine)
    client = await _customer(admin_engine, org)
    contact = await _person(admin_engine, org, f"c-{uuid4().hex[:8]}@x.test", client=client)
    _, mislabelled = await _session(admin_engine, org, contact, kind="staff")
    _, labelled = await _session(admin_engine, org, contact, kind="contact")

    async with app_engine.connect() as conn:
        call = text("SELECT kind FROM its_auth_session(:h)")
        assert (await conn.execute(call, {"h": mislabelled})).all() == []
        assert (await conn.execute(call, {"h": labelled})).scalars().all() == ["contact"]


async def test_the_principal_is_found_by_organization_and_address_alone(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, slug_a = await _org(admin_engine)
    org_b, slug_b = await _org(admin_engine)
    ana_a = await _person(admin_engine, org_a, "Ana@Acme.test")
    ana_b = await _person(admin_engine, org_b, "ana@acme.test")
    client = await _customer(admin_engine, org_a)
    lucia = await _person(admin_engine, org_a, "lucia@cliente.test", client=client)

    async with app_engine.connect() as conn:

        async def principal(slug: str, email: str) -> list[tuple[Any, ...]]:
            result = await conn.execute(
                text("SELECT * FROM its_auth_principal(:slug, :email)"),
                {"slug": slug, "email": email},
            )
            # Four columns: never the address, a hash or a secret.
            assert list(result.keys()) == ["user_id", "org_id", "kind", "status"]
            return [tuple(r) for r in result.all()]

        assert await principal(slug_a, "ANA@acme.TEST") == [(ana_a, org_a, "staff", "active")]
        assert await principal(slug_b, "Ana@Acme.test") == [(ana_b, org_b, "staff", "active")]
        assert await principal(slug_a, "lucia@cliente.test") == [
            (lucia, org_a, "contact", "active")
        ]
        assert await principal(slug_a, "nadie@acme.test") == []
        assert await principal("no-such-org", "ana@acme.test") == []


async def test_a_technician_and_a_contact_sharing_an_address_come_staff_first(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """0232 refuses a contact whose address another user holds, but not a
    technician created after the contact: the function answers both, staff
    first, and the caller picks by kind."""
    org, slug = await _org(admin_engine)
    client = await _customer(admin_engine, org)
    contact = await _person(admin_engine, org, "shared@acme.test", client=client)
    staff = await _person(admin_engine, org, "shared@acme.test")

    async with app_engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT user_id, kind FROM its_auth_principal(:s, :e)"),
                {"s": slug, "e": "shared@acme.test"},
            )
        ).all()
    assert [tuple(r) for r in rows] == [(staff, "staff"), (contact, "contact")]


@pytest.mark.parametrize("signature", ["its_auth_principal(text,text)", "its_auth_session(bytea)"])
async def test_only_the_application_role_may_call_them(
    signature: str, admin_engine: AsyncEngine, portal_engine: AsyncEngine
) -> None:
    async with admin_engine.connect() as conn:
        fn = (
            await conn.execute(
                text(
                    "SELECT p.prosecdef, pg_get_userbyid(p.proowner) AS owner, p.proconfig, "
                    "has_function_privilege('app_user', p.oid, 'EXECUTE') AS app, "
                    "has_function_privilege('portal_user', p.oid, 'EXECUTE') AS portal, "
                    "has_function_privilege('public', p.oid, 'EXECUTE') AS anyone "
                    "FROM pg_proc p WHERE p.oid = to_regprocedure(:sig)"
                ),
                {"sig": signature},
            )
        ).one()
    assert (fn.prosecdef, fn.owner) == (True, "its_admin")
    assert fn.proconfig == ["search_path=public, pg_temp"]
    assert (fn.app, fn.portal, fn.anyone) == (True, False, False)

    call = (
        "SELECT * FROM its_auth_principal('x', 'y')"
        if signature.startswith("its_auth_principal")
        else "SELECT * FROM its_auth_session('\\x00'::bytea)"
    )
    with pytest.raises(DBAPIError, match="permission denied"):
        async with portal_engine.connect() as conn:
            await conn.execute(text(call))


#: Every SECURITY DEFINER function in the schema, each a reviewed decision.
#: A new one -- above all a new read before the organization is known -- has
#: to be added here with its reason (design §6.2: "queda en la lista revisada").
REVIEWED_SECURITY_DEFINER = {
    # 0065: the one write path into role_permissions, which validates first.
    "set_role_permissions",
    # 0230/0233: creates an organization's internal and unassigned clients.
    "its_org_system_clients",
    # 0232: uniqueness checks that must see every client's rows.
    "its_client_domains_unique",
    "its_contact_email_unique",
    # 0245: the two reads before the organization is known.
    "its_auth_principal",
    "its_auth_session",
}


async def test_the_security_definer_functions_are_the_reviewed_list(
    admin_engine: AsyncEngine,
) -> None:
    async with admin_engine.connect() as conn:
        found = set(
            (
                await conn.execute(
                    text(
                        "SELECT proname FROM pg_proc "
                        "WHERE pronamespace = 'public'::regnamespace AND prosecdef"
                    )
                )
            ).scalars()
        )
    assert found == REVIEWED_SECURITY_DEFINER


async def test_two_technicians_of_one_organization_never_share_an_address(
    admin_engine: AsyncEngine,
) -> None:
    org_a, _ = await _org(admin_engine)
    org_b, _ = await _org(admin_engine)
    await _person(admin_engine, org_a, "dup@acme.test")

    with pytest.raises(IntegrityError, match="ux_users_org_lower_email_staff"):
        await _person(admin_engine, org_a, "DUP@acme.test")
    # Another organization's technician may hold it; so may a contact (0232
    # holds contacts) created before the technician.
    await _person(admin_engine, org_b, "dup@acme.test")
    client = await _customer(admin_engine, org_a)
    await _person(admin_engine, org_a, "first@acme.test", client=client)
    await _person(admin_engine, org_a, "first@acme.test")


async def test_the_session_settings_default_and_stay_in_bounds(
    admin_engine: AsyncEngine,
) -> None:
    org, _ = await _org(admin_engine)
    # A raw INSERT, so the values come from the migration's server_defaults
    # and not from the ORM's `default=`.
    async with admin_engine.begin() as conn:
        got = (
            await conn.execute(
                text(
                    "INSERT INTO auth_org_settings (org_id) VALUES (:o) RETURNING "
                    "staff_idle_minutes, staff_max_hours, remember_device_enabled, "
                    "remember_device_days, remembered_max_hours, contact_idle_minutes, "
                    "contact_max_days"
                ),
                {"o": org},
            )
        ).one()
    assert tuple(got) == (30, 12, True, 15, 168, 60, 7)


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ({"staff_idle_minutes": 4}, "ck_auth_org_settings_staff_idle"),
        ({"staff_idle_minutes": 481, "staff_max_hours": 24}, "ck_auth_org_settings_staff_idle"),
        ({"staff_max_hours": 25}, "ck_auth_org_settings_staff_max"),
        ({"remember_device_days": 0}, "ck_auth_org_settings_remember_days"),
        ({"remember_device_days": 31}, "ck_auth_org_settings_remember_days"),
        ({"remembered_max_hours": 0}, "ck_auth_org_settings_remembered_max"),
        ({"remembered_max_hours": 721}, "ck_auth_org_settings_remembered_max"),
        ({"contact_idle_minutes": 4}, "ck_auth_org_settings_contact_idle"),
        ({"contact_idle_minutes": 481}, "ck_auth_org_settings_contact_idle"),
        ({"contact_max_days": 0}, "ck_auth_org_settings_contact_max"),
        ({"contact_max_days": 31}, "ck_auth_org_settings_contact_max"),
        (
            {"staff_idle_minutes": 120, "staff_max_hours": 1},
            "ck_auth_org_settings_staff_idle_within_max",
        ),
    ],
)
async def test_a_setting_out_of_bounds_is_refused(
    values: dict[str, int], constraint: str, admin_engine: AsyncEngine
) -> None:
    org, _ = await _org(admin_engine)
    with pytest.raises(IntegrityError, match=constraint):
        await _add(admin_engine, AuthOrgSettings(org_id=org, **values))


async def test_a_session_cannot_name_another_persons_device(admin_engine: AsyncEngine) -> None:
    org, _ = await _org(admin_engine)
    ana = await _person(admin_engine, org, f"a-{uuid4().hex[:8]}@acme.test")
    bea = await _person(admin_engine, org, f"b-{uuid4().hex[:8]}@acme.test")
    device = AuthTrustedDevice(org_id=org, user_id=ana, token_hash=_digest(), expires_at=_later())
    await _add(admin_engine, device)

    def session(user: UUID) -> AuthSession:
        return AuthSession(
            org_id=org, user_id=user, token_hash=_digest(), kind="staff",
            expires_at=_later(), trusted_device_id=device.id,
        )

    await _add(admin_engine, session(ana))
    with pytest.raises(IntegrityError, match="fk_auth_sessions_device_user"):
        await _add(admin_engine, session(bea))


@pytest.mark.parametrize(
    ("make", "constraint"),
    [
        (
            lambda org, user: AuthSession(
                org_id=org, user_id=user, token_hash=b"\x00" * 31, kind="staff",
                expires_at=_later(),
            ),
            "ck_auth_sessions_token_hash",
        ),
        (
            lambda org, user: AuthSession(
                org_id=org, user_id=user, token_hash=_digest(), kind="robot",
                expires_at=_later(),
            ),
            "ck_auth_sessions_kind",
        ),
        (
            lambda org, user: AuthSession(
                org_id=org, user_id=user, token_hash=_digest(), kind="staff",
                expires_at=_later(), ended_at=datetime.now(UTC),
            ),
            "ck_auth_sessions_ended",
        ),
        (
            lambda org, user: AuthTrustedDevice(
                org_id=org, user_id=user, token_hash=b"\x00" * 16, expires_at=_later()
            ),
            "ck_auth_trusted_devices_token_hash",
        ),
        (
            lambda org, user: AuthActionToken(
                org_id=org, user_id=user, purpose="reset", token_hash=b"\x00" * 33,
                expires_at=_later(),
            ),
            "ck_auth_action_tokens_token_hash",
        ),
        (
            lambda org, user: AuthActionToken(
                org_id=org, user_id=user, purpose="login", token_hash=_digest(),
                expires_at=_later(),
            ),
            "ck_auth_action_tokens_purpose",
        ),
        (
            lambda org, user: AuthRecoveryCode(org_id=org, user_id=user, code_hash=b"code"),
            "ck_auth_recovery_codes_code_hash",
        ),
        (
            lambda org, user: AuthRecoveryCode(
                org_id=org, user_id=user, code_hash=b"\x00" * 257
            ),
            "ck_auth_recovery_codes_code_hash",
        ),
        (
            lambda org, user: AuthPassword(
                org_id=org, user_id=user, hash="$argon2id$x", pepper_version=0
            ),
            "ck_auth_passwords_pepper_version",
        ),
        *[
            (
                lambda org, user, bad=bad: AuthPasskey(
                    **({
                        "org_id": org, "user_id": user,
                        "credential_id": secrets.token_bytes(16), "public_key": b"cose",
                        "backup_eligible": True, "backup_state": False, "name": "Llave",
                    } | bad)
                ),
                constraint,
            )
            for bad, constraint in (
                ({"name": ""}, "ck_auth_passkeys_name"),
                ({"name": "x" * 101}, "ck_auth_passkeys_name"),
                ({"sign_count": -1}, "ck_auth_passkeys_sign_count"),
            )
        ],
        (
            lambda org, user: AuthFailure(
                org_id=org, user_id=user, factor="password", consecutive=-1,
                last_failed_at=datetime.now(UTC),
            ),
            "ck_auth_failures_consecutive",
        ),
        (
            lambda org, user: AuthActionToken(
                org_id=org, user_id=user, purpose="reset", token_hash=_digest(),
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            ),
            "ck_auth_action_tokens_expiry",
        ),
        (
            lambda org, user: AuthWebauthnChallenge(
                org_id=org, user_id=user, purpose="login", challenge=secrets.token_bytes(32),
                expires_at=_later(),
            ),
            "ck_auth_webauthn_challenges_purpose",
        ),
        (
            lambda org, user: AuthWebauthnChallenge(
                org_id=org, user_id=user, purpose="register", challenge=b"\x00" * 15,
                expires_at=_later(),
            ),
            "ck_auth_webauthn_challenges_challenge",
        ),
        (
            lambda org, user: AuthWebauthnChallenge(
                org_id=org, user_id=None, purpose="step_up", challenge=secrets.token_bytes(32),
                expires_at=_later(),
            ),
            "ck_auth_webauthn_challenges_person",
        ),
        (
            lambda org, user: AuthFailure(
                org_id=org, user_id=user, factor="sms", last_failed_at=datetime.now(UTC)
            ),
            "ck_auth_failures_factor",
        ),
        *[
            (
                lambda org, user, bad=bad: AccountMailSettings(
                    **({
                        "org_id": org, "host": "smtp.acme.test", "port": 587,
                        "from_address": "it@acme.test", "encryption": "starttls",
                    } | bad)
                ),
                constraint,
            )
            for bad, constraint in (
                ({"host": ""}, "ck_account_mail_host"),
                ({"port": 0}, "ck_account_mail_port"),
                ({"port": 65536}, "ck_account_mail_port"),
                ({"encryption": "tls"}, "ck_account_mail_encryption"),
                ({"auth_type": "oauth"}, "ck_account_mail_auth_type"),
            )
        ],
    ],
)
async def test_a_row_of_the_wrong_shape_is_refused(
    make: Any, constraint: str, admin_engine: AsyncEngine
) -> None:
    org, _ = await _org(admin_engine)
    user = await _person(admin_engine, org, f"t-{uuid4().hex[:8]}@acme.test")
    with pytest.raises(IntegrityError, match=constraint):
        await _add(admin_engine, make(org, user))


@pytest.mark.parametrize(
    ("make", "constraint"),
    [
        (
            lambda org, user, h: AuthSession(
                org_id=org, user_id=user, token_hash=h, kind="staff", expires_at=_later()
            ),
            "uq_auth_sessions_token_hash",
        ),
        (
            lambda org, user, h: AuthTrustedDevice(
                org_id=org, user_id=user, token_hash=h, expires_at=_later()
            ),
            "uq_auth_trusted_devices_token_hash",
        ),
        (
            lambda org, user, h: AuthActionToken(
                org_id=org, user_id=user, purpose="reset", token_hash=h, expires_at=_later()
            ),
            "uq_auth_action_tokens_token_hash",
        ),
    ],
)
async def test_a_second_row_with_the_same_token_hash_is_refused(
    make: Any, constraint: str, admin_engine: AsyncEngine
) -> None:
    """The door finds these rows by hash before the organization is known, so
    the hash is unique across the whole platform, not per organization."""
    org_a, _ = await _org(admin_engine)
    org_b, _ = await _org(admin_engine)
    user_a = await _person(admin_engine, org_a, f"a-{uuid4().hex[:8]}@acme.test")
    user_b = await _person(admin_engine, org_b, f"b-{uuid4().hex[:8]}@acme.test")
    digest = _digest()
    await _add(admin_engine, make(org_a, user_a, digest))
    with pytest.raises(IntegrityError, match=constraint):
        await _add(admin_engine, make(org_b, user_b, digest))


async def test_a_passkey_credential_id_is_registered_once_on_the_platform(
    admin_engine: AsyncEngine,
) -> None:
    """W3C WebAuthn §7.1: a credential id already registered to anyone is
    refused, whatever the organization or person."""
    org_a, _ = await _org(admin_engine)
    org_b, _ = await _org(admin_engine)
    user_a = await _person(admin_engine, org_a, f"a-{uuid4().hex[:8]}@acme.test")
    user_b = await _person(admin_engine, org_b, f"b-{uuid4().hex[:8]}@acme.test")
    credential = secrets.token_bytes(16)

    def passkey(org: UUID, user: UUID) -> AuthPasskey:
        return AuthPasskey(
            org_id=org, user_id=user, credential_id=credential, public_key=b"cose",
            backup_eligible=True, backup_state=False, name="Llave",
        )

    await _add(admin_engine, passkey(org_a, user_a))
    with pytest.raises(IntegrityError, match="uq_auth_passkeys_credential_id"):
        await _add(admin_engine, passkey(org_b, user_b))


async def test_signing_in_may_start_before_the_person_is_known(
    admin_engine: AsyncEngine,
) -> None:
    """The one challenge without a person: a global row (no client)."""
    org, _ = await _org(admin_engine)
    row = AuthWebauthnChallenge(
        org_id=org, user_id=None, purpose="authenticate", challenge=secrets.token_bytes(32),
        expires_at=_later(),
    )
    await _add(admin_engine, row)
    assert row.client_id is None
