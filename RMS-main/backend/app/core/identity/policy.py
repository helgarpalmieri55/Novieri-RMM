"""Where the console's email-domain allowlist comes from, and what changing it
would cost.

`docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §2.2.

THE PROBLEM THIS REPLACES. The allowlist used to be `ITS_CONSOLE_EMAIL_DOMAIN`
and nothing else. Changing it meant an SSH session, a file edit and a restart;
a wrong value refused every operator at all three admission doors with no way
back through the UI; and the only defence was an operator remembering to run
`scripts/preflight_email_domain.py` over SSH first, whose entire job was to
print who was about to be locked out. Both halves of that are exactly what the
buyer test forbids.

So the value lives in ``organizations.settings["console_email_domain"]``,
written through the org-settings PATCH route like any other setting, and the
two SSH-shaped things become structure instead of procedure:

  * :func:`self_lockout_refusal` lets the save path REFUSE a value that would
    refuse the caller's own address. The lockout is impossible, not warned
    about.
  * :func:`users_refused_by` answers the preflight's question -- "who would
    this refuse" -- inside the product, scoped to the caller's own tenant,
    using the SAME predicate the doors use.

THE ENV VAR SURVIVES AS THE PLATFORM DEFAULT, AND THAT IS A SECURITY DECISION
=============================================================================

The plan demotes `ITS_CONSOLE_EMAIL_DOMAIN` to nothing but a break-glass
switch. Taken literally that opens a hole it does not mention: migration 0067
backfills the org setting for orgs that EXIST at upgrade time, but an org
created afterwards carries no key, and "no key" read as "unrestricted" means a
multi-tenant deployment silently loses its allowlist for every new tenant.

So the resolution order is: **override, then org setting, then platform
default**. The demotion the plan actually cares about still holds --  the org
setting overrides the env var, so nobody has to SSH anywhere to change it --
and a deployment that configured the allowlist keeps it for tenants created
later.

THE OVERRIDE IS THE BREAK-GLASS, AND IT IS DELIBERATELY LITERAL
===============================================================

``ITS_CONSOLE_EMAIL_DOMAIN_OVERRIDE=off`` disables the gate everywhere. It
exists for the one case the save guard cannot prevent: a value written before
this shipped, or straight into the JSONB column, that admits nobody. ANY other
value is ignored and the gate stays on -- an operator who puts a domain in the
override variable gets the gate, because the fail-safe direction for a switch
whose only job is to turn a security control OFF is "did not turn it off".

READ FROM ``os.environ`` DIRECTLY, and that is a stated compromise rather than
a preference: `app/core/config.py`'s `Settings` is the right home for it, and
this task's file ownership did not include that module. The precedent is
`app/core/ai/providers/__init__.py`, which reads `os.environ` in one documented
place for the same reason. When the field lands on `Settings`, the change is
to :func:`gate_disabled_by_override` and nothing else.

RESOLVED PER REQUEST, WITH NO CACHE
===================================

The spec offers a short-TTL cache. There is none, on purpose. The read is one
primary-key SELECT on a session the door has already opened for its own work,
against a table it is about to read anyway; a cache would buy a fraction of
that and cost the property the whole change exists for -- that a value saved in
the console applies to the NEXT request, with no restart and no TTL to wait
out. A cache also has to be keyed per org and invalidated across processes, and
a stale entry here is either a locked-out operator or a gate that is off after
being switched on. If a measurement ever shows this read mattering, the answer
is to carry the org row the doors already fetch, not to add a TTL.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.i18n import tr
from app.core.identity import ACTIVE_STATUS
from app.core.identity.email_domain import domain_refusal
from app.models import Organization, User

# The allowlisted `organizations.settings` key. Named here rather than spelled
# as a literal at each call site so the doors, the save guard, the preview and
# migration 0067 cannot drift apart by a typo.
CONSOLE_DOMAIN_KEY: Final = "console_email_domain"

OVERRIDE_ENV_VAR: Final = "ITS_CONSOLE_EMAIL_DOMAIN_OVERRIDE"
OVERRIDE_OFF: Final = "off"


def gate_disabled_by_override(env: Mapping[str, str] | None = None) -> bool:
    """True iff the vendor break-glass switch is set to exactly ``off``.

    ``env`` is injectable so the rule is testable without mutating process
    state; production passes nothing and reads `os.environ`.
    """
    raw = (os.environ if env is None else env).get(OVERRIDE_ENV_VAR)
    return raw is not None and raw.strip().lower() == OVERRIDE_OFF


def effective_domain(
    org_settings: Mapping[str, Any],
    *,
    platform_default: str | None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """The base domain this org's console enforces, or ``None`` for no rule.

    ``None`` is what :func:`app.core.identity.email_domain.domain_refusal`
    reads as NO RESTRICTION, so an unconfigured deployment is unchanged.

    A stored value that is not a string can only arrive by a hand edit of the
    JSONB column -- the PATCH route type-checks the key -- and it falls back to
    the platform default rather than being coerced. Coercing it to `str` would
    turn `null` into the literal ``"None"``, which matches no address and locks
    out the whole tenant; unrestricted-or-default is the recoverable direction.
    """
    if gate_disabled_by_override(env):
        return None
    stored = org_settings.get(CONSOLE_DOMAIN_KEY)
    if isinstance(stored, str):
        return stored
    return platform_default


async def resolve_console_domain(
    session: AsyncSession, org_id: UUID, settings: Settings
) -> str | None:
    """The base domain to enforce for ``org_id``, read through ``session``.

    ``session`` MUST be a tenant session (`app.core.tenancy.org_session`): the
    read is then constrained by RLS in the database rather than by the
    `WHERE` clause alone (invariant 5), and the doors already hold one open.

    THE ORDER THE DOORS ASK THEIR TWO QUESTIONS IN, AND WHAT IT DISCLOSES
    --------------------------------------------------------------------
    All three doors now resolve the ORG FIRST and apply the allowlist SECOND,
    because this function cannot answer before the org is known -- the value
    is the tenant's. That is a change from the env-var era, when the domain
    refusal ran ahead of the org lookup, and it is OBSERVABLE: a token naming
    an unknown slug is refused as "unknown organization" (401 / 4401) while
    the same address against a known slug is refused as "email domain not
    permitted" (403 / 4403). So a caller can learn which org slugs exist on
    this deployment.

    THAT IS ACCEPTED, DELIBERATELY, and pinned by
    `tests/test_domain_policy.py::test_an_unknown_org_is_401_and_a_known_org_refuses_the_domain_at_403`
    so it cannot be flipped back without an argument. The accounting:

      * What leaks is a slug, and only to a caller already holding a token
        this deployment's own IdP signed with that slug in its `org` claim.
        The slug is not a secret -- it is in the sign-in URL and in the token
        the caller is presenting.
      * What the alternative costs is correctness, not just code. Restoring
        the old order means deciding admission from the PLATFORM DEFAULT,
        because the org's own value is unreadable at that point -- so a
        deployment whose tenants each configure their own allowlist would
        refuse identities by a rule none of them chose, and admit identities
        their rule refuses. Answering the right question about the wrong
        tenant is worse than disclosing which tenants exist.
      * Hiding it properly would mean ONE indistinguishable refusal for both
        cases, which also merges "your token is not genuine" with "your
        identity is not admitted". Every door keeps that distinction on
        purpose (403 vs 401, 4403 vs 4401), because an operator debugging a
        lockout has only the wire, and §2.2 exists to make that lockout
        debuggable from the console rather than from an SSH session.

    If a deployment ever needs slugs to be unguessable, the fix is opaque
    slugs, not a refusal order that lies about which rule was applied.
    """
    if gate_disabled_by_override():
        return None
    stored = (
        await session.execute(
            select(Organization.settings).where(Organization.id == org_id)
        )
    ).scalar_one_or_none()
    # `stored or {}` would be wrong for a falsy-but-present value only if the
    # column were nullable; it is NOT NULL, so the `None` here means "no such
    # row visible in this tenant", which reads as "no org setting".
    return effective_domain(stored or {}, platform_default=settings.console_email_domain)


@dataclass(frozen=True)
class RefusedUser:
    """One person a candidate domain would turn away.

    ``active`` is separated from ``status`` because it is the number that
    decides whether the change costs anybody anything: a `disabled` or
    `deleted` row is already refused by the status half of the same gate, so
    the allowlist changes nothing for it. `scripts/preflight_email_domain.py`
    draws the same distinction, and its exit code depends on it.
    """

    email: str
    display_name: str | None
    status: str
    active: bool


async def users_refused_by(
    session: AsyncSession, org_id: UUID, *, domain: str | None
) -> list[RefusedUser]:
    """Every user in ``org_id`` whom ``domain`` would refuse, ordered by email.

    THE PREDICATE IS `domain_refusal`, NEVER A SQL `LIKE`. The preflight script
    makes the same point at length and for the same reason: `LIKE '%toeshee.com'`
    says `eviltoeshee.com` is fine and the gate says it is not, so the operator
    is told nobody is affected and locks somebody out anyway. The rows are
    fetched and partitioned in Python precisely so the answer cannot disagree
    with the door.

    Tenant-scoped: ``session`` is an `org_session`, so RLS constrains the read
    even though the `WHERE` clause names `org_id` as well.
    """
    rows = (
        await session.execute(
            select(User.email, User.display_name, User.status)
            .where(User.org_id == org_id)
            .order_by(User.email)
        )
    ).all()
    return [
        RefusedUser(
            email=email,
            display_name=display_name,
            status=status,
            active=status == ACTIVE_STATUS,
        )
        for email, display_name, status in rows
        if domain_refusal(email, domain) is not None
    ]


def self_lockout_refusal(domain: str | None, *, caller_email: str) -> str | None:
    """``None`` if ``caller_email`` survives ``domain``, else why it may not be
    saved.

    THE STRUCTURAL HALF OF §2.2, and the reason the SSH preflight can be
    retired from the runbook. The old defence was procedural -- run a script,
    read its output, decide -- and procedures are skipped. This one cannot be:
    the save path refuses the value.

    It is checked against the EFFECTIVE domain that would result from the save,
    not against the string the caller typed. Clearing the org key does not mean
    "no restriction"; it means "fall back to the platform default", which may
    be exactly the value that refuses the person clearing it.

    WHAT IT DOES NOT CLOSE, said plainly: it protects the CALLER, not everyone
    else. An org-admin may still save a value that locks out a colleague --
    that is a legitimate thing to want to do, which is what
    :func:`users_refused_by` is for. And the caller is judged on the address in
    their own token, so an operator whose IdP claim later changes is outside
    what this can promise.
    """
    if domain_refusal(caller_email, domain) is None:
        return None
    return tr(
        "that console email domain would refuse your own address ({caller_email}), so you "
        "would be locked out of the console the moment it was saved",
        caller_email=caller_email,
    )


__all__ = [
    "CONSOLE_DOMAIN_KEY",
    "OVERRIDE_ENV_VAR",
    "OVERRIDE_OFF",
    "RefusedUser",
    "effective_domain",
    "gate_disabled_by_override",
    "resolve_console_domain",
    "self_lockout_refusal",
    "users_refused_by",
]
