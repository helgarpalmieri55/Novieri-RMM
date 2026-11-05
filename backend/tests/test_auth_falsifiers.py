"""The design's falsifiers for phase 1 of own authentication (§10: 1, 2, 3, 4,
5, 7, 9; 10 lives in the two vendor-name guards).

Each test names its falsifier and the protection whose removal turns it red.
They run through the HTTP surface after the console switched to the session
cookie, so they test the platform the way a browser meets it; the unit tests of
Tasks 4-8 stay where they are. Falsifiers 6 and 8 belong to phases 3 and 2.
Falsifier 4's browser half is the `webauthn-uv` CI job
(`frontend/scripts/webauthn-uv.mjs`).
"""

import ast
import base64
import contextlib
import json
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import argon2
import httpx
import pyotp
import pytest
import webauthn
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import app as app_package
from app.api.deps import AuthContext, require_step_up
from app.core.auth import passkeys
from app.core.auth.loopback import LOOPBACK_HEADER
from app.core.auth.passwords import hash_password
from app.core.auth.refusals import AuthRefusal
from app.core.auth.sessions import DEVICE_COOKIE, SessionPrincipal
from app.core.client_address import parse_trusted_proxies
from app.core.client_scope import ClientScope
from app.core.router_inspect import flatten
from app.core.security import TokenClaims
from app.core.tenancy import org_session
from app.models import AuditEvent, Client, User
from app.models.auth import AuthFailure, AuthPasskey, AuthPassword, AuthSession
from tests.health_fixtures import seed_org
from tests.mcp_support import MCP_BASE, marked_client, mcp_token
from tests.session_support import (
    CONSOLE,
    console_headers,
    cookie_header,
    refusal_code,
    sign_in,
)
from tests.test_auth_recover_command import _issue_calls

PASSWORD = "correct horse battery staple 12"
NEW_PASSWORD = "a different passphrase for 2026"
APP = Path(app_package.__file__).resolve().parent


class _Browser:
    """One browser on the console: it keeps the cookies the platform sets and
    sends them back, with the console's CSRF header and Origin -- from an
    address of its own, as Task 7's `tests/auth_support.py::browser` does. The
    test client's peer (127.0.0.1) is trusted as the proxy, so the
    `X-Forwarded-For` names this browser and its per-IP sign-in limits
    (`auth-login`, `auth-factor`: 30 in 10 minutes) are its own: a second run
    of this file inside ten minutes does not meet the first one's 429."""

    def __init__(self, env: Any) -> None:
        self.env = env
        self.jar: dict[str, str] = {}
        env.app.state.trusted_proxies = parse_trusted_proxies("127.0.0.1/32")
        a, b = secrets.randbelow(65536), secrets.randbelow(65536)
        self.address = f"2001:db8:{a:x}:{b:x}::1"

    async def send(self, method: str, path: str, body: Any = None) -> httpx.Response:
        headers = {**console_headers(), "X-Forwarded-For": self.address}
        if self.jar:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.jar.items())
        response = await self.env.client.request(method, path, json=body, headers=headers)
        for line in response.headers.get_list("set-cookie"):
            name, _, rest = line.partition("=")
            value, _, attributes = rest.partition(";")
            if value == "" or "max-age=0" in attributes.lower().replace(" ", ""):
                self.jar.pop(name.strip(), None)
            else:
                self.jar[name.strip()] = value.strip()
        return response

    async def post(self, path: str, body: Any = None) -> httpx.Response:
        return await self.send("POST", path, body)

    async def login(self, email: str, password: str) -> httpx.Response:
        return await self.post("/api/v1/auth/login", {"email": email, "password": password})


