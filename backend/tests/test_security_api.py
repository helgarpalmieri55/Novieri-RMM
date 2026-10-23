import csv
import io
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import Device, Organization, SecurityEvent
from app.modules.security_logs.api import _filtered_query


async def _seed(api_env, org_id: UUID, hostname: str = "sec-host") -> UUID:
    now = datetime.now(UTC)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device = Device(org_id=org_id, hostname=hostname, os_family="windows")
        session.add(device)
        await session.flush()
        events = [
            ("a1", now - timedelta(hours=1), "logon_success", "alice", "success"),
            ("a2", now - timedelta(hours=2), "logon_failure", "alice", "failure"),
            ("b1", now - timedelta(hours=3), "privilege_elevation", "bob", "n_a"),
            ("c1", now - timedelta(days=40), "logon_success", "carol", "success"),
        ]
        for uid, ts, event_type, user, outcome in events:
            session.add(
                SecurityEvent(
                    org_id=org_id,
                    device_id=device.id,
                    event_uid=uid,
                    ts=ts,
                    event_type=event_type,
                    username=user,
                    source="win:4624",
                    outcome=outcome,
                    raw={"uid": uid},
                )
            )
        await session.commit()
        return device.id


async def test_rbac_and_filters(api_env) -> None:
    org_id = await api_env.create_org("sapi")
    device_id = await _seed(api_env, org_id)

    tech = await api_env.create_user(org_id, "technician")
    assert (
        await api_env.client.get(
            "/api/v1/security-events", headers=await api_env.headers(org_id, tech)
        )
    ).status_code == 403

    auditor = await api_env.create_user(org_id, "auditor")
    headers = await api_env.headers(org_id, auditor)

    everything = await api_env.client.get("/api/v1/security-events", headers=headers)
    assert everything.status_code == 200
    assert everything.json()["total"] == 4
    # newest first
    assert [e["username"] for e in everything.json()["items"]][:2] == ["alice", "alice"]

    by_user = await api_env.client.get("/api/v1/security-events?username=ALI", headers=headers)
    assert by_user.json()["total"] == 2

    by_type = await api_env.client.get(
        "/api/v1/security-events?event_type=privilege_elevation", headers=headers
    )
    assert by_type.json()["total"] == 1
    assert by_type.json()["items"][0]["username"] == "bob"
    assert by_type.json()["items"][0]["hostname"] == "sec-host"

    since = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    windowed = await api_env.client.get(
        "/api/v1/security-events", params={"from_ts": since}, headers=headers
    )
    assert windowed.json()["total"] == 3  # carol's 40-day-old logon excluded

    by_device = await api_env.client.get(
        f"/api/v1/security-events?device_id={device_id}", headers=headers
    )
    assert by_device.json()["total"] == 4


async def test_another_orgs_security_event_answers_404_and_detail_carries_raw(api_env) -> None:
    org_a = await api_env.create_org("sapi")
    org_b = await api_env.create_org("sapi-b")
    await _seed(api_env, org_b, "foreign-host")

    auditor = await api_env.create_user(org_a, "auditor")
    headers = await api_env.headers(org_a, auditor)
    assert (
        await api_env.client.get("/api/v1/security-events", headers=headers)
    ).json()["total"] == 0

    b_auditor = await api_env.create_user(org_b, "auditor")
    b_headers = await api_env.headers(org_b, b_auditor)
    listing = (await api_env.client.get("/api/v1/security-events", headers=b_headers)).json()
    event_id = listing["items"][0]["id"]
    detail = await api_env.client.get(f"/api/v1/security-events/{event_id}", headers=b_headers)
    assert detail.status_code == 200
    assert "raw" in detail.json() and detail.json()["raw"]
    # foreign org cannot fetch it
    assert (
        await api_env.client.get(f"/api/v1/security-events/{event_id}", headers=headers)
    ).status_code == 404


