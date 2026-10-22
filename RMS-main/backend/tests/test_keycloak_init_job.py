"""The first-boot job that configures the identity provider so nobody has to.

`docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §3.2.

WHAT THIS REPLACES. Until now `deploy/README.md` steps 8-10 asked the operator
to open a second admin portal, click two service-account clients together, copy
two secrets out of it by hand into files on a volume, and then -- on trust --
delete the bootstrap admin whose password sits in `.env`. Every one of those is
an SSH session or a second portal, which is exactly what the buyer test forbids.
`deploy/keycloak/init-clients.py` does all of it once, unattended, at
`docker compose up`.

WHY THE SCRIPT IS SHAPED AS DECISIONS PLUS A THIN CALLER. The identity provider
is a pinned third-party container, and standing one up per unit test would
measure its startup time rather than this logic. So everything that DECIDES --
what to create, which roles to grant, what to write where, and in what ORDER --
is pure and lives here under test, and the HTTP verbs that carry those
decisions to a live realm are left to the `deploy` CI job, which since
2026-10-15 runs the job against a real Keycloak and requires a second run to
write nothing (`.github/workflows/ci.yml`, the MCP step).

THE ORDER IS THE POINT, and it is the one thing a reviewer should check first.
The job retires the bootstrap admin by rotating its password to a random value
only the secrets volume holds. Do that BEFORE the service clients exist and the
job has destroyed the only credential it can authenticate with, on a job whose
NORMAL case is being re-run -- compose starts one-shot services again on every
`up`. `test_the_bootstrap_admin_is_retired_only_after_the_clients_exist` is
that assertion, and it is not a style preference.
"""

import importlib.util
import json
import re
import stat
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy/keycloak/init-clients.py"
COMPOSE = REPO_ROOT / "deploy/docker-compose.yml"
REALM_PATH = REPO_ROOT / "deploy/keycloak/realm-it-suite.json"

#: THE SHIPPED REALM FILE, not a fixture built to suit these tests.
#:
#: The whole point of the convergence half of this job is that the realm file
#: is the single source of the realm's desired state -- imported on a fresh
#: install, converged onto a running one. A test that invented its own realm
#: representation would verify the mechanism while saying nothing about
#: whether the values the product actually ships can be converged, which is
#: the only question an operator has.
REALM_FILE: dict[str, Any] = json.loads(REALM_PATH.read_text())

#: The browser client, pulled out once. `next` rather than a fixed index: the
#: order of `clients` in the file is not something this test should pin.
REALM_FILE_WEB: dict[str, Any] = next(
    c for c in REALM_FILE["clients"] if c["clientId"] == "it-suite-web"
)


