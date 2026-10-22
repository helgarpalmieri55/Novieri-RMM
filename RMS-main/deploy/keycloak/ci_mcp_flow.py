#!/usr/bin/env python3
"""Live proof of the MCP sign-in path: Keycloak, through Traefik, as an AI app sees it.

MCP spec 2026-10-09 §3; Keycloak task 11 (fix rounds 0-4) and its "What Task
12's live CI step must prove" list. `backend/tests/test_keycloak_init_job.py`
proves what keycloak-init DECIDES against a fake realm, and
`backend/tests/test_deploy_keycloak_registration_edge.py` proves what the
Traefik rules SAY. Neither can show what Keycloak 26.2 and Traefik 3.6 DO with
them. This script is that half: it runs against the real compose `traefik` and
`keycloak` services, from outside, the way an AI app does.

TWO ADDRESSES, ON PURPOSE.
  --base        the public entrypoint, through Traefik (https, :443). Every
                registration, sign-in and token request goes here, because
                the edge rules are part of what is being proved.
  --admin-base  Keycloak's own port on the compose network, which Traefik
                never routes (the admin API is off :443 by design). Used only
                to set up the test person and to look at what a request did
                or did not create.

WHAT IT NEVER PRINTS. Tokens, secrets, passwords, registration access tokens
and authorization codes. Claims are printed by NAME, with values only for the
non-secret claims in `SHOWN_CLAIMS`.

Standard library only (`python3` on the CI runner, no venv). Exits 1 with a
`FAIL` line per failed assertion, 2 on a usage or connection error.

    python3 deploy/keycloak/ci_mcp_flow.py --base https://localhost \\
        --cafile deploy/certs/fullchain.pem --realm it-suite \\
        --resource https://itms.example.test/mcp \\
        --admin-base http://172.19.240.3:8080 --admin-user admin \\
        --admin-password-file /tmp/kc/admin --provisioning-secret-file /tmp/kc/prov

    # Before ITS_PUBLIC_CONSOLE_URL is set: registration must be closed.
    python3 deploy/keycloak/ci_mcp_flow.py --base https://localhost \\
        --cafile deploy/certs/fullchain.pem --realm it-suite --expect-registration-closed
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import secrets
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from email.message import Message
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from typing import Any

#: A loopback http callback, the form native MCP clients register (task 11
#: fix round 2 allows it; PKCE binds the code).
REDIRECT_URI = "http://127.0.0.1:33418/callback"
#: The console's own token audience. No token this script obtains for an app
#: may carry it.
CONSOLE_AUDIENCE = "it-suite"
#: The value the test person's `org` attribute holds and the token must carry.
TEST_ORG = "ci-mcp-org"
#: Claims whose VALUES are safe to print. Everything else is printed by name.
SHOWN_CLAIMS = frozenset(
    {"typ", "aud", "azp", "scope", "org", "client_id", "preferred_username", "iss"}
)
TIMEOUT_S = 30


class Failed(Exception):
    """One assertion that did not hold. The message says what was expected."""


def check(condition: object, message: str) -> None:
    if not condition:
        raise Failed(message)


# --------------------------------------------------------------------------- #
# Pure helpers (backend/tests/test_ci_mcp_flow.py)
# --------------------------------------------------------------------------- #


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def s256(verifier: str) -> str:
    """RFC 7636 §4.2: BASE64URL(SHA256(ASCII(code_verifier))), unpadded."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def pkce_pair() -> tuple[str, str]:
    """`(verifier, S256 challenge)`. 48 random bytes make a 64-character
    verifier, inside RFC 7636 §4.1's 43-128."""
    verifier = _b64url(secrets.token_bytes(48))
    return verifier, s256(verifier)


def jwt_claims(token: str) -> dict[str, Any]:
    """The payload of a JWT, NOT verified -- CI asserts on what Keycloak put
    in it; the api's verification is the backend suite's business."""
    parts = token.split(".")
    if len(parts) != 3:
        raise Failed("token is not a three-part JWT")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    if not isinstance(claims, dict):
        raise Failed("token payload is not a JSON object")
    return claims


def audiences(claims: Mapping[str, Any]) -> list[str]:
    aud = claims.get("aud")
    if aud is None:
        return []
    return [aud] if isinstance(aud, str) else [str(a) for a in aud]


def exact_audience(claims: Mapping[str, Any], resource: str) -> bool:
    """The token's audience is the MCP resource and nothing else -- no
    `it-suite`, no `account`, no second resource (spec §3.2, invariant 1c(a))."""
    return audiences(claims) == [resource]


#: The console's browser client: every console door admits a token only when
#: its `azp` is this (`app/core/security.py`, `ITS_OIDC_CONSOLE_CLIENT_ID`).
CONSOLE_CLIENT = "it-suite-web"


def check_console_token(claims: Mapping[str, Any], resource: str) -> list[str]:
    """The console's own sign-in, as the api requires it on every console
    door: `azp` the console's client, `typ` Bearer, the console's audience
    and never the MCP resource. Returns the audience, for the success line."""
    check(
        claims.get("azp") == CONSOLE_CLIENT,
        f"the console's own token has azp {claims.get('azp')!r}, not {CONSOLE_CLIENT!r}",
    )
    check(claims.get("typ") == "Bearer", f"the console's own token has typ {claims.get('typ')!r}")
    aud = audiences(claims)
    check(CONSOLE_AUDIENCE in aud, f"the console's own token has aud {aud}")
    check(resource not in aud, "the console's own token carries the MCP resource")
    return aud


