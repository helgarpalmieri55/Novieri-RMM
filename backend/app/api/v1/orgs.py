"""The organization's own record, and the ONLY write path to it.

The operator's requirement is that everything under the Settings menu is
editable from the console rather than by editing files on a server. Until this
module existed the `organizations` row -- name, slug, status and a JSONB
`settings` blob -- had no write route at all, so "change the org's display
name" meant a hand-written UPDATE against production. Two routes close that:

    GET   /api/v1/orgs/current   any authenticated operator
    PATCH /api/v1/orgs/current   `orgs.manage`

THERE IS NO ROUTE THAT TAKES AN ORG ID, and that is a design decision rather
than an omission. `ctx.org_id` comes from the validated token, and the UPDATE
is `WHERE organizations.id = ctx.org_id`; the request body cannot name a
target. A route shaped `PATCH /orgs/{org_id}` would make cross-tenant writes a
matter of getting one authorization check right on every future edit, and
invariant #5 is not a check you get to write once per route. The body model is
`extra="forbid"`, so a caller who sends `id` or `org_id` gets a 422 rather than
a silent no-op that reads like success. `tests/test_org_settings.py` pins both
the absence of an id-taking route and the fact that a supplied id changes
nothing.

`slug` and `status` are READ-ONLY here. The slug is the tenant's identity in
the OIDC `org` claim (`app.api.deps.auth_context` resolves the org BY SLUG,
before any tenant context exists) -- renaming it through the console would log
every operator in that org out of a platform they can no longer authenticate
to, and would do it through a route whose own authorization depends on the
value being changed. `status` is a platform-operator lifecycle field, not a
tenant-editable one: an org must not be able to mark itself active.

WHY `settings` IS AN ALLOWLIST AND NOT A FREE-FORM DICT
-------------------------------------------------------
A JSONB column reachable from an authenticated route is two hazards wearing
one hat, and neither is hypothetical:

1. It is unbounded storage. Nothing about `dict[str, Any]` stops a caller
   writing tens of megabytes per PATCH into a row that every request path
   reads, on a table with no per-row size limit of its own.
2. Worse, it is a namespace a FUTURE reader will treat as configuration. The
   column has no readers in this tree today (verified: nothing selects
   `Organization.settings`). The moment one appears, whatever key it reads is
   operator-supplied data that arrived over HTTP -- and if the write path
   accepted any key at all, the value was never validated against what that
   reader expects. This is how "the console can set a preference" becomes "the
   console can set a flag some subsystem trusts".

So: `SETTINGS_ALLOWLIST` is a closed, documented set of keys with declared
types and per-key bounds, `MAX_SETTINGS_BYTES` caps the whole document, and
anything else is a 422. Refused, never ignored -- silently dropping an unknown
key answers a typo with a 200 having changed nothing, and writes an audit row
for a no-op edit.

The size cap is checked on the SUPPLIED document BEFORE any per-key work, and
again on the MERGED result afterwards. The first is what makes a multi-megabyte
body cheap to refuse; the second is the backstop that still holds if a future
key's own length bound is generous, and is the one that survives someone
loosening a per-key cap without thinking about the total.

WHAT THIS ROUTE REFUSES TO MAKE WRITABLE, AND WHY
-------------------------------------------------
None of the following is an org setting, and no key naming one may ever be
added to the allowlist:

  * database credentials and DSNs (`ITS_DATABASE_URL`, `ITS_DATABASE_ADMIN_URL`,
    `ITS_DATABASE_PORTAL_URL`)
  * the OpenBao address and token (`ITS_OPENBAO_ADDR`, `ITS_OPENBAO_TOKEN`)
    and the local vault root key (`ITS_VAULT_ROOT_KEY_PATH`)
  * the job-signing key and backend (`ITS_SIGNING_KEY_PATH`,
    `ITS_SIGNING_BACKEND`) -- invariant #1's root of trust
  * the audit HMAC key (`ITS_AUDIT_HMAC_KEY_PATH`) -- invariant #4's
  * TLS/CA material (`ITS_CA_DIR`, `ITS_CA_BACKEND`) -- invariant #2's
  * object-storage keys and endpoints (`ITS_S3_ACCESS_KEY`,
    `ITS_S3_SECRET_KEY`, `ITS_S3_ENDPOINT`, `ITS_S3_PUBLIC_ENDPOINT`)
  * OIDC issuer/JWKS/audience -- the identity this route's own auth rests on
  * the gateway URLs and the RBAC/permission catalogue

These live in the environment or in the vault (invariant #3) precisely so that
compromising an operator account is not the same as compromising the platform.
A console field that rewrote any of them would be a privilege-escalation path
by construction: `orgs.manage` is an ORG-admin grant, and repointing the
signing key or the OIDC issuer is a FLEET-level action. The org-settings screen
is not the place where a tenant admin gets to redefine what the control plane
trusts. `NEVER_ORG_SETTINGS` below names them in code, and a test asserts the
allowlist stays disjoint from that list and free of secret-shaped names, so a
future addition has to argue with a red test rather than with this paragraph.

For the same reason the allowlist deliberately contains NO key that names a
security control -- no session lifetime, no MFA toggle, no IP allowlist. Not
because such settings could never exist, but because enforcement for every one
of them lives somewhere else (the IdP, the gateway, the reverse proxy), so a
key here would either be inert -- a control that reads as enforced and is not,
which is worse than no control -- or would have to be wired into enforcement,
which is a spec'd module and not a JSONB key.

THE ONE EXCEPTION, ADDED 2026-08-19, AND IT MEETS THAT BAR RATHER THAN BENDING IT
--------------------------------------------------------------------------------
`console_email_domain` IS a security control: it is the email-domain allowlist
that `app/api/deps.py`, `app/api/v1/ws.py` and `app/modules/remote/tunnel.py`
enforce at every door that turns a token into a principal. It is here because
the paragraph above named the condition under which such a key is admissible
and this key satisfies it -- the enforcement is spec'd
(`docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §2.2),
it is wired to the three doors through
`app.core.identity.policy.resolve_console_domain`, and there is a test at each
door proving the stored value is what they read. It is not inert, so it is not
the failure mode the rule exists to prevent.

It is also, uniquely, a key whose enforcement can refuse the person editing it.
So this route carries a guard that no other key needs: a value that would
refuse the CALLER'S OWN address is a 422 (`self_lockout_refusal`), because
the alternative is the failure the whole change exists to remove -- a one-line
edit whose blast radius is every operator in the tenant, previously defended
by an SSH script the operator had to remember to run. `orgs.manage` is
deliberately still the permission: the scope of the setting is exactly one
org, so it is an org-admin decision, and widening the gate would put the
value out of reach of the console it configures.

THE SECOND ADMITTED SECURITY CONTROL, ADDED WITH THE MCP SERVER: `mcp_enabled`
is the org's switch for connecting AI apps over MCP, and it is off by default.
It meets the same bar as `console_email_domain` rather than bending it: its
enforcement is wired and tested in `app/mcp/server.py` and
`tests/test_mcp_server.py`, where it is read on every request, so a value here
is never inert.

AN EMPTY VALUE IS REFUSED; `null` IS THE CLEAR. `domain_refusal` reads an
empty base as NO RESTRICTION -- correctly, and that is not changed -- so
`{"console_email_domain": ""}` arriving here would be an org-admin switching a
security control off for their entire tenant with a blank form field, past a
self-lockout guard that cannot object because an unrestricted gate refuses
nobody. So a value that is empty after stripping is a 422, and the way to
remove the org's own value is `null`: the key is deleted, the audit row says
`settings_cleared` rather than `settings_set`, and the effective value falls
back to the PLATFORM DEFAULT. An org-admin can stop overriding the
deployment's allowlist; they cannot turn it off. Whitespace is stripped before
storage for the same key and the same reason -- see `_normalised`.

WHAT THIS KEY DOES NOT LET AN ORG-ADMIN DO. It cannot reach another tenant
(the UPDATE is `WHERE id = ctx.org_id`), it cannot disable the gate for the
platform (that is the vendor's `ITS_CONSOLE_EMAIL_DOMAIN_OVERRIDE`), and it
cannot admit an identity the IdP would not authenticate -- the rule only ever
narrows who this console admits.

AUDIT (invariant #4)
--------------------
Every accepted PATCH writes one `org.updated` event to the hash-chained log,
inside the same transaction as the UPDATE, so the write and its record commit
or roll back together.

It records WHICH settings keys were set or cleared, and NOT their values. The
allowlist holds nothing secret-shaped today -- that is enforced by test -- so
this is not about today's values. It is that the audit log is append-only and
tamper-evident BY DESIGN: a value written into it cannot be redacted later. The
day someone adds a key whose value turns out to be sensitive, every historical
audit row would already contain it, and the property that makes the log
trustworthy is exactly the property that makes that unfixable. Keys are enough
to answer "who changed what, when". `name` is the one exception and its old and
new values ARE recorded: it is the org's display name, it is already returned
by an unauthenticated-of-permission read to every operator in the org, and
"who renamed us" is unanswerable without the previous value.
"""