async def _staff(env: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[UUID, UUID, str]:
    """`(org, user, email)` of an org-admin with a password and no factor yet.
    Sign-in looks the person up in the deployment's org (D2), so the test org
    becomes it."""
    org_id = await env.create_org("falsifier")
    monkeypatch.setattr(env.settings, "default_org_slug", await env.org_slug(org_id))
    sub = await env.create_user(org_id, "org-admin")
    stored, version = await hash_password(org_id, PASSWORD)
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as session:
        user = (
            await session.execute(select(User).where(User.external_subject == sub))
        ).scalar_one()
        session.add(
            AuthPassword(
                user_id=user.id,
                org_id=org_id,
                hash=stored,
                pepper_version=version,
                changed_at=datetime.now(UTC),
            )
        )
        await session.commit()
        return org_id, user.id, user.email


async def _enrol(browser: _Browser, email: str) -> tuple[Any, list[str]]:
    """The first sign-in: password, TOTP enrolment, recovery codes. Returns the
    TOTP generator and the codes; the browser holds a complete session."""
    first = await browser.login(email, PASSWORD)
    assert first.json() == {"state": "enroll_required"}, first.text
    started = await browser.post("/api/v1/auth/enroll/totp/start")
    assert started.status_code == 200, started.text
    totp = pyotp.parse_uri(started.json()["otpauth_uri"])
    confirmed = await browser.post("/api/v1/auth/enroll/totp/confirm", {"code": totp.now()})
    assert confirmed.status_code < 300, confirmed.text
    finished = await browser.post("/api/v1/auth/enroll/finish")
    assert finished.status_code == 200, finished.text
    codes: list[str] = finished.json()["recovery_codes"]
    assert len(codes) == 10
    return totp, codes


def _next_code(totp: Any) -> str:
    """The code of the NEXT 30-second step: enrolment consumed the current one,
    and ±1 step is accepted (design §2.2)."""
    return str(totp.at(int(time.time()) + 30))


async def _mark_step_up(env: Any, user_id: UUID) -> None:
    async with AsyncSession(env.admin_engine) as session:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.ended_at.is_(None))
            .values(step_up_at=datetime.now(UTC))
        )
        await session.commit()


# --------------------------------------------------------------------------- #
# Falsifier 1: one org's session reads nothing of another's; a contact's
# session never passes the console's door.
# --------------------------------------------------------------------------- #