#: The consent screen's line for the `mcp` scope, as the itms login theme
#: words it (`messages_<lang>.properties`, key `itmsMcpConsent`).
CONSENT_TEXT = {
    "en": (
        "Act as you in ITMS: everything your role allows, including running actions "
        "on managed computers"
    ),
    "es": (
        "Actuar como tú en ITMS: todo lo que permite tu rol, incluido ejecutar acciones "
        "en equipos gestionados"
    ),
}


def consent_page_says(page: str, language: str) -> bool:
    """The consent page carries the `mcp` line in `language`, resolved from
    the theme's bundle (an unresolved key shows as the key itself)."""
    return CONSENT_TEXT[language] in " ".join(html.unescape(page).split())


def describe_claims(claims: Mapping[str, Any]) -> str:
    """Claim names, with values only for `SHOWN_CLAIMS`. Safe to print."""
    shown = []
    for name in sorted(claims):
        if name in SHOWN_CLAIMS:
            shown.append(f"{name}={json.dumps(claims[name])}")
        else:
            shown.append(name)
    return " ".join(shown)


def is_callback(location: str, redirect_uri: str) -> bool:
    """Same scheme, host, port and path -- not a string prefix, which
    `.../callback.evil` would also pass."""
    got, want = urllib.parse.urlsplit(location), urllib.parse.urlsplit(redirect_uri)
    return (got.scheme, got.netloc, got.path) == (want.scheme, want.netloc, want.path)


def callback_params(location: str, redirect_uri: str) -> dict[str, str]:
    """The query of a redirect to `redirect_uri`. Raises if it goes elsewhere."""
    if not is_callback(location, redirect_uri):
        raise Failed(f"redirected to {location.split('?')[0]!r}, not the registered callback")
    query = urllib.parse.urlsplit(location).query
    return {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}


@dataclass
class Form:
    action: str
    method: str
    #: Every named field with its value (hidden, text, password, submit...).
    fields: dict[str, str] = field(default_factory=dict)
    #: Submit controls, `name -> value`, kept apart so only one is sent.
    submits: dict[str, str] = field(default_factory=dict)


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[Form] = []
        self._open: Form | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "form":
            self._open = Form(a.get("action", ""), a.get("method", "get").lower())
            self.forms.append(self._open)
        elif tag in ("input", "button") and self._open is not None and a.get("name"):
            kind = a.get("type", "submit" if tag == "button" else "text").lower()
            if kind == "submit":
                self._open.submits[a["name"]] = a.get("value", "")
            elif kind not in ("checkbox", "radio") or "checked" in a:
                self._open.fields[a["name"]] = a.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._open = None


def parse_forms(html: str) -> list[Form]:
    parser = _FormParser()
    parser.feed(html)
    return parser.forms


def page_title(html: str) -> str:
    start = html.find("<title>")
    end = html.find("</title>", start)
    return " ".join(html[start + 7 : end].split()) if start >= 0 and end > start else "?"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


