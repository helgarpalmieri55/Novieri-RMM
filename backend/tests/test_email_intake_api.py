"""API surface, RBAC completeness, and the no-credential guarantee (spec §10).

ROUTE DISCOVERY IS FROM THE APP, NOT FROM THE MODULE ROUTER (and not from a
hand-maintained list). The task brief told this file to filter
`app.router.routes` by path prefix, on the claim that `app.routes` is the
vacuous one. Both claims are wrong for FastAPI 0.139.2: `FastAPI.routes` IS
`self.router.routes` (the same list object), and `include_router` appends a
single lazy `_IncludedRouter` wrapper whose own `.path` is `None`, so
filtering EITHER collection by a path prefix yields an empty list forever --
the exact vacuity the brief was trying to avoid, reached by the route it
recommended. `_app_routes` below therefore FLATTENS the app's router tree
(descending through `_IncludedRouter.original_router` and re-applying
`include_context.prefix`), which yields the real `APIRoute` objects under
their real, fully-prefixed paths.

That is strictly stronger than every sibling module's version of this test
(`test_notification_api.py`, `test_portal_api.py`), which reads the module's
OWN `router.routes` and therefore cannot tell whether the router was ever
mounted on the app at all. `test_the_routes_are_mounted_under_the_versioned_
prefix` below pins the flattener itself against an empty result, so a future
FastAPI that changes the wrapper's shape fails loudly here instead of
silently passing every RBAC assertion over nothing.
"""

import contextlib
import socket
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rbac import route_permission
from app.core.tenancy import admin_session, org_session
from app.core.vault import get_vault
from app.models import AuditEvent, Credential
from app.modules.email_intake import messages as messages_service
from app.modules.email_intake import poller as poller_module
from app.modules.email_intake.mailboxes import create_mailbox
from app.modules.email_intake.messages import BODY_EXCERPT_CHARS, MAX_LIMIT
from app.modules.email_intake.models import EmailMailbox, InboundMessage
from app.modules.email_intake.schema import MailboxCreate
from tests.test_email_intake_migration import _seed_category, _seed_user

EMAIL_INTAKE_PREFIX = "/api/v1/email-intake"
SECRET = "hunter2"

# Every route registration spec §10 tabulates, fully prefixed. Method+path,
# because GET/PATCH/DELETE share one path and each needs its own gate.
EXPECTED_ROUTES = {
    ("GET", f"{EMAIL_INTAKE_PREFIX}/mailboxes"),
    ("POST", f"{EMAIL_INTAKE_PREFIX}/mailboxes"),
    ("GET", f"{EMAIL_INTAKE_PREFIX}/mailboxes/{{mailbox_id}}"),
    ("PATCH", f"{EMAIL_INTAKE_PREFIX}/mailboxes/{{mailbox_id}}"),
    ("DELETE", f"{EMAIL_INTAKE_PREFIX}/mailboxes/{{mailbox_id}}"),
    ("POST", f"{EMAIL_INTAKE_PREFIX}/mailboxes/{{mailbox_id}}/test"),
    ("GET", f"{EMAIL_INTAKE_PREFIX}/messages"),
    # Microsoft 365 sign-in (docs/superpowers/specs/2026-09-11-mail-oauth-design.md).
    ("GET", f"{EMAIL_INTAKE_PREFIX}/oauth-app"),
    ("PUT", f"{EMAIL_INTAKE_PREFIX}/oauth-app"),
    ("POST", f"{EMAIL_INTAKE_PREFIX}/mailboxes/{{mailbox_id}}/oauth/start"),
    ("POST", f"{EMAIL_INTAKE_PREFIX}/mailboxes/{{mailbox_id}}/oauth/disconnect"),
    ("POST", f"{EMAIL_INTAKE_PREFIX}/oauth/complete"),
    ("POST", f"{EMAIL_INTAKE_PREFIX}/oauth/abandon"),
}


# --------------------------------------------------------------------------- #
# Router flattening (see the module docstring).
# --------------------------------------------------------------------------- #


