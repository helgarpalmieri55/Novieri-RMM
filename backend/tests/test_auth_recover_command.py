"""`python -m app.core.auth.recover --email <address>` (design §7, Q5; falsifier 9).

For the one administrator who lost every second factor and has nobody to
reset them: a one-time, 15-minute link, printed on the server's terminal.
Following it (`POST /auth/recover/accept`) removes the factors, forgets the
devices, ends the sessions and opens a session that goes straight to
enrolling a new factor; the password does not change. Audited at both ends,
and the holder is told.

FALSIFIER 9: it does not exist over HTTP -- no route reaches the module,
nothing in `app/` imports it, it builds no router, and no other module issues a
`recover` link.
"""

import ast
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import recover
from app.core.auth.sessions import SESSION_COOKIE
from app.core.router_inspect import flatten
from app.models import AuditEvent
from app.models.auth import AuthPasskey, AuthRecoveryCode, AuthTotp
from tests.auth_support import (
    CONSOLE,
    add_passkey,
    browser,
    capture_mail,
    console_ready,
    cookie,
    enrol_totp,
    login,
    signed_in,
)
from tests.tool_surface import build_router_only_app

APP = Path(__file__).resolve().parents[1] / "app"
RECOVER_PY = APP / "core" / "auth" / "recover.py"


@pytest.fixture()
def auth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    return api_env


async def test_the_link_removes_the_factors_once_and_leads_to_a_new_one(auth_env: Any) -> None:
    org = await auth_env.create_org("recover")
    who = await signed_in(auth_env, org, "org-admin")
    await add_passkey(auth_env, who)
    old_session = who.session

    link = await recover.issue_recovery_link(settings=auth_env.settings, email=who.email)
    token = link.split("#", 1)[1]
    details = await auth_env.client.post(
        "/api/v1/auth/recover/details", headers=browser(), json={"token": token}
    )
    network = browser()
    accepted = await auth_env.client.post(
        "/api/v1/auth/recover/accept", headers=network, json={"token": token}
    )
    # Every second factor went: the passkey, the TOTP and the recovery codes
    # (`clear_second_factors`). Read before a new one is enrolled.
    async with AsyncSession(auth_env.admin_engine) as session:
        left = [
            (await session.execute(select(table.id).where(table.user_id == who.user_id))).all()
            for table in (AuthPasskey, AuthRecoveryCode)
        ]
        totp = await session.get(AuthTotp, who.user_id)
    again = await auth_env.client.post(
        "/api/v1/auth/recover/accept", headers=browser(), json={"token": token}
    )
    as_reset = await auth_env.client.post(
        "/api/v1/auth/password/reset",
        headers=browser(),
        json={"token": token, "password": "Pelican-Orbit-Lantern-58"},
    )

    assert link.startswith(f"{CONSOLE}/recover#")
    assert details.json() == {"email": who.email}
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"state": "enroll_required"}
    assert left == [[], []] and totp is None
    assert again.status_code == 410 and again.json()["code"] == "invalid_link"
    assert as_reset.status_code == 410
    # Every earlier session ended; the new one is pending and enrols a factor.
    who.session = old_session
    assert (await auth_env.client.get("/api/v1/me", headers=who.headers())).status_code == 401
    who.session, who.network = cookie(accepted, SESSION_COOKIE), network
    await enrol_totp(auth_env, who)
    assert (await auth_env.client.get("/api/v1/me", headers=who.headers())).status_code == 200
    # The password did not change.
    who.session = None
    assert (await login(auth_env, who)).json()["state"] == "factor_required"
    async with AsyncSession(auth_env.admin_engine) as session:
        events = {
            e.action: e
            for e in (
                await session.execute(select(AuditEvent).where(AuditEvent.org_id == org))
            ).scalars()
        }
    issued = events["user.recovery_link_issued"]
    assert (issued.actor_type, issued.actor_id) == ("system", None)
    assert token not in str(issued.meta)
    used = events["user.recovery_link_used"]
    assert (used.actor_type, used.actor_id) == ("user", who.user_id)
    assert token not in str(used.meta)
    subjects = [m.subject.lower() for m in auth_env.mail.sent if m.to == who.email]
    assert any("recovery link" in s for s in subjects)
    assert any("sign-in methods were reset" in s for s in subjects)


