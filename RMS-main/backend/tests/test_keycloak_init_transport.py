"""The HTTP half of the init job, against a server that answers like Keycloak.

WHY THIS FILE EXISTS, and it is not a theoretical gap. `test_keycloak_init_job.
py` tests every DECISION the job makes against an in-memory realm, and its
header states plainly that the only untested code is `HttpRealmAdmin` -- the
verbs that carry those decisions to a live realm -- because there is no realm
in CI and there will not be one.

That argument is sound about SEMANTICS and was silently wrong about EXISTENCE.
On 2026-08-20 the realm-convergence work added seven methods to the protocol
and to the fake, and none to `HttpRealmAdmin`. Thirty-one tests passed. The job
died on the operator's deployment with `AttributeError` at the first line of
convergence -- after writing the secrets, so it looked like it had worked --
and the bug it was deployed to fix was still there. A method-set check now
catches that exact shape; this file catches the rest of the class it belongs
to: a wrong path, a wrong verb, a body Keycloak's API would not recognise.

WHAT IT CANNOT DO, stated as plainly as the other file states its own limit:
the server below answers the way this project BELIEVES Keycloak answers. It
proves the requests this code sends and the way it threads responses back
through itself. It proves nothing about whether a real Keycloak 26 agrees --
that is still verified on the operator's deployment, and the job's own log
lines are what report it.
"""

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from tests.test_keycloak_init_job import REALM_FILE, _load_script, _stock_registration_policies

init = _load_script()

REALM = "it-suite"


class FakeKeycloak:
    """Realm state, and the admin-API surface this job touches."""

    def __init__(self) -> None:
        #: uuid -> representation. `realm-management` and the browser client
        #: exist already, as they do on any imported realm.
        self.clients: dict[str, dict[str, Any]] = {
            "uuid-management": {"id": "uuid-management", "clientId": "realm-management"},
            "uuid-web": {
                "id": "uuid-web",
                "clientId": "it-suite-web",
                "rootUrl": "https://localhost",
                "redirectUris": ["https://*/auth/callback"],
                # PKCE is here to be preserved, not converged: a full-
                # representation PUT that dropped it would be a security
                # downgrade shipped as a redirect-URI fix.
                "attributes": {
                    "pkce.code.challenge.method": "S256",
                    "post.logout.redirect.uris": "+",
                },
            },
        }
        self.management_roles = [
            {"id": "r1", "name": "manage-users"},
            {"id": "r2", "name": "manage-realm"},
            {"id": "r3", "name": "view-events"},
        ]
        #: user id -> list of granted role representations
        self.granted: dict[str, list[dict[str, Any]]] = {}
        #: The operator's live realm, as it actually was.
        self.realm: dict[str, Any] = {
            "realm": REALM,
            "smtpServer": {"host": "smtp.internal", "from": "itms@toeshee.com"},
        }
        self.required_actions: dict[str, dict[str, Any]] = {
            "CONFIGURE_TOTP": {
                "alias": "CONFIGURE_TOTP",
                "name": "Configure OTP",
                "providerId": "CONFIGURE_TOTP",
                "enabled": True,
                "defaultAction": True,
                "priority": 10,
            }
        }
        #: alias -> flat execution list. Keycloak's stock browser flow, which
        #: is what the job copies to build the passkey one.
        self.flows: dict[str, list[dict[str, Any]]] = {
            "browser": [
                {"id": "e1", "providerId": "auth-cookie", "requirement": "ALTERNATIVE", "level": 0},
                {"id": "e4", "displayName": "forms", "requirement": "ALTERNATIVE", "level": 0},
                {
                    "id": "e5",
                    "providerId": "auth-username-password-form",
                    "requirement": "REQUIRED",
                    "level": 1,
                },
            ]
        }
        self.bootstrap_password: str | None = None
        self.requests: list[tuple[str, str]] = []
        self._serial = 0
        # -- the MCP surfaces (2026-10-15), as a realm that predates them has
        #    them. Keycloak's built-in client scopes and default-optional list
        #    (`DefaultClientScopes`), its stock registration policies
        #    (`DefaultClientRegistrationPolicies.addDefaultPolicies`, Trusted
        #    Hosts included), and no client profiles or policies of its own.
        self.realm["id"] = "realm-id"
        #: scope id -> representation, `protocolMappers` included as the admin
        #: API returns it.
        self.client_scopes: dict[str, dict[str, Any]] = {
            f"scope-{name}": {
                "id": f"scope-{name}",
                "name": name,
                "protocol": "openid-connect",
                "attributes": {},
                "protocolMappers": [],
            }
            for name in ("basic", "email", "offline_access", "phone", "profile", "roles")
        }
        self.default_optional: list[str] = ["scope-offline_access", "scope-phone"]
        #: component id -> representation.
        self.components: dict[str, dict[str, Any]] = {
            c["id"]: json.loads(json.dumps(c)) for c in _stock_registration_policies()
        }
        self.client_profiles: list[dict[str, Any]] = []
        self.client_policies: list[dict[str, Any]] = []
        #: `(method, path, body)` of every write, for the wire-shape tests.
        self.bodies: list[tuple[str, str, Any]] = []


