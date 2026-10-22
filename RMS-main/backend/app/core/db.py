"""Engine and session factories.

Two engines exist deliberately:
- the app engine (RLS-constrained `app_user`) — all request/gateway traffic;
- the admin engine — migrations and explicitly cross-org, audited paths only.
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_app_engine: AsyncEngine | None = None
_admin_engine: AsyncEngine | None = None
_portal_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    global _app_engine
    if _app_engine is None:
        _app_engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    return _app_engine


def get_admin_engine() -> AsyncEngine:
    global _admin_engine
    if _admin_engine is None:
        _admin_engine = create_async_engine(get_settings().database_admin_url, pool_pre_ping=True)
    return _admin_engine


def get_portal_engine() -> AsyncEngine:
    """The RLS-constrained, row-scoped role used ONLY by portal requests.

    A third engine rather than a reused one: portal_user's policies are keyed
    on a requester GUC that staff sessions never set, so the two connection
    pools must not be shared or a portal request could land on a staff
    connection.
    """
    global _portal_engine
    if _portal_engine is None:
        url = get_settings().database_portal_url
        if not url:
            # `database_portal_url` defaults to "" rather than being a required
            # setting, because ~20 pre-existing test modules construct Settings
            # directly and a required field would break all of them. The cost is
            # that a deployment which forgets ITS_DATABASE_PORTAL_URL cannot fail
            # at startup -- so it must fail HERE, on the first portal request,
            # with a message that names the missing variable. Without this guard
            # create_async_engine("") raises an opaque SQLAlchemy parse error
            # that gives an operator nothing to act on.
            raise RuntimeError(  # i18n-exempt: misconfiguration, logged; client gets a 500
                "ITS_DATABASE_PORTAL_URL is not set. The end-user portal needs "
                "its own RLS-constrained connection as the `portal_user` role "
                "(see deploy/postgres/init-app-user.sh); staff credentials must "
                "not be reused, because portal_user's row-scoped policies do not "
                "apply to app_user."
            )
        _portal_engine = create_async_engine(url, pool_pre_ping=True)
    return _portal_engine


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def dispose_engines() -> None:
    global _app_engine, _admin_engine, _portal_engine
    if _app_engine is not None:
        await _app_engine.dispose()
        _app_engine = None
    if _admin_engine is not None:
        await _admin_engine.dispose()
        _admin_engine = None
    if _portal_engine is not None:
        await _portal_engine.dispose()
        _portal_engine = None
