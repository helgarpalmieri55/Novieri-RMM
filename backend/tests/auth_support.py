"""Signing a technician in through the platform's own routes (P2 phase 1 Tasks 7-8).

`tests/conftest.py::ApiEnv.headers` still mints a console Bearer token until
Task 13, and `tests/session_support.py::sign_in` makes a session row directly;
these helpers drive the cookie path through HTTP instead: a password written
with the real hash, `POST /auth/login`, the first-factor enrolment and its
recovery codes.

THE COOKIE IS SENT BY HAND. httpx's jar will not send a `Secure` cookie to
`http://api.test`, and the `__Host-` cookies are always `Secure`, so `Staff`
keeps their values and `headers()` writes the `Cookie` header itself.

EVERY PERSON SIGNS IN FROM AN ADDRESS OF ITS OWN. The per-IP limits
(`app/api/v1/auth.py`) key by address. `console_ready` trusts the test
client's peer (`127.0.0.1`) as a proxy, and `browser()` hands out a random
address of the documentation prefix (RFC 3849), so one test's attempts never
count against another's.
"""

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pyotp
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.passwords import hash_password
from app.core.auth.sessions import DEVICE_COOKIE, SESSION_COOKIE
from app.core.auth.totp import step_of
from app.core.client_address import parse_trusted_proxies
from app.core.mail import MailFailure, MailNotConfigured, MailSendFailed
from app.models import Client, Role, User, UserRole
from app.models.auth import AuthPasskey, AuthPassword, AuthTotp
from tests.session_support import CONSOLE, console_headers, use_console

__all__ = [
    "CONSOLE",
    "CONTACT_PASSWORD",
    "PASSWORD",
    "MailBox",
    "Staff",
    "add_passkey",
    "browser",
    "capture_mail",
    "console_ready",
    "contact",
    "cookie",
    "enrol_totp",
    "fresh_code",
    "link_token",
    "login",
    "point_at",
    "signed_in",
    "staff",
    "wrong_code",
]

#: Long, not on the blocklist, and containing neither a test address nor the
#: product's name -- so `check_new_password` accepts it wherever a test sets it.
PASSWORD = "Ultramarine-Kettle-7319"