def _handler(state: FakeKeycloak) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # keep the test output clean
            return

        # -- plumbing -------------------------------------------------------

        def _body(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length)) if length else None

        def _send(self, payload: Any, status: int = 200) -> None:
            body = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _route(self, method: str) -> None:
            path, _, query = self.path.partition("?")
            state.requests.append((method, path))
            # EVERY admin call must carry the bearer token. A job that dropped
            # it would get 401s from a real server and this fake would
            # otherwise cheerfully answer, hiding the defect.
            if path.startswith("/admin/") and self.headers.get("Authorization") != "Bearer tok":
                self._send({"error": "unauthorized"}, 401)
                return
            body = self._body()
            if method in ("POST", "PUT", "DELETE"):
                state.bodies.append((method, path, body))
            self._send(*_respond(state, method, path, query, body))

        def do_GET(self) -> None:
            self._route("GET")

        def do_POST(self) -> None:
            self._route("POST")

        def do_PUT(self) -> None:
            self._route("PUT")

        def do_DELETE(self) -> None:
            self._route("DELETE")

    return Handler


def _respond(
    state: FakeKeycloak, method: str, path: str, query: str, body: Any
) -> tuple[Any, int]:
    parts = [p for p in path.strip("/").split("/") if p]

    # /admin/realms/master/users...  (the bootstrap rotation)
    if path == "/admin/realms/master/users":
        return [{"id": "master-admin"}], 200
    if path.endswith("/reset-password"):
        state.bootstrap_password = body["value"]
        return None, 204

    # /admin/realms/{realm}
    if parts == ["admin", "realms", REALM]:
        if method == "GET":
            return dict(state.realm), 200
        state.realm = dict(body)
        return None, 204

    # /admin/realms/{realm}/clients[?clientId=]
    if parts == ["admin", "realms", REALM, "clients"]:
        if method == "GET":
            wanted = urllib.parse.parse_qs(query).get("clientId", [""])[0]
            return [c for c in state.clients.values() if c["clientId"] == wanted], 200
        if any(c["clientId"] == body["clientId"] for c in state.clients.values()):
            return {"errorMessage": "Client already exists"}, 409
        state._serial += 1
        uuid = f"uuid-new-{state._serial}"
        state.clients[uuid] = {"id": uuid, **body}
        return None, 201

    # /admin/realms/{realm}/clients/{uuid}...
    if len(parts) >= 5 and parts[:4] == ["admin", "realms", REALM, "clients"]:
        uuid, rest = parts[4], parts[5:]
        if rest == ["client-secret"]:
            return {"value": f"secret-for-{state.clients[uuid]['clientId']}"}, 200
        if rest == ["service-account-user"]:
            return {"id": f"svc-{uuid}"}, 200
        if rest == ["roles"]:
            return list(state.management_roles), 200
        if not rest:
            if method == "GET":
                return dict(state.clients[uuid]), 200
            state.clients[uuid] = dict(body)
            return None, 204

    # /admin/realms/{realm}/users/{id}/role-mappings/clients/{management}
    if "role-mappings" in parts:
        user_id = parts[4]
        if method == "GET":
            return list(state.granted.get(user_id, [])), 200
        state.granted.setdefault(user_id, []).extend(body)
        return None, 204

    # /admin/realms/{realm}/authentication/flows...
    if parts[3:] == ["authentication", "flows"]:
        return [{"alias": alias} for alias in state.flows], 200
    if parts[3:5] == ["authentication", "flows"] and parts[6:] == ["copy"]:
        state.flows[body["newName"]] = [dict(e) for e in state.flows[parts[5]]]
        return None, 201
    if parts[3:5] == ["authentication", "flows"] and parts[6:] == ["executions"]:
        alias = parts[5]
        if method == "GET":
            return [dict(e) for e in state.flows[alias]], 200
        for execution in state.flows[alias]:
            if execution["id"] == body["id"]:
                execution.update(body)
        return None, 204
    if parts[3:5] == ["authentication", "flows"] and parts[6:] == ["executions", "execution"]:
        # Keycloak creates it DISABLED. The fake must too, or the job could
        # forget the enabling step and still look correct here.
        state.flows[parts[5]].append(
            {
                "id": f"new-{body['provider']}",
                "providerId": body["provider"],
                "requirement": "DISABLED",
                "level": 0,
            }
        )
        return None, 201

    # /admin/realms/{realm}/authentication/...
    if parts[3:] == ["authentication", "required-actions"]:
        return list(state.required_actions.values()), 200
    if parts[3:] == ["authentication", "register-required-action"]:
        alias = body["providerId"]
        state.required_actions[alias] = {
            "alias": alias,
            "name": body["name"],
            "providerId": alias,
            "enabled": False,
            "defaultAction": False,
            "priority": 0,
        }
        return None, 204
    if parts[3:5] == ["authentication", "required-actions"] and len(parts) == 6:
        state.required_actions[parts[5]] = dict(body)
        return None, 204

    mcp = _respond_mcp(state, method, parts, query, body)
    if mcp is not None:
        return mcp

    return {"error": f"unrouted {method} {path}"}, 404


