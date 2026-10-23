"""The console email-domain allowlist as an ORG SETTING rather than an env var.

`docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §2.2.

WHAT CHANGED AND WHY IT IS NOT COSMETIC. The allowlist used to be
`ITS_CONSOLE_EMAIL_DOMAIN` alone: changing it meant an SSH session, an edit and
a restart, and a wrong value refused every operator at all three doors with no
way back through the UI -- which is why `scripts/preflight_email_domain.py`
exists at all. That script's whole job is to tell the operator, over SSH,
whom they are about to lock out. Both halves fail the buyer test.

So the value moves into `organizations.settings["console_email_domain"]`, and
the two SSH-shaped things are replaced by structure:

  * the SAVE PATH REFUSES a value that would refuse the caller's own address,
    so the self-lockout is impossible rather than warned about; and
  * the preview endpoint answers the preflight's question -- "who would this
    refuse" -- inside the product, scoped to the caller's own tenant.

THE ENV VAR IS NOT DELETED, AND THE REASON IS A SECURITY ONE RATHER THAN
CAUTION. It becomes the PLATFORM DEFAULT for an org that has not set its own
value. Migration 0067 backfills existing orgs from it, but an org created
AFTER the upgrade would have no key at all, and reading that as "unrestricted"
would mean a multi-tenant deployment silently loses the allowlist for every
new tenant. The plan's §2.2 does not address that case; this does, and it is
recorded here rather than in a commit message. The demotion the plan asks for
is real all the same: the env var can no longer be the thing an operator has
to SSH in to change, because the org setting overrides it from the console.

THE OVERRIDE IS THE BREAK-GLASS. `ITS_CONSOLE_EMAIL_DOMAIN_OVERRIDE=off`
disables the gate everywhere, for the one case the save guard cannot prevent:
a setting written before this shipped, or through the database, that admits
nobody. Anything OTHER than `off` is ignored and the gate stays on -- an
operator who types the domain into the override variable gets the gate, not a
silently disabled one.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.identity.email_domain import domain_refusal, is_allowed_domain
from app.core.identity.policy import (
    CONSOLE_DOMAIN_KEY,
    OVERRIDE_ENV_VAR,
    RefusedUser,
    effective_domain,
    gate_disabled_by_override,
    resolve_console_domain,
    self_lockout_refusal,
    users_refused_by,
)
from app.core.tenancy import org_session
from app.models import Organization, User

BASE_DOMAIN = "toeshee.com"


def _unique(local: str, domain: str) -> str:
    """A seeded address nothing else in the run can also hold."""
    return f"{local}-{uuid4().hex[:10]}@{domain}"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


async def _set_org_domain(api_env: Any, org_id: UUID, value: str | None) -> None:
    """Write the org setting directly, on the BYPASSRLS admin engine.

    Directly rather than through the PATCH route: several tests below need a
    value the PATCH route would REFUSE (that is the point of the save guard),
    and a fixture that could only produce acceptable values could not set up
    the break-glass case at all.
    """
    document: dict[str, Any] = {} if value is None else {CONSOLE_DOMAIN_KEY: value}
    async with AsyncSession(api_env.admin_engine) as session:
        await session.execute(
            update(Organization).where(Organization.id == org_id).values(settings=document)
        )
        await session.commit()


async def _seed_user(api_env: Any, org_id: UUID, email: str, status: str = "active") -> None:
    """Seed one `users` row.

    CALLERS MUST PASS A GLOBALLY UNIQUE ADDRESS -- see :func:`_unique`. The
    `pg_cluster` fixture is SESSION-SCOPED, so every row seeded here outlives
    the test that made it and is visible to every other test file in the run.
    A memorable-but-fixed address like `ada@elsewhere.example` therefore
    collides with the identical literal in another suite, and the symptom lands
    THERE rather than here: `tests/test_user_admin_api.py` looks a user up by
    address on the admin engine with `scalar_one_or_none()`, which raises
    `MultipleResultsFound` the moment two orgs hold the same one. That is
    exactly what this file did on its first run, and it failed only in
    combination -- both files were green alone.
    """
    async with AsyncSession(api_env.admin_engine) as session:
        session.add(
            User(
                org_id=org_id,
                external_subject=f"sub-{uuid4().hex[:10]}",
                email=email,
                display_name=email.split("@")[0],
                status=status,
            )
        )
        await session.commit()


# --------------------------------------------------------------------------- #
# resolution: org setting, platform default, override
# --------------------------------------------------------------------------- #


def test_the_org_setting_wins_over_the_platform_default() -> None:
    assert (
        effective_domain(
            {CONSOLE_DOMAIN_KEY: "acme.example"}, platform_default=BASE_DOMAIN, env={}
        )
        == "acme.example"
    )


def test_an_org_with_no_setting_falls_back_to_the_platform_default() -> None:
    """And with NO platform default either, the answer is `None` -- which
    `domain_refusal` reads as NO RESTRICTION. That default is load-bearing:
    any other reading refuses every token on every existing deployment."""
    assert effective_domain({}, platform_default=BASE_DOMAIN, env={}) == BASE_DOMAIN
    assert effective_domain({}, platform_default=None, env={}) is None
    assert domain_refusal("anyone@anywhere.example", None) is None


def test_a_non_string_stored_value_falls_back_rather_than_refusing_everyone() -> None:
    """The PATCH route type-checks the key, so this can only arrive by a hand
    edit of the JSONB column. Read as a restriction it would match no address
    at all and lock out the whole tenant; the recoverable direction wins."""
    assert effective_domain({CONSOLE_DOMAIN_KEY: 7}, platform_default=None, env={}) is None


def test_the_override_disables_the_gate_and_only_the_exact_word_does_it() -> None:
    assert gate_disabled_by_override({OVERRIDE_ENV_VAR: "off"}) is True
    assert gate_disabled_by_override({OVERRIDE_ENV_VAR: "  OFF \n"}) is True
    assert gate_disabled_by_override({}) is False
    # NOT "off": an operator who put the DOMAIN in the override variable gets
    # the gate, not a silently disabled one.
    assert gate_disabled_by_override({OVERRIDE_ENV_VAR: BASE_DOMAIN}) is False
    assert gate_disabled_by_override({OVERRIDE_ENV_VAR: ""}) is False
    assert (
        effective_domain(
            {CONSOLE_DOMAIN_KEY: BASE_DOMAIN},
            platform_default=BASE_DOMAIN,
            env={OVERRIDE_ENV_VAR: "off"},
        )
        is None
    )


async def test_resolve_reads_the_setting_out_of_the_tenant_session(api_env: Any) -> None:
    """The doors resolve per request, inside the `org_session` they already
    hold, so a change through the console applies WITHOUT A RESTART."""
    org_id = await api_env.create_org("policy-resolve")
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert await resolve_console_domain(session, org_id, api_env.settings) is None

    await _set_org_domain(api_env, org_id, BASE_DOMAIN)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert await resolve_console_domain(session, org_id, api_env.settings) == BASE_DOMAIN


async def test_resolve_is_per_org_not_per_deployment(api_env: Any) -> None:
    """The setting is a TENANT's, so one org's value must not decide another's
    -- the failure this closes is a shared cache keyed on nothing."""
    a = await api_env.create_org("policy-a")
    b = await api_env.create_org("policy-b")
    await _set_org_domain(api_env, a, BASE_DOMAIN)
    await _set_org_domain(api_env, b, "acme.example")

    async with org_session(a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert await resolve_console_domain(session, a, api_env.settings) == BASE_DOMAIN
    async with org_session(b, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert await resolve_console_domain(session, b, api_env.settings) == "acme.example"


async def test_the_override_beats_the_stored_setting(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id = await api_env.create_org("policy-override")
    await _set_org_domain(api_env, org_id, BASE_DOMAIN)
    monkeypatch.setenv(OVERRIDE_ENV_VAR, "off")
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert await resolve_console_domain(session, org_id, api_env.settings) is None


# --------------------------------------------------------------------------- #
# the preview: the preflight script's question, answered in the product
# --------------------------------------------------------------------------- #


async def test_the_preview_lists_exactly_what_the_script_would_list(api_env: Any) -> None:
    """`scripts/preflight_email_domain.py` partitions on
    `is_allowed_domain`, never on a SQL `LIKE`, precisely so it cannot disagree
    with the gate. This asserts the same rule decides the in-UI answer, by
    computing the script's own partition over the same rows."""
    org_id = await api_env.create_org("policy-preview")
    mallory = _unique("mallory", f"evil{BASE_DOMAIN}")
    seeded = [
        (_unique("ada", BASE_DOMAIN), "active"),
        (_unique("bob", f"mail.{BASE_DOMAIN}"), "active"),
        (mallory, "active"),
        (_unique("carol", "elsewhere.example"), "disabled"),
    ]
    for email, status in seeded:
        await _seed_user(api_env, org_id, email, status)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        refused = await users_refused_by(session, org_id, domain=BASE_DOMAIN)

    script_answer = sorted(
        email for email, _status in seeded if not is_allowed_domain(email, BASE_DOMAIN)
    )
    assert sorted(row.email for row in refused) == script_answer
    # Non-vacuity: the partition is not "everybody" and not "nobody".
    assert len(script_answer) == 2

    # The ACTIVE half is the half that costs somebody their access; a row that
    # is already `disabled` is refused by the status rule anyway.
    assert {row.email for row in refused if row.active} == {mallory}
    assert all(isinstance(row, RefusedUser) for row in refused)