def _load_script() -> ModuleType:
    """Import the deploy script by path.

    `deploy/` is not a package and is not on `sys.path`, and the file's name is
    not an identifier. Importing by location keeps the script the single source
    of these decisions instead of a copy in the test that drifts the first time
    somebody edits one and not the other.
    """
    spec = importlib.util.spec_from_file_location("_kc_init_clients", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


init = _load_script()


def _stock_browser_executions() -> list[dict[str, Any]]:
    """Keycloak 26's `browser` flow, as the admin API reports it."""
    return [
        {"id": "e1", "providerId": "auth-cookie", "requirement": "ALTERNATIVE", "level": 0},
        {"id": "e2", "providerId": "auth-spnego", "requirement": "DISABLED", "level": 0},
        {
            "id": "e3",
            "providerId": "identity-provider-redirector",
            "requirement": "ALTERNATIVE",
            "level": 0,
        },
        {"id": "e4", "displayName": "forms", "requirement": "ALTERNATIVE", "level": 0},
        {
            "id": "e5",
            "providerId": "auth-username-password-form",
            "requirement": "REQUIRED",
            "level": 1,
        },
        {
            "id": "e6",
            "displayName": "Browser - Conditional OTP",
            "requirement": "CONDITIONAL",
            "level": 1,
        },
        {"id": "e7", "providerId": "auth-otp-form", "requirement": "REQUIRED", "level": 2},
    ]


#: Keycloak 26.2's stock "Allowed Protocol Mapper Types" list
#: (`DefaultClientRegistrationPolicies.DEFAULT_ALLOWED_PROTOCOL_MAPPERS`).
STOCK_ALLOWED_MAPPERS = [
    "saml-user-attribute-mapper",
    "oidc-usermodel-attribute-mapper",
    "saml-user-property-mapper",
    "oidc-usermodel-property-mapper",
    "oidc-full-name-mapper",
    "oidc-address-mapper",
    "oidc-sha256-pairwise-sub-mapper",
    "saml-role-list-mapper",
]

_REGISTRATION_POLICY = "org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy"


def _stock_registration_policies() -> list[dict[str, Any]]:
    """What `DefaultClientRegistrationPolicies.addDefaultPolicies` gives a
    realm, as the admin API reads it back (an empty list stores no row, so
    `allowed-client-scopes` is simply absent)."""

    def component(cid: str, name: str, provider: str, sub: str, config: dict[str, Any]) -> Any:
        return {
            "id": cid,
            "name": name,
            "providerId": provider,
            "providerType": _REGISTRATION_POLICY,
            "parentId": "realm-id",
            "subType": sub,
            "config": config,
        }

    return [
        component(
            "stock-trusted",
            "Trusted Hosts",
            "trusted-hosts",
            "anonymous",
            {
                "host-sending-registration-request-must-match": ["true"],
                "client-uris-must-match": ["true"],
            },
        ),
        component("stock-consent", "Consent Required", "consent-required", "anonymous", {}),
        component("stock-scope", "Full Scope Disabled", "scope", "anonymous", {}),
        component(
            "stock-max", "Max Clients Limit", "max-clients", "anonymous", {"max-clients": ["200"]}
        ),
        component(
            "stock-anon-mappers",
            "Allowed Protocol Mapper Types",
            "allowed-protocol-mappers",
            "anonymous",
            {"allowed-protocol-mapper-types": list(STOCK_ALLOWED_MAPPERS)},
        ),
        component(
            "stock-anon-scopes",
            "Allowed Client Scopes",
            "allowed-client-templates",
            "anonymous",
            {"allow-default-scopes": ["true"]},
        ),
        component(
            "stock-auth-mappers",
            "Allowed Protocol Mapper Types",
            "allowed-protocol-mappers",
            "authenticated",
            {"allowed-protocol-mapper-types": list(STOCK_ALLOWED_MAPPERS)},
        ),
        component(
            "stock-auth-scopes",
            "Allowed Client Scopes",
            "allowed-client-templates",
            "authenticated",
            {"allow-default-scopes": ["true"]},
        ),
    ]



class FakeRealm:
    """An in-memory realm that records every call, in order.

    Not a mock of HTTP -- a model of the realm's STATE, so a second `run_init`
    against the same instance is genuinely the second run against a realm the
    first one already changed. That is what makes the idempotence assertions
    below mean anything: a call-counting mock would pass them while the real
    job created a duplicate client every morning.
    """

    def __init__(
        self,
        *,
        bootstrap_password: str = "the-env-bootstrap-password",
        settings: dict[str, Any] | None = None,
        required_actions: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.calls: list[str] = []
        #: clientId -> {"uuid", "representation", "secret", "roles"}
        self.clients: dict[str, dict[str, Any]] = {}
        self.bootstrap_password = bootstrap_password
        #: A call name that must raise the first time it is reached.
        self.fail_once_on: str | None = None
        self._serial = 0
        #: THE REALM AS AN EXISTING DEPLOYMENT ACTUALLY HAS IT. The default
        #: models the operator's live realm on 2026-08-20: built by an older
        #: realm file, so it carries an SMTP block somebody configured through
        #: Settings -> Email and exactly ONE registered required action.
        #: Keycloak's import does not merge a supplied `requiredActions` array
        #: with the defaults -- if it is present, it IS the set -- which is why
        #: `UPDATE_PASSWORD` and `webauthn-register-passwordless` are missing
        #: here and why the console's links to them did nothing.
        self.settings: dict[str, Any] = (
            {"smtpServer": {"host": "smtp.example.com", "from": "itms@toeshee.com"}}
            if settings is None
            else dict(settings)
        )
        #: alias -> flat execution list, as the admin API reports it. Seeded
        #: with Keycloak's stock browser flow, because that is what the job
        #: copies on a realm that has never had a passkey flow.
        self.flows: dict[str, list[dict[str, Any]]] = {
            "browser": _stock_browser_executions()
        }
        self.required_actions: dict[str, dict[str, Any]] = (
            {
                "CONFIGURE_TOTP": {
                    "alias": "CONFIGURE_TOTP",
                    "name": "Configure OTP",
                    "enabled": True,
                    "defaultAction": True,
                }
            }
            if required_actions is None
            else {k: dict(v) for k, v in required_actions.items()}
        )
        #: THE MCP SURFACES (2026-10-14), as a Keycloak 26.2 realm that predates
        #: them has them: the built-in client scopes and default-optional list
        #: (`DefaultClientScopes`), the stock registration policies
        #: (`DefaultClientRegistrationPolicies.addDefaultPolicies`) -- Trusted
        #: Hosts with an empty list, which refuses every anonymous registration
        #: -- and no client profiles or policies of the realm's own.
        self.client_scopes: dict[str, dict[str, Any]] = {
            name: {
                "id": f"scope-{name}",
                "name": name,
                "protocol": "openid-connect",
                "attributes": {},
                "mappers": [],
            }
            for name in (
                "acr",
                "address",
                "basic",
                "email",
                "microprofile-jwt",
                "offline_access",
                "organization",
                "phone",
                "profile",
                "roles",
                "web-origins",
            )
        }
        self.default_optional_scope_ids: list[str] = [
            "scope-address",
            "scope-microprofile-jwt",
            "scope-offline_access",
            "scope-organization",
            "scope-phone",
        ]
        self.components: dict[str, dict[str, Any]] = {
            c["id"]: c for c in _stock_registration_policies()
        }
        self.client_profiles: dict[str, Any] = {"profiles": []}
        self.client_policies: dict[str, Any] = {"policies": []}
        #: RESERVED clientIds (fix round 3). In Keycloak these are ordinary
        #: clients; the fake keeps them in their own map so that the assertions
        #: above about `self.clients` keep meaning "the service clients, once
        #: each". `find_client`, `client_settings` and `update_client` see both.
        self.reserved_clients: dict[str, dict[str, Any]] = {}

    # -- the RealmAdmin protocol -------------------------------------------

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_once_on == name:
            self.fail_once_on = None
            raise RuntimeError(f"injected failure at {name}")

    def find_client(self, client_id: str) -> str | None:
        self._record(f"find_client:{client_id}")
        found = self.clients.get(client_id) or self.reserved_clients.get(client_id)
        return None if found is None else str(found["uuid"])

    def create_reserved_client(self, representation: dict[str, Any]) -> str:
        client_id = str(representation["clientId"])
        self._record(f"create_reserved_client:{client_id}")
        self._serial += 1
        uuid = f"uuid-{self._serial}"
        self.reserved_clients[client_id] = {
            "uuid": uuid,
            "representation": dict(representation),
            "secret": None,
            "roles": set(),
        }
        return uuid

    def create_client(self, representation: dict[str, Any]) -> str:
        client_id = str(representation["clientId"])
        self._record(f"create_client:{client_id}")
        self._serial += 1
        uuid = f"uuid-{self._serial}"
        self.clients[client_id] = {
            "uuid": uuid,
            "representation": dict(representation),
            "secret": f"secret-for-{client_id}",
            "roles": set(),
        }
        return uuid

    def client_secret(self, client_uuid: str) -> str:
        self._record(f"client_secret:{client_uuid}")
        return str(self._by_uuid(client_uuid)["secret"])

    def service_account_user_id(self, client_uuid: str) -> str:
        self._record(f"service_account_user_id:{client_uuid}")
        return f"svc-{client_uuid}"

    def granted_roles(self, user_id: str) -> frozenset[str]:
        self._record(f"granted_roles:{user_id}")
        return frozenset(self._by_service_user(user_id)["roles"])

    def grant_roles(self, user_id: str, roles: tuple[str, ...]) -> None:
        self._record(f"grant_roles:{user_id}:{','.join(roles)}")
        self._by_service_user(user_id)["roles"].update(roles)

    def set_bootstrap_password(self, password: str) -> None:
        self._record("set_bootstrap_password")
        self.bootstrap_password = password

    # -- the realm itself, added 2026-08-20 --------------------------------

    def realm_settings(self) -> dict[str, Any]:
        self._record("realm_settings")
        return dict(self.settings)

    def update_realm(self, changes: dict[str, Any]) -> None:
        self._record(f"update_realm:{','.join(sorted(changes))}")
        self.settings.update(changes)

    def registered_required_actions(self) -> dict[str, dict[str, Any]]:
        self._record("registered_required_actions")
        return {k: dict(v) for k, v in self.required_actions.items()}

    def register_required_action(self, alias: str, name: str) -> None:
        self._record(f"register_required_action:{alias}")
        self.required_actions[alias] = {
            "alias": alias,
            "name": name,
            "enabled": False,
            "defaultAction": False,
        }

    def update_required_action(self, alias: str, representation: dict[str, Any]) -> None:
        self._record(f"update_required_action:{alias}")
        self.required_actions[alias].update(representation)

    def client_settings(self, client_uuid: str) -> dict[str, Any]:
        self._record(f"client_settings:{client_uuid}")
        return dict(self._by_uuid(client_uuid)["representation"])

    def update_client(self, client_uuid: str, changes: dict[str, Any]) -> None:
        self._record(f"update_client:{','.join(sorted(changes))}")
        self._by_uuid(client_uuid)["representation"].update(changes)

    # -- authentication flows ----------------------------------------------

    def flow_aliases(self) -> frozenset[str]:
        self._record("flow_aliases")
        return frozenset(self.flows)

    def copy_flow(self, source_alias: str, new_alias: str) -> None:
        self._record(f"copy_flow:{source_alias}->{new_alias}")
        self.flows[new_alias] = [dict(e) for e in self.flows[source_alias]]

    def flow_executions(self, alias: str) -> list[dict[str, Any]]:
        self._record(f"flow_executions:{alias}")
        return [dict(e) for e in self.flows[alias]]

    def add_execution(self, alias: str, provider: str) -> None:
        self._record(f"add_execution:{alias}:{provider}")
        # Keycloak adds a new execution DISABLED, at the top level. Modelling
        # that is the point: a fake that added it ALTERNATIVE would let the
        # job forget the enabling step and still pass.
        self.flows[alias].append(
            {
                "id": f"new-{provider}",
                "providerId": provider,
                "requirement": "DISABLED",
                "level": 0,
            }
        )

    def update_execution(self, alias: str, representation: dict[str, Any]) -> None:
        self._record(f"update_execution:{alias}:{representation.get('requirement')}")
        for execution in self.flows[alias]:
            if execution["id"] == representation["id"]:
                execution.update(representation)

    # -- the MCP surfaces (2026-10-14) ---------------------------------------

    def list_client_scopes(self) -> list[dict[str, Any]]:
        self._record("list_client_scopes")
        return [
            {k: v for k, v in s.items() if k != "mappers"} for s in self.client_scopes.values()
        ]

    def create_client_scope(self, representation: dict[str, Any]) -> str:
        name = str(representation["name"])
        self._record(f"create_client_scope:{name}")
        self.client_scopes[name] = {
            "id": f"scope-{name}",
            **{k: v for k, v in representation.items() if k != "protocolMappers"},
            "mappers": [],
        }
        return f"scope-{name}"

    def update_client_scope(self, scope_id: str, representation: dict[str, Any]) -> None:
        scope = self._scope(scope_id)
        self._record(f"update_client_scope:{scope['name']}")
        # `RepresentationToModel.updateClientScope`: attributes are set key by
        # key, never replaced as a map.
        for key in ("name", "description", "protocol"):
            if representation.get(key) is not None:
                scope[key] = representation[key]
        scope["attributes"] = {
            **scope.get("attributes", {}),
            **(representation.get("attributes") or {}),
        }

    def _scope(self, scope_id: str) -> dict[str, Any]:
        return next(s for s in self.client_scopes.values() if s["id"] == scope_id)

    def list_scope_mappers(self, scope_id: str) -> list[dict[str, Any]]:
        self._record(f"list_scope_mappers:{scope_id}")
        return json.loads(json.dumps(self._scope(scope_id)["mappers"]))

    def create_scope_mapper(self, scope_id: str, representation: dict[str, Any]) -> None:
        self._record(f"create_scope_mapper:{representation['name']}")
        self._serial += 1
        mapper = json.loads(json.dumps(representation))
        mapper["id"] = f"mapper-{self._serial}"
        self._scope(scope_id)["mappers"].append(mapper)

    def update_scope_mapper(
        self, scope_id: str, mapper_id: str, representation: dict[str, Any]
    ) -> None:
        self._record(f"update_scope_mapper:{representation['name']}")
        mappers = self._scope(scope_id)["mappers"]
        for i, mapper in enumerate(mappers):
            if mapper["id"] == mapper_id:
                mappers[i] = {**json.loads(json.dumps(representation)), "id": mapper_id}

    def get_default_optional_scopes(self) -> list[dict[str, Any]]:
        self._record("get_default_optional_scopes")
        return [
            {"id": s["id"], "name": s["name"]}
            for s in self.client_scopes.values()
            if s["id"] in self.default_optional_scope_ids
        ]

    def add_default_optional_scope(self, scope_id: str) -> None:
        self._record(f"add_default_optional_scope:{scope_id}")
        self.default_optional_scope_ids.append(scope_id)

    def list_components(self, provider_type: str) -> list[dict[str, Any]]:
        self._record(f"list_components:{provider_type}")
        return [
            json.loads(json.dumps(c))
            for c in self.components.values()
            if c["providerType"] == provider_type
        ]

    def create_component(self, representation: dict[str, Any]) -> None:
        self._record(f"create_component:{representation['providerId']}")
        self._serial += 1
        component = json.loads(json.dumps(representation))
        component["id"] = f"component-{self._serial}"
        # Keycloak stores no row for an empty list, so it reads back absent.
        component["config"] = {k: v for k, v in component.get("config", {}).items() if v}
        self.components[component["id"]] = component

    def update_component(self, component_id: str, representation: dict[str, Any]) -> None:
        current = self.components[component_id]
        self._record(f"update_component:{current['providerId']}")
        # `RepresentationToModel.updateComponent`: a key sent with an empty
        # list is REMOVED, a key not sent is KEPT.
        config = dict(current.get("config", {}))
        for key, values in representation.get("config", {}).items():
            if values:
                config[key] = list(values)
            else:
                config.pop(key, None)
        self.components[component_id] = {**current, "config": config}

    def delete_component(self, component_id: str) -> None:
        self._record(f"delete_component:{self.components[component_id]['providerId']}")
        del self.components[component_id]

    def get_client_profiles(self) -> dict[str, Any]:
        self._record("get_client_profiles")
        return json.loads(json.dumps(self.client_profiles))

    def put_client_profiles(self, representation: dict[str, Any]) -> None:
        self._record("put_client_profiles")
        self.client_profiles = json.loads(json.dumps(representation))

    def get_client_policies(self) -> dict[str, Any]:
        self._record("get_client_policies")
        return json.loads(json.dumps(self.client_policies))

    def put_client_policies(self, representation: dict[str, Any]) -> None:
        self._record("put_client_policies")
        # Keycloak refuses a policy naming a profile the realm does not have,
        # which is what makes the profiles-then-policies order load-bearing.
        known = {p["name"] for p in self.client_profiles.get("profiles", [])}
        for policy in representation.get("policies", []):
            missing = set(policy.get("profiles", [])) - known
            if missing:
                raise RuntimeError(f"policy names unknown profiles {sorted(missing)}")
        self.client_policies = json.loads(json.dumps(representation))

    def scope_mappers(self, name: str) -> dict[str, dict[str, Any]]:
        return {m["name"]: m for m in self.client_scopes[name]["mappers"]}

    def anonymous_policies(self) -> dict[str, dict[str, Any]]:
        return {
            c["providerId"]: c for c in self.components.values() if c.get("subType") == "anonymous"
        }

    def seed_client(self, client_id: str, representation: dict[str, Any]) -> str:
        """A client the realm import created, which this job never creates."""
        self._serial += 1
        uuid = f"uuid-{self._serial}"
        self.clients[client_id] = {
            "uuid": uuid,
            "representation": {"clientId": client_id, **representation},
            "secret": f"secret-for-{client_id}",
            "roles": set(),
        }
        return uuid

    # -- helpers ------------------------------------------------------------

    def _by_uuid(self, client_uuid: str) -> dict[str, Any]:
        for entry in [*self.clients.values(), *self.reserved_clients.values()]:
            if entry["uuid"] == client_uuid:
                return entry
        raise KeyError(client_uuid)

    def _by_service_user(self, user_id: str) -> dict[str, Any]:
        return self._by_uuid(user_id.removeprefix("svc-"))

    def roles_of(self, client_id: str) -> set[str]:
        return set(self.clients[client_id]["roles"])


def _secret_files(secrets_dir: Path) -> dict[str, str]:
    return {p.name: p.read_text().strip() for p in sorted(secrets_dir.iterdir())}


def _client_secret_files(secrets_dir: Path) -> dict[str, str]:
    """Everything except the rotated bootstrap password.

    That one is a fresh random value on every run BY DESIGN -- it is the
    credential being retired, and re-rotating it costs nothing since nobody
    holds it -- so it is the one file that must not be compared across runs.
    """
    written = _secret_files(secrets_dir)
    written.pop("keycloak-bootstrap-admin.secret", None)
    return written


# --- the script is where this test thinks it is ---------------------------


def test_the_script_exists_where_compose_will_look_for_it() -> None:
    """Non-vacuity, first order. Everything below imports this path; a moved or
    renamed file must fail here and say so, not surface as a confusing import
    error thirty assertions later."""
    assert SCRIPT.is_file(), f"{SCRIPT} does not exist; this test measures nothing"


# --- what gets created, and with what authority ---------------------------


def test_one_run_creates_both_service_clients(tmp_path: Path) -> None:
    realm = FakeRealm()

    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert set(realm.clients) == {"it-suite-provisioning", "it-suite-events"}


def test_the_widening_touches_exactly_one_client(tmp_path: Path) -> None:
    """The credential accounting in the design doc's §4, asserted.

    Provisioning widens to `manage-realm` because ITMS now writes the realm's
    SMTP block. The event sweep does not, and must not inherit it: its whole
    justification is that a `view-events`-only credential's worst case is an
    information leak, while `manage-users` is every account in the realm. One
    line granting both to both would erase that distinction silently, and
    nothing else in the tree would notice.
    """
    realm = FakeRealm()

    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert realm.roles_of("it-suite-provisioning") == {"manage-users", "manage-realm"}
    assert realm.roles_of("it-suite-events") == {"view-events"}


def test_the_service_clients_cannot_be_used_as_a_login(tmp_path: Path) -> None:
    """GO-LIVE §2's warning, made structural.

    A confidential client with the direct access grant enabled turns a leaked
    secret into a password-grant endpoint with no browser and no MFA prompt.
    These two clients exist to hold a service account; they have no interactive
    flow to enable.
    """
    realm = FakeRealm()

    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    for entry in realm.clients.values():
        representation = entry["representation"]
        assert representation["serviceAccountsEnabled"] is True
        assert representation["publicClient"] is False
        assert representation["standardFlowEnabled"] is False
        assert representation["directAccessGrantsEnabled"] is False


# --- idempotence ----------------------------------------------------------


def test_running_twice_creates_one_of_each_client(tmp_path: Path) -> None:
    """The normal case, not the exceptional one: compose starts a one-shot
    service again on every `docker compose up`, so this job runs on every
    deploy of the appliance's life."""
    realm = FakeRealm()

    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)
    realm.calls.clear()
    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert set(realm.clients) == {"it-suite-provisioning", "it-suite-events"}
    assert [c for c in realm.calls if c.startswith("create_client")] == []


def test_a_second_run_grants_no_role_it_already_granted(tmp_path: Path) -> None:
    realm = FakeRealm()

    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)
    realm.calls.clear()
    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert [c for c in realm.calls if c.startswith("grant_roles")] == []