def _respond_mcp(
    state: FakeKeycloak, method: str, parts: list[str], query: str, body: Any
) -> tuple[Any, int] | None:
    """The admin endpoints the MCP convergence uses, answering as Keycloak
    26.2 does (paths: `RealmAdminResource` :224, :337, :383, :1403, :1409)."""
    if parts[:3] != ["admin", "realms", REALM]:
        return None
    rest = parts[3:]

    # client-scopes; client-scopes/{id}; client-scopes/{id}/protocol-mappers/models[/{id}]
    if rest == ["client-scopes"]:
        if method == "GET":
            return [json.loads(json.dumps(s)) for s in state.client_scopes.values()], 200
        if any(s["name"] == body["name"] for s in state.client_scopes.values()):
            return {"errorMessage": "Client Scope already exists"}, 409
        state._serial += 1
        scope_id = f"scope-new-{state._serial}"
        state.client_scopes[scope_id] = {
            **body,
            "id": scope_id,
            "protocolMappers": list(body.get("protocolMappers") or []),
        }
        return None, 201
    if len(rest) >= 2 and rest[0] == "client-scopes" and rest[1] in state.client_scopes:
        scope = state.client_scopes[rest[1]]
        if rest[2:] == [] and method == "PUT":
            # `RepresentationToModel.updateClientScope`: attributes set key by
            # key, never replaced as a map.
            for key in ("name", "description", "protocol"):
                if body.get(key) is not None:
                    scope[key] = body[key]
            scope["attributes"] = {**scope.get("attributes", {}), **(body.get("attributes") or {})}
            return None, 204
        if rest[2:] == ["protocol-mappers", "models"]:
            if method == "GET":
                return json.loads(json.dumps(scope["protocolMappers"])), 200
            state._serial += 1
            scope["protocolMappers"].append({**body, "id": f"mapper-{state._serial}"})
            return None, 201
        if rest[2:4] == ["protocol-mappers", "models"] and len(rest) == 5 and method == "PUT":
            # `ProtocolMappersResource.update` rebuilds the model from the
            # BODY: without its id, Keycloak cannot find what to update.
            if body.get("id") != rest[4]:
                return {"errorMessage": "Model not found"}, 404
            mappers = scope["protocolMappers"]
            for i, mapper in enumerate(mappers):
                if mapper["id"] == rest[4]:
                    mappers[i] = dict(body)
                    return None, 204
            return {"errorMessage": "Model not found"}, 404

    # default-optional-client-scopes[/{id}]
    if rest == ["default-optional-client-scopes"] and method == "GET":
        return [
            {"id": sid, "name": state.client_scopes[sid]["name"]} for sid in state.default_optional
        ], 200
    if len(rest) == 2 and rest[0] == "default-optional-client-scopes" and method == "PUT":
        if rest[1] not in state.client_scopes:
            return {"errorMessage": "Client scope not found"}, 404
        if rest[1] not in state.default_optional:
            state.default_optional.append(rest[1])
        return None, 204

    # components[?type=]; components/{id}
    if rest == ["components"]:
        if method == "GET":
            wanted = urllib.parse.parse_qs(query).get("type", [None])[0]
            return [
                json.loads(json.dumps(c))
                for c in state.components.values()
                if wanted is None or c["providerType"] == wanted
            ], 200
        state._serial += 1
        component_id = f"component-{state._serial}"
        # The parent defaults to the realm (ComponentResource:139), and an
        # empty list stores no row, so it reads back absent.
        state.components[component_id] = {
            **body,
            "id": component_id,
            "parentId": body.get("parentId") or state.realm["id"],
            "config": {k: v for k, v in (body.get("config") or {}).items() if v},
        }
        return None, 201
    if len(rest) == 2 and rest[0] == "components" and rest[1] in state.components:
        if method == "DELETE":
            del state.components[rest[1]]
            return None, 204
        if method == "PUT":
            # `RepresentationToModel.updateComponent`: a key sent EMPTY is
            # removed, a key NOT SENT is kept.
            current = state.components[rest[1]]
            config = dict(current.get("config") or {})
            for key, values in (body.get("config") or {}).items():
                if values:
                    config[key] = list(values)
                else:
                    config.pop(key, None)
            state.components[rest[1]] = {**current, "config": config}
            return None, 204

    # client-policies/profiles; client-policies/policies
    if rest == ["client-policies", "profiles"]:
        if method == "GET":
            return {"profiles": json.loads(json.dumps(state.client_profiles))}, 200
        names = [p.get("name") for p in body.get("profiles") or []]
        if len(names) != len(set(names)) or not all(names):
            return {"error": "invalid client profiles"}, 400
        state.client_profiles = json.loads(json.dumps(body.get("profiles") or []))
        return None, 204
    if rest == ["client-policies", "policies"]:
        if method == "GET":
            return {"policies": json.loads(json.dumps(state.client_policies))}, 200
        known = {p["name"] for p in state.client_profiles}
        for policy in body.get("policies") or []:
            if set(policy.get("profiles") or []) - known:
                return {"error": "proposed client policy refers to an unknown profile"}, 400
        state.client_policies = json.loads(json.dumps(body.get("policies") or []))
        return None, 204

    return None


