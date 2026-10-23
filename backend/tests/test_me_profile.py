"""Piece E: the account profile, and the email-staleness fix.

`docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §7,
falsifiers 11-14.

TWO CLAIMS, ONE FILE, BECAUSE THEY ARE THE SAME CLAIM SEEN FROM TWO SIDES.
`PATCH /api/v1/me` gives ITMS a writable `display_name`; `app/api/deps.py`
refreshes `email` from the token claim on every admission. The asymmetry is
the design (§7.2): Keycloak is authoritative for the identity fact, ITMS for
the presentation preference. A test suite that checked only one of them would
pass on an implementation that refreshed both -- which would silently undo the
profile form on the next sign-in -- or on one that refreshed neither, which
leaves the security decision (`claims.email`, live) and the displayed fact
(`users.email`, stale) reading from different sources.

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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import AuditEvent, User


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


async def _headers(env: Any, org_id: UUID, sub: str, *, email: str | None = None) -> dict[str, str]:
    """Auth headers whose email claim MATCHES the seeded row by default.

    `ApiEnv.headers` issues the fake IdP's default address, which now differs
    from the row `create_user` seeds -- so every request through it would
    exercise the §7.2 refresh. Correct behaviour, but it would entangle the
    profile tests with the staleness tests; those below pass the address
    explicitly when the mismatch IS the subject.
    """
    slug = await env.org_slug(org_id)
    token = env.idp.token(sub, slug, email=email or f"{sub}@test.local")
    return {"Authorization": f"Bearer {token}"}


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
# Falsifier 13 -- a changed email claim reaches the row, and is audited
# --------------------------------------------------------------------------- #


async def test_a_changed_email_claim_updates_the_row(api_env) -> None:
    org_id = await api_env.create_org("stale")
    sub = await api_env.create_user(org_id, "technician")
    assert (await _row(api_env.admin_engine, sub)).email == f"{sub}@test.local"

    response = await api_env.client.get(
        "/api/v1/me",
        headers=await _headers(api_env, org_id, sub, email="ada.new@toeshee.com"),
    )
    assert response.status_code == 200
    assert response.json()["email"] == "ada.new@toeshee.com"
    assert (await _row(api_env.admin_engine, sub)).email == "ada.new@toeshee.com"


async def test_a_changed_email_claim_is_audited_with_both_values(api_env) -> None:
    org_id = await api_env.create_org("stale")
    sub = await api_env.create_user(org_id, "technician")
    old = f"{sub}@test.local"

    await api_env.client.get(
        "/api/v1/me", headers=await _headers(api_env, org_id, sub, email="ada.new@toeshee.com")
    )

    events = await _audit(api_env.admin_engine, org_id, "auth.email_changed")
    assert len(events) == 1
    assert events[0].meta["from"] == old
    assert events[0].meta["to"] == "ada.new@toeshee.com"
    assert events[0].target_id == str((await _row(api_env.admin_engine, sub)).id)


async def test_an_unchanged_email_claim_writes_nothing(api_env) -> None:
    """Every request re-runs admission. A refresh that fired on equality would
    append an audit event per HTTP call and fill the chain with noise."""
    org_id = await api_env.create_org("stale")
    sub = await api_env.create_user(org_id, "technician")
    headers = await _headers(api_env, org_id, sub, email="ada.new@toeshee.com")

    for _ in range(3):
        await api_env.client.get("/api/v1/me", headers=headers)

    events = await _audit(api_env.admin_engine, org_id, "auth.email_changed")
    assert len(events) == 1


# --------------------------------------------------------------------------- #
# Falsifier 14 -- display_name is NOT refreshed
# --------------------------------------------------------------------------- #


async def test_a_display_name_set_in_itms_survives_the_next_sign_in(api_env) -> None:
    org_id = await api_env.create_org("override")
    sub = await api_env.create_user(org_id, "technician")
    headers = await _headers(api_env, org_id, sub)

    await api_env.client.patch("/api/v1/me", headers=headers, json={"display_name": "Ada L."})

    # A later sign-in, with a token whose `name` claim says something else --
    # which is what Keycloak sends for everybody by default.
    later = await _headers(api_env, org_id, sub, email="ada.new@toeshee.com")
    body = (await api_env.client.get("/api/v1/me", headers=later)).json()

    assert body["display_name"] == "Ada L."
    assert (await _row(api_env.admin_engine, sub)).display_name == "Ada L."
    # ... while the email DID move. The asymmetry is the design.
    assert body["email"] == "ada.new@toeshee.com"


async def test_a_token_with_no_email_claim_does_not_blank_the_stored_address(
    api_env,
) -> None:
    """DATA LOSS ON THE SHIPPED DEFAULT, and no other test reaches it.

    `validate_token` yields `claims.get("email", "")`, so a token carrying no
    address presents one as the empty string. With `console_email_domain`
    unset -- which IS the default, and the state of every deployment that has
    not opted into the allowlist -- `domain_refusal` admits it. The refresh
    then compared `""` against the stored address, found them unequal, and
    overwrote a real address with nothing.

    AN ABSENT CLAIM IS NOT EVIDENCE THE ADDRESS WAS REMOVED. It is evidence
    that this particular token did not carry one, which happens for any IdP
    or client scope that omits `email`. The refresh is for propagating a
    CHANGE, and "" is not a change.

    What made it invisible: every other refresh test passes an explicit
    `email=`, because they are about an address that moved. The full backend
    suite is green with this bug present.
    """
    org_id = await api_env.create_org("noclaim")
    sub = await api_env.create_user(org_id, "technician")
    stored = f"{sub}@test.local"
    assert (await _row(api_env.admin_engine, sub)).email == stored

    # NOT via `_headers`: its `email or <default>` makes "" falsy and
    # substitutes the seeded address, so routing this through the helper
    # produces a test that passes without ever sending an empty claim. Minted
    # directly for that reason.
    slug = await api_env.org_slug(org_id)
    token = api_env.idp.token(sub, slug, email="")
    response = await api_env.client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert (await _row(api_env.admin_engine, sub)).email == stored, (
        "an absent email claim blanked the stored address"
    )
    assert await _audit(api_env.admin_engine, org_id, "auth.email_changed") == [], (
        "nothing changed, so nothing may be recorded as an identity change"
    )


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