def test_a_second_run_restores_a_secret_file_that_was_lost(tmp_path: Path) -> None:
    """Self-healing, and the reason the job re-reads the secret every time.

    A restore that brings back the database but not the `its-secrets` volume
    leaves the clients in the realm and no secret on disk. Re-running the job
    must repair that; a job that only wrote secrets for clients it had just
    created would report success and leave the platform unable to provision a
    single user.
    """
    realm = FakeRealm()
    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)
    before = _client_secret_files(tmp_path)
    assert before, "nothing was written; this assertion is vacuous"
    (tmp_path / "keycloak-admin.secret").unlink()

    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert _client_secret_files(tmp_path) == before


# --- the secrets on disk --------------------------------------------------


def test_both_client_secrets_are_written_to_the_volume(tmp_path: Path) -> None:
    realm = FakeRealm()

    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    written = _secret_files(tmp_path)
    assert written["keycloak-admin.secret"] == "secret-for-it-suite-provisioning"
    assert written["keycloak-events.secret"] == "secret-for-it-suite-events"


def test_every_secret_the_job_writes_is_mode_600(tmp_path: Path) -> None:
    """Including the rotated bootstrap password.

    The volume is shared with the api, gateway and worker containers. World- or
    group-readable key material there is readable by every process in the
    stack, which is the difference between one compromised container and the
    whole realm.
    """
    realm = FakeRealm()

    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    written = sorted(p.name for p in tmp_path.iterdir())
    assert written, "the job wrote nothing; this assertion is vacuous"
    for path in tmp_path.iterdir():
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"{path.name} is mode {mode:o}, not 600"


def test_the_secrets_never_reach_the_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The job's own output goes to a container log `docker logs` will happily
    print. It may say what it did; it may never say what the values are."""
    realm = FakeRealm()

    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    output = capsys.readouterr()
    printed = output.out + output.err
    for secret in _secret_files(tmp_path).values():
        assert secret not in printed


# --- retiring the bootstrap admin -----------------------------------------


def test_the_bootstrap_admin_is_retired_only_after_the_clients_exist(
    tmp_path: Path,
) -> None:
    """THE ORDERING INVARIANT. See this module's docstring.

    Rotating first would leave a job that cannot authenticate on its next run,
    and its next run is the normal case. Asserted as a position in the recorded
    call order rather than as a comment, because a refactor that hoists the
    rotation to the top reads perfectly well and breaks nothing until the
    second `docker compose up`.
    """
    realm = FakeRealm()

    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    rotation = realm.calls.index("set_bootstrap_password")
    creations = [i for i, c in enumerate(realm.calls) if c.startswith("create_client")]
    grants = [i for i, c in enumerate(realm.calls) if c.startswith("grant_roles")]
    assert creations and grants, "nothing was created; the ordering assertion is vacuous"
    assert rotation > max(creations + grants)


def test_the_rotated_password_is_on_disk_before_it_is_in_the_realm(
    tmp_path: Path,
) -> None:
    """Write-then-apply, and the order is not arbitrary.

    Apply-then-write loses the password permanently if the process dies in
    between: the account exists, nobody knows its password, and the vendor's
    break-glass is gone for good. Write-then-apply's failure mode is a file
    holding a password that is not in effect, which the candidate list below
    recovers from on the next run.
    """
    realm = FakeRealm()
    realm.fail_once_on = "set_bootstrap_password"

    with pytest.raises(RuntimeError):
        init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    stored = (tmp_path / "keycloak-bootstrap-admin.secret").read_text().strip()
    assert stored, "the rotated password was applied without being recorded anywhere"
    assert realm.bootstrap_password == "the-env-bootstrap-password"


def test_the_bootstrap_admin_is_not_deleted(tmp_path: Path) -> None:
    """The vendor needs one master account to exist. Retired means nobody knows
    the password, not that the account is gone -- a realm with no master admin
    at all cannot be repaired from outside itself."""
    realm = FakeRealm()

    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert realm.bootstrap_password
    assert realm.bootstrap_password != "the-env-bootstrap-password"
    assert realm.bootstrap_password == (
        (tmp_path / "keycloak-bootstrap-admin.secret").read_text().strip()
    )


def test_the_rotated_password_is_not_the_one_it_replaced(tmp_path: Path) -> None:
    realm = FakeRealm()

    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)
    first = realm.bootstrap_password
    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert realm.bootstrap_password != first
    assert len(first) >= 32


# --- authenticating a re-run ----------------------------------------------


def test_the_stored_password_is_tried_before_the_one_from_the_environment(
    tmp_path: Path,
) -> None:
    """Which credential a re-run authenticates with.

    After the first run the environment's `KEYCLOAK_ADMIN_PASSWORD` is stale --
    the job rotated it away. The volume holds the live one, so it is tried
    first. The environment value stays in the list as the fallback for the
    very first run and for the crash-between-write-and-apply case above.
    """
    path = tmp_path / "keycloak-bootstrap-admin.secret"
    path.write_text("the-rotated-password\n")

    assert init.bootstrap_password_candidates("from-env", tmp_path) == (
        "the-rotated-password",
        "from-env",
    )


def test_before_the_first_run_only_the_environment_has_a_password(
    tmp_path: Path,
) -> None:
    assert init.bootstrap_password_candidates("from-env", tmp_path) == ("from-env",)


def test_a_blank_stored_password_is_not_offered_as_a_candidate(
    tmp_path: Path,
) -> None:
    """A zero-byte file is what a crash mid-write leaves. Presenting `""` as a
    password produces an authentication failure that reads like a wrong
    password rather than like a truncated file."""
    (tmp_path / "keycloak-bootstrap-admin.secret").write_text("   \n")

    assert init.bootstrap_password_candidates("from-env", tmp_path) == ("from-env",)


# --- re-runnable after a partial failure ----------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        "create_client:it-suite-events",
        "grant_roles:svc-uuid-1:manage-realm,manage-users",
        "set_bootstrap_password",
    ],
)
def test_a_failure_part_way_leaves_the_job_re_runnable(tmp_path: Path, failure: str) -> None:
    """Every partial state this job can be interrupted in must be repaired by
    running it again -- which is all an operator can do, since the job has no
    other interface. Parameterised over one failure point per phase rather than
    asserted once, because "re-runnable" is a property of each phase, not of
    the job as a whole.
    """
    realm = FakeRealm()
    realm.fail_once_on = failure

    with pytest.raises(RuntimeError):
        init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    # The re-run authenticates the way the entrypoint would.
    candidates = init.bootstrap_password_candidates("the-env-bootstrap-password", tmp_path)
    assert realm.bootstrap_password in candidates

    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert set(realm.clients) == {"it-suite-provisioning", "it-suite-events"}
    assert realm.roles_of("it-suite-provisioning") == {"manage-users", "manage-realm"}
    assert realm.roles_of("it-suite-events") == {"view-events"}
    written = _secret_files(tmp_path)
    assert written["keycloak-admin.secret"] == "secret-for-it-suite-provisioning"
    assert written["keycloak-events.secret"] == "secret-for-it-suite-events"
    assert realm.bootstrap_password == written["keycloak-bootstrap-admin.secret"]


# --- the compose service that runs it -------------------------------------


def _keycloak_init_service() -> str:
    """The `keycloak-init:` block, as text.

    A YAML parse would be stricter, but this test must not require PyYAML to be
    a backend dependency for a deploy-only assertion; the block is read to the
    next top-level service key.
    """
    text = COMPOSE.read_text()
    start = text.index("\n  keycloak-init:")
    rest = text[start + 1 :]
    lines = rest.splitlines()
    body = [lines[0]]
    for line in lines[1:]:
        if line and not line.startswith("   ") and not line.startswith("#"):
            break
        body.append(line)
    return "\n".join(body)


def test_compose_runs_the_init_job() -> None:
    service = _keycloak_init_service()
    assert "./keycloak/init-clients.py" in service, (
        "the init job's script is not mounted; the service would run whatever "
        "the image happens to contain at that path"
    )
    assert "./keycloak/init-clients.py:/init/init-clients.py:ro" in service, (
        "the script is not mounted read-only; a container that can rewrite it "
        "can change what the next `docker compose up` does to the realm"
    )
    assert "its-secrets:/var/lib/its/secrets" in service, (
        "the init job does not mount the secrets volume, so the secrets it "
        "writes vanish with the container and nothing can read them"
    )
    assert 'restart: "no"' in service, "a one-shot job must not be restarted on exit"


# --- realm convergence (2026-08-20) ---------------------------------------
#
# WHY THIS HALF EXISTS AT ALL. A Keycloak realm is imported from
# `deploy/keycloak/realm-it-suite.json` ONLY when the realm does not already
# exist. Every deployment past its first boot therefore ignores that file
# completely, and three failures on the operator's live install were the same
# fact wearing three hats:
#
#   * "Change password" and "Add a passkey" did nothing. The realm that built
#     that install declares a `requiredActions` array of ONE entry, and
#     Keycloak's import does not merge it with the defaults -- if the array is
#     present, it is the whole set. `UPDATE_PASSWORD` and
#     `webauthn-register-passwordless` were never registered, and `kc_action`
#     naming an unregistered action is ignored: silent re-auth, silent return.
#   * Sign-out dead-ended on Keycloak's "Invalid redirect uri". The live
#     `it-suite-web` client still carries `redirectUris: ["https://*/auth/
#     callback"]`; a wildcard is honoured only in the trailing position, so
#     `post.logout.redirect.uris: "+"` resolves to a set matching nothing.
#   * The login theme, the passkey policy and `editUsername` were all correct
#     in the file and absent from the realm.
#
# So the file becomes the desired state on BOTH paths: imported on a fresh
# install, converged onto a running one, by this job, on every `docker compose
# up`.


def _converged() -> tuple[Any, Any]:
    realm = FakeRealm()
    realm.seed_client(
        "it-suite-web",
        {
            "rootUrl": "https://localhost",
            "redirectUris": ["https://*/auth/callback"],
            "attributes": {"post.logout.redirect.uris": "+"},
        },
    )
    return realm, realm


def test_the_realm_file_is_the_only_source_of_the_desired_state(tmp_path: Path) -> None:
    """Not a copy of it in the script. The shipped file is loaded here and its
    values are what the assertions below compare against, so a change to the
    file that the script cannot carry fails HERE rather than on a customer's
    realm."""
    desired = init.desired_realm_settings(REALM_FILE)

    assert desired["loginTheme"] == REALM_FILE["loginTheme"]
    assert (
        desired["webAuthnPolicyPasswordlessRequireResidentKey"]
        == (REALM_FILE["webAuthnPolicyPasswordlessRequireResidentKey"])
    )


def test_the_smtp_block_is_never_converged(tmp_path: Path) -> None:
    """THE ONE THAT WOULD HAVE COST THE OPERATOR THEIR EMAIL CONFIGURATION.

    Settings -> Email writes the realm's `smtpServer` through the provisioning
    client's `manage-realm` grant. This job runs on every `docker compose up`.
    A convergence that carried the whole realm representation from the file --
    or that took `smtpServer` as one of its keys -- would silently revert the
    operator's mail settings on every single deploy, and the symptom would be
    'email stopped working again' with nothing in any log connecting it to the
    upgrade.

    So convergence is an ALLOWLIST of realm keys, and this test is the guard
    on that list. It is not a style preference: the failure it prevents is
    invisible until somebody correlates two unrelated events."""
    assert "smtpServer" not in init.desired_realm_settings(REALM_FILE)
    assert "smtpServer" not in init.CONVERGED_REALM_KEYS

    realm, _ = _converged()
    before = dict(realm.settings["smtpServer"])
    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert realm.settings["smtpServer"] == before


def test_the_missing_required_actions_are_registered_and_enabled(tmp_path: Path) -> None:
    """The operator's live bug, as a test. The realm starts as theirs did --
    `CONFIGURE_TOTP` and nothing else -- and both actions the console's
    profile page links to must exist and be enabled afterwards."""
    realm, _ = _converged()
    assert set(realm.required_actions) == {"CONFIGURE_TOTP"}

    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    for alias in ("UPDATE_PASSWORD", "webauthn-register-passwordless"):
        assert alias in realm.required_actions, f"{alias} still unregistered"
        assert realm.required_actions[alias]["enabled"] is True


def test_an_action_the_file_ships_disabled_is_not_enabled(tmp_path: Path) -> None:
    """`TERMS_AND_CONDITIONS` and `delete_account` ship `enabled: false`.
    Registering every alias in the file and turning them all on would put a
    terms-and-conditions gate in front of every sign-in on the fleet."""
    disabled = [
        action["alias"]
        for action in REALM_FILE["requiredActions"]
        if not action.get("enabled", False)
    ]
    assert disabled, "the fixture assumption is gone; this test proves nothing"

    realm, _ = _converged()
    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    for alias in disabled:
        assert realm.required_actions.get(alias, {}).get("enabled", False) is False
        # AND NOT REGISTERED EITHER. Weaker versions of this test asserted only
        # the line above, and passed while the job registered every alias in the
        # file -- harmless in effect, but it left the `enabled` filter in
        # `required_actions_to_register` with nothing holding it in place, and
        # that filter is one edit away from being the fleet-wide
        # terms-and-conditions gate this test is named after.
        assert alias not in realm.required_actions, (
            f"{alias} ships disabled and the realm had never registered it"
        )


def test_the_web_clients_redirect_uris_are_corrected(tmp_path: Path) -> None:
    """The sign-out BLOCKER. `https://*/auth/callback` matches nothing --
    Keycloak honours a wildcard only as a trailing `*` -- so the logout call's
    `post_logout_redirect_uri` is refused and the user is stranded on a raw
    error page with no link back."""
    realm, _ = _converged()

    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    web = realm.clients["it-suite-web"]["representation"]
    assert web["redirectUris"] == REALM_FILE_WEB["redirectUris"]
    assert web["rootUrl"] == REALM_FILE_WEB["rootUrl"]
    assert "https://*/auth/callback" not in web["redirectUris"]


def test_a_client_list_in_another_order_is_not_a_change() -> None:
    """FOUND LIVE (MCP task 12, the first CI run against a real realm).
    Keycloak stores `redirectUris` and `webOrigins` as sets and returns them in
    its own order -- for the shipped realm file, the reverse of the file's --
    so an order-sensitive comparison PUT `it-suite-web` on every run and
    printed `clients corrected=['it-suite-web']` for a realm nobody touched."""
    file_order = {"redirectUris": ["/auth/callback", "http://localhost:5173/auth/callback"]}
    keycloak_order = {"redirectUris": ["http://localhost:5173/auth/callback", "/auth/callback"]}

    assert init.client_settings_to_apply(file_order, keycloak_order) == {}
    # A real difference is still one.
    assert init.client_settings_to_apply(file_order, {"redirectUris": ["/auth/callback"]}) == (
        file_order
    )


def test_a_realm_already_in_the_desired_state_is_not_written_to(tmp_path: Path) -> None:
    """IDEMPOTENCE, AS A WRITE COUNT rather than as a final-state check. This
    job runs on every `up`; a convergence that PUT the same values every time
    would work perfectly and fill the realm's admin event log with a change
    record per deploy, which is where a real unauthorised change would then be
    invisible."""
    realm, _ = _converged()
    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    realm.calls.clear()
    result = init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    writes = [
        c
        for c in realm.calls
        if c.startswith(("update_realm", "update_client", "register_required_action"))
    ]
    assert writes == []
    assert result.realm_changed == ()
    assert result.clients_updated == ()


def test_convergence_happens_after_provisioning_and_before_the_rotation(
    tmp_path: Path,
) -> None:
    """ORDER, and both halves of it are load-bearing.

    AFTER the clients: this is the newer code, and a bug in it must not be
    able to stop a fresh install from getting the provisioning client that its
    setup screen needs. A failure here leaves an install that can create its
    first administrator but has a stale theme -- the other order leaves an
    appliance nobody can log into.

    BEFORE the rotation: convergence authenticates with the bootstrap admin's
    current password, and the rotation is what invalidates it."""
    realm, _ = _converged()
    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    secrets_written = realm.calls.index("client_secret:uuid-3")
    converged = realm.calls.index("realm_settings")
    rotated = realm.calls.index("set_bootstrap_password")

    assert secrets_written < converged < rotated


def test_a_realm_without_the_web_client_is_left_alone(tmp_path: Path) -> None:
    """The client is the realm import's to create, not this job's. Creating a
    public browser client -- with its redirect URIs, which are an open-redirect
    surface if got wrong -- is not a decision to make from a convergence loop
    that cannot see why the client is missing."""
    realm = FakeRealm()

    result = init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert "it-suite-web" not in realm.clients
    assert result.clients_updated == ()


def test_convergence_failing_leaves_the_provisioning_it_already_did(
    tmp_path: Path,
) -> None:
    """A failure in the newer half must not cost the older half's work. The
    secrets are on the volume before convergence begins, so the setup screen
    works on the next request even though this run died."""
    realm, _ = _converged()
    realm.fail_once_on = "realm_settings"

    with pytest.raises(RuntimeError):
        init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert _client_secret_files(tmp_path) == {
        "keycloak-admin.secret": "secret-for-it-suite-provisioning",
        "keycloak-events.secret": "secret-for-it-suite-events",
    }


def test_compose_mounts_the_realm_file_into_the_init_job() -> None:
    """The convergence has no desired state without it, and the failure would
    otherwise be a job that exits non-zero on a customer's first upgrade."""
    service = _keycloak_init_service()

    assert "realm-it-suite.json" in service, (
        "keycloak-init cannot converge a realm whose file it cannot read"
    )