import json
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.api.deps import AuthContext, bound_auth_context, require_permission
from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.identity.policy import (
    CONSOLE_DOMAIN_KEY,
    effective_domain,
    self_lockout_refusal,
    users_refused_by,
)
from app.core.tenancy import org_session
from app.models import Organization, User

router = APIRouter(prefix="/orgs", tags=["orgs"])


@dataclass(frozen=True)
class SettingSpec:
    """One allowlisted key's declared type and bounds.

    `kind` is checked with `isinstance` and `bool` is excluded from `int`
    explicitly -- in Python `isinstance(True, int)` is True, so a bare int
    check accepts `true` for a numeric setting and stores it as a boolean in
    JSONB, which the next reader gets back as a boolean.
    """

    kind: Literal["str", "int", "bool"]
    max_length: int | None = None
    choices: frozenset[str] | None = None
    bounds: tuple[int, int] | None = None


#: The org's MCP switch (MCP server design §6); its moves are audited as
#: `mcp.settings_changed` as well as `org.updated`.
MCP_SWITCH_KEY = "mcp_enabled"

# THE CLOSED SET. Every key here is presentation or contact information: what
# the console shows and who to call. Nothing here is read by an enforcement
# path, and nothing here may ever become one -- see the module docstring.
SETTINGS_ALLOWLIST: dict[str, SettingSpec] = {
    # IANA zone name, e.g. "Europe/London". Not validated against the tz
    # database here: the console renders with it, nothing schedules on it, and
    # a wrong-but-well-formed zone is a display bug rather than a security one.
    "timezone": SettingSpec(kind="str", max_length=64),
    # BCP-47-ish language tag, e.g. "en-GB".
    "locale": SettingSpec(kind="str", max_length=16),
    "date_format": SettingSpec(kind="str", choices=frozenset({"iso", "dmy", "mdy"})),
    # Contact details the console shows to operators and end users.
    "support_email": SettingSpec(kind="str", max_length=254),
    "support_phone": SettingSpec(kind="str", max_length=40),
    "support_url": SettingSpec(kind="str", max_length=300),
    # A notice shown in the console chrome ("maintenance window Friday").
    # Rendered as TEXT by React, never as HTML -- it is operator-supplied and
    # is not markup.
    "console_banner": SettingSpec(kind="str", max_length=280),
    "console_banner_severity": SettingSpec(
        kind="str", choices=frozenset({"info", "warning", "critical"})
    ),
    # "#RRGGBB". Length-bounded and shape-checked below so it cannot carry a
    # CSS expression into a style attribute.
    "brand_primary_color": SettingSpec(kind="str", max_length=7),
    # How many rows the console's list screens request by default.
    "default_page_size": SettingSpec(kind="int", bounds=(10, 200)),
    "show_device_serials": SettingSpec(kind="bool"),
    # THE ONE ENFORCEMENT KEY -- see the module docstring's exception section.
    # 253 is the maximum length of a DNS name; the value is a base domain, not
    # a list and not a pattern.
    CONSOLE_DOMAIN_KEY: SettingSpec(kind="str", max_length=253),
    # THE SECOND ENFORCEMENT KEY -- see the module docstring. Off unless true;
    # `app/mcp/server.py` reads it on every MCP request.
    MCP_SWITCH_KEY: SettingSpec(kind="bool"),
}

