"""Response security headers (defense in depth). The API returns JSON, but
nosniff still matters, and secret-bearing responses must never be cached."""

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pyotp
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import security_headers
from app.core.auth.sessions import SESSION_COOKIE
from app.core.router_inspect import flatten
from app.mcp.exclusions import NEVER_EXPOSED_REASONS
from app.models import Role
from tests.auth_support import (
    capture_mail,
    console_ready,
    cookie,
    login,
    point_at,
    signed_in,
    staff,
)
from tests.tool_surface import build_router_only_app

#: EVERY answer that carries a secret once (final review C-M9), the list a new
#: one must join: `security_headers.NO_STORE_ROUTES` must equal it, and every
#: never-exposed route whose reason says it RETURNS a secret must be in it.
SECRET_ONCE: frozenset[tuple[str, str]] = frozenset(
    {
        # Own authentication: a TOTP secret, recovery codes, an invitation link.
        ("POST", "/api/v1/auth/enroll/totp/start"),
        ("POST", "/api/v1/auth/enroll/finish"),
        ("POST", "/api/v1/me/recovery-codes"),
        ("POST", "/api/v1/users/invite"),
        ("POST", "/api/v1/users/{user_id}/invite/resend"),
        # A stored credential's plaintext and a licence key (invariant 3).
        ("POST", "/api/v1/credentials/{credential_id}/checkout"),
        ("GET", "/api/v1/licenses/{license_id}/key"),
        # Enrollment material: lets a machine join the fleet.
        ("POST", "/api/v1/enrollment-tokens"),
        ("PUT", "/api/v1/sites/{site_id}/install-link"),
        ("POST", "/api/v1/sites/{site_id}/install-link/regenerate"),
        ("POST", "/api/v1/agent/releases/{release_id}/install-command"),
        ("POST", "/api/v1/agent/releases/{release_id}/deployment-token"),
    }
)


async def test_nosniff_on_all_responses(api_env) -> None:
    response = await api_env.client.get("/livez")
    assert response.headers["X-Content-Type-Options"] == "nosniff"


async def test_checkout_response_is_not_cacheable(api_env) -> None:
    org_id = await api_env.create_org("hdr")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    created = await api_env.client.post(
        "/api/v1/credentials",
        json={"name": "c", "type": "windows_admin", "secret": "s3cr3t"},
        headers=headers,
    )
    cred_id = created.json()["id"]
    checkout = await api_env.client.post(
        f"/api/v1/credentials/{cred_id}/checkout",
        json={"reason": "test"},
        headers=headers,
    )
    assert checkout.status_code == 200
    # the plaintext secret must not sit in any cache
    assert checkout.headers["Cache-Control"] == "no-store"
    assert checkout.headers.get("Pragma") == "no-cache"


async def test_ordinary_responses_not_forced_no_store(api_env) -> None:
    org_id = await api_env.create_org("hdr")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    listing = await api_env.client.get("/api/v1/credentials", headers=headers)
    # a non-secret listing does not carry the no-store directive
    assert listing.headers.get("Cache-Control") != "no-store"


# --- every answer that carries a secret once (final review C-M9) -----------


def test_the_secret_answers_are_the_listed_ones() -> None:
    """The middleware's list and this one are the same set: a route added to
    either alone is red here."""
    assert set(security_headers.NO_STORE_ROUTES) == set(SECRET_ONCE)


def test_every_listed_answer_names_a_real_route() -> None:
    """A renamed route would silently fall out of the header."""
    real = {
        (method, path)
        for path, route in flatten(build_router_only_app().router)
        for method in getattr(route, "methods", None) or ()
    }
    assert set(SECRET_ONCE) - real == set()


def test_every_never_exposed_route_that_returns_a_secret_is_listed() -> None:
    """The MCP guard's list already names every route that hands a secret
    back (invariant 1c(c)); each one whose reason says it RETURNS one must
    also be uncacheable. A new secret answer that reaches one list reaches
    the other, or this is red."""
    returning = {key for key, reason in NEVER_EXPOSED_REASONS.items() if "return" in reason}
    assert returning - set(SECRET_ONCE) == set()


