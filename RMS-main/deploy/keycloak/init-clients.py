#!/usr/bin/env python3
"""Configure the identity provider on first boot, so that nobody has to.

`docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §3.2.

THE PROBLEM THIS SOLVES. A freshly installed appliance needs two service-account
clients in the identity realm before it can provision a single user or read a
single audit event. Until now `deploy/README.md` asked the operator to open the
identity provider's own admin console, click both clients together, assign roles
by hand, copy two generated secrets into files on a shared volume over SSH, and
then -- on trust -- delete the bootstrap admin whose password is sitting in
`.env`. That is a second portal and three SSH sessions, which is precisely what
the buyer test forbids. This job does all of it, unattended, at
`docker compose up`.

IT IS IDEMPOTENT, AND THAT IS THE HARD PART. Compose starts a one-shot service
again on every `up`, so being re-run is this job's NORMAL case, not its
exceptional one. Everything here is written to converge on the desired state
from whatever partial state a previous run was interrupted in: clients are
created only if absent, roles granted only if missing, and secrets re-read and
re-written every time so that a restore which brought back the database but not
the secrets volume is repaired rather than reported as fine.

THE ORDER IS LOAD-BEARING. The last thing the job does is retire the bootstrap
admin by rotating its password to a random value only the secrets volume holds.
Doing that FIRST would destroy the only credential the job can authenticate
with, on a job that expects to run again tomorrow. `backend/tests/
test_keycloak_init_job.py` asserts the ordering as a position in the recorded
call sequence, because a refactor that hoists the rotation to the top reads
perfectly well and breaks nothing until the second deploy.

THE BOOTSTRAP ADMIN IS RETIRED, NOT DELETED. The identity provider needs one
master-realm account to exist or the realm cannot be repaired from outside
itself, and the vendor needs a break-glass door. What changes is that no human
knows its password and no runbook step uses it -- strictly stronger than the
previous posture, which was an instruction asking the operator to please delete
it.

WHAT IS TESTED AND WHAT IS NOT. Every decision below -- what to create, which
roles to grant, what to write where, in what order -- is a pure function or a
call against an injected `RealmAdmin`, and all of it is under test. The HTTP
verbs that carry those decisions to a live realm (`HttpRealmAdmin`) are not
unit-tested; since the MCP work (2026-10-15) the `deploy` CI job runs this job
against a real Keycloak 26.2 three times -- registration closed, then open,
then again asserting that every convergence line says `nothing` -- with
`deploy/keycloak/ci_mcp_flow.py` checking the result from outside.

Usage (see the `keycloak-init` service in `deploy/docker-compose.yml`):

    ITS_KEYCLOAK_URL=http://keycloak:8080 \
    ITS_KEYCLOAK_REALM=it-suite \
    KEYCLOAK_ADMIN_USER=admin \
    KEYCLOAK_ADMIN_PASSWORD=... \
    python /init/init-clients.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

# --- what the appliance needs to exist ------------------------------------


@dataclass(frozen=True)
class ServiceClient:
    """One service-account client, its authority, and where its secret lands."""

    client_id: str
    #: Roles taken from the realm's own `realm-management` client.
    roles: tuple[str, ...]
    #: Filename on the shared secrets volume. Must match the `ITS_KEYCLOAK_*
    #: _SECRET_PATH` values in `deploy/.env.example`, which is where the api,
    #: worker and gateway read them from.
    secret_filename: str


#: THE TWO CLIENTS, AND WHY THEY ARE TWO.
#:
#: Their worst cases are not comparable. `it-suite-provisioning` can create any
#: user and reset any password in the realm; `it-suite-events` can read an event
#: log. A single client holding both means the smaller job carries the bigger
#: blast radius for ever, so the split is deliberate and predates this job
#: (`backend/app/core/config.py`, `keycloak_admin_client_id`).
#:
#: `manage-realm` on provisioning is the 2026-08-19 widening, and it is
#: accounted for in the design doc's §4: ITMS now writes the realm's SMTP block
#: from Settings -> Email, which `manage-users` alone cannot do. The widening
#: touches THIS CLIENT ONLY -- the event sweep stays `view-events` and a test
#: asserts it, because the sentence "grant them both everything" is one line
#: away at all times and nothing else in the tree would notice.
DESIRED_CLIENTS: tuple[ServiceClient, ...] = (
    ServiceClient(
        client_id="it-suite-provisioning",
        roles=("manage-users", "manage-realm"),
        secret_filename="keycloak-admin.secret",
    ),
    ServiceClient(
        client_id="it-suite-events",
        roles=("view-events",),
        secret_filename="keycloak-events.secret",
    ),
)

#: Where the rotated bootstrap-admin password lives, and the ONLY place it ever
#: exists. Back this volume up (`deploy/GO-LIVE.md` §6): losing it and the
#: `.env` value together means the master account cannot be authenticated as by
#: anyone, including the vendor.
BOOTSTRAP_SECRET_FILENAME = "keycloak-bootstrap-admin.secret"

#: The shared secrets volume (`its-secrets:/var/lib/its/secrets`), where the
#: audit HMAC key and the install code already live.
DEFAULT_SECRETS_DIR = Path("/var/lib/its/secrets")

#: The realm file, bind-mounted read-only by the `keycloak-init` service. The
#: SAME file Keycloak itself imports on a fresh install -- one desired state,
#: two paths to it.
DEFAULT_REALM_FILE = Path("/init/realm-it-suite.json")

#: 43 characters of url-safe base64, ~256 bits. Nobody types this: it is
#: generated, written to a file and read by a machine, so it is sized against
#: offline guessing rather than against a human's patience.
_ROTATED_PASSWORD_BYTES = 32


# --- what the realm itself needs to be, added 2026-08-20 -------------------
#
# A KEYCLOAK REALM IS IMPORTED ONLY WHEN IT DOES NOT ALREADY EXIST. Every
# deployment past its first boot ignores `realm-it-suite.json` entirely, so a
# correction to that file reaches new installs and no existing one -- and
# nothing anywhere reports the divergence. Three failures on the operator's
# live install on 2026-08-20 were that single fact:
#
#   * "Change password" and "Add a passkey" did nothing. The realm file that
#     built that install lists ONE required action, and Keycloak's import does
#     not merge a supplied `requiredActions` array with the defaults -- if it
#     is present, it IS the set. `kc_action` naming an action the realm has
#     not registered is ignored, so the user was re-authenticated and returned,
#     silently, with nothing to see.
#   * Sign-out dead-ended on "Invalid redirect uri", because the live
#     `it-suite-web` client still carries `redirectUris: ["https://*/auth/
#     callback"]` -- Keycloak honours a wildcard only as a trailing `*` -- and
#     `post.logout.redirect.uris: "+"` resolves against that empty-matching set.
#   * The login theme, passkey policy and `editUsername` were right in the file
#     and absent from the realm.
#
# So the file is the desired state on both paths: imported on a fresh install,
# converged onto a running one, here, on every `docker compose up`.
#
# AN ALLOWLIST, NEVER THE WHOLE REPRESENTATION, and this is the constraint that
# shapes everything below. `Settings -> Email` writes the realm's `smtpServer`
# through this same client's `manage-realm` grant. A convergence that carried
# the file's whole realm representation would revert the operator's mail
# configuration on every deploy, and the symptom -- "email stopped working
# again" -- names nothing that would lead anyone back to the upgrade. Only the
# keys below are converged; everything an operator can set from inside the
# product is untouchable from here by construction.

#: Realm-level keys converged verbatim from the file. EXPLICIT NAMES ONLY
#: (2026-09-23): a key not listed here is never written, whatever the file
#: says. `test_every_passkey_policy_key_in_the_file_is_on_the_allowlist` fails
#: if the file gains a policy key this list does not name.
CONVERGED_REALM_KEYS: tuple[str, ...] = (
    # The ITMS sign-in branding. Realm-level, so the themes bind-mount alone
    # does not select it.
    "loginTheme",
    # THE SIGN-IN PAGES' LANGUAGES (spec 2026-10-09 §3.5). Three keys, one
    # decision: Keycloak offers `en` and `es`, and falls back to `en`.
    #
    #   internationalizationEnabled  Off, Keycloak ignores every locale signal
    #       (the user's `locale` attribute, the console's `ui_locales`, the
    #       browser's Accept-Language) and renders English, and `locale` is not
    #       a user-profile attribute at all, so the admin API silently drops it
    #       (the realm sets no `unmanagedAttributePolicy`). So this is also
    #       what lets `PATCH /me` mirror a person's language to their
    #       Keycloak account (`kc_client.set_locale_attribute`): Keycloak 26.2
    #       adds `locale` to the admin API's user profile by itself once this
    #       is true (`DeclarativeUserProfileProviderFactory`, USER_API context),
    #       which is why the realm file's `kc.user.profile.config` does NOT
    #       list it and need not be converged.
    #   supportedLocales             The two the console has catalogues for,
    #       and the only two `users.locale` ever holds. A third offered here
    #       would be a language a person could pick on the sign-in page and
    #       never see again after signing in.
    #   defaultLocale                What a visitor Keycloak knows nothing about
    #       gets, and what an email to a user with no `locale` is written in
    #       (the email theme ignores Accept-Language).
    #
    # CONVERGED, NOT JUST SHIPPED: a realm that predates this has all three
    # unset, and the realm file is never read again after first boot.
    "internationalizationEnabled",
    "supportedLocales",
    "defaultLocale",
    # Whether a user may change their own username. The console's profile page
    # states that sign-in address changes are refused here; that is a claim
    # about this setting.
    "editUsername",
    # The name Keycloak prints above every page of the flow. Unset, it falls
    # back to the realm's ID -- so the operator's first themed login screen
    # read "it-suite" above "ITMS": an internal identifier stacked on the
    # product's own name, on the first screen a buyer sees.
    "displayName",
    "displayNameHtml",
    # Security posture the file declares: no self-registration, lockout on.
    "registrationAllowed",
    "bruteForceProtected",
    # The passkey policy.
    "webAuthnPolicyPasswordlessRpEntityName",
    "webAuthnPolicyPasswordlessRpId",
    "webAuthnPolicyPasswordlessSignatureAlgorithms",
    "webAuthnPolicyPasswordlessAttestationConveyancePreference",
    "webAuthnPolicyPasswordlessAuthenticatorAttachment",
    "webAuthnPolicyPasswordlessRequireResidentKey",
    "webAuthnPolicyPasswordlessUserVerificationRequirement",
    "webAuthnPolicyPasswordlessCreateTimeout",
    "webAuthnPolicyPasswordlessAvoidSameAuthenticatorRegister",
    "webAuthnPolicyPasswordlessAcceptableAaguids",
    "webAuthnPolicyPasswordlessExtraOrigins",
)

#: Realm keys written at RUNTIME by someone other than this repo. Never on the
#: list above, and a test asserts the two are disjoint:
#:   smtpServer          Settings -> Email (the provisioning client)
#:   events*/adminEvents* switched on by hand ("Forwarding Keycloak's
#:                       credential events" in deploy/README.md)
#:   browserFlow         bound by `converge_passkey_flow`, behind its own
#:                       lockout check, never copied from the file
RUNTIME_OWNED_REALM_KEYS: tuple[str, ...] = (
    "smtpServer",
    "eventsEnabled",
    "eventsExpiration",
    "eventsListeners",
    "enabledEventTypes",
    "adminEventsEnabled",
    "adminEventsDetailsEnabled",
    "browserFlow",
)

#: The browser client, whose redirect URIs this job corrects but which it
#: never CREATES -- see `converge_client`.
WEB_CLIENT_ID = "it-suite-web"

#: The browser flow that offers a passkey, and the alias it is bound under.
#: A COPY of Keycloak's stock `browser`, so the password leg is whatever the
#: vendor shipped rather than a graph reconstructed here.
PASSKEY_FLOW_ALIAS = "browser-passkey"
STOCK_BROWSER_FLOW = "browser"

#: The execution that accepts a passkey at sign-in. Distinct from the
#: `webauthn-register-passwordless` REQUIRED ACTION converged above: that one
#: puts a credential on an account, this one is what will accept it. The
#: operator had the first and not the second, which is why a passkey they had
#: enrolled was not offered on the login page.
PASSKEY_AUTHENTICATOR = "webauthn-authenticator-passwordless"

#: Executions that can sign somebody in with a password.
_PASSWORD_AUTHENTICATORS = frozenset(
    {"auth-username-password-form", "auth-password-form"}
)

#: Requirements under which an execution can actually run.
_LIVE_REQUIREMENTS = frozenset({"REQUIRED", "ALTERNATIVE", "CONDITIONAL"})

@dataclass(frozen=True)
class OwnedClient:
    """A client whose listed fields the repo owns. Corrected, never created.

    `attributes` is merged key-by-key rather than replaced: Keycloak keeps
    unrelated per-client state in there.
    """

    client_id: str
    keys: tuple[str, ...]
    attributes: tuple[str, ...] = ()


CONVERGED_CLIENTS: tuple[OwnedClient, ...] = (
    OwnedClient(
        WEB_CLIENT_ID,
        keys=("rootUrl", "redirectUris", "directAccessGrantsEnabled"),
        attributes=("post.logout.redirect.uris", "pkce.code.challenge.method"),
    ),
    # Closed deliberately; the live host kept it OPEN because this was not
    # converged (deploy/README.md "Realm drift").
    OwnedClient("account-console", keys=("enabled",)),
    # Kept enabled: the action-token mail resolves it as the system client.
    OwnedClient("account", keys=("enabled",)),
)


# --- what MCP needs of the realm, added 2026-10-14 -------------------------
#
# `docs/superpowers/specs/2026-10-09-mcp-server-design.md` §3.2-§3.3. Any
# MCP-capable AI app registers itself ANONYMOUSLY (dynamic client
# registration) and runs code+PKCE for a token whose audience is
# `<ITS_PUBLIC_CONSOLE_URL>/mcp` -- never the console's `it-suite`. Opening
# anonymous registration is the most permissive thing this job has done to the
# realm, so everything that narrows it is converged here on every deploy.
#
# EVERY KEYCLOAK ID BELOW WAS READ FROM THE 26.2.5 SOURCES, not remembered
# (`keycloak-services`, `keycloak-server-spi(-private)`,
# `keycloak-model-storage-private`, `keycloak-common`, all 26.2.5 `-sources`
# jars from Maven Central). Paths are under `org/keycloak/`:
#
#   registration policies  services/clientregistration/policy/impl/
#     trusted-hosts             TrustedHostClientRegistrationPolicyFactory:37
#     consent-required          ConsentRequiredClientRegistrationPolicyFactory:34
#     allowed-client-templates  ClientScopesClientRegistrationPolicyFactory:41
#       allowed-client-scopes     ...:43     allow-default-scopes  ...:45
#     allowed-protocol-mappers  ProtocolMappersClientRegistrationPolicyFactory:40
#       allowed-protocol-mapper-types ...:42
#     scope ("Full Scope Disabled") ScopeClientRegistrationPolicyFactory:34
#     max-clients               MaxClientsClientRegistrationPolicyFactory:53
#       max-clients (config)      ...:37
#     subType "anonymous"       ClientRegistrationPolicyManager:135-136
#                               (`RegistrationAuth.ANONYMOUS` lower-cased)
#   client-policy executors  services/clientpolicy/executor/
#     pkce-enforcer             PKCEEnforcerExecutorFactory:33, auto-configure :35
#     reject-implicit-grant     RejectImplicitGrantExecutorFactory:33
#     reject-ropc-grant         RejectResourceOwnerPasswordCredentialsGrantExecutorFactory:33
#     secure-redirect-uris-enforcer  SecureRedirectUrisEnforcerExecutorFactory:30,
#                               config keys :32-41
#   client-policy condition  services/clientpolicy/condition/
#     client-updater-context    ClientUpdaterContextConditionFactory:32
#       update-client-source      ...:34   ByAnonymous :37
#       ByRegistrationAccessToken :39
#   mappers                  protocol/oidc/mappers/
#     oidc-audience-mapper      AudienceProtocolMapper:75,
#                               included.custom.audience :44
#     oidc-sub-mapper           SubMapper:41 (an access-token mapper, :38)
#     access.token.claim / id.token.claim  OIDCAttributeMapperHelper:57,60
#     introspection.token.claim OIDCAttributeMapperHelper:71
#   scope attributes         models/ClientScopeModel (server-spi):65,68
#   fix round 1:
#     client-scope PUT          resources/admin/ClientScopesResource:138 ->
#                               ClientScopeResource:111; attributes are set key
#                               by key (RepresentationToModel.updateClientScope:732)
#   fix round 3:
#     registration providers    services/clientregistration/: `default`
#                               DefaultClientRegistrationProviderFactory:48 (takes
#                               the registrant's clientId, DefaultClientRegistration-
#                               Provider:50), `install` AdapterInstallation...Factory:48,
#                               `openid-connect` oidc/OIDCClientRegistrationProviderFactory
#                               :31 (refuses a client_id, OIDCClientRegistrationProvider
#                               :78), `saml2-entity-descriptor` protocol/saml/
#                               clientregistration/EntityDescriptor...Factory:31,
#                               `oid4vc` protocol/oid4vc/OID4VCClientRegistration-
#                               ProviderFactory:60. Only `openid-connect` is reachable
#                               from outside (docker-compose.yml, Traefik).
#     trusted-hosts config      TrustedHostClientRegistrationPolicyFactory:39-42, stock
#                               values DefaultClientRegistrationPolicies:78-83

#: The client scope a registered app requests, and its three mappers.
MCP_SCOPE = "mcp"
MCP_AUDIENCE_MAPPER = "mcp-audience"
ORG_CLAIM_MAPPER = "org-claim"
#: `sub` from the scope itself. Keycloak's stock `sub` mapper lives in the
#: `basic` scope (OIDCLoginProtocolFactory:428-440), and an app whose
#: registration names its own `scope` list loses every other scope from its
#: client, `basic` included (RepresentationToModel.updateClientScopes:636-660).
#: A token for `mcp` must name its person whatever the app registered with.
MCP_SUB_MAPPER = "mcp-sub"

#: The attributes this job owns on the scope, converged on an existing one.
#:
#: `consent.screen.text` is the one line the consent screen shows for `mcp`,
#: and the consent screen is the only check between a sign-in link someone
#: was sent and that person's whole authority (deploy/README.md, "Residual
#: risks"). So it says what approving hands over. A `${key}`, not the
#: sentence: the stock `login-oauth-grant.ftl` renders it through
#: `advancedMsg`, which looks `${key}` up in the login theme's bundles
#: (`AdvancedMessageFormatterMethod`, 26.2.5), so the `itms` theme's
#: `messages_en/es.properties` word it in the person's language.
MCP_SCOPE_ATTRIBUTES: dict[str, str] = {
    "include.in.token.scope": "true",
    "display.on.consent.screen": "true",
    "consent.screen.text": "${itmsMcpConsent}",
}

#: `it-suite-web`'s own `org-claim` config, which the realm file carries per
#: client. The SAME claim read the same way, or `/mcp` and `/api` disagree
#: about which organization a person belongs to; a test holds the two equal.
ORG_CLAIM_CONFIG: dict[str, str] = {
    "user.attribute": "org",
    "claim.name": "org",
    "jsonType.label": "String",
    "access.token.claim": "true",
    "id.token.claim": "true",
}

REGISTRATION_POLICY_TYPE = (
    "org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy"
)
ANONYMOUS = "anonymous"

#: Anonymous registration policies REMOVED when registration opens. Trusted
#: Hosts with Keycloak's stock empty list refuses every anonymous
#: registration; removing it is the operator's choice (spec Q4: any AI app may
#: register) -- but only once ITS_PUBLIC_CONSOLE_URL is set and MCP can work
#: at all (fix round 3). Until then it is kept, and restored if missing.
REMOVED_REGISTRATION_POLICIES: tuple[str, ...] = ("trusted-hosts",)

#: THE OPERATOR'S OPT-IN (operator decision, 2026-10-15). A console URL is set
#: on every real deployment for other reasons (mail links, push install), so
#: it is no evidence that anybody chose to let strangers register apps in the
#: realm. Registration opens only when `ITS_MCP_REGISTRATION` is exactly this
#: word as well; anything else, unset included, keeps it closed.
REGISTRATION_OPT_IN = "open"


def registration_opted_in(value: str) -> bool:
    """`ITS_MCP_REGISTRATION` says `open`, and nothing else. Surrounding
    whitespace is forgiven (a trailing newline in `.env` is not a decision);
    any other spelling is not, because the safe reading of a typo is closed."""
    return (value or "").strip() == REGISTRATION_OPT_IN


def registration_blockers(public_url: str, registration: str) -> list[str]:
    """The settings still missing before registration may open, by name;
    empty when it may."""
    missing: list[str] = []
    if mcp_resource(public_url) is None:
        missing.append("ITS_PUBLIC_CONSOLE_URL is set")
    if not registration_opted_in(registration):
        missing.append(f"ITS_MCP_REGISTRATION={REGISTRATION_OPT_IN}")
    return missing

#: The client profile and the client policy that applies it, one name for both.
#:
#: LESSON KEPT IN `desired_client_policies` (fix round 3): a policy this job
#: owns is gated on a condition that votes only on registration events. An
#: earlier draft of this branch -- never on main, never deployed -- paired
#: `grant-type` with `client-scopes` in front of `reject-request` and refused
#: EVERY MCP token, because `DefaultClientPolicyManager.isSatisfied`
#: (:93-118) skips an abstention and is satisfied by one yes with no no.
MCP_CLIENT_PROFILE = "itms-mcp-apps"

#: The console's token audience (`ITS_OIDC_AUDIENCE`, `app/core/config.py`
#: `oidc_audience`), and the clientIds this job RESERVES (fix round 3).
#:
#: Keycloak names the requesting clientId in an ID token's `aud`. A
#: registration that let an app choose its clientId could therefore mint
#: tokens carrying the console's audience. `openid-connect` registration
#: refuses a chosen client_id and the edge closes every other provider, but
#: the name is also simply TAKEN: a disabled client with no flows holds it,
#: and an app that squatted it before this ran is switched off in place. The
#: MCP resource string is reserved the same way.
DEFAULT_CONSOLE_AUDIENCE = "it-suite"

#: Keycloak's per-realm built-in clients (`RealmManager.setupRealmDefaults`
#: and friends).
KEYCLOAK_BUILTIN_CLIENTS: tuple[str, ...] = (
    "account",
    "account-console",
    "admin-cli",
    "broker",
    "realm-management",
    "security-admin-console",
)


#: What no reservation may ever name: the console's own client, every client
#: this job creates or corrects, and Keycloak's built-ins.
PROTECTED_CLIENT_IDS: frozenset[str] = frozenset(
    {
        WEB_CLIENT_ID,
        *(c.client_id for c in DESIRED_CLIENTS),
        *(c.client_id for c in CONVERGED_CLIENTS),
        *KEYCLOAK_BUILTIN_CLIENTS,
    }
)


class ReservationRefused(ValueError):
    """A reservation named a client the platform or Keycloak itself needs."""

#: The fields of a reservation this job owns, converged on an existing client.
RESERVATION_KEYS: tuple[str, ...] = (
    "enabled",
    "bearerOnly",
    "publicClient",
    "standardFlowEnabled",
    "implicitFlowEnabled",
    "directAccessGrantsEnabled",
    "serviceAccountsEnabled",
    "redirectUris",
    "webOrigins",
)

#: Keycloak's stock Trusted Hosts policy: no host trusted, so every anonymous
#: registration is refused (DefaultClientRegistrationPolicies:78-83). Present
#: until `ITS_PUBLIC_CONSOLE_URL` is set; the realm file ships it, so a fresh
#: install is closed until keycloak-init opens it.
STOCK_TRUSTED_HOSTS: dict[str, Any] = {
    "name": "Trusted Hosts",
    "providerId": "trusted-hosts",
    "providerType": "org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy",
    "subType": "anonymous",
    "config": {
        "trusted-hosts": [],
        "host-sending-registration-request-must-match": ["true"],
        "client-uris-must-match": ["true"],
    },
}

#: Max Clients. Keycloak counts EVERY client in the realm against it, the
#: platform's own included (MaxClientsClientRegistrationPolicy:45-48,
#: `realm.getClientsCount()`): six per-realm built-ins, `it-suite-web` and the
#: two service clients -- nine -- so about 190 are left for registered apps.
#: A test holds that margin.
MAX_CLIENTS = 200

#: Where a redirect URI of a registered app may point. Controller decision,
#: 2026-10-14: native MCP clients' loopback http callbacks are allowed.
#:
#: HTTP IS ALLOWED FOR ANY HOST, because Keycloak 26.2's
#: secure-redirect-uris-enforcer cannot restrict it to loopback: one flag,
#: `allow-http-scheme`, gates both the loopback check
#: (SecureRedirectUrisEnforcerExecutor:365-368, `isValidLoopbackAddress`) and
#: the ordinary-host check (:453-456, `isValidNormalUri`). An http redirect to
#: a non-loopback host exposes only a PKCE-bound code to the network, which
#: cannot be redeemed without the registering app's verifier: `pkce-enforcer`
#: in the same profile writes S256 onto every registered app, and consent is
#: required, so the person still sees which app they are letting in.
#:
#:   accepted  http://localhost:<port>/... and http://127.0.0.1:<port>/... --
#:             the RFC 8252 §7.3 callback desktop and CLI MCP clients register
#:             (`localhost` counts as loopback because the executor resolves
#:             it, :319-326); [::1] likewise; any https callback, such as
#:             https://claude.ai/api/mcp/auth_callback; a private-use scheme
#:             such as `cursor://...` (RFC 8252 §7.1); and, as the stated
#:             cost, an http:// callback on an ordinary host.
#:   refused   wildcards (`allow-wildcard-context-path` off, `isIncludeWildcard`)
#:             and anything-goes (`allow-open-redirect` off); a URI that does
#:             not parse, or whose scheme is neither http, https nor a
#:             private-use scheme.
#:   NOT enforced, and why: fragments. Only `oauth-2-1-compliant` refuses them,
#:             and that mode also refuses `localhost` by name, any port in a
#:             registered loopback URI and every non-https ordinary host --
#:             the very callbacks this allows. So it stays off.
MCP_REDIRECT_URI_POLICY: dict[str, str] = {
    "allow-ipv4-loopback-address": "true",
    "allow-ipv6-loopback-address": "true",
    "allow-http-scheme": "true",
    "allow-private-use-uri-scheme": "true",
}


# --- pure decisions about the MCP surfaces -----------------------------------


def mcp_resource(public_url: str) -> str | None:
    """`<ITS_PUBLIC_CONSOLE_URL>/mcp`, normalised exactly as the api does
    (`app.core.mcp_channel.mcp_resource`), or `None` when the URL is unset.
    The audience a token carries must equal the resource `/mcp` checks
    byte-for-byte; a doubled slash is a refusal of every token."""
    base = (public_url or "").strip().rstrip("/")
    return f"{base}/mcp" if base else None


def desired_mcp_scope(public_url: str) -> dict[str, Any]:
    """The `mcp` client scope, with its mappers, as the admin API takes it.

    WITHOUT A CONSOLE URL THERE IS NO AUDIENCE MAPPER, rather than a mapper
    naming a guess. `/mcp` answers 503 in that state anyway (spec §2), and a
    token with no MCP audience is refused there -- the failure is closed, and
    `mcp_summary` says why.
    """
    mappers: list[dict[str, Any]] = []
    resource = mcp_resource(public_url)
    if resource is not None:
        mappers.append(
            {
                "name": MCP_AUDIENCE_MAPPER,
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "config": {
                    "included.custom.audience": resource,
                    "access.token.claim": "true",
                    "id.token.claim": "false",
                },
            }
        )
    mappers.append(
        {
            "name": ORG_CLAIM_MAPPER,
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-attribute-mapper",
            "config": dict(ORG_CLAIM_CONFIG),
        }
    )
    # The same config Keycloak gives its own `sub` mapper
    # (OIDCLoginProtocolFactory:239, `SubMapper.create(SUBJECT, true, true)`).
    mappers.append(
        {
            "name": MCP_SUB_MAPPER,
            "protocol": "openid-connect",
            "protocolMapper": "oidc-sub-mapper",
            "config": {
                "access.token.claim": "true",
                "introspection.token.claim": "true",
            },
        }
    )
    return {
        "name": MCP_SCOPE,
        "protocol": "openid-connect",
        "description": "ITMS over MCP: lets an AI app act as you, within your role.",
        "attributes": dict(MCP_SCOPE_ATTRIBUTES),
        "protocolMappers": mappers,
    }


def scope_changes(
    existing: Mapping[str, Any], desired: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The representation to PUT on an existing `mcp` scope, or `None`.

    Only the protocol and this job's own attributes are compared. Attributes
    are MERGED: Keycloak keeps others there (`gui.order`, a consent text an
    operator wrote) and its update sets them key by key anyway.
    """
    attributes = dict(existing.get("attributes") or {})
    if existing.get("protocol") == desired["protocol"] and all(
        attributes.get(k) == v for k, v in desired["attributes"].items()
    ):
        return None
    return {
        **{k: v for k, v in existing.items() if k != "protocolMappers"},
        "protocol": desired["protocol"],
        "attributes": {**attributes, **desired["attributes"]},
    }