def console_ready(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """`ITS_PUBLIC_CONSOLE_URL` set (Task 6's `use_console`), and the test
    client's peer trusted as the proxy, so `X-Forwarded-For` names the browser."""
    use_console(monkeypatch, env)
    env.app.state.trusted_proxies = parse_trusted_proxies("127.0.0.1/32")


async def point_at(env: Any, org_id: UUID) -> None:
    """`ITS_DEFAULT_ORG_SLUG`: the organization `POST /auth/login` looks in."""
    env.settings.default_org_slug = await env.org_slug(org_id)


def browser() -> dict[str, str]:
    """The console's headers (Task 6), from an address nobody else uses."""
    a, b = secrets.randbelow(65536), secrets.randbelow(65536)
    return {**console_headers(), "X-Forwarded-For": f"2001:db8:{a:x}:{b:x}::1"}


def cookie(response: httpx.Response, name: str) -> str | None:
    """The value `response` sets for `name`; None when it sets none or clears
    it (Task 6's `clear_*_cookie` writes `name=""; Max-Age=0`)."""
    for header in response.headers.get_list("set-cookie"):
        key, _, rest = header.partition("=")
        if key.strip() == name:
            return rest.split(";", 1)[0].strip('"') or None
    return None


def link_token(text: str) -> str:
    """The token of the one link in a mail: everything after its `#`."""
    line = next(part for part in text.split() if part.startswith(CONSOLE))
    return line.split("#", 1)[1]


@dataclass
class Staff:
    org_id: UUID
    user_id: UUID
    sub: str
    email: str
    password: str
    network: dict[str, str]
    session: str | None = None
    device: str | None = None
    totp_secret: str | None = None
    last_code: str | None = None
    recovery_codes: list[str] = field(default_factory=list)
    client_id: UUID | None = None

    def headers(self) -> dict[str, str]:
        jar = []
        if self.session:
            jar.append(f"{SESSION_COOKIE}={self.session}")
        if self.device:
            jar.append(f"{DEVICE_COOKIE}={self.device}")
        out = dict(self.network)
        if jar:
            out["Cookie"] = "; ".join(jar)
        return out


async def staff(env: Any, org_id: UUID, *roles: str, password: str = PASSWORD) -> Staff:
    """A technician with global `roles` and a password, and no second factor yet."""
    sub = await env.create_user(org_id, *roles)
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as session:
        user = (
            await session.execute(select(User).where(User.external_subject == sub))
        ).scalar_one()
        digest, version = await hash_password(org_id, password)
        session.add(
            AuthPassword(
                user_id=user.id,
                org_id=org_id,
                hash=digest,
                pepper_version=version,
                changed_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return Staff(org_id, user.id, sub, user.email, password, browser())


#: A contact's password: 15 characters at least (design §3), not on the
#: blocklist, and free of every test address's words and the product's name.
CONTACT_PASSWORD = "Ultramarine-Kettle-7319-Harbor"


async def contact(
    env: Any, org_id: UUID, *, password: str | None = CONTACT_PASSWORD, role: bool = True
) -> Staff:
    """A contact of a new client, holding the portal role (`requester`) on that
    client only (design §3) unless `role` is False -- the state before an
    invitation is accepted, which grants it (Task 8) -- and, unless `password`
    is None, a password. The same `Staff` bag the technician helpers use:
    `sub` is the contact's `external_subject`."""
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as session:
        client = Client(org_id=org_id, name=f"Acme-{secrets.token_hex(3)}", kind="customer")
        session.add(client)
        await session.flush()
        user = User(
            org_id=org_id,
            kind="contact",
            client_id=client.id,
            email=f"{secrets.token_hex(4)}@acme.test",
            external_subject=f"contact-pending:{uuid4()}",
        )
        session.add(user)
        await session.flush()
        if role:
            requester = (
                await session.execute(
                    select(Role).where(Role.name == "requester", Role.org_id.is_(None))
                )
            ).scalar_one()
            session.add(
                UserRole(
                    org_id=org_id, user_id=user.id, role_id=requester.id, client_id=client.id
                )
            )
        if password is not None:
            digest, version = await hash_password(org_id, password)
            session.add(
                AuthPassword(
                    user_id=user.id,
                    org_id=org_id,
                    hash=digest,
                    pepper_version=version,
                    changed_at=datetime.now(UTC),
                )
            )
        await session.commit()
    return Staff(
        org_id,
        user.id,
        user.external_subject,
        user.email,
        password or "",
        browser(),
        client_id=client.id,
    )


async def login(env: Any, who: Staff, *, password: str | None = None) -> httpx.Response:
    response = await env.client.post(
        "/api/v1/auth/login",
        headers=who.headers(),
        json={"email": who.email, "password": password or who.password},
    )
    token = cookie(response, SESSION_COOKIE)
    if token:
        who.session = token
    return response


async def enrol_totp(env: Any, who: Staff) -> None:
    """From a session waiting for its first factor to a signed-in one."""
    start = await env.client.post("/api/v1/auth/enroll/totp/start", headers=who.headers())
    assert start.status_code == 200, start.text
    who.totp_secret = parse_qs(urlsplit(start.json()["otpauth_uri"]).query)["secret"][0]
    who.last_code = pyotp.TOTP(who.totp_secret).now()
    confirm = await env.client.post(
        "/api/v1/auth/enroll/totp/confirm", headers=who.headers(), json={"code": who.last_code}
    )
    assert confirm.status_code == 204, confirm.text
    finish = await env.client.post("/api/v1/auth/enroll/finish", headers=who.headers())
    assert finish.status_code == 200, finish.text
    who.recovery_codes = finish.json()["recovery_codes"]
    who.session = cookie(finish, SESSION_COOKIE)


async def signed_in(env: Any, org_id: UUID, *roles: str) -> Staff:
    """A technician signed in with a confirmed TOTP and fresh recovery codes."""
    who = await staff(env, org_id, *roles)
    await point_at(env, org_id)
    first = await login(env, who)
    assert first.json() == {"state": "enroll_required"}, first.text
    await enrol_totp(env, who)
    return who


async def fresh_code(env: Any, who: Staff) -> str:
    """A code the anti-replay rule takes again. TEST ONLY: it moves the last
    accepted step two steps back (never to NULL, which Task 4 reads as a
    pending secret), so the current step is newer. The replay test never
    calls it."""
    assert who.totp_secret is not None
    async with AsyncSession(env.admin_engine) as session:
        await session.execute(
            update(AuthTotp)
            .where(AuthTotp.user_id == who.user_id)
            .values(last_step=step_of(datetime.now(UTC)) - 2)
        )
        await session.commit()
    who.last_code = pyotp.TOTP(who.totp_secret).now()
    return who.last_code


def wrong_code(secret: str) -> str:
    """Six digits that no step from -2 to +2 produces: wrong whatever the clock does."""
    totp = pyotp.TOTP(secret)
    now = datetime.now(UTC)
    taken = {totp.at(now, offset) for offset in range(-2, 3)}
    return next(code for code in (f"{n:06d}" for n in range(1_000_000)) if code not in taken)


async def add_passkey(env: Any, who: Staff, name: str = "Laptop") -> UUID:
    """A passkey row. Its key material is never verified here: the tests that
    use it replace `flows.verify_authentication` (Task 5 tests the crypto)."""
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as session:
        key = AuthPasskey(
            org_id=who.org_id,
            user_id=who.user_id,
            credential_id=secrets.token_bytes(32),
            public_key=secrets.token_bytes(77),
            sign_count=0,
            transports=["internal"],
            backup_eligible=True,
            backup_state=True,
            name=name,
            created_at=datetime.now(UTC),
        )
        session.add(key)
        await session.commit()
        return key.id


@dataclass(frozen=True)
class Sent:
    to: str
    subject: str
    text: str


@dataclass
class MailBox:
    sent: list[Sent] = field(default_factory=list)
    configured: bool = True
    failing: bool = False


def capture_mail(monkeypatch: pytest.MonkeyPatch) -> MailBox:
    """Replace the account mail (Task 3) with a box the test reads.
    `configured=False` is a deployment with no account mail; `failing=True`,
    a relay that cannot be reached."""
    box = MailBox()

    async def fake(
        session: Any,
        *,
        org_id: UUID,
        to: str,
        subject: str,
        text: str,
        html: str | None = None,
        vault: Any,
        actor_id: UUID | None = None,
    ) -> None:
        if not box.configured:
            raise MailNotConfigured()
        if box.failing:
            raise MailSendFailed(MailFailure.UNREACHABLE)
        box.sent.append(Sent(to=to, subject=subject, text=text))

    monkeypatch.setattr("app.core.auth.notices.send_account_mail", fake)
    return box
