"""The operator's own arrangement of the command centre.

The operator, 2026-09-01: "main dashboard should be customizable with tiles
also give me the ability to resize and order in whichever way i want."

UNGATED BEYOND AUTHENTICATION, ALL THREE VERBS, for exactly the reason
`app/api/v1/me.py` states for the profile PATCH: a permission code here would
be a code every role must hold, since this is how a person arranges their own
first screen. It is safe without one because of the same rule, which is the
whole of this module's security argument:

    THE TARGET IS `ctx.user_id`, FROM THE VALIDATED TOKEN, NEVER THE BODY.

There is no `user_id` parameter to tamper with on any of the three routes, so
a caller can read, write and delete exactly one row and it is their own.
`tests/test_route_gating.py::UNGATED_BY_DESIGN` records all three.

THE LAYOUT IS NOT A CAPABILITY, AND STORING ONE GRANTS NOTHING. A widget on a
dashboard renders from a module query that is permission-gated on its own
(`DashboardPage.tsx`: `enabled:` per query, and the server re-checks every
one). Saving a layout naming a widget the caller may not see does not make it
visible — the query behind it still answers 403 — so this surface cannot be
used to reach data. That is why the server does not need to know the widget
list at all, which is the next point.

WHAT IS VALIDATED IS SHAPE, NEVER MEMBERSHIP. The set of widgets is a property
of the console BUILD. A release that adds a tile must not need a server change,
and a release that removes one must not strand every stored row that names it.
So: a list of `{id, size}`, bounded in length, ids bounded in length and drawn
from a conservative character set, sizes from a closed set — and NO check that
an id is a widget anyone has heard of. The CLIENT resolves ids against the
widgets it actually has, ignoring what it does not recognise and appending what
the row does not mention. Both halves are load-bearing: without the bound here
anyone with a session could store an arbitrarily large document, and without
the tolerance there a stored row would blank somebody's dashboard on the first
release that touched the widget list.

NOT AUDITED, deliberately. Invariant 4 covers privileged action — job dispatch,
credential access, config and policy change. Where a person put a tile on their
own screen is none of those, and writing it to the hash-chained log would
dilute a record whose entire value is that everything in it matters.
"""

from typing import Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.api.deps import AuthContext, bound_auth_context
from app.core.tenancy import org_session
from app.models import DashboardLayout

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

#: Widgets one layout may name. Comfortably above the console's current count
#: (twelve) with room for several releases, and low enough that the stored
#: document stays a preference rather than a payload.
MAX_WIDGETS = 64

#: An id's shape. The console's own ids are short kebab-case slugs; the bound
#: exists so a caller cannot make the row large one id at a time, having been
#: stopped from making it long one entry at a time.
ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"


class WidgetPlacement(BaseModel):
    """One widget, where it sits and how wide it is.

    `size` is a NAME, not a column count. The console's grid is an
    implementation detail of the console — pinning "small" to a number here
    would put a CSS decision in the database, and a later change to the grid
    would have to migrate every stored row to keep the same screen.

    `hidden` is how a widget is removed from the dashboard WITHOUT being
    forgotten: the entry keeps its place and its size, so putting it back
    restores where it was rather than appending it to the end. A layout that
    dropped hidden widgets entirely would make "hide" and "reset this one"
    the same gesture.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN)
    size: Literal["small", "medium", "wide"] = "small"
    hidden: bool = False


class LayoutBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # `min_length=1`: an empty list is refused rather than stored, so "no row"
    # keeps its single meaning — this person has never arranged the dashboard.
    # Clearing a layout is DELETE, which is also what the console's "Reset to
    # default" calls.
    widgets: list[WidgetPlacement] = Field(min_length=1, max_length=MAX_WIDGETS)


class LayoutResponse(BaseModel):
    """`widgets` is `None` when this person has never arranged the dashboard.

    NOT `[]`. An empty list and "never customized" would render the same on a
    client that treats falsy as absent, and they mean opposite things: one is
    a dashboard the operator emptied, the other is the stock arrangement they
    have not touched. An empty list cannot be stored (`LayoutBody`), so `None`
    is the only absent value and it says exactly one thing.
    """

    widgets: list[WidgetPlacement] | None


@router.get("/layout", response_model=LayoutResponse)
async def get_layout(
    request: Request,
    ctx: AuthContext = Depends(bound_auth_context),
) -> LayoutResponse:
    """The caller's own dashboard arrangement, or `null` if they have none.

    `null` means this person has never arranged the dashboard — it is not the
    same as an empty one, which cannot be stored (see `LayoutBody`), so the
    absent value says exactly one thing.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        row = (
            await session.execute(
                select(DashboardLayout).where(
                    DashboardLayout.org_id == ctx.org_id,
                    DashboardLayout.user_id == ctx.user_id,
                )
            )
        ).scalar_one_or_none()
    if row is None:
        return LayoutResponse(widgets=None)
    # Re-validated on the way OUT as well as in. A row written by an older
    # release, or by a hand at the database, must not reach the console as a
    # shape it does not expect — and a `{id, size}` list is exactly the kind of
    # document that survives a schema change nobody remembered to apply to it.
    return LayoutResponse(
        widgets=[WidgetPlacement.model_validate(entry) for entry in row.layout]
    )


@router.put("/layout", response_model=LayoutResponse)
async def put_layout(
    body: LayoutBody,
    request: Request,
    ctx: AuthContext = Depends(bound_auth_context),
) -> LayoutResponse:
    """Replace the caller's own arrangement, in full.

    Last-write-wins over the WHOLE list rather than a patch of it: the document
    is small, its writer is one person, and a merge of two orderings has no
    meaning anyone could define. Returns what was stored.
    """
    widgets = [entry.model_dump() for entry in body.widgets]
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        # UPSERT on the composite primary key, not read-then-write. Two tabs
        # arranging the same dashboard is an ordinary thing to do, and the
        # read-then-write version of this loses one of them to a race for no
        # reason — the row has one writer's worth of meaning either way.
        await session.execute(
            insert(DashboardLayout)
            .values(org_id=ctx.org_id, user_id=ctx.user_id, layout=widgets)
            .on_conflict_do_update(
                index_elements=[DashboardLayout.org_id, DashboardLayout.user_id],
                set_={"layout": widgets},
            )
        )
    return LayoutResponse(widgets=body.widgets)


@router.delete("/layout", status_code=status.HTTP_204_NO_CONTENT)
async def reset_layout(
    request: Request,
    ctx: AuthContext = Depends(bound_auth_context),
) -> None:
    """Reset to the stock arrangement, by deleting the row.

    IDEMPOTENT AND SILENT ON A MISS. "Reset a dashboard I have never arranged"
    is a request that has already been satisfied, and answering 404 to it would
    make the console's Reset button fail for exactly the operators for whom it
    has the least to do.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        await session.execute(
            delete(DashboardLayout).where(
                DashboardLayout.org_id == ctx.org_id,
                DashboardLayout.user_id == ctx.user_id,
            )
        )