def test_the_live_caller_implements_every_method_the_job_calls() -> None:
    """THE GAP THE `# pragma: no cover` ON `HttpRealmAdmin` LEFT WIDE OPEN, and
    which cost the operator a deploy.

    This file's header says, correctly, that the only untested code is the HTTP
    verbs that carry the job's decisions to a live realm -- there is no realm in
    CI and there will not be one. What that argument quietly assumed is that the
    untested class at least EXISTS in the shape the tested code calls. On
    2026-08-20 it did not: the realm-convergence half added seven methods to the
    `RealmAdmin` protocol and to `FakeRealm`, and none to `HttpRealmAdmin`.
    Thirty-one tests passed. The job then died on the first line of convergence
    with `AttributeError`, on the operator's deployment, having already written
    the secrets -- so it looked like it had worked, and the bug it was deployed
    to fix was still there.

    A protocol in Python is structural and checked by a type checker, and this
    file is not type-checked (CI runs `mypy app`, and this script is in
    `deploy/`). So the agreement needs a test. It costs nothing, needs no realm,
    and catches the entire class of "added it to the interface and the fake, not
    to the implementation".

    `HttpBootstrapAdmin` rather than `HttpRealmAdmin` because that is what
    `main()` actually constructs -- checking the base class would miss a method
    the subclass was supposed to add.
    """
    required = {
        name
        for name in vars(init.RealmAdmin)
        if not name.startswith("_") and callable(getattr(init.RealmAdmin, name, None))
    }
    assert "register_required_action" in required, (
        "the protocol's method set could not be read; this test proves nothing"
    )

    missing = sorted(name for name in required if not hasattr(init.HttpBootstrapAdmin, name))

    assert missing == [], (
        f"{missing} are called by run_init and do not exist on the class that "
        "talks to a live realm; the job will raise AttributeError on deploy"
    )


# --- the passkey sign-in flow (2026-08-20) ---------------------------------
#
# THE OPERATOR ENROLLED A PASSKEY AND THE LOGIN PAGE DID NOT OFFER IT. Those
# are two different pieces of realm state: `webauthn-register-passwordless` (a
# required action, converged above) puts the credential on the account, and the
# BROWSER FLOW decides whether anything at sign-in will accept it. Stock
# Keycloak's browser flow has no WebAuthn execution at all.
#
# BINDING A BROWSER FLOW IS THE MOST DANGEROUS WRITE THIS JOB MAKES, and the
# runbook has said so from the beginning: a wrong graph does not fail loudly,
# it refuses every account including the one that would repair it, and the only
# way back is `kcadm.sh` inside the container. The operator chose automation
# with that stated. So the safety is structural rather than procedural:
#
#   1. the flow is a COPY of the stock browser flow, so the password leg is
#      whatever Keycloak shipped rather than something reconstructed here;
#   2. passkey is added as an ALTERNATIVE beside it, never in front of it, so
#      a WebAuthn execution that cannot run falls through to the password form;
#   3. the job REFUSES TO BIND unless it can see a working password
#      alternative in the flow it just built.
#
# (3) is the one that matters. Everything else is care; that is a check.


def test_a_flow_without_a_password_alternative_is_refused() -> None:
    """THE CHECK THAT STANDS BETWEEN THIS JOB AND A FLEET-WIDE LOCKOUT.

    Every other guard here is care taken while writing. This one is a
    measurement of the result: if the flow that was just built cannot sign
    somebody in with a password, it is not bound, whatever else is true of it.
    """
    passkey_only = [
        {
            "id": "e1",
            "providerId": "webauthn-authenticator-passwordless",
            "requirement": "ALTERNATIVE",
            "level": 0,
        }
    ]

    assert init.password_alternative_survives(passkey_only) is False
    assert init.password_alternative_survives(_stock_browser_executions()) is True


def test_a_password_form_that_is_disabled_does_not_count() -> None:
    """A DISABLED execution is present in the list and cannot authenticate
    anyone. Counting it would make the guard above pass on exactly the realm it
    exists to refuse."""
    executions = _stock_browser_executions()
    for execution in executions:
        if execution.get("providerId") == "auth-username-password-form":
            execution["requirement"] = "DISABLED"

    assert init.password_alternative_survives(executions) is False


def test_a_password_form_inside_a_disabled_subflow_does_not_count() -> None:
    """The subtler version: the form itself is REQUIRED, and the `forms`
    subflow containing it is DISABLED, so nothing reaches it. Reading the
    execution's own requirement alone would call this flow safe."""
    executions = _stock_browser_executions()
    for execution in executions:
        if execution.get("displayName") == "forms":
            execution["requirement"] = "DISABLED"

    assert init.password_alternative_survives(executions) is False


def test_the_passkey_flow_is_built_and_bound(tmp_path: Path) -> None:
    """The operator's report, closed: a passkey they had enrolled was not
    offered at sign-in, because the browser flow had nothing that accepts one."""
    realm, _ = _converged()

    result = init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert result.passkey_flow == "bound"
    assert realm.settings["browserFlow"] == "browser-passkey"
    passkey = [
        e
        for e in realm.flows["browser-passkey"]
        if e.get("providerId") == "webauthn-authenticator-passwordless"
    ]
    assert len(passkey) == 1
    # ALTERNATIVE, not REQUIRED: REQUIRED would demand a passkey from everybody
    # and lock out every account that does not have one -- the exact failure
    # this whole path is built to avoid.
    assert passkey[0]["requirement"] == "ALTERNATIVE"


def test_the_password_form_is_still_reachable_afterwards(tmp_path: Path) -> None:
    """THE PROPERTY THE OPERATOR WAS PROMISED. Adding passkey must leave the
    password leg working, so somebody without a passkey -- or with one that
    fails -- still gets in."""
    realm, _ = _converged()

    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert init.password_alternative_survives(realm.flows["browser-passkey"]) is True


def test_a_flow_that_lost_its_password_leg_is_never_bound(tmp_path: Path) -> None:
    """The guard, exercised through the whole job rather than as a unit.

    The realm's stock browser flow is rigged so the copy cannot sign anyone in
    with a password. The job must build the flow and REFUSE to bind it, leaving
    the realm on whatever it was using -- which is the difference between a
    cosmetic failure and an outage nobody can log in to fix.
    """
    realm, _ = _converged()
    realm.flows["browser"] = [
        {"id": "e1", "providerId": "auth-cookie", "requirement": "ALTERNATIVE", "level": 0}
    ]

    result = init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert result.passkey_flow == "built-not-bound"
    assert "browserFlow" not in realm.settings


