"""People REST API (docs/modules/people/spec.md §3).

Every route is gated server-side. Leaver checklists additionally need
`people.offboard`, checked in the handler because the checklist's kind is
only known once it is loaded. Static paths are declared before
`/people/{person_id}` so they are never parsed as an id.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_permission
from app.core.i18n import tr
from app.core.rbac import has_permission
from app.core.tenancy import org_session
from app.models import DevicePerson, Person
from app.modules.people import checklists, service
from app.modules.people.detail import person_detail
from app.modules.people.errors import PeopleConflictError, PeopleError, PeopleNotFoundError
from app.modules.people.schema import (
    AccountLink,
    ChecklistOut,
    ChecklistStart,
    DevicePersonOut,
    DevicePersonSet,
    LoginCreate,
    LoginOut,
    PersonCreate,
    PersonDetailOut,
    PersonListRow,
    PersonOut,
    PersonStatus,
    PersonUpdate,
    SeatLink,
    StepUpdate,
    UnmatchedOut,
)

router = APIRouter(prefix="/people", tags=["people"])


def _http(exc: PeopleError) -> HTTPException:
    if isinstance(exc, PeopleNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, PeopleConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


def _person_out(person: Person) -> PersonOut:
    return PersonOut.model_validate(person, from_attributes=True)


def _device_out(
    device_id: UUID, link: DevicePerson | None, person: Person | None
) -> DevicePersonOut:
    return DevicePersonOut(
        device_id=device_id,
        person=_person_out(person) if person is not None else None,
        source=link.source if link is not None else None,
        observed_login=link.observed_login if link is not None else None,
        observed_at=link.observed_at if link is not None else None,
    )


async def _require_offboard(session: AsyncSession, ctx: AuthContext) -> None:
    if not await has_permission(session, ctx.user_id, "people.offboard"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, tr("requires people.offboard"))


async def _checklist_out(
    session: AsyncSession, org_id: UUID, person_id: UUID, checklist_id: UUID
) -> ChecklistOut:
    person = await service.load_person(session, org_id, person_id)
    row = await checklists.load_checklist(session, org_id, person_id, checklist_id)
    facts = await checklists.gather_facts(session, org_id, person)
    view: dict[str, Any] = await checklists.checklist_view(session, org_id, row, facts)
    return ChecklistOut.model_validate(view)


# --------------------------------------------------------------------------- #
# Static paths first
# --------------------------------------------------------------------------- #


@router.get("", response_model=list[PersonListRow])
async def list_people(
    request: Request,
    q: str | None = Query(default=None, max_length=200),
    person_status: PersonStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=500, ge=1, le=2000),
    ctx: AuthContext = Depends(require_permission("people.read")),
) -> list[PersonListRow]:
    """Everyone who works here, by name, with how many computers each uses.
    Accounts that have no person yet get one first."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        await service.sync_accounts(session, ctx.org_id)
        rows = await service.list_people(
            session, ctx.org_id, q=q, status=person_status, limit=limit
        )
        return [
            PersonListRow(**_person_out(p).model_dump(), device_count=count) for p, count in rows
        ]


@router.post("", response_model=PersonOut, status_code=status.HTTP_201_CREATED)
async def create_person(
    body: PersonCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("people.manage")),
) -> PersonOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            person = await service.create_person(
                session, org_id=ctx.org_id, actor_id=ctx.user_id, **body.model_dump()
            )
        except PeopleError as exc:
            raise _http(exc) from exc
        return _person_out(person)


@router.get("/unmatched", response_model=UnmatchedOut)
async def unmatched(
    request: Request,
    ctx: AuthContext = Depends(require_permission("people.read")),
) -> UnmatchedOut:
    """Seats, computers and licences that are not linked to anyone yet."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        return UnmatchedOut.model_validate(await service.unmatched(session, ctx.org_id))


@router.post("/unmatched/seats/{seat_id}/link", response_model=PersonOut)
async def link_seat(
    seat_id: UUID,
    body: SeatLink,
    request: Request,
    ctx: AuthContext = Depends(require_permission("people.manage")),
) -> PersonOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            seat = await service.link_seat(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                seat_id=seat_id,
                person_id=body.person_id,
            )
            assert seat.person_id is not None
            return _person_out(await service.load_person(session, ctx.org_id, seat.person_id))
        except PeopleError as exc:
            raise _http(exc) from exc


@router.get("/by-user/{user_id}", response_model=PersonOut)
async def person_for_user(
    user_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("people.read")),
) -> PersonOut:
    """The person behind a console or portal account."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        await service.sync_accounts(session, ctx.org_id)
        person = await service.person_for_user(session, ctx.org_id, user_id)
        if person is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("person not found"))
        return _person_out(person)


