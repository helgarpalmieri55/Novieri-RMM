"""Piece E: the account profile, and the email-staleness fix.

`docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §7,
falsifiers 11-14.

TWO CLAIMS, ONE FILE, BECAUSE THEY ARE THE SAME CLAIM SEEN FROM TWO SIDES.
`PATCH /api/v1/me` gives ITMS a writable `display_name`; since own
authentication phase 3 ITMS owns the address too: no token carries one, and
every door reads the row's. What §7.2 asked for -- the security decision and
the displayed fact read from ONE source -- holds by construction now, and the
tests below hold it over the MCP channel, the door that used to refresh from a
claim.

WHY THE ROUTE FORBIDS UNKNOWN FIELDS RATHER THAN IGNORING THEM. §7.3 says the
route "takes no `user_id`: the target is `ctx.user_id` from the validated
token, never the body". Ignoring an unrecognised key satisfies that too, but
only for the keys somebody remembered to think about: the day a `status`
column is added to the request model by accident, "ignored" becomes "written"
and nothing here would have said so. `extra="forbid"` makes falsifiers 11 and
12 structural -- the request is refused before any handler code runs -- and
`test_the_request_model_owns_exactly_four_fields` pins the model's shape so a
future field cannot be added without this file failing.
"""

from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import AuditEvent, User
from tests.mcp_support import MCP_BASE, marked_client, mcp_claims, mcp_token


async def _row(engine: AsyncEngine, sub: str) -> User:
    async with AsyncSession(engine) as session:
        return (
            await session.execute(select(User).where(User.external_subject == sub))
        ).scalar_one()


async def _audit(engine: AsyncEngine, org_id: UUID, action: str) -> list[AuditEvent]:
    async with AsyncSession(engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == org_id, AuditEvent.action == action)
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        )


async def _headers(env: Any, org_id: UUID, sub: str) -> dict[str, str]:
    """The console's session for the seeded user (`ApiEnv.headers`). It
    carries no claim -- the row's address is the only one."""
    return await env.headers(org_id, sub)


async def _mcp_me(env: Any, org_id: UUID, sub: str) -> httpx.Response:
    """`GET /api/v1/me` on the MCP channel with the platform's own token, which
    carries no address: the row's is the only one there is."""
    env.settings.public_console_url = MCP_BASE
    token = await mcp_token(env, await env.org_slug(org_id), sub)
    async with marked_client(env) as client:
        return await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})


# --------------------------------------------------------------------------- #
# GET /api/v1/me -- the profile is readable
# --------------------------------------------------------------------------- #


async def test_get_me_returns_the_profile_fields(api_env) -> None:
    org_id = await api_env.create_org("me")
    sub = await api_env.create_user(org_id, "technician")
    body = (
        await api_env.client.get("/api/v1/me", headers=await _headers(api_env, org_id, sub))
    ).json()

    assert body["phone"] is None
    assert body["timezone"] is None
    assert body["locale"] is None
    assert body["display_name"] == ""
    assert body["email"] == f"{sub}@test.local"


# --------------------------------------------------------------------------- #
# PATCH /api/v1/me -- what it writes
# --------------------------------------------------------------------------- #