def mapper_changes(
    existing: Sequence[Mapping[str, Any]], desired: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[tuple[str, dict[str, Any]]]]:
    """Mappers to create, and `(id, representation)` pairs to correct.

    Matched by NAME. A mapper is correct when its type and every config key
    this job owns already hold the desired value; keys Keycloak keeps that are
    not ours (`introspection.token.claim`, ...) are neither compared nor
    dropped. Never deletes: a mapper an operator added is theirs.
    """
    by_name = {str(m.get("name")): m for m in existing}
    to_create: list[dict[str, Any]] = []
    to_update: list[tuple[str, dict[str, Any]]] = []
    for want in desired:
        have = by_name.get(str(want["name"]))
        if have is None:
            to_create.append(dict(want))
            continue
        have_config = dict(have.get("config") or {})
        if have.get("protocolMapper") == want["protocolMapper"] and all(
            have_config.get(k) == v for k, v in want["config"].items()
        ):
            continue
        to_update.append(
            (
                str(have["id"]),
                {**have, **want, "config": {**have_config, **want["config"]}},
            )
        )
    return to_create, to_update


def desired_registration_policies() -> list[dict[str, Any]]:
    """The anonymous client-registration policies, as components.

    Spec §3.3, item by item:

      * Consent Required -- the consent screen names the app.
      * Allowed Client Scopes -- `mcp`, `openid` and `profile` by name, plus
        the realm's defaults (which the authorization still has to request).
        Never `it-suite`'s audience: that lives on `it-suite-web` as a
        per-client mapper, not in any scope a registrant can name.
      * Allowed Protocol Mappers -- NONE. Keycloak's stock list includes
        `oidc-usermodel-attribute-mapper`, which would let an app write a
        claim named `org` from an attribute the person can edit; an empty
        list allows no type (`MultivaluedMap.getList` reads absent as empty).
      * Full Scope Disabled -- the `scope` provider.
      * Max Clients -- `MAX_CLIENTS`. Keycloak counts EVERY client in the realm
        (`realm.getClientsCount()`), built-ins included.
    """

    def policy(name: str, provider: str, config: dict[str, list[str]]) -> dict[str, Any]:
        return {
            "name": name,
            "providerId": provider,
            "providerType": REGISTRATION_POLICY_TYPE,
            "subType": ANONYMOUS,
            "config": config,
        }

    return [
        policy("Consent Required", "consent-required", {}),
        policy(
            "Allowed Client Scopes",
            "allowed-client-templates",
            {
                "allowed-client-scopes": [MCP_SCOPE, "openid", "profile"],
                "allow-default-scopes": ["true"],
            },
        ),
        policy(
            "Allowed Protocol Mapper Types",
            "allowed-protocol-mappers",
            {"allowed-protocol-mapper-types": []},
        ),
        policy("Full Scope Disabled", "scope", {}),
        policy("Max Clients Limit", "max-clients", {"max-clients": [str(MAX_CLIENTS)]}),
    ]


