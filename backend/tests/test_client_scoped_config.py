"""Per-client configuration: documentation and patch approvals (plan
2026-10-26 Task 14; phase-1 ledger PRODUCT GAP; decision C8).

Phase 1 wrote every article, record, document and patch approval GLOBAL,
because no request could name a client: a technician narrowed to one client
could create none of them (the global-row guard answers 403). Now each create
body takes an optional `client_id`. With one, the row is that client's and the
permission in that client is enough; without one, the row is global and needs
every client, as before.

A patch approval of client X authorizes X's devices only, wherever its reader
runs -- the apply sweep runs under scope "all" -- and, by C8, only a change of
X authorizes it; a global approval's change is the internal client's.

The per-record permission checks (T4-C2) and the phase-1 leftovers (health
resolution, upsert races, a device's site) are `tests/test_client_record_checks.py`.
"""

import asyncio
import datetime as _dt
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    ChangeGate,
    ChangeLink,
    ChangeRequest,
    Credential,
    Device,
    DeviceGroup,
    DevicePatchStatus,
    DocArticle,
    DocDocument,
    DocDocumentLink,
    DocLink,
    DocRecord,
    DocVersion,
    Job,
    Patch,
    PatchApproval,
    PatchPolicy,
)
from tests.client_scope_support import (
    EVERY_CLIENT,
    client_with_site,
    everything_on,
    wait_until_blocked,
)
from tests.conftest import ApiEnv

DOCS = "/api/v1/docs"
APPROVALS = "/api/v1/patch/approvals"


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


async def _admin(env: ApiEnv, org: UUID) -> dict[str, str]:
    return await env.headers(org, await env.create_user(org, "org-admin"))


async def _unassigned(env: ApiEnv, org: UUID) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        found: UUID = (
            await s.execute(
                text("SELECT id FROM clients WHERE org_id = :o AND kind = 'unassigned'"),
                {"o": org},
            )
        ).scalar_one()
    return found


async def _device(
    env: ApiEnv, org: UUID, client: UUID, site: UUID, group: UUID | None = None
) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        d = Device(
            org_id=org, client_id=client, site_id=site, hostname=f"pc-{uuid4().hex[:4]}",
            os_family="windows", device_group_id=group,
        )
        s.add(d)
        await s.commit()
        return d.id


async def _count(env: ApiEnv, org: UUID, *models: Any) -> tuple[int, ...]:
    """Rows of `org` in each table: what a refused write must leave as it found."""
    out: list[int] = []
    async with AsyncSession(env.admin_engine) as s:
        for model in models:
            out.append(
                (
                    await s.execute(
                        select(func.count()).select_from(model).where(model.org_id == org)
                    )
                ).scalar_one()
            )
    return tuple(out)


async def _audits(env: ApiEnv, org: UUID, prefix: str) -> list[tuple[str, str | None]]:
    """`(action, client)` of every event of `org` whose action starts with
    `prefix`, sorted: an exact multiset (Task 13 review)."""
    async with AsyncSession(env.admin_engine) as s:
        rows = (
            await s.execute(
                select(AuditEvent.action, AuditEvent.client_id).where(
                    AuditEvent.org_id == org, AuditEvent.action.startswith(prefix)
                )
            )
        ).all()
    return _multiset((a, None if c is None else str(c)) for a, c in rows)


def _multiset(events: Any) -> list[tuple[str, str | None]]:
    """Sorted, with no client (global) before any client."""
    return sorted(events, key=lambda e: (e[0], e[1] or ""))


# --------------------------------------------------------------------------- #
# Documentation
# --------------------------------------------------------------------------- #

#: kind -> (create route, a valid body without a client).
DOC_CREATES: dict[str, tuple[str, dict[str, Any]]] = {
    "article": (f"{DOCS}/articles", {"title": "VPN", "body": "pasos"}),
    "record": (f"{DOCS}/records", {"record_type": "service", "name": "VPN", "fields": {}}),
    "document": (f"{DOCS}/documents", {"title": "Contrato"}),
}

DOC_TABLES = (DocArticle, DocRecord, DocDocument, DocVersion, AuditEvent)


async def test_a_technician_of_x_documents_x(api_env: ApiEnv) -> None:
    org = await api_env.create_org("cfg-docs")
    x, _ = await client_with_site(api_env, org)
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.post(
        f"{DOCS}/articles", json={"title": "VPN de X", "body": "", "client_id": str(x)},
        headers=x_tech,
    )
    assert r.status_code == 201, r.text
    assert r.json()["client_id"] == str(x)
    global_one = await api_env.client.post(
        f"{DOCS}/articles", json={"title": "Global", "body": ""}, headers=x_tech
    )
    assert global_one.status_code == 403  # a global row is every client's config


@pytest.mark.parametrize("kind", sorted(DOC_CREATES))
async def test_each_documentation_create_files_under_the_client_it_names(
    api_env: ApiEnv, kind: str
) -> None:
    """The client named is the row's, read back by the API and in the table;
    a narrowed technician's create without one is the global-row refusal and
    writes nothing; an every-client administrator's is global."""
    path, body = DOC_CREATES[kind]
    org = await api_env.create_org(f"cfg-doc-{kind}")
    x, _ = await client_with_site(api_env, org)
    x_tech = await everything_on(api_env, org, x)
    admin = await _admin(api_env, org)

    mine = await api_env.client.post(path, json={**body, "client_id": str(x)}, headers=x_tech)
    assert mine.status_code == 201, mine.text
    assert mine.json()["client_id"] == str(x)

    before = await _count(api_env, org, *DOC_TABLES)
    refused = await api_env.client.post(path, json=body, headers=x_tech)
    assert (refused.status_code, refused.json()["detail"]) == (403, EVERY_CLIENT)
    assert await _count(api_env, org, *DOC_TABLES) == before

    shared = await api_env.client.post(path, json=body, headers=admin)
    assert shared.status_code == 201, shared.text
    assert shared.json()["client_id"] is None

    model = {"article": DocArticle, "record": DocRecord, "document": DocDocument}[kind]
    async with AsyncSession(api_env.admin_engine) as s:
        stored = dict(
            (
                await s.execute(
                    select(model.id, model.client_id).where(
                        model.id.in_([UUID(mine.json()["id"]), UUID(shared.json()["id"])])
                    )
                )
            ).all()
        )
    assert stored == {UUID(mine.json()["id"]): x, UUID(shared.json()["id"]): None}
    if kind != "document":
        # Its first version is filed under the same client (the trigger's).
        async with AsyncSession(api_env.admin_engine) as s:
            parent = DocVersion.article_id if kind == "article" else DocVersion.record_id
            version_client = (
                await s.execute(
                    select(DocVersion.client_id).where(parent == UUID(mine.json()["id"]))
                )
            ).scalar_one()
        assert version_client == x