@pytest.fixture()
def keycloak() -> Any:
    state = FakeKeycloak()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base_url = f"http://127.0.0.1:{server.server_address[1]}"  # type: ignore[attr-defined]
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _run(keycloak: Any, secrets_dir: Path, public_url: str = "", registration: str = "open") -> Any:
    admin = init.HttpBootstrapAdmin(keycloak.base_url, REALM, "tok", "admin")
    return init.run_init(
        admin,
        secrets_dir=secrets_dir,
        realm_file=REALM_FILE,
        public_url=public_url,
        mcp_registration=registration,
    )


def test_a_whole_run_reaches_a_server_over_http(keycloak: Any, tmp_path: Path) -> None:
    """The end-to-end shape, and the one that would have caught the missing
    methods on its own: it constructs the class `main()` constructs and drives
    it through every phase."""
    result = _run(keycloak, tmp_path)

    assert sorted(result.created) == ["it-suite-events", "it-suite-provisioning"]
    assert keycloak.bootstrap_password is not None
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "keycloak-admin.secret",
        "keycloak-bootstrap-admin.secret",
        "keycloak-events.secret",
    ]


def test_the_missing_required_actions_are_registered_over_http(
    keycloak: Any, tmp_path: Path
) -> None:
    """The operator's bug, driven through the transport that failed to carry
    the fix. Registration and enabling are two different calls, and this is
    what proves the second one lands: the register endpoint creates an action
    DISABLED, so a job that only registered would leave `kc_action` ignored
    exactly as before."""
    _run(keycloak, tmp_path)

    for alias in ("UPDATE_PASSWORD", "webauthn-register-passwordless"):
        assert keycloak.required_actions[alias]["enabled"] is True

    assert (
        "PUT",
        f"/admin/realms/{REALM}/authentication/required-actions/UPDATE_PASSWORD",
    ) in keycloak.requests


