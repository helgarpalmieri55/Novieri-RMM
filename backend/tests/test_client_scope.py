from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope, parse_client_header
from app.core.tenancy import org_session, portal_session

A, B, C = uuid4(), uuid4(), uuid4()


async def _guc(s: AsyncSession, name: str) -> str | None:
    got = await s.execute(text("SELECT current_setting(:n, true)"), {"n": name})
    value: str | None = got.scalar()
    return value


def test_all_narrows_to_one_client() -> None:
    assert ClientScope.all().narrow(A) == ClientScope.only([A])


def test_narrowing_never_widens() -> None:
    assert ClientScope.only([A, B]).narrow(C) == ClientScope.only([])
    assert ClientScope.only([A, B]).narrow(A) == ClientScope.only([A])
    assert ClientScope.only([A]).narrow(None) == ClientScope.only([A])


def test_intersect_is_never_wider_than_either_side() -> None:
    assert ClientScope.all().intersect(ClientScope.all()) == ClientScope.all()
    assert ClientScope.all().intersect(ClientScope.only([A])) == ClientScope.only([A])
    assert ClientScope.only([A]).intersect(ClientScope.all()) == ClientScope.only([A])
    assert ClientScope.only([A, B]).intersect(ClientScope.only([B, C])) == ClientScope.only([B])
    assert ClientScope.only([A]).intersect(ClientScope.only([C])) == ClientScope.only([])
    # A narrowed-to-nothing scope stays nothing, whatever it meets.
    assert ClientScope.only([]).intersect(ClientScope.all()) == ClientScope.only([])


def test_is_empty() -> None:
    assert ClientScope.only([]).is_empty
    assert not ClientScope.only([A]).is_empty
    assert not ClientScope.all().is_empty


def test_allows() -> None:
    assert ClientScope.all().allows(C)
    assert ClientScope.only([A]).allows(A)
    assert not ClientScope.only([A]).allows(B)


@pytest.mark.parametrize("raw", ["", "not-a-uuid", "1,2"])
def test_a_malformed_header_is_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_client_header(raw)


def test_an_absent_header_is_none() -> None:
    assert parse_client_header(None) is None


async def test_org_session_pins_both_gucs(app_engine) -> None:
    org = uuid4()
    async with org_session(org, clients=ClientScope.only([A, B]), engine=app_engine) as s:
        scope = await _guc(s, "app.client_scope")
        ids = await _guc(s, "app.current_clients")
    assert scope == "list"
    assert ids is not None
    assert set(ids.split(",")) == {str(A), str(B)}


async def test_org_session_all(app_engine) -> None:
    async with org_session(uuid4(), clients=ClientScope.all(), engine=app_engine) as s:
        scope = await _guc(s, "app.client_scope")
    assert scope == "all"


async def test_an_empty_list_is_a_list_of_nothing_not_all(app_engine) -> None:
    async with org_session(uuid4(), clients=ClientScope.only([]), engine=app_engine) as s:
        scope = await _guc(s, "app.client_scope")
        ids = await _guc(s, "app.current_clients")
    assert (scope, ids) == ("list", "")


async def test_portal_session_pins_the_client_gucs_too(portal_engine) -> None:
    """The third pin (spec §2.3): `portal_session` sets the client scope as
    well as the org and requester GUCs."""
    org, requester = uuid4(), uuid4()
    async with portal_session(
        org, requester, clients=ClientScope.only([A]), engine=portal_engine
    ) as s:
        got = await s.execute(
            text(
                "SELECT current_setting('app.current_org', true), "
                "       current_setting('app.current_requester', true), "
                "       current_setting('app.client_scope', true), "
                "       current_setting('app.current_clients', true)"
            )
        )
        row = tuple(got.one())
    assert row == (str(org), str(requester), "list", str(A))
