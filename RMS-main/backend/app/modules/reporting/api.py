"""The reporting API -- ROUTES ARE GENERATED FROM THE REGISTRY IN A LOOP
(spec §6.1, §6.2, §0.10).

**THIS FILE IS THE MECHANISM BEHIND §0.10, AND THE LOOP IS THE WHOLE POINT.**
`docs/modules/ai-admin/spec.md` §0.3 defines "clean enough to be a tool" as five
mechanical properties, and the roadmap calls the AI persona "a forcing function
for clean, well-documented module APIs". Measured against this repo at HEAD,
across 173 `GET` routes under `/api/v1`: **zero** carry `summary=` on the
decorator, 76 `list[Model]` routes have no `limit` parameter at all, 19 have no
typed response model, and **zero** pass all five under the strict reading. The
forcing function had forced nothing.

Every other module has hand-written route functions and a review checklist. That
is what produced the table above. So reporting has NO ROUTE AUTHOR: `summary`
cannot be forgotten because the definition must supply it, the response model
cannot be untyped because it is built from `row_model`, the gate cannot be
omitted because the loop supplies it, and `limit` cannot be unbounded because the
params model declares `le=definition.max_limit`. A report that reaches the router
has the five properties because there is no code path by which it could not.

**TWO DEPENDENCIES, IN THIS ORDER, ON EVERY ROUTE (§0.5, §4.1).** Exactly one
kernel `require_permission("reporting.read")` -- so `route_permission` still
recovers exactly one code by reflection and every router-derived RBAC
completeness test covers reporting -- plus the separately-named
`require_report_sources(key)`, which performs the conjunction. Both complete
before the handler body, so no query runs for an unauthorized caller and a
timing difference cannot report on data they may not see.

**LITERAL PATHS, NOT A `/{key}` TEMPLATE.** §6.2's route table prints the export
path as `/{key}/export.csv`, but §6.1's loop generates one route per definition,
and the loop is what has to win. A templated path cannot carry a per-definition
`summary` (property 5), cannot bind `require_report_sources(key)` at route-build
time (the conjunction would have to be resolved inside the handler, after the
data session is a possibility), and would let an unknown key reach a handler at
all. `register()` refuses a key containing `/` or an empty key, so a generated
path can never be anything but one literal segment.

**ERROR TRANSLATION HAPPENS OUTSIDE THE `org_session` BLOCK.** `org_session`
wraps `session.begin()`, so the block commits when it exits NORMALLY. A
`ReportTimeout` caught INSIDE and re-raised outside would leave the block exiting
normally on an already-aborted transaction; caught outside, the exception carries
the rollback with it. Nothing here writes, so the stake is a confusing 500 rather
than a partial commit -- but the discipline is the module convention and this is
not the file to make an exception in.
"""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse

from app.api.deps import AuthContext, bound_auth_context, require_permission
from app.core.i18n import tr
from app.core.rbac import user_permission_codes
from app.core.tenancy import org_session

# IMPORTED FOR ITS SIDE EFFECT, AND THE SIDE EFFECT IS THE CATALOGUE. Importing
# the definitions package registers every definition and runs `verify_registry()`
# (see its docstring). The loop at the bottom of this file reads `REGISTRY` at
# IMPORT time, so without this the router would be built from an empty registry
# and mount nothing at all -- a module that registers cleanly and serves no
# report, which no test that only looks at reporting's own routes would notice.
from app.modules.reporting import definitions as _definitions  # noqa: F401
from app.modules.reporting.deps import require_report_sources
from app.modules.reporting.errors import ReportExportTooLarge, ReportTimeout
from app.modules.reporting.export import export_report
from app.modules.reporting.registry import REGISTRY, ReportDefinition, export_path
from app.modules.reporting.schema import ReportCatalogue, ReportPage
from app.modules.reporting.service import build_catalogue, execute_report

router = APIRouter(tags=["reports"])

# The prefix `register()` mounts this router under. Declared here so a generated
# export path is `export_path(key)` with that prefix removed rather than a second
# hand-written f-string: `_NEVER_A_TOOL` names the FULL paths, and a denylist
# that could drift from the router by one edit is not a tripwire.
_MOUNT_PREFIX = "/api/v1"


async def _catalogue(
    request: Request, ctx: AuthContext = Depends(bound_auth_context)
) -> ReportCatalogue:
    """`GET /api/v1/reports` -- every report, and whether THIS caller may run it.

    GATED ON `reporting.read` ALONE (§6.2), and deliberately carries no
    `require_report_sources`: it is the one route in this module that reads no
    org data, and refusing to tell a caller which codes they lack would defeat
    the only thing it is for. The session it opens is the RBAC read, nothing
    else -- `build_catalogue` takes a set of codes and returns platform data
    compiled into the image.
    """
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        # Org-wide grants only, `device_group_id` left at its `None` default --
        # the same question `require_report_sources` asks, so the catalogue can
        # never claim a report is runnable that the report itself would refuse
        # (§0.6).
        granted = await user_permission_codes(session, ctx.user_id)
    return build_catalogue(granted)


# DECLARED BEFORE THE GENERATION LOOP, and the order is load-bearing: the loop
# mounts `/reports/{key}` for every definition, and a definition key can never
# be empty or contain a slash (`register()` refuses both), so nothing it
# generates can shadow this path. Registering it after the loop would leave that
# resting on route-matching order instead of on a rule.
router.add_api_route(
    path="/reports",
    endpoint=_catalogue,
    methods=["GET"],
    response_model=ReportCatalogue,
    summary="Every report this deployment can run, and whether you may run each one.",
    description=(
        "The report catalogue, with `entitled` and `missing_permissions` computed for "
        "the calling user. Every report is listed, including the ones you may not run: "
        "`reporting.read` grants nothing on its own, so a report additionally requires "
        "the read permission of every module it draws from, and this route is what turns "
        "the resulting 403 into a request you can take to whoever grants those codes. It "
        "reads no organisation data -- the catalogue is compiled into the image."
    ),
    dependencies=[Depends(require_permission("reporting.read"))],
)


