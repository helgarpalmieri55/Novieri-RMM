"""Membership rules over HTTP -- cycle 2 of `docs/superpowers/specs/
2026-08-22-device-group-management-design.md`.

WHY THIS IS NOT "THE OTHER HALF OF CRUD". A device group is the platform's
targeting primitive: `deploy/dispatch.py` resolves a `target_type="group"` job
to `Device.device_group_id == target_id`, and `patch`, `backup` and `remote`
each key a policy on the same column. A rules document therefore decides WHICH
MACHINES A SIGNED JOB REACHES, from operator input, which makes it the one
place in this codebase where a form field becomes an authorization decision.

The three properties this file holds, and each is a way that could go wrong:

  1. THE VOCABULARY IS CLOSED, so a rule cannot say anything the evaluator
     cannot run. The `ne`-on-a-typo case is the specific disaster: an unknown
     fact path resolves to `_MISSING`, and `ne` holds on an absent fact, so
     `{"fact": "os_familly", "op": "ne", "value": "windows"}` claims EVERY
     device in the organization and reads like a reasonable rule.
  2. THE MATCHED SET IS INSPECTABLE BEFORE IT IS USED. `POST /preview` answers
     the exact count and a bounded sample, writes nothing, and runs the same
     matcher the sweep runs -- an author shown one number and given another has
     been shown a guess.
  3. STATIC XOR DYNAMIC IS ENFORCED (§10.3). `evaluate_rules` OVERWRITES a hand
     placement, so a group that is both is a group whose operator edits are
     reverted by a background process with no message.

THE TENANCY TESTS DRIVE THE REAL ROUTE ON THE BYPASSRLS ADMIN ENGINE (idiom 3
of `docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md`). Everything
served through `org_session` runs as `app_user` (NOBYPASSRLS), so a preview
that dropped its `org_id` predicate would still answer correctly under the
ordinary engine -- RLS covers for it -- and the audit's whole finding is that
such a test turns nothing red.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import AuditEvent, Device, DeviceGroup, InventorySnapshot
from tests.test_inventory_schema import FULL_DOC

GROUPS = "/api/v1/device-groups"
PREVIEW = f"{GROUPS}/preview"

WINDOWS = {"all": [{"fact": "os_family", "op": "eq", "value": "windows"}]}


async def _admin(api_env: Any, org_id: UUID) -> dict[str, str]:
    return await api_env.headers(org_id, await api_env.create_user(org_id, "org-admin"))


async def _create(api_env: Any, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    response = await api_env.client.post(GROUPS, headers=headers, json=body)
    assert response.status_code == 201, response.text
    payload: dict[str, Any] = response.json()
    return payload


async def _device_with_doc(
    api_env: Any,
    org_id: UUID,
    hostname: str,
    *,
    doc: dict[str, Any] | None = None,
    group_id: UUID | None = None,
    engine: AsyncEngine | None = None,
) -> UUID:
    """A device and, unless `doc` is None, one `InventorySnapshot` for it.

    A DEVICE WITH NO SNAPSHOT IS THE INTERESTING CASE, not an edge one. The
    evaluator reads the inventory DOCUMENT and never the device row, so a
    manually-created asset, a discovered switch and an enrolled machine that
    has not yet collected are all unmatchable -- and the console has to say so,
    or an operator writes a rule for a fleet and receives half of it with no
    explanation (§5.3).
    """
    async with AsyncSession(
        engine or api_env.admin_engine, expire_on_commit=False
    ) as session:
        device = Device(
            org_id=org_id,
            hostname=hostname,
            os_family="windows",
            device_group_id=group_id,
            client_id=org_id,
            site_id=org_id,
        )
        session.add(device)
        await session.flush()
        if doc is not None:
            session.add(
                InventorySnapshot(
                    org_id=org_id,
                    device_id=device.id,
                    taken_at=datetime.now(UTC),
                    raw={**doc, "hostname": hostname},
                )
            )
        await session.commit()
        return device.id


async def _group_row(api_env: Any, group_id: UUID) -> DeviceGroup:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        return (
            await session.execute(select(DeviceGroup).where(DeviceGroup.id == group_id))
        ).scalar_one()


async def _placements(api_env: Any, org_id: UUID) -> dict[UUID, UUID | None]:
    async with AsyncSession(api_env.admin_engine) as session:
        rows = (
            await session.execute(
                select(Device.id, Device.device_group_id).where(Device.org_id == org_id)
            )
        ).all()
    return {device_id: group_id for device_id, group_id in rows}


# --------------------------------------------------------------------------- #
# §5.1 -- the closed document, and the three validators
# --------------------------------------------------------------------------- #


async def test_a_rules_document_creates_a_dynamic_group_and_reads_back_typed(
    api_env: Any,
) -> None:
    """The route cycle 1 refused with "membership rules are not accepted yet".

    `GET /{id}` returns the document; the LIST route still does not, because a
    paged response carrying a per-row document is a different decision and
    `787461f` made it deliberately.
    """
    org_id = await api_env.create_org("dgr")
    headers = await _admin(api_env, org_id)

    created = await _create(api_env, headers, client_id=str(org_id), name="windows", rules=WINDOWS)
    assert created["dynamic"] is True

    detail = await api_env.client.get(f"{GROUPS}/{created['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["rules"] == WINDOWS

    listed = await api_env.client.get(GROUPS, headers=headers)
    assert "rules" not in listed.json()["items"][0]


async def test_a_typod_fact_under_ne_is_refused_at_authorship(api_env: Any) -> None:
    """THE ONE THAT MATTERS (§11.2). Under the evaluator this document claims
    every device in the organization, because an unknown fact path resolves to
    `_MISSING` and `ne` holds on an absent fact.

    It is refused on CREATE and on UPDATE, and the message names the fact --
    a 422 that says "value is not a valid dict" would send the author looking
    at their `value`.
    """
    org_id = await api_env.create_org("dgr")
    headers = await _admin(api_env, org_id)
    typo = {"all": [{"fact": "os_familly", "op": "ne", "value": "windows"}]}

    created = await api_env.client.post(
        GROUPS, headers=headers, json={"name": "oops", "client_id": str(org_id), "rules": typo}
    )
    assert created.status_code == 422, created.text
    assert "os_familly" in created.text

    live = await _create(api_env, headers, client_id=str(org_id), name="fine")
    updated = await api_env.client.patch(
        f"{GROUPS}/{live['id']}", headers=headers, json={"rules": typo}
    )
    assert updated.status_code == 422, updated.text

    # NON-VACUOUS: the same shape with the fact spelled correctly is accepted,
    # so the refusal is about the fact path and not about `ne`.
    ok = await api_env.client.patch(
        f"{GROUPS}/{live['id']}",
        headers=headers,
        json={"rules": {"all": [{"fact": "os_family", "op": "ne", "value": "linux"}]}},
    )
    assert ok.status_code == 200, ok.text


async def test_the_documents_the_evaluator_cannot_run_are_each_422(
    api_env: Any,
) -> None:
    """§11.3, plus the two prefix forms that must stay OPEN.

    A rule that matches nothing is not an error. A rule that cannot be
    EVALUATED is -- and every refusal below is a document the ingest path would
    have caught, logged at warning, and skipped in silence.
    """
    org_id = await api_env.create_org("dgr")
    headers = await _admin(api_env, org_id)

    async def refused(rules: Any) -> int:
        response = await api_env.client.post(
            GROUPS,
            headers=headers,
            json={"name": f"g-{uuid4().hex[:8]}", "client_id": str(org_id), "rules": rules},
        )
        return response.status_code

    # `gt` calls `float(expected)` and raises on anything else.
    assert await refused({"all": [{"fact": "ram_bytes", "op": "gt", "value": "soon"}]}) == 422
    # An empty `all` matches every device vacuously in a naive reading, and
    # raises in `matches_document`. Either way it is not a rule.
    assert await refused({"all": []}) == 422
    # An unknown key ANYWHERE: `extra="forbid"` on both models.
    assert await refused({"all": [{"fact": "hostname", "op": "eq", "value": "a", "x": 1}]}) == 422
    one = {"all": [{"fact": "hostname", "op": "eq", "value": "a"}], "any": []}
    assert await refused(one) == 422
    # An op outside the closed set.
    assert await refused({"all": [{"fact": "hostname", "op": "startswith", "value": "a"}]}) == 422
    # A prefix with nothing after it names no fact.
    assert await refused({"all": [{"fact": "facts.", "op": "eq", "value": "a"}]}) == 422
    assert await refused({"all": [{"fact": "software:", "op": "eq", "value": "a"}]}) == 422

    # AND THE THINGS THAT MUST STILL BE ACCEPTED, or the validator has quietly
    # become a straitjacket: both open prefixes, and `contains` on a numeric
    # fact -- `str(value)` is well defined and the operator may want it.
    for rules in (
        {"all": [{"fact": "facts.domain_joined", "op": "eq", "value": True}]},
        {"all": [{"fact": "software:7-Zip", "op": "contains", "value": "24"}]},
        {"all": [{"fact": "ram_bytes", "op": "contains", "value": "17"}]},
        {"all": [{"fact": "ram_bytes", "op": "gt", "value": "8000"}]},
    ):
        assert await refused(rules) == 201, rules


# --------------------------------------------------------------------------- #
# §5.3 -- the preview
# --------------------------------------------------------------------------- #


async def test_preview_counts_exactly_writes_nothing_and_truncates_its_sample(
    api_env: Any,
) -> None:
    """§11.5. Three claims in one fixture, because they are one property: the
    author is shown what the rule reaches, and being shown it costs nothing.

    THE SAMPLE IS BOUNDED AND THE COUNTS ARE NOT. A screen cannot render 4,000
    hostnames and an author must still be told the rule claims 4,000 machines;
    a truncated count would be the more dangerous of the two to round.
    """
    org_id = await api_env.create_org("dgr")
    headers = await _admin(api_env, org_id)
    home = await _create(api_env, headers, client_id=str(org_id), name="home")

    for index in range(30):
        await _device_with_doc(
            api_env, org_id, f"win-{index:02d}", doc=FULL_DOC, group_id=UUID(home["id"])
        )
    await _device_with_doc(
        api_env, org_id, "linux-01", doc={**FULL_DOC, "os_family": "linux"}
    )
    # No snapshot: unmatchable, and counted separately rather than as a miss.
    await _device_with_doc(api_env, org_id, "never-collected", doc=None)

    before = await _placements(api_env, org_id)
    response = await api_env.client.post(PREVIEW, headers=headers, json={"rules": WINDOWS})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["matched"] == 30
    assert body["devices_without_inventory"] == 1
    assert body["sample_truncated"] is True
    assert len(body["sample"]) == 25
    assert body["sample"][0]["current_group_name"] == "home"
    assert body["sample"][0]["current_group_id"] == home["id"]
    assert body["sample"][0]["hostname"].startswith("win-")

    assert await _placements(api_env, org_id) == before, "the preview moved a device"
    async with AsyncSession(api_env.admin_engine) as session:
        actions = (
            await session.execute(
                select(AuditEvent.action).where(AuditEvent.org_id == org_id)
            )
        ).scalars().all()
    assert "device_group.membership_reevaluated" not in actions


async def test_preview_reports_the_no_inventory_count_that_explains_a_short_match(
    api_env: Any,
) -> None:
    """§5.3's third consequence, stated rather than discovered: an operator who
    is not told about the un-collected half concludes their rule is wrong."""
    org_id = await api_env.create_org("dgr")
    headers = await _admin(api_env, org_id)
    await _device_with_doc(api_env, org_id, "collected", doc=FULL_DOC)
    for index in range(4):
        await _device_with_doc(api_env, org_id, f"bare-{index}", doc=None)

    body = (
        await api_env.client.post(PREVIEW, headers=headers, json={"rules": WINDOWS})
    ).json()
    assert body["matched"] == 1
    assert body["devices_without_inventory"] == 4
    assert body["sample_truncated"] is False


async def test_preview_agrees_with_the_sweeps_matcher(api_env: Any) -> None:
    """The property that makes a preview a preview: the same document, the same
    stored snapshot, the same answer. Two matchers is how an author is shown 12
    and given 400."""
    from app.modules.inventory.rules import matches_document
    from app.modules.inventory.schema import InventoryDocument

    org_id = await api_env.create_org("dgr")
    headers = await _admin(api_env, org_id)
    rules = {
        "all": [
            {"fact": "os_family", "op": "eq", "value": "windows"},
            {"fact": "ram_bytes", "op": "gt", "value": 8_000_000_000},
            {"fact": "software:7-Zip", "op": "contains", "value": "23."},
        ]
    }
    await _device_with_doc(api_env, org_id, "match", doc=FULL_DOC)
    await _device_with_doc(
        api_env, org_id, "small", doc={**FULL_DOC, "hardware": {"ram_bytes": 4}}
    )

    body = (
        await api_env.client.post(PREVIEW, headers=headers, json={"rules": rules})
    ).json()
    assert body["matched"] == 1
    assert matches_document(rules, InventoryDocument(**FULL_DOC))
    assert not matches_document(
        rules, InventoryDocument(**{**FULL_DOC, "hardware": {"ram_bytes": 4}})
    )


async def test_preview_takes_devices_manage_and_not_devices_read(api_env: Any) -> None:
    """§11.21, and §8.1's reason: `/preview` answers "which machines would this
    claim", which is the AUTHORING act, and it is the one read that enumerates
    the fleet against an arbitrary caller-supplied predicate."""
    org_id = await api_env.create_org("dgr")
    admin = await _admin(api_env, org_id)
    reader = await api_env.headers(org_id, await api_env.create_user(org_id, "read-only"))
    tech = await api_env.headers(org_id, await api_env.create_user(org_id, "technician"))

    for headers in (reader, tech):
        response = await api_env.client.post(PREVIEW, headers=headers, json={"rules": WINDOWS})
        assert response.status_code == 403, response.text

    # Non-vacuous: `read-only` holds `devices.read` and the DETAIL route answers
    # it, so the 403s above are about this route's gate and not about the token.
    group = await _create(api_env, admin, client_id=str(org_id), name="g")
    assert (
        await api_env.client.get(f"{GROUPS}/{group['id']}", headers=reader)
    ).status_code == 200
    assert (
        await api_env.client.post(PREVIEW, headers=admin, json={"rules": WINDOWS})
    ).status_code == 200


async def test_preview_refuses_a_document_it_cannot_run(api_env: Any) -> None:
    """The preview validates the SAME model the write routes do. A preview that
    accepted a document the save then refused would be advice about a rule the
    operator cannot have."""
    org_id = await api_env.create_org("dgr")
    headers = await _admin(api_env, org_id)
    response = await api_env.client.post(
        PREVIEW,
        headers=headers,
        json={"rules": {"all": [{"fact": "os_familly", "op": "ne", "value": "x"}]}},
    )
    assert response.status_code == 422, response.text


async def test_preview_counts_only_this_orgs_devices_on_the_admin_engine(
    api_env: Any, admin_engine: AsyncEngine
) -> None:
    """THE TENANCY TEST THAT CAN ACTUALLY FAIL (audit idiom 3).

    `/preview` enumerates the fleet against a caller-supplied predicate, which
    makes it the easiest route in this cycle to lose an `org_id` on. Driven
    through the real route with `app.state.app_engine` repointed at the
    BYPASSRLS admin engine, so RLS is not there to mask a missing predicate.

    FALSIFIED: delete `Device.org_id == org_id` from `_active_device_batch` and
    this turns red. Verified by doing it.

    AND `devices_without_inventory` IS WHY IT TURNS RED, which is worth stating
    because the first version of this test asserted only the match count and
    STAYED GREEN under that deletion. With the device predicate gone the
    neighbour's three machines are enumerated, but `_latest_documents` still
    carries its own `org_id`, so their snapshots are not -- they land in the
    no-inventory bucket and `matched` is still 1. A route that had just walked
    another tenant's fleet would have reported a clean number. The count of
    what could not be matched is the half that sees it.

    THE SNAPSHOT QUERY'S OWN `org_id` IS NOT SEPARATELY FALSIFIABLE, and that
    is said rather than implied: `device_id IN (batch)` already scopes it while
    the predicate above holds, so deleting it alone changes no answer. It stays
    because it costs nothing and because the batch's provenance is not a thing
    this file should have to assume -- but no test here can hold it, and a
    docstring claiming otherwise would be the comfort this audit is about.
    """
    theirs = await api_env.create_org("dgr-theirs")
    mine = await api_env.create_org("dgr-mine")
    headers = await _admin(api_env, mine)

    for index in range(3):
        await _device_with_doc(
            api_env, theirs, f"their-win-{index}", doc=FULL_DOC, engine=admin_engine
        )
    await _device_with_doc(api_env, mine, "my-win", doc=FULL_DOC, engine=admin_engine)

    original = api_env.app.state.app_engine
    api_env.app.state.app_engine = admin_engine
    try:
        response = await api_env.client.post(PREVIEW, headers=headers, json={"rules": WINDOWS})
    finally:
        api_env.app.state.app_engine = original

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["matched"] == 1, "the preview counted a neighbour's fleet"
    assert body["devices_without_inventory"] == 0, (
        "the preview enumerated devices from another tenant"
    )
    assert [row["hostname"] for row in body["sample"]] == ["my-win"]


async def test_preview_for_a_neighbours_group_is_a_404_on_the_admin_engine(
    api_env: Any, admin_engine: AsyncEngine
) -> None:
    """`group_id` is optional and, when given, must resolve in THIS org. Same
    refusal for absent and for another tenant's, indistinguishably: a 403 would
    confirm the id exists somewhere, which is a cross-tenant existence oracle.
    """
    theirs = await api_env.create_org("dgr-theirs")
    mine = await api_env.create_org("dgr-mine")
    headers = await _admin(api_env, mine)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        victim = DeviceGroup(org_id=theirs, name="their-group", client_id=theirs)
        session.add(victim)
        await session.commit()
        victim_id = victim.id

    original = api_env.app.state.app_engine
    api_env.app.state.app_engine = admin_engine
    try:
        response = await api_env.client.post(
            PREVIEW,
            headers=headers,
            json={"rules": WINDOWS, "group_id": str(victim_id)},
        )
    finally:
        api_env.app.state.app_engine = original
    assert response.status_code == 404, response.text


# --------------------------------------------------------------------------- #
# §10.3 -- static XOR dynamic, enforced
# --------------------------------------------------------------------------- #


async def test_rules_are_refused_on_a_group_holding_hand_placed_devices(
    api_env: Any,
) -> None:
    """§5.2. The refusal names the COUNT and the remedy, because "409" alone
    leaves the operator to guess which of their groups is the occupied one.

    Without this, the next sweep silently evicts every hand placement in the
    group -- `evaluate_rules` assigns on a match whatever was there before, and
    the support ticket says "it forgot my change".
    """
    org_id = await api_env.create_org("dgr")
    headers = await _admin(api_env, org_id)
    group = await _create(api_env, headers, client_id=str(org_id), name="hand-picked")
    for index in range(3):
        await _device_with_doc(
            api_env, org_id, f"h-{index}", doc=FULL_DOC, group_id=UUID(group["id"])
        )

    refused = await api_env.client.patch(
        f"{GROUPS}/{group['id']}", headers=headers, json={"rules": WINDOWS}
    )
    assert refused.status_code == 409, refused.text
    assert "3" in refused.json()["detail"]
    assert (await _group_row(api_env, UUID(group["id"]))).rules is None

    # THE REMEDY THE MESSAGE NAMES, exercised: empty it, then add the rules.
    for device_id in [
        device
        for device, group_of in (await _placements(api_env, org_id)).items()
        if group_of == UUID(group["id"])
    ]:
        assert (
            await api_env.client.delete(
                f"{GROUPS}/{group['id']}/members/{device_id}", headers=headers
            )
        ).status_code == 204
    accepted = await api_env.client.patch(
        f"{GROUPS}/{group['id']}", headers=headers, json={"rules": WINDOWS}
    )
    assert accepted.status_code == 200, accepted.text


async def test_clearing_the_rules_moves_nobody_and_makes_the_group_static(
    api_env: Any,
) -> None:
    """§7.2's paragraph, which exists because without it the two rules in this
    spec DEADLOCK a rule-bearing group into being unarchivable.

    `POST /{id}/members` and `DELETE /{id}/members/{device}` are both refused on
    a rule-bearing group, so its members must first stop being rule-claimed --
    and `PATCH` to `null` does that WITHOUT moving anyone, because
    `evaluate_rules` only ever assigns on a match and never unassigns. The
    devices stay exactly where they are and the group becomes static, holding
    the same members, which can then be released one visible act at a time.
    """
    org_id = await api_env.create_org("dgr")
    headers = await _admin(api_env, org_id)
    group = await _create(
        api_env, headers, client_id=str(org_id), name="rule-driven", rules=WINDOWS
    )
    held = await _device_with_doc(
        api_env, org_id, "claimed", doc=FULL_DOC, group_id=UUID(group["id"])
    )

    blocked = await api_env.client.delete(
        f"{GROUPS}/{group['id']}/members/{held}", headers=headers
    )
    assert blocked.status_code == 409, blocked.text

    cleared = await api_env.client.patch(
        f"{GROUPS}/{group['id']}", headers=headers, json={"rules": None}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["dynamic"] is False
    assert (await _placements(api_env, org_id))[held] == UUID(group["id"]), (
        "clearing the rules moved a device"
    )

    released = await api_env.client.delete(
        f"{GROUPS}/{group['id']}/members/{held}", headers=headers
    )
    assert released.status_code == 204, released.text
    assert (await _placements(api_env, org_id))[held] is None


async def test_an_edit_that_does_not_mention_rules_leaves_them_alone(
    api_env: Any,
) -> None:
    """`exclude_unset` is doing real work here. `rules: GroupRules | None =
    None` means "absent" and "explicitly null" arrive identically unless the
    handler asks which it was -- and a rename that silently emptied a rules
    document would be the quietest fleet re-segmentation in the product."""
    org_id = await api_env.create_org("dgr")
    headers = await _admin(api_env, org_id)
    group = await _create(
        api_env, headers, client_id=str(org_id), name="rule-driven", rules=WINDOWS
    )

    renamed = await api_env.client.patch(
        f"{GROUPS}/{group['id']}", headers=headers, json={"name": "renamed"}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["dynamic"] is True
    assert (await _group_row(api_env, UUID(group["id"]))).rules == WINDOWS


# --------------------------------------------------------------------------- #
# §8.2 -- audit
# --------------------------------------------------------------------------- #


async def test_a_rules_write_records_the_document_on_both_sides(api_env: Any) -> None:
    """§8.2: `device_group.created` carries the full document when present, and
    `device_group.updated` carries `{"from": ..., "to": ...}` for `rules`.

    A rules edit can empty a group entirely. An audit entry saying only that
    `rules` changed answers nothing an investigator asked.
    """
    org_id = await api_env.create_org("dgr")
    headers = await _admin(api_env, org_id)
    group = await _create(api_env, headers, client_id=str(org_id), name="win", rules=WINDOWS)
    later = {"all": [{"fact": "os_family", "op": "eq", "value": "linux"}]}
    assert (
        await api_env.client.patch(
            f"{GROUPS}/{group['id']}", headers=headers, json={"rules": later}
        )
    ).status_code == 200

    async with AsyncSession(api_env.admin_engine) as session:
        events = (
            await session.execute(
                select(AuditEvent.action, AuditEvent.meta)
                .where(AuditEvent.org_id == org_id)
                .order_by(AuditEvent.id)
            )
        ).all()
    by_action = {action: meta for action, meta in events}
    assert by_action["device_group.created"]["dynamic"] is True
    assert by_action["device_group.created"]["rules"] == WINDOWS
    assert by_action["device_group.updated"]["changed"]["rules"] == {
        "from": WINDOWS,
        "to": later,
    }

    verified = await api_env.client.get("/api/v1/audit/verify", headers=headers)
    assert verified.status_code == 200, verified.text
    assert verified.json()["ok"] is True


async def test_a_static_group_is_written_with_sql_null_and_not_jsonb_null(
    api_env: Any,
) -> None:
    """The model-side half of the same defect, asserted at the column.

    `DeviceGroup.rules` carries `JSONB(none_as_null=True)`; without it
    SQLAlchemy writes the JSON literal `null`, which is a VALUE and not an
    absence -- `rules IS NOT NULL` is true of it. Nothing in any response
    changes either way (it deserializes to Python `None`), which is precisely
    why this has to be asserted against the stored bytes rather than against a
    payload.

    FALSIFIED: drop `none_as_null=True` and this reads `'null'` instead of
    NULL. Verified by doing it.
    """
    org_id = await api_env.create_org("dgr")
    headers = await _admin(api_env, org_id)
    static = await _create(api_env, headers, client_id=str(org_id), name="hand-picked")
    dynamic = await _create(
        api_env, headers, client_id=str(org_id), name="rule-driven", rules=WINDOWS
    )

    async with AsyncSession(api_env.admin_engine) as session:
        kinds = dict(
            (
                await session.execute(
                    text(
                        "SELECT id::text, jsonb_typeof(rules) FROM device_groups "
                        "WHERE org_id = :org"
                    ),
                    {"org": str(org_id)},
                )
            ).all()
        )
    assert kinds[static["id"]] is None, "a static group stored jsonb `null`, not SQL NULL"
    assert kinds[dynamic["id"]] == "object"