def test_a_second_run_does_not_rebind_the_flow(tmp_path: Path) -> None:
    """Compose starts this job on every `up`. Rewriting the mechanism every
    account depends on, daily, is not something to do for no reason."""
    realm, _ = _converged()
    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    realm.calls.clear()
    result = init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert result.passkey_flow == "already-bound"
    assert [c for c in realm.calls if c.startswith(("copy_flow", "add_execution"))] == []


def test_the_passkey_flow_reports_its_writes_and_a_rerun_has_none(tmp_path: Path) -> None:
    """The job's log line, and CI's zero-writes check on a re-run (MCP task 12
    fix round 1), read these: the outcome alone says "already-bound" even for
    a run that had to re-add the passkey execution first."""
    realm, _ = _converged()

    first = init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)
    again = init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert first.passkey_flow_writes == ("copy_flow", "add_execution", "update_execution", "bind")
    assert again.passkey_flow_writes == ()
    assert init.passkey_summary(again) == (
        "identity init: passkey flow=already-bound writes=nothing"
    )
    assert init.passkey_summary(first) == (
        "identity init: passkey flow=bound "
        "writes=['copy_flow', 'add_execution', 'update_execution', 'bind']"
    )


def test_a_rebound_flow_that_lost_its_passkey_reports_the_repair(tmp_path: Path) -> None:
    realm, _ = _converged()
    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)
    realm.flows["browser-passkey"] = [
        e
        for e in realm.flows["browser-passkey"]
        if e.get("providerId") != "webauthn-authenticator-passwordless"
    ]

    result = init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert result.passkey_flow == "already-bound"
    assert result.passkey_flow_writes == ("add_execution", "update_execution")


def test_a_realm_with_no_browser_flow_to_copy_is_left_alone(tmp_path: Path) -> None:
    """Inventing a browser flow for a realm shaped in a way this job does not
    recognise is exactly the improvisation that produces a lockout."""
    realm, _ = _converged()
    realm.flows = {}

    result = init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert result.passkey_flow == "unchanged"
    assert "browserFlow" not in realm.settings


# --- the realm-settings allowlist (2026-09-23) -----------------------------
#
# `--import-realm` skips an existing realm, so a setting the repo changes
# reaches an existing host only through this job. The account console stayed
# OPEN on the live host because `account-console.enabled` was not on the list.
# The list is explicit: a key not named is never written, whatever the file
# says -- `smtpServer` above all, which Settings -> Email owns at runtime.


def _realm_with_account_clients(
    *, console_enabled: bool = True, account_enabled: bool = True
) -> Any:
    realm, _ = _converged()
    realm.seed_client(
        "account-console",
        {"enabled": console_enabled, "redirectUris": ["/realms/it-suite/account/*"]},
    )
    realm.seed_client("account", {"enabled": account_enabled})
    return realm


def test_the_realm_allowlist_is_explicit_names_not_prefixes() -> None:
    assert not hasattr(init, "CONVERGED_REALM_PREFIXES"), (
        "a prefix converges keys nobody listed; name each one"
    )
    desired = init.desired_realm_settings(REALM_FILE)
    assert set(desired) <= set(init.CONVERGED_REALM_KEYS)


def test_every_passkey_policy_key_in_the_file_is_on_the_allowlist() -> None:
    """Adding a key to the file without listing it would ship a setting that
    never reaches a live realm."""
    in_file = {k for k in REALM_FILE if k.startswith("webAuthnPolicyPasswordless")}
    assert in_file, "the fixture assumption is gone; this test proves nothing"
    assert in_file <= set(init.CONVERGED_REALM_KEYS)


def test_the_allowlist_names_the_settings_the_repo_owns() -> None:
    for key in ("loginTheme", "registrationAllowed", "bruteForceProtected"):
        assert key in init.CONVERGED_REALM_KEYS, key
    owned = {c.client_id: c for c in init.CONVERGED_CLIENTS}
    assert "enabled" in owned["account-console"].keys
    assert "enabled" in owned["account"].keys


def test_runtime_owned_settings_are_never_on_the_allowlist() -> None:
    """Settings -> Email writes `smtpServer`; the event log is switched on by
    hand; the passkey flow code owns `browserFlow`. None may be converged
    from the file."""
    runtime = set(init.RUNTIME_OWNED_REALM_KEYS)
    assert {"smtpServer", "eventsEnabled", "browserFlow"} <= runtime
    assert runtime.isdisjoint(init.CONVERGED_REALM_KEYS)


def test_a_file_key_outside_the_allowlist_is_never_written(tmp_path: Path) -> None:
    realm = _realm_with_account_clients()
    realm.settings["eventsEnabled"] = True
    realm.settings["sslRequired"] = "all"
    hostile = {
        **REALM_FILE,
        "smtpServer": {"host": "from-the-file"},
        "eventsEnabled": False,
        "sslRequired": "none",
    }

    init.run_init(realm, secrets_dir=tmp_path, realm_file=hostile)

    assert realm.settings["smtpServer"]["host"] == "smtp.example.com"
    assert realm.settings["eventsEnabled"] is True
    assert realm.settings["sslRequired"] == "all"


def test_an_open_account_console_is_closed(tmp_path: Path) -> None:
    """The drift that left the self-service portal open on the live host."""
    realm = _realm_with_account_clients(console_enabled=True)

    result = init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    console = realm.clients["account-console"]["representation"]
    assert console["enabled"] is False
    assert "account-console" in result.clients_updated
    # Only the owned field moved.
    assert console["redirectUris"] == ["/realms/it-suite/account/*"]


def test_the_account_client_is_kept_enabled(tmp_path: Path) -> None:
    """The action-token mail (enrolment, password reset) resolves it as the
    system client; disabled, no invitation can be sent."""
    realm = _realm_with_account_clients(account_enabled=False)

    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert realm.clients["account"]["representation"]["enabled"] is True


def test_a_missing_account_console_is_not_created(tmp_path: Path) -> None:
    realm, _ = _converged()

    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert "account-console" not in realm.clients
    assert not any(c.startswith("create_client:account") for c in realm.calls)


def test_the_web_clients_password_grant_is_switched_back_off(tmp_path: Path) -> None:
    """GO-LIVE asked the operator to turn it off by hand after a scripted
    first-boot check. The file says off, so the job does it."""
    realm, _ = _converged()
    realm.clients["it-suite-web"]["representation"]["directAccessGrantsEnabled"] = True

    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    web = realm.clients["it-suite-web"]["representation"]
    assert web["directAccessGrantsEnabled"] is False
    assert web["attributes"]["pkce.code.challenge.method"] == "S256"


def test_realm_security_flags_are_converged(tmp_path: Path) -> None:
    realm = _realm_with_account_clients()
    realm.settings.update({"registrationAllowed": True, "bruteForceProtected": False})

    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert realm.settings["registrationAllowed"] is False
    assert realm.settings["bruteForceProtected"] is True


def test_the_realms_languages_are_converged(tmp_path: Path) -> None:
    """THE SIGN-IN PAGES AND KEYCLOAK'S OWN EMAILS FOLLOW THE PERSON'S LANGUAGE
    only on a realm that has internationalization switched on. A realm file is
    imported once, so a live realm that predates the Spanish work would keep
    English-only pages whatever the file says -- and `locale` would not be a
    profile attribute there, so `PATCH /me`'s mirror to Keycloak would be
    refused. Spec 2026-10-09 §3.5.
    """
    for key in ("internationalizationEnabled", "supportedLocales", "defaultLocale"):
        assert key in init.CONVERGED_REALM_KEYS, key
    assert REALM_FILE["internationalizationEnabled"] is True
    assert REALM_FILE["supportedLocales"] == ["en", "es"]
    assert REALM_FILE["defaultLocale"] == "en"

    realm = _realm_with_account_clients()
    realm.settings.update(
        {
            "internationalizationEnabled": False,
            "supportedLocales": [],
            "defaultLocale": "",
        }
    )

    result = init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert realm.settings["internationalizationEnabled"] is True
    assert realm.settings["supportedLocales"] == ["en", "es"]
    assert realm.settings["defaultLocale"] == "en"
    assert {"internationalizationEnabled", "supportedLocales", "defaultLocale"} <= set(
        result.realm_changed
    )


def test_the_languages_the_realm_offers_are_the_two_the_console_writes() -> None:
    """`users.locale` only ever holds `en` or `es`; a third language offered on
    the sign-in page with no console catalogue behind it would be a language a
    person can pick there and never see again after signing in."""
    assert set(REALM_FILE["supportedLocales"]) == {"en", "es"}
    assert REALM_FILE["defaultLocale"] in REALM_FILE["supportedLocales"]


def test_an_action_the_file_disables_is_switched_off_when_live(tmp_path: Path) -> None:
    """A terms gate or self-service account deletion switched on live is
    switched back off: the file owns `enabled`."""
    realm = _realm_with_account_clients()
    for alias in ("TERMS_AND_CONDITIONS", "delete_account"):
        realm.required_actions[alias] = {
            "alias": alias,
            "name": alias,
            "enabled": True,
            "defaultAction": False,
        }

    result = init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    for alias in ("TERMS_AND_CONDITIONS", "delete_account"):
        assert realm.required_actions[alias]["enabled"] is False, alias
        assert alias in result.required_actions_updated


def test_the_default_action_flag_is_converged(tmp_path: Path) -> None:
    """`CONFIGURE_TOTP` is a default action in the file: every new account
    is asked to enrol MFA. A live realm that lost that flag is corrected."""
    realm = _realm_with_account_clients()
    realm.required_actions["CONFIGURE_TOTP"]["defaultAction"] = False

    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    assert realm.required_actions["CONFIGURE_TOTP"]["defaultAction"] is True


def test_a_converged_realm_gets_zero_writes_of_any_kind(tmp_path: Path) -> None:
    """Idempotence across every owned surface, not only the realm keys."""
    realm = _realm_with_account_clients(console_enabled=True, account_enabled=False)
    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    realm.calls.clear()
    result = init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE)

    writes = [
        c
        for c in realm.calls
        if c.startswith(
            (
                "update_realm",
                "update_client",
                "register_required_action",
                "update_required_action",
                "create_client",
                # The MCP surfaces (2026-10-14).
                "create_reserved_client",
                "create_client_scope",
                "update_client_scope",
                "create_scope_mapper",
                "update_scope_mapper",
                "add_default_optional_scope",
                "create_component",
                "update_component",
                "delete_component",
                "put_client_profiles",
                "put_client_policies",
            )
        )
    ]
    assert writes == []
    assert result.clients_updated == ()
    assert result.required_actions_updated == ()


# --- the MCP scope, open registration and client policies (2026-10-14) ----
#
# `docs/superpowers/specs/2026-10-09-mcp-server-design.md` §3.2-§3.3. Any
# MCP-capable AI app registers itself anonymously and runs code+PKCE for a
# token whose audience is `<ITS_PUBLIC_CONSOLE_URL>/mcp` -- never the
# console's `it-suite`. Opening anonymous registration is the most permissive
# thing this job has ever done to the realm, so everything that narrows it
# is converged on every deploy, and in place BEFORE the stock Trusted Hosts
# policy (which refuses every anonymous registration) is removed.

CONSOLE_URL = "https://itms.example.com"


def _mcp_run(
    tmp_path: Path,
    realm: Any = None,
    public_url: str = CONSOLE_URL,
    registration: str = "open",
) -> Any:
    """A run with MCP fully configured unless told otherwise: the console URL
    set and the operator's opt-in to open registration given."""
    realm = realm or FakeRealm()
    result = init.run_init(
        realm,
        secrets_dir=tmp_path,
        realm_file=REALM_FILE,
        public_url=public_url,
        mcp_registration=registration,
    )
    return realm, result


def _web_org_claim() -> dict[str, Any]:
    return next(m for m in REALM_FILE_WEB["protocolMappers"] if m["name"] == "org-claim")


