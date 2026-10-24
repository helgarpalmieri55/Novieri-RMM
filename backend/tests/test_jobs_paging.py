"""Asking `GET /jobs` for a page in a stated order (audit
`docs/superpowers/audits/2026-08-21-ux-remediation-closeout.md`, rec. 10 —
finding B46, "job list has no filter, search, sort, pagination or refresh
control").

WHY ORDERING AND PAGING ARE ONE CHANGE. The closeout deliberately did NOT add
client-side sorting, and said why: `listJobs()` sent no query string, so the
console silently took the server default of `limit=100` while its own tile
showed the true total. Sorting that page by "oldest first" would have re-sorted
the newest hundred and labelled the result oldest — worse than no sort. So the
server has to be able to answer "the oldest N", and this is that answer.

THE SORT KEY IS `id`, WHICH IS NOT MERELY THE EASY ONE. `jobs.id` is a uuid7:
issue-ordered, unique, and never rewritten. `issued_at` is the obvious
alternative and is disqualified by `JobEngine.resign_for_retry`, which stamps
it with `now` on every retry — a mutable sort key makes offset paging lie, so a
retry landing between two page fetches can show a row twice or skip one
entirely. `newest` is byte-for-byte the ordering this route has always had, so
a caller that sends nothing sees no change.
"""

import asyncio
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device


async def _device(api_env, org_id: UUID) -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device = Device(
            org_id=org_id,
            hostname="paging-host",
            os_family="windows",
            client_id=org_id,
            site_id=org_id,
        )
        session.add(device)
        await session.commit()
        return device.id


async def _three_jobs(api_env, org_id: UUID, headers: dict[str, str]) -> list[str]:
    device_id = await _device(api_env, org_id)
    ids: list[str] = []
    for _ in range(3):
        created = await api_env.client.post(
            "/api/v1/jobs",
            json={"type": "hello", "device_id": str(device_id), "params": {}},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        ids.append(created.json()["id"])
        # uuid7 is time-ordered to the millisecond; three dispatches inside one
        # can tie, and a test about ORDER must not depend on how fast the
        # machine is.
        await asyncio.sleep(0.005)
    return ids


async def test_the_queue_can_be_asked_for_oldest_first(api_env) -> None:
    org_id = await api_env.create_org("jobpage")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    first, second, third = await _three_jobs(api_env, org_id, headers)

    oldest = await api_env.client.get("/api/v1/jobs?order=oldest", headers=headers)
    assert [j["id"] for j in oldest.json()["items"]] == [first, second, third]

    newest = await api_env.client.get("/api/v1/jobs?order=newest", headers=headers)
    assert [j["id"] for j in newest.json()["items"]] == [third, second, first]


async def test_the_default_order_is_the_one_this_route_has_always_had(api_env) -> None:
    """A caller that sends no `order` must see exactly what it saw before this
    parameter existed. Anything else is a silent behaviour change to every
    console and script already reading this route."""
    org_id = await api_env.create_org("jobpage")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    await _three_jobs(api_env, org_id, headers)

    default = await api_env.client.get("/api/v1/jobs", headers=headers)
    newest = await api_env.client.get("/api/v1/jobs?order=newest", headers=headers)
    assert [j["id"] for j in default.json()["items"]] == [
        j["id"] for j in newest.json()["items"]
    ]


async def test_a_page_is_a_page_of_the_ordering_and_the_total_is_the_whole(
    api_env,
) -> None:
    """The half B46 actually turns on: `limit`/`offset` must cut the ORDERED
    sequence, and `total` must keep counting past the cut. A page that
    silently truncates while the total agrees with it is the defect the
    closeout refused to sort."""
    org_id = await api_env.create_org("jobpage")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    first, second, third = await _three_jobs(api_env, org_id, headers)

    page = await api_env.client.get(
        "/api/v1/jobs?order=oldest&limit=2&offset=0", headers=headers
    )
    assert [j["id"] for j in page.json()["items"]] == [first, second]
    assert page.json()["total"] == 3

    tail = await api_env.client.get(
        "/api/v1/jobs?order=oldest&limit=2&offset=2", headers=headers
    )
    assert [j["id"] for j in tail.json()["items"]] == [third]
    assert tail.json()["total"] == 3


async def test_ordering_applies_after_the_filters_not_instead_of_them(api_env) -> None:
    """`order` composes with `status`; it does not replace the WHERE clause.
    Asserted because "sort the queue" and "sort what I filtered the queue to"
    are one careless `select(Job)` apart."""
    org_id = await api_env.create_org("jobpage")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    ids = await _three_jobs(api_env, org_id, headers)

    queued = await api_env.client.get(
        "/api/v1/jobs?order=oldest&status=queued", headers=headers
    )
    assert [j["id"] for j in queued.json()["items"]] == ids
    assert queued.json()["total"] == 3

    none_left = await api_env.client.get(
        "/api/v1/jobs?order=oldest&status=succeeded", headers=headers
    )
    assert none_left.json()["items"] == []
    assert none_left.json()["total"] == 0


async def test_an_order_this_route_cannot_honour_is_refused(api_env) -> None:
    """A typed refusal rather than a silent fallback to the default. A console
    that asked for `oldest` and got `newest` back, with a 200, would label the
    newest hundred "oldest first" — the exact deception the closeout declined
    to build client-side."""
    org_id = await api_env.create_org("jobpage")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)

    refused = await api_env.client.get("/api/v1/jobs?order=sideways", headers=headers)
    assert refused.status_code == 422