@pytest.mark.parametrize("kind", sorted(DOC_CREATES))
async def test_a_documentation_create_refuses_a_client_it_cannot_file_under(
    api_env: ApiEnv, kind: str
) -> None:
    """`client_for_new_rows`: a client the request cannot see (another
    client of a narrowed technician, another organisation's, none at all) is
    one 404; an archived one a 409; Unassigned a 422. Each writes nothing."""
    path, body = DOC_CREATES[kind]
    org = await api_env.create_org(f"cfg-docx-{kind}")
    other_org = await api_env.create_org(f"cfg-docx-o-{kind}")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    archived, _ = await client_with_site(api_env, org, client_archived=True)
    foreign, _ = await client_with_site(api_env, other_org)
    x_tech = await everything_on(api_env, org, x)
    admin = await _admin(api_env, org)
    unassigned = await _unassigned(api_env, org)

    cases = [
        (x_tech, y, 404, "no such client"),
        (admin, foreign, 404, "no such client"),
        (admin, uuid4(), 404, "no such client"),
        (admin, archived, 409, "that client is archived"),
        (admin, unassigned, 422, "documentation cannot be filed under Unassigned"),
    ]
    before = await _count(api_env, org, *DOC_TABLES)
    for headers, client, code, detail in cases:
        r = await api_env.client.post(
            path, json={**body, "client_id": str(client)}, headers=headers
        )
        assert (r.status_code, r.json()["detail"]) == (code, detail), (client, r.text)
    assert await _count(api_env, org, *DOC_TABLES) == before
    async with AsyncSession(api_env.admin_engine) as s:
        assert (
            await s.execute(
                select(func.count()).select_from(AuditEvent).where(AuditEvent.org_id == other_org)
            )
        ).scalar_one() == 0


@pytest.mark.parametrize("kind", sorted(DOC_CREATES))
async def test_a_documentation_create_takes_its_client_before_the_audit_chain(
    api_env: ApiEnv, kind: str
) -> None:
    """Lock order (`app.services.client_sites`): the create reads its client
    FOR SHARE before anything takes the audit chain's lock. An archive holds
    the client and takes that lock next; with the client held, the create
    waits BEFORE the audit lock, the holder takes it and commits, and the
    create then goes through (a create that audited first would deadlock)."""
    path, body = DOC_CREATES[kind]
    org = await api_env.create_org(f"cfg-doc-lock-{kind}")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    async with AsyncSession(api_env.admin_engine) as holder, AsyncSession(
        api_env.admin_engine
    ) as observer:
        await holder.execute(
            text("SELECT id FROM clients WHERE id = :c FOR NO KEY UPDATE"), {"c": x}
        )
        me = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        create = asyncio.create_task(
            api_env.client.post(path, json={**body, "client_id": str(x)}, headers=admin)
        )
        if not await wait_until_blocked(observer, me):
            create.cancel()
            pytest.fail("the create never waited on the client lock")
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('audit:' || :o))"), {"o": str(org)}
        )
        await holder.commit()
    r = await create
    assert r.status_code == 201, r.text


async def _credential(env: ApiEnv, org: UUID, group: UUID | None = None) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        c = Credential(
            org_id=org, name=f"cred-{uuid4().hex[:6]}", type="password",
            vault_ref=f"vault:{uuid4().hex}", device_group_id=group,
        )
        s.add(c)
        await s.commit()
        return c.id


async def _record(env: ApiEnv, org: UUID, client: UUID | None) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        r = DocRecord(org_id=org, client_id=client, record_type="service", name="r", fields={})
        s.add(r)
        await s.commit()
        return r.id


@pytest.mark.parametrize("parent", ["articles", "documents"])
async def test_a_client_s_documentation_links_only_to_its_client_or_global_targets(
    api_env: ApiEnv, parent: str
) -> None:
    """Spec: a link of an article (or document) of a client names that
    client's targets or global ones; another client's target is a 422 that
    writes nothing. A global article links to anything the caller can see,
    as before. A target the caller cannot see stays a 404."""
    org = await api_env.create_org(f"cfg-links-{parent}")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    x_device = await _device(api_env, org, x, x_site)
    y_device = await _device(api_env, org, y, y_site)
    global_credential = await _credential(api_env, org)
    y_record = await _record(api_env, org, y)
    global_record = await _record(api_env, org, None)
    admin = await _admin(api_env, org)
    x_tech = await everything_on(api_env, org, x)

    create_body = (
        {"title": "X", "body": ""} if parent == "articles" else {"title": "X"}
    )
    mine = await api_env.client.post(
        f"{DOCS}/{parent}", json={**create_body, "client_id": str(x)}, headers=admin
    )
    shared = await api_env.client.post(f"{DOCS}/{parent}", json=create_body, headers=admin)
    assert mine.status_code == shared.status_code == 201
    mine_id, shared_id = mine.json()["id"], shared.json()["id"]
    link_table = DocLink if parent == "articles" else DocDocumentLink

    def link(row: str, target_type: str, target: UUID, headers: dict[str, str]) -> Any:
        return api_env.client.post(
            f"{DOCS}/{parent}/{row}/links",
            json={"target_type": target_type, "target_id": str(target)},
            headers=headers,
        )

    before = await _count(api_env, org, link_table, AuditEvent)
    for target_type, target in (("device", y_device), ("record", y_record)):
        r = await link(mine_id, target_type, target, admin)
        assert r.status_code == 422, r.text
        assert "belongs to another client" in r.json()["detail"]
    assert await _count(api_env, org, link_table, AuditEvent) == before

    # A target the technician cannot see is not found, as it always was.
    hidden = await link(mine_id, "device", y_device, x_tech)
    assert hidden.status_code == 404, hidden.text

    for target_type, target in (
        ("device", x_device), ("credential", global_credential), ("record", global_record),
    ):
        r = await link(mine_id, target_type, target, x_tech)
        assert r.status_code == 201, (target_type, r.text)
    # The global row links to another client's machine as before.
    r = await link(shared_id, "device", y_device, admin)
    assert r.status_code == 201, r.text


