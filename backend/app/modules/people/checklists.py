"""New starter and leaver checklists (docs/modules/people/spec.md §5).

A checklist records work; it performs none. Each step's check reads live
data at request time and is never stored, so a step ticks itself the moment
the owning module's data says the work is done.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import N_, tr, tr_marked
from app.models import (
    Device,
    DevicePerson,
    LicenseAllocation,
    Person,
    PersonChecklist,
    PersonChecklistStep,
    SeatAssignment,
    Ticket,
    User,
)
from app.modules.people.errors import PeopleConflictError, PeopleError, PeopleNotFoundError
from app.modules.people.service import _audit, load_person

# A ticket in any of these is not open (service_desk/lifecycle.py).
CLOSED_TICKET_STATUSES = ("closed", "cancelled", "rejected", "resolved", "fulfilled")


@dataclass
class PersonFacts:
    """Everything the checks read, gathered once per request."""

    account_status: str | None
    devices: list[tuple[UUID, str, UUID | None]] = field(default_factory=list)
    seats: int = 0
    licences: int = 0
    installs: int = 0
    open_tickets: int = 0
    unrotated_credentials: int = 0


Check = Callable[[PersonFacts], tuple[bool | None, str]]


@dataclass(frozen=True)
class StepDef:
    key: str
    title: str
    help: str
    link: str
    check: Check
    destructive: bool = False


def _pick(n: int, one: str, many: str) -> str:
    """The singular or the plural wording, each already translated by the caller:
    Spanish does not make a plural by adding an "s" to the singular."""
    return one if n == 1 else many


# Every check returns what it found as a sentence the console prints beside the
# step (`evidence`). It is computed on each request from live data and never
# stored, so it is worded in the request's language.


def _account_active(f: PersonFacts) -> tuple[bool | None, str]:
    if f.account_status is None:
        return False, tr("No sign-in yet")
    if f.account_status == "active":
        return True, tr("Their sign-in works")
    return False, tr("Their sign-in is turned off")


def _has_device(f: PersonFacts) -> tuple[bool | None, str]:
    n = len(f.devices)
    if not n:
        return False, tr("No computer yet")
    return True, _pick(n, tr("{count} computer", count=n), tr("{count} computers", count=n))


def _devices_grouped(f: PersonFacts) -> tuple[bool | None, str]:
    if not f.devices:
        return False, tr("No computer yet")
    missing = sum(1 for _, _, group in f.devices if group is None)
    if missing:
        return False, _pick(
            missing,
            tr("{count} computer is not in a group", count=missing),
            tr("{count} computers are not in a group", count=missing),
        )
    return True, tr("Every computer is in a group")


def _has_licences(f: PersonFacts) -> tuple[bool | None, str]:
    if not f.licences:
        return False, tr("No licences yet")
    return True, _pick(
        f.licences,
        tr("{count} licence", count=f.licences),
        tr("{count} licences", count=f.licences),
    )


def _has_seats(f: PersonFacts) -> tuple[bool | None, str]:
    if not f.seats:
        return False, tr("No subscriptions yet")
    return True, _pick(
        f.seats,
        tr("{count} subscription", count=f.seats),
        tr("{count} subscriptions", count=f.seats),
    )


def _baseline(f: PersonFacts) -> tuple[bool | None, str]:
    if f.installs:
        return None, _pick(
            f.installs,
            tr("{count} install succeeded on their computers", count=f.installs),
            tr("{count} installs succeeded on their computers", count=f.installs),
        )
    return None, tr("Nothing installed through ITMS yet")


def _access_off(f: PersonFacts) -> tuple[bool | None, str]:
    if f.account_status is None:
        return True, tr("They have no sign-in")
    if f.account_status == "active":
        return False, tr("Their sign-in still works")
    return True, tr("Their sign-in is turned off")


def _no_seats(f: PersonFacts) -> tuple[bool | None, str]:
    if f.seats:
        return False, _pick(
            f.seats,
            tr("Still holds {count} subscription", count=f.seats),
            tr("Still holds {count} subscriptions", count=f.seats),
        )
    return True, tr("Holds no subscriptions")


def _no_licences(f: PersonFacts) -> tuple[bool | None, str]:
    if f.licences:
        return False, _pick(
            f.licences,
            tr("Still holds {count} licence", count=f.licences),
            tr("Still holds {count} licences", count=f.licences),
        )
    return True, tr("Holds no licences")


def _devices_back(f: PersonFacts) -> tuple[bool | None, str]:
    in_use = sum(1 for _, lifecycle, _ in f.devices if lifecycle not in ("stock", "retired"))
    if in_use:
        return False, _pick(
            in_use,
            tr("{count} computer is still theirs and in use", count=in_use),
            tr("{count} computers are still theirs and in use", count=in_use),
        )
    return True, tr("No computer of theirs is in use")


def _credentials_changed(f: PersonFacts) -> tuple[bool | None, str]:
    if f.unrotated_credentials:
        return False, _pick(
            f.unrotated_credentials,
            tr(
                "{count} password they revealed not changed since",
                count=f.unrotated_credentials,
            ),
            tr(
                "{count} passwords they revealed not changed since",
                count=f.unrotated_credentials,
            ),
        )
    return True, tr("Nothing they revealed is still unchanged")


def _no_tickets(f: PersonFacts) -> tuple[bool | None, str]:
    if f.open_tickets:
        return False, _pick(
            f.open_tickets,
            tr("{count} ticket still open", count=f.open_tickets),
            tr("{count} tickets still open", count=f.open_tickets),
        )
    return True, tr("No open tickets")


# The step titles and help are `N_()` marked: they are defined once, here, and
# `checklist_view` says them in the request's language with `tr_marked`.
STEPS: dict[str, tuple[StepDef, ...]] = {
    "starter": (
        StepDef(
            "account",
            N_("Give them a sign-in"),
            N_("Create their account, or link the one they already have."),
            "/settings",
            _account_active,
        ),
        StepDef(
            "device",
            N_("Give them a computer"),
            N_("Link a computer to them. Inventory also does this when they sign in."),
            "/devices",
            _has_device,
        ),
        StepDef(
            "group",
            N_("Put their computer in the right group"),
            N_("Groups decide which patches, software and backups it gets."),
            "/device-groups",
            _devices_grouped,
        ),
        StepDef(
            "licences",
            N_("Assign their licences"),
            N_("Give them the licences they need."),
            "/licenses",
            _has_licences,
        ),
        StepDef(
            "seats",
            N_("Add them to the subscriptions they need"),
            N_("Give them a seat on each service they use."),
            "/subscriptions",
            _has_seats,
        ),
        StepDef(
            "baseline",
            N_("Install their standard software"),
            N_("Run your usual deployment on their computer, then tick this."),
            "/deploy",
            _baseline,
        ),
    ),
    "leaver": (
        StepDef(
            "access",
            N_("Turn off their sign-in"),
            N_("They can no longer use the console or the portal."),
            "/settings",
            _access_off,
            destructive=True,
        ),
        StepDef(
            "seats",
            N_("Take back their subscriptions"),
            N_("Release each seat so someone else can use it."),
            "/subscriptions",
            _no_seats,
            destructive=True,
        ),
        StepDef(
            "licences",
            N_("Take back their licences"),
            N_("Release each licence so it returns to the pool."),
            "/licenses",
            _no_licences,
            destructive=True,
        ),
        StepDef(
            "devices",
            N_("Collect or reassign their computers"),
            N_("Mark each computer as in stock or retired, or give it to someone else."),
            "/devices",
            _devices_back,
            destructive=True,
        ),
        StepDef(
            "credentials",
            N_("Change passwords they have seen"),
            N_("Change every stored password they revealed."),
            "/credentials",
            _credentials_changed,
            destructive=True,
        ),
        StepDef(
            "tickets",
            N_("Close their open tickets"),
            N_("Resolve or close anything they raised."),
            "/service-desk",
            _no_tickets,
        ),
    ),
}

PERMISSION_FOR_KIND = {"starter": "people.manage", "leaver": "people.offboard"}


def step_def(kind: str, key: str) -> StepDef:
    for step in STEPS[kind]:
        if step.key == key:
            return step
    raise PeopleNotFoundError(tr("no such step"))


async def gather_facts(session: AsyncSession, org_id: UUID, person: Person) -> PersonFacts:
    account_status = None
    if person.user_id is not None:
        account_status = (
            await session.execute(
                select(User.status).where(User.org_id == org_id, User.id == person.user_id)
            )
        ).scalar_one_or_none()
    devices = [
        (row[0], row[1], row[2])
        for row in (
            await session.execute(
                select(Device.id, Device.lifecycle, Device.device_group_id)
                .join(DevicePerson, DevicePerson.device_id == Device.id)
                .where(
                    Device.org_id == org_id,
                    DevicePerson.org_id == org_id,
                    DevicePerson.person_id == person.id,
                )
            )
        ).all()
    ]
    seats = (
        await session.execute(
            select(func.count()).select_from(SeatAssignment).where(
                SeatAssignment.org_id == org_id,
                SeatAssignment.person_id == person.id,
                SeatAssignment.released_at.is_(None),
            )
        )
    ).scalar_one()
    licences = (
        await session.execute(
            select(func.count()).select_from(LicenseAllocation).where(
                LicenseAllocation.org_id == org_id,
                LicenseAllocation.person_id == person.id,
                LicenseAllocation.released_at.is_(None),
            )
        )
    ).scalar_one()
    installs = 0
    if devices:
        installs = (
            await session.execute(
                text(
                    "SELECT count(*) FROM deployment_results WHERE org_id = :org"
                    " AND state = 'installed' AND device_id = ANY(:ids)"
                ),
                {"org": org_id, "ids": [d[0] for d in devices]},
            )
        ).scalar_one()
    open_tickets = 0
    unrotated = 0
    if person.user_id is not None:
        open_tickets = (
            await session.execute(
                select(func.count()).select_from(Ticket).where(
                    Ticket.org_id == org_id,
                    Ticket.requester_id == person.user_id,
                    Ticket.status.not_in(CLOSED_TICKET_STATUSES),
                )
            )
        ).scalar_one()
        unrotated = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FROM (
                        SELECT target_id, max(ts) AS last_seen FROM audit_events
                        WHERE org_id = :org AND actor_type = 'user' AND actor_id = :uid
                          AND action = 'credential.checked_out'
                        GROUP BY target_id
                    ) seen
                    WHERE NOT EXISTS (
                        SELECT 1 FROM audit_events later
                        WHERE later.org_id = :org AND later.target_type = 'credential'
                          AND later.target_id = seen.target_id
                          AND later.action IN ('credential.rotated', 'credential.deleted')
                          AND later.ts >= seen.last_seen
                    )
                    """
                ),
                {"org": org_id, "uid": person.user_id},
            )
        ).scalar_one()
    return PersonFacts(
        account_status=account_status,
        devices=devices,
        seats=int(seats),
        licences=int(licences),
        installs=int(installs),
        open_tickets=int(open_tickets),
        unrotated_credentials=int(unrotated),
    )