@dataclass
class Response:
    url: str
    status: int
    headers: Message
    body: bytes

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except json.JSONDecodeError as exc:
            raise Failed(f"{self.url.split('?')[0]} answered {self.status} with non-JSON") from exc

    def error(self) -> str:
        """Keycloak's `error`/`error_description`, or the body's start. Never
        called on a body that could hold a secret."""
        try:
            data = json.loads(self.body)
            if isinstance(data, dict):
                return " ".join(
                    str(data[k])
                    for k in ("error", "error_description", "errorMessage")
                    if k in data
                )
        except json.JSONDecodeError:
            pass
        return self.text()[:120].replace("\n", " ")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is a result here (the code lives in it), not something to follow."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class Browser:
    """One cookie jar, no redirects followed, no proxy, TLS verified."""

    def __init__(self, cafile: str | None) -> None:
        context = ssl.create_default_context(cafile=cafile)
        self._opener = urllib.request.build_opener(
            # Everything this talks to is local; an inherited HTTPS_PROXY
            # would send the edge probes somewhere else entirely.
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
            urllib.request.HTTPCookieProcessor(CookieJar()),
            _NoRedirect(),
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        form: Mapping[str, str] | None = None,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        data = None
        all_headers = dict(headers or {})
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            all_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif body is not None:
            data = json.dumps(body).encode()
            all_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(url, data=data, headers=all_headers, method=method)
        try:
            with self._opener.open(request, timeout=TIMEOUT_S) as reply:
                return Response(url, reply.status, reply.headers, reply.read())
        except urllib.error.HTTPError as exc:
            return Response(url, exc.code, exc.headers, exc.read())


def _basic(client_id: str, secret: str) -> dict[str, str]:
    pair = f"{urllib.parse.quote(client_id)}:{urllib.parse.quote(secret)}"
    return {"Authorization": "Basic " + base64.b64encode(pair.encode()).decode()}


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #


@dataclass
class Registered:
    client_id: str
    registration_access_token: str
    metadata: dict[str, Any]
    #: The registration URL it was created at (to manage it with RFC 7592).
    endpoint: str


@dataclass
class Run:
    args: argparse.Namespace
    browser: Browser
    admin_token: str = ""
    user_id: str = ""
    username: str = ""
    password: str = ""
    registered: list[Registered] = field(default_factory=list)
    #: The clientId the edge probes ask for; removed at cleanup if one leaked.
    edge_probe: str = ""
    failures: list[str] = field(default_factory=list)
    #: The consent pages the sign-ins accepted, in order (`consent_says`).
    consent_pages: list[str] = field(default_factory=list)

    # -- addresses -------------------------------------------------------

    @property
    def realm_url(self) -> str:
        return f"{self.args.base}/realms/{self.args.realm}"

    @property
    def registration(self) -> str:
        return f"{self.realm_url}/clients-registrations/openid-connect"

    @property
    def token_url(self) -> str:
        return f"{self.realm_url}/protocol/openid-connect/token"

    @property
    def auth_url(self) -> str:
        return f"{self.realm_url}/protocol/openid-connect/auth"

    @property
    def admin_realm(self) -> str:
        return f"{self.args.admin_base}/admin/realms/{self.args.realm}"

    # -- output ----------------------------------------------------------

    @staticmethod
    def ok(message: str) -> None:
        print(f"ok    {message}", flush=True)

    @staticmethod
    def info(message: str) -> None:
        print(f"info  {message}", flush=True)

    def section(self, name: str, step: Callable[[], None]) -> None:
        print(f"--- {name}", flush=True)
        try:
            step()
        except Failed as exc:
            self.failures.append(f"{name}: {exc}")
            print(f"FAIL  {exc}", flush=True)

    # -- admin (Keycloak's own port, never through Traefik) ---------------

    def admin(self, method: str, path: str, body: Any = None) -> Response:
        """An admin call. The master realm's access tokens live about a
        minute, shorter than a slow run, so a 401 signs in again once."""
        for attempt in (1, 2):
            reply = self.browser.request(
                method,
                f"{self.admin_realm}{path}",
                body=body,
                headers={"Authorization": f"Bearer {self.admin_token}"},
            )
            if reply.status != 401 or attempt == 2:
                return reply
            self.admin_login()
        raise AssertionError("unreachable")

    def admin_login(self) -> None:
        with open(self.args.admin_password_file, encoding="utf-8") as handle:
            password = handle.read().strip()
        reply = self.browser.request(
            "POST",
            f"{self.args.admin_base}/realms/master/protocol/openid-connect/token",
            form={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": self.args.admin_user,
                "password": password,
            },
        )
        check(reply.status == 200, f"admin sign-in refused ({reply.status} {reply.error()})")
        self.admin_token = str(reply.json()["access_token"])

    def clients_named(self, client_id: str) -> list[dict[str, Any]]:
        reply = self.admin("GET", f"/clients?clientId={urllib.parse.quote(client_id)}")
        check(reply.status == 200, f"admin client search answered {reply.status}")
        return [c for c in reply.json() if c.get("clientId") == client_id]

    def client_count(self) -> int:
        reply = self.admin("GET", "/clients?first=0&max=10000")
        check(reply.status == 200, f"admin client list answered {reply.status}")
        return len(reply.json())

    def client_by_id(self, client_id: str) -> dict[str, Any]:
        found = self.clients_named(client_id)
        check(len(found) == 1, f"expected one client {client_id!r}, found {len(found)}")
        return found[0]

    # -- registration ----------------------------------------------------

    def register(
        self, metadata: Mapping[str, Any], *, endpoint: str | None = None
    ) -> tuple[Response, Registered | None]:
        """POST to the registration endpoint. Every client it creates is kept
        for cleanup, whether or not the caller expected one."""
        url = endpoint or self.registration
        reply = self.browser.request("POST", url, body=dict(metadata))
        if reply.status != 201:
            return reply, None
        data = reply.json()
        app = Registered(str(data["client_id"]), str(data["registration_access_token"]), data, url)
        self.registered.append(app)
        return reply, app

    def register_ok(
        self, metadata: Mapping[str, Any], *, endpoint: str | None = None
    ) -> Registered:
        reply, app = self.register(metadata, endpoint=endpoint)
        if app is None:
            raise Failed(f"registration answered {reply.status} ({reply.error()})")
        return app

    # -- sign-in ---------------------------------------------------------

    def authorize(
        self,
        browser: Browser,
        params: Mapping[str, str],
        redirect_uri: str,
    ) -> dict[str, str]:
        """Drive the sign-in pages until Keycloak redirects to `redirect_uri`,
        and return that redirect's query. Fills the login form (username and
        password, on one page or two) and accepts the consent page."""
        reply = browser.request("GET", f"{self.auth_url}?{urllib.parse.urlencode(params)}")
        for _ in range(8):
            if reply.status in (301, 302, 303, 307):
                location = urllib.parse.urljoin(reply.url, reply.headers.get("Location", ""))
                if is_callback(location, redirect_uri):
                    return callback_params(location, redirect_uri)
                reply = browser.request("GET", location)
                continue
            check(
                reply.status == 200,
                f"sign-in page answered {reply.status} ({page_title(reply.text())})",
            )
            forms = [f for f in parse_forms(reply.text()) if f.method == "post"]
            login = next((f for f in forms if "password" in f.fields), None) or next(
                (f for f in forms if "username" in f.fields), None
            )
            consent = next((f for f in forms if "accept" in f.submits), None)
            if login is not None:
                data = dict(login.fields)
                data["username"] = self.username
                if "password" in data:
                    data["password"] = self.password
                form, what = data, "login"
                target = login
            elif consent is not None:
                self.consent_pages.append(reply.text())
                form = {**consent.fields, "accept": consent.submits["accept"]}
                target, what = consent, "consent"
            else:
                raise Failed(
                    f"unexpected sign-in page {page_title(reply.text())!r} with forms "
                    f"{[sorted(f.fields) + sorted(f.submits) for f in forms]}"
                )
            self.info(f"sign-in: submitted the {what} page")
            reply = browser.request(
                "POST", urllib.parse.urljoin(reply.url, target.action), form=form
            )
        raise Failed("sign-in did not reach the callback within 8 pages")

    def code_flow(
        self,
        client_id: str,
        *,
        scope: str,
        redirect_uri: str = REDIRECT_URI,
        browser: Browser | None = None,
        ui_locales: str | None = None,
    ) -> dict[str, Any]:
        """Authorization code + PKCE S256; returns the token response."""
        browser = browser or Browser(self.args.cafile)
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(16)
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if ui_locales is not None:
            params["ui_locales"] = ui_locales
        result = self.authorize(browser, params, redirect_uri)
        check("code" in result, f"no code in the callback ({result.get('error', '?')})")
        check(result.get("state") == state, "the callback's state does not match")
        reply = browser.request(
            "POST",
            self.token_url,
            form={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": result["code"],
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
        )
        check(reply.status == 200, f"token request answered {reply.status} ({reply.error()})")
        tokens: dict[str, Any] = reply.json()
        check(isinstance(tokens, dict) and "access_token" in tokens, "no access_token returned")
        return tokens

    def assert_mcp_token(self, tokens: Mapping[str, Any], client_id: str) -> None:
        claims = jwt_claims(str(tokens["access_token"]))
        self.info(f"access token claims: {describe_claims(claims)}")
        aud = audiences(claims)
        check(claims.get("typ") == "Bearer", f"typ is {claims.get('typ')!r}, not 'Bearer'")
        check(CONSOLE_AUDIENCE not in aud, f"aud {aud} carries the console's {CONSOLE_AUDIENCE!r}")
        check(
            exact_audience(claims, self.args.resource),
            f"aud {aud} is not exactly [{self.args.resource!r}]",
        )
        check(claims.get("sub"), "no sub: the token does not name a person")
        # The api requires `sid` at /mcp (`require_person_mcp_token`): a person's
        # token must carry their sign-in session.
        check(claims.get("sid"), "no sid: the api would refuse this person's token at /mcp")
        check(claims.get("org") == TEST_ORG, f"org is {claims.get('org')!r}, not {TEST_ORG!r}")
        check(claims.get("azp") == client_id, "azp is not the registered client's id")
        check("mcp" in str(claims.get("scope", "")).split(), "scope does not include mcp")
        # The api tells a service account from a person by this claim
        # (app/core/security.py, `is_service_account`); a person's must lack it.
        check(
            not {"client_id", "clientId"} & set(claims),
            "a person's token carries the service-account client_id claim",
        )
        self.ok(
            f"access token: typ Bearer, aud exactly {aud}, sub, org, azp = the registered client"
        )
        if "id_token" in tokens:
            id_claims = jwt_claims(str(tokens["id_token"]))
            self.info(f"id token claims: {describe_claims(id_claims)}")
            id_aud = audiences(id_claims)
            check(CONSOLE_AUDIENCE not in id_aud, f"id token aud {id_aud} carries the console's")
            self.ok(f"id token: aud {id_aud}, no {CONSOLE_AUDIENCE!r}")

    # -- sections ----------------------------------------------------------

    def discovery(self) -> None:
        reply = self.browser.request("GET", f"{self.realm_url}/.well-known/openid-configuration")
        check(reply.status == 200, f"OIDC discovery answered {reply.status}")
        doc = reply.json()
        check(
            str(doc.get("registration_endpoint", "")).endswith(
                f"/realms/{self.args.realm}/clients-registrations/openid-connect"
            ),
            "discovery names no OIDC registration endpoint",
        )
        check("S256" in doc.get("code_challenge_methods_supported", []), "S256 not supported")
        self.ok("OIDC discovery through Traefik: registration endpoint, S256")
        reply = self.browser.request(
            "GET", f"{self.realm_url}/.well-known/oauth-authorization-server"
        )
        check(reply.status == 200, f"RFC 8414 metadata answered {reply.status}")
        self.ok("RFC 8414 metadata through Traefik")

    def registration_closed(self) -> None:
        reply, _ = self.register(
            {"redirect_uris": [REDIRECT_URI], "token_endpoint_auth_method": "none"}
        )
        check(reply.status == 403, f"anonymous registration answered {reply.status}, not 403")
        # Keycloak's refusal, not the edge's: Traefik's deny says only "Forbidden".
        check(
            "Trusted Hosts" in reply.text(),
            f"refused, but not by Keycloak's Trusted Hosts policy ({reply.error()})",
        )
        self.ok("anonymous registration refused by Trusted Hosts")

    def setup_person(self) -> None:
        self.admin_login()
        self.username = f"mcp-ci-{secrets.token_hex(4)}"
        # Never printed. The suffix satisfies any character-class policy.
        self.password = secrets.token_urlsafe(24) + "-Aa1!"
        reply = self.admin(
            "POST",
            "/users",
            {
                "username": self.username,
                "email": f"{self.username}@example.invalid",
                "emailVerified": True,
                "firstName": "MCP",
                "lastName": "CI",
                "enabled": True,
                "attributes": {"org": [TEST_ORG]},
                "requiredActions": [],
                "credentials": [{"type": "password", "value": self.password, "temporary": False}],
            },
        )
        check(reply.status == 201, f"creating the test person answered {reply.status}")
        self.user_id = reply.headers.get("Location", "").rstrip("/").rsplit("/", 1)[-1]
        user = self.admin("GET", f"/users/{self.user_id}").json()
        if user.get("requiredActions"):
            # A realm default action (CONFIGURE_TOTP) is added at creation.
            self.info(f"clearing default required actions {user['requiredActions']}")
            user["requiredActions"] = []
            reply = self.admin("PUT", f"/users/{self.user_id}", user)
            check(reply.status == 204, f"clearing required actions answered {reply.status}")
            user = self.admin("GET", f"/users/{self.user_id}").json()
        check(not user.get("requiredActions"), "the test person still has required actions")
        check(
            user.get("attributes", {}).get("org") == [TEST_ORG],
            "the realm dropped the test person's org attribute",
        )
        self.ok(f"test person {self.username} with org={TEST_ORG}, no required actions")

    def reservations(self) -> None:
        for client_id in (CONSOLE_AUDIENCE, self.args.resource):
            client = self.client_by_id(client_id)
            check(client.get("enabled") is False, f"reservation {client_id!r} is enabled")
            self.ok(f"reservation {client_id!r} exists and is disabled")

    def edge(self) -> None:
        """Every spelling that reaches a non-OIDC provider is refused by
        Traefik (403, body "Forbidden"), and Keycloak creates nothing."""
        probe = self.edge_probe = f"ci-edge-probe-{secrets.token_hex(4)}"
        realm = self.args.realm
        cr = f"/realms/{realm}/clients-registrations"
        oidc = f"{cr}/openid-connect"
        paths = [
            f"{cr}/default",
            f"{cr}/default;x=y",
            f"{cr}/default%3Bx=y",
            f"{cr}/default%3bx=y",
            f"{cr}/Default",
            f"{cr}/DEFAULT",
            f"/realms;x/{realm}/clients-registrations/default",
            f"/realms%3Bx/{realm}/clients-registrations/default",
            f"/realms%3bx/{realm}/clients-registrations/default",
            f"/realms/{realm};x/clients-registrations/default",
            f"/realms/{realm}%3Bx/clients-registrations/default",
            f"{cr};x/default",
            f"{cr}%3Bx/default",
            f"{oidc}/../default",
            f"{oidc}/%2E%2E/default",
            f"{oidc}/%2e%2e/default",
            f"{oidc}%2F..%2Fdefault",
            f"{oidc}%5C..%5Cdefault",
            f"{oidc}\\..\\default",
            f"{oidc}%00",
            f"{cr}/install/{probe}",
            f"{cr}/saml2-entity-descriptor",
            f"{cr}/oid4vc",
            f"/realms/{realm}/Clients-Registrations/openid-connect",
            f"/realms/{realm}/CLIENTS-REGISTRATIONS/openid-connect",
            cr,
            f"{cr}/",
        ]
        before = self.client_count()
        for path in paths:
            for method in ("POST", "GET"):
                reply = self.browser.request(
                    method,
                    f"{self.args.base}{path}",
                    body={"clientId": probe, "redirectUris": [REDIRECT_URI]}
                    if method == "POST"
                    else None,
                )
                check(
                    reply.status == 403 and reply.text().strip() == "Forbidden",
                    f"{method} {path} answered {reply.status} {reply.error()!r}, not Traefik's 403",
                )
        after = self.client_count()
        check(not self.clients_named(probe), f"a denied request created client {probe!r}")
        check(after == before, f"the realm went from {before} to {after} clients")
        self.ok(f"{len(paths)} paths x POST/GET refused at Traefik; Keycloak created nothing")

    def mcp_sign_in(self) -> None:
        """Checklist 1: an app that sends its own `scope` (so it has no `basic`
        scope, and `sub` must come from the mcp scope's own mapper)."""
        app = self.register_ok(
            {
                "client_name": "ITMS CI MCP app",
                "redirect_uris": [REDIRECT_URI],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": "mcp",
            }
        )
        self.ok(f"anonymous OIDC registration with scope=mcp: 201 ({app.client_id})")
        browser = Browser(self.args.cafile)
        self.consent_pages.clear()
        tokens = self.code_flow(app.client_id, scope="openid mcp", browser=browser)
        self.assert_mcp_token(tokens, app.client_id)
        self.consent_says("en")
        self.pkce_is_required(app.client_id, browser)

    def consent_says(self, language: str) -> None:
        """The consent page that was just accepted said what approving hands
        over, in `language`."""
        check(self.consent_pages, "the sign-in never showed a consent page")
        check(
            consent_page_says(self.consent_pages[-1], language),
            f"the consent page does not carry the {language} mcp line "
            f"({page_title(self.consent_pages[-1])})",
        )
        self.ok(f"consent page ({language}): {CONSENT_TEXT[language]!r}")

    def pkce_is_required(self, client_id: str, browser: Browser) -> None:
        base = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "scope": "openid mcp",
            "state": "s",
        }
        # No challenge at all, then a `plain` one: neither may produce a code.
        for extra, what in (
            ({}, "no code_challenge"),
            ({"code_challenge": "x" * 43, "code_challenge_method": "plain"}, "method plain"),
        ):
            result = self.authorize(browser, {**base, **extra}, REDIRECT_URI)
            check("code" not in result, f"authorization with {what} issued a code")
            self.ok(f"authorization with {what} refused ({result.get('error', '?')})")
        # A code redeemed without its verifier.
        verifier, challenge = pkce_pair()
        del verifier
        result = self.authorize(
            browser,
            {**base, "code_challenge": challenge, "code_challenge_method": "S256"},
            REDIRECT_URI,
        )
        check("code" in result, f"no code ({result.get('error', '?')})")
        reply = browser.request(
            "POST",
            self.token_url,
            form={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": result["code"],
                "redirect_uri": REDIRECT_URI,
            },
        )
        check(reply.status == 400, f"token without code_verifier answered {reply.status}")
        self.ok(f"token request without code_verifier refused ({reply.error()})")

    def default_scope_sign_in(self) -> None:
        """Checklist 1, second half: an app that sends NO `scope` gets the
        realm's defaults plus the optional mcp scope. Registered at the
        trailing-slash spelling of the endpoint, which the edge forwards."""
        app = self.register_ok(
            {
                "client_name": "ITMS CI MCP app (default scopes)",
                "redirect_uris": [REDIRECT_URI],
                "grant_types": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_method": "none",
            },
            endpoint=f"{self.registration}/",
        )
        self.ok(f"registration at .../openid-connect/ (trailing slash): 201 ({app.client_id})")
        # In Spanish, so both bundles are proved to resolve the consent line.
        self.consent_pages.clear()
        tokens = self.code_flow(app.client_id, scope="openid mcp", ui_locales="es")
        self.assert_mcp_token(tokens, app.client_id)
        self.consent_says("es")

    def self_management(self) -> None:
        """RFC 7592 through the edge: read, update, delete one's own client."""
        app = self.register_ok(
            {
                "client_name": "ITMS CI RFC 7592",
                "redirect_uris": [REDIRECT_URI],
                "token_endpoint_auth_method": "none",
            }
        )
        url = f"{self.registration}/{app.client_id}"
        auth = {"Authorization": f"Bearer {app.registration_access_token}"}
        reply = self.browser.request("GET", url, headers=auth)
        check(reply.status == 200, f"RFC 7592 GET answered {reply.status}")
        current = reply.json()
        # Each response rotates the registration access token.
        auth = {"Authorization": f"Bearer {current['registration_access_token']}"}
        update = {k: v for k, v in current.items() if k != "registration_access_token"}
        update["client_name"] = "ITMS CI RFC 7592 (renamed)"
        reply = self.browser.request("PUT", url, body=update, headers=auth)
        check(reply.status == 200, f"RFC 7592 PUT answered {reply.status} ({reply.error()})")
        auth = {"Authorization": f"Bearer {reply.json()['registration_access_token']}"}
        reply = self.browser.request("DELETE", url, headers=auth)
        check(reply.status == 204, f"RFC 7592 DELETE answered {reply.status}")
        self.registered.remove(app)
        self.ok("RFC 7592 GET 200, PUT 200, DELETE 204 through Traefik")

    def console_audience_unreachable(self) -> None:
        """Checklist 3. Naming the console's audience as a client id, or asking
        for a scope outside the allowed list, gets nowhere."""
        for key in ("client_id", "clientId"):
            reply, app = self.register(
                {
                    key: CONSOLE_AUDIENCE,
                    "redirect_uris": [REDIRECT_URI],
                    "token_endpoint_auth_method": "none",
                }
            )
            if app is not None:
                check(
                    app.client_id != CONSOLE_AUDIENCE,
                    f"registration with {key}={CONSOLE_AUDIENCE!r} took that client id",
                )
                self.info(
                    f"registration with {key}={CONSOLE_AUDIENCE!r}: accepted under a generated id"
                )
            else:
                self.ok(
                    f"registration with {key}={CONSOLE_AUDIENCE!r} refused: "
                    f"{reply.status} ({reply.error()})"
                )
        reply, app = self.register(
            {
                "redirect_uris": [REDIRECT_URI],
                "token_endpoint_auth_method": "none",
                "scope": f"mcp {CONSOLE_AUDIENCE}",
            }
        )
        if app is not None:
            granted = str(app.metadata.get("scope", "")).split()
            check(CONSOLE_AUDIENCE not in granted, f"scope {CONSOLE_AUDIENCE!r} was granted")
            self.info(f"scope 'mcp {CONSOLE_AUDIENCE}': accepted, stripped to {granted}")
        else:
            self.ok(
                f"registration asking for scope {CONSOLE_AUDIENCE!r} refused: "
                f"{reply.status} ({reply.error()})"
            )
        reservation = self.client_by_id(CONSOLE_AUDIENCE)
        check(reservation.get("enabled") is False, "the console audience's reservation is enabled")
        self.ok(f"the {CONSOLE_AUDIENCE!r} client is the disabled reservation")

    def grant_policy(self) -> None:
        """Checklist 4. Asking for ROPC or implicit at registration, and again
        by a registration-access-token update, ends with both off, PKCE
        S256 on, and a password grant refused."""
        reply, app = self.register(
            {
                "redirect_uris": [REDIRECT_URI],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "password", "implicit"],
                "response_types": ["code", "token", "id_token token"],
            }
        )
        if app is None:
            self.ok(f"registration asking for password+implicit refused: {reply.status}")
            return
        self.assert_flows_off(app.client_id, "at registration")
        url = f"{self.registration}/{app.client_id}"
        update = {k: v for k, v in app.metadata.items() if k != "registration_access_token"}
        update["grant_types"] = ["authorization_code", "password", "implicit"]
        reply = self.browser.request(
            "PUT",
            url,
            body=update,
            headers={"Authorization": f"Bearer {app.registration_access_token}"},
        )
        if reply.status == 200:
            app.registration_access_token = str(reply.json()["registration_access_token"])
            self.assert_flows_off(app.client_id, "after a registration-access-token update")
        else:
            self.ok(f"update asking for password+implicit refused: {reply.status}")
        reply = self.browser.request(
            "POST",
            self.token_url,
            form={
                "grant_type": "password",
                "client_id": app.client_id,
                "username": self.username,
                "password": self.password,
                "scope": "mcp",
            },
        )
        check(reply.status in (400, 401), f"password grant answered {reply.status}")
        check("access_token" not in reply.text(), "password grant returned a token")
        self.ok(f"password grant refused ({reply.status} {reply.error()})")

    def assert_flows_off(self, client_id: str, when: str) -> None:
        client = self.client_by_id(client_id)
        check(client.get("directAccessGrantsEnabled") is False, f"ROPC is on {when}")
        check(client.get("implicitFlowEnabled") is False, f"implicit is on {when}")
        method = client.get("attributes", {}).get("pkce.code.challenge.method")
        check(method == "S256", f"PKCE method is {method!r} {when}")
        self.ok(f"ROPC off, implicit off, PKCE S256 {when}")

    def service_account(self) -> None:
        """Recorded, not refused here: Keycloak 26.2 cannot stop a registered
        confidential app from holding a service account (task 11 round 2).
        What it gets must not look like a person -- the api refuses it at
        /mcp on that difference."""
        reply, app = self.register(
            {
                "client_name": "ITMS CI service account",
                "token_endpoint_auth_method": "client_secret_basic",
                "grant_types": ["client_credentials"],
                "scope": "mcp",
            }
        )
        if app is None:
            self.ok(f"confidential client_credentials registration refused: {reply.status}")
            return
        self.info(f"confidential client_credentials app registered ({app.client_id})")
        reply = self.browser.request(
            "POST",
            self.token_url,
            form={"grant_type": "client_credentials", "scope": "mcp"},
            headers=_basic(app.client_id, str(app.metadata.get("client_secret", ""))),
        )
        if reply.status != 200:
            self.ok(f"client_credentials with scope mcp refused: {reply.status} ({reply.error()})")
            return
        claims = jwt_claims(str(reply.json()["access_token"]))
        self.info(f"client_credentials token claims: {describe_claims(claims)}")
        for name in ("client_id", "sid", "preferred_username", "org", "sub"):
            self.info(f"  {name}: {'present' if name in claims else 'absent'}")
        check("org" not in claims, "a service-account token carries an org claim")
        # ...and the api's positive marker: no sign-in session, so no `sid`.
        check("sid" not in claims, "a service-account token carries sid; the api's sid rule fails")
        # THE API'S MARKER, proved on a real token: `is_service_account` reads
        # `client_id` (or `clientId`), or a `service-account-` username. With
        # scope=mcp alone there is no username, so the claim is all it has.
        check(
            {"client_id", "clientId"} & set(claims)
            or str(claims.get("preferred_username", "")).startswith("service-account-"),
            "a service-account token carries no marker the api recognises",
        )
        self.ok(
            "client_credentials token: no org, and the client_id marker the api refuses at /mcp"
        )

    def our_clients(self) -> None:
        """Checklist 6: the platform's own clients still work."""
        with open(self.args.provisioning_secret_file, encoding="utf-8") as handle:
            secret = handle.read().strip()
        reply = self.browser.request(
            "POST",
            self.token_url,
            form={"grant_type": "client_credentials"},
            headers=_basic("it-suite-provisioning", secret),
        )
        check(reply.status == 200, f"it-suite-provisioning client_credentials: {reply.status}")
        claims = jwt_claims(str(reply.json()["access_token"]))
        check(claims.get("azp") == "it-suite-provisioning", "provisioning token has the wrong azp")
        check(self.args.resource not in audiences(claims), "provisioning token has the MCP aud")
        self.ok("it-suite-provisioning client_credentials still returns a token")
        # The console's own sign-in -- and the proof that the "no it-suite"
        # assertions above could fail: THIS token must carry it.
        redirect = f"{self.args.base}/auth/callback"
        tokens = self.code_flow(CONSOLE_CLIENT, scope="openid", redirect_uri=redirect)
        aud = check_console_token(jwt_claims(str(tokens["access_token"])), self.args.resource)
        self.ok(
            f"{CONSOLE_CLIENT} sign-in still works; its token has azp {CONSOLE_CLIENT!r}, "
            f"typ 'Bearer' and aud {aud} with {CONSOLE_AUDIENCE!r}"
        )

    def http_redirect_recorded(self) -> None:
        """Residual risk 1 (deploy/README.md): recorded so a change shows."""
        reply, _ = self.register(
            {
                "redirect_uris": ["http://app.example.invalid/callback"],
                "token_endpoint_auth_method": "none",
            }
        )
        self.info(
            f"http redirect to a non-loopback host: {reply.status} "
            "(accepted is the documented residual; PKCE binds the code)"
        )

    def cleanup(self) -> None:
        for app in list(self.registered):
            reply = self.browser.request(
                "DELETE",
                f"{self.registration}/{app.client_id}",
                headers={"Authorization": f"Bearer {app.registration_access_token}"},
            )
            if reply.status != 204 and self.admin_token:
                for client in self.clients_named(app.client_id):
                    self.admin("DELETE", f"/clients/{client['id']}")
        if self.edge_probe and self.admin_token:
            for client in self.clients_named(self.edge_probe):
                self.admin("DELETE", f"/clients/{client['id']}")
        if self.user_id and self.admin_token:
            self.admin("DELETE", f"/users/{self.user_id}")
        self.info(f"removed {len(self.registered)} registered apps and the test person")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True, help="the public entrypoint (Traefik)")
    parser.add_argument("--cafile", help="CA bundle that verifies --base")
    parser.add_argument("--realm", default="it-suite")
    parser.add_argument("--resource", help="the MCP resource, <ITS_PUBLIC_CONSOLE_URL>/mcp")
    parser.add_argument("--admin-base", help="Keycloak on the compose network (not Traefik)")
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-password-file")
    parser.add_argument("--provisioning-secret-file")
    parser.add_argument(
        "--expect-registration-closed",
        action="store_true",
        help="ITS_PUBLIC_CONSOLE_URL is unset: assert registration is refused, and stop",
    )
    args = parser.parse_args(argv)
    args.base = args.base.rstrip("/")
    if not args.expect_registration_closed:
        missing = [
            name
            for name in (
                "resource",
                "admin_base",
                "admin_password_file",
                "provisioning_secret_file",
            )
            if not getattr(args, name)
        ]
        if missing:
            parser.error(f"required unless --expect-registration-closed: {missing}")
        args.admin_base = args.admin_base.rstrip("/")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    run = Run(args, Browser(args.cafile))
    try:
        run.section("discovery through Traefik", run.discovery)
        if args.expect_registration_closed:
            run.section("registration closed (Trusted Hosts in place)", run.registration_closed)
        else:
            run.section("test person (admin API, compose network)", run.setup_person)
            if run.failures:
                # Everything below signs this person in.
                return _finish(run)
            run.section("reservations", run.reservations)
            run.section("the edge: only strict OIDC registration reaches Keycloak", run.edge)
            run.section("MCP sign-in: scope=mcp app, code + PKCE S256", run.mcp_sign_in)
            run.section("MCP sign-in: default-scopes app", run.default_scope_sign_in)
            run.section("RFC 7592 self-management", run.self_management)
            run.section("the console audience cannot be taken", run.console_audience_unreachable)
            run.section("client policy: no ROPC, no implicit, PKCE", run.grant_policy)
            run.section("service account (recorded)", run.service_account)
            run.section("the platform's own clients", run.our_clients)
            run.section("http redirect to a real host (recorded)", run.http_redirect_recorded)
    except (urllib.error.URLError, OSError) as exc:
        print(f"ERROR cannot reach Keycloak or Traefik: {exc}", file=sys.stderr)
        return 2
    finally:
        if not args.expect_registration_closed:
            try:
                run.cleanup()
            except (Failed, urllib.error.URLError, OSError) as exc:
                print(f"info  cleanup incomplete: {exc}")
    return _finish(run)


def _finish(run: Run) -> int:
    if run.failures:
        print(f"\n{len(run.failures)} check(s) FAILED:", file=sys.stderr)
        for failure in run.failures:
            print(f"FAIL  {failure}", file=sys.stderr)
        return 1
    print("\nall MCP identity checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