async def test_documentation_audits_carry_the_row_s_client(api_env: ApiEnv) -> None:
    """Every documentation event is filed under the client of the row it is
    about: the article's, record's or document's; none for a global one."""
    org = await api_env.create_org("cfg-doc-audit")
    x, x_site = await client_with_site(api_env, org)
    x_device = await _device(api_env, org, x, x_site)
    x_tech = await everything_on(api_env, org, x)
    admin = await _admin(api_env, org)
    c = api_env.client

    article = (
        await c.post(f"{DOCS}/articles", json={"title": "a", "body": "", "client_id": str(x)},
                     headers=x_tech)
    ).json()["id"]
    assert (await c.put(f"{DOCS}/articles/{article}", json={"title": "b", "body": ""},
                        headers=x_tech)).status_code == 200
    assert (await c.put(f"{DOCS}/articles/{article}/portal-visibility",
                        json={"portal_visible": True}, headers=x_tech)).status_code == 200
    link = (
        await c.post(f"{DOCS}/articles/{article}/links",
                     json={"target_type": "device", "target_id": str(x_device)}, headers=x_tech)
    ).json()["id"]
    assert (await c.delete(f"{DOCS}/articles/{article}/links/{link}",
                           headers=x_tech)).status_code == 200
    assert (await c.post(f"{DOCS}/articles/{article}/archive", headers=x_tech)).status_code == 200

    record = (
        await c.post(f"{DOCS}/records", json={"record_type": "service", "name": "r",
                                              "client_id": str(x)}, headers=x_tech)
    ).json()["id"]
    assert (await c.put(f"{DOCS}/records/{record}", json={"name": "s"},
                        headers=x_tech)).status_code == 200
    assert (await c.post(f"{DOCS}/records/{record}/archive", headers=x_tech)).status_code == 200

    document = (
        await c.post(f"{DOCS}/documents", json={"title": "d", "client_id": str(x)},
                     headers=x_tech)
    ).json()["id"]
    assert (await c.put(f"{DOCS}/documents/{document}", json={"title": "e"},
                        headers=x_tech)).status_code == 200
    dlink = (
        await c.post(f"{DOCS}/documents/{document}/links",
                     json={"target_type": "device", "target_id": str(x_device)}, headers=x_tech)
    ).json()["id"]
    assert (await c.delete(f"{DOCS}/documents/{document}/links/{dlink}",
                           headers=x_tech)).status_code == 200
    assert (await c.post(f"{DOCS}/documents/{document}/archive",
                         headers=x_tech)).status_code == 200

    assert (await c.post(f"{DOCS}/articles", json={"title": "g", "body": ""},
                         headers=admin)).status_code == 201

    xs = str(x)
    assert await _audits(api_env, org, "docs.") == _multiset(
        [
            ("docs.article_created", xs), ("docs.article_updated", xs),
            ("docs.article_portal_visibility_changed", xs), ("docs.link_added", xs),
            ("docs.link_removed", xs), ("docs.article_archived", xs),
            ("docs.record_created", xs), ("docs.record_updated", xs),
            ("docs.record_archived", xs),
            ("docs.document_created", xs), ("docs.document_updated", xs),
            ("docs.document_link_added", xs), ("docs.document_link_removed", xs),
            ("docs.document_archived", xs),
            ("docs.article_created", None),
        ]
    )


