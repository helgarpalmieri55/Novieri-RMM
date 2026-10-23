"""Portal API (spec §5, §9).

The org_session test is the structural one: a portal route that opened an
org_session would connect as app_user and see the whole org, and no
behavioural test would necessarily catch it.

Note on the router-derived tests below: they walk `router.routes` off
`app.modules.portal.api.router` directly, not `app.routes`. This project's
FastAPI (0.139) wraps an included router in a lazy `_IncludedRouter` rather
than flattening its routes onto `app.routes` (confirmed empirically -- see
`tests/test_notification_api.py`'s identical note), so filtering `app.routes`
by a path prefix silently yields an empty list forever, not a failing
assertion that would catch a missing route. `router.routes` is unaffected by
inclusion and is what every sibling module's router-derived test already
uses for exactly this reason.
"""

import ast
import pathlib
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.core.rbac import route_permission
from app.modules.portal.api import router as portal_router

API = "/api/v1/portal"


def _portal_routes() -> list[Any]:
    routes = list(portal_router.routes)
    assert routes, "no portal routes registered"
    return routes


# --------------------------------------------------------------------------- #
# Requirement #2 (task brief): RBAC completeness derived from the router.
# --------------------------------------------------------------------------- #


def test_every_portal_route_requires_a_permission() -> None:
    """Router-derived, as in the notification module -- a portal route added
    later without a permission fails this test."""
    for route in _portal_routes():
        assert route_permission(route) is not None, f"{route.path} has no permission"


def test_reads_use_ticket_read_and_writes_use_ticket_write() -> None:
    """Finer-grained than "has some permission": every GET is
    portal.ticket.read, every POST is portal.ticket.write (spec §5's route
    table)."""
    for route in _portal_routes():
        methods = route.methods - {"HEAD"}
        permission = route_permission(route)
        for method in methods:
            if method == "GET":
                assert permission == "portal.ticket.read", (method, route.path, permission)
            else:
                assert permission == "portal.ticket.write", (method, route.path, permission)


def test_the_routes_match_the_spec_table() -> None:
    declared = {(m, r.path) for r in _portal_routes() for m in r.methods if m != "HEAD"}
    expected = {
        ("GET", "/portal/categories"),
        ("GET", "/portal/help/suggest"),
        ("GET", "/portal/help/articles/{article_id}"),
        ("GET", "/portal/tickets"),
        ("POST", "/portal/tickets"),
        ("GET", "/portal/tickets/{ticket_id}"),
        ("POST", "/portal/tickets/{ticket_id}/comments"),
        ("POST", "/portal/tickets/{ticket_id}/attachments"),
        ("GET", "/portal/tickets/{ticket_id}/attachments/{attachment_id}"),
    }
    assert declared == expected, declared


# --------------------------------------------------------------------------- #
# Structural: no portal module file (except principal.py) may reach the
# database by any path other than `portal_session` (spec §3, §9.7).
# --------------------------------------------------------------------------- #

# I3 fix. The ORIGINAL version of this check rejected exactly one thing: a
# `Name`/`ImportFrom` of the literal string "org_session". That is a PROPER
# SUBSET of what spec §3 actually requires ("It must be the ONLY way a
# portal request reaches the database") -- a future route written as any of
# the shapes the whole-branch review named would pass the old check silently
# and run as `app_user` (whole org) or `its_admin`-equivalent (BYPASSRLS,
# every org):
#
#   async with AsyncSession(request.app.state.app_engine) as session: ...
#   async with admin_session(engine=request.app.state.admin_engine) as s: ...
#   engine = db.get_engine()
#   engine = create_async_engine(request.app.state.settings.database_url)
#
# `_FORBIDDEN_DB_NAMES` below covers `org_session`/`admin_session` (imported
# or bare-referenced, exactly as the original check did for `org_session`
# alone) AND `app_engine`/`admin_engine`/`get_engine`/`get_admin_engine` --
# the latter four are reached as `request.app.state.app_engine` (an
# `ast.Attribute`, not a `Name`) at least as often as they would be imported,
# so this checks BOTH node shapes for every name in the set. `AsyncSession`
# and `create_async_engine` are checked separately, as bare CALLS (`ast.Call`
# with an `ast.Name` func) rather than any reference -- both names are
# legitimately used as TYPE ANNOTATIONS throughout this package (e.g.
# `_load_my_ticket_row(session: AsyncSession, ...)`), which are `Name` nodes
# but never `Call` nodes, so annotation usage can never trip this.
# `create_async_engine` has exactly one legitimate call site in this whole
# package -- `api.py::_portal_engine`, which mints the `portal_user`-role
# engine itself (see that function's own docstring) -- so calls inside that
# one function's subtree are excluded; every other call site, in every other
# function, is a violation.
_FORBIDDEN_DB_NAMES = frozenset(
    {"org_session", "admin_session", "app_engine", "admin_engine", "get_engine", "get_admin_engine"}
)