async def test_falsifier_1_a_session_of_one_org_reads_nothing_of_another(api_env: Any) -> None:
    """Holds the session -> org binding (`its_auth_session` returns the
    session's own org) and RLS on `auth_sessions`."""
    mine, theirs = await seed_org(api_env, "f1-mine"), await seed_org(api_env, "f1-theirs")
    sub = await api_env.create_user(mine.org_id, "org-admin")
    headers = await api_env.headers(mine.org_id, sub)
    other = await api_env.create_user(theirs.org_id, "org-admin")
    await sign_in(api_env, theirs.org_id, other)

    listed = await api_env.client.get("/api/v1/devices", headers=headers)
    one = await api_env.client.get(f"/api/v1/devices/{theirs.device_id}", headers=headers)

    assert listed.status_code == 200, listed.text
    assert str(mine.device_id) in listed.text
    assert str(theirs.device_id) not in listed.text
    assert one.status_code == 404
    async with org_session(
        mine.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        seen = set((await session.execute(select(AuthSession.org_id))).scalars())
    assert seen == {mine.org_id}


async def test_falsifier_1_a_contact_session_does_not_pass_the_console_door(api_env: Any) -> None:
    """Holds `contact_refusal` on the session path of `admit(contacts=False)`."""
    org_id = await api_env.create_org("f1-contact")
    sub = f"contact-pending:{uuid4()}"
    async with AsyncSession(api_env.admin_engine) as session:
        client = Client(org_id=org_id, name="Acme", kind="customer")
        session.add(client)
        await session.flush()
        session.add(
            User(
                org_id=org_id,
                external_subject=sub,
                email=f"cam-{uuid4().hex[:6]}@acme.example",
                kind="contact",
                client_id=client.id,
            )
        )
        await session.commit()
    signed = await sign_in(api_env, org_id, sub)

    me = await api_env.client.get("/api/v1/me", headers=signed.headers)

    assert me.status_code == 403


# --------------------------------------------------------------------------- #
# Falsifier 2: the same answer, and the same work, whether or not the account
# exists.
# --------------------------------------------------------------------------- #


async def test_falsifier_2_sign_in_answers_the_same_for_an_unknown_account(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Holds the dummy verification for an unknown address: without it the
    unknown account is answered faster -- zero argon2 verifications against
    one -- which is the timing oracle §2.3 forbids.

    WHAT IT DOES NOT CLAIM, ACCEPTED (final review C-M2). It counts argon2
    verifications, not milliseconds. A known address also opens its
    identity session (an advisory lock, three or four queries, a failure
    written; a device cookie of ours adds one), and an unknown or disabled
    one does not: measured locally, medians of 25 were 130-139 ms unknown,
    142 ms disabled, 147-149 ms known. That 10-15 ms is accepted rather than
    equalised: the argon2 work -- most of each answer's time, and what a
    timing oracle would need to be missing -- is the same, and the per-IP
    limits (30 attempts per 10 minutes per address) leave too few samples
    to separate a 10 % shift from the network's own jitter. Design §2.3
    records the acceptance."""
    _, _, email = await _staff(api_env, monkeypatch)
    verifications: list[int] = []
    real = argon2.PasswordHasher.verify

    def counted(self: argon2.PasswordHasher, hash: str | bytes, password: str | bytes) -> Any:
        verifications.append(1)
        return real(self, hash, password)

    monkeypatch.setattr(argon2.PasswordHasher, "verify", counted)
    browser = _Browser(api_env)

    wrong = "a wrong but long enough password"
    known = await browser.login(email, wrong)
    after_known = len(verifications)
    unknown = await browser.login(f"nobody-{uuid4().hex[:6]}@test.local", wrong)

    assert known.status_code == unknown.status_code
    assert known.status_code >= 400
    assert known.json() == unknown.json()
    assert after_known == 1
    assert len(verifications) == 2


# --------------------------------------------------------------------------- #
# Falsifier 3: at 100 failures the factor is disabled; a TOTP code is never
# accepted twice.
# --------------------------------------------------------------------------- #


async def test_falsifier_3_the_hundredth_failure_disables_the_factor(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Holds `DISABLE_AT = 100` and the check that a disabled factor refuses a
    RIGHT code too (`failures.check_allowed`)."""
    org_id, user_id, email = await _staff(api_env, monkeypatch)
    browser = _Browser(api_env)
    totp, _ = await _enrol(browser, email)
    await browser.post("/api/v1/auth/logout")
    assert (await browser.login(email, PASSWORD)).json()["state"] == "factor_required"
    async with AsyncSession(api_env.admin_engine) as session:
        await session.merge(
            AuthFailure(
                org_id=org_id,
                user_id=user_id,
                factor="totp",
                consecutive=99,
                last_failed_at=datetime.now(UTC) - timedelta(days=1),
                disabled_at=None,
            )
        )
        await session.commit()
    valid = {str(totp.at(int(time.time()) + 30 * k)) for k in (-1, 0, 1, 2)}
    wrong = next(code for code in ("000000", "111111", "222222", "333333") if code not in valid)

    refused = await browser.post(
        "/api/v1/auth/factor/totp", {"code": wrong, "remember_device": False}
    )
    right = await browser.post(
        "/api/v1/auth/factor/totp", {"code": _next_code(totp), "remember_device": False}
    )

    assert refused.status_code >= 400
    assert right.status_code >= 400 and right.json().get("state") != "signed_in"
    async with AsyncSession(api_env.admin_engine) as session:
        row = await session.get(AuthFailure, (user_id, "totp"))
        assert row is not None and row.consecutive >= 100 and row.disabled_at is not None
        disabled = (
            await session.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.org_id == org_id, AuditEvent.action == "auth.factor_disabled")
            )
        ).scalar_one()
    assert disabled == 1