@pytest.mark.parametrize(
    ("method", "path", "marked"),
    [
        ("POST", "/api/v1/users/0192/invite/resend", True),
        ("POST", "/api/v1/users/invite", True),
        ("GET", "/api/v1/licenses/abc/key", True),
        ("POST", "/api/v1/credentials/abc/checkout", True),
        ("GET", "/api/v1/users/0192/invite/resend", False),
        ("POST", "/api/v1/users/0192/invite/resend/more", False),
        ("POST", "/api/v1/users/a/b/invite/resend", False),
        ("GET", "/api/v1/licenses", False),
        ("POST", "/api/v1/auth/login", False),
    ],
)
def test_the_match_is_by_method_and_whole_path(method: str, path: str, marked: bool) -> None:
    assert security_headers.no_store(method, path) is marked


@pytest.fixture()
def auth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    return api_env


async def test_the_sign_in_secrets_are_answered_no_store(auth_env: Any) -> None:
    """The TOTP secret, the recovery codes (at enrolment and again from My
    security) and an invitation link: each `Cache-Control: no-store`. Drop a
    route from `NO_STORE_ROUTES` and its line here is red. The sign-in answer
    itself carries no secret in its body and is not marked."""
    org = await auth_env.create_org("hdr-auth")
    who = await staff(auth_env, org, "org-admin")
    await point_at(auth_env, org)
    signed = await login(auth_env, who)
    assert signed.headers.get("Cache-Control") != "no-store"

    start = await auth_env.client.post("/api/v1/auth/enroll/totp/start", headers=who.headers())
    assert start.status_code == 200, start.text
    assert start.headers["Cache-Control"] == "no-store"
    secret = parse_qs(urlsplit(start.json()["otpauth_uri"]).query)["secret"][0]
    confirm = await auth_env.client.post(
        "/api/v1/auth/enroll/totp/confirm",
        headers=who.headers(),
        json={"code": pyotp.TOTP(secret).now()},
    )
    assert confirm.status_code == 204, confirm.text
    finish = await auth_env.client.post("/api/v1/auth/enroll/finish", headers=who.headers())
    assert finish.status_code == 200, finish.text
    assert finish.headers["Cache-Control"] == "no-store"
    who.session = cookie(finish, SESSION_COOKIE)

    codes = await auth_env.client.post("/api/v1/me/recovery-codes", headers=who.headers())
    assert codes.status_code == 200, codes.text
    assert codes.headers["Cache-Control"] == "no-store"

    async with AsyncSession(auth_env.admin_engine) as session:
        tech = (
            await session.execute(
                select(Role.id).where(Role.name == "technician", Role.org_id.is_(None))
            )
        ).scalar_one()
    invited = await auth_env.client.post(
        "/api/v1/users/invite",
        headers=who.headers(),
        json={
            "email": "hdr-ana@toeshee.test",
            "display_name": "Ana",
            "grants": [{"role_id": str(tech)}],
        },
    )
    assert invited.status_code == 201, invited.text
    assert invited.headers["Cache-Control"] == "no-store"
    resent = await auth_env.client.post(
        f"/api/v1/users/{invited.json()['user_id']}/invite/resend", headers=who.headers()
    )
    assert resent.status_code == 200, resent.text
    assert resent.headers["Cache-Control"] == "no-store"


async def test_a_signed_in_technician_reads_my_security_without_no_store(auth_env: Any) -> None:
    """Non-vacuity: an ordinary read of the same person is not marked."""
    org = await auth_env.create_org("hdr-plain")
    who = await signed_in(auth_env, org, "technician")
    plain = await auth_env.client.get("/api/v1/me/security", headers=who.headers())
    assert plain.status_code == 200
    assert plain.headers.get("Cache-Control") != "no-store"