def test_a_fresh_realm_gets_the_mcp_scope_with_both_mappers(tmp_path: Path) -> None:
    realm, result = _mcp_run(tmp_path)

    scope = realm.client_scopes["mcp"]
    assert scope["protocol"] == "openid-connect"
    assert scope["attributes"]["include.in.token.scope"] == "true"
    assert scope["attributes"]["display.on.consent.screen"] == "true"
    assert scope["attributes"]["consent.screen.text"] == "${itmsMcpConsent}"

    mappers = realm.scope_mappers("mcp")
    assert set(mappers) == {"mcp-audience", "org-claim", "mcp-sub"}
    audience = mappers["mcp-audience"]
    assert audience["protocolMapper"] == "oidc-audience-mapper"
    assert audience["config"]["included.custom.audience"] == "https://itms.example.com/mcp"
    assert audience["config"]["access.token.claim"] == "true"
    assert audience["config"]["id.token.claim"] == "false"
    assert "client-scope:mcp" in result.mcp_converged


def test_the_mcp_org_mapper_is_the_web_clients_org_mapper(tmp_path: Path) -> None:
    """The same claim, read the same way, or `/mcp` and `/api` disagree about
    which organization a person is in."""
    realm, _ = _mcp_run(tmp_path)

    org = realm.scope_mappers("mcp")["org-claim"]
    web = _web_org_claim()
    assert org["protocolMapper"] == web["protocolMapper"]
    assert org["config"] == web["config"]


def test_the_mcp_audience_is_never_the_consoles(tmp_path: Path) -> None:
    """§3.2: an MCP token replayed against `/api` must fail its audience check."""
    realm, _ = _mcp_run(tmp_path)

    for mapper in realm.scope_mappers("mcp").values():
        assert mapper["config"].get("included.custom.audience") != "it-suite"
        assert mapper["config"].get("included.client.audience") is None


def test_a_trailing_slash_on_the_console_url_is_not_doubled(tmp_path: Path) -> None:
    realm, _ = _mcp_run(tmp_path, public_url="https://itms.example.com/ ")

    audience = realm.scope_mappers("mcp")["mcp-audience"]
    assert audience["config"]["included.custom.audience"] == "https://itms.example.com/mcp"


def test_mcp_is_offered_as_an_optional_scope(tmp_path: Path) -> None:
    """Optional, so a registered app can REQUEST it; not default, so the
    console's own client never carries the MCP audience."""
    realm, _ = _mcp_run(tmp_path)

    assert "scope-mcp" in realm.default_optional_scope_ids
    assert "scope-offline_access" in realm.default_optional_scope_ids


def test_trusted_hosts_is_removed_and_the_narrowing_policies_are_in_place(
    tmp_path: Path,
) -> None:
    realm, _ = _mcp_run(tmp_path)

    anonymous = realm.anonymous_policies()
    assert "trusted-hosts" not in anonymous
    assert "consent-required" in anonymous
    assert "scope" in anonymous, "Full Scope Disabled is the `scope` provider"
    assert anonymous["max-clients"]["config"] == {"max-clients": ["200"]}
    templates = anonymous["allowed-client-templates"]["config"]
    assert "mcp" in templates["allowed-client-scopes"]
    assert templates["allow-default-scopes"] == ["true"]


def test_a_registering_app_cannot_bring_its_own_protocol_mappers(tmp_path: Path) -> None:
    """§3.3: "Apps cannot add mappers, for example one that copies another
    attribute into a claim." Keycloak's stock list allows exactly that mapper
    (`oidc-usermodel-attribute-mapper`), which could write a claim named `org`
    from an attribute the person can edit themselves. An empty list allows no
    type at all (`MultivaluedMap.getList` reads an absent key as empty)."""
    realm, _ = _mcp_run(tmp_path)

    config = realm.anonymous_policies()["allowed-protocol-mappers"]["config"]
    assert config.get("allowed-protocol-mapper-types", []) == []


def test_authenticated_registration_policies_are_left_alone(tmp_path: Path) -> None:
    realm, _ = _mcp_run(tmp_path)

    authenticated = {
        c["id"]: c for c in realm.components.values() if c["subType"] == "authenticated"
    }
    stock = {
        c["id"]: c for c in _stock_registration_policies() if c["subType"] == "authenticated"
    }
    assert authenticated == stock


def test_registered_apps_get_the_client_profile_and_policy(tmp_path: Path) -> None:
    realm, _ = _mcp_run(tmp_path)

    profile = next(p for p in realm.client_profiles["profiles"] if p["name"] == "itms-mcp-apps")
    executors = {e["executor"]: e["configuration"] for e in profile["executors"]}
    assert set(executors) == {
        "pkce-enforcer",
        "reject-implicit-grant",
        "reject-ropc-grant",
        "secure-redirect-uris-enforcer",
    }
    assert executors["pkce-enforcer"]["auto-configure"] == "true"

    policy = next(p for p in realm.client_policies["policies"] if p["name"] == "itms-mcp-apps")
    assert policy["enabled"] is True
    assert policy["profiles"] == ["itms-mcp-apps"]
    (condition,) = policy["conditions"]
    assert condition["condition"] == "client-updater-context"
    assert "ByAnonymous" in condition["configuration"]["update-client-source"]


def test_the_policy_also_covers_an_apps_update_of_itself(tmp_path: Path) -> None:
    """An anonymously registered app holds a registration access token and can
    rewrite itself with it (RFC 7592). Keycloak labels that request
    `ByRegistrationAccessToken`, not `ByAnonymous`
    (`ClientUpdaterContextCondition.isAuthMethodMatched`), and the anonymous
    registration policies do not check grant types -- so a condition naming
    only `ByAnonymous` would let an app turn on the password grant for itself
    one request after registering."""
    realm, _ = _mcp_run(tmp_path)

    policy = next(p for p in realm.client_policies["policies"] if p["name"] == "itms-mcp-apps")
    sources = policy["conditions"][0]["configuration"]["update-client-source"]
    assert sorted(sources) == ["ByAnonymous", "ByRegistrationAccessToken"]


def test_the_operators_own_profiles_and_policies_are_kept(tmp_path: Path) -> None:
    realm = FakeRealm()
    realm.client_profiles = {"profiles": [{"name": "theirs", "executors": []}]}
    realm.client_policies = {
        "policies": [{"name": "their-policy", "enabled": False, "profiles": ["theirs"]}]
    }

    _mcp_run(tmp_path, realm)

    assert [p["name"] for p in realm.client_profiles["profiles"]] == ["theirs", "itms-mcp-apps"]
    assert [p["name"] for p in realm.client_policies["policies"]] == [
        "their-policy",
        "itms-mcp-apps",
    ]


def test_a_second_run_writes_nothing_to_the_mcp_surfaces(tmp_path: Path) -> None:
    realm, _ = _mcp_run(tmp_path)

    realm.calls.clear()
    _, result = _mcp_run(tmp_path, realm)

    writes = [
        c
        for c in realm.calls
        if c.startswith(
            (
                "create_reserved_client",
                "create_client_scope",
                "update_client_scope",
                "create_scope_mapper",
                "update_scope_mapper",
                "add_default_optional_scope",
                "create_component",
                "update_component",
                "delete_component",
                "put_client_profiles",
                "put_client_policies",
            )
        )
    ]
    assert writes == []
    assert result.mcp_converged == ()


def test_without_a_console_url_the_scope_has_no_audience_and_the_job_says_so(
    tmp_path: Path,
) -> None:
    """Not a failure: the rest converges, `/mcp` answers 503 saying the same
    thing (spec §2), and the job's log names the missing setting."""
    realm, result = _mcp_run(tmp_path, public_url="")

    assert set(realm.scope_mappers("mcp")) == {"org-claim", "mcp-sub"}
    assert "trusted-hosts" in realm.anonymous_policies(), "registration must stay closed"

    lines = init.mcp_summary(result.mcp_converged, "", "open")
    assert any("ITS_PUBLIC_CONSOLE_URL" in line for line in lines)
    assert any(line.startswith("identity init: mcp converged=") for line in lines)


def test_the_summary_says_nothing_when_nothing_changed() -> None:
    assert init.mcp_summary((), CONSOLE_URL, "open") == ["identity init: mcp converged=nothing"]
    assert init.mcp_summary(("client-scope:mcp",), CONSOLE_URL, "open") == [
        "identity init: mcp converged=['client-scope:mcp']"
    ]


def test_a_wrong_audience_is_corrected_in_place(tmp_path: Path) -> None:
    """The console moved hosts. The mapper keeps its id; only its audience is
    rewritten."""
    realm, _ = _mcp_run(tmp_path, public_url="https://old.example.com")
    old_id = realm.scope_mappers("mcp")["mcp-audience"]["id"]

    realm.calls.clear()
    _, result = _mcp_run(tmp_path, realm)

    audience = realm.scope_mappers("mcp")["mcp-audience"]
    assert audience["id"] == old_id
    assert audience["config"]["included.custom.audience"] == "https://itms.example.com/mcp"
    assert "update_scope_mapper:mcp-audience" in realm.calls
    assert not any(c.startswith("create_scope_mapper") for c in realm.calls)
    assert "mapper:mcp-audience" in result.mcp_converged


def test_a_missing_org_mapper_is_put_back(tmp_path: Path) -> None:
    realm, _ = _mcp_run(tmp_path)
    realm.client_scopes["mcp"]["mappers"] = [
        m for m in realm.client_scopes["mcp"]["mappers"] if m["name"] != "org-claim"
    ]

    _mcp_run(tmp_path, realm)

    assert set(realm.scope_mappers("mcp")) == {"mcp-audience", "org-claim", "mcp-sub"}


def test_a_widened_allowed_scopes_policy_is_narrowed_back(tmp_path: Path) -> None:
    realm, _ = _mcp_run(tmp_path)
    templates = realm.anonymous_policies()["allowed-client-templates"]
    templates["config"]["allowed-client-scopes"] = ["mcp", "roles", "it-suite-audience"]

    realm.calls.clear()
    _mcp_run(tmp_path, realm)

    config = realm.anonymous_policies()["allowed-client-templates"]["config"]
    assert "it-suite-audience" not in config["allowed-client-scopes"]
    assert "update_component:allowed-client-templates" in realm.calls


def test_mcp_convergence_happens_after_the_clients_and_before_the_rotation(
    tmp_path: Path,
) -> None:
    """The same two-sided ordering as the realm convergence, for the same two
    reasons: the newest code must not be able to cost a fresh install its
    provisioning client, and it authenticates with the password the rotation
    destroys."""
    realm, _ = _mcp_run(tmp_path, FakeRealm())

    secrets_written = realm.calls.index("client_secret:uuid-2")
    realm_converged = realm.calls.index("realm_settings")
    mcp_started = realm.calls.index("list_client_scopes")
    rotated = realm.calls.index("set_bootstrap_password")

    assert secrets_written < realm_converged < mcp_started < rotated


def test_trusted_hosts_goes_last(tmp_path: Path) -> None:
    """Deleting Trusted Hosts is what OPENS registration. Every policy that
    narrows it, and the client policy, must already be in force, so that a run
    that dies part-way leaves registration closed rather than open and
    unrestricted."""
    realm, _ = _mcp_run(tmp_path)

    deleted = realm.calls.index("delete_component:trusted-hosts")
    narrowing = [
        i
        for i, c in enumerate(realm.calls)
        if c.startswith(("create_component", "update_component", "put_client_"))
    ]
    assert narrowing and max(narrowing) < deleted


@pytest.mark.parametrize(
    "failure",
    [
        "put_client_policies",
        "update_component:allowed-protocol-mappers",
        "delete_component:trusted-hosts",
    ],
)
def test_a_failed_mcp_run_converges_on_the_next(tmp_path: Path, failure: str) -> None:
    realm = FakeRealm()
    realm.fail_once_on = failure
    with pytest.raises(RuntimeError):
        _mcp_run(tmp_path, realm)

    _mcp_run(tmp_path, realm)

    assert "trusted-hosts" not in realm.anonymous_policies()
    assert any(p["name"] == "itms-mcp-apps" for p in realm.client_policies["policies"])