def _effective(state: str, auto: bool | None) -> str:
    if state == "skipped":
        return "skipped"
    if state == "done" or auto is True:
        return "done"
    return "pending"


async def _steps(
    session: AsyncSession, org_id: UUID, checklist_id: UUID
) -> dict[str, PersonChecklistStep]:
    rows = (
        await session.execute(
            select(PersonChecklistStep).where(
                PersonChecklistStep.org_id == org_id,
                PersonChecklistStep.checklist_id == checklist_id,
            )
        )
    ).scalars()
    return {row.key: row for row in rows}


async def checklist_view(
    session: AsyncSession, org_id: UUID, checklist: PersonChecklist, facts: PersonFacts
) -> dict[str, Any]:
    stored = await _steps(session, org_id, checklist.id)
    steps: list[dict[str, Any]] = []
    for position, definition in enumerate(STEPS[checklist.kind]):
        row = stored.get(definition.key)
        auto, evidence = definition.check(facts)
        state = row.state if row is not None else "pending"
        steps.append(
            {
                "key": definition.key,
                "position": position,
                "title": tr_marked(definition.title),
                "help": tr_marked(definition.help),
                "link": definition.link,
                "destructive": definition.destructive,
                "checked": auto,
                "evidence": evidence,
                "marked": state,
                "state": _effective(state, auto),
                "note": row.note if row is not None else "",
                "updated_at": row.updated_at if row is not None else None,
            }
        )
    return {
        "id": checklist.id,
        "kind": checklist.kind,
        "status": checklist.status,
        "started_at": checklist.started_at,
        "closed_at": checklist.closed_at,
        "steps": steps,
        "can_complete": checklist.status == "open"
        and all(step["state"] != "pending" for step in steps),
    }