def _make_handler(definition: ReportDefinition) -> Callable[..., Any]:
    """One handler per definition, closed over it. A SHELL: it resolves the
    principal, opens the tenant session, calls the service, maps errors."""
    params_model = definition.params

    async def handler(
        request: Request,
        # `Annotated[Model, Query()]` -- NOT `Depends()`. With `Depends()`,
        # FastAPI builds the model from its declared fields and never sees the
        # rest of the query string, so `extra="forbid"` would have nothing to
        # forbid and a misspelled filter would return the unfiltered fleet with
        # a 200. With `Query()`, the whole query mapping is validated by the
        # model and an unknown parameter is a 422 (§6.5).
        params: Annotated[params_model, Query()],  # type: ignore[valid-type]
        ctx: AuthContext = Depends(bound_auth_context),
    ) -> ReportPage[Any]:
        try:
            async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
                return await execute_report(
                    session, definition, params, org_id=ctx.org_id
                )
        except ReportTimeout as exc:
            raise HTTPException(
                status.HTTP_504_GATEWAY_TIMEOUT,
                detail={
                    "error": "report_timeout",
                    "report_key": exc.report_key,
                    "message": tr(
                        "the report exceeded its statement timeout; narrow the window "
                        "or the filters and retry"
                    ),
                },
            ) from exc

    return handler


def _make_export_handler(definition: ReportDefinition) -> Callable[..., Any]:
    """One export handler per definition, closed over it.

    **AN EXPORT NEVER HAS A WEAKER GATE THAN THE REPORT IT EXPORTS.** It is
    generated by the same loop, from the same definition, carrying the same two
    dependencies -- so there is no route author who could gate the file more
    loosely than the page, which is the shape this mistake usually takes (the
    CSV is added later, by hand, "next to" the route it exports).
    """
    params_model = definition.params

    async def export_handler(
        request: Request,
        # Same `Annotated[Model, Query()]` as the report handler, for the same
        # reason: a misspelled filter must be a 422, not an unfiltered file.
        params: Annotated[params_model, Query()],  # type: ignore[valid-type]
        ctx: AuthContext = Depends(bound_auth_context),
    ) -> PlainTextResponse:
        try:
            async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
                body = await export_report(
                    session, definition, params, org_id=ctx.org_id, actor_id=ctx.user_id
                )
        except ReportExportTooLarge as exc:
            # 413, AND NO CSV BODY AT ALL (§0.8). A truncated file is
            # indistinguishable from a complete one once it is in a spreadsheet,
            # so the answer to "too many rows" is a refusal naming the ceiling
            # and how far over it the caller is -- never a shorter file.
            raise HTTPException(
                # `HTTP_413_CONTENT_TOO_LARGE`, not the deprecated
                # `HTTP_413_REQUEST_ENTITY_TOO_LARGE` spelling -- `app/core/http.py`
                # already uses this one, and the deprecated alias emits a
                # `StarletteDeprecationWarning` on every refused export.
                status.HTTP_413_CONTENT_TOO_LARGE,
                detail={
                    "error": "report_export_too_large",
                    "report_key": exc.report_key,
                    "cap": exc.cap,
                    "matched": exc.matched,
                    "message": tr(
                        "the export matched more rows than its cap; narrow the "
                        "parameters and retry -- the file is refused rather than "
                        "truncated, because a short CSV looks complete"
                    ),
                },
            ) from exc
        except ReportTimeout as exc:
            raise HTTPException(
                status.HTTP_504_GATEWAY_TIMEOUT,
                detail={
                    "error": "report_timeout",
                    "report_key": exc.report_key,
                    "message": tr(
                        "the report exceeded its statement timeout; narrow the window "
                        "or the filters and retry"
                    ),
                },
            ) from exc
        return PlainTextResponse(body, media_type="text/csv")

    return export_handler


def _generate_routes() -> None:
    """§6.1's loop. There is no per-report route function for anyone to write
    badly, and no argument below can be omitted for one report and supplied for
    another. Two routes per definition -- the report and its export -- so an
    export cannot be forgotten for one report or written differently for
    another."""
    for definition in REGISTRY.values():
        page_model: Any = ReportPage[definition.row_model]  # type: ignore[name-defined]
        router.add_api_route(
            path=f"/reports/{definition.key}",
            endpoint=_make_handler(definition),
            methods=["GET"],
            response_model=page_model,
            summary=definition.summary,
            description=definition.description,
            dependencies=[
                Depends(require_permission("reporting.read")),
                Depends(require_report_sources(definition.key)),
            ],
        )
        router.add_api_route(
            path=export_path(definition.key).removeprefix(_MOUNT_PREFIX),
            endpoint=_make_export_handler(definition),
            methods=["GET"],
            response_class=PlainTextResponse,
            summary=f"{definition.summary} As a CSV file.",
            description=(
                f"{definition.description}\n\n"
                "Returned as CSV, with the definition's declared export columns as its "
                "header row and in that order. The whole match is exported -- `limit` "
                "and `offset` page the JSON report and do not shrink this file. A match "
                f"over {definition.export_row_cap} rows is refused with a 413 naming the "
                "ceiling rather than truncated, because a short CSV is indistinguishable "
                "from a complete one once it is open in a spreadsheet. Exporting writes "
                "one `report.exported` audit event; running the report writes none."
            ),
            dependencies=[
                Depends(require_permission("reporting.read")),
                Depends(require_report_sources(definition.key)),
            ],
        )


_generate_routes()