def test_registration_policy_changes_on_a_stock_realm() -> None:
    """The decision, pure: from Keycloak's stock set, two corrections and one
    removal; the three policies the stock set already gets right are not
    touched."""
    to_create, to_update, to_delete = init.registration_policy_changes(
        _stock_registration_policies(),
        init.desired_registration_policies(),
        open_registration=True,
    )

    assert to_create == []
    assert sorted(c["providerId"] for c in to_update) == [
        "allowed-client-templates",
        "allowed-protocol-mappers",
    ]
    assert all(c["subType"] == "anonymous" for c in to_update)
    assert to_delete == ["stock-trusted"]


def test_compose_gives_the_init_job_the_console_url() -> None:
    assert "ITS_PUBLIC_CONSOLE_URL: ${ITS_PUBLIC_CONSOLE_URL:-}" in _keycloak_init_service()


# --- the realm file and the convergence agree -------------------------------
#
# A fresh install gets the realm FILE; every other install gets what this job
# converges. The two must describe the same realm, or the first boot of a new
# customer and the hundredth deploy of an old one disagree about who may
# register an app.


def _file_registration_policies(sub_type: str) -> list[dict[str, Any]]:
    return [
        c
        for c in REALM_FILE["components"].get(_REGISTRATION_POLICY, [])
        if c.get("subType") == sub_type
    ]


def test_the_realm_file_registers_the_same_anonymous_policies() -> None:
    declared = _file_registration_policies("anonymous")
    to_create, to_update, to_delete = init.registration_policy_changes(
        [{**c, "id": f"file-{i}"} for i, c in enumerate(declared)],
        init.desired_registration_policies(),
        open_registration=False,
    )

    assert (to_create, to_update, to_delete) == ([], [], [])
    # Every narrowing policy, PLUS Trusted Hosts: a fresh install is closed
    # until keycloak-init runs with ITS_PUBLIC_CONSOLE_URL set.
    assert len(declared) == len(init.desired_registration_policies()) + 1
    trusted = next(c for c in declared if c["providerId"] == "trusted-hosts")
    assert trusted["config"] == init.STOCK_TRUSTED_HOSTS["config"]


def test_the_realm_file_keeps_keycloaks_authenticated_policies() -> None:
    """Declaring ANY registration policy makes Keycloak skip its whole stock
    set on import (`DefaultClientRegistrationPolicies.addDefaultPolicies` runs
    only when the realm has none), so the authenticated pair must be declared
    too or a fresh install would allow any mapper type to an authenticated
    registrant."""
    declared = {c["providerId"]: c["config"] for c in _file_registration_policies("authenticated")}
    stock = {
        c["providerId"]: c["config"]
        for c in _stock_registration_policies()
        if c["subType"] == "authenticated"
    }

    assert declared == stock


def test_the_realm_file_declares_the_same_client_profile_and_policy() -> None:
    profiles, policies = init.client_policy_changes(
        REALM_FILE["clientProfiles"], REALM_FILE["clientPolicies"]
    )

    assert profiles is None, "the file's client profile differs from the converged one"
    assert policies is None, "the file's client policy differs from the converged one"


def test_the_realm_file_does_not_declare_client_scopes() -> None:
    """The scope is converged, never imported, and that is forced rather than
    chosen. A realm file that declares `clientScopes` makes Keycloak create
    none of its built-in scopes (`RealmManager.importRealm` runs
    `createDefaultClientScopes` only when the key is absent) -- so a file
    listing `mcp` alone would ship a realm without `basic`, whose tokens carry
    no `sub` -- and `defaultOptionalClientScopes` resolves only names declared
    in that same file. The audience is per-deployment anyway: no static file
    can know `ITS_PUBLIC_CONSOLE_URL`."""
    assert "clientScopes" not in REALM_FILE
    assert "defaultOptionalClientScopes" not in REALM_FILE
    assert "defaultDefaultClientScopes" not in REALM_FILE


# --- fix round 1 (2026-10-14): controller decisions on the round-0 concerns ---


def test_every_mcp_token_names_a_person(tmp_path: Path) -> None:
    """`sub` must not depend on `basic`. An app whose registration names its
    own `scope` list has EVERY other scope removed from its client
    (`RepresentationToModel.updateClientScopes`), `basic` with them, and
    `basic` is where Keycloak's stock `sub` mapper lives. So the `mcp` scope
    carries its own: any token issued for `mcp` names its person."""
    realm, _ = _mcp_run(tmp_path)

    sub = realm.scope_mappers("mcp")["mcp-sub"]
    assert sub["protocolMapper"] == "oidc-sub-mapper"
    assert sub["config"]["access.token.claim"] == "true"
    assert sub["config"]["introspection.token.claim"] == "true"


def test_the_sub_mapper_is_converged_without_a_console_url_too(tmp_path: Path) -> None:
    realm, _ = _mcp_run(tmp_path, public_url="")

    assert "mcp-sub" in realm.scope_mappers("mcp")


def _profile(realm: Any, name: str) -> dict[str, Any]:
    return next(p for p in realm.client_profiles["profiles"] if p["name"] == name)


def _policy(realm: Any, name: str) -> dict[str, Any]:
    return next(p for p in realm.client_policies["policies"] if p["name"] == name)


def test_no_client_credentials_refusal_policy_is_converged(tmp_path: Path) -> None:
    """Fix round 3, Critical 1. Round 1 converged `grant-type` +
    `client-scopes` -> `reject-request` on the belief that every condition
    had to vote yes. It does not: `DefaultClientPolicyManager.isSatisfied`
    SKIPS an abstention and needs one yes and no no. At the code exchange
    `grant-type` abstains (it votes only on its own grant events) and
    `client-scopes` votes yes for `scope=mcp`, so the policy refused EVERY MCP
    token. Nothing of it is converged; a person-only check lives in the API."""
    realm, _ = _mcp_run(tmp_path)

    assert [p["name"] for p in realm.client_policies["policies"]] == ["itms-mcp-apps"]
    assert [p["name"] for p in realm.client_profiles["profiles"]] == ["itms-mcp-apps"]
    for policy in init.desired_client_policies():
        conditions = [c["condition"] for c in policy["conditions"]]
        assert conditions == ["client-updater-context"], (
            "every owned policy must be gated on a condition that votes only on "
            "registration events, or an abstaining partner makes it fire at sign-in"
        )


def test_an_operators_own_profile_and_policy_are_kept(tmp_path: Path) -> None:
    """The admin API's PUT replaces the realm's whole list, so ours is merged
    in beside whatever else is there, and nothing of anyone else's is removed.
    Two PUTs, profiles before policies: Keycloak refuses a policy naming a
    profile it does not have yet."""
    realm = FakeRealm()
    realm.client_profiles = {"profiles": [{"name": "theirs", "executors": []}]}
    realm.client_policies = {
        "policies": [{"name": "theirs", "enabled": True, "profiles": ["theirs"]}]
    }

    _, result = _mcp_run(tmp_path, realm)

    assert [p["name"] for p in realm.client_profiles["profiles"]] == ["theirs", "itms-mcp-apps"]
    assert [p["name"] for p in realm.client_policies["policies"]] == ["theirs", "itms-mcp-apps"]
    puts = [c for c in realm.calls if c.startswith("put_client_")]
    assert puts == ["put_client_profiles", "put_client_policies"]
    assert result.mcp_converged


def test_nothing_is_converged_away_that_main_never_shipped() -> None:
    """Round 1's refusal policy and profile existed only on the MCP branch
    while it was being built; no deployment ever ran them. So there is no
    third, pruning PUT and no list of names to remove."""
    assert not hasattr(init, "REMOVED_CLIENT_POLICIES")
    assert not hasattr(init, "REMOVED_CLIENT_PROFILES")


def test_native_mcp_clients_can_register_loopback_http_callbacks() -> None:
    """Pins the redirect-URI policy (controller decision, 2026-10-14).

    Desktop and CLI MCP clients register `http://localhost:<port>` or
    `http://127.0.0.1:<port>` (RFC 8252 §7.3); refusing them breaks "connect
    this platform to any AI". `SecureRedirectUrisEnforcerExecutor` cannot
    allow http for loopback only: `allow-http-scheme` gates both
    `isValidLoopbackAddress` and `isValidNormalUri`. So http is allowed for
    any host, and what an http redirect exposes is a PKCE-bound code that
    cannot be redeemed without the registering app's verifier."""
    policy = init.MCP_REDIRECT_URI_POLICY

    assert policy == {
        "allow-ipv4-loopback-address": "true",
        "allow-ipv6-loopback-address": "true",
        "allow-http-scheme": "true",
        "allow-private-use-uri-scheme": "true",
    }
    # Still refused: wildcards (isIncludeWildcard) and anything-goes.
    for never in ("allow-wildcard-context-path", "allow-open-redirect"):
        assert never not in policy, never
    # OFF ON PURPOSE: OAuth 2.1 mode would also refuse fragments, but it
    # refuses `localhost` by name, any port in a registered loopback URI, and
    # every non-https ordinary host -- the very callbacks this allows.
    assert "oauth-2-1-compliant" not in policy


def test_the_redirect_policy_relies_on_pkce_being_enforced() -> None:
    """http is safe to allow only because the code is PKCE-bound. If the
    S256 enforcer ever left the profile, this configuration would become a
    code-interception hole; the two travel together."""
    profile = next(p for p in init.desired_client_profiles() if p["name"] == "itms-mcp-apps")
    executors = {e["executor"]: e["configuration"] for e in profile["executors"]}

    assert executors["pkce-enforcer"] == {"auto-configure": "true"}
    assert executors["secure-redirect-uris-enforcer"]["allow-http-scheme"] == "true"


def test_an_mcp_scope_with_the_wrong_attributes_is_corrected(tmp_path: Path) -> None:
    realm = FakeRealm()
    realm.client_scopes["mcp"] = {
        "id": "scope-mcp",
        "name": "mcp",
        "protocol": "saml",
        "attributes": {
            "include.in.token.scope": "false",
            "display.on.consent.screen": "false",
            "gui.order": "7",
        },
        "mappers": [],
    }

    _, result = _mcp_run(tmp_path, realm)

    scope = realm.client_scopes["mcp"]
    assert scope["id"] == "scope-mcp", "corrected in place, not recreated"
    assert scope["protocol"] == "openid-connect"
    assert scope["attributes"]["include.in.token.scope"] == "true"
    assert scope["attributes"]["display.on.consent.screen"] == "true"
    assert scope["attributes"]["consent.screen.text"] == "${itmsMcpConsent}"
    assert scope["attributes"]["gui.order"] == "7", "attributes that are not ours are kept"
    assert "update_client_scope:mcp" in realm.calls
    assert "client-scope:mcp" in result.mcp_converged


def test_scope_changes_is_none_for_a_correct_scope() -> None:
    desired = init.desired_mcp_scope(CONSOLE_URL)
    existing = {
        "id": "x",
        "name": "mcp",
        "protocol": "openid-connect",
        "attributes": {**desired["attributes"], "gui.order": ""},
    }

    assert init.scope_changes(existing, desired) is None


#: Every client the platform itself puts in the realm: Keycloak's per-realm
#: built-ins (`RealmManager.setupRealmDefaults` and friends) plus the file's
#: and this job's.
_KEYCLOAK_BUILTIN_CLIENTS = {
    "account",
    "account-console",
    "admin-cli",
    "broker",
    "realm-management",
    "security-admin-console",
}


def test_max_clients_leaves_room_for_apps_after_the_platforms_own() -> None:
    """Keycloak's Max Clients counts EVERY client in the realm
    (`MaxClientsClientRegistrationPolicy.beforeRegister`,
    `realm.getClientsCount()`), so the platform's own clients come out of the
    200. They must leave the great majority for registered apps."""
    platform = (
        _KEYCLOAK_BUILTIN_CLIENTS
        | {c["clientId"] for c in REALM_FILE["clients"]}
        | {c.client_id for c in init.DESIRED_CLIENTS}
    )
    max_clients = int(
        next(
            p for p in init.desired_registration_policies() if p["providerId"] == "max-clients"
        )["config"]["max-clients"][0]
    )

    assert max_clients == init.MAX_CLIENTS == 200
    assert max_clients - len(platform) >= 150, sorted(platform)