# Named in code so the disjointness test has something to compare against
# rather than a paragraph of prose. These are environment/vault-owned; see the
# module docstring for why each one being writable here would be an escalation.
NEVER_ORG_SETTINGS: frozenset[str] = frozenset(
    {
        "database_url",
        "database_admin_url",
        "database_portal_url",
        "openbao_addr",
        "openbao_token",
        "vault_root_key_path",
        "signing_key_path",
        "signing_backend",
        "audit_hmac_key_path",
        "ca_dir",
        "ca_backend",
        "s3_access_key",
        "s3_secret_key",
        "s3_endpoint",
        "s3_public_endpoint",
        "oidc_issuer",
        "oidc_jwks_url",
        "oidc_audience",
        "gateway_url",
        "gateway_relay_url",
    }
)

# Substrings that make a key name secret-shaped. A key matching one of these
# is refused entry to the allowlist by test, not by review.
SECRET_SHAPED_FRAGMENTS: frozenset[str] = frozenset(
    {
        "secret",
        "password",
        "passwd",
        "token",
        "credential",
        "private",
        "hmac",
        "signing",
        "cert",
        "tls",
        "dsn",
        "database_url",
        "apikey",
        "api_key",
        "access_key",
    }
)

# The whole settings document, compact JSON, in bytes. Roughly five times what
# the per-key bounds above can sum to, so it is a genuine second gate rather
# than the same gate written twice -- and it is the one that still holds if a
# key's own bound is loosened later.
MAX_SETTINGS_BYTES = 4096

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


