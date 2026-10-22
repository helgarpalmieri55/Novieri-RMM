"""Remote-control module registration (docs/modules/remote-control/spec.md §11).

register(app) is called by app.modules.register_all(): it registers the
remote job types, constructs the RemoteSessionService on app.state (holding
the frozen guacd path's in-process SessionRelay, the vault, the Redis relay
registry for screen sessions and the relay URL agents dial), and mounts the
module's API routes. The gateway process registers the job types too (see
app.gateway.main): results complete there.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.remote.jobtypes import register_job_type

    register_job_type()

    from app.modules.remote.relay import RelayRegistry, SessionRelay
    from app.modules.remote.service import RemoteSessionService

    # The service holds per-process state (viewer tickets, guacd params), so
    # it lives on app.state for the process lifetime.
    settings = app.state.settings
    app.state.remote_service = RemoteSessionService(
        app.state.job_engine,
        SessionRelay(),
        app.state.vault,
        relay_url=settings.gateway_relay_url,
        registry=RelayRegistry(app.state.redis),
        screen_max_duration_s=settings.remote_screen_max_duration_s,
    )

    from app.modules.remote.api import router

    app.include_router(router, prefix="/api/v1")