def _config_values(config: Mapping[str, Any], key: str) -> list[str]:
    """One component config key as Keycloak means it: a multi-valued list,
    order-free, where an empty list and an absent key are the same thing --
    Keycloak stores no row for either and reads both back absent."""
    raw = config.get(key)
    if raw is None:
        return []
    values = raw if isinstance(raw, list) else [raw]
    return sorted(str(v) for v in values if v is not None and str(v).strip())


def registration_policy_changes(
    existing_components: Sequence[Mapping[str, Any]],
    desired: Sequence[Mapping[str, Any]],
    *,
    open_registration: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """`(to_create, to_update, to_delete)` for the ANONYMOUS policies only.

    `open_registration` decides Trusted Hosts (fix round 3): removed when
    true, and when false PRESENT -- re-created first, ahead of every other
    write, if something removed it. Its config is never corrected: an
    operator who trusted a host chose that. It is a required keyword, not a
    default, because either default silently decides whether strangers may
    register.

    Matched by provider id. Authenticated-registration policies are not this
    job's and are never read as candidates. A second component with the same
    provider is left alone: registration policies only ever REFUSE, every one
    of them runs, so a duplicate can narrow registration and never widen it.
    `to_update` entries carry the whole component with the desired keys
    written over it, because the admin API removes a key sent empty and keeps
    a key not sent (`RepresentationToModel.updateComponent`).
    """
    anonymous = [c for c in existing_components if c.get("subType") == ANONYMOUS]
    to_delete: list[str] = []
    to_create: list[dict[str, Any]] = []
    trusted = [c for c in anonymous if c.get("providerId") in REMOVED_REGISTRATION_POLICIES]
    if open_registration:
        to_delete = [str(c["id"]) for c in trusted]
    elif not trusted:
        to_create.append(json.loads(json.dumps(STOCK_TRUSTED_HOSTS)))
    to_update: list[dict[str, Any]] = []
    for want in desired:
        have = next((c for c in anonymous if c.get("providerId") == want["providerId"]), None)
        if have is None:
            to_create.append(dict(want))
            continue
        have_config = dict(have.get("config") or {})
        if all(
            _config_values(have_config, k) == _config_values(want["config"], k)
            for k in want["config"]
        ):
            continue
        to_update.append({**have, "config": {**have_config, **want["config"]}})
    return to_create, to_update, to_delete


def desired_client_profiles() -> list[dict[str, Any]]:
    """The client profile this job owns.

    `itms-mcp-apps`: public clients, authorization code with PKCE only (spec
    §3.3). `auto-configure` on all three grant executors: the registration is
    CORRECTED rather than refused, so an app that asks for nothing in
    particular still ends up with S256 PKCE, no implicit flow and no password
    grant written onto its client -- which is what keeps enforcing them at
    sign-in, since the policy's condition only votes on registration events.
    """
    return [
        {
            "name": MCP_CLIENT_PROFILE,
            "description": "AI apps registered over MCP: code flow with PKCE only.",
            "executors": [
                {"executor": "pkce-enforcer", "configuration": {"auto-configure": "true"}},
                {
                    "executor": "reject-implicit-grant",
                    "configuration": {"auto-configure": "true"},
                },
                {"executor": "reject-ropc-grant", "configuration": {"auto-configure": "true"}},
                {
                    "executor": "secure-redirect-uris-enforcer",
                    "configuration": dict(MCP_REDIRECT_URI_POLICY),
                },
            ],
        },
    ]


def desired_client_policies() -> list[dict[str, Any]]:
    """The client policy this job owns.

    `itms-mcp-apps` applies the profile to every anonymous registration AND to
    every update an app makes to itself afterwards. `ByRegistrationAccessToken`
    IS NOT OPTIONAL. Registration hands the app a registration access token
    (RFC 7592), and Keycloak labels a request made with it
    `ByRegistrationAccessToken`, never `ByAnonymous`
    (`ClientUpdaterContextCondition.isAuthMethodMatched`). The anonymous
    registration policies above do not look at grant types, so with
    `ByAnonymous` alone an app could register cleanly and then turn on the
    password grant for itself one request later. No client in this realm is
    created with an initial access token, so the wider match reaches only the
    clients this policy exists for.

    HOW KEYCLOAK COMBINES CONDITIONS, because getting it wrong once refused
    every MCP sign-in (see `MCP_CLIENT_PROFILE`): an ABSTAIN is skipped,
    any NO fails the policy, and one YES with no NO satisfies it
    (`DefaultClientPolicyManager.isSatisfied`, :93-118). `client-updater-
    context` votes only on REGISTER/UPDATE/REGISTERED/UPDATED and abstains on
    everything else (ClientUpdaterContextCondition:72-82), so this policy can
    never fire at sign-in or at the token endpoint. Any condition added beside
    it that votes on a runtime event would make the policy fire THERE, alone;
    a test pins the condition list.
    """
    return [
        {
            "name": MCP_CLIENT_PROFILE,
            "description": "Every app registered anonymously, and its own later updates.",
            "enabled": True,
            "conditions": [
                {
                    "condition": "client-updater-context",
                    "configuration": {
                        "update-client-source": ["ByAnonymous", "ByRegistrationAccessToken"]
                    },
                }
            ],
            "profiles": [MCP_CLIENT_PROFILE],
        },
    ]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _merged_by_name(
    existing: Sequence[Mapping[str, Any]], wanted: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]] | None:
    """The list with each of `wanted` in place of the entry of the same name
    (appended if absent), or `None` if nothing changes. Every other entry --
    an operator's own profile or policy -- is kept, in order: the admin API's
    PUT replaces the realm's whole list."""
    merged = [dict(e) for e in existing]
    changed = False
    for want in wanted:
        index = next((i for i, e in enumerate(merged) if e.get("name") == want["name"]), None)
        if index is None:
            merged.append(dict(want))
            changed = True
        elif not all(
            _canonical(merged[index].get(k)) == _canonical(v) for k, v in want.items()
        ):
            merged[index] = {**merged[index], **want}
            changed = True
    return merged if changed else None