def _uses_a_forbidden_db_path(path: pathlib.Path) -> bool:
    """AST-based, not a bare substring scan (see the note above this test):
    true only if the file actually CODES one of the forbidden shapes -- a
    `Name`/`Attribute`/`ImportFrom` naming one of `_FORBIDDEN_DB_NAMES`, or a
    bare `AsyncSession(...)`/`create_async_engine(...)` call outside
    `_portal_engine`. A docstring or comment that merely *mentions* one of
    these in prose (`tickets.py`'s own module docstring does this,
    explaining why an EARLIER version of this module used to open a second,
    elevated connection and no longer does; this very test file's own
    docstring above does too) is a string literal, not one of these node
    types, so it can never trip this check."""
    tree = ast.parse(path.read_text(), filename=str(path))

    # The one legitimate `create_async_engine(...)` call site's subtree,
    # identified by node identity so its call is excluded below without
    # exempting `create_async_engine` calls ANYWHERE ELSE in the same file.
    portal_engine_subtree: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_portal_engine":
            portal_engine_subtree = {id(n) for n in ast.walk(node)}
            break

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(
            alias.name in _FORBIDDEN_DB_NAMES for alias in node.names
        ):
            return True
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_DB_NAMES:
            return True
        if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_DB_NAMES:
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "AsyncSession":
                return True
            if node.func.id == "create_async_engine" and id(node) not in portal_engine_subtree:
                return True
    return False


def test_no_portal_route_opens_anything_but_a_portal_session() -> None:
    """Spec §3, §9.7 -- structural, and the STRONG form of spec §3's claim:
    `portal_session` must be the ONLY way a portal request reaches the
    database. `org_session`/`admin_session` connect as `app_user`/an
    RLS-bypassing role, which see the whole org (or every org); a bare
    `AsyncSession(app_engine)` or `create_async_engine(...)` reaches the
    database with no `portal_user`-scoped policy applied at all.

    `principal.py` is exempt: it deliberately uses `org_session` (via
    `request.app.state.app_engine`) to read the caller's permissions BEFORE
    a portal context exists -- `portal_session` needs a requester id up
    front, which is exactly what this file is still establishing.

    This is deliberately an AST walk, not a substring scan: a naive text scan
    trips on this very test file's own docstrings, and on `tickets.py`'s
    module docstring, which discusses (in prose) why an earlier version of
    this module used to open a second `org_session`-scoped connection and no
    longer does -- those files contain the WORDS but not the CODE.
    """
    root = pathlib.Path(__file__).resolve().parent.parent / "app" / "modules" / "portal"
    found = list(root.rglob("*.py"))
    assert found, "portal package not found where expected"
    for path in found:
        if path.name == "principal.py":
            continue
        assert not _uses_a_forbidden_db_path(path), path