class OrgOut(BaseModel):
    """One organization: its identity, its lifecycle status, and its settings
    document. `settings` holds only allowlisted presentation keys (see
    `SETTINGS_ALLOWLIST`) -- it is never a place a secret can be, so this model
    is safe to return in full. `slug` and `status` are read-only here.

    The docstring is load-bearing: `tests/tool_surface.py`'s property 5 fails a
    response model whose fields are undescribed AND which has no docstring of
    its own, and `tests/test_ai_tool_surface.py` pins that count.
    """

    id: UUID
    name: str
    slug: str
    status: str
    settings: dict[str, Any]


class OrgUpdate(BaseModel):
    """`extra="forbid"`: a body carrying `id`/`org_id` is a 422, not a no-op.

    Both fields are optional and `model_fields_set` distinguishes "absent"
    from "explicitly null", so PATCH is a genuine partial update.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    settings: dict[str, Any] | None = None


def _reject(message: str) -> HTTPException:
    # `HTTP_422_UNPROCESSABLE_CONTENT`, not the `..._ENTITY` spelling most of
    # this codebase still uses: Starlette deprecated the latter, and it emits a
    # warning on every rejected request.
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, message)


def _document_bytes(document: dict[str, Any]) -> int:
    """Compact, sorted JSON length. `default=str` never runs for a validated
    document -- it is here so that measuring an UNVALIDATED body (the first,
    cheap size gate) cannot itself raise on an exotic value."""
    return len(json.dumps(document, sort_keys=True, separators=(",", ":"), default=str))


def _normalised(key: str, value: object) -> object:
    """Strip surrounding whitespace off the one key where an invisible byte is
    a security decision rather than a cosmetic one.

    `console_email_domain` only. The presentation keys can hold whatever
    padding an operator typed -- it shows in the console and that is the whole
    consequence. This value is read by three admission doors, and
    `app.core.identity.email_domain.domain_refusal` already strips the base
    before comparing, which it learned the hard way: an earlier version
    compared against the RAW string, and one trailing newline off a copied
    `.env` line was a value no address could match, i.e. a refusal of EVERY
    user at ALL THREE doors from a byte nobody could see.

    Stripping at the SAVE path is not a second copy of that rule; it is what
    stops the padded byte existing at rest. The stored value is then the value
    that is enforced -- so the form shows what is in force, `_check_value`'s
    emptiness refusal below cannot be dodged with a space, and a future reader
    that forgets to strip still cannot disagree with the door.
    """
    if key == CONSOLE_DOMAIN_KEY and isinstance(value, str):
        return value.strip()
    return value


def _check_value(key: str, value: object, spec: SettingSpec) -> None:
    if spec.kind == "bool":
        if not isinstance(value, bool):
            raise _reject(tr("settings.{key} must be a boolean", key=key))
        return
    if spec.kind == "int":
        # `bool` is a subclass of `int`; excluded deliberately (see SettingSpec).
        if not isinstance(value, int) or isinstance(value, bool):
            raise _reject(tr("settings.{key} must be an integer", key=key))
        if spec.bounds is not None:
            low, high = spec.bounds
            if not low <= value <= high:
                raise _reject(tr(
                    "settings.{key} must be between {low} and {high}",
                    key=key,
                    low=low,
                    high=high,
                ))
        return
    if not isinstance(value, str):
        raise _reject(tr("settings.{key} must be a string", key=key))
    if key == CONSOLE_DOMAIN_KEY and not value:
        # THE ONE PLACE AN EMPTY STRING IS A SECURITY EVENT, AND THE CHOICE IS
        # TO REFUSE IT. (`value` is already stripped -- see `_normalised` --
        # so this catches `""` and `"   "` alike, which is required: the doors
        # strip the base too, so whitespace-only IS empty to them.)
        #
        # `domain_refusal` reads an empty base as NO RESTRICTION. That reading
        # is correct and is not what changes here -- it is what keeps an unset
        # `ITS_CONSOLE_EMAIL_DOMAIN=` from refusing an entire deployment. But
        # at THIS path it would mean an `orgs.manage` holder can switch the
        # allowlist off for their whole tenant by saving a blank form field,
        # and the self-lockout guard next door cannot object, because an
        # unrestricted gate refuses nobody -- least of all the person saving
        # it. A security control must not be disableable by a value that is
        # indistinguishable from a typo.
        #
        # `null` IS THE CLEAR, and it already exists: `merge_settings` removes
        # the key, the audit row records it under `settings_cleared` rather
        # than `settings_set`, and the effective value falls back to the
        # PLATFORM DEFAULT -- so an org-admin can stop overriding the
        # deployment's allowlist but cannot turn it off. Turning it off
        # everywhere stays the vendor's `ITS_CONSOLE_EMAIL_DOMAIN_OVERRIDE`.
        raise _reject(
            tr(
                "settings.{key} cannot be empty; send null to clear this organization's value "
                "and fall back to the platform default",
                key=key,
            )
        )
    if spec.max_length is not None and len(value) > spec.max_length:
        raise _reject(tr(
            "settings.{key} must be at most {max_length} characters",
            key=key,
            max_length=spec.max_length,
        ))
    if spec.choices is not None and value not in spec.choices:
        raise _reject(tr(
            "settings.{key} must be one of {choices}",
            key=key,
            choices=sorted(spec.choices),
        ))
    if key == "brand_primary_color" and not (
        len(value) == 7 and value[0] == "#" and all(c in _HEX_DIGITS for c in value[1:])
    ):
        raise _reject(tr("settings.brand_primary_color must be a #RRGGBB hex colour"))


def merge_settings(stored: dict[str, Any], supplied: dict[str, Any]) -> dict[str, Any]:
    """Validate `supplied` against the allowlist and merge it over `stored`.

    A key mapped to `null` is REMOVED (the console's "reset to default"); any
    other value replaces. Raises a 422 `HTTPException` on anything the
    allowlist does not describe -- including an EMPTY `console_email_domain`,
    for which `null` is the only way to stop overriding the platform default
    (see `_check_value`).

    Order matters. The size of the SUPPLIED document is checked first, so a
    multi-megabyte body is refused before any per-key work touches it; the
    MERGED document is checked last, because that is what actually lands in the
    column. Returns a NEW dict -- SQLAlchemy does not track in-place mutation
    of a JSONB dict, so an in-place update would silently not persist.
    """
    supplied_bytes = _document_bytes(supplied)
    if supplied_bytes > MAX_SETTINGS_BYTES:
        raise _reject(
            tr(
                "settings document is {supplied_bytes} bytes; the maximum is "
                "{max_settings_bytes}",
                supplied_bytes=supplied_bytes,
                max_settings_bytes=MAX_SETTINGS_BYTES,
            )
        )
    unknown = sorted(set(supplied) - set(SETTINGS_ALLOWLIST))
    if unknown:
        raise _reject(
            tr(
                "unknown settings key(s): {unknown}; the settable keys are {settings_allowlist}",
                unknown=unknown,
                settings_allowlist=sorted(SETTINGS_ALLOWLIST),
            )
        )
    # Normalise BEFORE validating and merging, so the value that is checked is
    # the value that lands in the column -- a check against one string and a
    # write of another is how a validated setting stops being the enforced one.
    cleaned = {key: _normalised(key, value) for key, value in supplied.items()}
    for key, value in cleaned.items():
        if value is None:
            continue
        _check_value(key, value, SETTINGS_ALLOWLIST[key])

    merged = dict(stored)
    for key, value in cleaned.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value

    merged_bytes = _document_bytes(merged)
    if merged_bytes > MAX_SETTINGS_BYTES:
        raise _reject(
            tr(
                "merged settings document would be {merged_bytes} bytes; the maximum is "
                "{max_settings_bytes}",
                merged_bytes=merged_bytes,
                max_settings_bytes=MAX_SETTINGS_BYTES,
            )
        )
    return merged


@router.get("/current", response_model=OrgOut)
async def get_current_org(
    request: Request,
    ctx: AuthContext = Depends(bound_auth_context),
) -> OrgOut:
    """The caller's OWN organization. Ungated beyond authentication.

    The console chrome shows the org name on every screen, so gating this on a
    permission would mean an operator with `devices.read` and nothing else
    could not render the page they are allowed to see. It exposes nothing an
    authenticated member of the org does not already have: the name and slug
    are their own tenant's, and `settings` can only ever contain allowlisted
    presentation keys that this same console wrote.

    `bound_auth_context`, not `auth_context` -- an ungated route still needs
    org_id/user_id bound as contextvars for its log lines, and
    `tests/test_bound_auth_context.py` sweeps for routes that forget.

    The `id == ctx.org_id` predicate is redundant with RLS on `organizations`
    (migration 0002 policies it on `id = current_org`) and is written anyway:
    RLS is the backstop, not the design, and a bare `SELECT` over a table with
    one row per org returns whatever the planner hands back first the moment
    this runs on a session without the GUC.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        org = (
            await session.execute(select(Organization).where(Organization.id == ctx.org_id))
        ).scalar_one_or_none()
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such organization"))
    return OrgOut(
        id=org.id, name=org.name, slug=org.slug, status=org.status, settings=dict(org.settings)
    )


@router.patch("/current", response_model=OrgOut)
async def update_current_org(
    body: OrgUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("orgs.manage")),
) -> OrgOut:
    """Update this org's `name` and/or `settings`. `orgs.manage`, server-side.

    The target is `ctx.org_id` -- from the validated token, never the body.
    """
    supplied = body.model_fields_set
    if not supplied:
        raise _reject(tr("no fields supplied; send name and/or settings"))

    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        org = (
            await session.execute(select(Organization).where(Organization.id == ctx.org_id))
        ).scalar_one_or_none()
        if org is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such organization"))

        metadata: dict[str, Any] = {"fields": sorted(supplied)}
        mcp_switch_changed_to: bool | None = None

        if "name" in supplied:
            if body.name is None:
                raise _reject(tr("name cannot be null"))
            metadata["name_from"] = org.name
            metadata["name_to"] = body.name
            org.name = body.name

        if "settings" in supplied:
            if body.settings is None:
                raise _reject(tr("settings cannot be null; send {{}} to change nothing"))
            merged = merge_settings(dict(org.settings), body.settings)
            # THE SELF-LOCKOUT GUARD. Only when the key is actually TOUCHED --
            # an operator editing the timezone must not be refused because of
            # a domain value they are not changing (and, on a break-glass
            # instance where the override is off, the effective value can
            # legitimately be one that would refuse them).
            #
            # Judged on the EFFECTIVE value the save would produce, never on
            # the string supplied: clearing the key does not mean "no
            # restriction", it means "fall back to the platform default",
            # which may be the very value that refuses the person clearing it.
            # A guard that inspected only the submitted string waves that
            # through and locks them out on the next request.
            #
            # `env={}` — the vendor override is DELIBERATELY IGNORED here.
            # The question is "would this configuration refuse you", not
            # "does it refuse you right now". A break-glass session, where the
            # override is off precisely because a stored value locked everyone
            # out, is the one session that can be signed in while the setting
            # refuses the person holding it — and if the guard deferred to the
            # override, that session could re-save the very lockout it exists
            # to undo, and nobody would find out until the override was
            # removed.
            if CONSOLE_DOMAIN_KEY in body.settings:
                lockout = self_lockout_refusal(
                    effective_domain(
                        merged,
                        platform_default=request.app.state.settings.console_email_domain,
                        env={},
                    ),
                    caller_email=ctx.claims.email,
                )
                if lockout is not None:
                    # Raised INSIDE the tenant transaction, so the UPDATE and
                    # the audit append below both roll back: a refused change
                    # is not a change and must not leave a record saying it
                    # was one.
                    raise _reject(lockout)
            # The MCP switch moving is its own audit event (MCP server design
            # §7), with its new value -- a boolean, never sensitive -- so
            # "who let AI apps in, and when" is one query. Only when the
            # EFFECTIVE value changes (`is True`, as the gate reads it):
            # re-saving the same value, or clearing an already-off switch, is
            # not a change.
            if MCP_SWITCH_KEY in body.settings:
                was_on = dict(org.settings).get(MCP_SWITCH_KEY) is True
                now_on = merged.get(MCP_SWITCH_KEY) is True
                if was_on != now_on:
                    mcp_switch_changed_to = now_on
            # A NEW dict, not an in-place edit: SQLAlchemy does not track
            # mutation inside a JSONB value, so mutating `org.settings` would
            # produce a 200 that persisted nothing.
            org.settings = merged
            # KEYS ONLY, never values -- see the module docstring's AUDIT note.
            metadata["settings_set"] = sorted(
                k for k, v in body.settings.items() if v is not None
            )
            metadata["settings_cleared"] = sorted(
                k for k, v in body.settings.items() if v is None
            )

        session.add(org)
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="org.updated",
            target_type="organization",
            target_id=str(ctx.org_id),
            metadata=metadata,
        )
        if mcp_switch_changed_to is not None:
            await audit_service.record(
                session,
                org_id=ctx.org_id,
                actor_type="user",
                actor_id=ctx.user_id,
                action="mcp.settings_changed",
                target_type="organization",
                target_id=str(ctx.org_id),
                metadata={"mcp_enabled": mcp_switch_changed_to},
            )
        await session.flush()
        return OrgOut(
            id=org.id,
            name=org.name,
            slug=org.slug,
            status=org.status,
            settings=dict(org.settings),
        )


