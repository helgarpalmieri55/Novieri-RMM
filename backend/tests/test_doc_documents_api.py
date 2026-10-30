"""Stored documents: routes, limits, audit, purge and the soft object store
(`docs/modules/documentation/spec-attachments.md` §10, falsifiers F1a/F1b, F2b,
F3a, F4a/F4b, F5, F6, F7, F8, plus the RBAC/link/search coverage of §13).

Tenancy on the SERVICE's own `org_id` predicate is a separate file
(`test_doc_documents_tenancy.py`) and deliberately so: those assertions have to
run on the BYPASSRLS session to falsify at all, and mixing them in here would
put two very different kinds of test behind one name.
"""

import hashlib
from typing import Any
from uuid import UUID, uuid4

import pytest
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.models import AuditEvent, Device, DocDocumentVersion

API = "/api/v1/docs"

PDF = b"%PDF-1.7\nsigned contract\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
ZIP = b"PK\x03\x04" + b"\x00" * 30


@pytest.fixture()
def s3(api_env):
    """A real (moto) object store bound to the app, the shape every other
    attachment test in this suite uses."""
    mock = mock_aws()
    mock.start()
    api_env.app.state.object_store = ObjectStore(api_env.settings)
    yield api_env.app.state.object_store
    mock.stop()
    api_env.app.state.object_store = None


async def _org_with(api_env, *roles: str, slug: str = "docd") -> tuple[UUID, dict[str, str]]:
    org_id = await api_env.create_org(slug)
    sub = await api_env.create_user(org_id, *roles)
    return org_id, await api_env.headers(org_id, sub)