async def test_access_evidence_csv(api_env) -> None:
    org_id = await api_env.create_org("sapi")
    await _seed(api_env, org_id, "csv-sec-host")
    auditor = await api_env.create_user(org_id, "auditor")
    headers = await api_env.headers(org_id, auditor)

    since = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    response = await api_env.client.get(
        "/api/v1/reports/access.csv", params={"from_ts": since}, headers=headers
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == ["ts", "hostname", "username", "event_type", "source", "outcome"]
    assert len(rows) == 4  # header + 3 in-window events
    assert {r[2] for r in rows[1:]} == {"alice", "bob"}


async def test_access_evidence_csv_escapes_free_text_columns(api_env) -> None:
    """`username`/`hostname`/`source` are agent-reported free text with no
    fixed vocabulary (`SecurityEventDoc.username`/`.source` are plain,
    length-capped strings) -- each is a formula-injection candidate in this
    SOC 2 evidence export, which is explicitly meant to be opened in a
    spreadsheet. `event_type`/`outcome` are `Literal`-typed on
    `SecurityEventDoc` -- ingestion rejects anything outside that fixed
    vocabulary -- and `ts` is a server-formatted timestamp, so none of those
    three can carry a payload and none should be escaped."""
    org_id = await api_env.create_org("sapi")
    now = datetime.now(UTC)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device = Device(org_id=org_id, hostname="=cmd|' /C calc'!A1", os_family="windows")
        session.add(device)
        await session.flush()
        session.add(
            SecurityEvent(
                org_id=org_id,
                device_id=device.id,
                event_uid="inj-1",
                ts=now,
                event_type="logon_success",
                username="+1+1",
                source="-win:4624",
                outcome="success",
                raw={},
            )
        )
        await session.commit()

    auditor = await api_env.create_user(org_id, "auditor")
    headers = await api_env.headers(org_id, auditor)

    response = await api_env.client.get("/api/v1/reports/access.csv", headers=headers)
    assert response.status_code == 200, response.text
    rows = list(csv.reader(io.StringIO(response.text)))
    header = rows[0]
    row = next(r for r in rows[1:] if r[header.index("username")] == "'+1+1")

    assert row[header.index("hostname")] == "'=cmd|' /C calc'!A1"
    assert row[header.index("username")] == "'+1+1"
    assert row[header.index("source")] == "'-win:4624"
    # Literal-constrained / server-formatted columns: never escaped.
    assert row[header.index("event_type")] == "logon_success"
    assert row[header.index("outcome")] == "success"


# --------------------------------------------------------------------------- #
# The export matches the screen (audit
# `docs/superpowers/audits/2026-08-21-ux-remediation-closeout.md`, rec. 7 --
# finding A58, "'Export evidence CSV' ignores the active username and
# event-type filters, the export does not match what is on screen").
#
# The console could not fix this: appending the two parameters to the href
# would have sent values FastAPI discarded while the link claimed otherwise,
# so the closeout shipped the honest label instead ("Export all events (24h)",
# plus a sentence naming the filters the file does not carry). These tests are
# what lets that label go back to telling the truth.
# --------------------------------------------------------------------------- #


async def test_the_export_carries_the_username_and_event_type_filters(api_env) -> None:
    org_id = await api_env.create_org("sapi")
    await _seed(api_env, org_id, "filtered-host")
    auditor = await api_env.create_user(org_id, "auditor")
    headers = await api_env.headers(org_id, auditor)

    by_user = await api_env.client.get(
        "/api/v1/reports/access.csv", params={"username": "alice"}, headers=headers
    )
    assert by_user.status_code == 200
    rows = list(csv.reader(io.StringIO(by_user.text)))
    assert {r[2] for r in rows[1:]} == {"alice"}
    assert len(rows) == 3  # header + alice's two events

    by_type = await api_env.client.get(
        "/api/v1/reports/access.csv",
        params={"event_type": "privilege_elevation"},
        headers=headers,
    )
    rows = list(csv.reader(io.StringIO(by_type.text)))
    assert len(rows) == 2
    assert rows[1][2] == "bob"


async def test_the_export_filters_exactly_as_the_search_does(api_env) -> None:
    """A58's actual complaint is not "the CSV takes no filters", it is "the CSV
    does not match what is on screen". Matching means the SAME predicate, so
    both routes go through `_filtered_query`: `username` is the substring,
    case-insensitive match the search offers (`ALI` finds alice), and
    `event_type` is exact on both. A second, subtly different implementation
    here would reproduce the defect with extra steps."""
    org_id = await api_env.create_org("sapi")
    await _seed(api_env, org_id, "same-predicate-host")
    auditor = await api_env.create_user(org_id, "auditor")
    headers = await api_env.headers(org_id, auditor)

    params = {"username": "ALI", "event_type": "logon_failure"}
    screen = await api_env.client.get(
        "/api/v1/security-events", params=params, headers=headers
    )
    exported = await api_env.client.get(
        "/api/v1/reports/access.csv", params=params, headers=headers
    )
    rows = list(csv.reader(io.StringIO(exported.text)))[1:]

    assert screen.json()["total"] == len(rows) == 1
    assert rows[0][2] == screen.json()["items"][0]["username"] == "alice"


async def test_the_export_is_audited_with_the_filters_that_produced_it(api_env) -> None:
    """Invariant #4. A file of who-logged-into-what leaving the platform is a
    privileged act, and this route recorded nothing at all before now -- the
    same gap `bd04957` found behind the error monitor's resolve.

    The record is the reporting module's shape (`app/modules/reporting/
    export.py`): the FILTERS, the ROW COUNT and the FORMAT, never the rows.
    `audit_events` is append-only and unprunable by `app_user`, so a row of
    exported evidence written here could not be taken back. The filters are
    the half that matters for this route: without them the log says a CSV left
    the platform and cannot say which events were in it.
    """
    org_id = await api_env.create_org("sapi")
    await _seed(api_env, org_id, "audited-host")
    auditor = await api_env.create_user(org_id, "auditor")
    headers = await api_env.headers(org_id, auditor)

    before = await api_env.client.get(
        "/api/v1/audit", params={"action": "security_logs.evidence_exported"}, headers=headers
    )
    assert before.json()["total"] == 0

    since = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    exported = await api_env.client.get(
        "/api/v1/reports/access.csv",
        params={"from_ts": since, "username": "ali"},
        headers=headers,
    )
    assert exported.status_code == 200

    after = await api_env.client.get(
        "/api/v1/audit", params={"action": "security_logs.evidence_exported"}, headers=headers
    )
    assert after.json()["total"] == 1
    event = after.json()["items"][0]
    assert event["actor_type"] == "user"
    assert event["target_type"] == "report"
    assert event["target_id"] == "access-evidence"
    assert set(event["metadata"]) == {"filters", "row_count", "format"}
    assert event["metadata"]["format"] == "csv"
    assert event["metadata"]["row_count"] == 2
    assert event["metadata"]["filters"]["username"] == "ali"
    assert event["metadata"]["filters"]["event_type"] is None
    assert event["metadata"]["filters"]["from_ts"] is not None


async def test_searching_writes_no_audit_event(api_env) -> None:
    """The negative control the reporting spec's §0.7 test pairs with the one
    above: an export is audited and a SEARCH is not. Without this, "audit
    everything the auditor touches" is one careless edit away and the log
    fills with the reading of it."""
    org_id = await api_env.create_org("sapi")
    await _seed(api_env, org_id, "unaudited-host")
    auditor = await api_env.create_user(org_id, "auditor")
    headers = await api_env.headers(org_id, auditor)

    before = (await api_env.client.get("/api/v1/audit", headers=headers)).json()["total"]
    assert (
        await api_env.client.get("/api/v1/security-events", headers=headers)
    ).status_code == 200
    after = (await api_env.client.get("/api/v1/audit", headers=headers)).json()["total"]
    assert after == before


async def test_the_event_query_is_tenant_scoped_where_rls_is_not_the_thing_rejecting(
    admin_engine: AsyncEngine,
) -> None:
    """`_filtered_query`'s OWN `org_id` predicates, pinned against an engine
    that ignores RLS.

    Both routes here run through `org_session` on a NOBYPASSRLS role, where
    Postgres rejects a cross-tenant row whether or not the query asked it to --
    so deleting a `WHERE org_id` from this builder turns none of the route
    tests above red (`docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md`).
    `its_admin` carries BYPASSRLS, so here the SQL is the only thing scoping
    anything.

    BOTH SIDES OF THE JOIN ARE CHECKED, and the fixture includes the row
    nothing in the schema forbids: an event naming ANOTHER org's device.
    `security_events.device_id` is a bare FK with no org-aware constraint, and
    the join exists to put that device's HOSTNAME in the evidence file -- so a
    missing predicate on `Device` prints a neighbour tenant's machine name into
    this org's SOC 2 export. `theirs` is asserted to still see its own row, so
    the test cannot pass by matching nothing.
    """
    orgs: dict[str, UUID] = {}
    devices: dict[str, UUID] = {}
    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        for key in ("mine", "theirs"):
            org = Organization(name=key, slug=f"sec-{uuid4().hex[:8]}")
            session.add(org)
            await session.flush()
            orgs[key] = org.id
            device = Device(org_id=org.id, hostname=f"host-{key}", os_family="windows")
            session.add(device)
            await session.flush()
            devices[key] = device.id
            session.add(
                SecurityEvent(
                    org_id=org.id,
                    device_id=device.id,
                    event_uid=f"uid-{key}",
                    ts=now,
                    event_type="logon_success",
                    username=f"user-{key}",
                    source="win:4624",
                    outcome="success",
                    raw={},
                )
            )
        # Legal, and unconstrained by the schema: MY event naming THEIR device.
        session.add(
            SecurityEvent(
                org_id=orgs["mine"],
                device_id=devices["theirs"],
                event_uid="uid-crossed",
                ts=now,
                event_type="logon_success",
                username="user-crossed",
                source="win:4624",
                outcome="success",
                raw={},
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        mine = (
            await session.execute(_filtered_query(orgs["mine"], None, None, None, None, None))
        ).all()
        theirs = (
            await session.execute(_filtered_query(orgs["theirs"], None, None, None, None, None))
        ).all()

    assert {event.username for event, _ in mine} == {"user-mine"}, (
        "a neighbour's event must not appear, and mine naming their device must not "
        "drag their hostname in with it"
    )
    assert {event.username for event, _ in theirs} == {"user-theirs"}