def _flatten(router: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """`(full path, route)` for every real route reachable from `router`.

    `include_router` in this FastAPI appends a `_IncludedRouter` wrapper
    rather than flattening; the wrapper exposes the router it wrapped as
    `.original_router` and the prefix it was mounted under as
    `.include_context.prefix`. Descend through both, recursively, so a
    router included into a router included into the app is still found.
    """
    found: list[tuple[str, Any]] = []
    for route in getattr(router, "routes", []):
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            found.extend(_flatten(original, prefix + (getattr(context, "prefix", "") or "")))
        else:
            found.append((prefix + getattr(route, "path", ""), route))
    return found


def _app_routes(app: Any) -> list[tuple[str, Any]]:
    return [
        (path, route)
        for path, route in _flatten(app.router)
        if path.startswith(EMAIL_INTAKE_PREFIX)
    ]


def _method_paths(app: Any) -> set[tuple[str, str]]:
    return {
        (method, path)
        for path, route in _app_routes(app)
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
    }


# --------------------------------------------------------------------------- #
# Seeding.
# --------------------------------------------------------------------------- #


@dataclass
class _Env:
    api_env: Any
    org_id: UUID
    actor_id: UUID
    category_id: UUID
    mailbox_id: UUID
    credential_id: UUID
    admin_headers: dict[str, str]


async def _seed_mailbox(
    api_env: Any,
    *,
    password: str = SECRET,
    host: str = "imap.acme.test",
    port: int = 993,
    address: str = "helpdesk@acme.test",
    use_ssl: bool = True,
) -> _Env:
    """A mailbox that ACTUALLY HAS a vault-held credential.

    The `assert` on the checkout below is not decoration: an equivalent
    "no credential in the response" test elsewhere in this codebase was found
    vacuous because its fixture had no credential to leak. This one fails
    here, in the fixture, if the secret is not really stored.
    """
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    category_id = await _seed_category(api_env, org_id)
    vault = get_vault(api_env.settings)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        mailbox = await create_mailbox(
            session,
            org_id,
            actor_id,
            MailboxCreate(
                address=address,
                host=host,
                port=port,
                use_ssl=use_ssl,
                username=address,
                password=password,
                default_category_id=category_id,
            ),
            vault=vault,
        )
        mailbox_id, credential_id = mailbox.id, mailbox.credential_id

    async with org_session(org_id, engine=api_env.app_engine) as session:
        stored = await vault.checkout(session, org_id=org_id, credential_id=credential_id)
    assert stored == password, "fixture is vacuous: no credential was stored"

    sub = await api_env.create_user(org_id, "org-admin")
    return _Env(
        api_env=api_env,
        org_id=org_id,
        actor_id=actor_id,
        category_id=category_id,
        mailbox_id=mailbox_id,
        credential_id=credential_id,
        admin_headers=await api_env.headers(org_id, sub),
    )


async def _seed_messages(api_env: Any) -> _Env:
    """Five inbound rows on one mailbox: two quarantined, three not.

    `created_at` is 12:00..12:04 and `received_at` is FIVE YEARS EARLIER on
    every row, deliberately: `received_at` is the sender's own `Date:` header
    and must decide neither visibility nor order, so the time-range
    assertions below are written against `created_at` and would fail if the
    filter ever went back to `received_at`.
    """
    env = await _seed_mailbox(api_env)
    base = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    rows = [
        ("created", "one"),
        ("quarantined", "two"),
        ("commented", "three"),
        ("quarantined", "four"),
        ("refused_loop", "five"),
    ]
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        for index, (disposition, tag) in enumerate(rows):
            session.add(
                InboundMessage(
                    org_id=env.org_id,
                    mailbox_id=env.mailbox_id,
                    message_id=f"<{tag}@acme.test>",
                    from_address=f"{tag}@acme.test",
                    subject=f"subject {tag}",
                    received_at=base + timedelta(minutes=index) - timedelta(days=365 * 5),
                    created_at=base + timedelta(minutes=index),
                    disposition=disposition,
                    reason="",
                    body_text=f"body of {tag}",
                )
            )
        await session.commit()
    return env


async def _reader_headers(api_env: Any, org_id: UUID, *codes: str) -> dict[str, str]:
    """A user holding EXACTLY `codes` -- there is no stock role with
    `email_intake.read` and not `email_intake.admin` (migration 0032 grants
    both codes only to super-admin/org-admin), so the read-only tier this
    module's RBAC split exists for has to be built here."""
    from app.models import Permission, Role, RolePermission, User, UserRole

    sub = f"sub-{uuid4().hex[:10]}"
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user = User(org_id=org_id, external_subject=sub, email=f"{sub}@test.local")
        role = Role(org_id=org_id, name=f"role-{uuid4().hex[:8]}")
        session.add_all([user, role])
        await session.flush()
        for code in codes:
            permission_id = (
                await session.execute(select(Permission.id).where(Permission.code == code))
            ).scalar_one()
            session.add(RolePermission(role_id=role.id, permission_id=permission_id))
        session.add(UserRole(org_id=org_id, user_id=user.id, role_id=role.id))
        await session.commit()
    return await api_env.headers(org_id, sub)


async def _credentials(api_env: Any, org_id: UUID) -> list[Credential]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (await session.execute(select(Credential).where(Credential.org_id == org_id)))
            .scalars()
            .all()
        )


async def _audit_actions(api_env: Any, org_id: UUID) -> list[str]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent.action).where(AuditEvent.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )


# --------------------------------------------------------------------------- #
# RBAC completeness, derived from the router (invariant #6).
# --------------------------------------------------------------------------- #


async def test_the_routes_are_mounted_under_the_versioned_prefix(api_env) -> None:
    """Pins the FLATTENER as well as the routes. If `_flatten` ever stops
    finding real routes -- a FastAPI that changes `_IncludedRouter`'s shape,
    or a `register()` that is never called -- this fails loudly instead of
    letting every RBAC assertion below pass over an empty list."""
    assert _method_paths(api_env.app) == EXPECTED_ROUTES


async def test_every_route_carries_a_permission(api_env) -> None:
    """Invariant #6, DERIVED from the router rather than a hand-maintained
    list -- a route added later without require_permission fails here."""
    routes = _app_routes(api_env.app)
    # `len(EXPECTED_ROUTES)`, not a smaller literal: this test must not
    # silently degrade to "at least some of the routes are gated" if the
    # separate mounting test above is ever weakened or deleted.
    assert len(routes) >= len(EXPECTED_ROUTES), (
        f"expected the email-intake routes, found {len(routes)}"
    )
    for path, route in routes:
        assert route_permission(route) is not None, f"{path} has no permission"


async def test_write_routes_require_the_admin_permission(api_env) -> None:
    for path, route in _app_routes(api_env.app):
        methods = getattr(route, "methods", set())
        if methods & {"POST", "PATCH", "PUT", "DELETE"}:
            assert route_permission(route) == "email_intake.admin", path