async def test_only_an_active_technician_gets_a_link(auth_env: Any) -> None:
    org = await auth_env.create_org("recover-none")
    await signed_in(auth_env, org, "org-admin")

    with pytest.raises(recover.RecoveryRefused):
        await recover.issue_recovery_link(settings=auth_env.settings, email="nobody@test.local")


def test_the_command_prints_the_link_or_says_why_not(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def gives(*, settings: Any, email: str) -> str:
        return f"{CONSOLE}/recover#abc"

    async def refuses(*, settings: Any, email: str) -> str:
        raise recover.RecoveryRefused("no active technician has that address")

    monkeypatch.setattr(recover, "get_settings", lambda: None)
    monkeypatch.setattr(recover, "issue_recovery_link", gives)
    assert recover.main(["--email", "ada@toeshee.test"]) == 0
    assert capsys.readouterr().out.strip() == f"{CONSOLE}/recover#abc"

    monkeypatch.setattr(recover, "issue_recovery_link", refuses)
    assert recover.main(["--email", "ada@toeshee.test"]) == 1
    printed = capsys.readouterr()
    assert printed.out == "" and "no active technician" in printed.err


def test_the_recovery_command_does_not_exist_over_http() -> None:
    """FALSIFIER 9. Each assertion fails if somebody wires the command to a route."""
    for path, route in flatten(build_router_only_app().router):
        module = getattr(getattr(route, "endpoint", None), "__module__", "")
        assert module != "app.core.auth.recover", path

    for py in APP.rglob("*.py"):
        if py == RECOVER_PY:
            continue
        for node in ast.walk(ast.parse(py.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "app.core.auth.recover", py
                assert not (
                    node.module == "app.core.auth" and any(a.name == "recover" for a in node.names)
                ), py
            elif isinstance(node, ast.Import):
                assert all(a.name != "app.core.auth.recover" for a in node.names), py

    source = RECOVER_PY.read_text()
    assert "APIRouter" not in source and "fastapi" not in source
    issued = _issue_calls()
    # Not vacuous: the walk sees Task 7's reset link and this task's
    # invitation as well as the recovery link, whatever spelling each uses.
    assert {purpose for _, purpose in issued} >= {"INVITE", "RESET", "RECOVER"}, issued
    recovery_issuers = sorted(
        path for path, purpose in issued if purpose not in {"INVITE", "RESET"}
    )
    assert recovery_issuers == ["core/auth/recover.py"], issued


def _purpose(call: ast.Call) -> str:
    """The `purpose=` of one `issue(...)` call as written: a constant's name
    (`action_tokens.RECOVER`, or `RECOVER` imported bare) or a literal
    upper-cased (`"recover"`). Anything else -- a variable, `**kwargs`, no
    keyword at all -- comes back as its own source and counts as a possible
    recovery issuer: the safe direction for this list to be wrong in."""
    for keyword in call.keywords:
        if keyword.arg == "purpose":
            value = keyword.value
            if isinstance(value, ast.Attribute):
                return value.attr
            if isinstance(value, ast.Name):
                return value.id
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value.upper()
            return ast.unparse(value)
    return "<no purpose keyword>"


def _issue_calls() -> list[tuple[str, str]]:
    """Every call to `issue(...)` or `<anything>.issue(...)` under `app/` --
    `action_tokens.issue` however it was imported or aliased -- as
    `(path, purpose)`. Matched on the AST, not on text, so neither a line break
    nor another spelling of the purpose hides an issuer."""
    found: list[tuple[str, str]] = []
    for py in sorted(APP.rglob("*.py")):
        for node in ast.walk(ast.parse(py.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Name) and func.id == "issue") or (
                isinstance(func, ast.Attribute) and func.attr == "issue"
            ):
                found.append((str(py.relative_to(APP)), _purpose(node)))
    return found