# --- fix round 3 (2026-10-15): review Criticals and Importants ----------------


def test_the_console_audience_client_id_is_reserved(tmp_path: Path) -> None:
    """Critical 2. Keycloak puts the requesting clientId in an ID token's
    `aud`, and a registration provider that lets the registrant choose the
    clientId (`default`, DefaultClientRegistrationProvider:50) would let an
    app call itself `it-suite`. A disabled client holding that clientId makes
    the name taken. Same for the MCP resource string."""
    realm, result = _mcp_run(tmp_path)

    for client_id in ("it-suite", "https://itms.example.com/mcp"):
        rep = realm.reserved_clients[client_id]["representation"]
        assert rep["enabled"] is False, client_id
        assert rep["bearerOnly"] is True
        for flow in (
            "standardFlowEnabled",
            "implicitFlowEnabled",
            "directAccessGrantsEnabled",
            "serviceAccountsEnabled",
        ):
            assert rep[flow] is False, (client_id, flow)
        assert rep["redirectUris"] == [] and rep["webOrigins"] == []
        assert "reserv" in rep["description"].lower()
    assert "reserved:it-suite" in result.mcp_converged


def test_without_a_console_url_only_the_console_audience_is_reserved(tmp_path: Path) -> None:
    realm, _ = _mcp_run(tmp_path, public_url="")

    assert set(realm.reserved_clients) == {"it-suite"}


def test_the_console_audience_comes_from_its_setting(tmp_path: Path) -> None:
    realm = FakeRealm()
    init.run_init(
        realm,
        secrets_dir=tmp_path,
        realm_file=REALM_FILE,
        public_url=CONSOLE_URL,
        console_audience="acme-console",
    )

    assert "acme-console" in realm.reserved_clients


def test_a_squatted_console_audience_is_disabled_in_place(tmp_path: Path) -> None:
    """An app that already registered as `it-suite` before this ran: its
    client is switched off and stripped of every flow, not left working."""
    realm = FakeRealm()
    realm.seed_client(
        "it-suite",
        {
            "enabled": True,
            "publicClient": True,
            "standardFlowEnabled": True,
            "redirectUris": ["https://evil.example/cb"],
        },
    )

    _mcp_run(tmp_path, realm)

    rep = realm.clients["it-suite"]["representation"]
    assert rep["enabled"] is False
    assert rep["standardFlowEnabled"] is False
    assert rep["redirectUris"] == []
    assert not any(c.startswith("create_reserved_client:it-suite") for c in realm.calls)


def test_the_realm_file_reserves_the_console_audience() -> None:
    declared = next(c for c in REALM_FILE["clients"] if c["clientId"] == "it-suite")
    desired = init.reserved_client("it-suite", "the console's token audience")

    assert {k: declared.get(k) for k in init.RESERVATION_KEYS} == {
        k: desired[k] for k in init.RESERVATION_KEYS
    }


def test_compose_gives_the_init_job_the_console_audience() -> None:
    assert "ITS_OIDC_AUDIENCE: ${ITS_OIDC_AUDIENCE:-it-suite}" in _keycloak_init_service()


def test_registration_stays_closed_until_the_console_url_is_set(tmp_path: Path) -> None:
    """Important 3. Trusted Hosts (stock: no host trusted) is what keeps
    anonymous registration shut. It goes only when MCP can actually work."""
    realm, result = _mcp_run(tmp_path, public_url="")

    assert "trusted-hosts" in realm.anonymous_policies()
    assert not any(c.startswith("delete_component") for c in realm.calls)
    lines = init.mcp_summary(result.mcp_converged, "", "open")
    assert any("registration stays closed" in line for line in lines)


def test_a_missing_trusted_hosts_is_restored_while_the_url_is_unset(tmp_path: Path) -> None:
    """Closing comes FIRST: restoring Trusted Hosts precedes every other
    registration-policy write."""
    realm = FakeRealm()
    del realm.components["stock-trusted"]

    _mcp_run(tmp_path, realm, public_url="")

    restored = realm.anonymous_policies()["trusted-hosts"]
    assert restored["config"] == {
        k: v for k, v in init.STOCK_TRUSTED_HOSTS["config"].items() if v
    }
    writes = [c for c in realm.calls if c.startswith(("create_component", "update_component"))]
    assert writes[0] == "create_component:trusted-hosts"


# --- final review (2026-10-15) -------------------------------------------------


THEME_MESSAGES = REPO_ROOT / "deploy/keycloak/themes/itms/login/messages"


def _bundle(language: str) -> dict[str, str]:
    lines = (THEME_MESSAGES / f"messages_{language}.properties").read_text(encoding="utf-8")
    entries: dict[str, str] = {}
    for line in lines.splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            entries[key] = value
    return entries


def test_the_consent_screen_says_what_approving_hands_over() -> None:
    """I-3. The consent screen is the only check between a sign-in link and
    the person's whole authority, so the `mcp` line says so, in the person's
    language. Keycloak's stock `login-oauth-grant.ftl` renders a scope's
    `consent.screen.text` through `advancedMsg`, which resolves `${key}`
    from the login theme's bundles (`AdvancedMessageFormatterMethod`); the
    `itms` theme does not override that template."""
    assert init.MCP_SCOPE_ATTRIBUTES["consent.screen.text"] == "${itmsMcpConsent}"
    assert _bundle("en")["itmsMcpConsent"] == (
        "Act as you in ITMS: everything your role allows, including running actions "
        "on managed computers"
    )
    assert _bundle("es")["itmsMcpConsent"] == (
        "Actuar como tú en ITMS: todo lo que permite tu rol, incluido ejecutar acciones "
        "en equipos gestionados"
    )
    assert not (THEME_MESSAGES.parent / "login-oauth-grant.ftl").exists()


def test_the_consent_text_is_converged_once(tmp_path: Path) -> None:
    realm, _ = _mcp_run(tmp_path)
    _, second = _mcp_run(tmp_path, realm)

    assert realm.client_scopes["mcp"]["attributes"]["consent.screen.text"] == "${itmsMcpConsent}"
    assert second.mcp_converged == ()


#: Keycloak's per-realm built-in clients (`RealmManager.setupRealmDefaults`).
_BUILTINS = (
    "account",
    "account-console",
    "admin-cli",
    "broker",
    "realm-management",
    "security-admin-console",
)


@pytest.mark.parametrize(
    "client_id",
    ["it-suite-web", "it-suite-provisioning", "it-suite-events", *_BUILTINS],
)
def test_a_reservation_never_targets_a_platform_or_builtin_client(
    tmp_path: Path, client_id: str
) -> None:
    """I-1. A reservation DISABLES the client it names and strips its flows.
    `ITS_OIDC_AUDIENCE=it-suite-web` would switch the console's own sign-in
    off; `realm-management` would take the admin API down with it. Refused
    before the job writes anything at all."""
    realm = FakeRealm()

    with pytest.raises(init.ReservationRefused, match=re.escape(client_id)):
        init.run_init(
            realm,
            secrets_dir=tmp_path,
            realm_file=REALM_FILE,
            public_url=CONSOLE_URL,
            console_audience=client_id,
        )

    assert realm.calls == []


def test_the_refused_set_is_every_client_the_platform_or_keycloak_owns() -> None:
    assert init.PROTECTED_CLIENT_IDS == frozenset(
        {
            init.WEB_CLIENT_ID,
            *(c.client_id for c in init.DESIRED_CLIENTS),
            *(c.client_id for c in init.CONVERGED_CLIENTS),
            *_BUILTINS,
        }
    )
    assert set(_BUILTINS) == _KEYCLOAK_BUILTIN_CLIENTS


def test_the_default_reservations_are_allowed() -> None:
    reserved = init.desired_reservations(CONSOLE_URL, init.DEFAULT_CONSOLE_AUDIENCE)

    assert [r["clientId"] for r in reserved] == ["it-suite", "https://itms.example.com/mcp"]


# --- operator decision (2026-10-15): registration opens only on an opt-in -----


ENV_EXAMPLE = REPO_ROOT / "deploy/.env.example"


def _closed_line(lines: list[str]) -> str:
    (line,) = [x for x in lines if "registration stays closed" in x]
    return line


def test_registration_stays_closed_without_the_opt_in(tmp_path: Path) -> None:
    """The console URL alone is not consent to let strangers register apps:
    everything else converges -- scope, audience, reservations, policies -- so
    turning it on later is the flag and a redeploy, and Trusted Hosts stays."""
    realm, result = _mcp_run(tmp_path, registration="")

    assert "trusted-hosts" in realm.anonymous_policies()
    assert not any(c.startswith("delete_component") for c in realm.calls)
    assert "mapper:mcp-audience" in result.mcp_converged
    assert "reserved:https://itms.example.com/mcp" in result.mcp_converged
    assert "client-policies:itms-mcp-apps" in result.mcp_converged
    line = _closed_line(init.mcp_summary(result.mcp_converged, CONSOLE_URL, ""))
    assert "ITS_MCP_REGISTRATION=open" in line
    assert "ITS_PUBLIC_CONSOLE_URL" not in line


def test_registration_stays_closed_without_the_console_url(tmp_path: Path) -> None:
    realm, result = _mcp_run(tmp_path, public_url="", registration="open")

    assert "trusted-hosts" in realm.anonymous_policies()
    line = _closed_line(init.mcp_summary(result.mcp_converged, "", "open"))
    assert "ITS_PUBLIC_CONSOLE_URL" in line
    assert "ITS_MCP_REGISTRATION" not in line


def test_the_closed_line_names_both_settings_when_both_are_missing() -> None:
    line = _closed_line(init.mcp_summary((), "", ""))

    assert "ITS_PUBLIC_CONSOLE_URL" in line and "ITS_MCP_REGISTRATION=open" in line


def test_registration_opens_with_both_and_last(tmp_path: Path) -> None:
    realm, result = _mcp_run(tmp_path, registration="open")

    assert "trusted-hosts" not in realm.anonymous_policies()
    assert result.mcp_converged[-1] == "registration:removed:trusted-hosts"
    assert not any(
        "registration stays closed" in x
        for x in init.mcp_summary(result.mcp_converged, CONSOLE_URL, "open")
    )


@pytest.mark.parametrize("value", ["", "yes", "true", "1", "OPEN", "opened", "closed"])
def test_only_the_word_open_opens_registration(value: str) -> None:
    assert init.registration_opted_in(value) is False


def test_the_opt_in_tolerates_surrounding_whitespace() -> None:
    assert init.registration_opted_in(" open\n") is True


def test_withdrawing_the_opt_in_closes_registration_again(tmp_path: Path) -> None:
    realm, _ = _mcp_run(tmp_path, registration="open")
    assert "trusted-hosts" not in realm.anonymous_policies()

    _mcp_run(tmp_path, realm, registration="")

    assert "trusted-hosts" in realm.anonymous_policies()


def test_run_init_defaults_to_closed(tmp_path: Path) -> None:
    """The default is the safe side: a caller that forgets the flag keeps
    registration shut."""
    realm = FakeRealm()
    init.run_init(realm, secrets_dir=tmp_path, realm_file=REALM_FILE, public_url=CONSOLE_URL)

    assert "trusted-hosts" in realm.anonymous_policies()


def test_compose_gives_the_init_job_the_opt_in_default_closed() -> None:
    assert "ITS_MCP_REGISTRATION: ${ITS_MCP_REGISTRATION:-}" in _keycloak_init_service()


def test_the_env_example_documents_the_opt_in_and_leaves_it_off() -> None:
    lines = ENV_EXAMPLE.read_text().splitlines()
    assert "# ITS_MCP_REGISTRATION=open" in lines
    assert not [x for x in lines if x.startswith("ITS_MCP_REGISTRATION=")], (
        "the example must not opt anybody in"
    )
    text = ENV_EXAMPLE.read_text()
    assert "AI connections (MCP)" in text