async def test_read_routes_require_the_read_permission(api_env) -> None:
    for path, route in _app_routes(api_env.app):
        methods = getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
        if methods == {"GET"}:
            assert route_permission(route) == "email_intake.read", path


async def test_a_reader_cannot_create_or_test_a_mailbox(api_env) -> None:
    """Server-side, not UI-side (invariant #6). The caller holds
    `email_intake.read` and NOTHING else, so this proves the read/admin split
    rather than merely proving an unprivileged user is refused."""
    env = await _seed_mailbox(api_env)
    headers = await _reader_headers(api_env, env.org_id, "email_intake.read")

    listed = await api_env.client.get(f"{EMAIL_INTAKE_PREFIX}/mailboxes", headers=headers)
    assert listed.status_code == 200

    for method, path in (
        ("POST", f"{EMAIL_INTAKE_PREFIX}/mailboxes"),
        ("PATCH", f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}"),
        ("DELETE", f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}"),
        ("POST", f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}/test"),
    ):
        response = await api_env.client.request(method, path, headers=headers, json={})
        assert response.status_code == 403, (method, path, response.status_code)


async def test_a_user_with_no_email_intake_grant_is_refused(api_env) -> None:
    org_id = await api_env.create_org()
    sub = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, sub)
    response = await api_env.client.get(f"{EMAIL_INTAKE_PREFIX}/mailboxes", headers=headers)
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# The no-credential guarantee (invariant #3, spec §10/§11.12).
# --------------------------------------------------------------------------- #


async def test_no_response_contains_the_password_or_a_credential_id(api_env) -> None:
    """Asserted over the RAW BODY, so a credential leaking through a nested
    or newly-added field is caught. The fixture's mailbox genuinely has a
    stored credential (`_seed_mailbox` proves it by checking it out)."""
    env = await _seed_mailbox(api_env, password=SECRET)
    for path in (
        f"{EMAIL_INTAKE_PREFIX}/mailboxes",
        f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}",
        f"{EMAIL_INTAKE_PREFIX}/messages",
    ):
        response = await api_env.client.get(path, headers=env.admin_headers)
        assert response.status_code == 200, path
        body = response.text
        assert SECRET not in body, path
        assert str(env.credential_id) not in body, path
        assert "credential_id" not in body, path

    # ...and the mailbox response's FIELD SET is pinned. `has_credential is
    # True` on its own asserts nothing -- `email_mailbox.credential_id` is
    # NOT NULL, so the flag is a tautology (schema.py says so) and that
    # assertion cannot fail. What can fail, and is the actual invariant-#3
    # contract, is a field appearing that should not: this catches a
    # `credential_id` (or any other new column) added to `MailboxOut` later,
    # which the substring checks above would only catch if the value happened
    # to be non-null.
    detail = await api_env.client.get(
        f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}", headers=env.admin_headers
    )
    assert set(detail.json()) == {
        "id",
        "address",
        "host",
        "port",
        "use_ssl",
        "folder",
        "username",
        "has_credential",
        "auth_kind",
        "credential_state",
        "default_category_id",
        "reopen_window_days",
        "active",
        "last_polled_at",
        "last_error",
        "created_at",
    }


async def test_the_mailbox_response_surfaces_last_error_and_last_polled_at(api_env) -> None:
    """A poison message stops its own mailbox by design (Task 7), so this
    surface is the only place an operator learns a mailbox is failing."""
    env = await _seed_mailbox(api_env)
    polled = datetime(2026, 5, 1, 9, 30, tzinfo=UTC)
    async with AsyncSession(api_env.admin_engine) as session:
        mailbox = await session.get(EmailMailbox, env.mailbox_id)
        assert mailbox is not None
        mailbox.last_error = "tls failed: certificate-invalid"
        mailbox.last_polled_at = polled
        await session.commit()

    response = await api_env.client.get(
        f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}", headers=env.admin_headers
    )
    body = response.json()
    assert body["last_error"] == "tls failed: certificate-invalid"
    assert body["last_polled_at"] is not None


# --------------------------------------------------------------------------- #
# Tenancy: not-mine is 404, never 403.
# --------------------------------------------------------------------------- #


async def test_a_mailbox_from_another_org_is_404(api_env) -> None:
    """Never 403 -- distinguishing 'exists elsewhere' from 'does not exist'
    is an org-membership oracle. Every by-id route, not only the GET."""
    env = await _seed_mailbox(api_env)
    other_org = await api_env.create_org("other")
    other_sub = await api_env.create_user(other_org, "org-admin")
    other_headers = await api_env.headers(other_org, other_sub)

    for method, path, payload in (
        ("GET", f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}", None),
        ("PATCH", f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}", {"folder": "Other"}),
        ("DELETE", f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}", None),
        ("POST", f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}/test", None),
    ):
        response = await api_env.client.request(
            method, path, headers=other_headers, json=payload
        )
        assert response.status_code == 404, (method, response.status_code)

    # ...and the other org's list does not contain it either.
    listed = await api_env.client.get(
        f"{EMAIL_INTAKE_PREFIX}/mailboxes", headers=other_headers
    )
    assert listed.json() == []


# --------------------------------------------------------------------------- #
# The message log (spec §10).
# --------------------------------------------------------------------------- #


async def test_the_message_log_filters_and_reports_the_quarantined_count(api_env) -> None:
    """Spec §10: quarantined mail is mail a human expected to become a
    ticket and which did not. It is invisible unless surfaced."""
    env = await _seed_messages(api_env)
    response = await api_env.client.get(
        f"{EMAIL_INTAKE_PREFIX}/messages", headers=env.admin_headers
    )
    body = response.json()
    assert body["quarantined_count"] == 2
    assert len(body["messages"]) == 5

    filtered = await api_env.client.get(
        f"{EMAIL_INTAKE_PREFIX}/messages?disposition=quarantined", headers=env.admin_headers
    )
    assert {m["disposition"] for m in filtered.json()["messages"]} == {"quarantined"}


