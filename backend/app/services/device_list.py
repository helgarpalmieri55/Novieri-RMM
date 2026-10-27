"""The fleet list: one page, its filters, and every count on the screen — in SQL.

**THIS EXISTS BECAUSE THE FLEET LIST STOPPED FITTING ON ONE PAGE.** `GET
/devices` served one capped page and the console computed the search, the
provenance filter and all five tab counts *in the browser, over that page*.
That was survivable while inventory grew by hand. Since the operator's ruling of
2026-08-26 a `net.sweep` records every host it finds, so a single /24 adds ~200
rows in one act and a /16 far more — and past the page limit the counts were
wrong, the search missed rows it should have found, and the provenance filter
answered "nobody vouched for any of these" about an estate full of swept rows.
Every one of those failures renders as a correct-looking empty answer.

# The counts, and which filters each one obeys

Three different questions are asked of the same table, and conflating any two of
them is how a tile comes to disagree with the table under it — a defect this
cycle has already produced twice:

* **`total`** — rows matching *every* filter, including the tab. It is the count
  of the thing the operator is looking at, and it is what paging is computed
  from.
* **`state_counts`** — what each tab *would* show from the current search and
  provenance filter, with the tab itself not applied. The tab strip needs this:
  a strip reading "All devices 40" above eleven rows is worse than no strip.
* **`fleet`** — the whole estate, no filters at all. The stat tiles are claims
  about the estate ("Managed endpoints"), and a tile that shrank as somebody
  typed in a search box would be a different and much worse defect.

**`total` IS TAKEN FROM `state_counts`, NOT COUNTED SEPARATELY.** They answer the
same question for the active tab, so computing them twice is an invitation for
them to differ. The tile and the table cannot disagree while one is defined as an
element of the other. This is the same discipline `patch/reporting.py`'s
`update_rollup` applies with `total` / `total_unfiltered` / `severity_counts`.

# Certificate attention is derived here, and it must match `cert_state`

`cert_state()` derives `ok | renewing | expiring | expired` from
`agents.cert_not_after` in Python; the console's "certificate attention" is
`revoked`, `expiring` or `expired`. Expressed in SQL, that is `cert_revoked` OR
`cert_not_after <= now + EXPIRING_REMAINING_DAYS`. The threshold is IMPORTED from
`cert_renewal` rather than written out, because a copied `36` here would keep
agreeing with the Python for exactly as long as nobody changed
`AGENT_CERT_TTL_DAYS` — and would then disagree silently, in the direction of
under-reporting machines about to fall off the fleet.

# Tenancy

**EVERY QUERY CARRIES `Device.org_id == org_id` EXPLICITLY** (invariant 5). RLS
says the same thing inside `org_session` — but `org_session` runs as a
NOBYPASSRLS role, so deleting the predicate turns no route test red. That is
exactly the position an aggregate must not be in: a missing predicate does not
raise here, it inflates a number on a screen with another tenant's machines.
`tests/test_devices_list.py` pins the predicate on the BYPASSRLS admin engine,
where Postgres has stopped filtering and the predicate is the only thing scoping
anything.

# Who a device is assigned to (operator decision 2026-09-23)

The People page left the menu and each device now carries its holder: the
`person` its `device_person` link names, whether an operator set it or
inventory resolved it from who signs in (`app/modules/people/service.py`
decides which; this only reads the result). It is resolved HERE, as two outer
joins on the page query, rather than per row -- a page of 100 is one statement,
not 101.

**BOTH JOINS CARRY `org_id == org_id` IN THEIR `ON` CLAUSE.** The foreign keys
are to `devices.id` and `person.id` alone, so a link row in this org that names
another org's person is a row Postgres accepts; without the predicate on the
`person` join, that person's name is this org's holder. Same reasoning as the
`WHERE` above, and `tests/test_devices_assigned_to.py` pins it on the BYPASSRLS
engine the same way.

**`with_holder` IS A DISCLOSURE SWITCH, NOT A PERFORMANCE ONE.** The list is
gated `devices.read`; a person's name is `people.read`. With it off, the holder
is not selected AND is not searched -- a search that matched on a name the row
then declined to show would answer the same question the null withheld.

**AND `people.read` IS HELD PER CLIENT** (plan 2026-10-26 Task 5). A
technician may read the devices of X and Y and the people of X alone; then
X's machines show their holder and Y's show null, and a search by the name of
Y's holder finds nothing. `holder_reader` makes that the person join's own
condition (`_reads_people_there`), so the holder, the search and the counts
agree by construction -- one predicate, in the one place each of them reads
the holder through -- and a page is still one statement, not one permission
check per client on it. There is no holder without a reader: `with_holder`
without `holder_reader` raises rather than reading every client's holders
(fails closed, Task 5 review).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Select, and_, exists, func, null, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Agent,
    Device,
    DevicePerson,
    Permission,
    Person,
    RolePermission,
    UserRole,
)
from app.modules.remote.capability import can_share_screen
from app.services.cert_renewal import EXPIRING_REMAINING_DAYS
from app.services.device_review import LEFT_BEHIND

__all__ = [
    "DEVICE_STATES",
    "DeviceHolder",
    "DevicePage",
    "FleetCounts",
    "StateCounts",
    "device_page",
]

#: The tab strip, as a vocabulary. `all` is not a filter; the other five are.
#:
#: `assets` is rows with NO agent — warehouse stock, network gear, anything a
#: sweep recorded — and it is deliberately NOT part of `offline`: offline is a
#: claim about something that should be answering, and a switch is not offline
#: (asset-inventory spec §5).
#:
#: `review` is a machine that joined through the install link and that nobody
#: has approved yet (install-link spec §1.5). It is ALSO in `online`/`offline`:
#: the tabs were never a partition (`certs` overlaps them too). It ALSO holds a
#: rejected machine whose agent may still be installed, until a technician
#: says they removed it by hand (`_left_behind`) -- and that row is in no
#: other tab.
DEVICE_STATES = ("all", "online", "offline", "assets", "certs", "review")


@dataclass(frozen=True)
class StateCounts:
    """What each tab would show FROM THE CURRENT SEARCH, tab not applied."""

    all: int
    online: int
    offline: int
    assets: int
    certs: int
    review: int

    def of(self, state: str) -> int:
        return {
            "all": self.all,
            "online": self.online,
            "offline": self.offline,
            "assets": self.assets,
            "certs": self.certs,
            "review": self.review,
        }[state]


@dataclass(frozen=True)
class FleetCounts:
    """The estate, unfiltered. The stat tiles are these numbers.

    `managed` is a JOIN and never a stored column: a device is managed exactly
    when an `agents` row exists beside it. Counting `devices` rows was right
    until a row could be warehouse stock, at which point "Managed endpoints"
    quietly means something else while reading the same (asset-inventory §9).
    """

    total: int
    managed: int
    online: int
    cert_attention: int


@dataclass(frozen=True)
class DeviceHolder:
    """Who a device is assigned to: the `person` its `device_person` link names.

    Only the id and the name -- enough for a table cell and a link. Anything
    else about the person is the People API's to serve, under its own gate.
    """

    id: UUID
    display_name: str


@dataclass(frozen=True)
class DevicePage:
    #: `(device, agent_status, cert_not_after, cert_revoked, screen_capable,
    #: holder)`, page-ordered. `holder` is None when nobody is assigned, and
    #: always None when the page was asked for `with_holder=False`.
    #: `screen_capable` is None when there is no agent row, else whether that
    #: agent can share its screen -- decided by `remote.capability`, the rule
    #: the start route refuses by.
    rows: list[
        tuple[
            Device,
            str | None,
            datetime | None,
            bool | None,
            bool | None,
            DeviceHolder | None,
        ]
    ]
    #: Rows matching EVERY filter including the tab. Always
    #: `state_counts.of(state)` -- see the module docstring.
    total: int
    state_counts: StateCounts
    fleet: FleetCounts


def _cert_attention(now: datetime) -> ColumnElement[bool]:
    """Certificates a human should look at: revoked, expiring, or expired.

    `renewing` is excluded deliberately -- it is the healthy automatic path, and
    surfacing it as "attention" trains operators to ignore the tab that also
    carries `expired`.

    A device with no `agents` row has no certificate and is never in this set;
    the outer join hands back NULL for every column here and NULL is not true.
    """
    cutoff = now + timedelta(days=EXPIRING_REMAINING_DAYS)
    return or_(
        Agent.cert_revoked.is_(True),
        and_(Agent.cert_not_after.is_not(None), Agent.cert_not_after <= cutoff),
    )


def _shown() -> ColumnElement[bool]:
    """Every row but a rejected one. A REJECTED ROW IS A TOMBSTONE, NOT A
    DEVICE (install-link plan D1): no device-delete path exists and ~35
    foreign keys point at `devices.id`, so Reject leaves the row and this is
    where it stops being shown -- every tab but `review`, every tab count and
    every fleet tile."""
    return Device.review_state != "rejected"


def _left_behind() -> ColumnElement[bool]:
    """A rejected row whose agent may still be on the computer, and that nobody
    has yet said they removed by hand. The ONE kind of tombstone that is
    listed, and only under `review`: hidden, it would mean no technician is
    ever told the agent is still installed."""
    return and_(
        Device.review_state == "rejected",
        Device.rejection_outcome.in_(sorted(LEFT_BEHIND)),
        Device.removal_acknowledged_at.is_(None),
    )


def _review() -> ColumnElement[bool]:
    """The *Waiting for review* tab: a machine nobody has approved yet, or a
    rejection that still needs someone to remove the agent by hand."""
    return or_(Device.review_state == "pending", _left_behind())


def _state_clause(state: str, now: datetime) -> ColumnElement[bool]:
    if state == "online":
        return and_(_shown(), Agent.status == "online")
    if state == "offline":
        return and_(_shown(), Agent.status == "offline")
    if state == "assets":
        # NO AGENT ROW, which is what `agent_status is null` means on the wire.
        return and_(_shown(), Agent.id.is_(None))
    if state == "certs":
        return and_(_shown(), _cert_attention(now))
    if state == "review":
        return _review()
    return _shown()


def _search_clause(term: str, *, with_holder: bool) -> ColumnElement[bool]:
    """The columns the console's search box says it searches, in SQL.

    `host(primary_ip)` rather than a text cast: asyncpg hands back an
    `ipaddress` object and a raw `inet::text` can carry a `/24` the operator
    never typed. `os_family || ' ' || os_version` is concatenated because that
    is what the table CELL shows -- searching for "windows 11" must match a row
    rendering "windows 11 Pro 23H2", which neither column matches alone.

    `serial` is absent because the box does not offer it; adding a column here
    silently widens what "no devices match" means.

    The holder's name is searched only `with_holder` -- see the module
    docstring: a match on a name the caller may not see discloses it.
    """
    needle = f"%{term}%"
    holder: list[ColumnElement[bool]] = (
        [Person.display_name.ilike(needle)] if with_holder else []
    )
    return or_(
        *holder,
        Device.hostname.ilike(needle),
        Device.fqdn.ilike(needle),
        Device.description.ilike(needle),
        func.host(Device.primary_ip).ilike(needle),
        (
            func.coalesce(Device.os_family, "")
            + " "
            + func.coalesce(Device.os_version, "")
        ).ilike(needle),
        Device.architecture.ilike(needle),
    )


def _filtered(
    org_id: UUID,
    *,
    os_family: str | None,
    device_status: str | None,
    provenance: str | None,
    q: str | None,
    with_holder: bool,
) -> list[ColumnElement[bool]]:
    """Every predicate EXCEPT the tab. `state_counts` is counted over this set.

    The `org_id` predicate leads, and it is written out rather than left to RLS:
    see the module docstring.

    Rejected rows are cut to the one kind any tab may list (`_left_behind`);
    WHICH tab lists it is `_state_clause`'s question, and its answer is
    `review` only.
    """
    clauses: list[ColumnElement[bool]] = [Device.org_id == org_id]
    clauses.append(or_(_shown(), _left_behind()))
    if os_family:
        clauses.append(Device.os_family == os_family)
    if device_status:
        clauses.append(Device.status == device_status)
    if provenance:
        clauses.append(Device.provenance == provenance)
    if q and q.strip():
        clauses.append(_search_clause(q.strip(), with_holder=with_holder))
    return clauses


#: Who holds a machine is a fact about a person: this code, in the person's
#: client (the device's: `fk_device_person_person_client`).
HOLDER_PERMISSION = "people.read"


def _reads_people_there(org_id: UUID, reader: UUID) -> ColumnElement[bool]:
    """`reader` holds `people.read` on every client or on the holder's own:
    `has_permission(..., client_id=person.client_id)` as a predicate
    correlated to the joined `Person`.

    Read straight from the grants (`user_roles`, G: no client RLS), the rows
    `rbac._grants` reads -- the question is a fact about one record's client,
    as in `notification/routing._sees_client`, not a scope handed to a
    request."""
    return (
        exists()
        .where(
            UserRole.org_id == org_id,
            UserRole.user_id == reader,
            RolePermission.role_id == UserRole.role_id,
            Permission.id == RolePermission.permission_id,
            Permission.code == HOLDER_PERMISSION,
            or_(UserRole.client_id.is_(None), UserRole.client_id == Person.client_id),
        )
        .correlate(Person)
    )


def _joined(*columns: Any, holder_for: tuple[UUID, UUID] | None = None) -> Select[Any]:
    """`devices` beside its agent and, given `holder_for = (org_id, reader)`,
    its holder -- only a holder `reader` may read. One argument, so the
    person join cannot be built without a reader.

    Every join is outer and at most one-to-one (`agents.device_id` is unique,
    `device_person.device_id` is the primary key), so no count is inflated by
    joining. The holder joins are scoped to `org_id` in their `ON` clause: see
    the module docstring for the row that predicate keeps out. The reader's
    permission is in the person join's `ON` clause too, so a holder they may
    not read joins as NULL: unnamed AND unsearched.
    """
    stmt = select(*columns).select_from(Device).join(Agent, isouter=True)
    if holder_for is not None:
        org_id, reader = holder_for
        stmt = stmt.outerjoin(
            DevicePerson,
            and_(DevicePerson.device_id == Device.id, DevicePerson.org_id == org_id),
        ).outerjoin(
            Person,
            and_(
                Person.id == DevicePerson.person_id,
                Person.org_id == org_id,
                _reads_people_there(org_id, reader),
            ),
        )
    return stmt


async def device_page(
    session: AsyncSession,
    org_id: UUID,
    *,
    os_family: str | None = None,
    device_status: str | None = None,
    provenance: str | None = None,
    q: str | None = None,
    state: str = "all",
    limit: int = 100,
    offset: int = 0,
    now: datetime | None = None,
    with_holder: bool = False,
    holder_reader: UUID | None = None,
) -> DevicePage:
    """One page of the fleet list, plus every count the screen renders.

    Three statements, not one per number: the page, one aggregate row for
    `state_counts`, one aggregate row for `fleet`. `count(*) FILTER (WHERE ...)`
    is what makes five tab counts a single scan rather than five.

    An unrecognised `state` falls back to `all` rather than raising: the tab is a
    view preference carried in a URL, and a stale bookmark should render the
    list.

    `with_holder` joins, selects and searches who each device is assigned to.
    The caller decides it from `people.read`; it defaults off so a new caller
    discloses nothing it did not ask to. `holder_reader` is the user the
    holders are read FOR -- `people.read` in each holder's client -- and it is
    required with `with_holder`: a call that names no reader raises, rather
    than reading every client's holders (fails closed).
    """
    holder_for: tuple[UUID, UUID] | None = None
    if with_holder:
        if holder_reader is None:
            raise ValueError(  # i18n-exempt: programmer error, unhandled 500
                "device_page(with_holder=True) needs holder_reader: the user the "
                "holders are read for"
            )
        holder_for = (org_id, holder_reader)
    stamp = now or datetime.now(UTC)
    if state not in DEVICE_STATES:
        state = "all"
    filters = _filtered(
        org_id,
        os_family=os_family,
        device_status=device_status,
        provenance=provenance,
        q=q,
        with_holder=with_holder,
    )
    attention = _cert_attention(stamp)

    # Each count is its tab's own clause, so a count and the rows its tab
    # selects cannot disagree -- including over which rejected rows are shown.
    counts_row = (
        await session.execute(
            _joined(
                func.count().filter(_state_clause("all", stamp)).label("all"),
                func.count().filter(_state_clause("online", stamp)).label("online"),
                func.count().filter(_state_clause("offline", stamp)).label("offline"),
                func.count().filter(_state_clause("assets", stamp)).label("assets"),
                func.count().filter(_state_clause("certs", stamp)).label("certs"),
                func.count().filter(_state_clause("review", stamp)).label("review"),
                # Joined when the holder is, because the search may name it.
                holder_for=holder_for,
            ).where(*filters)
        )
    ).one()
    state_counts = StateCounts(
        all=counts_row.all,
        online=counts_row.online,
        offline=counts_row.offline,
        assets=counts_row.assets,
        certs=counts_row.certs,
        review=counts_row.review,
    )

    fleet_row = (
        await session.execute(
            _joined(
                func.count().label("total"),
                func.count().filter(Agent.id.is_not(None)).label("managed"),
                func.count().filter(Agent.status == "online").label("online"),
                func.count().filter(attention).label("cert_attention"),
            ).where(Device.org_id == org_id, _shown())
        )
    ).one()
    fleet = FleetCounts(
        total=fleet_row.total,
        managed=fleet_row.managed,
        online=fleet_row.online,
        cert_attention=fleet_row.cert_attention,
    )

    page = _joined(
        Device,
        Agent.status,
        Agent.cert_not_after,
        Agent.cert_revoked,
        # The whole reported set, judged in Python by the start route's own
        # rule rather than re-expressed as a JSON predicate here.
        Agent.id,
        Agent.capabilities,
        # Two NULL columns when the holder is off, so every row has one shape.
        *((Person.id, Person.display_name) if with_holder else (null(), null())),
        holder_for=holder_for,
    ).where(*filters, _state_clause(state, stamp))
    rows = (
        await session.execute(
            # `Device.id` breaks the tie: `hostname` is nullable and duplicated
            # across a swept estate, and an unstable sort silently repeats and
            # drops rows across pages -- which reads as an inventory that
            # changes while you page through it.
            page.order_by(Device.hostname, Device.id).limit(limit).offset(offset)
        )
    ).all()

    return DevicePage(
        rows=[
            (
                device,
                status,
                not_after,
                revoked,
                can_share_screen(capabilities) if agent_id is not None else None,
                DeviceHolder(id=holder_id, display_name=holder_name)
                if holder_id is not None and holder_name is not None
                else None,
            )
            for (
                device,
                status,
                not_after,
                revoked,
                agent_id,
                capabilities,
                holder_id,
                holder_name,
            ) in rows
        ],
        # NOT counted again. See the module docstring: the tile and the table
        # cannot disagree while one is defined as an element of the other.
        total=state_counts.of(state),
        state_counts=state_counts,
        fleet=fleet,
    )