def test_the_realms_smtp_survives_a_full_representation_put(
    keycloak: Any, tmp_path: Path
) -> None:
    """READ-MERGE-WRITE, PROVEN AT THE WIRE. Keycloak's realm PUT takes a full
    representation: a body carrying only the changed keys does not mean "leave
    the rest alone", it means "here is the realm". Sending the diff would blank
    the operator's mail configuration on every deploy -- the exact accident the
    allowlist exists to prevent, arriving through the transport instead."""
    before = dict(keycloak.realm["smtpServer"])

    _run(keycloak, tmp_path)

    assert keycloak.realm["smtpServer"] == before
    assert keycloak.realm["loginTheme"] == REALM_FILE["loginTheme"]


def test_the_web_clients_pkce_survives_the_redirect_uri_correction(
    keycloak: Any, tmp_path: Path
) -> None:
    """Dropping `pkce.code.challenge.method` would be a security downgrade
    shipped as a redirect-URI fix, and nothing in the product would report it.

    WHERE THE MERGE THAT SAVES IT ACTUALLY HAPPENS, since this test would
    otherwise be read as guarding the transport: `client_settings_to_apply`
    merges the attributes map before the transport sees it, so this passes even
    if the client PUT sends only the diff -- measured, not assumed. The
    transport's own read-merge-write is held in place by
    `test_a_second_run_over_http_changes_nothing`, which goes red on that
    change because a PUT without `clientId` breaks the next lookup."""
    _run(keycloak, tmp_path)

    web = keycloak.clients["uuid-web"]
    assert web["attributes"]["pkce.code.challenge.method"] == "S256"
    assert web["attributes"]["post.logout.redirect.uris"] == "/"
    assert web["redirectUris"] == ["/auth/callback", "http://localhost:5173/auth/callback"]


def test_a_second_run_over_http_changes_nothing(keycloak: Any, tmp_path: Path) -> None:
    """Compose starts a one-shot service again on every `up`, so this is the
    normal case. Asserted as a WRITE COUNT: a job that PUT identical values
    every deploy would work perfectly and fill the realm's admin event log with
    a change record each time, which is where a real unauthorised change would
    then be invisible."""
    _run(keycloak, tmp_path)
    keycloak.requests.clear()

    result = _run(keycloak, tmp_path)

    writes = [
        (method, path)
        for method, path in keycloak.requests
        if method in ("POST", "PUT") and "reset-password" not in path
    ]
    assert writes == []
    assert result.created == ()
    assert result.realm_changed == ()


def test_every_admin_call_carries_the_bearer_token(keycloak: Any, tmp_path: Path) -> None:
    """The fake refuses an unauthenticated admin call with 401, so this passing
    means every request the run made was authenticated. A job that dropped the
    header on one path would be a 401 at 3am on somebody else's deployment."""
    _run(keycloak, tmp_path)

    assert len([p for _, p in keycloak.requests if p.startswith("/admin/")]) > 10


def test_the_passkey_flow_is_built_and_bound_over_http(
    keycloak: Any, tmp_path: Path
) -> None:
    """The whole passkey path over a socket: copy the stock flow, add the
    authenticator, enable it, check the password leg, bind.

    The enabling step is what this catches that the unit tests cannot: Keycloak
    creates a new execution DISABLED, and the PUT that enables it goes to the
    FLOW's executions collection with the execution as the body -- not to an
    execution URL, which is the shape every other update in this job uses. A
    wrong verb or path there leaves a passkey button that is never offered, and
    nothing anywhere reports it.
    """
    result = _run(keycloak, tmp_path)

    assert result.passkey_flow == "bound"
    assert keycloak.realm["browserFlow"] == "browser-passkey"
    passkey = [
        e
        for e in keycloak.flows["browser-passkey"]
        if e.get("providerId") == "webauthn-authenticator-passwordless"
    ]
    assert [e["requirement"] for e in passkey] == ["ALTERNATIVE"]
    assert init.password_alternative_survives(keycloak.flows["browser-passkey"])


def test_a_second_run_over_http_does_not_touch_the_flow(
    keycloak: Any, tmp_path: Path
) -> None:
    """Rebinding the mechanism every account depends on, on every deploy, for
    no reason."""
    _run(keycloak, tmp_path)
    keycloak.requests.clear()

    result = _run(keycloak, tmp_path)

    assert result.passkey_flow == "already-bound"
    assert [
        (m, p) for m, p in keycloak.requests if "copy" in p or p.endswith("/execution")
    ] == []