@pytest.mark.parametrize(
    "snippet",
    [
        "from app.core.tenancy import org_session\n",
        "from app.core.tenancy import admin_session\n",
        "def f(request):\n    return request.app.state.app_engine\n",
        "def f(request):\n    return request.app.state.admin_engine\n",
        "from app.core import db\ndef f():\n    return db.get_engine()\n",
        "from app.core import db\ndef f():\n    return db.get_admin_engine()\n",
        "from sqlalchemy.ext.asyncio import AsyncSession\n"
        "def f(engine):\n    return AsyncSession(engine)\n",
        "from sqlalchemy.ext.asyncio import create_async_engine\n"
        "def f(url):\n    return create_async_engine(url)\n",
    ],
)
def test_the_broadened_check_actually_catches_every_new_shape(
    tmp_path: pathlib.Path, snippet: str
) -> None:
    """Canary for I3's fix: each of these is a shape the review found the
    OLD, narrower check would have passed silently. Written to a throwaway
    file (never under `app/modules/portal`, so the real suite is unaffected)
    and fed straight to the checker -- if any of these ever stops tripping
    it, this fails at the file that caused it, not at some unrelated
    completeness test."""
    path = tmp_path / "synthetic.py"
    path.write_text(snippet)
    assert _uses_a_forbidden_db_path(path), snippet


def test_the_broadened_check_does_not_flag_portal_engines_own_call_site(
    tmp_path: pathlib.Path,
) -> None:
    """The one legitimate `create_async_engine(...)` call in the whole
    package -- `api.py::_portal_engine` -- must NOT trip the check, or this
    test suite would be unable to pass at all."""
    snippet = (
        "from sqlalchemy.ext.asyncio import create_async_engine\n"
        "def _portal_engine(request):\n"
        "    return create_async_engine(request.app.state.settings.database_portal_url)\n"
    )
    path = tmp_path / "synthetic.py"
    path.write_text(snippet)
    assert not _uses_a_forbidden_db_path(path)


# --------------------------------------------------------------------------- #
# Functional: the RBAC boundary and the 404-not-403 discipline.
# --------------------------------------------------------------------------- #


async def test_a_technician_cannot_reach_the_portal(api_env) -> None:
    org_id = await api_env.create_org("portal-tech")
    sub = await api_env.create_user(org_id, "technician")
    got = await api_env.client.get(
        f"{API}/tickets", headers=await api_env.headers(org_id, sub)
    )
    assert got.status_code == 403


async def test_a_requester_cannot_reach_a_staff_route(api_env) -> None:
    """The other direction, and the more important one: the requester tier must
    not open the org-wide ticket API."""
    org_id = await api_env.create_org("portal-cross")
    sub = await api_env.create_user(org_id, "requester")
    got = await api_env.client.get(
        "/api/v1/service-desk/tickets", headers=await api_env.headers(org_id, sub)
    )
    assert got.status_code == 403


async def test_another_requesters_ticket_is_404(api_env, a_ticket_for) -> None:
    org_id = await api_env.create_org("portal-api-404")
    theirs = await a_ticket_for(org_id, "them")
    sub = await api_env.create_user(org_id, "requester")
    got = await api_env.client.get(
        f"{API}/tickets/{theirs.id}", headers=await api_env.headers(org_id, sub)
    )
    assert got.status_code == 404, "403 would confirm the id exists"


async def test_an_absent_ticket_is_also_404(api_env) -> None:
    org_id = await api_env.create_org("portal-api-404-absent")
    sub = await api_env.create_user(org_id, "requester")
    got = await api_env.client.get(
        f"{API}/tickets/{uuid4()}", headers=await api_env.headers(org_id, sub)
    )
    assert got.status_code == 404


# --------------------------------------------------------------------------- #
# Functional: end-to-end walk -- categories, create, get, comment, attach,
# download, all through the real HTTP surface, as a real requester.
# --------------------------------------------------------------------------- #