def client_policy_changes(
    existing_profiles: Mapping[str, Any], existing_policies: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Up to two PUTs, in order, each `None` when it would change nothing:

      1. profiles with ours in place, so the policy in 2 can name them;
      2. policies with ours in place.

    Keycloak refuses a policy that names a profile it does not have, hence
    the order. Every other profile and policy is kept. Takes and returns the
    admin API's own shapes, `{"profiles": [...]}` and `{"policies": [...]}`.
    """
    profiles = _merged_by_name(existing_profiles.get("profiles") or [], desired_client_profiles())
    policies = _merged_by_name(existing_policies.get("policies") or [], desired_client_policies())
    return (
        None if profiles is None else {"profiles": profiles},
        None if policies is None else {"policies": policies},
    )


def mcp_summary(changed: Sequence[str], public_url: str, registration: str) -> list[str]:
    """What the job prints about MCP. Names what changed, never a value, and
    while registration is closed, which setting is still missing."""
    lines = [f"identity init: mcp converged={list(changed) or 'nothing'}"]
    if mcp_resource(public_url) is None:
        lines.append(
            "identity init: ITS_PUBLIC_CONSOLE_URL is not set, so the mcp scope has no "
            "audience and AI apps cannot connect over MCP; set it in .env and re-run"
        )
    missing = registration_blockers(public_url, registration)
    if missing:
        lines.append(
            "identity init: anonymous client registration stays closed (Trusted Hosts "
            f"kept) until {' and '.join(missing)}; read deploy/README.md \"AI connections "
            '(MCP)\" before opening it'
        )
    return lines


def reserved_client(client_id: str, purpose: str) -> dict[str, Any]:
    """A clientId held so nobody else can register it: disabled, bearer-only,
    no flow of any kind, nowhere to redirect to."""
    return {
        "clientId": client_id,
        "name": f"Reserved: {purpose}",
        "description": (
            f"RESERVED by the ITMS init job: {purpose}. Holds this clientId so no "
            "registered app can take it. Never enable it."
        ),
        "enabled": False,
        "protocol": "openid-connect",
        "bearerOnly": True,
        "publicClient": False,
        "standardFlowEnabled": False,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
        "redirectUris": [],
        "webOrigins": [],
    }


def desired_reservations(public_url: str, console_audience: str) -> list[dict[str, Any]]:
    """The console's audience always; the MCP resource when it is known.

    RAISES `ReservationRefused` WHEN ONE WOULD NAME A CLIENT THAT MUST WORK.
    A reservation disables the client it names and strips every flow from
    it, in place. `ITS_OIDC_AUDIENCE=it-suite-web` -- an easy slip, since the
    console's client and its audience differ by four letters -- would switch
    the console's own sign-in off; `realm-management` or `admin-cli` would
    take the admin API, and this job, down with it. So the set is checked
    here, and `run_init` and `main` call this before anything is written.
    """
    reservations = [reserved_client(console_audience, "the console's token audience")]
    resource = mcp_resource(public_url)
    if resource is not None:
        reservations.append(reserved_client(resource, "the MCP resource"))
    refused = sorted({str(r["clientId"]) for r in reservations} & PROTECTED_CLIENT_IDS)
    if refused:
        raise ReservationRefused(
            f"refusing to reserve {', '.join(refused)}: a reservation disables the client "
            "it names, and this one belongs to the platform or to Keycloak. Check "
            "ITS_OIDC_AUDIENCE in .env (the console's token audience, default "
            f"{DEFAULT_CONSOLE_AUDIENCE!r}); nothing was changed"
        )
    return reservations


# --- pure decisions --------------------------------------------------------


def client_representation(spec: ServiceClient) -> dict[str, Any]:
    """The client to create, as the admin API wants it.

    EVERY INTERACTIVE FLAG IS OFF, and that is the security content of this
    function rather than boilerplate. A confidential client with
    `directAccessGrantsEnabled` turns a leaked secret into a password-grant
    endpoint with no browser and no second factor -- the exact failure
    `deploy/GO-LIVE.md` §2 warns about for the console's own client. These two
    exist to hold a service account and have no interactive flow to enable.
    """
    return {
        "clientId": spec.client_id,
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": False,
        "serviceAccountsEnabled": True,
        "standardFlowEnabled": False,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "description": (
            "Created by the ITMS init job. Machine credential; no human signs in as this."
        ),
    }


def roles_to_grant(spec: ServiceClient, already_granted: frozenset[str]) -> tuple[str, ...]:
    """The roles still missing, sorted.

    Sorted because the order lands in a recorded call the tests assert on, and
    because an unordered set makes a diff between two runs unreadable. Empty on
    a converged realm, which is what keeps a re-run from writing anything.
    """
    return tuple(sorted(set(spec.roles) - already_granted))


def generate_password() -> str:
    return secrets.token_urlsafe(_ROTATED_PASSWORD_BYTES)


def bootstrap_password_candidates(env_password: str, secrets_dir: Path) -> tuple[str, ...]:
    """Which passwords a run should try for the bootstrap admin, in order.

    THE STORED ONE FIRST. After the first successful run the environment's
    `KEYCLOAK_ADMIN_PASSWORD` is stale -- this job rotated it away -- and the
    volume holds the live one.

    THE ENVIRONMENT ONE STILL LAST, for two states that are both real: the very
    first run, when nothing is stored; and a run killed between writing the new
    password and applying it, where the stored value is a password that was
    never put into effect. Trying both is what makes that second state
    self-healing instead of terminal.

    A blank or whitespace-only stored value is not offered. A zero-byte file is
    what a crash mid-write leaves behind, and presenting `""` produces an
    authentication failure that reads like a wrong password rather than like a
    truncated file.
    """
    candidates: list[str] = []
    stored = _read_secret(secrets_dir / BOOTSTRAP_SECRET_FILENAME)
    if stored:
        candidates.append(stored)
    if env_password and env_password not in candidates:
        candidates.append(env_password)
    return tuple(candidates)


# --- the secrets volume ----------------------------------------------------


def _read_secret(path: Path) -> str | None:
    try:
        content = path.read_text().strip()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError):
        return None
    return content or None


def write_secret(path: Path, value: str) -> None:
    """Write one secret to the shared volume, mode 600.

    The volume is mounted into the api, gateway and worker containers as well.
    Key material that is group- or world-readable there is readable by every
    process in the stack, which is the difference between one compromised
    container and the whole realm.

    `os.open` with the mode rather than write-then-chmod, so the file is never
    momentarily readable; the explicit `chmod` afterwards covers the case where
    the file already existed with wider bits, since `mode` only applies on
    creation.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w") as fh:
        fh.write(value + "\n")
    os.chmod(path, 0o600)


# --- pure decisions about the realm ----------------------------------------


def desired_realm_settings(realm_file: Mapping[str, Any]) -> dict[str, Any]:
    """The realm-level keys this job converges, taken from the shipped file.

    Absent keys are simply not converged -- a realm file that does not mention
    `editUsername` is not asserting anything about it, and inventing a default
    here would make this script a second source of truth for the realm.
    """
    return {key: value for key, value in realm_file.items() if key in CONVERGED_REALM_KEYS}


def settings_to_apply(desired: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """Only what actually differs.

    An empty result means no write at all, which is the normal outcome from the
    second deploy onwards. Writing the same values unconditionally would work
    and would put a realm-change record in Keycloak's admin event log on every
    `up` -- which is where an actual unauthorised change would then be
    indistinguishable from the deploy noise.
    """
    return {key: value for key, value in desired.items() if current.get(key) != value}


@dataclass(frozen=True)
class RequiredAction:
    """One credential step the realm must know about for `kc_action` to work."""

    alias: str
    name: str
    enabled: bool
    default_action: bool = False


def desired_required_actions(
    realm_file: Mapping[str, Any],
) -> tuple[RequiredAction, ...]:
    """The realm file's `requiredActions`, as this job needs them.

    `enabled` IS CARRIED, NOT ASSUMED. The file ships `TERMS_AND_CONDITIONS`
    and `delete_account` disabled; registering every alias and turning them all
    on would put a terms-and-conditions gate in front of every sign-in in the
    fleet, which is a fleet-wide outage delivered by a convenience loop.
    """
    return tuple(
        RequiredAction(
            alias=str(action["alias"]),
            name=str(action.get("name", action["alias"])),
            enabled=bool(action.get("enabled", False)),
            default_action=bool(action.get("defaultAction", False)),
        )
        for action in realm_file.get("requiredActions", ())
    )


def required_actions_to_register(
    desired: Sequence[RequiredAction], registered: Mapping[str, Any]
) -> tuple[RequiredAction, ...]:
    """Those the realm has never heard of.

    Only the ones the file wants ENABLED: registering a disabled action would
    be a write with no effect, and this job's write count is something a test
    asserts on.
    """
    return tuple(a for a in desired if a.enabled and a.alias not in registered)


def required_actions_to_update(
    desired: Sequence[RequiredAction], registered: Mapping[str, Mapping[str, Any]]
) -> tuple[RequiredAction, ...]:
    """Registered actions whose `enabled` or `defaultAction` differs from the
    file -- in EITHER direction.

    A separate step from registration because Keycloak's register endpoint
    creates the action DISABLED. An action the file ships disabled and the
    realm never registered is left unregistered (nothing to switch off).
    """
    return tuple(
        a
        for a in desired
        if a.alias in registered
        and (
            bool(registered[a.alias].get("enabled", False)) != a.enabled
            or bool(registered[a.alias].get("defaultAction", False)) != a.default_action
        )
    )


def desired_client_settings(realm_file: Mapping[str, Any], owned: OwnedClient) -> dict[str, Any]:
    """The owned fields of one client, from the file. Empty if absent."""
    for client in realm_file.get("clients", ()):
        if client.get("clientId") != owned.client_id:
            continue
        desired = {k: client[k] for k in owned.keys if k in client}
        attributes = {
            k: v for k, v in client.get("attributes", {}).items() if k in owned.attributes
        }
        if attributes:
            desired["attributes"] = attributes
        return desired
    return {}


def password_alternative_survives(executions: Sequence[Mapping[str, Any]]) -> bool:
    """Can this flow still sign somebody in with a password?

    THE CHECK THAT STANDS BETWEEN THIS JOB AND A FLEET-WIDE LOCKOUT. Binding a
    browser flow is the most dangerous write here: a graph that refuses
    everybody refuses the account that would repair it too, and the only way
    back is a shell inside the container. Every other precaution -- copying the
    stock flow rather than building one, adding passkey as an ALTERNATIVE
    beside the password form rather than in front of it -- is care taken while
    writing. This is a measurement of the result, and nothing is bound without
    it.

    REACHABILITY, NOT PRESENCE. The admin API returns a flat list with a
    `level`, so a password form nested inside a DISABLED subflow appears as a
    REQUIRED execution while being unreachable. Reading its own requirement
    alone would call that flow safe. So the walk carries the enclosing
    requirement down: an execution counts only when it and every subflow above
    it can run.
    """
    # `level` -> whether everything enclosing that depth is live.
    enclosing: dict[int, bool] = {0: True}
    for execution in executions:
        level = int(execution.get("level", 0))
        live = execution.get("requirement") in _LIVE_REQUIREMENTS
        reachable = enclosing.get(level, False) and live
        # This execution becomes the enclosing state for anything nested in it.
        enclosing[level + 1] = reachable
        if execution.get("providerId") in _PASSWORD_AUTHENTICATORS and reachable:
            return True
    return False


def flow_needs_passkey(executions: Sequence[Mapping[str, Any]]) -> bool:
    """True when the flow has no live passkey authenticator yet. Re-running
    this job is its normal case, so the second deploy must add nothing."""
    return not any(
        execution.get("providerId") == PASSKEY_AUTHENTICATOR
        and execution.get("requirement") in _LIVE_REQUIREMENTS
        for execution in executions
    )


def client_settings_to_apply(
    desired: Mapping[str, Any], current: Mapping[str, Any]
) -> dict[str, Any]:
    """Only what differs, with `attributes` MERGED rather than replaced.

    Replacing the attributes map would drop every per-client key Keycloak
    keeps in there that the realm file does not mention -- PKCE among them,
    which is a security downgrade delivered as a redirect-URI fix.

    LISTS COMPARE AS MULTISETS (order ignored, duplicates counted). Keycloak
    stores `redirectUris` and `webOrigins` as sets and returns them in its own
    order, so comparing them as lists PUT
    `it-suite-web` on every run of a realm nobody had touched (found by the
    first live CI run, MCP task 12).
    """
    changes: dict[str, Any] = {}
    for key, value in desired.items():
        if key == "attributes":
            merged = {**current.get("attributes", {}), **value}
            if merged != current.get("attributes", {}):
                changes["attributes"] = merged
        elif isinstance(value, list) and isinstance(current.get(key), list):
            if sorted(map(str, value)) != sorted(map(str, current[key])):
                changes[key] = value
        elif current.get(key) != value:
            changes[key] = value
    return changes


# --- the realm, as this job needs it ---------------------------------------


class RealmAdmin(Protocol):
    """The realm operations this job performs.

    A protocol, not a class, so the whole orchestration below is testable
    against an in-memory realm that models STATE -- and therefore genuinely
    answers "what does the second run see?" -- instead of against a mock that
    counts calls and would pass every idempotence assertion while the real job
    created a duplicate client every morning.
    """

    def find_client(self, client_id: str) -> str | None:
        """The client's internal id, or `None` if it does not exist."""

    def create_client(self, representation: dict[str, Any]) -> str:
        """Create it and return its internal id."""

    def create_reserved_client(self, representation: dict[str, Any]) -> str:
        """Create a reservation (an ordinary client to Keycloak); its id."""

    def client_secret(self, client_uuid: str) -> str: ...

    def service_account_user_id(self, client_uuid: str) -> str: ...

    def granted_roles(self, user_id: str) -> frozenset[str]:
        """The `realm-management` roles that service account already holds."""

    def grant_roles(self, user_id: str, roles: tuple[str, ...]) -> None: ...

    def set_bootstrap_password(self, password: str) -> None: ...

    # -- the realm itself ---------------------------------------------------

    def realm_settings(self) -> dict[str, Any]:
        """The realm's current representation."""

    def update_realm(self, changes: Mapping[str, Any]) -> None:
        """Apply these keys, leaving every other one alone."""

    def registered_required_actions(self) -> dict[str, dict[str, Any]]:
        """alias -> representation, for actions the realm has registered."""

    def register_required_action(self, alias: str, name: str) -> None: ...

    def update_required_action(self, alias: str, representation: Mapping[str, Any]) -> None: ...

    def client_settings(self, client_uuid: str) -> dict[str, Any]: ...

    def update_client(self, client_uuid: str, changes: Mapping[str, Any]) -> None: ...

    # -- authentication flows ----------------------------------------------

    def flow_aliases(self) -> frozenset[str]:
        """Every top-level authentication flow the realm has."""

    def copy_flow(self, source_alias: str, new_alias: str) -> None: ...

    def flow_executions(self, alias: str) -> list[dict[str, Any]]:
        """Flat list, in order, each carrying `level` and `requirement`."""

    def add_execution(self, alias: str, provider: str) -> None: ...

    def update_execution(self, alias: str, representation: Mapping[str, Any]) -> None: ...

    # -- the MCP surfaces (2026-10-14) ---------------------------------------

    def list_client_scopes(self) -> list[dict[str, Any]]: ...

    def create_client_scope(self, representation: dict[str, Any]) -> str:
        """Create it and return its id."""

    def update_client_scope(self, scope_id: str, representation: dict[str, Any]) -> None: ...

    def list_scope_mappers(self, scope_id: str) -> list[dict[str, Any]]: ...

    def create_scope_mapper(self, scope_id: str, representation: dict[str, Any]) -> None: ...

    def update_scope_mapper(
        self, scope_id: str, mapper_id: str, representation: dict[str, Any]
    ) -> None: ...

    def get_default_optional_scopes(self) -> list[dict[str, Any]]:
        """`[{"id", "name"}]` of the realm's default OPTIONAL client scopes."""

    def add_default_optional_scope(self, scope_id: str) -> None: ...

    def list_components(self, provider_type: str) -> list[dict[str, Any]]: ...

    def create_component(self, representation: dict[str, Any]) -> None: ...

    def update_component(self, component_id: str, representation: dict[str, Any]) -> None: ...

    def delete_component(self, component_id: str) -> None: ...

    def get_client_profiles(self) -> dict[str, Any]:
        """`{"profiles": [...]}`: the realm's own, never the global ones."""

    def put_client_profiles(self, representation: dict[str, Any]) -> None: ...

    def get_client_policies(self) -> dict[str, Any]:
        """`{"policies": [...]}`: the realm's own, never the global ones."""

    def put_client_policies(self, representation: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class InitResult:
    """What a run did, in terms safe to print. No secret appears here."""

    created: tuple[str, ...]
    granted: tuple[str, ...]
    rotated_bootstrap_admin: bool
    #: Realm keys this run actually wrote. Empty from the second deploy on.
    realm_changed: tuple[str, ...] = ()
    required_actions_registered: tuple[str, ...] = ()
    #: Aliases whose `enabled`/`defaultAction` were corrected (either way).
    required_actions_updated: tuple[str, ...] = ()
    #: clientId of each client whose converged fields were corrected.
    clients_updated: tuple[str, ...] = ()
    #: What happened to the passkey sign-in flow: "bound", "already-bound",
    #: "built-not-bound" (the safety check refused it) or "unchanged".
    passkey_flow: str = "unchanged"
    #: The writes behind `passkey_flow`, in order (`copy_flow`, `add_execution`,
    #: `update_execution`, `bind`). Empty on a re-run of a converged realm --
    #: which "already-bound" alone does not say: a run that had to re-add the
    #: passkey execution first reports that outcome too.
    passkey_flow_writes: tuple[str, ...] = ()
    #: What `converge_mcp` wrote. Empty from the second deploy on.
    mcp_converged: tuple[str, ...] = ()


def run_init(
    admin: RealmAdmin,
    *,
    secrets_dir: Path,
    realm_file: Mapping[str, Any],
    clients: Sequence[ServiceClient] = DESIRED_CLIENTS,
    public_url: str = "",
    console_audience: str = DEFAULT_CONSOLE_AUDIENCE,
    mcp_registration: str = "",
) -> InitResult:
    """Bring the realm to the state the appliance needs, from wherever it is.

    Phases, and the order between them is the invariant:

      1. every desired client exists,
      2. every desired role is granted,
      3. every client secret is on the volume,
      4. the realm's own converged state matches `realm_file`,
      5. THEN the bootstrap admin's password is rotated.

    Steps 1-3 are what a future run needs in order to be able to do anything;
    step 5 is what invalidates the credential this run authenticated with. Doing
    5 before 1-3 leaves a half-configured realm and no way back into it.

    STEP 4 SITS WHERE IT DOES FOR TWO SEPARATE REASONS, and both are asserted.
    After 1-3, because convergence is the newer code and a defect in it must not
    be able to stop a fresh install from getting the provisioning client its
    setup screen needs: a failure at 4 leaves an appliance that can create its
    first administrator and has a stale theme, where the other order leaves an
    appliance nobody can log into at all. Before 5, because it authenticates
    with the credential 5 destroys.

    `realm_file` IS REQUIRED, with no default, deliberately. An optional
    parameter would give this function a silent do-nothing branch, and the
    first refactor of `main()` that stopped passing it would disable
    convergence across the fleet with every test still green.

    `public_url` (2026-10-14) DOES default, to empty, and that is not the same
    trade. Empty is a state the product supports -- `ITS_PUBLIC_CONSOLE_URL`
    unset, `/mcp` answering 503 -- in which everything else MCP needs still
    converges, only the audience mapper waits, and `mcp_summary` prints the
    reason on every run. Nothing about it is silent. The MCP surfaces converge
    after step 4 and before step 5, for step 4's two reasons.

    `mcp_registration` (`ITS_MCP_REGISTRATION`, 2026-10-15) defaults to empty,
    which is CLOSED: a caller that forgets it keeps strangers out.
    """
    # REFUSED BEFORE THE FIRST WRITE: a reservation naming a client the
    # platform needs is a configuration error, not something to half-apply.
    desired_reservations(public_url, console_audience)

    created: list[str] = []
    granted: list[str] = []

    for spec in clients:
        uuid = admin.find_client(spec.client_id)
        if uuid is None:
            uuid = admin.create_client(client_representation(spec))
            created.append(spec.client_id)

        service_account = admin.service_account_user_id(uuid)
        missing = roles_to_grant(spec, admin.granted_roles(service_account))
        if missing:
            admin.grant_roles(service_account, missing)
            granted.extend(f"{spec.client_id}:{role}" for role in missing)

        # Re-read and re-write EVERY run, not only on creation. A restore that
        # brought back the database but not the secrets volume leaves the
        # clients present and their secrets gone; a job that wrote secrets only
        # for clients it had just created would report success on that and
        # leave the platform unable to provision a user.
        write_secret(secrets_dir / spec.secret_filename, admin.client_secret(uuid))

    # --- the realm itself --------------------------------------------------
    realm_changed, registered, actions_updated, updated = converge_realm(admin, realm_file)
    passkey_writes: list[str] = []
    passkey_flow = converge_passkey_flow(admin, passkey_writes)
    # The newest convergence last, so a defect in it cannot cost the realm
    # settings or the sign-in flow theirs.
    mcp_converged = converge_mcp(
        admin, public_url, console_audience, registration=mcp_registration
    )

    # --- and only now, the bootstrap admin --------------------------------
    #
    # WRITE, THEN APPLY. Apply-then-write loses the password permanently if the
    # process dies in between: the account exists, nobody can authenticate as
    # it, and the vendor's break-glass door is gone for good. Write-then-apply's
    # failure mode is a file holding a password that is not in effect, which
    # `bootstrap_password_candidates` recovers from on the next run by falling
    # back to the environment.
    replacement = generate_password()
    write_secret(secrets_dir / BOOTSTRAP_SECRET_FILENAME, replacement)
    admin.set_bootstrap_password(replacement)

    return InitResult(
        created=tuple(created),
        granted=tuple(granted),
        rotated_bootstrap_admin=True,
        realm_changed=realm_changed,
        required_actions_registered=registered,
        required_actions_updated=actions_updated,
        clients_updated=updated,
        passkey_flow=passkey_flow,
        passkey_flow_writes=tuple(passkey_writes),
        mcp_converged=tuple(mcp_converged),
    )


def converge_realm(
    admin: RealmAdmin, realm_file: Mapping[str, Any]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Bring the realm's own settings, required actions and browser client to
    what the shipped realm file says, writing only where they differ.

    Returns what was written, in four buckets, so the job can print it and a
    test can assert that a converged realm is written to exactly zero times.
    """
    changes = settings_to_apply(desired_realm_settings(realm_file), admin.realm_settings())
    if changes:
        admin.update_realm(changes)

    desired_actions = desired_required_actions(realm_file)
    registered_now = admin.registered_required_actions()
    to_register = required_actions_to_register(desired_actions, registered_now)
    for action in to_register:
        admin.register_required_action(action.alias, action.name)
        # Keycloak's register endpoint creates the action DISABLED, so the
        # local view has to say so or the enable pass below skips it.
        registered_now[action.alias] = {"alias": action.alias, "enabled": False}

    to_update = required_actions_to_update(desired_actions, registered_now)
    for action in to_update:
        admin.update_required_action(
            action.alias,
            {
                "alias": action.alias,
                "name": action.name,
                "enabled": action.enabled,
                "defaultAction": action.default_action,
            },
        )

    # THE BROWSER CLIENT IS CORRECTED, NEVER CREATED. If it is absent, the
    # realm was not built from this file and something is wrong that a
    # convergence loop cannot see. Creating a public client here would mean
    # inventing its redirect URIs -- an open-redirect surface -- from a
    # position of ignorance, so the job leaves it alone and says nothing was
    # updated.
    updated: list[str] = []
    for owned in CONVERGED_CLIENTS:
        desired = desired_client_settings(realm_file, owned)
        if not desired:
            continue
        uuid = admin.find_client(owned.client_id)
        if uuid is None:
            continue
        client_changes = client_settings_to_apply(desired, admin.client_settings(uuid))
        if client_changes:
            admin.update_client(uuid, client_changes)
            updated.append(owned.client_id)

    return (
        tuple(sorted(changes)),
        tuple(a.alias for a in to_register),
        tuple(a.alias for a in to_update),
        tuple(updated),
    )


def converge_passkey_flow(admin: RealmAdmin, writes: list[str] | None = None) -> str:
    """Offer a passkey at sign-in, without ever making the realm unusable.

    THE PROBLEM. `webauthn-register-passwordless` puts a passkey on an account;
    the BROWSER FLOW decides whether anything at sign-in will accept one, and
    stock Keycloak's has no WebAuthn execution at all. The operator enrolled a
    passkey and the login page did not offer it -- two pieces of realm state,
    one converged and one not.

    WHY THIS IS THE MOST DANGEROUS THING THE JOB DOES, stated plainly because
    the operator chose it with the risk on the table: a browser flow that
    refuses everybody refuses the account that would repair it, and the way
    back is a shell inside the container. Four things contain that:

      1. The flow is a COPY of the stock `browser` flow, so the password leg is
         whatever Keycloak shipped -- not a graph reconstructed here from an
         understanding of the admin API that has never been tested against a
         live realm.
      2. Passkey is added as an ALTERNATIVE beside the existing legs, never in
         front of them. An alternative that cannot run falls through to the
         next one, which is the password form.
      3. `password_alternative_survives` is checked against the flow AS BUILT,
         and nothing is bound if it fails. That is a measurement, not a
         precaution.
      4. Binding happens once. A realm already pointing at this flow is left
         alone, so the daily deploy is not repeatedly rewriting the mechanism
         every account depends on.

    Returns what happened, for the job's log line, and appends each write it
    makes to `writes` (see `InitResult.passkey_flow_writes`).
    """
    wrote = writes if writes is not None else []
    aliases = admin.flow_aliases()
    if PASSKEY_FLOW_ALIAS not in aliases:
        if STOCK_BROWSER_FLOW not in aliases:
            # Nothing to copy. Better to leave sign-in alone than to invent a
            # flow for a realm shaped in a way this job does not recognise.
            return "unchanged"
        admin.copy_flow(STOCK_BROWSER_FLOW, PASSKEY_FLOW_ALIAS)
        wrote.append("copy_flow")

    executions = admin.flow_executions(PASSKEY_FLOW_ALIAS)
    if flow_needs_passkey(executions):
        admin.add_execution(PASSKEY_FLOW_ALIAS, PASSKEY_AUTHENTICATOR)
        wrote.append("add_execution")
        # Keycloak adds a new execution DISABLED. It has to become an
        # ALTERNATIVE to be offered at all -- and ALTERNATIVE rather than
        # REQUIRED, which would demand a passkey from everybody and lock out
        # every account that does not have one.
        executions = admin.flow_executions(PASSKEY_FLOW_ALIAS)
        for execution in executions:
            if execution.get("providerId") == PASSKEY_AUTHENTICATOR:
                admin.update_execution(
                    PASSKEY_FLOW_ALIAS, {**execution, "requirement": "ALTERNATIVE"}
                )
                wrote.append("update_execution")
        executions = admin.flow_executions(PASSKEY_FLOW_ALIAS)

    if not password_alternative_survives(executions):
        # Built, and deliberately not bound. The realm keeps whatever flow it
        # had, which is a working one.
        return "built-not-bound"

    if admin.realm_settings().get("browserFlow") == PASSKEY_FLOW_ALIAS:
        return "already-bound"

    admin.update_realm({"browserFlow": PASSKEY_FLOW_ALIAS})
    wrote.append("bind")
    return "bound"


def passkey_summary(result: InitResult) -> str:
    """The job's passkey line: the outcome and the writes behind it. Names
    what happened, never a value."""
    writes = list(result.passkey_flow_writes) or "nothing"
    return f"identity init: passkey flow={result.passkey_flow} writes={writes}"


def converge_mcp(
    admin: RealmAdmin,
    public_url: str,
    console_audience: str = DEFAULT_CONSOLE_AUDIENCE,
    *,
    registration: str = "",
) -> list[str]:
    """The `mcp` client scope, open registration hardened, and the client
    policy, writing only where the realm differs. Returns what it wrote.

    THE ORDER OPENS REGISTRATION LAST. Removing Trusted Hosts is the one write
    here that WIDENS anything: with it gone, anybody may register. So the
    reserved clientIds, the scope, the client profile and policy, and every
    narrowing registration policy are in force first, and the removal is the
    final write -- a run that dies part-way leaves registration closed, never
    open and unrestricted. And it happens only when `public_url` is set AND
    the operator opted in (`registration`, `ITS_MCP_REGISTRATION=open`);
    otherwise Trusted Hosts is kept, or restored before anything else.
    Everything else converges either way, so opening later is the flag and a
    redeploy.

    THE SCOPE IS CONVERGED HERE ON EVERY INSTALL, INCLUDING A FRESH ONE, and
    the realm file deliberately does not declare it: see
    `test_the_realm_file_does_not_declare_client_scopes` for why it cannot.
    """
    changed: list[str] = []
    desired_scope = desired_mcp_scope(public_url)

    # 0. The names no app may register as: created, or switched off in place.
    for reservation in desired_reservations(public_url, console_audience):
        client_id = str(reservation["clientId"])
        uuid = admin.find_client(client_id)
        if uuid is None:
            admin.create_reserved_client(reservation)
            changed.append(f"reserved:{client_id}")
            continue
        wanted = {k: reservation[k] for k in RESERVATION_KEYS}
        correction = client_settings_to_apply(wanted, admin.client_settings(uuid))
        if correction:
            admin.update_client(uuid, correction)
            changed.append(f"reserved:{client_id}")

    # 1. The scope.
    existing = next((s for s in admin.list_client_scopes() if s.get("name") == MCP_SCOPE), None)
    if existing is None:
        scope_id = admin.create_client_scope(
            {k: v for k, v in desired_scope.items() if k != "protocolMappers"}
        )
        changed.append(f"client-scope:{MCP_SCOPE}")
    else:
        scope_id = str(existing["id"])
        scope_correction = scope_changes(existing, desired_scope)
        if scope_correction is not None:
            admin.update_client_scope(scope_id, scope_correction)
            changed.append(f"client-scope:{MCP_SCOPE}")

    # 2. Its mappers.
    to_create, to_update = mapper_changes(
        admin.list_scope_mappers(scope_id), desired_scope["protocolMappers"]
    )
    for mapper in to_create:
        admin.create_scope_mapper(scope_id, mapper)
        changed.append(f"mapper:{mapper['name']}")
    for mapper_id, mapper in to_update:
        admin.update_scope_mapper(scope_id, mapper_id, mapper)
        changed.append(f"mapper:{mapper['name']}")

    # 3. Requestable by a registered app.
    optional = {str(s.get("name")) for s in admin.get_default_optional_scopes()}
    if MCP_SCOPE not in optional:
        admin.add_default_optional_scope(scope_id)
        changed.append(f"optional-scope:{MCP_SCOPE}")

    # 4. Profiles, then policies -- Keycloak refuses a policy naming a
    #    profile it does not have.
    profiles, policies = client_policy_changes(
        admin.get_client_profiles(), admin.get_client_policies()
    )
    if profiles is not None:
        admin.put_client_profiles(profiles)
        changed.append("client-profiles:" + ",".join(p["name"] for p in desired_client_profiles()))
    if policies is not None:
        admin.put_client_policies(policies)
        changed.append("client-policies:" + ",".join(p["name"] for p in desired_client_policies()))

    # 5. Narrow anonymous registration, and only then open it -- if MCP can
    #    work at all. Restoring Trusted Hosts, when it is due, is the first
    #    create (`registration_policy_changes` puts it there).
    to_add, to_fix, to_remove = registration_policy_changes(
        admin.list_components(REGISTRATION_POLICY_TYPE),
        desired_registration_policies(),
        open_registration=not registration_blockers(public_url, registration),
    )
    for component in to_add:
        admin.create_component(component)
        changed.append(f"registration:created:{component['providerId']}")
    for component in to_fix:
        admin.update_component(str(component["id"]), component)
        changed.append(f"registration:updated:{component['providerId']}")
    for component_id in to_remove:
        admin.delete_component(component_id)
        changed.append(f"registration:removed:{'/'.join(REMOVED_REGISTRATION_POLICIES)}")

    return changed


# --- the thin caller: HTTP against a live realm ---------------------------
#
# NOTHING BELOW IS UNDER TEST. It is the transport for the decisions above and
# nothing else; every branch worth arguing about lives higher up in this file.
# It uses `urllib` rather than httpx so the job has no dependency beyond the
# standard library and can run in any python image.


class RealmError(RuntimeError):
    pass


def _request(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    json_body: Any = None,
    form: dict[str, str] | None = None,
) -> Any:
    data: bytes | None = None
    headers: dict[str, str] = {"Accept": "application/json"}
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:  # pragma: no cover - transport
        # The body can carry the realm's own explanation; it never carries a
        # secret this job wrote, because this job only ever SENDS those.
        raise RealmError(f"{method} {url} -> {exc.code}: {exc.read()[:200]!r}") from exc
    return json.loads(body) if body else None


class HttpRealmAdmin:  # pragma: no cover - exercised only against a live realm
    """`RealmAdmin` over the admin REST API."""

    def __init__(self, base_url: str, realm: str, token: str) -> None:
        self._base = base_url.rstrip("/")
        self._realm = realm
        self._token = token
        self._management_uuid: str | None = None

    def _get(self, path: str) -> Any:
        return _request(f"{self._base}{path}", token=self._token)

    def find_client(self, client_id: str) -> str | None:
        query = urllib.parse.urlencode({"clientId": client_id})
        found = self._get(f"/admin/realms/{self._realm}/clients?{query}")
        return str(found[0]["id"]) if found else None

    def create_client(self, representation: dict[str, Any]) -> str:
        _request(
            f"{self._base}/admin/realms/{self._realm}/clients",
            method="POST",
            token=self._token,
            json_body=representation,
        )
        uuid = self.find_client(str(representation["clientId"]))
        if uuid is None:
            raise RealmError(f"created {representation['clientId']} but cannot find it")
        return uuid

    def create_reserved_client(self, representation: dict[str, Any]) -> str:
        return self.create_client(representation)

    def client_secret(self, client_uuid: str) -> str:
        found = self._get(f"/admin/realms/{self._realm}/clients/{client_uuid}/client-secret")
        return str(found["value"])

    def service_account_user_id(self, client_uuid: str) -> str:
        found = self._get(f"/admin/realms/{self._realm}/clients/{client_uuid}/service-account-user")
        return str(found["id"])

    # -- the realm itself ---------------------------------------------------
    #
    # EVERY WRITE HERE IS READ-MERGE-WRITE, and that is not caution for its own
    # sake. Keycloak's admin API takes FULL representations on these PUTs: a
    # body carrying only the fields being changed does not mean "leave the rest
    # alone", it means "here is the object". Sending the diff would blank
    # everything omitted -- the realm's SMTP block, a client's PKCE setting, a
    # required action's priority -- which is the same accident this job's
    # allowlist exists to prevent, arriving through the transport instead.

    def realm_settings(self) -> dict[str, Any]:
        return dict(self._get(f"/admin/realms/{self._realm}"))

    def update_realm(self, changes: Mapping[str, Any]) -> None:
        current = self.realm_settings()
        _request(
            f"{self._base}/admin/realms/{self._realm}",
            method="PUT",
            token=self._token,
            json_body={**current, **changes},
        )

    def registered_required_actions(self) -> dict[str, dict[str, Any]]:
        found = self._get(f"/admin/realms/{self._realm}/authentication/required-actions")
        return {str(action["alias"]): dict(action) for action in found or ()}

    def register_required_action(self, alias: str, name: str) -> None:
        # `providerId`, not `alias`: this endpoint registers one of the
        # provider implementations the server offers, and the alias it ends up
        # with is the provider id. The available set is
        # `/authentication/unregistered-required-actions`.
        _request(
            f"{self._base}/admin/realms/{self._realm}/authentication/register-required-action",
            method="POST",
            token=self._token,
            json_body={"providerId": alias, "name": name},
        )

    def update_required_action(self, alias: str, representation: Mapping[str, Any]) -> None:
        current = self.registered_required_actions().get(alias, {"alias": alias})
        _request(
            f"{self._base}/admin/realms/{self._realm}/authentication/required-actions/{alias}",
            method="PUT",
            token=self._token,
            json_body={**current, **representation},
        )

    def client_settings(self, client_uuid: str) -> dict[str, Any]:
        return dict(self._get(f"/admin/realms/{self._realm}/clients/{client_uuid}"))

    def update_client(self, client_uuid: str, changes: Mapping[str, Any]) -> None:
        current = self.client_settings(client_uuid)
        _request(
            f"{self._base}/admin/realms/{self._realm}/clients/{client_uuid}",
            method="PUT",
            token=self._token,
            json_body={**current, **changes},
        )

    # -- authentication flows ------------------------------------------------

    def flow_aliases(self) -> frozenset[str]:
        found = self._get(f"/admin/realms/{self._realm}/authentication/flows")
        return frozenset(str(flow["alias"]) for flow in found or ())

    def copy_flow(self, source_alias: str, new_alias: str) -> None:
        _request(
            f"{self._base}/admin/realms/{self._realm}/authentication/flows/"
            f"{urllib.parse.quote(source_alias)}/copy",
            method="POST",
            token=self._token,
            json_body={"newName": new_alias},
        )

    def flow_executions(self, alias: str) -> list[dict[str, Any]]:
        found = self._get(
            f"/admin/realms/{self._realm}/authentication/flows/"
            f"{urllib.parse.quote(alias)}/executions"
        )
        return [dict(execution) for execution in found or ()]

    def add_execution(self, alias: str, provider: str) -> None:
        _request(
            f"{self._base}/admin/realms/{self._realm}/authentication/flows/"
            f"{urllib.parse.quote(alias)}/executions/execution",
            method="POST",
            token=self._token,
            json_body={"provider": provider},
        )

    def update_execution(self, alias: str, representation: Mapping[str, Any]) -> None:
        # PUT on the FLOW's executions collection, with the execution
        # representation as the body -- not on an execution URL. That is the
        # shape Keycloak's admin API takes here, and it is easy to get wrong
        # because every other update in this file addresses the object.
        _request(
            f"{self._base}/admin/realms/{self._realm}/authentication/flows/"
            f"{urllib.parse.quote(alias)}/executions",
            method="PUT",
            token=self._token,
            json_body=dict(representation),
        )

    # -- the MCP surfaces (2026-10-14) ---------------------------------------
    #
    # Paths from `services/resources/admin/RealmAdminResource` (26.2.5):
    # client-scopes :224, default-optional-client-scopes/{id} :337,
    # components :383, client-policies/policies :1403, client-policies/profiles
    # :1409; mappers from `ProtocolMappersResource` (`models`, `models/{id}`).

    def _send(self, path: str, method: str, body: Any = None) -> Any:
        return _request(
            f"{self._base}/admin/realms/{self._realm}{path}",
            method=method,
            token=self._token,
            json_body=body,
        )

    def list_client_scopes(self) -> list[dict[str, Any]]:
        found = self._get(f"/admin/realms/{self._realm}/client-scopes")
        return [dict(scope) for scope in found or ()]

    def create_client_scope(self, representation: dict[str, Any]) -> str:
        self._send("/client-scopes", "POST", representation)
        for scope in self.list_client_scopes():
            if scope.get("name") == representation["name"]:
                return str(scope["id"])
        raise RealmError(f"created client scope {representation['name']} but cannot find it")

    def update_client_scope(self, scope_id: str, representation: dict[str, Any]) -> None:
        # ClientScopesResource:138 -> ClientScopeResource:111. Attributes are
        # set key by key (RepresentationToModel.updateClientScope:732-743).
        self._send(f"/client-scopes/{scope_id}", "PUT", representation)

    def list_scope_mappers(self, scope_id: str) -> list[dict[str, Any]]:
        found = self._get(
            f"/admin/realms/{self._realm}/client-scopes/{scope_id}/protocol-mappers/models"
        )
        return [dict(mapper) for mapper in found or ()]

    def create_scope_mapper(self, scope_id: str, representation: dict[str, Any]) -> None:
        self._send(f"/client-scopes/{scope_id}/protocol-mappers/models", "POST", representation)

    def update_scope_mapper(
        self, scope_id: str, mapper_id: str, representation: dict[str, Any]
    ) -> None:
        # The id travels in the BODY as well: `ProtocolMappersResource.update`
        # rebuilds the model from the representation alone.
        self._send(
            f"/client-scopes/{scope_id}/protocol-mappers/models/{mapper_id}",
            "PUT",
            {**representation, "id": mapper_id},
        )

    def get_default_optional_scopes(self) -> list[dict[str, Any]]:
        found = self._get(f"/admin/realms/{self._realm}/default-optional-client-scopes")
        return [dict(scope) for scope in found or ()]

    def add_default_optional_scope(self, scope_id: str) -> None:
        self._send(f"/default-optional-client-scopes/{scope_id}", "PUT")

    def list_components(self, provider_type: str) -> list[dict[str, Any]]:
        # `parent` defaults to the realm's own id (`ComponentResource`).
        query = urllib.parse.urlencode({"type": provider_type})
        found = self._get(f"/admin/realms/{self._realm}/components?{query}")
        return [dict(component) for component in found or ()]

    def create_component(self, representation: dict[str, Any]) -> None:
        self._send("/components", "POST", representation)

    def update_component(self, component_id: str, representation: dict[str, Any]) -> None:
        self._send(f"/components/{component_id}", "PUT", representation)

    def delete_component(self, component_id: str) -> None:
        self._send(f"/components/{component_id}", "DELETE")

    def get_client_profiles(self) -> dict[str, Any]:
        found = self._get(f"/admin/realms/{self._realm}/client-policies/profiles")
        return {"profiles": list((found or {}).get("profiles") or [])}

    def put_client_profiles(self, representation: dict[str, Any]) -> None:
        self._send("/client-policies/profiles", "PUT", representation)

    def get_client_policies(self) -> dict[str, Any]:
        found = self._get(f"/admin/realms/{self._realm}/client-policies/policies")
        return {"policies": list((found or {}).get("policies") or [])}

    def put_client_policies(self, representation: dict[str, Any]) -> None:
        self._send("/client-policies/policies", "PUT", representation)

    def _realm_management_uuid(self) -> str:
        if self._management_uuid is None:
            uuid = self.find_client("realm-management")
            if uuid is None:
                raise RealmError("the realm-management client is missing")
            self._management_uuid = uuid
        return self._management_uuid

    def granted_roles(self, user_id: str) -> frozenset[str]:
        management = self._realm_management_uuid()
        found = self._get(
            f"/admin/realms/{self._realm}/users/{user_id}/role-mappings/clients/{management}"
        )
        return frozenset(str(role["name"]) for role in found or ())

    def grant_roles(self, user_id: str, roles: tuple[str, ...]) -> None:
        management = self._realm_management_uuid()
        available = self._get(f"/admin/realms/{self._realm}/clients/{management}/roles")
        by_name = {str(role["name"]): role for role in available}
        missing = [role for role in roles if role not in by_name]
        if missing:
            raise RealmError(f"realm-management has no role(s) {missing}")
        _request(
            f"{self._base}/admin/realms/{self._realm}/users/{user_id}"
            f"/role-mappings/clients/{management}",
            method="POST",
            token=self._token,
            json_body=[by_name[role] for role in roles],
        )


class HttpBootstrapAdmin(HttpRealmAdmin):  # pragma: no cover - live realm only
    """The full `RealmAdmin`: `HttpRealmAdmin` plus the bootstrap rotation.

    A subclass rather than one more method on the base, because the bootstrap
    account lives in the `master` realm and everything else here lives in the
    product's realm. Conflating the two in a class that carries one `self._realm`
    is how a rotation ends up resetting somebody else's password.
    """

    def __init__(self, base_url: str, realm: str, token: str, bootstrap_user: str) -> None:
        super().__init__(base_url, realm, token)
        self._bootstrap_user = bootstrap_user

    def set_bootstrap_password(self, password: str) -> None:
        query = urllib.parse.urlencode({"username": self._bootstrap_user, "exact": "true"})
        found = self._get(f"/admin/realms/master/users?{query}")
        if not found:
            raise RealmError(f"no master-realm account named {self._bootstrap_user!r}")
        user_id = found[0]["id"]
        _request(
            f"{self._base}/admin/realms/master/users/{user_id}/reset-password",
            method="PUT",
            token=self._token,
            json_body={"type": "password", "value": password, "temporary": False},
        )


def _obtain_token(base_url: str, username: str, password: str) -> str:  # pragma: no cover
    found = _request(
        f"{base_url.rstrip('/')}/realms/master/protocol/openid-connect/token",
        method="POST",
        form={
            "client_id": "admin-cli",
            "grant_type": "password",
            "username": username,
            "password": password,
        },
    )
    return str(found["access_token"])


def _wait_for_realm(base_url: str, realm: str, *, attempts: int = 60) -> None:  # pragma: no cover
    """Block until the realm answers.

    Compose can only express `service_started` for the identity provider (it
    publishes no port and ships no healthcheck), and a JVM takes tens of
    seconds to import a realm. Without this the job's normal outcome on a cold
    host is a connection error, which looks like a misconfiguration and is not
    one.
    """
    url = f"{base_url.rstrip('/')}/realms/{realm}/.well-known/openid-configuration"
    for attempt in range(attempts):
        try:
            _request(url)
            return
        except (RealmError, urllib.error.URLError, OSError):
            if attempt == attempts - 1:
                raise
            time.sleep(2)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live realm only
    del argv
    base_url = os.environ.get("ITS_KEYCLOAK_URL", "http://keycloak:8080")
    realm = os.environ.get("ITS_KEYCLOAK_REALM", "it-suite")
    bootstrap_user = os.environ.get("KEYCLOAK_ADMIN_USER", "admin")
    env_password = os.environ.get("KEYCLOAK_ADMIN_PASSWORD", "")
    secrets_dir = Path(os.environ.get("ITS_SECRETS_DIR", "").strip() or DEFAULT_SECRETS_DIR)
    realm_path = Path(os.environ.get("ITS_REALM_FILE", "").strip() or DEFAULT_REALM_FILE)

    # FAIL LOUDLY RATHER THAN CONVERGE NOTHING. The file is bind-mounted by the
    # `keycloak-init` service; if it is not there, compose is wrong, and a job
    # that shrugged and skipped would leave the fleet's realms drifting with
    # every deploy reporting success.
    try:
        realm_file = json.loads(realm_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read the realm file at {realm_path}: {exc}", file=sys.stderr)
        return 4

    # The console's public address, from which the MCP audience is derived.
    # Unset is a supported state: see `run_init` and `mcp_summary`.
    public_url = os.environ.get("ITS_PUBLIC_CONSOLE_URL", "")
    # The console's audience, whose clientId is reserved (fix round 3).
    console_audience = (
        os.environ.get("ITS_OIDC_AUDIENCE", "").strip() or DEFAULT_CONSOLE_AUDIENCE
    )
    # The operator's opt-in to anonymous AI-app registration; closed unless
    # it says `open` (`registration_opted_in`).
    mcp_registration = os.environ.get("ITS_MCP_REGISTRATION", "")
    # A reservation that would disable the console's own client or one of
    # Keycloak's is refused before the job even authenticates. `run_init`
    # checks again, for any other caller.
    try:
        desired_reservations(public_url, console_audience)
    except ReservationRefused as exc:
        print(f"identity init: {exc}", file=sys.stderr)
        return 5

    _wait_for_realm(base_url, realm)

    candidates = bootstrap_password_candidates(env_password, secrets_dir)
    if not candidates:
        print(
            "no bootstrap credential available: neither KEYCLOAK_ADMIN_PASSWORD nor "
            f"{secrets_dir / BOOTSTRAP_SECRET_FILENAME} holds one",
            file=sys.stderr,
        )
        return 2

    token: str | None = None
    for password in candidates:
        try:
            token = _obtain_token(base_url, bootstrap_user, password)
            break
        except (RealmError, urllib.error.URLError, OSError):
            continue
    if token is None:
        print(
            f"could not authenticate as {bootstrap_user!r} with any known password; "
            "restore the secrets volume or reset the account by hand",
            file=sys.stderr,
        )
        return 3

    admin = HttpBootstrapAdmin(base_url, realm, token, bootstrap_user)
    result = run_init(
        admin,
        secrets_dir=secrets_dir,
        realm_file=realm_file,
        public_url=public_url,
        console_audience=console_audience,
        mcp_registration=mcp_registration,
    )

    # Says WHAT happened, never with WHAT values -- this goes to a container log
    # that `docker logs` prints to anybody on the host.
    print(f"identity init: created={list(result.created) or 'nothing'}")
    print(f"identity init: granted={list(result.granted) or 'nothing'}")
    print(f"identity init: realm keys converged={list(result.realm_changed) or 'nothing'}")
    print(
        "identity init: required actions "
        f"registered={list(result.required_actions_registered) or 'nothing'} "
        f"updated={list(result.required_actions_updated) or 'nothing'}"
    )
    print(f"identity init: clients corrected={list(result.clients_updated) or 'nothing'}")
    print(passkey_summary(result))
    for line in mcp_summary(result.mcp_converged, public_url, mcp_registration):
        print(line)
    print("identity init: bootstrap admin retired; its password lives only on the secrets volume")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