async def test_the_preview_answers_with_only_this_tenants_users_under_rls(api_env: Any) -> None:
    """Invariant 5, as the console actually runs it: through `org_session`, so
    a leak here would be a real one.

    IT IS NOT THE FILTER THIS PINS, THOUGH, AND THE NAME USED TO SAY IT WAS.
    `app_user` is NOBYPASSRLS and `users` carries a forced `tenant_isolation`
    policy, so the neighbour's row is unreachable on this connection whether or
    not `users_refused_by` remembers `User.org_id == org_id` -- deleting that
    predicate leaves this green.
    `test_the_preview_org_predicate_holds_where_rls_is_not_doing_the_rejecting`
    below is the one that goes red."""
    mine = await api_env.create_org("policy-mine")
    theirs = await api_env.create_org("policy-theirs")
    ada = _unique("ada", "elsewhere.example")
    await _seed_user(api_env, mine, ada)
    await _seed_user(api_env, theirs, _unique("eve", "elsewhere.example"))

    async with org_session(mine, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        refused = await users_refused_by(session, mine, domain=BASE_DOMAIN)
    assert [row.email for row in refused] == [ada]


# --------------------------------------------------------------------------- #
# the save guard: the lockout becomes impossible, not warned about
# --------------------------------------------------------------------------- #


def test_self_lockout_refusal_names_the_problem_and_only_fires_on_a_lockout() -> None:
    assert self_lockout_refusal(BASE_DOMAIN, caller_email=f"me@{BASE_DOMAIN}") is None
    assert self_lockout_refusal(None, caller_email="me@elsewhere.example") is None
    reason = self_lockout_refusal(BASE_DOMAIN, caller_email="me@elsewhere.example")
    assert reason is not None
    assert "locked out" in reason.lower()


async def test_saving_a_domain_that_refuses_the_caller_is_refused(api_env: Any) -> None:
    """FALSIFIER 5 of the design doc. The old answer was an SSH script that
    printed a warning; this one is a 422 and an unchanged row."""
    org_id = await api_env.create_org("policy-save")
    sub = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, sub)  # claims `{sub}@test.local`

    response = await api_env.client.patch(
        "/api/v1/orgs/current",
        json={"settings": {CONSOLE_DOMAIN_KEY: BASE_DOMAIN}},
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert "locked out" in response.text.lower()

    async with AsyncSession(api_env.admin_engine) as session:
        stored = (
            await session.execute(
                select(Organization.settings).where(Organization.id == org_id)
            )
        ).scalar_one()
    assert CONSOLE_DOMAIN_KEY not in stored

    # NOT VACUOUS: the same route accepts a value that admits the caller.
    ok = await api_env.client.patch(
        "/api/v1/orgs/current",
        json={"settings": {CONSOLE_DOMAIN_KEY: "test.local"}},
        headers=headers,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["settings"][CONSOLE_DOMAIN_KEY] == "test.local"


async def test_clearing_the_setting_is_refused_when_the_default_would_refuse_you(
    api_env: Any,
) -> None:
    """The guard reads the RESULTING effective value, not the supplied one.

    Clearing the org key does not mean "no restriction" -- it means "fall back
    to the platform default", which may be exactly the value that refuses the
    person clearing it. A guard that only inspected the submitted string would
    wave this through and lock them out on the next request.
    """
    org_id = await api_env.create_org("policy-clear")
    sub = await api_env.create_user(org_id, "org-admin")
    await _set_org_domain(api_env, org_id, "test.local")
    api_env.settings.console_email_domain = BASE_DOMAIN
    try:
        headers = await api_env.headers(org_id, sub)
        response = await api_env.client.patch(
            "/api/v1/orgs/current",
            json={"settings": {CONSOLE_DOMAIN_KEY: None}},
            headers=headers,
        )
    finally:
        api_env.settings.console_email_domain = None
    assert response.status_code == 422, response.text
    assert "locked out" in response.text.lower()


async def test_an_unrelated_patch_is_not_caught_by_the_guard(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard fires only when the key is TOUCHED.

    The scenario is the break-glass one, which is the only way an operator can
    be signed in WHILE the stored domain would refuse them: the override is
    off, so the doors admit them. Editing the org's timezone from that session
    must not be refused because of a domain value they are not changing --
    otherwise the recovery session cannot do anything except fix the setting
    that broke.
    """
    org_id = await api_env.create_org("policy-unrelated")
    sub = await api_env.create_user(org_id, "org-admin")
    await _set_org_domain(api_env, org_id, BASE_DOMAIN)
    monkeypatch.setenv(OVERRIDE_ENV_VAR, "off")
    headers = await api_env.headers(org_id, sub)  # claims `{sub}@test.local`

    response = await api_env.client.patch(
        "/api/v1/orgs/current",
        json={"settings": {"timezone": "Europe/London"}},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["settings"][CONSOLE_DOMAIN_KEY] == BASE_DOMAIN

    # NOT VACUOUS: touching the key in the SAME break-glass session, with the
    # same value, is still refused -- the guard is about the key, not about
    # whether the gate happens to be enforcing right now. A recovery session
    # that could re-save the lockout would not be a recovery.
    refused = await api_env.client.patch(
        "/api/v1/orgs/current",
        json={"settings": {CONSOLE_DOMAIN_KEY: BASE_DOMAIN}},
        headers=headers,
    )
    assert refused.status_code == 422, refused.text


async def test_a_typo_in_the_key_is_refused_rather_than_silently_ignored(
    api_env: Any,
) -> None:
    """`console_email_domains` looks right and enforces nothing. A 200 that
    changed no gate is the worst possible answer here."""
    org_id = await api_env.create_org("policy-typo")
    sub = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, sub)

    response = await api_env.client.patch(
        "/api/v1/orgs/current",
        json={"settings": {"console_email_domains": "test.local"}},
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert "unknown settings key" in response.text


# --------------------------------------------------------------------------- #
# switching the gate OFF is not something a save can do by accident
# --------------------------------------------------------------------------- #


async def test_an_empty_console_domain_is_refused_rather_than_disabling_the_gate(
    api_env: Any,
) -> None:
    """THE CHOSEN ANSWER: an empty or whitespace-only value is a 422. `null` is
    the clear (the test below).

    `domain_refusal` reads an EMPTY base as NO RESTRICTION, and that reading is
    correct and deliberately unchanged -- it is what keeps an unset
    `ITS_CONSOLE_EMAIL_DOMAIN=` from locking out a whole deployment. The
    consequence at the SAVE path is what this pins: without a refusal here,
    `{"console_email_domain": ""}` is an `orgs.manage` holder turning a
    security control off for their entire tenant, through a value that is
    indistinguishable from a cleared form field, and the self-lockout guard
    cannot catch it because an unrestricted gate refuses nobody -- least of all
    the person saving it.

    Whitespace-only is the same value wearing a disguise: `domain_refusal`
    strips the base before deciding, so `" "` is exactly `""` to every door.
    """
    org_id = await api_env.create_org("policy-empty")
    sub = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, sub)  # claims `{sub}@test.local`
    await _set_org_domain(api_env, org_id, "test.local")

    for blank in ("", " ", "   \t", "\n"):
        response = await api_env.client.patch(
            "/api/v1/orgs/current",
            json={"settings": {CONSOLE_DOMAIN_KEY: blank}},
            headers=headers,
        )
        assert response.status_code == 422, f"{blank!r}: {response.text}"
        assert "cannot be empty" in response.text, f"{blank!r}: {response.text}"
        async with AsyncSession(api_env.admin_engine) as session:
            stored = (
                await session.execute(
                    select(Organization.settings).where(Organization.id == org_id)
                )
            ).scalar_one()
        # The gate is still on, and still says what it said before.
        assert stored[CONSOLE_DOMAIN_KEY] == "test.local", f"{blank!r}"

    # NOT VACUOUS: the same route, the same key, a real domain -> 200.
    ok = await api_env.client.patch(
        "/api/v1/orgs/current",
        json={"settings": {CONSOLE_DOMAIN_KEY: "test.local"}},
        headers=headers,
    )
    assert ok.status_code == 200, ok.text


async def test_clearing_the_key_still_works_and_is_audited_as_a_clear(
    api_env: Any,
) -> None:
    """The other half of the choice, and the reason refusing `""` costs nothing.

    Removing the org's own value is `null`, which the route already treats as
    "reset to default" and already records under `settings_cleared` -- so the
    audit log distinguishes a deliberate removal from a typo, which is exactly
    what an empty string could not do. What the operator gets back is the
    PLATFORM DEFAULT, not "no rule": an org-admin cannot switch off a gate the
    deployment configured, only stop overriding it. Turning the gate off
    everywhere stays the vendor's `ITS_CONSOLE_EMAIL_DOMAIN_OVERRIDE`.
    """
    org_id = await api_env.create_org("policy-clear-ok")
    sub = await api_env.create_user(org_id, "org-admin", "auditor")
    headers = await api_env.headers(org_id, sub)
    await _set_org_domain(api_env, org_id, "test.local")

    response = await api_env.client.patch(
        "/api/v1/orgs/current",
        json={"settings": {CONSOLE_DOMAIN_KEY: None}},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert CONSOLE_DOMAIN_KEY not in response.json()["settings"]

    events = await api_env.client.get("/api/v1/audit?action=org.updated", headers=headers)
    assert events.json()["total"] == 1, events.text
    meta = events.json()["items"][0]["metadata"]
    assert meta["settings_cleared"] == [CONSOLE_DOMAIN_KEY]
    assert meta["settings_set"] == []

    # And the doors now read the platform default, which in this deployment is
    # unset -- so the org is unrestricted BECAUSE the platform is, not because
    # a tenant admin said so.
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert await resolve_console_domain(session, org_id, api_env.settings) is None


async def test_a_padded_domain_is_stripped_before_it_is_stored(api_env: Any) -> None:
    """`"  toeshee.com "` is stored as `"toeshee.com"`.

    The doors survive padding either way -- `domain_refusal` strips the base
    before comparing, which it learned the hard way: an earlier version
    compared against the RAW value and a single trailing newline from a
    copied `.env` line refused EVERY user at ALL THREE doors. Storing the
    stripped value is how that class of byte stops existing at rest rather
    than being forgiven at each read: the form shows what is enforced, the
    emptiness check above cannot be dodged with a space, and a future reader
    that forgets to strip cannot disagree with the door.
    """
    org_id = await api_env.create_org("policy-padded")
    sub = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, sub)

    response = await api_env.client.patch(
        "/api/v1/orgs/current",
        json={"settings": {CONSOLE_DOMAIN_KEY: "  test.local \n"}},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["settings"][CONSOLE_DOMAIN_KEY] == "test.local"

    async with AsyncSession(api_env.admin_engine) as session:
        stored = (
            await session.execute(
                select(Organization.settings).where(Organization.id == org_id)
            )
        ).scalar_one()
    assert stored[CONSOLE_DOMAIN_KEY] == "test.local"
    # The value the doors resolve is the stripped one, with nothing left for
    # `domain_refusal` to forgive.
    assert effective_domain(stored, platform_default=None, env={}) == "test.local"


# --------------------------------------------------------------------------- #
# the preview route
# --------------------------------------------------------------------------- #


async def test_the_preview_route_shows_who_a_candidate_would_refuse(api_env: Any) -> None:
    org_id = await api_env.create_org("policy-preview-api")
    sub = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, sub)
    ada = _unique("ada", BASE_DOMAIN)
    mallory = _unique("mallory", "elsewhere.example")
    await _seed_user(api_env, org_id, ada)
    await _seed_user(api_env, org_id, mallory)

    response = await api_env.client.get(
        "/api/v1/orgs/current/console-domain-preview",
        params={"domain": BASE_DOMAIN},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["domain"] == BASE_DOMAIN
    refused = {row["email"] for row in body["refused"]}
    # The org-admin's own seeded row (`{sub}@test.local`) is refused too, and
    # the flag says so explicitly -- that is the number the form must shout.
    assert mallory in refused
    assert ada not in refused
    assert body["would_refuse_you"] is True
    assert body["blocking"] == len([r for r in body["refused"] if r["status"] == "active"])


async def test_the_preview_route_is_rbac_gated(api_env: Any) -> None:
    """Server-side, on `orgs.manage` -- the same permission that can save the
    value. The console never gates security; it reflects it."""
    org_id = await api_env.create_org("policy-preview-rbac")
    sub = await api_env.create_user(org_id, "read-only")
    headers = await api_env.headers(org_id, sub)
    response = await api_env.client.get(
        "/api/v1/orgs/current/console-domain-preview",
        params={"domain": BASE_DOMAIN},
        headers=headers,
    )
    assert response.status_code == 403, response.text


# --------------------------------------------------------------------------- #
# the order the doors ask their two questions in, pinned deliberately
# --------------------------------------------------------------------------- #


async def test_an_unknown_org_is_401_and_a_known_org_refuses_the_domain_at_403(
    api_env: Any,
) -> None:
    """THE ORDER IS: resolve the org, THEN apply the allowlist. Pinned on
    purpose, because it is observable and it changed.

    Before the allowlist became an org setting, the domain refusal ran ahead of
    the org lookup, so ANY refused address got 403 and learned nothing about
    the deployment. Now an unknown slug answers 401 "unknown organization"
    while a known slug answers 403 "email domain not permitted" -- which tells
    the holder of a refused token which org slugs exist here.

    That is accepted, and the reasoning is at the refusal site
    (`app.core.identity.policy.resolve_console_domain`, "THE ORDER THE DOORS
    ASK..."): the setting belongs to the tenant, so it cannot be read before
    the tenant is known, and the alternative answers admission from the
    platform default -- a different rule from the one the org configured. The
    oracle costs a slug, to a caller already holding a token this deployment's
    own IdP signed with that slug in it.

    This test exists so the next person cannot flip it back by accident: they
    have to come here and change an assertion that says why.
    """
    org_id = await api_env.create_org("policy-order")
    slug = await api_env.org_slug(org_id)
    await _set_org_domain(api_env, org_id, BASE_DOMAIN)
    stranger = f"eve-{uuid4().hex[:8]}@elsewhere.example"

    # The PLATFORM DEFAULT is set too, and that is what makes this an ordering
    # test rather than a coincidence: a door that refused on the domain FIRST
    # would have a value to refuse this address with even when the org is
    # unknown, and would answer 403 below instead of 401.
    api_env.settings.console_email_domain = BASE_DOMAIN
    try:
        unknown = api_env.idp.token(
            f"sub-{uuid4().hex[:10]}", f"no-such-org-{uuid4().hex[:8]}", email=stranger
        )
        known = api_env.idp.token(f"sub-{uuid4().hex[:10]}", slug, email=stranger)
        unknown_org = await api_env.client.get(
            "/api/v1/orgs/current", headers={"Authorization": f"Bearer {unknown}"}
        )
        refused_domain = await api_env.client.get(
            "/api/v1/orgs/current", headers={"Authorization": f"Bearer {known}"}
        )
    finally:
        api_env.settings.console_email_domain = None

    assert unknown_org.status_code == 401, unknown_org.text
    assert unknown_org.json()["detail"] == "unknown organization"

    assert refused_domain.status_code == 403, refused_domain.text
    assert "email domain not permitted" in refused_domain.json()["detail"]

    # The two answers are DIFFERENT, which is the whole subject: a single
    # indistinguishable refusal would hide the slug and would also merge "your
    # token is not genuine" with "your identity is not admitted" -- the
    # distinction all three doors keep so an operator debugging a lockout can
    # tell them apart from the wire alone.
    assert unknown_org.status_code != refused_domain.status_code

    # Non-vacuity: with the allowlist satisfied, the same known slug is admitted
    # -- so the 403 above is the domain rule answering and not the route.
    admitted = await api_env.client.get(
        "/api/v1/orgs/current",
        headers={
            "Authorization": f"Bearer "
            f"{api_env.idp.token(f'sub-{uuid4().hex[:10]}', slug, email=f'ada@{BASE_DOMAIN}')}"
        },
    )
    assert admitted.status_code == 200, admitted.text


# --------------------------------------------------------------------------- #
# Migration 0067 — the backfill that makes the upgrade a no-op
# --------------------------------------------------------------------------- #


def _migration_0067() -> Any:
    """Load `0067_org_console_domain.py` by path.

    Alembic revision files are not an importable package (the module name
    starts with a digit), so this loads it the way Alembic does: as a file.
    """
    import importlib.util
    import pathlib

    path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "migrations"
        / "versions"
        / "0067_org_console_domain.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0067", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_backfill_writes_the_env_value_and_then_leaves_it_alone(
    api_env: Any,
) -> None:
    """WHY THE MIGRATION EXISTS: without it, an upgrade CHANGES BEHAVIOUR for a
    deployment that had configured `ITS_CONSOLE_EMAIL_DOMAIN`, because the org
    setting is read first and there is nothing in it.

    Executed against the real Postgres rather than asserted about a string:
    `jsonb_set` on a NOT NULL JSONB column with a text cast is exactly the kind
    of statement that is either right or a migration that fails in the field,
    and no amount of reading proves which.

    The second half is the rule that matters more. Re-running the backfill --
    which happens the moment somebody replays migrations, and which the
    round-trip CI job does on every push -- must NOT put the old environment
    value back over a value an operator has since chosen in the console.
    """
    module = _migration_0067()
    org_id = await api_env.create_org("policy-0067")

    async with AsyncSession(api_env.admin_engine) as session:
        await session.execute(module.backfill_statement(BASE_DOMAIN))
        await session.commit()
        stored = (
            await session.execute(
                select(Organization.settings).where(Organization.id == org_id)
            )
        ).scalar_one()
    assert stored[CONSOLE_DOMAIN_KEY] == BASE_DOMAIN

    # The operator changes it in the console; the backfill runs again.
    await _set_org_domain(api_env, org_id, "chosen-later.example")
    async with AsyncSession(api_env.admin_engine) as session:
        await session.execute(module.backfill_statement(BASE_DOMAIN))
        await session.commit()
        stored = (
            await session.execute(
                select(Organization.settings).where(Organization.id == org_id)
            )
        ).scalar_one()
    assert stored[CONSOLE_DOMAIN_KEY] == "chosen-later.example"


def test_the_backfill_writes_nothing_when_no_allowlist_was_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank or absent variable means "no allowlist was configured", and a
    migration that wrote a key anyway would be INVENTING a security setting.

    A whitespace-only value counts as blank for the same reason
    `app.core.identity.email_domain.domain_refusal` reads it that way: read as
    a restriction it matches no address at all, and backfilling THAT into every
    org's stored settings would be a permanent, fleet-wide lockout written by
    an upgrade.
    """
    module = _migration_0067()
    executed: list[Any] = []
    monkeypatch.setattr(module.op, "execute", executed.append)

    for blank in ("", "   ", "\n"):
        monkeypatch.setenv(module.LEGACY_ENV_VAR, blank)
        module.upgrade()
    assert executed == []

    # Non-vacuity, and the strip is load-bearing: a trailing newline is what a
    # here-doc or a copied `.env` line produces, and it must be stored as the
    # domain rather than as a value no address can match.
    monkeypatch.setenv(module.LEGACY_ENV_VAR, f" {BASE_DOMAIN}\n")
    module.upgrade()
    assert len(executed) == 1
    assert executed[0].compile().params["value"] == BASE_DOMAIN


async def test_the_preview_org_predicate_holds_where_rls_is_not_doing_the_rejecting(
    api_env: Any, bypassrls_session: AsyncSession
) -> None:
    """THE TEST IN THIS FILE THAT CAN ACTUALLY FAIL.

    `its_admin` carries BYPASSRLS (`tests/conftest.py`), so `users_refused_by`'s
    own `User.org_id == org_id` is the only thing scoping the read -- which is
    the shape a leak would take in production if the preview were ever called
    on anything but an `org_session`.

    WHAT WOULD ESCAPE IS THE POINT. The preview returns email address, display
    name and account status for everybody it names, so a missing predicate does
    not merely miscount: it hands one tenant's administrator a directory of
    another tenant's people, on a screen whose whole purpose is to be read
    carefully before saving.

    BOTH ORGS HOLD A REFUSABLE ADDRESS, deliberately. If only org A's user were
    off-domain, `domain_refusal`'s Python-side partition would filter org B's
    row out for a reason that has nothing to do with tenancy, and the test
    would pass with the predicate deleted.

    FALSIFIED: deleting `.where(User.org_id == org_id)` from `users_refused_by`
    puts org B's address in org A's preview.
    """
    mine = await api_env.create_org("policy-pred-mine")
    theirs = await api_env.create_org("policy-pred-theirs")
    ada = _unique("ada", "elsewhere.example")
    eve = _unique("eve", "elsewhere.example")
    await _seed_user(api_env, mine, ada)
    await _seed_user(api_env, theirs, eve)

    refused_mine = await users_refused_by(bypassrls_session, mine, domain=BASE_DOMAIN)
    refused_theirs = await users_refused_by(bypassrls_session, theirs, domain=BASE_DOMAIN)

    assert [row.email for row in refused_mine] == [ada], (
        "the preview named an address outside the calling tenant"
    )
    assert [row.email for row in refused_theirs] == [eve], (
        "...and the neighbour really did hold a refusable address to leak"
    )