async def test_the_quarantined_count_is_not_a_count_of_the_page(api_env) -> None:
    """It comes from a count over the whole filtered set. A page of one
    `created` message still reports both quarantined rows -- counting the
    page would report zero and the alert would vanish exactly when an
    operator narrowed the view to look at something."""
    env = await _seed_messages(api_env)

    paged = await api_env.client.get(
        f"{EMAIL_INTAKE_PREFIX}/messages?limit=1", headers=env.admin_headers
    )
    assert len(paged.json()["messages"]) == 1
    assert paged.json()["quarantined_count"] == 2

    # DELIBERATE: `disposition` narrows the LIST but not the COUNT. A count
    # that honoured it would read 0 whenever an operator filtered to anything
    # else, which is precisely when the number needs to still be visible.
    created_only = await api_env.client.get(
        f"{EMAIL_INTAKE_PREFIX}/messages?disposition=created", headers=env.admin_headers
    )
    assert {m["disposition"] for m in created_only.json()["messages"]} == {"created"}
    assert created_only.json()["quarantined_count"] == 2


async def test_the_message_log_filters_by_mailbox_and_time_range(api_env) -> None:
    """`mailbox_id` and the time range DO narrow the count -- they are scope,
    not a disposition view."""
    env = await _seed_messages(api_env)
    other = await api_env.client.get(
        f"{EMAIL_INTAKE_PREFIX}/messages?mailbox_id={uuid4()}", headers=env.admin_headers
    )
    assert other.json()["messages"] == []
    assert other.json()["quarantined_count"] == 0

    mine = await api_env.client.get(
        f"{EMAIL_INTAKE_PREFIX}/messages?mailbox_id={env.mailbox_id}",
        headers=env.admin_headers,
    )
    assert len(mine.json()["messages"]) == 5

    # Rows are at 12:00..12:04; from 12:03 keeps the last two, of which one
    # ("four") is quarantined.
    since = await api_env.client.get(
        f"{EMAIL_INTAKE_PREFIX}/messages?since=2026-05-01T12:03:00Z",
        headers=env.admin_headers,
    )
    assert len(since.json()["messages"]) == 2
    assert since.json()["quarantined_count"] == 1

    until = await api_env.client.get(
        f"{EMAIL_INTAKE_PREFIX}/messages?until=2026-05-01T12:01:00Z",
        headers=env.admin_headers,
    )
    assert len(until.json()["messages"]) == 2
    assert until.json()["quarantined_count"] == 1


async def test_the_message_log_reports_the_total_for_paging(api_env) -> None:
    """`total` is the extent of the LIST -- every filter including
    `disposition` -- so a caller can tell whether another page exists.
    Distinct from `quarantined_count`, which describes the scope and ignores
    `disposition`."""
    env = await _seed_messages(api_env)
    paged = (
        await api_env.client.get(
            f"{EMAIL_INTAKE_PREFIX}/messages?limit=2", headers=env.admin_headers
        )
    ).json()
    assert len(paged["messages"]) == 2
    assert paged["total"] == 5
    assert paged["quarantined_count"] == 2

    narrowed = (
        await api_env.client.get(
            f"{EMAIL_INTAKE_PREFIX}/messages?disposition=quarantined&limit=1",
            headers=env.admin_headers,
        )
    ).json()
    # The list is narrowed, so `total` is too...
    assert narrowed["total"] == 2
    # ...while the alarm still counts the whole scope.
    assert narrowed["quarantined_count"] == 2


# --------------------------------------------------------------------------- #
# The sender does not choose what the operator sees (`received_at` is a
# `Date:` header).
# --------------------------------------------------------------------------- #


async def _seed_dated_messages(api_env: Any) -> tuple[_Env, datetime]:
    """Three quarantined rows recorded one minute apart, whose `Date:`
    headers are absurd in both directions.

    Creation order is future -> past -> honest, so if ordering followed
    `received_at` the 2099 row would be row 1, and if it followed
    `created_at` the honest row is. Returns the first row's `created_at`.
    """
    env = await _seed_mailbox(api_env)
    recorded = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    rows = [
        ("future", datetime(2099, 1, 1, tzinfo=UTC)),
        ("past", datetime(2001, 1, 1, tzinfo=UTC)),
        ("honest", recorded + timedelta(minutes=2)),
    ]
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        for index, (tag, dated) in enumerate(rows):
            session.add(
                InboundMessage(
                    org_id=env.org_id,
                    mailbox_id=env.mailbox_id,
                    message_id=f"<{tag}@acme.test>",
                    from_address=f"{tag}@acme.test",
                    subject=tag,
                    received_at=dated,
                    created_at=recorded + timedelta(minutes=index),
                    disposition="quarantined",
                    reason="unknown sender",
                    body_text=f"body of {tag}",
                )
            )
        await session.commit()
    return env, recorded