class _MemoryStore:
    """Just enough of `ObjectStore` for one stored version's download and
    purge: the bytes of the keys `_version` planted."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def get_doc_document(self, ref: str, org_id: UUID) -> bytes:
        return self.objects[ref]

    async def delete_doc_document(self, org_id: UUID, ref: str) -> None:
        self.objects.pop(ref, None)


async def test_a_document_version_s_download_and_purge_carry_its_client(
    api_env: ApiEnv,
) -> None:
    """The two version events read the client before the purge's UPDATE
    expires it (lesson 4): the version's, which is its document's."""
    from app.models import DocDocumentVersion

    org = await api_env.create_org("cfg-doc-ver")
    x, _ = await client_with_site(api_env, org)
    x_tech = await everything_on(api_env, org, x)
    document = (
        await api_env.client.post(f"{DOCS}/documents", json={"title": "d", "client_id": str(x)},
                                  headers=x_tech)
    ).json()["id"]
    store = _MemoryStore()
    ref = f"s3://bucket/{org}/docs/{uuid4()}"
    store.objects[ref] = b"%PDF-1.4"
    async with AsyncSession(api_env.admin_engine) as s:
        s.add(
            DocDocumentVersion(
                org_id=org, document_id=UUID(document), version=1, object_key=ref,
                filename="c.pdf", byte_size=8, content_type="application/pdf", viewer="pdf",
                sha256="0" * 64, created_at=_dt.datetime.now(_dt.UTC),
            )
        )
        await s.execute(
            update(DocDocument).where(DocDocument.id == UUID(document)).values(current_version=1)
        )
        await s.commit()
    api_env.app.state.object_store = store
    try:
        got = await api_env.client.get(
            f"{DOCS}/documents/{document}/versions/1/content", headers=x_tech
        )
        assert got.status_code == 200, got.text
        purged = await api_env.client.post(
            f"{DOCS}/documents/{document}/versions/1/purge", headers=x_tech
        )
        assert purged.status_code == 200, purged.text
    finally:
        api_env.app.state.object_store = None
    events = await _audits(api_env, org, "docs.document_")
    assert ("docs.document_downloaded", str(x)) in events
    assert ("docs.document_version_purged", str(x)) in events


# --------------------------------------------------------------------------- #
# Patch approvals
# --------------------------------------------------------------------------- #


class _FakeJobEngine:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create(self, session: AsyncSession, **kw: Any) -> Job:
        job = Job(
            org_id=kw["org_id"], type=kw["type"], target_type="device",
            target_id=kw["target_device_id"], params=kw["params"], nonce="n",
            issued_at=_dt.datetime.now(_dt.UTC), expires_at=_dt.datetime.now(_dt.UTC),
        )
        session.add(job)
        await session.flush()
        self.created.append(kw)
        return job

    async def signed_envelope(self, job: Job) -> dict[str, Any]:
        return {"v": 1, "kind": "job", "job": {"id": str(job.id)}, "sig": "x"}


class _PatchWorld:
    """An org with clients X and Y, each with one device in ring `prod`
    (one group per client, each with its policy), and one missing patch on
    both."""

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)

    org: UUID
    x: UUID
    y: UUID
    x_device: UUID
    y_device: UUID
    x_group: UUID
    y_group: UUID
    patch: UUID


async def _patch_world(env: ApiEnv, tag: str) -> _PatchWorld:
    org = await env.create_org(tag)
    x, x_site = await client_with_site(env, org)
    y, y_site = await client_with_site(env, org)
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        groups = {}
        for client in (x, y):
            g = DeviceGroup(org_id=org, client_id=client, name=f"prod-{uuid4().hex[:4]}")
            s.add(g)
            await s.flush()
            s.add(PatchPolicy(org_id=org, device_group_id=g.id, ring_name="prod"))
            groups[client] = g.id
        devices = {}
        for client, site in ((x, x_site), (y, y_site)):
            d = Device(org_id=org, client_id=client, site_id=site, hostname=f"h-{client.hex[:4]}",
                       os_family="windows", device_group_id=groups[client])
            s.add(d)
            await s.flush()
            devices[client] = d.id
        patch = Patch(org_id=org, os_family="windows", source="windows_update",
                      identifier="KB1", title="t", severity="important")
        s.add(patch)
        await s.flush()
        for device in devices.values():
            s.add(DevicePatchStatus(org_id=org, device_id=device, patch_id=patch.id,
                                    state="missing"))
        await s.commit()
    return _PatchWorld(
        org=org, x=x, y=y, x_device=devices[x], y_device=devices[y], x_group=groups[x],
        y_group=groups[y], patch=patch.id,
    )


async def _states(env: ApiEnv, w: _PatchWorld) -> dict[UUID, str]:
    async with AsyncSession(env.admin_engine) as s:
        return dict(
            (
                await s.execute(
                    select(DevicePatchStatus.device_id, DevicePatchStatus.state).where(
                        DevicePatchStatus.patch_id == w.patch
                    )
                )
            ).all()
        )


async def _approval_rows(env: ApiEnv, org: UUID) -> list[tuple[UUID | None, bool]]:
    async with AsyncSession(env.admin_engine) as s:
        return sorted(
            (
                await s.execute(
                    select(PatchApproval.client_id, PatchApproval.revoked).where(
                        PatchApproval.org_id == org
                    )
                )
            ).all(),
            key=repr,
        )


async def test_a_client_s_patch_approval_reaches_only_that_client(api_env: ApiEnv) -> None:
    """research-6 §4: approvals were matched by (org, ring, patch) only. Under
    the apply sweep (scope all) an approval of X authorised Y's devices."""
    from app.modules.patch.apply import _ring_approved_patch_ids

    org = await api_env.create_org("cfg-patch")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        patch = Patch(org_id=org, os_family="windows", source="windows_update",
                      identifier="KB1", kb="KB1", title="t", severity="important")
        s.add(patch)
        await s.flush()
        s.add(PatchApproval(org_id=org, client_id=x, patch_id=patch.id, ring_name="prod"))
        y_device = Device(org_id=org, client_id=y, site_id=y_site, hostname="y",
                          os_family="windows")
        x_device = Device(org_id=org, client_id=x, site_id=x_site, hostname="x",
                          os_family="windows")
        s.add_all([y_device, x_device])
        await s.commit()
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        for_x = await _ring_approved_patch_ids(
            s, org_id=org, ring_name="prod", patch_ids={patch.id}, client_id=x
        )
        for_y = await _ring_approved_patch_ids(
            s, org_id=org, ring_name="prod", patch_ids={patch.id}, client_id=y
        )
    assert for_x == {patch.id} and for_y == set()


async def test_the_rings_say_how_many_of_each_ring_s_machines_are_each_client_s(
    api_env: ApiEnv,
) -> None:
    """The console states an approval's reach by its owner (plan 2026-10-26
    Task 14 review): X's approval reaches X's machines in the ring, a global
    one every client's. `GET /patch/rings` says both: `device_count`, and
    `client_device_counts` -- of the machines this request can see, each
    client's -- which add up to it. A technician of X counts X's only."""
    w = await _patch_world(api_env, "cfg-rings")
    x_site = await _site_of(api_env, w.x)
    await _device(api_env, w.org, w.x, x_site, w.x_group)
    admin = await _admin(api_env, w.org)
    r = await api_env.client.get("/api/v1/patch/rings", headers=admin)
    assert r.status_code == 200, r.text
    rings = {item["ring_name"]: item for item in r.json()["items"]}

    def by_client(item: dict[str, Any]) -> dict[str, int]:
        return {row["client_id"]: row["device_count"] for row in item["client_device_counts"]}

    assert rings["prod"]["device_count"] == 3
    assert by_client(rings["prod"]) == {str(w.x): 2, str(w.y): 1}
    for item in rings.values():
        assert sum(by_client(item).values()) == item["device_count"]

    x_tech = await everything_on(api_env, w.org, w.x)
    r = await api_env.client.get("/api/v1/patch/rings", headers=x_tech)
    prod = next(i for i in r.json()["items"] if i["ring_name"] == "prod")
    assert (prod["device_count"], by_client(prod)) == (2, {str(w.x): 2})


async def test_the_device_rollup_names_each_machine_s_client(api_env: ApiEnv) -> None:
    """The device drill-down approves under the ROW's client (the operator's
    rule, 2026-10-28), so each row of `GET /patch/device-rollup` carries it."""
    w = await _patch_world(api_env, "cfg-rollup-client")
    admin = await _admin(api_env, w.org)
    r = await api_env.client.get("/api/v1/patch/device-rollup", headers=admin)
    assert r.status_code == 200, r.text
    assert {row["device_id"]: row["client_id"] for row in r.json()["items"]} == {
        str(w.x_device): str(w.x), str(w.y_device): str(w.y),
    }