# --- the MCP surfaces over HTTP (2026-10-15) ----------------------------------
#
# `test_keycloak_init_job.py` proves what the MCP convergence DECIDES; this
# proves the requests that carry it: method, path and body shape, against a
# server that answers the way Keycloak 26.2's admin API does. As above, that
# is this project's belief about Keycloak, checked live by Task 12's CI step.

CONSOLE_URL = "https://itms.example.com"
MCP_RESOURCE = f"{CONSOLE_URL}/mcp"
REALM_PATH = f"/admin/realms/{REALM}"


def _writes(keycloak: Any) -> list[tuple[str, str, Any]]:
    return [(m, p, b) for m, p, b in keycloak.bodies if "reset-password" not in p]


def _mcp_scope(keycloak: Any) -> dict[str, Any]:
    return next(s for s in keycloak.client_scopes.values() if s["name"] == "mcp")


def _anonymous(keycloak: Any) -> dict[str, dict[str, Any]]:
    return {
        c["providerId"]: c for c in keycloak.components.values() if c["subType"] == "anonymous"
    }


def test_the_mcp_scope_is_created_with_its_mappers_over_http(
    keycloak: Any, tmp_path: Path
) -> None:
    _run(keycloak, tmp_path, CONSOLE_URL)

    scope = _mcp_scope(keycloak)
    created = [
        b for m, p, b in keycloak.bodies if (m, p) == ("POST", f"{REALM_PATH}/client-scopes")
    ]
    assert len(created) == 1
    assert created[0]["name"] == "mcp" and created[0]["protocol"] == "openid-connect"
    assert "protocolMappers" not in created[0], "mappers go through their own endpoint"
    assert scope["attributes"]["include.in.token.scope"] == "true"

    mapper_posts = [
        b
        for m, p, b in keycloak.bodies
        if m == "POST" and p == f"{REALM_PATH}/client-scopes/{scope['id']}/protocol-mappers/models"
    ]
    assert sorted(b["name"] for b in mapper_posts) == ["mcp-audience", "mcp-sub", "org-claim"]
    by_name = {m["name"]: m for m in scope["protocolMappers"]}
    assert by_name["mcp-audience"]["config"]["included.custom.audience"] == MCP_RESOURCE
    assert by_name["mcp-sub"]["protocolMapper"] == "oidc-sub-mapper"


def test_mcp_is_made_a_default_optional_scope_over_http(keycloak: Any, tmp_path: Path) -> None:
    _run(keycloak, tmp_path, CONSOLE_URL)

    scope_id = _mcp_scope(keycloak)["id"]
    assert ("PUT", f"{REALM_PATH}/default-optional-client-scopes/{scope_id}", None) in (
        keycloak.bodies
    )
    assert scope_id in keycloak.default_optional


def test_a_wrong_audience_is_put_with_the_mappers_id_in_the_body(
    keycloak: Any, tmp_path: Path
) -> None:
    """Keycloak rebuilds the mapper from the BODY on PUT; without its `id` the
    update finds nothing. The fake answers 404 then, as Keycloak does."""
    _run(keycloak, tmp_path, "https://old.example.com")
    scope = _mcp_scope(keycloak)
    old_id = next(m["id"] for m in scope["protocolMappers"] if m["name"] == "mcp-audience")
    keycloak.bodies.clear()

    _run(keycloak, tmp_path, CONSOLE_URL)

    puts = [
        (p, b)
        for m, p, b in keycloak.bodies
        if m == "PUT" and "/protocol-mappers/models/" in p
    ]
    assert puts == [
        (
            f"{REALM_PATH}/client-scopes/{scope['id']}/protocol-mappers/models/{old_id}",
            puts[0][1],
        )
    ]
    assert puts[0][1]["id"] == old_id
    assert puts[0][1]["config"]["included.custom.audience"] == MCP_RESOURCE


def test_a_wrong_scope_attribute_is_corrected_over_http(keycloak: Any, tmp_path: Path) -> None:
    keycloak.client_scopes["scope-mcp"] = {
        "id": "scope-mcp",
        "name": "mcp",
        "protocol": "openid-connect",
        "attributes": {"include.in.token.scope": "false", "gui.order": "3"},
        "protocolMappers": [],
    }

    _run(keycloak, tmp_path, CONSOLE_URL)

    assert ("PUT", f"{REALM_PATH}/client-scopes/scope-mcp") in [
        (m, p) for m, p, _ in keycloak.bodies
    ]
    attributes = keycloak.client_scopes["scope-mcp"]["attributes"]
    assert attributes["include.in.token.scope"] == "true"
    assert attributes["gui.order"] == "3"