async def load_checklist(
    session: AsyncSession, org_id: UUID, person_id: UUID, checklist_id: UUID
) -> PersonChecklist:
    row = (
        await session.execute(
            select(PersonChecklist).where(
                PersonChecklist.org_id == org_id,
                PersonChecklist.person_id == person_id,
                PersonChecklist.id == checklist_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise PeopleNotFoundError(tr("checklist not found"))
    return row


async def open_checklist(
    session: AsyncSession, org_id: UUID, person_id: UUID
) -> PersonChecklist | None:
    return (
        await session.execute(
            select(PersonChecklist).where(
                and_(
                    PersonChecklist.org_id == org_id,
                    PersonChecklist.person_id == person_id,
                    PersonChecklist.status == "open",
                )
            )
        )
    ).scalar_one_or_none()


async def start_checklist(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, person_id: UUID, kind: str
) -> PersonChecklist:
    if kind not in STEPS:
        raise PeopleError(tr("unknown checklist"))
    person = await load_person(session, org_id, person_id)
    if await open_checklist(session, org_id, person_id) is not None:
        raise PeopleConflictError(tr("finish or cancel the open checklist first"))
    if kind == "leaver" and person.status == "left":
        raise PeopleError(tr("this person has already left"))
    checklist = PersonChecklist(
        org_id=org_id, person_id=person.id, kind=kind, status="open", started_by=actor_id
    )
    session.add(checklist)
    await session.flush()
    for position, definition in enumerate(STEPS[kind]):
        session.add(
            PersonChecklistStep(
                org_id=org_id,
                checklist_id=checklist.id,
                key=definition.key,
                position=position,
                state="pending",
            )
        )
    person.status = "starting" if kind == "starter" else "leaving"
    await session.flush()
    await _audit(
        session,
        org_id=org_id,
        actor_id=actor_id,
        action="people.checklist_started",
        target_type="person",
        target_id=person.id,
        client_id=person.client_id,
        metadata={"checklist_id": str(checklist.id), "kind": kind},
    )
    return checklist


async def update_step(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    person_id: UUID,
    checklist_id: UUID,
    key: str,
    state: str,
    note: str,
) -> PersonChecklistStep:
    checklist = await load_checklist(session, org_id, person_id, checklist_id)
    if checklist.status != "open":
        raise PeopleError(tr("this checklist is closed"))
    definition = step_def(checklist.kind, key)
    note = note.strip()
    if state == "done":
        person = await load_person(session, org_id, person_id)
        auto, evidence = definition.check(await gather_facts(session, org_id, person))
        if auto is False and not note:
            raise PeopleError(
                tr(
                    "{evidence}. Add a note saying why this is done.",
                    evidence=evidence,
                )
            )
    if state == "skipped" and not note:
        raise PeopleError(tr("Add a note saying why this step is skipped."))
    row = (await _steps(session, org_id, checklist.id)).get(key)
    if row is None:
        raise PeopleNotFoundError(tr("no such step"))
    previous = row.state
    row.state = state
    row.note = note[:500]
    row.updated_by = actor_id
    row.updated_at = datetime.now(UTC)
    await session.flush()
    await _audit(
        session,
        org_id=org_id,
        actor_id=actor_id,
        action="people.checklist_step_updated",
        target_type="person",
        target_id=person_id,
        # The checklist's client is its person's (H, migration 0235).
        client_id=checklist.client_id,
        metadata={
            "checklist_id": str(checklist.id),
            "kind": checklist.kind,
            "step": key,
            "from": previous,
            "to": state,
            "note": row.note,
        },
    )
    return row


async def close_checklist(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    person_id: UUID,
    checklist_id: UUID,
    outcome: str,
) -> PersonChecklist:
    checklist = await load_checklist(session, org_id, person_id, checklist_id)
    if checklist.status != "open":
        raise PeopleError(tr("this checklist is closed"))
    person = await load_person(session, org_id, person_id)
    if outcome == "completed":
        view = await checklist_view(
            session, org_id, checklist, await gather_facts(session, org_id, person)
        )
        pending = [s["title"] for s in view["steps"] if s["state"] == "pending"]
        if pending:
            raise PeopleError(tr("Not finished yet: {titles}", titles="; ".join(pending)))
        if checklist.kind == "leaver":
            person.status = "left"
            if person.end_date is None:
                person.end_date = date.today()
        else:
            person.status = "active"
    else:
        person.status = "active"
    checklist.status = outcome
    checklist.closed_by = actor_id
    checklist.closed_at = datetime.now(UTC)
    await session.flush()
    await _audit(
        session,
        org_id=org_id,
        actor_id=actor_id,
        action=f"people.checklist_{outcome}",
        target_type="person",
        target_id=person.id,
        client_id=person.client_id,
        metadata={"checklist_id": str(checklist.id), "kind": checklist.kind},
    )
    return checklist