# --------------------------------------------------------------------------- #
# The console email-domain preview
# --------------------------------------------------------------------------- #

# The list is capped so one response cannot carry a whole tenant's user table.
# The COUNTS are computed over every row regardless and stay exact, so a
# truncated answer still tells the operator the number that matters; `truncated`
# says which case they are looking at rather than leaving a short list to be
# read as a complete one.
MAX_PREVIEWED_USERS = 500


class RefusedUserOut(BaseModel):
    """One person a candidate console email domain would turn away."""

    email: str = Field(description="The address held on this user's ITMS record.")
    display_name: str | None = Field(
        description="The person's name as the console shows it, if it is known."
    )
    status: str = Field(description="The account's lifecycle state: active, disabled or deleted.")
    active: bool = Field(
        description=(
            "True when this refusal would actually cost somebody their access. A "
            "disabled or deleted account is already refused by the account-status "
            "rule, so the domain setting changes nothing for it."
        )
    )


class ConsoleDomainPreviewOut(BaseModel):
    """What saving a candidate console email domain would refuse.

    The in-product replacement for `scripts/preflight_email_domain.py`, which
    could only be run over SSH and only told an operator what they were about
    to do AFTER they thought to ask.
    """

    domain: str = Field(description="The candidate base domain this answer is about.")
    examined: int = Field(description="How many users in this organization were checked.")
    refused: list[RefusedUserOut] = Field(
        description=(
            f"The users this domain would refuse, by address, at most "
            f"{MAX_PREVIEWED_USERS} of them."
        )
    )
    blocking: int = Field(
        description=(
            "How many of the refused users are active, and would therefore lose "
            "access. Counted over every user, not only the listed ones."
        )
    )
    truncated: bool = Field(
        description="True when more users were refused than this answer lists."
    )
    would_refuse_you: bool = Field(
        description=(
            "True when this domain would refuse your own address. Saving such a "
            "value is refused outright."
        )
    )