async def _document(api_env, headers, **overrides: Any) -> dict[str, Any]:
    payload = {"title": "Firewall contract", "description": "2026 renewal", "category": "Vendors"}
    payload.update(overrides)
    resp = await api_env.client.post(f"{API}/documents", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _upload(
    api_env, headers, document_id: str, filename: str, data: bytes
) -> Any:
    return await api_env.client.post(
        f"{API}/documents/{document_id}/versions?filename={filename}",
        content=data,
        headers=headers,
    )


async def _audit_count(api_env, org_id: UUID, action: str) -> int:
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        rows = (
            (await session.execute(select(AuditEvent).where(AuditEvent.action == action)))
            .scalars()
            .all()
        )
        return len(rows)


async def _version_rows(api_env, org_id: UUID) -> list[DocDocumentVersion]:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        return list(
            (
                await session.execute(
                    select(DocDocumentVersion).where(DocDocumentVersion.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )


# --------------------------------------------------------------------------- #
# The happy path, end to end.
# --------------------------------------------------------------------------- #


async def test_create_upload_list_and_download(api_env, s3) -> None:
    org_id, headers = await _org_with(api_env, "technician")
    document = await _document(api_env, headers)
    assert document["current_version"] == 0
    assert document["current"] is None

    uploaded = await _upload(api_env, headers, document["id"], "contract.pdf", PDF)
    assert uploaded.status_code == 201, uploaded.text
    version = uploaded.json()
    assert version["version"] == 1
    assert version["content_type"] == "application/pdf"
    assert version["viewer"] == "pdf"
    assert version["byte_size"] == len(PDF)
    assert version["sha256"] == hashlib.sha256(PDF).hexdigest()

    detail = await api_env.client.get(f"{API}/documents/{document['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["current_version"] == 1
    assert detail.json()["current"]["filename"] == "contract.pdf"

    listed = await api_env.client.get(f"{API}/documents", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    content = await api_env.client.get(
        f"{API}/documents/{document['id']}/versions/1/content", headers=headers
    )
    assert content.status_code == 200
    assert content.content == PDF


async def test_a_document_with_no_file_is_a_legal_visible_state(api_env) -> None:
    """§4.1: metadata and bytes cannot share a request (no multipart parser),
    so a zero-version document is normal, not an error -- and `current_version`
    starts at 0 rather than 1 to say so honestly."""
    _, headers = await _org_with(api_env, "technician")
    document = await _document(api_env, headers)
    listed = await api_env.client.get(f"{API}/documents", headers=headers)
    assert listed.json()["items"][0]["current_version"] == 0
    versions = await api_env.client.get(
        f"{API}/documents/{document['id']}/versions", headers=headers
    )
    assert versions.status_code == 200
    assert versions.json() == []


async def test_versions_are_immutable_and_append_only(api_env, s3) -> None:
    """§0.6/§6.1: a new version writes a NEW object at a new key and never
    touches the old one, so the previous file stays downloadable. An overwrite
    is the one operation in this design that can destroy the only copy of a
    file on a mis-click."""
    _, headers = await _org_with(api_env, "technician")
    document = await _document(api_env, headers)
    assert (await _upload(api_env, headers, document["id"], "v1.txt", b"one")).status_code == 201
    assert (await _upload(api_env, headers, document["id"], "v2.txt", b"two")).status_code == 201

    first = await api_env.client.get(
        f"{API}/documents/{document['id']}/versions/1/content", headers=headers
    )
    assert first.content == b"one"
    second = await api_env.client.get(
        f"{API}/documents/{document['id']}/versions/2/content", headers=headers
    )
    assert second.content == b"two"

    versions = await api_env.client.get(
        f"{API}/documents/{document['id']}/versions", headers=headers
    )
    assert [v["version"] for v in versions.json()] == [2, 1]


# --------------------------------------------------------------------------- #
# F1a / F1b -- cross-org 404, and no key in any response.
# --------------------------------------------------------------------------- #


async def test_another_orgs_document_is_indistinguishable_from_a_missing_one(
    api_env, s3
) -> None:
    """F1a. Detail, versions, content and links all 404 under org B's token.
    A different status for "exists but not yours" would itself be a
    cross-tenant disclosure -- it confirms the id."""
    _, a_headers = await _org_with(api_env, "org-admin", slug="doc-a")
    _, b_headers = await _org_with(api_env, "org-admin", slug="doc-b")
    document = await _document(api_env, a_headers)
    await _upload(api_env, a_headers, document["id"], "secret.pdf", PDF)
    did = document["id"]

    for path in (
        f"{API}/documents/{did}",
        f"{API}/documents/{did}/versions",
        f"{API}/documents/{did}/versions/1/content",
    ):
        resp = await api_env.client.get(path, headers=b_headers)
        assert resp.status_code == 404, (path, resp.status_code, resp.text)

    for path, kwargs in (
        (f"{API}/documents/{did}/archive", {}),
        (f"{API}/documents/{did}/versions/1/purge", {}),
        (
            f"{API}/documents/{did}/links",
            {"json": {"target_type": "device", "target_id": str(uuid4())}},
        ),
    ):
        resp = await api_env.client.post(path, headers=b_headers, **kwargs)
        assert resp.status_code == 404, (path, resp.status_code, resp.text)

    put = await api_env.client.put(
        f"{API}/documents/{did}",
        json={"title": "hijacked", "description": "", "category": "x"},
        headers=b_headers,
    )
    assert put.status_code == 404

    upload = await _upload(api_env, b_headers, did, "evil.txt", b"x")
    assert upload.status_code == 404

    # And nothing was changed by any of that.
    detail = await api_env.client.get(f"{API}/documents/{did}", headers=a_headers)
    assert detail.json()["title"] == "Firewall contract"


async def test_no_response_anywhere_contains_an_object_key(api_env, s3) -> None:
    """F1b and F2b in one sweep, over EVERY documents route rather than a spot
    check. Mechanism 2 of the four that replace RLS on the object store (§3.3):
    if a key never leaves the control plane, guessing one is the only way to
    have one -- and no route takes a key anyway."""
    org_id, headers = await _org_with(api_env, "org-admin")
    document = await _document(api_env, headers)
    await _upload(api_env, headers, document["id"], "contract.pdf", PDF)
    device_id = await _a_device(api_env, org_id)
    link = await api_env.client.post(
        f"{API}/documents/{document['id']}/links",
        json={"target_type": "device", "target_id": str(device_id)},
        headers=headers,
    )
    assert link.status_code == 201, link.text

    bodies = []
    for path in (
        f"{API}/documents",
        f"{API}/documents/{document['id']}",
        f"{API}/documents/{document['id']}/versions",
        f"{API}/search?q=contract",
    ):
        resp = await api_env.client.get(path, headers=headers)
        assert resp.status_code == 200, (path, resp.text)
        bodies.append((path, resp.text))
    bodies.append(("link", link.text))

    for path, text in bodies:
        assert "s3://" not in text, path
        assert "object_key" not in text, path
        assert "/docs/documents/" not in text, path
        # F2b: and no presigned grant, which would carry these.
        assert "X-Amz-Signature" not in text, path
        assert "X-Amz-Credential" not in text, path


async def _a_device(api_env, org_id: UUID) -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device = Device(
            org_id=org_id, hostname="doc-dev", os_family="linux", client_id=org_id, site_id=org_id
        )
        session.add(device)
        await session.commit()
        return device.id


# --------------------------------------------------------------------------- #
# F3a -- the transport is always download-shaped, whatever the bytes are.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("filename", "data"),
    [
        ("script.txt", b"<script>alert(1)</script>"),
        ("evil.html", b"<html><body><script>alert(1)</script></body></html>"),
        ("net.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
        ("raw.md", b"# title\n\n<script>alert(1)</script>\n"),
        ("report.pdf", ZIP),
        ("photo.png", PNG),
        ("contract.pdf", PDF),
    ],
)
async def test_every_content_response_is_an_attachment_with_no_store(
    api_env, s3, filename: str, data: bytes
) -> None:
    """F3a, wall 1 of §0.4. `application/octet-stream` REGARDLESS of the
    sniffed type -- including for PDFs and images -- so direct navigation to
    this URL downloads a file and never renders a page. `no-store` because a
    documentation library holds signed contracts and those should not sit in a
    shared workstation's disk cache.

    STATED AND NOT COVERED (§13 F3): direct browser navigation cannot be
    exercised in the in-process ASGI harness -- the same structural gap that
    makes `sanitize_attachment_filename` a unit test. The two headers the
    browser actually acts on are pinned instead.
    """
    _, headers = await _org_with(api_env, "technician")
    document = await _document(api_env, headers)
    uploaded = await _upload(api_env, headers, document["id"], filename, data)
    assert uploaded.status_code == 201, uploaded.text

    content = await api_env.client.get(
        f"{API}/documents/{document['id']}/versions/1/content", headers=headers
    )
    assert content.status_code == 200
    assert content.headers["content-type"] == "application/octet-stream"
    assert content.headers["content-disposition"] == f'attachment; filename="{filename}"'
    assert content.headers["cache-control"] == "no-store"
    assert content.headers["x-content-type-options"] == "nosniff"
    assert content.content == data


@pytest.mark.parametrize(
    ("filename", "content_type", "viewer"),
    [
        ("export.html", "text/plain", "text"),
        ("net.svg", "text/plain", "text"),
        ("config.xml", "text/plain", "text"),
    ],
)
async def test_html_svg_and_xml_store_as_plain_text(
    api_env, s3, filename: str, content_type: str, viewer: str
) -> None:
    """F3b at the API boundary. The sniffer's unit tests are the real coverage;
    this pins that the stored METADATA -- the thing the console keys its render
    map off -- carries the same answer end to end."""
    _, headers = await _org_with(api_env, "technician")
    document = await _document(api_env, headers)
    uploaded = await _upload(
        api_env, headers, document["id"], filename, b'<svg><script>alert(1)</script></svg>'
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["content_type"] == content_type
    assert uploaded.json()["viewer"] == viewer


# --------------------------------------------------------------------------- #
# F4 -- the client never declares a type.
# --------------------------------------------------------------------------- #


async def test_a_zip_named_pdf_stores_as_a_zip(api_env, s3) -> None:
    """F4a."""
    _, headers = await _org_with(api_env, "technician")
    document = await _document(api_env, headers)
    uploaded = await _upload(api_env, headers, document["id"], "report.pdf", ZIP)
    assert uploaded.json()["content_type"] == "application/zip"
    assert uploaded.json()["viewer"] == "none"


async def test_a_pdf_named_txt_stores_as_a_pdf(api_env, s3) -> None:
    """F4b."""
    _, headers = await _org_with(api_env, "technician")
    document = await _document(api_env, headers)
    uploaded = await _upload(api_env, headers, document["id"], "notes.txt", PDF)
    assert uploaded.json()["content_type"] == "application/pdf"
    assert uploaded.json()["viewer"] == "pdf"


async def test_an_unrecognised_binary_is_refused_with_the_accepted_list(api_env, s3) -> None:
    _, headers = await _org_with(api_env, "technician")
    document = await _document(api_env, headers)
    uploaded = await _upload(
        api_env, headers, document["id"], "installer.msi", b"\x00\x01\x02\x03MZ\xff"
    )
    assert uploaded.status_code == 415, uploaded.text
    assert "PDF" in uploaded.json()["detail"]


async def test_a_rejected_filename_is_a_400_before_the_body_is_read(api_env, s3) -> None:
    """§4.2/§4.4 step 2. `sanitize_attachment_filename` is imported, not
    re-derived: its ASCII-only `fullmatch` allow-list and its
    reject-don't-truncate posture are both load-bearing and both have their
    reasons recorded at the shared implementation."""
    org_id, headers = await _org_with(api_env, "technician")
    document = await _document(api_env, headers)
    resp = await api_env.client.post(
        f"{API}/documents/{document['id']}/versions?filename=../../etc/passwd",
        content=PDF,
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
    assert await _version_rows(api_env, org_id) == []


# --------------------------------------------------------------------------- #
# F5 -- size and quota.
# --------------------------------------------------------------------------- #


async def test_a_declared_content_length_over_the_cap_is_refused(api_env, s3) -> None:
    """F5a. And the absences are asserted, not just the status: no version row
    and no stored object. A 413 that still wrote something would be a 413 that
    did not mean anything."""
    from app.modules.documentation.documents import MAX_DOCUMENT_BYTES

    org_id, headers = await _org_with(api_env, "technician")
    document = await _document(api_env, headers)
    resp = await api_env.client.post(
        f"{API}/documents/{document['id']}/versions?filename=huge.txt",
        content=b"x" * (MAX_DOCUMENT_BYTES + 1),
        headers=headers,
    )
    assert resp.status_code == 413, resp.text
    assert await _version_rows(api_env, org_id) == []
    assert (
        await api_env.client.get(f"{API}/documents/{document['id']}", headers=headers)
    ).json()["current_version"] == 0


async def test_a_chunked_body_over_the_cap_is_refused(api_env, s3) -> None:
    """F5b -- the case F5a cannot catch. `Content-Length` is client-supplied
    and a chunked request carries none at all, so the header check alone is
    bypassed by any client that simply omits it. `read_bounded_body` counts
    real bytes off the wire, which is the guard that actually holds."""
    from app.modules.documentation.documents import MAX_DOCUMENT_BYTES

    org_id, headers = await _org_with(api_env, "technician")
    document = await _document(api_env, headers)

    async def chunks():
        sent = 0
        while sent <= MAX_DOCUMENT_BYTES:
            block = b"x" * (1024 * 1024)
            sent += len(block)
            yield block

    resp = await api_env.client.post(
        f"{API}/documents/{document['id']}/versions?filename=huge.txt",
        content=chunks(),
        headers=headers,
    )
    assert resp.status_code == 413, resp.text
    assert "content-length" not in {k.lower() for k in resp.request.headers}
    assert await _version_rows(api_env, org_id) == []


async def test_an_org_at_quota_gets_507_and_stores_nothing(api_env, s3, monkeypatch) -> None:
    """F5c. The quota exists because the bucket is SHARED WITH BACKUP
    REPOSITORIES: an unbounded technician-tier upload path can stop backups
    from writing (§0.10). Driven by lowering the constant rather than by
    uploading 5 GiB, which is the only way to test a 5 GiB limit in under a
    day."""
    monkeypatch.setattr(
        "app.modules.documentation.documents.CLIENT_DOCUMENT_QUOTA_BYTES", 32
    )
    org_id, headers = await _org_with(api_env, "technician")
    document = await _document(api_env, headers)

    first = await _upload(api_env, headers, document["id"], "small.txt", b"x" * 16)
    assert first.status_code == 201, first.text

    # 16 already stored + 24 incoming = 40 > 32: refused by pass two, the pass
    # that actually enforces the limit.
    second = await _upload(api_env, headers, document["id"], "more.txt", b"y" * 24)
    assert second.status_code == 507, second.text
    assert "quota" in second.json()["detail"].lower()

    rows = await _version_rows(api_env, org_id)
    assert [r.version for r in rows] == [1]
    assert (
        await api_env.client.get(f"{API}/documents/{document['id']}", headers=headers)
    ).json()["current_version"] == 1


async def test_the_first_quota_pass_refuses_before_the_body_is_read(
    api_env, s3, monkeypatch
) -> None:
    """§4.4 step 5, WRITTEN SO THAT DELETING THE FIRST PASS TURNS IT RED.

    The first pass cannot see the incoming size -- nothing has been read yet --
    so it is not the pass that enforces the limit. Its job is to stop an org
    that is ALREADY full from buffering 25 MiB in API memory to be told so, and
    a test that merely asserts "over quota -> 507" cannot tell the two passes
    apart, because the second one answers the same way.

    What distinguishes them is an over-cap body sent CHUNKED to a full org.
    With the first pass: step 5 answers 507 before a byte is read. Without it:
    step 6's `read_bounded_body` answers 413 having buffered the whole thing.
    Two different status codes for the same request, so the assertion is on
    which guard got there first, not on whether some guard did. (Chunked
    deliberately: a declared `Content-Length` would be refused by step 1 first,
    which is a third answer and not the one under test.)
    """
    from app.modules.documentation.documents import MAX_DOCUMENT_BYTES

    monkeypatch.setattr("app.modules.documentation.documents.CLIENT_DOCUMENT_QUOTA_BYTES", 32)
    _, headers = await _org_with(api_env, "technician")
    document = await _document(api_env, headers)
    # Fill the org to EXACTLY the cap. Pass two allows this (32 + 0 > 32 is
    # false), which is itself the behaviour an operator would predict from the
    # number; pass one refuses everything after it.
    assert (
        await _upload(api_env, headers, document["id"], "fill.txt", b"x" * 32)
    ).status_code == 201

    async def chunks():
        sent = 0
        while sent <= MAX_DOCUMENT_BYTES:
            block = b"x" * (1024 * 1024)
            sent += len(block)
            yield block

    resp = await api_env.client.post(
        f"{API}/documents/{document['id']}/versions?filename=huge.txt",
        content=chunks(),
        headers=headers,
    )
    assert "content-length" not in {k.lower() for k in resp.request.headers}
    assert resp.status_code == 507, resp.text


async def test_no_stored_byte_size_exceeds_the_per_file_cap(api_env, s3) -> None:
    """F5d."""
    from app.modules.documentation.documents import MAX_DOCUMENT_BYTES

    org_id, headers = await _org_with(api_env, "technician")
    document = await _document(api_env, headers)
    for name, body in (("a.txt", b"a"), ("b.pdf", PDF), ("c.png", PNG)):
        assert (await _upload(api_env, headers, document["id"], name, body)).status_code == 201
    for row in await _version_rows(api_env, org_id):
        assert 0 <= row.byte_size <= MAX_DOCUMENT_BYTES


# --------------------------------------------------------------------------- #
# F6 -- audit completeness.
# --------------------------------------------------------------------------- #


async def test_every_mutation_and_every_download_writes_its_event(api_env, s3) -> None:
    """F6a, over the §12.3 table."""
    org_id, headers = await _org_with(api_env, "org-admin")
    document = await _document(api_env, headers)
    device_id = await _a_device(api_env, org_id)
    await _upload(api_env, headers, document["id"], "contract.pdf", PDF)
    link = await api_env.client.post(
        f"{API}/documents/{document['id']}/links",
        json={"target_type": "device", "target_id": str(device_id)},
        headers=headers,
    )
    assert link.status_code == 201, link.text
    await api_env.client.delete(
        f"{API}/documents/{document['id']}/links/{link.json()['id']}", headers=headers
    )
    await api_env.client.put(
        f"{API}/documents/{document['id']}",
        json={"title": "Renamed", "description": "d", "category": "Vendors"},
        headers=headers,
    )
    await api_env.client.get(
        f"{API}/documents/{document['id']}/versions/1/content", headers=headers
    )
    await api_env.client.post(
        f"{API}/documents/{document['id']}/versions/1/purge", headers=headers
    )
    await api_env.client.post(f"{API}/documents/{document['id']}/archive", headers=headers)

    for action in (
        "docs.document_created",
        "docs.document_updated",
        "docs.document_version_uploaded",
        "docs.document_downloaded",
        "docs.document_version_purged",
        "docs.document_archived",
        "docs.document_link_added",
        "docs.document_link_removed",
    ):
        assert await _audit_count(api_env, org_id, action) == 1, action


async def test_two_views_are_two_download_events(api_env, s3) -> None:
    """F6b, and D10 stated as behaviour. Opening the viewer writes a download
    event because the server cannot distinguish "viewed" from "saved" -- the
    bytes left the control plane either way. No deduplication: a "we already
    logged that one" rule is how a log stops answering "how many times". The
    consequence the operator agreed to knowingly is that audit volume grows
    with READING."""
    org_id, headers = await _org_with(api_env, "read-only")
    manage_sub = await api_env.create_user(org_id, "technician")
    manage_headers = await api_env.headers(org_id, manage_sub)
    document = await _document(api_env, manage_headers)
    await _upload(api_env, manage_headers, document["id"], "contract.pdf", PDF)

    path = f"{API}/documents/{document['id']}/versions/1/content"
    assert (await api_env.client.get(path, headers=headers)).status_code == 200
    assert (await api_env.client.get(path, headers=headers)).status_code == 200
    assert await _audit_count(api_env, org_id, "docs.document_downloaded") == 2


async def test_a_no_op_update_writes_nothing(api_env) -> None:
    """F6c, matching `update_article`. An audit log that records edits which
    did not happen cannot answer "who changed this"."""
    org_id, headers = await _org_with(api_env, "technician")
    document = await _document(api_env, headers)
    same = {
        "title": document["title"],
        "description": document["description"],
        "category": document["category"],
    }
    resp = await api_env.client.put(
        f"{API}/documents/{document['id']}", json=same, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert await _audit_count(api_env, org_id, "docs.document_updated") == 0


# --------------------------------------------------------------------------- #
# F7 -- purge destroys, and the tombstone stays.
# --------------------------------------------------------------------------- #


async def test_purge_destroys_the_bytes_and_leaves_a_tombstone(api_env, s3) -> None:
    """F7a, every clause of it."""
    org_id, headers = await _org_with(api_env, "org-admin")
    document = await _document(api_env, headers)
    await _upload(api_env, headers, document["id"], "contract.pdf", PDF)
    stored = (await _version_rows(api_env, org_id))[0]
    ref = stored.object_key
    assert ref.startswith("s3://")

    purged = await api_env.client.post(
        f"{API}/documents/{document['id']}/versions/1/purge", headers=headers
    )
    assert purged.status_code == 200, purged.text

    content = await api_env.client.get(
        f"{API}/documents/{document['id']}/versions/1/content", headers=headers
    )
    assert content.status_code == 410, content.text

    row = (await _version_rows(api_env, org_id))[0]
    assert row.object_key == ""
    assert row.purged_at is not None
    assert row.purged_by is not None
    # The tombstone keeps what the audit record needs to mean anything.
    assert row.filename == "contract.pdf"
    assert row.byte_size == len(PDF)
    assert row.sha256 == hashlib.sha256(PDF).hexdigest()

    with pytest.raises(Exception):  # noqa: B017 -- botocore NoSuchKey
        await s3.get_doc_document(ref, org_id)

    assert await _audit_count(api_env, org_id, "docs.document_version_purged") == 1


async def test_purging_the_current_version_does_not_renumber_history(api_env, s3) -> None:
    """§6.3. The document's current version becomes a tombstone and the console
    says so; decrementing would make the history lie about how many uploads
    there were."""
    _, headers = await _org_with(api_env, "org-admin")
    document = await _document(api_env, headers)
    await _upload(api_env, headers, document["id"], "a.txt", b"one")
    await _upload(api_env, headers, document["id"], "b.txt", b"two")
    await api_env.client.post(
        f"{API}/documents/{document['id']}/versions/2/purge", headers=headers
    )
    detail = await api_env.client.get(f"{API}/documents/{document['id']}", headers=headers)
    assert detail.json()["current_version"] == 2
    assert detail.json()["current"]["purged_at"] is not None


async def test_a_purged_version_is_410_and_not_404(api_env, s3) -> None:
    """§5.1. The version existed and was deliberately destroyed, and the audit
    log says by whom. Conflating that with "never existed" loses the only fact
    worth keeping -- and a 404 here would also be indistinguishable from the
    cross-org answer, which means two very different things."""
    _, headers = await _org_with(api_env, "org-admin")
    document = await _document(api_env, headers)
    await _upload(api_env, headers, document["id"], "a.txt", b"one")
    await api_env.client.post(
        f"{API}/documents/{document['id']}/versions/1/purge", headers=headers
    )
    gone = await api_env.client.get(
        f"{API}/documents/{document['id']}/versions/1/content", headers=headers
    )
    assert gone.status_code == 410
    missing = await api_env.client.get(
        f"{API}/documents/{document['id']}/versions/9/content", headers=headers
    )
    assert missing.status_code == 404


async def test_a_round_trip_preserves_the_stored_digest(api_env, s3) -> None:
    """F7b at the route level. A mismatch means storage corruption or the wrong
    object, and the digest is the only thing that can tell you which."""
    org_id, headers = await _org_with(api_env, "technician")
    document = await _document(api_env, headers)
    body = PDF * 500
    await _upload(api_env, headers, document["id"], "big.pdf", body)
    row = (await _version_rows(api_env, org_id))[0]
    fetched = await api_env.client.get(
        f"{API}/documents/{document['id']}/versions/1/content", headers=headers
    )
    assert hashlib.sha256(fetched.content).hexdigest() == row.sha256


# --------------------------------------------------------------------------- #
# F8 -- the object store is a SOFT dependency.
# --------------------------------------------------------------------------- #


class _BrokenStore:
    """A store whose byte paths raise, as MinIO being down looks from here."""

    async def get_doc_document(self, ref: str, org_id: UUID) -> bytes:
        raise RuntimeError("connection refused")

    async def put_doc_document(self, *a: Any, **k: Any) -> str:
        raise RuntimeError("connection refused")

    async def delete_doc_document(self, *a: Any, **k: Any) -> None:
        raise RuntimeError("connection refused")


async def test_the_library_still_lists_when_object_storage_is_down(api_env, s3) -> None:
    """F8a. `probe_objectstore` is explicitly a SOFT probe -- object storage
    being down never gates readiness -- so this module inherits that: the page
    must still list from PostgreSQL. A metadata list rendered as an empty list
    would tell an operator their library is gone."""
    _, headers = await _org_with(api_env, "technician")
    document = await _document(api_env, headers)
    await _upload(api_env, headers, document["id"], "contract.pdf", PDF)

    api_env.app.state.object_store = _BrokenStore()
    listed = await api_env.client.get(f"{API}/documents", headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1
    detail = await api_env.client.get(f"{API}/documents/{document['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["current"]["filename"] == "contract.pdf"


async def test_the_byte_path_fails_loudly_and_names_the_dependency(api_env, s3) -> None:
    """F8b. Not a 500, and emphatically not an empty 200 -- a viewer that
    spins forever or shows nothing tells the operator the wrong thing about
    their own data."""
    _, headers = await _org_with(api_env, "technician")
    document = await _document(api_env, headers)
    await _upload(api_env, headers, document["id"], "contract.pdf", PDF)

    api_env.app.state.object_store = _BrokenStore()
    content = await api_env.client.get(
        f"{API}/documents/{document['id']}/versions/1/content", headers=headers
    )
    assert content.status_code == 503, content.text
    assert "object storage" in content.json()["detail"]


# --------------------------------------------------------------------------- #
# RBAC (§9, §13 "other coverage")
# --------------------------------------------------------------------------- #


async def test_read_only_can_read_and_download_but_not_mutate(api_env, s3) -> None:
    """`docs.read` gates download deliberately: a separate download code would,
    on the existing grants, leave an AUDITOR able to see that a signed contract
    exists and unable to read it, which is the opposite of an auditor. The
    control on reading is the audit event, not a second gate."""
    org_id, reader_headers = await _org_with(api_env, "read-only")
    tech_sub = await api_env.create_user(org_id, "technician")
    tech_headers = await api_env.headers(org_id, tech_sub)
    document = await _document(api_env, tech_headers)
    await _upload(api_env, tech_headers, document["id"], "contract.pdf", PDF)

    assert (
        await api_env.client.get(f"{API}/documents", headers=reader_headers)
    ).status_code == 200
    assert (
        await api_env.client.get(
            f"{API}/documents/{document['id']}/versions/1/content", headers=reader_headers
        )
    ).status_code == 200
    assert (
        await api_env.client.post(
            f"{API}/documents", json={"title": "x"}, headers=reader_headers
        )
    ).status_code == 403
    assert (
        await _upload(api_env, reader_headers, document["id"], "x.txt", b"x")
    ).status_code == 403


async def test_a_technician_uploads_and_archives_but_cannot_purge(api_env, s3) -> None:
    """§9's whole reason for a third code. Editing the library and destroying
    evidence are different acts with different blast radii -- the same
    reasoning `health.remediate` records for not folding into `health.admin`.
    """
    _, headers = await _org_with(api_env, "technician")
    document = await _document(api_env, headers)
    assert (
        await _upload(api_env, headers, document["id"], "contract.pdf", PDF)
    ).status_code == 201
    purge = await api_env.client.post(
        f"{API}/documents/{document['id']}/versions/1/purge", headers=headers
    )
    assert purge.status_code == 403, purge.text
    assert (
        await api_env.client.post(f"{API}/documents/{document['id']}/archive", headers=headers)
    ).status_code == 200


async def test_an_org_admin_can_purge(api_env, s3) -> None:
    _, headers = await _org_with(api_env, "org-admin")
    document = await _document(api_env, headers)
    await _upload(api_env, headers, document["id"], "contract.pdf", PDF)
    assert (
        await api_env.client.post(
            f"{API}/documents/{document['id']}/versions/1/purge", headers=headers
        )
    ).status_code == 200


async def test_every_document_route_requires_a_permission(api_env) -> None:
    """Invariant 6, derived from the router rather than from a hand list --
    `route_permission` recovers the code out of `require_permission`'s
    closure, so a route added without a gate fails here."""
    from app.core.rbac import route_permission
    from app.modules.documentation.documents_api import router

    # §10 names ELEVEN routes on this router. Pinned so the loop below cannot
    # pass by iterating over nothing, which is how a "every route is gated"
    # sweep quietly stops being one.
    assert len(router.routes) == 11, [r.path for r in router.routes]
    codes = set()
    for route in router.routes:
        code = route_permission(route)
        assert code is not None, route.path
        assert code.startswith("docs."), (route.path, code)
        codes.add(code)
    assert codes == {"docs.read", "docs.manage", "docs.document.purge"}


async def test_no_document_route_shadows_a_shipped_docs_route(api_env) -> None:
    """§10's ordering claim, checked rather than asserted -- `service_desk/
    api.py` had to fix a real shadowing bug of exactly this kind. Every
    document path sits under the literal `/docs/documents`, and every `{...}`
    segment is preceded by a literal."""
    from app.modules.documentation.api import router as shipped
    from app.modules.documentation.documents_api import router as documents

    shipped_paths = {r.path for r in shipped.routes}
    for route in documents.routes:
        assert route.path.startswith("/docs/documents"), route.path
        assert route.path not in shipped_paths, route.path
        segments = route.path.strip("/").split("/")
        for index, segment in enumerate(segments):
            if segment.startswith("{"):
                assert index > 0 and not segments[index - 1].startswith("{"), route.path


# --------------------------------------------------------------------------- #
# Links (§7) and the archived freeze
# --------------------------------------------------------------------------- #


async def test_links_resolve_in_org_and_404_everywhere_else(api_env) -> None:
    org_id, headers = await _org_with(api_env, "technician", slug="doc-link-a")
    other_org, _ = await _org_with(api_env, "technician", slug="doc-link-b")
    document = await _document(api_env, headers)
    device_id = await _a_device(api_env, org_id)
    foreign_device = await _a_device(api_env, other_org)

    ok = await api_env.client.post(
        f"{API}/documents/{document['id']}/links",
        json={"target_type": "device", "target_id": str(device_id)},
        headers=headers,
    )
    assert ok.status_code == 201, ok.text

    duplicate = await api_env.client.post(
        f"{API}/documents/{document['id']}/links",
        json={"target_type": "device", "target_id": str(device_id)},
        headers=headers,
    )
    assert duplicate.status_code == 409, duplicate.text

    for target in (foreign_device, uuid4()):
        resp = await api_env.client.post(
            f"{API}/documents/{document['id']}/links",
            json={"target_type": "device", "target_id": str(target)},
            headers=headers,
        )
        assert resp.status_code == 404, (target, resp.text)

    unknown = await api_env.client.post(
        f"{API}/documents/{document['id']}/links",
        json={"target_type": "spaceship", "target_id": str(device_id)},
        headers=headers,
    )
    assert unknown.status_code == 422

    filtered = await api_env.client.get(
        f"{API}/documents?target_type=device&target_id={device_id}", headers=headers
    )
    assert [i["id"] for i in filtered.json()["items"]] == [document["id"]]

    removed = await api_env.client.delete(
        f"{API}/documents/{document['id']}/links/{ok.json()['id']}", headers=headers
    )
    assert removed.status_code == 200
    assert (
        await api_env.client.delete(
            f"{API}/documents/{document['id']}/links/{ok.json()['id']}", headers=headers
        )
    ).status_code == 404


async def test_the_document_link_vocabulary_is_the_shipped_one(api_env) -> None:
    """§0.7. Two vocabularies over one page would be a permanent version of the
    trap `service.py` already carries a comment about -- the resolver dict and
    the route regex must move together or one side 404s while the other 422s.
    """
    from app.modules.documentation.documents import LINK_TARGET_TYPES
    from app.modules.documentation.service import _LINK_TARGET_MODELS

    assert set(LINK_TARGET_TYPES) == set(_LINK_TARGET_MODELS)
    assert "ticket" not in LINK_TARGET_TYPES  # §0.7/§15.5
    assert "org" not in LINK_TARGET_TYPES  # every document already carries org_id


async def test_an_archived_document_takes_no_new_versions(api_env, s3) -> None:
    """409, the same freeze Service Desk applies to terminal tickets."""
    _, headers = await _org_with(api_env, "technician")
    document = await _document(api_env, headers)
    await api_env.client.post(f"{API}/documents/{document['id']}/archive", headers=headers)
    refused = await _upload(api_env, headers, document["id"], "late.txt", b"x")
    assert refused.status_code == 409, refused.text


# --------------------------------------------------------------------------- #
# Search (§8)
# --------------------------------------------------------------------------- #


async def test_search_finds_documents_by_title_description_and_filename(api_env, s3) -> None:
    _, headers = await _org_with(api_env, "technician")
    document = await _document(
        api_env,
        headers,
        title="Datacentre rack elevation",
        description="cabling for the mezzanine",
        category="Diagrams",
    )
    await _upload(api_env, headers, document["id"], "mezzanine-rack.png", PNG)

    for query in ("elevation", "cabling", "mezzanine-rack.png"):
        resp = await api_env.client.get(f"{API}/search?q={query}", headers=headers)
        assert resp.status_code == 200, resp.text
        hits = [h for h in resp.json() if h["kind"] == "document"]
        assert [h["id"] for h in hits] == [document["id"]], (query, resp.text)
        assert hits[0]["category"] == "Diagrams"


async def test_search_excludes_archived_documents(api_env, s3) -> None:
    _, headers = await _org_with(api_env, "technician")
    document = await _document(api_env, headers, title="Superseded runbook")
    await api_env.client.post(f"{API}/documents/{document['id']}/archive", headers=headers)
    resp = await api_env.client.get(f"{API}/search?q=Superseded", headers=headers)
    assert [h for h in resp.json() if h["kind"] == "document"] == []


async def test_search_does_not_leak_another_orgs_document(api_env, s3) -> None:
    _, a_headers = await _org_with(api_env, "technician", slug="doc-s-a")
    _, b_headers = await _org_with(api_env, "technician", slug="doc-s-b")
    await _document(api_env, a_headers, title="Unmistakable zzyzx contract")
    resp = await api_env.client.get(f"{API}/search?q=zzyzx", headers=b_headers)
    assert resp.status_code == 200
    assert [h for h in resp.json() if h["kind"] == "document"] == []