async def _site_of(env: ApiEnv, client: UUID) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        found: UUID = (
            await s.execute(text("SELECT id FROM sites WHERE client_id = :c"), {"c": client})
        ).scalar_one()
    return found


@pytest.mark.parametrize("route", ["single", "bulk"])
async def test_a_technician_of_x_approves_for_x_and_flips_x_s_machines_only(
    api_env: ApiEnv, route: str
) -> None:
    """The approval is X's row, answered with its client; the ring it flips
    is X's machines in it (the ring spans both clients); the global path is
    still the every-client refusal for the same technician, writing nothing."""
    w = await _patch_world(api_env, f"cfg-pa-{route}")
    x_tech = await everything_on(api_env, w.org, w.x)
    path, body = (
        (APPROVALS, {"patch_id": str(w.patch), "ring_name": "prod"})
        if route == "single"
        else (f"{APPROVALS}/bulk", {"patch_ids": [str(w.patch)], "ring_name": "prod"})
    )

    before = await _count(api_env, w.org, PatchApproval, AuditEvent, ChangeRequest)
    refused = await api_env.client.post(path, json=body, headers=x_tech)
    assert (refused.status_code, refused.json()["detail"]) == (403, EVERY_CLIENT)
    assert await _count(api_env, w.org, PatchApproval, AuditEvent, ChangeRequest) == before

    r = await api_env.client.post(path, json={**body, "client_id": str(w.x)}, headers=x_tech)
    assert r.status_code == 201, r.text
    out = r.json() if route == "single" else r.json()["items"][0]
    assert out["client_id"] == str(w.x)
    assert await _approval_rows(api_env, w.org) == [(w.x, False)]
    assert await _states(api_env, w) == {w.x_device: "approved", w.y_device: "missing"}