@router.get("/current/console-domain-preview", response_model=ConsoleDomainPreviewOut)
async def preview_console_domain(
    request: Request,
    domain: str = Query(
        min_length=1,
        max_length=253,
        description="The base domain you are considering, e.g. toeshee.com.",
    ),
    ctx: AuthContext = Depends(require_permission("orgs.manage")),
) -> ConsoleDomainPreviewOut:
    """Who in this organization would be refused if `domain` were saved.

    `orgs.manage`, the same permission that can save the value -- a preview
    that a weaker principal could run would be a directory of every address in
    the tenant, handed out to answer a question they cannot act on.

    READ-ONLY AND NOT AUDITED. It changes nothing; the SAVE is the privileged
    action and it is what writes `org.updated` to the chain. Auditing a form
    keystroke would bury the events that matter under the ones that do not.

    The predicate is `app.core.identity.email_domain.domain_refusal` -- the
    same function the three admission doors call. The preflight script makes
    the point at length: a preview that re-expressed the rule as a SQL `LIKE`
    would say `eviltoeshee.com` is fine while the gate says it is not, and the
    operator would be told nobody is affected and lock somebody out anyway.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        examined = (
            await session.execute(
                select(func.count()).select_from(User).where(User.org_id == ctx.org_id)
            )
        ).scalar_one()
        refused = await users_refused_by(session, ctx.org_id, domain=domain)
    return ConsoleDomainPreviewOut(
        domain=domain,
        examined=examined,
        refused=[
            RefusedUserOut(
                email=row.email,
                display_name=row.display_name,
                status=row.status,
                active=row.active,
            )
            for row in refused[:MAX_PREVIEWED_USERS]
        ],
        blocking=sum(1 for row in refused if row.active),
        truncated=len(refused) > MAX_PREVIEWED_USERS,
        would_refuse_you=self_lockout_refusal(domain, caller_email=ctx.claims.email)
        is not None,
    )