async def test_falsifier_3_a_totp_code_is_not_accepted_twice(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Holds `last_step` (design §2.2: pyotp 2.10.0 does not refuse a replay)."""
    _, _, email = await _staff(api_env, monkeypatch)
    browser = _Browser(api_env)
    totp, _ = await _enrol(browser, email)
    code = _next_code(totp)

    await browser.post("/api/v1/auth/logout")
    await browser.login(email, PASSWORD)
    first = await browser.post("/api/v1/auth/factor/totp", {"code": code, "remember_device": False})
    await browser.post("/api/v1/auth/logout")
    await browser.login(email, PASSWORD)
    again = await browser.post("/api/v1/auth/factor/totp", {"code": code, "remember_device": False})

    assert first.json() == {"state": "signed_in"}, first.text
    assert again.status_code >= 400 and again.json().get("state") != "signed_in"


# --------------------------------------------------------------------------- #
# Falsifier 4 (server half): every passkey check requires user verification.
# --------------------------------------------------------------------------- #


async def test_falsifier_4_every_passkey_check_requires_user_verification(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Holds D22: py_webauthn's `require_user_verification` defaults to False,
    and the platform passes True on BOTH ceremonies; the options ask for it too.
    The verifier is replaced by a spy that records what it was given."""
    seen: dict[str, dict[str, Any]] = {}

    def spy(name: str) -> Any:
        def recorded(**kwargs: Any) -> Any:
            seen[name] = kwargs
            raise ValueError("stopped by the spy")

        return recorded

    for name in ("verify_registration_response", "verify_authentication_response"):
        monkeypatch.setattr(webauthn, name, spy(name))
        monkeypatch.setattr(passkeys, name, spy(name), raising=False)
    org_id = await api_env.create_org("f4")
    sub = await api_env.create_user(org_id, "org-admin")
    rp_id = "itms.test"

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user = (
            await session.execute(select(User).where(User.external_subject == sub))
        ).scalar_one()
        created = await passkeys.registration_options(session, user, rp_id=rp_id, rp_name="ITMS")
        assert created["authenticatorSelection"]["userVerification"] == "required"
        client_data = b64(
            json.dumps(
                {
                    "type": "webauthn.create",
                    "challenge": created["challenge"],
                    "origin": CONSOLE,
                }
            ).encode()
        )
        # The spy stops the ceremony; whatever the platform turns that into is
        # beside the point -- what it HANDED the verifier is the assertion.
        with contextlib.suppress(Exception):
            await passkeys.verify_registration(
                session,
                user,
                {
                    "id": b64(b"\x01" * 16),
                    "rawId": b64(b"\x01" * 16),
                    "type": "public-key",
                    "response": {"clientDataJSON": client_data, "attestationObject": b64(b"\x00")},
                    "clientExtensionResults": {},
                },
                name="spy",
                expected_origin=CONSOLE,
                rp_id=rp_id,
            )
        session.add(
            AuthPasskey(
                org_id=org_id,
                user_id=user.id,
                credential_id=b"\x02" * 16,
                public_key=b"\x00",
                sign_count=0,
                transports=["internal"],
                backup_eligible=False,
                backup_state=False,
                name="spy",
                created_at=datetime.now(UTC),
            )
        )
        await session.flush()
        asked = await passkeys.authentication_options(
            session, user.id, org_id=org_id, rp_id=rp_id, purpose="authenticate"
        )
        assert asked["userVerification"] == "required"
        assertion = b64(
            json.dumps(
                {"type": "webauthn.get", "challenge": asked["challenge"], "origin": CONSOLE}
            ).encode()
        )
        with contextlib.suppress(Exception):
            await passkeys.verify_authentication(
                session,
                {
                    "id": b64(b"\x02" * 16),
                    "rawId": b64(b"\x02" * 16),
                    "type": "public-key",
                    "response": {
                        "clientDataJSON": assertion,
                        "authenticatorData": b64(b"\x00" * 37),
                        "signature": b64(b"\x00"),
                        "userHandle": None,
                    },
                    "clientExtensionResults": {},
                },
                expected_origin=CONSOLE,
                rp_id=rp_id,
                purpose="authenticate",
            )

    assert seen["verify_registration_response"]["require_user_verification"] is True
    assert seen["verify_authentication_response"]["require_user_verification"] is True


# --------------------------------------------------------------------------- #
# Falsifier 5: a remembered device stops counting when the password changes;
# the vault still asks for the second factor.
# --------------------------------------------------------------------------- #


async def test_falsifier_5_the_vault_asks_and_the_device_forgets(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Holds two lines: the step-up gate on `POST /credentials/{id}/checkout`
    (design §2.5, §2.6: a remembered device skips the factor at sign-in, never
    at the vault) and the device revocation in `PUT /me/password`."""
    _, user_id, email = await _staff(api_env, monkeypatch)
    browser = _Browser(api_env)
    _, codes = await _enrol(browser, email)
    made = await browser.post(
        "/api/v1/credentials", {"name": "f5", "type": "password", "secret": "s3cret-value"}
    )
    assert made.status_code == 201, made.text
    credential = made.json()["id"]
    await browser.post("/api/v1/auth/logout")
    await browser.login(email, PASSWORD)
    remembered = await browser.post(
        "/api/v1/auth/factor/recovery", {"code": codes[0], "remember_device": True}
    )
    assert remembered.json() == {"state": "signed_in"}, remembered.text
    device = browser.jar[DEVICE_COOKIE]
    await browser.post("/api/v1/auth/logout")
    browser.jar[DEVICE_COOKIE] = device

    # The device counts: no second factor at sign-in...
    assert (await browser.login(email, PASSWORD)).json() == {"state": "signed_in"}
    # ...and still none at the vault.
    asked = await browser.post(f"/api/v1/credentials/{credential}/checkout", {"reason": "f5"})
    assert asked.status_code == 403
    assert refusal_code(asked) == "step_up_required"
    await _mark_step_up(api_env, user_id)
    opened = await browser.post(f"/api/v1/credentials/{credential}/checkout", {"reason": "f5"})
    assert opened.status_code == 200, opened.text

    changed = await browser.send(
        "PUT", "/api/v1/me/password", {"current": PASSWORD, "new": NEW_PASSWORD}
    )
    assert changed.status_code < 300, changed.text
    await browser.post("/api/v1/auth/logout")
    browser.jar[DEVICE_COOKIE] = device

    after = await browser.login(email, NEW_PASSWORD)

    assert after.json()["state"] == "factor_required", after.text


# --------------------------------------------------------------------------- #
# Falsifier 7: a cookie write without the custom header, or with Origin null,
# is refused.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "spoil",
    [
        pytest.param(lambda h: h.pop("X-ITMS-Request"), id="no-custom-header"),
        pytest.param(lambda h: h.update({"Origin": "null"}), id="origin-null"),
        pytest.param(lambda h: h.pop("Origin"), id="no-origin"),
        pytest.param(lambda h: h.update({"Origin": "https://evil.example"}), id="foreign-origin"),
    ],
)
async def test_falsifier_7_a_cookie_write_without_its_proof_of_origin_is_refused(
    api_env: Any, spoil: Any
) -> None:
    """Holds `RequireConsoleRequestHeader` (Task 6). The control is the same
    write with the headers a console sends."""
    org_id = await api_env.create_org("f7")
    sub = await api_env.create_user(org_id, "read-only")
    headers = await api_env.headers(org_id, sub)
    spoiled = dict(headers)
    spoil(spoiled)

    refused = await api_env.client.patch("/api/v1/me", headers=spoiled, json={"phone": "+57 1"})
    allowed = await api_env.client.patch("/api/v1/me", headers=headers, json={"phone": "+57 2"})

    assert refused.status_code == 403
    assert refusal_code(refused) == "csrf"
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["phone"] == "+57 2"


# --------------------------------------------------------------------------- #
# Falsifier 9: the recovery command does not exist over HTTP.
# --------------------------------------------------------------------------- #


_COMMAND = "app.core.auth.recover"


_NAMES_THE_COMMAND = re.compile(r"\bapp\.core\.auth\.recover\b(?!y)")


def _docstrings(tree: ast.AST) -> set[int]:
    """The `id()` of every docstring constant: a module's, a class's or a
    function's first statement when it is a bare string."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(id(first.value))
    return found


def _imports_the_command(tree: ast.AST, path: Path) -> bool:
    """Whether a module's CODE reaches the recovery command, however spelled:
    `import app.core.auth.recover`, `from app.core.auth import recover`, the
    relative `from . import recover` inside `app/core/auth/` -- or any string
    in the code that names the module, which is how
    `importlib.import_module("app.core.auth.recover")`, `__import__(...)` and
    the `python -m` route (`[sys.executable, "-m", "app.core.auth.recover"]`,
    a shell command line) all spell it, the last two without importing it.

    Read on the AST rather than the text: a docstring that NAMES the command
    (`app/core/auth/flows.py` says only it issues a recovery link) is not code,
    and neither is a comment. And `recovery`, the recovery CODES (Task 4), is
    another module."""
    package = ".".join(("app", *path.relative_to(APP).parent.parts))
    docstrings = _docstrings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == _COMMAND for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                base = ".".join([*anchor, *([base] if base else [])])
            if base == _COMMAND or (
                base == "app.core.auth" and any(alias.name == "recover" for alias in node.names)
            ):
                return True
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and _NAMES_THE_COMMAND.search(node.value)
        ):
            return True
    return False


def test_the_import_detector_is_not_vacuous() -> None:
    """Every spelling `_imports_the_command` exists to catch is caught; the
    recovery CODES and a docstring that only names the command are not."""
    inside = APP / "core" / "auth" / "flows.py"
    elsewhere = APP / "api" / "v1" / "auth.py"
    caught = [
        ("import app.core.auth.recover\n", elsewhere),
        ("from app.core.auth import recover\n", elsewhere),
        ("from app.core.auth.recover import main\n", elsewhere),
        ("from . import recover\n", inside),
        ("from .recover import main\n", inside),
        ("import importlib\nimportlib.import_module('app.core.auth.recover')\n", elsewhere),
        # The `python -m` route: a route that runs the command in a subprocess
        # issues a link over HTTP without importing anything.
        (
            "import subprocess, sys\n"
            "def issue(email):\n"
            "    subprocess.run(\n"
            "        [sys.executable, '-m', 'app.core.auth.recover', '--email', email]\n"
            "    )\n",
            elsewhere,
        ),
        ("CMD = 'python -m app.core.auth.recover --email {email}'\n", elsewhere),
    ]
    for source, path in caught:
        assert _imports_the_command(ast.parse(source), path), source
    passed = [
        ("from app.core.auth.recovery import use_code\n", elsewhere),
        ("from app.core.auth import recovery\n", elsewhere),
        ('"""Only `app.core.auth.recover` issues one."""\n', elsewhere),
        ('def f():\n    """Only `app.core.auth.recover` issues one."""\n', elsewhere),
        ("NAME = 'app.core.auth.recovery'\n", elsewhere),
    ]
    for source, path in passed:
        assert not _imports_the_command(ast.parse(source), path), source


def test_falsifier_9_the_recovery_command_has_no_http_route(api_env: Any) -> None:
    """Holds design §7: `python -m app.core.auth.recover` only -- a recovery
    link is ISSUED by shell access to the server and by nothing over HTTP.
    Three things together make an HTTP route that issues one impossible, and
    each assertion goes red without its own: no route's endpoint lives in the
    command's module; nothing in `app/` imports that module; and the only call
    in `app/` that issues an action token with a purpose other than an
    invitation or a reset is the command's (Task 8's AST walk,
    `_issue_calls`). The two routes whose path names `recover`,
    `POST /api/v1/auth/recover/details` and `/accept` (Task 8), CONSUME a link
    the command printed; a third such path is a new door and fails here."""
    recover = APP / "core" / "auth" / "recover.py"
    assert recover.exists()
    assert "APIRouter" not in recover.read_text()
    importers = [
        str(path.relative_to(APP))
        for path in sorted(APP.rglob("*.py"))
        if path != recover and _imports_the_command(ast.parse(path.read_text()), path)
    ]
    assert importers == []
    routes = flatten(api_env.app.router)
    assert routes, "the router walk found nothing; the next assertions would be vacuous"
    served_by_the_command = [
        path
        for path, route in routes
        if getattr(getattr(route, "endpoint", None), "__module__", None) == "app.core.auth.recover"
    ]
    assert served_by_the_command == []
    # `recover`, not `recovery`: `/auth/factor/recovery`, `/auth/step-up/recovery`
    # and `/me/recovery-codes` are the recovery CODES.
    naming_it = sorted(
        (method, path)
        for path, route in routes
        if re.search(r"recover(?!y)", path)
        for method in getattr(route, "methods", None) or ()
    )
    assert naming_it == [
        ("POST", "/api/v1/auth/recover/accept"),
        ("POST", "/api/v1/auth/recover/details"),
    ]
    issuers = sorted(
        {path for path, purpose in _issue_calls() if purpose not in {"INVITE", "RESET"}}
    )
    assert issuers == ["core/auth/recover.py"]


# --------------------------------------------------------------------------- #
# The door after the switch (decisions (1) and (2) of Task 13).
# --------------------------------------------------------------------------- #


async def test_the_console_bearer_opens_no_console_door(api_env: Any) -> None:
    """The token the console used to send -- issued to `it-suite-web` -- is no
    credential at /api/v1 any more; the same person by cookie is the control."""
    org_id = await api_env.create_org("door")
    sub = await api_env.create_user(org_id, "org-admin")
    token = api_env.idp.token(sub, await api_env.org_slug(org_id), email=f"{sub}@test.local")

    by_bearer = await api_env.client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    by_cookie = await api_env.client.get("/api/v1/me", headers=await api_env.headers(org_id, sub))

    assert by_bearer.status_code == 401
    assert refusal_code(by_bearer) == "auth_required"
    assert by_cookie.status_code == 200, by_cookie.text


async def test_the_mcp_channel_takes_no_cookie(api_env: Any) -> None:
    """1c(a): on the MCP channel only an OAuth token makes a session."""
    org_id = await api_env.create_org("door-mcp")
    sub = await api_env.create_user(org_id, "org-admin")
    signed = await sign_in(api_env, org_id, sub)

    async with marked_client(api_env) as client:
        me = await client.get("/api/v1/me", headers={"Cookie": cookie_header(signed.token)})

    assert me.status_code == 401


async def test_the_mcp_channel_refuses_a_request_claiming_the_assistant(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision (2), the other half: the two in-process channels never meet.
    On MCP a request that SAYS it is the assistant's own call
    (`X-ITMS-Loopback: 1`) is a 401 however genuine its MCP token -- the
    `claims_loopback` branch of `admit()`'s MCP path. The control is the same
    request, same token, without the claim: 200."""
    # The MCP resource is `MCP_AUD` at this console (tests/mcp_support.py).
    monkeypatch.setattr(api_env.settings, "public_console_url", MCP_BASE)
    org_id = await api_env.create_org("door-mcp-loopback")
    sub = await api_env.create_user(org_id, "org-admin")
    bearer = {"Authorization": f"Bearer {mcp_token(api_env, await api_env.org_slug(org_id), sub)}"}

    async with marked_client(api_env) as client:
        claimed = await client.get("/api/v1/me", headers={**bearer, LOOPBACK_HEADER: "1"})
        control = await client.get("/api/v1/me", headers=bearer)

    assert claimed.status_code == 401, claimed.text
    assert claimed.json()["detail"] == "the assistant's internal token was refused"
    assert control.status_code == 200, control.text


def test_require_step_up_admits_no_request_without_a_session() -> None:
    """Decision (2b): the Bearer pass Task 6 left in `require_step_up` is gone.
    The control is a session confirmed a moment ago."""
    now = datetime.now(UTC)
    fresh = SessionPrincipal(
        session_id=uuid4(),
        user_id=uuid4(),
        org_id=uuid4(),
        kind="staff",
        factor_done=True,
        step_up_at=now,
        email="ada@test.local",
        display_name="Ada",
        expires_at=now + timedelta(hours=1),
    )

    def ctx(principal: SessionPrincipal | None) -> AuthContext:
        claims = None if principal else TokenClaims(sub="s", email="", name="", org_slug="o")
        return AuthContext(
            org_id=uuid4(),
            user_id=uuid4(),
            claims=claims,
            clients=ClientScope.all(),
            principal=principal,
        )

    require_step_up(ctx(fresh), now=now)
    with pytest.raises(AuthRefusal) as refused:
        require_step_up(ctx(None), now=now)
    assert (refused.value.status_code, refused.value.code) == (403, "step_up_required")