async def test_a_forged_date_header_cannot_hide_a_message_from_the_window(api_env) -> None:
    """`received_at` is `parsing._extract_received_at` -- the SENDER'S `Date:`
    header. If the time filter used it, anyone who can send mail to a
    monitored mailbox could date a message 2001 and drop it out of the
    operator's "recent" view AND out of that view's quarantined count. That
    is the alarm going quiet for precisely the message that was buried.

    Filtering on `created_at` -- when the platform recorded the row, which no
    sender influences -- is what makes all three visible and counted.
    """
    env, recorded = await _seed_dated_messages(api_env)
    since = (recorded - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    until = (recorded + timedelta(hours=24)).isoformat().replace("+00:00", "Z")

    body = (
        await api_env.client.get(
            f"{EMAIL_INTAKE_PREFIX}/messages?since={since}&until={until}",
            headers=env.admin_headers,
        )
    ).json()
    assert {m["subject"] for m in body["messages"]} == {"future", "past", "honest"}
    assert body["total"] == 3
    # The 2001-dated row is INSIDE the window and counted: 3, not 2.
    assert body["quarantined_count"] == 3

    # And the window still means something -- it is not simply ignoring the
    # bounds. A window that ends before anything was recorded is empty even
    # though one row's `Date:` says 2099.
    empty = (
        await api_env.client.get(
            f"{EMAIL_INTAKE_PREFIX}/messages"
            f"?until={(recorded - timedelta(hours=1)).isoformat().replace('+00:00', 'Z')}",
            headers=env.admin_headers,
        )
    ).json()
    assert empty["messages"] == []
    assert empty["quarantined_count"] == 0


async def test_a_forged_date_header_cannot_pin_a_message_to_the_top(api_env) -> None:
    """The other half, and the sharper one: `received_at` was also the SORT
    KEY, so a message dated 2099 owned row 1 of every page forever and pushed
    real mail off the front of the operator's only diagnosis surface.

    Ordering on `created_at` puts the newest RECORDED row first. The 2099 row
    was recorded FIRST, so it is now last.
    """
    env, _ = await _seed_dated_messages(api_env)
    listed = (
        await api_env.client.get(
            f"{EMAIL_INTAKE_PREFIX}/messages", headers=env.admin_headers
        )
    ).json()["messages"]
    assert [m["subject"] for m in listed] == ["honest", "past", "future"]

    top = (
        await api_env.client.get(
            f"{EMAIL_INTAKE_PREFIX}/messages?limit=1", headers=env.admin_headers
        )
    ).json()["messages"]
    assert [m["subject"] for m in top] == ["honest"]

    # `received_at` is still SHOWN -- it is useful, and a wild value is itself
    # diagnostic -- next to the `created_at` that actually decides.
    by_subject = {m["subject"]: m for m in listed}
    assert by_subject["future"]["received_at"].startswith("2099")
    assert by_subject["past"]["received_at"].startswith("2001")
    assert by_subject["future"]["created_at"].startswith("2026")


# --------------------------------------------------------------------------- #
# The list response is bounded (a stored body is up to 1 MiB, and the sender
# chooses that).
# --------------------------------------------------------------------------- #


async def test_the_list_truncates_body_text_and_stays_bounded(api_env) -> None:
    """`parsing._MAX_BODY_CHARS` caps a stored body at 1 MiB and whoever sent
    the mail chooses that size; at `MAX_LIMIT` the untruncated list was a
    ~200 MB response any `email_intake.read` holder could ask for. The list
    serves a `BODY_EXCERPT_CHARS` excerpt with `body_truncated` set, which
    bounds a full page at well under 2 MiB.
    """
    env = await _seed_mailbox(api_env)
    planted = 8
    huge = "x" * (1024 * 1024)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        for index in range(planted):
            session.add(
                InboundMessage(
                    org_id=env.org_id,
                    mailbox_id=env.mailbox_id,
                    message_id=f"<big-{index}@acme.test>",
                    from_address="planter@evil.test",
                    subject=f"big {index}",
                    received_at=datetime(2026, 6, 1, tzinfo=UTC),
                    disposition="quarantined",
                    reason="unknown sender",
                    body_text=huge,
                )
            )
        await session.commit()

    response = await api_env.client.get(
        f"{EMAIL_INTAKE_PREFIX}/messages?limit={MAX_LIMIT}", headers=env.admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["messages"]) == planted
    for message in body["messages"]:
        assert len(message["body_text"]) == BODY_EXCERPT_CHARS
        assert message["body_truncated"] is True

    # THE STATED BOUND: a full page of the largest rows a sender can plant
    # stays under 2 MiB. Derived from what a page of them actually costs on
    # the wire rather than from the excerpt constant alone, so per-row
    # overhead is included. Without truncation the same page would be ~200 MB.
    page_bound = 2 * 1024 * 1024
    per_row = len(response.content) / planted
    assert per_row * MAX_LIMIT < page_bound, per_row
    assert (1024 * 1024) * MAX_LIMIT > page_bound  # what it would have been


def test_an_off_vocabulary_disposition_does_not_500_the_page() -> None:
    """A single malformed row must not take out the operator's ONLY
    diagnosis surface. `_message_out` is a pure function over a row, so this
    needs no database -- which is the point: the row it is handed here cannot
    exist while `ck_inbound_message_disposition_valid` holds, and the test is
    about what happens if that ever stops being true (a migration defect, a
    hand-edited row).
    """
    row = InboundMessage(
        id=uuid4(),
        org_id=uuid4(),
        mailbox_id=uuid4(),
        message_id="<odd@acme.test>",
        from_address="odd@acme.test",
        subject="odd",
        received_at=datetime(2026, 6, 1, tzinfo=UTC),
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        disposition="not-a-disposition",
        reason="",
        body_text="short",
    )
    out = messages_service._message_out(row)
    assert out.disposition == "not-a-disposition"
    assert out.body_truncated is False
    assert out.body_text == "short"


async def test_the_message_log_exposes_body_text_to_a_reader(api_env) -> None:
    """Spec §10, deliberately: it is how an operator diagnoses a quarantined
    message. A `email_intake.read` holder -- not only an admin -- sees it."""
    env = await _seed_messages(api_env)
    headers = await _reader_headers(api_env, env.org_id, "email_intake.read")
    response = await api_env.client.get(
        f"{EMAIL_INTAKE_PREFIX}/messages?disposition=quarantined", headers=headers
    )
    assert response.status_code == 200
    bodies = {m["body_text"] for m in response.json()["messages"]}
    assert bodies == {"body of two", "body of four"}


async def test_the_message_log_is_scoped_to_the_callers_org(api_env) -> None:
    await _seed_messages(api_env)
    other_org = await api_env.create_org("other")
    other_sub = await api_env.create_user(other_org, "org-admin")
    other_headers = await api_env.headers(other_org, other_sub)
    response = await api_env.client.get(
        f"{EMAIL_INTAKE_PREFIX}/messages", headers=other_headers
    )
    assert response.json() == {"messages": [], "total": 0, "quarantined_count": 0}


async def test_the_message_log_org_predicate_is_load_bearing(api_env) -> None:
    """F2. `test_the_message_log_is_scoped_to_the_callers_org` above goes
    through the API, i.e. through `org_session`, where RLS rejects the other
    org's rows before `_scope`'s own `InboundMessage.org_id == org_id` ever
    gets a chance to -- deleting that predicate leaves it GREEN, so the
    module's belt-and-braces half is untested and could be removed later
    without anything noticing.

    This probe calls the service on the RLS-BYPASSING admin engine, the same
    device
    `test_email_intake_intake.py::test_the_sender_rate_limit_org_predicate_is_load_bearing`
    and `test_email_intake_identity.py::test_the_ticket_org_predicate_is_load_bearing`
    use, so `_scope`'s predicate is the only thing that can keep one org's
    mail out of another's log -- and out of BOTH counts, which is where a
    leak would be loudest (§10's quarantine alarm).
    """
    env = await _seed_messages(api_env)
    other_org = await api_env.create_org("other-scope")

    async with admin_session(engine=api_env.admin_engine) as session:
        mine = await messages_service.list_messages(session, env.org_id)
        theirs = await messages_service.list_messages(session, other_org)

    assert (theirs.messages, theirs.total, theirs.quarantined_count) == ([], 0, 0), (
        "CROSS-ORG LEAK: an org with no mail of its own saw another org's inbound "
        f"messages ({theirs.total} rows, {theirs.quarantined_count} quarantined)"
    )
    # ...and the fixture really had something to leak.
    assert (mine.total, mine.quarantined_count) == (5, 2)


# --------------------------------------------------------------------------- #
# Create/update/delete.
# --------------------------------------------------------------------------- #


def _create_payload(category_id: UUID, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "address": "support@acme.test",
        "host": "imap.acme.test",
        "port": 993,
        "username": "support@acme.test",
        "password": SECRET,
        "default_category_id": str(category_id),
    }
    payload.update(overrides)
    return payload


async def test_create_round_trips_without_ever_echoing_the_password(api_env) -> None:
    env = await _seed_mailbox(api_env)
    response = await api_env.client.post(
        f"{EMAIL_INTAKE_PREFIX}/mailboxes",
        json=_create_payload(env.category_id),
        headers=env.admin_headers,
    )
    assert response.status_code == 201, response.text
    assert SECRET not in response.text
    body = response.json()
    assert body["address"] == "support@acme.test"
    assert body["has_credential"] is True
    assert "email_intake.mailbox_created" in await _audit_actions(api_env, env.org_id)


async def test_a_duplicate_address_is_400_and_leaves_no_orphan_credential(api_env) -> None:
    """The losing create must roll back OUT of the session. If the route
    caught `MailboxValidationError` inside the `org_session` block and let
    the block exit normally, the transaction would COMMIT and an ownerless
    `Credential` row would survive."""
    env = await _seed_mailbox(api_env)
    before = len(await _credentials(api_env, env.org_id))

    first = await api_env.client.post(
        f"{EMAIL_INTAKE_PREFIX}/mailboxes",
        json=_create_payload(env.category_id),
        headers=env.admin_headers,
    )
    assert first.status_code == 201
    second = await api_env.client.post(
        f"{EMAIL_INTAKE_PREFIX}/mailboxes",
        json=_create_payload(env.category_id),
        headers=env.admin_headers,
    )
    assert second.status_code == 400, second.text

    # Exactly one new credential -- the winner's. Nothing orphaned.
    assert len(await _credentials(api_env, env.org_id)) == before + 1
    async with AsyncSession(api_env.admin_engine) as session:
        mailboxes = (
            await session.execute(
                select(func.count())
                .select_from(EmailMailbox)
                .where(EmailMailbox.org_id == env.org_id)
            )
        ).scalar_one()
    assert mailboxes == 2


async def _queueless_category(api_env: Any, org_id: UUID) -> UUID:
    from app.models import User
    from app.modules.service_desk.models import TicketCategory

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        creator = User(
            org_id=org_id,
            external_subject=f"sub-{uuid4().hex[:10]}",
            email=f"queueless-{uuid4().hex}@seed.invalid",
        )
        session.add(creator)
        await session.flush()
        category = TicketCategory(
            org_id=org_id,
            key=f"nq-{uuid4().hex[:6]}",
            name=f"nq-{uuid4().hex[:6]}",
            type="incident",
            requires_approval=False,
            default_queue_id=None,
            created_by=creator.id,
        )
        session.add(category)
        await session.commit()
        return category.id


async def test_a_queueless_default_category_is_refused_at_create(api_env) -> None:
    """A category with no `default_queue_id` can never produce a ticket:
    `create_ticket` refuses it and Task 5 records a quarantined row rather
    than stalling, so every message to such a mailbox is lost work. Refuse
    the CONFIGURATION -- the cheapest place to catch it."""
    env = await _seed_mailbox(api_env)
    category_id = await _queueless_category(api_env, env.org_id)
    before = len(await _credentials(api_env, env.org_id))

    response = await api_env.client.post(
        f"{EMAIL_INTAKE_PREFIX}/mailboxes",
        json=_create_payload(category_id),
        headers=env.admin_headers,
    )
    assert response.status_code == 400, response.text
    assert "queue" in response.json()["detail"].lower()
    # No mailbox, and no credential minted for the mailbox that never was.
    assert len(await _credentials(api_env, env.org_id)) == before


async def test_a_queueless_default_category_is_refused_at_update(api_env) -> None:
    env = await _seed_mailbox(api_env)
    category_id = await _queueless_category(api_env, env.org_id)
    response = await api_env.client.patch(
        f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}",
        json={"default_category_id": str(category_id)},
        headers=env.admin_headers,
    )
    assert response.status_code == 400, response.text
    assert "queue" in response.json()["detail"].lower()

    async with AsyncSession(api_env.admin_engine) as session:
        mailbox = await session.get(EmailMailbox, env.mailbox_id)
        assert mailbox is not None
        assert mailbox.default_category_id == env.category_id