async def test_requester_can_file_view_comment_and_attach(api_env) -> None:
    from moto import mock_aws
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import TicketCategory, TicketQueue, User

    org_id = await api_env.create_org("portal-e2e")
    sub = await api_env.create_user(org_id, "requester")
    headers = await api_env.headers(org_id, sub)

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            user_id = (
                await session.execute(select(User.id).where(User.external_subject == sub))
            ).scalar_one()
            queue = TicketQueue(
                org_id=org_id, key="q-e2e", name="Q", created_by=user_id
            )
            session.add(queue)
            await session.flush()
            category = TicketCategory(
                org_id=org_id,
                key="c-e2e",
                name="C",
                type="incident",
                default_queue_id=queue.id,
                created_by=user_id,
            )
            session.add(category)
            await session.flush()
            category_id = category.id

    # No categories exist that aren't the fixture-created one above -- list
    # them through the portal surface.
    listed = await api_env.client.get(f"{API}/categories", headers=headers)
    assert listed.status_code == 200, listed.text
    assert any(c["id"] == str(category_id) for c in listed.json())

    created = await api_env.client.post(
        f"{API}/tickets",
        json={"category_id": str(category_id), "subject": "My printer is on fire"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    ticket = created.json()
    assert ticket["subject"] == "My printer is on fire"
    assert "queue_id" not in ticket
    assert "assigned_to" not in ticket

    mine = await api_env.client.get(f"{API}/tickets", headers=headers)
    assert mine.status_code == 200, mine.text
    assert any(t["id"] == ticket["id"] for t in mine.json())

    detail = await api_env.client.get(f"{API}/tickets/{ticket['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["id"] == ticket["id"]

    comment = await api_env.client.post(
        f"{API}/tickets/{ticket['id']}/comments",
        json={"body": "any update?"},
        headers=headers,
    )
    assert comment.status_code == 201, comment.text

    mock = mock_aws()
    mock.start()
    try:
        api_env.app.state.object_store = None
        uploaded = await api_env.client.post(
            f"{API}/tickets/{ticket['id']}/attachments?filename=screenshot.png",
            content=b"fake-png-bytes",
            headers=headers,
        )
        assert uploaded.status_code == 201, uploaded.text
        attachment = uploaded.json()

        downloaded = await api_env.client.get(
            f"{API}/tickets/{ticket['id']}/attachments/{attachment['id']}", headers=headers
        )
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == b"fake-png-bytes"
    finally:
        mock.stop()


async def test_a_requester_cannot_comment_on_someone_elses_ticket(
    api_env, a_ticket_for
) -> None:
    org_id = await api_env.create_org("portal-cross-comment")
    theirs = await a_ticket_for(org_id, "them")
    sub = await api_env.create_user(org_id, "requester")
    got = await api_env.client.post(
        f"{API}/tickets/{theirs.id}/comments",
        json={"body": "sneaky"},
        headers=await api_env.headers(org_id, sub),
    )
    assert got.status_code == 404


async def test_a_requester_cannot_attach_to_someone_elses_ticket(
    api_env, a_ticket_for
) -> None:
    from moto import mock_aws

    org_id = await api_env.create_org("portal-cross-attach")
    theirs = await a_ticket_for(org_id, "them")
    sub = await api_env.create_user(org_id, "requester")

    mock = mock_aws()
    mock.start()
    try:
        got = await api_env.client.post(
            f"{API}/tickets/{theirs.id}/attachments?filename=x.png",
            content=b"bytes",
            headers=await api_env.headers(org_id, sub),
        )
        assert got.status_code == 404
    finally:
        mock.stop()


async def test_a_staff_uploaded_attachment_is_not_downloadable_by_the_requester(
    api_env,
) -> None:
    """I1 -- `ticket_attachment` has no visibility column, so the parent-
    ticket-is-mine RLS policy alone admits a staff-uploaded file on the
    requester's OWN ticket. The portal's own download route must additionally
    scope to `uploaded_by`, or this comes back 200 with the staff-only body."""
    from moto import mock_aws
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.storage import ObjectStore
    from app.models import TicketCategory, TicketQueue, User
    from app.modules.service_desk import threads as sd_threads

    org_id = await api_env.create_org("portal-i1-download")
    sub = await api_env.create_user(org_id, "requester")
    headers = await api_env.headers(org_id, sub)

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            user_id = (
                await session.execute(select(User.id).where(User.external_subject == sub))
            ).scalar_one()
            queue = TicketQueue(org_id=org_id, key="q-i1", name="Q", created_by=user_id)
            session.add(queue)
            await session.flush()
            category = TicketCategory(
                org_id=org_id,
                key="c-i1",
                name="C",
                type="incident",
                default_queue_id=queue.id,
                created_by=user_id,
            )
            session.add(category)
            await session.flush()
            category_id = category.id

    created = await api_env.client.post(
        f"{API}/tickets",
        json={"category_id": str(category_id), "subject": "printer on fire"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    ticket_id = created.json()["id"]

    mock = mock_aws()
    mock.start()
    try:
        api_env.app.state.object_store = None
        async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
            async with session.begin():
                staff = User(
                    org_id=org_id, external_subject="staff-i1", email="staff-i1@test.local"
                )
                session.add(staff)
                await session.flush()
                attachment = await sd_threads.add_attachment(
                    session,
                    org_id,
                    staff.id,
                    UUID(ticket_id),
                    filename="dossier.txt",
                    data=b"STAFF ONLY escalation dossier",
                    store=ObjectStore(api_env.settings),
                )
                attachment_id = attachment.id

        got = await api_env.client.get(
            f"{API}/tickets/{ticket_id}/attachments/{attachment_id}", headers=headers
        )
        assert got.status_code == 404, got.text
        assert "STAFF ONLY" not in got.text
    finally:
        mock.stop()

    # And the ticket detail's own attachment list must not name it either
    # (I2 -- the same scoping, one layer up).
    detail = await api_env.client.get(f"{API}/tickets/{ticket_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["attachments"] == []


async def test_requester_sees_the_approval_outcome_of_a_rejected_request(api_env) -> None:
    """M5 -- spec §0.3 calls the approval outcome "the one genuinely new read
    this module closes"; nothing in the pre-existing suite exercised it
    server-side. Files a request under a `requires_approval` category, has a
    (non-requester) staff member reject it, and asserts the requester sees
    the outcome through the real portal HTTP surface."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import TicketCategory, TicketQueue, User
    from app.modules.service_desk.approvals import decide_approval

    org_id = await api_env.create_org("portal-m5")
    sub = await api_env.create_user(org_id, "requester")
    headers = await api_env.headers(org_id, sub)

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            requester_id = (
                await session.execute(select(User.id).where(User.external_subject == sub))
            ).scalar_one()
            approver = User(
                org_id=org_id, external_subject="approver-m5", email="approver-m5@test.local"
            )
            session.add(approver)
            await session.flush()
            approver_id = approver.id

            queue = TicketQueue(org_id=org_id, key="q-m5", name="Q", created_by=requester_id)
            session.add(queue)
            await session.flush()
            category = TicketCategory(
                org_id=org_id,
                key="c-m5",
                name="Needs approval",
                type="request",
                requires_approval=True,
                default_queue_id=queue.id,
                created_by=requester_id,
            )
            session.add(category)
            await session.flush()
            category_id = category.id

    created = await api_env.client.post(
        f"{API}/tickets",
        json={"category_id": str(category_id), "subject": "need a new laptop"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    ticket = created.json()
    assert ticket["status"] == "pending_approval"
    assert ticket["approval"] == {"state": "pending", "reason": "", "decided_at": None}

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            await decide_approval(
                session,
                org_id,
                approver_id,
                UUID(ticket["id"]),
                approve=False,
                reason="no budget this quarter",
            )

    detail = await api_env.client.get(f"{API}/tickets/{ticket['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    approval = detail.json()["approval"]
    assert approval["state"] == "rejected"
    assert approval["reason"] == "no budget this quarter"
    assert approval["decided_at"] is not None


async def test_oversized_attachment_is_rejected_before_ticket_lookup(api_env) -> None:
    """Task brief requirement #5: the declared Content-Length is checked
    FIRST -- an oversized upload is rejected (413) even against a ticket id
    that does not exist at all for the caller, never a 404. If ownership were
    checked (or the body read) before the size cap, this would come back 404
    instead, and a caller could learn the size limit doesn't apply to ids
    that aren't theirs."""
    from app.modules.service_desk.threads import MAX_ATTACHMENT_BYTES

    org_id = await api_env.create_org("portal-oversized")
    sub = await api_env.create_user(org_id, "requester")
    got = await api_env.client.post(
        f"{API}/tickets/{uuid4()}/attachments?filename=big.bin",
        content=b"x" * (MAX_ATTACHMENT_BYTES + 1),
        headers=await api_env.headers(org_id, sub),
    )
    assert got.status_code == 413, got.text