@router.get("/devices/{device_id}", response_model=DevicePersonOut)
async def device_person(
    device_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("people.read")),
) -> DevicePersonOut:
    """Who uses this computer, and whether inventory or a person said so."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            link, person = await service.device_link(session, ctx.org_id, device_id)
        except PeopleError as exc:
            raise _http(exc) from exc
        return _device_out(device_id, link, person)


@router.put("/devices/{device_id}", response_model=DevicePersonOut)
async def set_device_person(
    device_id: UUID,
    body: DevicePersonSet,
    request: Request,
    ctx: AuthContext = Depends(require_permission("people.manage")),
) -> DevicePersonOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await service.set_device_person(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                device_id=device_id,
                person_id=body.person_id,
            )
            link, person = await service.device_link(session, ctx.org_id, device_id)
        except PeopleError as exc:
            raise _http(exc) from exc
        return _device_out(device_id, link, person)


@router.delete("/devices/{device_id}/override", response_model=DevicePersonOut)
async def clear_device_override(
    device_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("people.manage")),
) -> DevicePersonOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await service.clear_device_override(
                session, org_id=ctx.org_id, actor_id=ctx.user_id, device_id=device_id
            )
            link, person = await service.device_link(session, ctx.org_id, device_id)
        except PeopleError as exc:
            raise _http(exc) from exc
        return _device_out(device_id, link, person)


# --------------------------------------------------------------------------- #
# One person
# --------------------------------------------------------------------------- #


@router.get("/{person_id}", response_model=PersonDetailOut)
async def get_person(
    person_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("people.read")),
) -> PersonDetailOut:
    """Everything one person holds: computers, licences, subscriptions,
    open tickets, sign-in and the open checklist."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            detail = await person_detail(
                session, org_id=ctx.org_id, viewer_id=ctx.user_id, person_id=person_id
            )
        except PeopleError as exc:
            raise _http(exc) from exc
        detail["person"] = _person_out(detail["person"])
        detail["logins"] = [
            LoginOut.model_validate(row, from_attributes=True) for row in detail["logins"]
        ]
        return PersonDetailOut.model_validate(detail)


@router.patch("/{person_id}", response_model=PersonOut)
async def update_person(
    person_id: UUID,
    body: PersonUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("people.manage")),
) -> PersonOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            person = await service.update_person(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                person_id=person_id,
                changes=body.model_dump(exclude_unset=True),
            )
        except PeopleError as exc:
            raise _http(exc) from exc
        return _person_out(person)


@router.put("/{person_id}/account", response_model=PersonOut)
async def link_account(
    person_id: UUID,
    body: AccountLink,
    request: Request,
    ctx: AuthContext = Depends(require_permission("people.manage")),
) -> PersonOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            person = await service.link_account(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                person_id=person_id,
                user_id=body.user_id,
            )
        except PeopleError as exc:
            raise _http(exc) from exc
        return _person_out(person)


@router.post("/{person_id}/logins", response_model=LoginOut, status_code=status.HTTP_201_CREATED)
async def add_login(
    person_id: UUID,
    body: LoginCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("people.manage")),
) -> LoginOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            row = await service.add_login(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                person_id=person_id,
                login=body.login,
            )
        except PeopleError as exc:
            raise _http(exc) from exc
        return LoginOut.model_validate(row, from_attributes=True)


@router.delete("/{person_id}/logins/{login_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_login(
    person_id: UUID,
    login_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("people.manage")),
) -> Response:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await service.remove_login(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                person_id=person_id,
                login_id=login_id,
            )
        except PeopleError as exc:
            raise _http(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Checklists
# --------------------------------------------------------------------------- #


@router.post(
    "/{person_id}/checklists", response_model=ChecklistOut, status_code=status.HTTP_201_CREATED
)
async def start_checklist(
    person_id: UUID,
    body: ChecklistStart,
    request: Request,
    ctx: AuthContext = Depends(require_permission("people.manage")),
) -> ChecklistOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        if body.kind == "leaver":
            await _require_offboard(session, ctx)
        try:
            row = await checklists.start_checklist(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                person_id=person_id,
                kind=body.kind,
            )
            return await _checklist_out(session, ctx.org_id, person_id, row.id)
        except PeopleError as exc:
            raise _http(exc) from exc


@router.patch("/{person_id}/checklists/{checklist_id}/steps/{key}", response_model=ChecklistOut)
async def update_step(
    person_id: UUID,
    checklist_id: UUID,
    key: str,
    body: StepUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("people.manage")),
) -> ChecklistOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            row = await checklists.load_checklist(session, ctx.org_id, person_id, checklist_id)
            if row.kind == "leaver":
                await _require_offboard(session, ctx)
            await checklists.update_step(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                person_id=person_id,
                checklist_id=checklist_id,
                key=key,
                state=body.state,
                note=body.note,
            )
            return await _checklist_out(session, ctx.org_id, person_id, checklist_id)
        except PeopleError as exc:
            raise _http(exc) from exc


async def _close(
    request: Request, ctx: AuthContext, person_id: UUID, checklist_id: UUID, outcome: str
) -> ChecklistOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            row = await checklists.load_checklist(session, ctx.org_id, person_id, checklist_id)
            if row.kind == "leaver":
                await _require_offboard(session, ctx)
            await checklists.close_checklist(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                person_id=person_id,
                checklist_id=checklist_id,
                outcome=outcome,
            )
            return await _checklist_out(session, ctx.org_id, person_id, checklist_id)
        except PeopleError as exc:
            raise _http(exc) from exc


@router.post("/{person_id}/checklists/{checklist_id}/complete", response_model=ChecklistOut)
async def complete_checklist(
    person_id: UUID,
    checklist_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("people.manage")),
) -> ChecklistOut:
    return await _close(request, ctx, person_id, checklist_id, "completed")


@router.post("/{person_id}/checklists/{checklist_id}/cancel", response_model=ChecklistOut)
async def cancel_checklist(
    person_id: UUID,
    checklist_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("people.manage")),
) -> ChecklistOut:
    return await _close(request, ctx, person_id, checklist_id, "cancelled")