async def test_an_unknown_category_is_400_and_an_unknown_mailbox_is_404(api_env) -> None:
    env = await _seed_mailbox(api_env)
    bad_category = await api_env.client.post(
        f"{EMAIL_INTAKE_PREFIX}/mailboxes",
        json=_create_payload(uuid4()),
        headers=env.admin_headers,
    )
    assert bad_category.status_code == 400

    missing = await api_env.client.get(
        f"{EMAIL_INTAKE_PREFIX}/mailboxes/{uuid4()}", headers=env.admin_headers
    )
    assert missing.status_code == 404


async def test_update_rotates_the_password_without_returning_it(api_env) -> None:
    env = await _seed_mailbox(api_env)
    response = await api_env.client.patch(
        f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}",
        json={"password": "rotated-secret", "folder": "Support"},
        headers=env.admin_headers,
    )
    assert response.status_code == 200, response.text
    assert "rotated-secret" not in response.text
    assert response.json()["folder"] == "Support"

    vault = get_vault(api_env.settings)
    async with org_session(env.org_id, engine=api_env.app_engine) as session:
        assert (
            await vault.checkout(session, org_id=env.org_id, credential_id=env.credential_id)
        ) == "rotated-secret"


async def test_delete_removes_the_mailbox_and_its_vault_credential(api_env) -> None:
    """`delete_mailbox` takes the vault so the credential dies with the
    mailbox; a DELETE route that omitted it would orphan the credential."""
    env = await _seed_mailbox(api_env)
    response = await api_env.client.delete(
        f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}", headers=env.admin_headers
    )
    assert response.status_code == 204, response.text

    async with AsyncSession(api_env.admin_engine) as session:
        assert await session.get(EmailMailbox, env.mailbox_id) is None
    assert [c.id for c in await _credentials(api_env, env.org_id)] == []
    assert "email_intake.mailbox_deleted" in await _audit_actions(api_env, env.org_id)