async def test_an_every_client_administrator_s_approval_for_x_flips_x_s_machines_only(
    api_env: ApiEnv,
) -> None:
    """Under the every-client scope RLS hides nothing, so only the ring's
    population per client keeps a client's approval off another client's
    machines: approved for X, X's machine flips and Y's does not; and a gate
    that covers only Y's group (Y uses change management) does not stop X's
    approval, because Y's machines are not what it reaches."""
    w = await _patch_world(api_env, "cfg-pa-admin-x")
    admin = await _admin(api_env, w.org)
    await _enable_change(api_env, admin, w.y)
    async with AsyncSession(api_env.admin_engine) as s:
        s.add(ChangeGate(org_id=w.org, device_group_id=w.y_group, applies_to_patches=True,
                         applies_to_deployments=False))
        await s.commit()
    r = await api_env.client.post(
        APPROVALS, json={"patch_id": str(w.patch), "ring_name": "prod", "client_id": str(w.x)},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    assert await _states(api_env, w) == {w.x_device: "approved", w.y_device: "missing"}


async def test_a_narrowed_global_approval_is_refused_before_its_gate_is_read(
    api_env: ApiEnv,
) -> None:
    """Under a gate that would refuse it, a technician of X asking for a
    GLOBAL approval gets the every-client 403 -- not the gate's 409 and its
    `change.gate_refused` event: the request names nothing it may write, so
    nothing is read or written for it."""
    w = await _patch_world(api_env, "cfg-pa-narrow-gate")
    admin = await _admin(api_env, w.org)
    await _enable_change(api_env, admin, w.x)
    await _gate_everything(api_env, w.org)
    x_tech = await everything_on(api_env, w.org, w.x)
    tables = (PatchApproval, AuditEvent, ChangeRequest, ChangeLink)
    before = await _count(api_env, w.org, *tables)
    r = await api_env.client.post(
        APPROVALS, json={"patch_id": str(w.patch), "ring_name": "prod"}, headers=x_tech
    )
    assert (r.status_code, r.json()["detail"]) == (403, EVERY_CLIENT)
    assert await _count(api_env, w.org, *tables) == before


async def test_an_every_client_administrator_still_approves_globally(api_env: ApiEnv) -> None:
    """No client: the global row, as phase 1 wrote it, flipping every
    client's machines in the ring."""
    w = await _patch_world(api_env, "cfg-pa-global")
    admin = await _admin(api_env, w.org)
    r = await api_env.client.post(
        APPROVALS, json={"patch_id": str(w.patch), "ring_name": "prod"}, headers=admin
    )
    assert r.status_code == 201, r.text
    assert r.json()["client_id"] is None
    assert await _approval_rows(api_env, w.org) == [(None, False)]
    assert await _states(api_env, w) == {w.x_device: "approved", w.y_device: "approved"}


async def test_a_patch_approval_refuses_a_client_it_cannot_file_under(api_env: ApiEnv) -> None:
    w = await _patch_world(api_env, "cfg-pa-refuse")
    other_org = await api_env.create_org("cfg-pa-refuse-o")
    foreign, _ = await client_with_site(api_env, other_org)
    archived, _ = await client_with_site(api_env, w.org, client_archived=True)
    x_tech = await everything_on(api_env, w.org, w.x)
    admin = await _admin(api_env, w.org)
    unassigned = await _unassigned(api_env, w.org)
    body = {"patch_id": str(w.patch), "ring_name": "prod"}
    cases = [
        (x_tech, w.y, 404, "no such client"),
        (admin, foreign, 404, "no such client"),
        (admin, uuid4(), 404, "no such client"),
        (admin, archived, 409, "that client is archived"),
        (admin, unassigned, 422, "a patch approval cannot be filed under Unassigned"),
    ]
    tables = (PatchApproval, AuditEvent, ChangeRequest, ChangeLink)
    before = await _count(api_env, w.org, *tables)
    for headers, client, code, detail in cases:
        r = await api_env.client.post(
            APPROVALS, json={**body, "client_id": str(client)}, headers=headers
        )
        assert (r.status_code, r.json()["detail"]) == (code, detail), (client, r.text)
    assert await _count(api_env, w.org, *tables) == before
    assert await _states(api_env, w) == {w.x_device: "missing", w.y_device: "missing"}


async def _enable_change(env: ApiEnv, admin: dict[str, str], *clients: UUID) -> None:
    for client in clients:
        r = await env.client.patch(
            f"/api/v1/clients/{client}", json={"enabled_modules": ["change"]}, headers=admin
        )
        assert r.status_code == 200, r.text


async def _approved_change(env: ApiEnv, admin: dict[str, str], client: UUID) -> UUID:
    r = await env.client.post(
        "/api/v1/changes",
        json={"type": "normal", "title": "Parches", "description": "d", "risk": "low",
              "affected": [], "rollback_plan": "r", "client_id": str(client)},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    change = UUID(r.json()["id"])
    async with AsyncSession(env.admin_engine) as s:
        await s.execute(
            update(ChangeRequest).where(ChangeRequest.id == change).values(state="approved")
        )
        await s.commit()
    return change


async def test_x_s_approval_is_authorized_by_x_s_change_only(api_env: ApiEnv) -> None:
    """C8, a client's approval: Y's approved change is not found, word for
    word as a change that does not exist, and nothing is written; X's links."""
    w = await _patch_world(api_env, "cfg-c8-x")
    admin = await _admin(api_env, w.org)
    await _enable_change(api_env, admin, w.x, w.y)
    y_change = await _approved_change(api_env, admin, w.y)
    x_change = await _approved_change(api_env, admin, w.x)
    body = {"patch_id": str(w.patch), "ring_name": "prod", "client_id": str(w.x)}

    missing = await api_env.client.post(
        APPROVALS, json={**body, "change_id": str(uuid4())}, headers=admin
    )
    tables = (PatchApproval, AuditEvent, ChangeLink, ChangeRequest)
    before = await _count(api_env, w.org, *tables)
    other = await api_env.client.post(
        APPROVALS, json={**body, "change_id": str(y_change)}, headers=admin
    )
    assert (other.status_code, other.json()) == (missing.status_code, missing.json())
    assert other.status_code == 404
    assert await _count(api_env, w.org, *tables) == before

    ok = await api_env.client.post(
        APPROVALS, json={**body, "change_id": str(x_change)}, headers=admin
    )
    assert ok.status_code == 201, ok.text
    async with AsyncSession(api_env.admin_engine) as s:
        linked = (
            await s.execute(select(ChangeLink.change_id).where(ChangeLink.org_id == w.org))
        ).scalars().all()
    assert linked == [x_change]


async def test_a_global_approval_is_authorized_by_the_internal_client_s_change(
    api_env: ApiEnv,
) -> None:
    """C8, a global approval: Novieri decides it, so X's approved change does
    not authorize it (refused as not found, nothing written); the internal
    client's -- whose id is the organisation's -- does."""
    w = await _patch_world(api_env, "cfg-c8-g")
    admin = await _admin(api_env, w.org)
    await _enable_change(api_env, admin, w.x)
    x_change = await _approved_change(api_env, admin, w.x)
    internal_change = await _approved_change(api_env, admin, w.org)
    body = {"patch_id": str(w.patch), "ring_name": "prod"}

    tables = (PatchApproval, AuditEvent, ChangeLink, ChangeRequest)
    before = await _count(api_env, w.org, *tables)
    refused = await api_env.client.post(
        APPROVALS, json={**body, "change_id": str(x_change)}, headers=admin
    )
    assert (refused.status_code, refused.json()["detail"]) == (404, "no such change")
    assert await _count(api_env, w.org, *tables) == before

    ok = await api_env.client.post(
        APPROVALS, json={**body, "change_id": str(internal_change)}, headers=admin
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["client_id"] is None


@pytest.mark.parametrize("which", ["client", "global"])
async def test_the_approval_takes_its_change_s_client_before_the_audit_chain(
    api_env: ApiEnv, which: str
) -> None:
    """Lock order (`app.services.client_sites`): the approval reads the
    client FOR SHARE -- X's, or for a global approval the internal client's
    through its change's module guard -- before anything takes the audit
    chain's lock. An archive holds the client and takes that lock next; with
    the client held, the approval waits BEFORE the audit lock, the holder
    takes it and commits, and the approval then goes through (no 40P01)."""
    w = await _patch_world(api_env, f"cfg-lock-{which}")
    admin = await _admin(api_env, w.org)
    held = w.x if which == "client" else w.org
    if which == "client":
        await _enable_change(api_env, admin, w.x)
    change = await _approved_change(api_env, admin, held)
    body: dict[str, Any] = {
        "patch_id": str(w.patch), "ring_name": "prod", "change_id": str(change),
    }
    if which == "client":
        body["client_id"] = str(w.x)

    async with AsyncSession(api_env.admin_engine) as holder, AsyncSession(
        api_env.admin_engine
    ) as observer:
        await holder.execute(
            text("SELECT id FROM clients WHERE id = :c FOR NO KEY UPDATE"), {"c": held}
        )
        me = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        create = asyncio.create_task(api_env.client.post(APPROVALS, json=body, headers=admin))
        if not await wait_until_blocked(observer, me):
            create.cancel()
            pytest.fail("the approval never waited on the client lock")
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('audit:' || :o))"), {"o": str(w.org)}
        )
        await holder.commit()
    r = await create
    assert r.status_code == 201, r.text


async def _gate_everything(env: ApiEnv, org: UUID) -> None:
    """A GLOBAL every-device gate on patch approvals."""
    async with AsyncSession(env.admin_engine) as s:
        s.add(ChangeGate(org_id=org, device_group_id=None, applies_to_patches=True,
                         applies_to_deployments=False))
        await s.commit()


async def _link(env: ApiEnv, org: UUID, change: UUID, approval: UUID) -> None:
    async with AsyncSession(env.admin_engine) as s:
        linked_by = (
            await s.execute(select(ChangeRequest.created_by).where(ChangeRequest.id == change))
        ).scalar_one()
        s.add(ChangeLink(org_id=org, change_id=change, action_type="patch_approval",
                         action_id=approval, linked_by=linked_by))
        await s.commit()


async def _approval(env: ApiEnv, w: _PatchWorld, client: UUID | None) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        a = PatchApproval(org_id=w.org, client_id=client, patch_id=w.patch, ring_name="prod")
        s.add(a)
        await s.commit()
        return a.id


async def _sweep(env: ApiEnv, org: UUID) -> list[UUID]:
    """The apply sweep's body for one org, under scope all."""
    from app.modules.patch.apply import dispatch_apply

    engine = _FakeJobEngine()
    async with org_session(org, clients=ClientScope.all(), engine=env.app_engine) as s:
        pending = await dispatch_apply(s, engine, org_id=org, now=_dt.datetime.now(_dt.UTC))
    return sorted(device for _, device, _ in pending)


async def test_the_sweep_sends_a_client_s_approval_to_that_client_s_machines(
    api_env: ApiEnv,
) -> None:
    """Scope all, two clients' machines in the ring, one approval of X: X's
    machine gets the job, Y's does not. A global approval reaches both."""
    w = await _patch_world(api_env, "cfg-sweep")
    await _approval(api_env, w, w.x)
    assert await _sweep(api_env, w.org) == [w.x_device]

    w2 = await _patch_world(api_env, "cfg-sweep-g")
    await _approval(api_env, w2, None)
    assert await _sweep(api_env, w2.org) == sorted([w2.x_device, w2.y_device])


async def test_the_sweep_s_recheck_wants_the_approval_s_own_client_s_change(
    api_env: ApiEnv,
) -> None:
    """C8 at dispatch (`has_authorizing_change`): under a global gate, X's
    approval linked to an approved change of the INTERNAL client -- the
    legacy shape 0237 left, every change filed under it -- authorizes
    nothing; linked to X's own approved change it does. A global approval
    linked to X's change authorizes nothing; to the internal client's, it
    does. Every withheld machine is audited under its own client."""
    w = await _patch_world(api_env, "cfg-recheck")
    admin = await _admin(api_env, w.org)
    await _enable_change(api_env, admin, w.x, w.y)
    await _gate_everything(api_env, w.org)
    internal_change = await _approved_change(api_env, admin, w.org)
    x_change = await _approved_change(api_env, admin, w.x)

    x_approval = await _approval(api_env, w, w.x)
    await _link(api_env, w.org, internal_change, x_approval)
    # Y's own approval of the same patch, authorized by Y's approved change:
    # it clears Y's machines, and authorizes nothing for X's.
    y_approval = await _approval(api_env, w, w.y)
    await _link(api_env, w.org, await _approved_change(api_env, admin, w.y), y_approval)
    assert await _sweep(api_env, w.org) == [w.y_device]
    assert await _audits(api_env, w.org, "change.gate_refused") == [
        ("change.gate_refused", str(w.x))
    ]
    await _link(api_env, w.org, x_change, x_approval)
    assert await _sweep(api_env, w.org) == sorted([w.x_device, w.y_device])

    w2 = await _patch_world(api_env, "cfg-recheck-g")
    admin2 = await _admin(api_env, w2.org)
    await _enable_change(api_env, admin2, w2.x)
    await _gate_everything(api_env, w2.org)
    x2_change = await _approved_change(api_env, admin2, w2.x)
    internal2 = await _approved_change(api_env, admin2, w2.org)
    shared = await _approval(api_env, w2, None)
    await _link(api_env, w2.org, x2_change, shared)
    # X uses change management and meets the global gate; Y does not use it
    # and meets none (D4), so Y's machine is sent the global approval.
    assert await _sweep(api_env, w2.org) == [w2.y_device]
    await _link(api_env, w2.org, internal2, shared)
    assert await _sweep(api_env, w2.org) == sorted([w2.x_device, w2.y_device])


async def test_patch_compliance_counts_a_client_s_approval_for_its_own_machines(
    api_env: ApiEnv,
) -> None:
    """The report's "missing approved" is approved FOR THAT MACHINE: X's
    approval is not one for Y's machine, whichever scope runs the report."""
    w = await _patch_world(api_env, "cfg-compliance")
    await _approval(api_env, w, w.x)
    admin = await _admin(api_env, w.org)
    r = await api_env.client.get("/api/v1/reports/patch-compliance", headers=admin)
    assert r.status_code == 200, r.text
    by_device = {row["device_id"]: row["missing_approved"] for row in r.json()["rows"]}
    assert by_device == {str(w.x_device): 1, str(w.y_device): 0}


async def test_revoking_one_approval_leaves_the_machines_another_still_covers(
    api_env: ApiEnv,
) -> None:
    """X's approval and a global one for the same patch and ring. Revoking
    X's flips none of X's machines back -- the global one still covers them
    -- and revoking the global one then flips X's and Y's; revoking a
    client's approval never touches another client's machines."""
    w = await _patch_world(api_env, "cfg-revoke")
    admin = await _admin(api_env, w.org)
    body = {"patch_id": str(w.patch), "ring_name": "prod"}
    mine = await api_env.client.post(
        APPROVALS, json={**body, "client_id": str(w.x)}, headers=admin
    )
    shared = await api_env.client.post(APPROVALS, json=body, headers=admin)
    assert mine.status_code == shared.status_code == 201
    assert await _states(api_env, w) == {w.x_device: "approved", w.y_device: "approved"}

    r = await api_env.client.delete(f"{APPROVALS}/{mine.json()['id']}", headers=admin)
    assert r.status_code == 204
    assert await _states(api_env, w) == {w.x_device: "approved", w.y_device: "approved"}
    r = await api_env.client.delete(f"{APPROVALS}/{shared.json()['id']}", headers=admin)
    assert r.status_code == 204
    assert await _states(api_env, w) == {w.x_device: "missing", w.y_device: "missing"}

    # Re-approve X, then a Y-only approval revoked: X's machine stays.
    again = await api_env.client.post(
        APPROVALS, json={**body, "client_id": str(w.x)}, headers=admin
    )
    theirs = await api_env.client.post(
        APPROVALS, json={**body, "client_id": str(w.y)}, headers=admin
    )
    assert again.status_code == theirs.status_code == 201
    r = await api_env.client.delete(f"{APPROVALS}/{theirs.json()['id']}", headers=admin)
    assert r.status_code == 204
    assert await _states(api_env, w) == {w.x_device: "approved", w.y_device: "missing"}


async def test_revoking_the_global_approval_leaves_a_client_s_own_approval_covering_it(
    api_env: ApiEnv,
) -> None:
    """The other order (plan 2026-10-26 Task 14 review): with X's approval
    and a global one live for the same patch and ring, revoking the GLOBAL
    one flips Y's machine back to missing and leaves X's approved -- X's own
    approval still covers it (`approve.revoke_approval`'s
    `DevicePatchStatus.client_id.not_in(covering)`)."""
    w = await _patch_world(api_env, "cfg-revoke-g")
    admin = await _admin(api_env, w.org)
    body = {"patch_id": str(w.patch), "ring_name": "prod"}
    mine = await api_env.client.post(
        APPROVALS, json={**body, "client_id": str(w.x)}, headers=admin
    )
    shared = await api_env.client.post(APPROVALS, json=body, headers=admin)
    assert mine.status_code == shared.status_code == 201
    assert await _states(api_env, w) == {w.x_device: "approved", w.y_device: "approved"}

    r = await api_env.client.delete(f"{APPROVALS}/{shared.json()['id']}", headers=admin)
    assert r.status_code == 204
    assert await _states(api_env, w) == {w.x_device: "approved", w.y_device: "missing"}
    assert await _approval_rows(api_env, w.org) == sorted([(w.x, False), (None, True)], key=repr)


async def test_patch_audits_carry_the_approval_s_or_the_machine_s_client(
    api_env: ApiEnv,
) -> None:
    """`patch.approved` / `patch.revoked` are filed under the approval's
    client (none for a global one); the automatic change and its link under
    X (C8: under the internal client for a global approval); the sweep's
    `patch.apply_dispatched` under each machine's."""
    w = await _patch_world(api_env, "cfg-pa-audit")
    admin = await _admin(api_env, w.org)
    await _enable_change(api_env, admin, w.x)
    body = {"patch_id": str(w.patch), "ring_name": "prod"}
    mine = await api_env.client.post(
        APPROVALS, json={**body, "client_id": str(w.x)}, headers=admin
    )
    shared = await api_env.client.post(APPROVALS, json=body, headers=admin)
    assert mine.status_code == shared.status_code == 201
    assert sorted(await _sweep(api_env, w.org)) == sorted([w.x_device, w.y_device])
    assert (await api_env.client.delete(f"{APPROVALS}/{mine.json()['id']}",
                                        headers=admin)).status_code == 204

    xs, internal = str(w.x), str(w.org)
    assert await _audits(api_env, w.org, "patch.") == _multiset(
        [
            ("patch.approved", xs), ("patch.approved", None), ("patch.revoked", xs),
            ("patch.apply_dispatched", xs), ("patch.apply_dispatched", str(w.y)),
        ]
    )
    assert await _audits(api_env, w.org, "change.auto_recorded") == _multiset(
        [("change.auto_recorded", xs), ("change.auto_recorded", internal)]
    )
    assert await _audits(api_env, w.org, "change.linked") == _multiset(
        [("change.linked", xs), ("change.linked", internal)]
    )


async def test_a_gate_refused_approval_is_audited_under_its_client(api_env: ApiEnv) -> None:
    """The refusal of X's approval under a gate is X's event (and X's
    machines are what the gate was evaluated over); a refused global
    approval's event has no client -- it reaches every client."""
    w = await _patch_world(api_env, "cfg-pa-refusal")
    admin = await _admin(api_env, w.org)
    await _enable_change(api_env, admin, w.x)
    await _gate_everything(api_env, w.org)
    body = {"patch_id": str(w.patch), "ring_name": "prod"}
    r = await api_env.client.post(APPROVALS, json={**body, "client_id": str(w.y)}, headers=admin)
    # Y does not use change management: it meets no gate (D4).
    assert r.status_code == 201, r.text
    r = await api_env.client.post(APPROVALS, json={**body, "client_id": str(w.x)}, headers=admin)
    assert r.status_code == 409, r.text
    r = await api_env.client.post(APPROVALS, json=body, headers=admin)
    assert r.status_code == 409, r.text
    assert await _audits(api_env, w.org, "change.gate_refused") == _multiset(
        [("change.gate_refused", str(w.x)), ("change.gate_refused", None)]
    )


# --------------------------------------------------------------------------- #
# Over MCP: the AI app acts as the person who connected it (CLAUDE.md 1c)
# --------------------------------------------------------------------------- #


@pytest.fixture()
async def mcp_env(api_env: ApiEnv) -> ApiEnv:
    """`api_env` with `ITS_PUBLIC_CONSOLE_URL` set before the MCP server
    starts: the gate answers only there."""
    from tests.mcp_support import MCP_BASE

    api_env.settings.public_console_url = MCP_BASE
    return api_env


async def test_an_ai_app_files_documentation_under_the_person_s_client(mcp_env: Any) -> None:
    """The create tool runs the route through the full ASGI stack, as the
    technician of X who connected it: an article for X is X's, and a global
    one is the every-client refusal, writing nothing."""
    from app.models import Permission, Role, RolePermission, User, UserRole
    from tests.mcp_support import mcp_org, mcp_session, mcp_token, running_mcp, tool_error_text

    org, slug = await mcp_org(mcp_env)
    x, _ = await client_with_site(mcp_env, org)
    sub = await mcp_env.create_user(org)
    async with AsyncSession(mcp_env.admin_engine) as s:
        user_id = (
            await s.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
        role = Role(org_id=org, name=f"everything-{uuid4().hex[:8]}")
        s.add(role)
        await s.flush()
        for permission_id in (await s.execute(select(Permission.id))).scalars():
            s.add(RolePermission(role_id=role.id, permission_id=permission_id))
        s.add(UserRole(org_id=org, user_id=user_id, role_id=role.id, client_id=x))
        await s.commit()
    token = mcp_token(mcp_env, slug, sub)
    async with running_mcp(mcp_env), mcp_session(mcp_env, token) as session:
        mine = await session.call_tool(
            "docs_articles_create", {"body": {"title": "VPN", "body": "", "client_id": str(x)}}
        )
        assert mine.is_error is False, mine
        assert mine.structured_content is not None
        assert mine.structured_content["client_id"] == str(x)
        before = await _count(mcp_env, org, DocArticle)
        refused = await session.call_tool(
            "docs_articles_create", {"body": {"title": "Global", "body": ""}}
        )
        assert EVERY_CLIENT in tool_error_text(refused)
    assert await _count(mcp_env, org, DocArticle) == before