async def test_patch_me_writes_the_four_owned_fields(api_env) -> None:
    org_id = await api_env.create_org("me")
    sub = await api_env.create_user(org_id, "technician")
    headers = await _headers(api_env, org_id, sub)

    response = await api_env.client.patch(
        "/api/v1/me",
        headers=headers,
        json={
            "display_name": "Ada Lovelace",
            "phone": "+44 20 7946 0958",
            "timezone": "Europe/London",
            "locale": "en-GB",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["display_name"] == "Ada Lovelace"
    assert body["phone"] == "+44 20 7946 0958"
    assert body["timezone"] == "Europe/London"
    assert body["locale"] == "en-GB"
    # The response is the whole of /me, permissions included, so the console
    # does not have to re-fetch to stay consistent.
    assert "devices.read" in body["permissions"]

    row = await _row(api_env.admin_engine, sub)
    assert (row.display_name, row.phone, row.timezone, row.locale) == (
        "Ada Lovelace",
        "+44 20 7946 0958",
        "Europe/London",
        "en-GB",
    )


async def test_patch_me_writes_only_the_supplied_fields(api_env) -> None:
    org_id = await api_env.create_org("me")
    sub = await api_env.create_user(org_id, "technician")
    headers = await _headers(api_env, org_id, sub)

    await api_env.client.patch(
        "/api/v1/me", headers=headers, json={"phone": "0101", "locale": "en-GB"}
    )
    await api_env.client.patch("/api/v1/me", headers=headers, json={"locale": "fr-FR"})

    row = await _row(api_env.admin_engine, sub)
    assert row.phone == "0101"
    assert row.locale == "fr-FR"


async def test_patch_me_clears_a_nullable_field(api_env) -> None:
    org_id = await api_env.create_org("me")
    sub = await api_env.create_user(org_id, "technician")
    headers = await _headers(api_env, org_id, sub)

    await api_env.client.patch("/api/v1/me", headers=headers, json={"phone": "0101"})
    response = await api_env.client.patch("/api/v1/me", headers=headers, json={"phone": None})
    assert response.status_code == 200
    assert response.json()["phone"] is None
    assert (await _row(api_env.admin_engine, sub)).phone is None


async def test_patch_me_refuses_a_null_display_name(api_env) -> None:
    """`users.display_name` is NOT NULL and defaults to ''. Sending null is the
    caller asking for a state the column cannot hold, which is a 400, not a
    silent write of ''."""
    org_id = await api_env.create_org("me")
    sub = await api_env.create_user(org_id, "technician")
    response = await api_env.client.patch(
        "/api/v1/me",
        headers=await _headers(api_env, org_id, sub),
        json={"display_name": None},
    )
    assert response.status_code == 400


async def test_patch_me_refuses_an_empty_body(api_env) -> None:
    org_id = await api_env.create_org("me")
    sub = await api_env.create_user(org_id, "technician")
    response = await api_env.client.patch(
        "/api/v1/me", headers=await _headers(api_env, org_id, sub), json={}
    )
    assert response.status_code == 400


async def test_patch_me_requires_a_token(api_env) -> None:
    response = await api_env.client.patch("/api/v1/me", json={"phone": "0101"})
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Falsifier 11 -- an id in the body must not redirect the write
# --------------------------------------------------------------------------- #


async def test_patch_me_refuses_a_user_id_in_the_body(api_env) -> None:
    org_id = await api_env.create_org("me")
    caller = await api_env.create_user(org_id, "technician")
    victim = await api_env.create_user(org_id, "org-admin")
    victim_row = await _row(api_env.admin_engine, victim)

    response = await api_env.client.patch(
        "/api/v1/me",
        headers=await _headers(api_env, org_id, caller),
        json={"user_id": str(victim_row.id), "display_name": "Owned"},
    )
    assert response.status_code == 422, response.text

    # Neither row moved -- not the victim's, and not the caller's either. A
    # rejected request writes nothing at all.
    assert (await _row(api_env.admin_engine, victim)).display_name == ""
    assert (await _row(api_env.admin_engine, caller)).display_name == ""


async def test_patch_me_writes_only_the_calling_users_row(api_env) -> None:
    """The positive half of falsifier 11: a legitimate write reaches exactly
    one row, identified by the token."""
    org_id = await api_env.create_org("me")
    caller = await api_env.create_user(org_id, "technician")
    other = await api_env.create_user(org_id, "technician")

    await api_env.client.patch(
        "/api/v1/me",
        headers=await _headers(api_env, org_id, caller),
        json={"display_name": "Only Me", "phone": "0101"},
    )

    assert (await _row(api_env.admin_engine, caller)).display_name == "Only Me"
    other_row = await _row(api_env.admin_engine, other)
    assert other_row.display_name == ""
    assert other_row.phone is None


# --------------------------------------------------------------------------- #
# Falsifier 12 -- nothing privilege-shaped is writable through a self-service form
# --------------------------------------------------------------------------- #


async def test_patch_me_refuses_privilege_shaped_fields(api_env) -> None:
    org_id = await api_env.create_org("me")
    sub = await api_env.create_user(org_id, "technician")
    headers = await _headers(api_env, org_id, sub)
    before = await _row(api_env.admin_engine, sub)

    forbidden: list[dict[str, Any]] = [
        {"email": "attacker@toeshee.com"},
        {"status": "active"},
        {"org_id": str(org_id)},
        {"external_subject": "someone-else"},
        {"role_ids": []},
        {"roles": ["org-admin"]},
        {"permissions": ["users.manage"]},
        {"display_name": "Fine", "status": "disabled"},
    ]
    refused: list[tuple[dict[str, Any], int]] = []
    for body in forbidden:
        response = await api_env.client.patch("/api/v1/me", headers=headers, json=body)
        if response.status_code != 422:
            refused.append((body, response.status_code))
    assert not refused, f"these were not refused: {refused}"

    after = await _row(api_env.admin_engine, sub)
    assert (after.email, after.status, after.org_id, after.external_subject) == (
        before.email,
        before.status,
        before.org_id,
        before.external_subject,
    )
    assert after.display_name == ""

    # And no role arrived by any of it.
    from app.models import UserRole

    async with AsyncSession(api_env.admin_engine) as session:
        roles = list(
            (
                await session.execute(select(UserRole).where(UserRole.user_id == before.id))
            ).scalars()
        )
    assert len(roles) == 1  # the `technician` seeded above, unchanged


def test_the_request_model_owns_exactly_four_fields() -> None:
    """A structural pin, not a behavioural one.

    `extra="forbid"` refuses what the model does not declare, so the model's
    field list IS the write surface. Adding `status` to it would make the
    behavioural test above pass -- the request would no longer be extra -- and
    this is the assertion that fails instead.
    """
    from app.api.v1.me import ProfileUpdate

    assert set(ProfileUpdate.model_fields) == {"display_name", "phone", "timezone", "locale"}
    assert ProfileUpdate.model_config.get("extra") == "forbid"


# --------------------------------------------------------------------------- #
# The profile write is audited
# --------------------------------------------------------------------------- #


async def test_patch_me_is_audited(api_env) -> None:
    org_id = await api_env.create_org("me")
    sub = await api_env.create_user(org_id, "technician")
    row = await _row(api_env.admin_engine, sub)

    await api_env.client.patch(
        "/api/v1/me",
        headers=await _headers(api_env, org_id, sub),
        json={"display_name": "Ada", "phone": "0101"},
    )

    events = await _audit(api_env.admin_engine, org_id, "user.profile_updated")
    assert len(events) == 1
    event = events[0]
    assert event.actor_type == "user"
    assert event.actor_id == row.id
    assert event.target_id == str(row.id)
    assert event.meta["fields"] == ["display_name", "phone"]
    # Keys, never values: a phone number is directory data about a person and
    # the audit log is read far more widely than the profile form.
    assert "0101" not in str(event.meta)


# --------------------------------------------------------------------------- #
# Falsifier 13 -- the address the door tests is the address the row holds
# --------------------------------------------------------------------------- #


async def test_an_address_changed_in_itms_is_the_one_mcp_sees(api_env) -> None:
    org_id = await api_env.create_org("stale")
    sub = await api_env.create_user(org_id, "technician")
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(
            update(User).where(User.external_subject == sub).values(email="ada.new@toeshee.com")
        )
        await s.commit()

    response = await _mcp_me(api_env, org_id, sub)
    assert response.status_code == 200
    assert response.json()["email"] == "ada.new@toeshee.com"
    assert (await _row(api_env.admin_engine, sub)).email == "ada.new@toeshee.com"


async def test_an_mcp_call_records_no_identity_change(api_env) -> None:
    org_id = await api_env.create_org("stale")
    sub = await api_env.create_user(org_id, "technician")

    await _mcp_me(api_env, org_id, sub)

    assert await _audit(api_env.admin_engine, org_id, "auth.email_changed") == []
    assert (await _row(api_env.admin_engine, sub)).email == f"{sub}@test.local"


async def test_repeated_mcp_calls_write_nothing(api_env) -> None:
    """Every request re-runs admission. A door that wrote on admission would
    append an audit event per HTTP call and fill the chain with noise."""
    org_id = await api_env.create_org("stale")
    sub = await api_env.create_user(org_id, "technician")

    for _ in range(3):
        await _mcp_me(api_env, org_id, sub)

    assert await _audit(api_env.admin_engine, org_id, "auth.email_changed") == []
    assert await _audit(api_env.admin_engine, org_id, "auth.login") == []


async def test_the_door_decides_on_the_address_the_row_holds(api_env) -> None:
    """Falsifier 13, the decision's side (§7.2: "The security decision and the
    displayed fact come from different sources" was the bug): the domain rule
    reads the address the row holds, the one `/me` shows."""
    org_id = await api_env.create_org("decide")
    sub = await api_env.create_user(org_id, "technician")

    async def set_address(address: str) -> None:
        async with AsyncSession(api_env.admin_engine) as s:
            await s.execute(
                update(User).where(User.external_subject == sub).values(email=address)
            )
            await s.commit()

    api_env.settings.console_email_domain = "toeshee.com"
    try:
        await set_address("ada@elsewhere.example")
        refused = await _mcp_me(api_env, org_id, sub)
        await set_address("ada@toeshee.com")
        admitted = await _mcp_me(api_env, org_id, sub)
    finally:
        api_env.settings.console_email_domain = None
    assert refused.status_code == 403
    assert "email domain not permitted" in refused.json()["detail"]
    assert (admitted.status_code, admitted.json()["email"]) == (200, "ada@toeshee.com")


# --------------------------------------------------------------------------- #
# Falsifier 14 -- display_name is NOT refreshed
# --------------------------------------------------------------------------- #


async def test_a_display_name_set_in_itms_survives_mcp_calls(api_env) -> None:
    org_id = await api_env.create_org("override")
    sub = await api_env.create_user(org_id, "technician")
    headers = await _headers(api_env, org_id, sub)

    await api_env.client.patch("/api/v1/me", headers=headers, json={"display_name": "Ada L."})
    body = (await _mcp_me(api_env, org_id, sub)).json()

    assert body["display_name"] == "Ada L."
    assert (await _row(api_env.admin_engine, sub)).display_name == "Ada L."
    assert body["email"] == f"{sub}@test.local"


async def test_an_mcp_token_carries_no_address_and_blanks_nothing(api_env) -> None:
    """DATA LOSS ON THE SHIPPED DEFAULT, as it was: a token with no address
    must never blank the stored one. The platform's token never carries one, so
    the property is now that the door does not read an address from a token."""
    org_id = await api_env.create_org("noclaim")
    sub = await api_env.create_user(org_id, "technician")
    stored = f"{sub}@test.local"
    api_env.settings.public_console_url = MCP_BASE
    token = await mcp_token(api_env, await api_env.org_slug(org_id), sub)
    assert "email" not in mcp_claims(token)

    response = await _mcp_me(api_env, org_id, sub)

    assert response.status_code == 200
    assert (await _row(api_env.admin_engine, sub)).email == stored
    assert await _audit(api_env.admin_engine, org_id, "auth.email_changed") == []


# --- the role names the chrome shows (UX remediation cycle 2) --------------


async def test_me_reports_the_callers_role_names(api_env) -> None:
    """THE CONSOLE WAS SHOWING A ROLE IT HAD INVENTED.

    `frontend/src/App.tsx` rendered `role: "Operator"` -- a constant -- under
    every user's name in the account chip, on every deployment, whoever was
    signed in. A super-admin was labelled "Operator" and so was a read-only
    auditor. That is not a cosmetic default: the chrome's job at that spot is
    to answer "who am I acting as" before somebody acts on a whole fleet, and a
    made-up answer is worse than none, because it is believed.

    `permissions` could not stand in for it -- ninety-two codes do not tell a
    human what hat they are wearing -- so the endpoint reports the names.
    """
    org = await api_env.create_org()
    sub = await api_env.create_user(org, "org-admin")

    body = (await api_env.client.get("/api/v1/me", headers=await api_env.headers(org, sub))).json()

    assert body["roles"] == ["org-admin"]


async def test_the_role_names_are_sorted_and_deduplicated(api_env) -> None:
    """Two roles carrying the same permission must not name that role twice,
    and the order must not depend on how the rows came back -- the chip renders
    whichever comes first, so an unstable order is a label that changes between
    page loads for no reason the user can see."""
    org = await api_env.create_org()
    sub = await api_env.create_user(org, "technician", "auditor")

    body = (await api_env.client.get("/api/v1/me", headers=await api_env.headers(org, sub))).json()

    assert body["roles"] == ["auditor", "technician"]


async def test_a_user_with_no_roles_reports_an_empty_list_not_a_null(api_env) -> None:
    """The console renders this directly. `null` reaching a `.length` is a
    blank chrome on an otherwise working session."""
    org = await api_env.create_org()
    sub = await api_env.create_user(org)

    body = (await api_env.client.get("/api/v1/me", headers=await api_env.headers(org, sub))).json()

    assert body["roles"] == []