async def test_delete_refuses_a_mailbox_that_has_processed_mail(api_env) -> None:
    """The disposition log is the operator's only record of what arrived and
    why it was refused; deleting a mailbox must not erase it."""
    env = await _seed_messages(api_env)
    response = await api_env.client.delete(
        f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}", headers=env.admin_headers
    )
    assert response.status_code == 400, response.text
    async with AsyncSession(api_env.admin_engine) as session:
        assert await session.get(EmailMailbox, env.mailbox_id) is not None


# --------------------------------------------------------------------------- #
# The test route (spec §10).
# --------------------------------------------------------------------------- #


async def test_the_test_route_never_returns_a_raw_server_error(api_env) -> None:
    """Spec §10: it validates credentials by connecting and listing the
    folder, and returns neither the password nor a raw server response."""
    env = await _seed_mailbox(api_env, password=SECRET, host="127.0.0.1", port=1)
    response = await api_env.client.post(
        f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}/test", headers=env.admin_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert SECRET not in response.text
    assert len(body["detail"]) <= 200
    # The classification, not `str(exc)`: stage + reason from imap.py's fixed
    # vocabularies.
    assert body["detail"] == "connect failed: connection-refused"


async def test_the_test_route_is_audited_and_records_the_credential_use(api_env) -> None:
    env = await _seed_mailbox(api_env, host="127.0.0.1", port=1)
    await api_env.client.post(
        f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}/test", headers=env.admin_headers
    )
    actions = await _audit_actions(api_env, env.org_id)
    assert "email_intake.mailbox_tested" in actions
    # Invariant #4: the vault checkout this route performs is itself a
    # privileged action and is recorded.
    assert "email_intake.credential_used" in actions

    async with AsyncSession(api_env.admin_engine) as session:
        events = list(
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == env.org_id,
                        AuditEvent.action == "email_intake.mailbox_tested",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert SECRET not in str(events[0].meta)


async def test_the_test_route_is_rate_limited_per_org(api_env) -> None:
    """Spec §10. Unmetered, it is a per-request oracle for which internal
    hosts resolve and answer -- the same concern the notification module's
    test-send throttle exists for."""
    env = await _seed_mailbox(api_env, host="127.0.0.1", port=1)
    path = f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}/test"
    codes = [
        (await api_env.client.post(path, headers=env.admin_headers)).status_code
        for _ in range(6)
    ]
    assert codes[:5] == [200] * 5, codes
    assert codes[5] == 429, codes

    # The limit is PER ORG: a different org is unaffected.
    other = await _seed_mailbox(api_env, host="127.0.0.1", port=1)
    fresh = await api_env.client.post(
        f"{EMAIL_INTAKE_PREFIX}/mailboxes/{other.mailbox_id}/test",
        headers=other.admin_headers,
    )
    assert fresh.status_code == 200