def test_registration_components_are_corrected_and_opened_over_http(
    keycloak: Any, tmp_path: Path
) -> None:
    """The emptied mapper allowlist is SENT as `[]`, because Keycloak removes a
    key sent empty and keeps one not sent; and Trusted Hosts is the last
    write, a DELETE."""
    _run(keycloak, tmp_path, CONSOLE_URL)

    component_puts = {
        p.rsplit("/", 1)[1]: b
        for m, p, b in keycloak.bodies
        if m == "PUT" and p.startswith(f"{REALM_PATH}/components/")
    }
    assert component_puts["stock-anon-mappers"]["config"]["allowed-protocol-mapper-types"] == []
    anonymous = _anonymous(keycloak)
    assert "allowed-protocol-mapper-types" not in anonymous["allowed-protocol-mappers"]["config"]
    assert "mcp" in anonymous["allowed-client-templates"]["config"]["allowed-client-scopes"]
    assert "trusted-hosts" not in anonymous

    writes = [(m, p) for m, p, _ in _writes(keycloak)]
    assert writes[-1] == ("DELETE", f"{REALM_PATH}/components/stock-trusted")
    assert ("GET", f"{REALM_PATH}/components") in keycloak.requests


def test_without_a_console_url_trusted_hosts_stays_over_http(
    keycloak: Any, tmp_path: Path
) -> None:
    _run(keycloak, tmp_path)

    assert "trusted-hosts" in _anonymous(keycloak)
    assert not [p for m, p, _ in keycloak.bodies if m == "DELETE"]


def test_a_missing_trusted_hosts_is_posted_back_over_http(keycloak: Any, tmp_path: Path) -> None:
    del keycloak.components["stock-trusted"]

    _run(keycloak, tmp_path)

    posted = [
        b for m, p, b in keycloak.bodies if (m, p) == ("POST", f"{REALM_PATH}/components")
    ]
    assert posted[0]["providerId"] == "trusted-hosts"
    assert posted[0]["providerType"] == (
        "org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy"
    )
    assert posted[0]["subType"] == "anonymous"
    restored = _anonymous(keycloak)["trusted-hosts"]
    assert restored["parentId"] == "realm-id"


def test_client_profiles_and_policies_are_put_whole_over_http(
    keycloak: Any, tmp_path: Path
) -> None:
    """The admin API's PUT replaces the realm's whole list, so an operator's
    own profile must travel in the body; and the profile lands before the
    policy that names it, or Keycloak answers 400."""
    keycloak.client_profiles = [{"name": "theirs", "executors": []}]

    _run(keycloak, tmp_path, CONSOLE_URL)

    puts = [
        (p, b) for m, p, b in keycloak.bodies if m == "PUT" and "/client-policies/" in p
    ]
    assert [p for p, _ in puts] == [
        f"{REALM_PATH}/client-policies/profiles",
        f"{REALM_PATH}/client-policies/policies",
    ]
    assert [x["name"] for x in puts[0][1]["profiles"]] == ["theirs", "itms-mcp-apps"]
    assert [x["name"] for x in puts[1][1]["policies"]] == ["itms-mcp-apps"]


def test_the_reserved_client_ids_are_posted_and_found_again_over_http(
    keycloak: Any, tmp_path: Path
) -> None:
    """The MCP resource is a URL used as a clientId: the lookup must encode it
    and still find it on the next run, or every deploy POSTs a duplicate (409
    from Keycloak)."""
    _run(keycloak, tmp_path, CONSOLE_URL)

    reserved = {c["clientId"]: c for c in keycloak.clients.values()}
    for client_id in ("it-suite", MCP_RESOURCE):
        assert reserved[client_id]["enabled"] is False
        assert reserved[client_id]["bearerOnly"] is True
        assert reserved[client_id]["redirectUris"] == []


def test_a_second_mcp_run_over_http_writes_nothing(keycloak: Any, tmp_path: Path) -> None:
    _run(keycloak, tmp_path, CONSOLE_URL)
    keycloak.bodies.clear()

    result = _run(keycloak, tmp_path, CONSOLE_URL)

    assert _writes(keycloak) == []
    assert result.mcp_converged == ()