class _TricklingServer:
    """A server that accepts and then emits one byte every 200ms, forever,
    never completing the greeting line.

    THIS IS THE SHAPE `IMAP_TIMEOUT_S` CANNOT CATCH. It is a per-socket-
    operation timeout, so it restarts on every byte: 30s of silence trips it,
    a byte every 200ms never does. Before the conversation deadline this held
    an API-process worker thread for as long as the server chose, and
    `asyncio.to_thread` cannot be cancelled, so nothing reclaimed it.

    `disconnected` is set when a send finally fails -- i.e. when the CLIENT's
    socket really went away. That is what distinguishes "the request gave up"
    (which a bare `wait_for` would also achieve, while the thread stayed
    stuck) from "the thread was actually released".
    """

    def __init__(self) -> None:
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = int(self._listener.getsockname()[1])
        self.disconnected = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        try:
            while not self._stop.is_set():
                try:
                    conn.sendall(b"*")
                except OSError:
                    self.disconnected.set()
                    return
                time.sleep(0.2)
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    def close(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self._listener.close()
        self._thread.join(timeout=5)


async def test_the_test_route_returns_within_its_deadline_against_a_trickling_server(
    api_env, monkeypatch
) -> None:
    """Spec §10's test route opens a socket to an ADMIN-SUPPLIED host from the
    API process, so it must be bounded in WALL CLOCK, not merely per socket
    operation.

    Without the deadlines this test does not fail slowly -- it never returns:
    the server tickles the socket often enough that no `IMAP_TIMEOUT_S` ever
    expires. The budgets are shrunk here only so the test is quick; the
    production numbers are `poller.TEST_CONVERSATION_DEADLINE_S` /
    `TEST_REQUEST_DEADLINE_S`.
    """
    monkeypatch.setattr(poller_module, "TEST_CONVERSATION_DEADLINE_S", 1.0)
    monkeypatch.setattr(poller_module, "TEST_REQUEST_DEADLINE_S", 3.0)
    server = _TricklingServer()
    try:
        env = await _seed_mailbox(
            api_env, host="127.0.0.1", port=server.port, use_ssl=False
        )
        started = time.monotonic()
        response = await api_env.client.post(
            f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}/test",
            headers=env.admin_headers,
        )
        elapsed = time.monotonic() - started

        assert response.status_code == 200, response.text
        assert response.json()["ok"] is False
        assert SECRET not in response.text
        # The REQUEST bound. Generous against the two database round trips
        # either side, and still an order of magnitude below the ">= 120s,
        # and unbounded under trickle" this route used to allow.
        assert elapsed < 10.0, elapsed

        # THE THREAD BOUND, which is the half a request deadline alone would
        # not give: the socket was closed underneath the blocked worker
        # thread rather than left running.
        assert server.disconnected.wait(timeout=10.0), "the connection was never closed"
    finally:
        server.close()


class _BrokenRedis:
    """Every command raises, as a Redis outage does."""

    async def incr(self, key: str) -> int:
        raise RedisConnectionError("Error 111 connecting to redis:6379. Connection refused.")

    async def expire(self, key: str, seconds: int) -> bool:
        raise RedisConnectionError("Error 111 connecting to redis:6379. Connection refused.")


async def test_the_test_route_fails_closed_when_redis_is_down(api_env) -> None:
    """A limiter that cannot count is not a limiter. Letting the request
    through would turn a Redis outage into an unmetered outbound probe with
    an informative failure vocabulary -- the one property the throttle exists
    to deny -- so it fails CLOSED with 503, and the connection is never
    attempted.

    503 rather than 500 because the request is well-formed and will work
    again shortly; and the body carries the classification only, never the
    Redis error (a Redis URL can carry credentials).
    """
    env = await _seed_mailbox(api_env, host="127.0.0.1", port=1)
    api_env.app.state.redis = _BrokenRedis()
    response = await api_env.client.post(
        f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}/test", headers=env.admin_headers
    )
    assert response.status_code == 503, response.text
    assert "unavailable" in response.json()["detail"]
    assert "6379" not in response.text and "Connection refused" not in response.text
    # Failing closed means the outbound connection was NOT made, so there is
    # nothing to audit and no credential was checked out.
    actions = await _audit_actions(api_env, env.org_id)
    assert "email_intake.mailbox_tested" not in actions
    assert "email_intake.credential_used" not in actions


@pytest.mark.parametrize("path", ["mailboxes", "messages"])
async def test_an_unauthenticated_request_is_401(api_env, path: str) -> None:
    response = await api_env.client.get(f"{EMAIL_INTAKE_PREFIX}/{path}")
    assert response.status_code == 401
