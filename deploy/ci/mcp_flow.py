#!/usr/bin/env python3
"""Live proof of the platform's own OAuth server and its MCP door, through
Traefik, as an AI app sees them (own authentication phase 4, plan 2026-11-15,
Task 7; design §5, §10.6).

Until phase 4 this was `deploy/keycloak/ci_mcp_flow.py`, which proved what
Keycloak 26.2 and Traefik 3.6 did with the realm's registration and audience
rules. Keycloak is gone; the authorization server is the platform's own
(phase 3), and the backend suite proves what it DECIDES. This script is the
half no unit test reaches: the real compose `traefik`, `web` and `api`
services, the api on a real OpenBao (its ES256 key never leaves it), reached
from outside on :443 with a certificate the script verifies.

TWO PHASES, because the registration switch is read when the api starts.
  --phase closed  before anyone has claimed the installation, with
                  ITS_MCP_REGISTRATION unset: the discovery documents, the
                  401 challenge, the 404 where an OpenID document would be,
                  the signing keys, registration refused, and the paths
                  Keycloak used to answer now answered by the console.
  --phase open    after the api restarted with ITS_MCP_REGISTRATION=open:
                  claim the installation with the install code, sign in,
                  enrol a TOTP, turn MCP on, then as an AI app register,
                  be approved, exchange the code with PKCE S256, call
                  `/mcp`, refresh, and see a reused refresh token, a reused
                  code, an RFC 7009 revocation and "sign out everywhere" each
                  end a grant at the next call. A token signed by anybody
                  else -- a tampered one, and one shaped like Keycloak's --
                  is refused at `/mcp`.

WHAT IT NEVER PRINTS. Tokens, codes, passwords, the install code, the TOTP
secret and recovery codes. Claims are printed by NAME, with values only for
the non-secret claims in `SHOWN_CLAIMS`.

NOTHING PASSES VACUOUSLY. Each phase names the sections it proves (`PHASES`),
each section is marked where its last assertion held, and a phase that comes
back without all of them -- an early return, a call lost to an edit -- is a
`FAIL` naming what it never reached, not "every assertion held". The list of
former Keycloak paths is refused empty before the first request.

Standard library only (the runner's `python3`, no venv). Exits 0 when every
assertion holds and every section was reached, 1 with one `FAIL` line at the
first assertion that does not hold (each step needs the ones before it) or
for the sections a phase never reached, 2 when the base cannot be reached at
all.

    python3 ci/mcp_flow.py --base https://localhost --cafile certs/fullchain.pem \\
        --phase closed
    python3 ci/mcp_flow.py --base https://localhost --cafile certs/fullchain.pem \\
        --phase open --setup-code-file "$RUNNER_TEMP/setup-code"
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import Message
from http.cookies import SimpleCookie
from typing import Any

#: A loopback callback, the form desktop MCP clients register; PKCE binds
#: the code to the verifier, so whatever listens there cannot redeem it.
REDIRECT_URI = "http://127.0.0.1:33418/callback"
#: The MCP revision the backend's own tests speak (`mcp.types.
#: LATEST_PROTOCOL_VERSION`; `backend/tests/test_ci_mcp_flow.py` holds the two
#: equal).
PROTOCOL_VERSION = "2026-07-28"
SESSION_COOKIE = "__Host-itms_session"
#: Claims whose VALUES are safe to print. Everything else is printed by name.
SHOWN_CLAIMS = frozenset({"aud", "iss", "scope", "client_id", "typ"})
#: Paths Keycloak's router (`kc`) answered until phase 4. Nothing routes them
#: to an identity provider any more: they fall to the console, whose nginx
#: answers its own page.
FORMER_KEYCLOAK_PATHS = (
    "/realms/it-suite/.well-known/openid-configuration",
    "/realms/it-suite/protocol/openid-connect/auth",
    "/realms/it-suite/clients-registrations/openid-connect",
    "/resources/x/login/itms/css/itms.css",
    "/js/keycloak.js",
    "/admin/master/console/",
)
#: What each phase has to have proven before it may say "every assertion
#: held". `Flow.done` marks a section where its last assertion holds; `main`
#: fails a phase that came back with any of these unmarked.
#: `backend/tests/test_ci_mcp_flow.py` holds both lists and every mark.
PHASES: dict[str, tuple[str, ...]] = {
    "closed": (
        "discovery",
        "registration refused",
        "the former Keycloak paths fall to the console",
    ),
    "open": (
        "signed in with a fresh factor",
        "MCP turned on",
        "discovery",
        "dynamic registration",
        "consent",
        "code exchange with PKCE S256",
        "/mcp",
        "a tampered token refused at /mcp",
        "a Keycloak-shaped token refused at /mcp",
        "a Keycloak-shaped token refused at the console",
        "refresh",
        "refresh reuse ends the grant",
        "code reuse ends the grant",
        "revocation ends the grant",
        "signing out everywhere ends the grant",
    ),
}
#: The access token's lifetime (design §5.3: ten minutes).
ACCESS_TOKEN_S = 600
TIMEOUT_S = 30


class Failed(Exception):
    """One assertion that did not hold. The message says what was expected."""


class Unreachable(Exception):
    """The base did not answer at all."""


def check(condition: object, message: str) -> None:
    if not condition:
        raise Failed(message)


# --------------------------------------------------------------------------- #
# Pure helpers (backend/tests/test_ci_mcp_flow.py)
# --------------------------------------------------------------------------- #


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def s256(verifier: str) -> str:
    """RFC 7636 §4.2: BASE64URL(SHA256(ASCII(code_verifier))), unpadded."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def pkce_pair() -> tuple[str, str]:
    """`(verifier, S256 challenge)`. 48 random bytes make a 64-character
    verifier, inside RFC 7636 §4.1's 43-128."""
    verifier = _b64url(secrets.token_bytes(48))
    return verifier, s256(verifier)


def jwt_parts(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """`(header, claims)` of a JWT, NOT verified: CI asserts on what the
    server put in it; verifying is the api's, and the backend suite's."""
    parts = token.split(".")
    if len(parts) != 3:
        raise Failed("the token is not a three-part JWT")
    try:
        header, claims = json.loads(_unb64url(parts[0])), json.loads(_unb64url(parts[1]))
    except ValueError as exc:
        raise Failed("the token's header or payload is not base64url JSON") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise Failed("the token's header or payload is not a JSON object")
    return header, claims


def audiences(claims: Mapping[str, Any]) -> list[str]:
    aud = claims.get("aud")
    if aud is None:
        return []
    return [aud] if isinstance(aud, str) else [str(a) for a in aud]


def exact_audience(claims: Mapping[str, Any], resource: str) -> bool:
    """The audience is the MCP resource, as a string, and nothing else
    (design §5.3; 1c(a): "carrying the MCP audience and no other platform
    audience"). A list holding it is refused by the api too."""
    return claims.get("aud") == resource


def totp(secret: str, at: float | None = None, *, step: int = 30, digits: int = 6) -> str:
    """RFC 6238 over RFC 4226, HMAC-SHA1: what an authenticator app shows."""
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    counter = int((time.time() if at is None else at) // step)
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{value % 10**digits:0{digits}d}"


def totp_secret(otpauth_uri: str) -> str:
    """The `secret` of an `otpauth://totp/...` URI."""
    parts = urllib.parse.urlsplit(otpauth_uri)
    check(parts.scheme == "otpauth" and parts.netloc == "totp", "not an otpauth://totp URI")
    secret = urllib.parse.parse_qs(parts.query).get("secret", [""])[0]
    check(secret, "the otpauth URI carries no secret")
    return secret


def challenge_params(header: str) -> dict[str, str]:
    """The parameters of a `WWW-Authenticate: Bearer k="v", ...` challenge."""
    scheme, _, rest = header.partition(" ")
    check(scheme == "Bearer", f"the challenge's scheme is {scheme!r}, not Bearer")
    params: dict[str, str] = {}
    for item in rest.split(","):
        key, sep, value = item.strip().partition("=")
        if sep:
            params[key.strip()] = value.strip().strip('"')
    return params


def authorize_query(client_id: str, challenge: str, state: str, resource: str) -> str:
    """The query of `/oauth/authorize` as an MCP client sends it."""
    return urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "resource": resource,
            "scope": "mcp",
        }
    )


def callback_params(location: str, redirect_uri: str = REDIRECT_URI) -> dict[str, str]:
    """The query of a redirect to `redirect_uri` -- same scheme, host, port
    and path, not a string prefix, which `.../callback.evil` would pass."""
    got, want = urllib.parse.urlsplit(location), urllib.parse.urlsplit(redirect_uri)
    if (got.scheme, got.netloc, got.path) != (want.scheme, want.netloc, want.path):
        raise Failed(f"redirected to {location.split('?')[0]!r}, not the registered callback")
    return {k: v[0] for k, v in urllib.parse.parse_qs(got.query).items()}


def tampered(token: str) -> str:
    """The same token with one byte of its signature changed."""
    head, payload, signature = token.split(".")
    raw = bytearray(_unb64url(signature))
    raw[0] ^= 0x01
    return f"{head}.{payload}.{_b64url(bytes(raw))}"


def foreign_token(base: str, sub: str) -> str:
    """An access token shaped exactly like Keycloak's for the console --
    `iss` the realm that used to live at `base`, `aud` `it-suite`, `azp` the
    console's SPA, `typ` Bearer, `org` -- signed with a key nobody has. The
    platform trusts no key but its own, so `/mcp` refuses it."""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT", "kid": "keycloak-ci"}
    claims = {
        "iss": base.rstrip("/") + "/realms/it-suite",
        "aud": "it-suite",
        "azp": "it-suite-web",
        "typ": "Bearer",
        "sub": sub,
        "org": "toeshee",
        "iat": now,
        "exp": now + 300,
    }
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}"
    signature = hmac.new(secrets.token_bytes(32), signing_input.encode(), hashlib.sha256)
    return f"{signing_input}.{_b64url(signature.digest())}"


def describe_claims(claims: Mapping[str, Any]) -> str:
    """Claim names, with values only for `SHOWN_CLAIMS`. Safe to print."""
    shown = []
    for name in sorted(claims):
        if name in SHOWN_CLAIMS:
            shown.append(f"{name}={json.dumps(claims[name])}")
        else:
            shown.append(name)
    return " ".join(shown)


class Cookies:
    """The cookies one host set, by name. Kept by hand rather than by
    `http.cookiejar`, whose policy for a dotless host (`localhost`) and for
    `__Host-` names is not this script's to second-guess: the api sets one
    host's cookies and this sends them back to that host."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def take(self, headers: Message) -> None:
        """Every `Set-Cookie` of a response; `Max-Age=0` or an empty value
        clears the name."""
        for line in headers.get_all("Set-Cookie") or []:
            jar: SimpleCookie = SimpleCookie()
            jar.load(line)
            for name, morsel in jar.items():
                if morsel.value == "" or morsel["max-age"] == "0":
                    self.values.pop(name, None)
                else:
                    self.values[name] = morsel.value

    def header(self) -> str | None:
        if not self.values:
            return None
        return "; ".join(f"{name}={value}" for name, value in sorted(self.values.items()))


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


@dataclass
class Response:
    path: str
    status: int
    headers: Message
    body: bytes

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except ValueError as exc:
            raise Failed(
                f"{self.path} answered {self.status} with a body that is not JSON"
            ) from exc

    def media(self) -> str:
        return (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is a result here, not something to follow."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class Client:
    """One host, its cookies, no redirects followed, no proxy, TLS verified."""

    def __init__(self, base: str, cafile: str | None) -> None:
        self.base = base.rstrip("/")
        self.cookies = Cookies()
        context = ssl.create_default_context(cafile=cafile)
        self._opener = urllib.request.build_opener(
            # Everything this talks to is the runner itself; an inherited
            # HTTPS_PROXY would send the requests somewhere else entirely.
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
            _NoRedirect(),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        form: Mapping[str, str] | None = None,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        console: bool = False,
        bearer: str | None = None,
        cookies: bool = True,
    ) -> Response:
        """`console`: the two headers the console's own client adds to every
        write (`X-ITMS-Request: 1`, and its `Origin`), without which the
        api's CSRF rule refuses it. `cookies`: send this host's cookies."""
        data = None
        sent = {"Accept": "application/json"}
        sent.update(headers or {})
        if console:
            sent.setdefault("X-ITMS-Request", "1")
            sent.setdefault("Origin", self.base)
        if bearer is not None:
            sent["Authorization"] = f"Bearer {bearer}"
        jar = self.cookies.header() if cookies else None
        if jar:
            sent["Cookie"] = jar
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            sent["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            sent["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, headers=sent, method=method)
        try:
            with self._opener.open(request, timeout=TIMEOUT_S) as reply:
                answer = Response(path.split("?")[0], reply.status, reply.headers, reply.read())
        except urllib.error.HTTPError as exc:
            answer = Response(path.split("?")[0], exc.code, exc.headers, exc.read())
        except (urllib.error.URLError, OSError) as exc:
            raise Unreachable(f"{self.base}{path.split('?')[0]}: {exc}") from exc
        if cookies:
            self.cookies.take(answer.headers)
        return answer


def ok(message: str) -> None:
    print(f"ok    {message}")


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #


@dataclass
class Grant:
    client_id: str
    access: str
    refresh: str


class Flow:
    def __init__(self, client: Client) -> None:
        self.http = client
        self.base = client.base
        self.resource = self.base + "/mcp"
        self.kids: set[str] = set()
        self.reached: set[str] = set()

    def done(self, section: str) -> None:
        """`section` of `PHASES` held: every assertion before this line did."""
        self.reached.add(section)

    # -- the parts both phases share ----------------------------------------- #

    def mcp(self, token: str | None) -> Response:
        """`initialize`, the first call any MCP client makes."""
        return self.http.request(
            "POST",
            "/mcp",
            body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "itms-ci", "version": "1"},
                },
            },
            headers={"Accept": "application/json, text/event-stream"},
            bearer=token,
            cookies=False,
        )

    def refused_at_mcp(self, token: str, what: str) -> None:
        answer = self.mcp(token)
        check(answer.status == 401, f"/mcp answered {answer.status} to {what}, not 401")
        params = challenge_params(answer.headers.get("WWW-Authenticate", ""))
        check(params.get("error") == "invalid_token", f"{what}: the 401 does not say invalid_token")
        ok(f"/mcp refuses {what}: 401 invalid_token")

    def refused_at_the_console(self, token: str, what: str) -> None:
        """The console's API door, with no cookie: it reads a session cookie
        only, and since own authentication phase 4 nothing on the platform can
        validate a Keycloak token at all."""
        answer = self.http.request("GET", "/api/v1/me", bearer=token, cookies=False)
        check(answer.status == 401, f"/api/v1/me answered {answer.status} to {what}, not 401")
        ok(f"/api/v1/me refuses {what}: 401")

    def discovery(self, *, registration: bool) -> dict[str, Any]:
        answer = self.mcp(None)
        check(answer.status == 401, f"/mcp without a token answered {answer.status}, not 401")
        metadata_url = self.base + "/.well-known/oauth-protected-resource/mcp"
        challenge = answer.headers.get("WWW-Authenticate", "")
        check(
            challenge == f'Bearer resource_metadata="{metadata_url}"',
            "the 401's challenge is not exactly Bearer resource_metadata=<the PRM URL>",
        )
        ok("/mcp without a token: 401 naming the protected-resource metadata")

        prm = self.http.request("GET", "/.well-known/oauth-protected-resource/mcp", cookies=False)
        check(prm.status == 200, f"the protected-resource metadata answered {prm.status}")
        document = prm.json()
        check(document.get("resource") == self.resource, "PRM: resource is not <base>/mcp")
        check(document.get("authorization_servers") == [self.base], "PRM: the server is not <base>")
        ok("RFC 9728 metadata: this resource, this authorization server")

        meta = self.http.request("GET", "/.well-known/oauth-authorization-server", cookies=False)
        check(meta.status == 200, f"the RFC 8414 document answered {meta.status}")
        server = meta.json()
        if not isinstance(server, dict):
            raise Failed("the RFC 8414 document is not a JSON object")
        expected = {
            "issuer": self.base,
            "authorization_endpoint": self.base + "/oauth/authorize",
            "token_endpoint": self.base + "/oauth/token",
            "revocation_endpoint": self.base + "/oauth/revoke",
            "jwks_uri": self.base + "/.well-known/jwks.json",
        }
        for key, value in expected.items():
            check(server.get(key) == value, f"RFC 8414: {key} is {server.get(key)!r}")
        check(server.get("code_challenge_methods_supported") == ["S256"], "RFC 8414: not S256 only")
        check(
            server.get("token_endpoint_auth_methods_supported") == ["none"],
            "RFC 8414: token endpoint auth is not `none` only",
        )
        check(server.get("client_id_metadata_document_supported") is True, "RFC 8414: no CIMD")
        check(
            server.get("authorization_response_iss_parameter_supported") is True,
            "RFC 8414: no RFC 9207 iss",
        )
        if not registration:
            check("registration_endpoint" not in server, "registration is offered while closed")
        ok("RFC 8414 document: issuer, endpoints, S256, public clients, CIMD, iss")

        oidc = self.http.request("GET", "/.well-known/openid-configuration", cookies=False)
        check(oidc.status == 404, f"/.well-known/openid-configuration answered {oidc.status}")
        check(oidc.media() == "application/json", "the OpenID path is not the api's JSON 404")
        ok("no OpenID document: the api's JSON 404, not the console's page")

        keys = self.http.request("GET", "/.well-known/jwks.json", cookies=False)
        check(keys.status == 200, f"the JWKS answered {keys.status}")
        listed = keys.json().get("keys", [])
        check(listed, "the JWKS lists no key")
        for key in listed:
            check(key.get("kty") == "EC" and key.get("crv") == "P-256", "a JWKS key is not P-256")
            check("d" not in key, "the JWKS carries a private key")
            self.kids.add(str(key.get("kid")))
        ok(f"JWKS: {len(listed)} public P-256 key(s)")
        self.done("discovery")
        return server

    # -- phase closed ---------------------------------------------------------- #

    def closed(self) -> None:
        check(FORMER_KEYCLOAK_PATHS, "there is no former Keycloak path to check")
        self.discovery(registration=False)
        refused = self.http.request(
            "POST", "/oauth/register", body={"redirect_uris": [REDIRECT_URI]}, cookies=False
        )
        check(refused.status == 403, f"registration answered {refused.status} while closed")
        check(
            refused.json().get("error") == "access_denied", "registration closed: not access_denied"
        )
        ok("registration closed: 403 access_denied")
        self.done("registration refused")
        for path in FORMER_KEYCLOAK_PATHS:
            page = self.http.request("GET", path, headers={"Accept": "text/html"}, cookies=False)
            check(page.status == 200, f"{path} answered {page.status}, not the console's 200")
            check(page.media() == "text/html", f"{path} is not the console's page")
            check("keycloak" not in page.text().lower(), f"{path}'s page names the old provider")
        ok(f"the {len(FORMER_KEYCLOAK_PATHS)} paths Keycloak answered: the console's page")
        self.done("the former Keycloak paths fall to the console")

    # -- phase open ------------------------------------------------------------ #

    def sign_in(self, setup_code: str) -> None:
        status = self.http.request("GET", "/api/v1/setup/status")
        check(status.status == 200 and status.json() == {"needed": True}, "setup is not needed")
        email = "owner@ci.invalid"
        password = "ci-" + secrets.token_urlsafe(24)
        claim = self.http.request(
            "POST",
            "/api/v1/setup/claim",
            body={
                "code": setup_code,
                "email": email,
                "display_name": "CI Owner",
                "organization_name": "CI",
                "password": password,
            },
            console=True,
        )
        check(claim.status == 201, f"the setup claim answered {claim.status}")
        ok("the installation is claimed with the install code")

        forged = self.http.request(
            "POST",
            "/api/v1/auth/login",
            body={"email": email, "password": password},
            headers={"Origin": self.base},
        )
        check(forged.status == 403, f"a sign-in without X-ITMS-Request answered {forged.status}")
        check(forged.json().get("code") == "csrf", "the refusal is not the CSRF rule's")
        check(SESSION_COOKIE not in self.http.cookies.values, "a refused sign-in set a session")
        ok("a sign-in without the console's header: 403 csrf, no session")

        login = self.http.request(
            "POST", "/api/v1/auth/login", body={"email": email, "password": password}, console=True
        )
        check(login.status == 200, f"the sign-in answered {login.status}")
        check(
            login.json() == {"state": "enroll_required"}, "the first sign-in is not enroll_required"
        )
        check(SESSION_COOKIE in self.http.cookies.values, "the sign-in set no session cookie")
        start = self.http.request("POST", "/api/v1/auth/enroll/totp/start", console=True)
        check(start.status == 200, f"TOTP start answered {start.status}")
        secret = totp_secret(start.json()["otpauth_uri"])
        confirm = self.http.request(
            "POST", "/api/v1/auth/enroll/totp/confirm", body={"code": totp(secret)}, console=True
        )
        check(confirm.status == 204, f"TOTP confirm answered {confirm.status}")
        finish = self.http.request("POST", "/api/v1/auth/enroll/finish", console=True)
        check(finish.status == 200, f"enrolment finish answered {finish.status}")
        check(finish.json().get("recovery_codes"), "enrolment returned no recovery codes")
        ok("signed in: password, a TOTP enrolled, recovery codes issued")
        self.done("signed in with a fresh factor")

        switch = self.http.request(
            "PATCH", "/api/v1/orgs/current", body={"settings": {"mcp_enabled": True}}, console=True
        )
        check(switch.status == 200, f"turning MCP on answered {switch.status}")
        ok("MCP is on for the organization")
        self.done("MCP turned on")

    def register(self, wait_s: float) -> str:
        """The metadata offers registration once the switch is read; the api
        keeps that answer for up to 30 s (`clients.OFFERED_FOR_S`)."""
        self.discovery(registration=True)
        deadline = time.monotonic() + wait_s
        while True:
            meta = self.http.request(
                "GET", "/.well-known/oauth-authorization-server", cookies=False
            )
            check(meta.status == 200, f"the RFC 8414 document answered {meta.status}")
            if meta.json().get("registration_endpoint") == self.base + "/oauth/register":
                break
            check(time.monotonic() < deadline, "RFC 8414 never offered registration")
            time.sleep(5)
        ok("RFC 8414 offers registration: opted in, and MCP is on")
        answer = self.http.request(
            "POST",
            "/oauth/register",
            body={
                "client_name": "ITMS CI",
                "redirect_uris": [REDIRECT_URI],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
            cookies=False,
        )
        check(answer.status == 201, f"registration answered {answer.status}")
        registered = answer.json()
        check(registered.get("token_endpoint_auth_method") == "none", "not a public client")
        check("client_secret" not in registered, "a public client was given a secret")
        ok("registered by RFC 7591, as a public client")
        self.done("dynamic registration")
        return str(registered["client_id"])

    def approve(self, client_id: str) -> tuple[str, str]:
        """The person approves: `(code, verifier)`. Approving needs a recent
        second factor (`require_step_up`, ten minutes: `STEP_UP_WINDOW` in
        `app/core/auth/sessions.py`); the enrolment at sign-in gave it, and
        the four approvals of `open` all fall inside that window."""
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(12)
        query = authorize_query(client_id, challenge, state, self.resource)
        page = self.http.request(
            "GET", "/oauth/authorize?" + query, headers={"Accept": "text/html"}
        )
        check(page.status == 200 and page.media() == "text/html", "the consent page is not served")
        details = self.http.request(
            "POST", "/api/v1/oauth/authorize/details", body={"query": query}, console=True
        )
        check(details.status == 200, f"the consent details answered {details.status}")
        asked = details.json()
        check(asked.get("outcome") == "consent", f"the consent outcome is {asked.get('outcome')!r}")
        check(asked.get("scope") == "mcp", "the consent does not ask for `mcp`")
        app = asked.get("client") or {}
        check(app.get("loopback_only") is True, "the app is not shown as loopback-only")
        check(
            app.get("redirect_host") == "127.0.0.1", "the consent does not name the redirect host"
        )
        decision = self.http.request(
            "POST",
            "/api/v1/oauth/authorize/decision",
            body={"query": query, "approve": True},
            console=True,
        )
        check(decision.status == 200, f"the approval answered {decision.status}")
        answered = decision.json()
        check(
            answered.get("outcome") == "redirect",
            f"the approval outcome is {answered.get('outcome')!r}",
        )
        params = callback_params(str(answered.get("redirect_to")))
        check(params.get("state") == state, "the callback's state is not the request's")
        check(params.get("iss") == self.base, "the callback carries no RFC 9207 iss")
        check(params.get("code"), "the callback carries no code")
        self.done("consent")
        return params["code"], verifier

    def token(self, form: Mapping[str, str]) -> Response:
        return self.http.request(
            "POST", "/oauth/token", form={**form, "resource": self.resource}, cookies=False
        )

    def exchange(self, client_id: str, code: str, verifier: str) -> Grant:
        answer = self.token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
            }
        )
        grant = self.issued(answer, client_id, "the code exchange")
        self.done("code exchange with PKCE S256")
        return grant

    def issued(self, answer: Response, client_id: str, what: str) -> Grant:
        check(answer.status == 200, f"{what} answered {answer.status}")
        check("no-store" in (answer.headers.get("Cache-Control") or ""), f"{what}: not no-store")
        body = answer.json()
        check(body.get("token_type") == "Bearer", f"{what}: token_type is not Bearer")
        check(
            body.get("expires_in") == ACCESS_TOKEN_S, f"{what}: expires_in is not {ACCESS_TOKEN_S}"
        )
        check(body.get("scope") == "mcp", f"{what}: scope is not mcp")
        header, claims = jwt_parts(str(body.get("access_token")))
        check(header.get("alg") == "ES256", f"{what}: the access token is not ES256")
        check(header.get("typ") == "at+jwt", f"{what}: the access token's typ is not at+jwt")
        check(str(header.get("kid")) in self.kids, f"{what}: the token's kid is not in the JWKS")
        check(exact_audience(claims, self.resource), f"{what}: the audience is not exactly /mcp")
        check(claims.get("iss") == self.base, f"{what}: the issuer is not <base>")
        ok(f"{what}: ES256 at+jwt from the JWKS, {describe_claims(claims)}")
        return Grant(client_id, str(body["access_token"]), str(body["refresh_token"]))

    def reaches_mcp(self, grant: Grant, what: str) -> None:
        answer = self.mcp(grant.access)
        check(answer.status == 200, f"/mcp answered {answer.status} to {what}")
        check(b"protocolVersion" in answer.body, f"/mcp's answer to {what} is not initialize's")
        ok(f"/mcp answers {what}")
        self.done("/mcp")

    def open(self, setup_code: str, wait_s: float) -> None:
        self.sign_in(setup_code)
        client_id = self.register(wait_s)

        # One grant through every turn of its life.
        code, verifier = self.approve(client_id)
        first = self.exchange(client_id, code, verifier)
        self.reaches_mcp(first, "the access token")
        self.refused_at_mcp(tampered(first.access), "a token with a changed signature")
        self.done("a tampered token refused at /mcp")
        keycloak_shaped = foreign_token(self.base, "ci-owner")
        self.refused_at_mcp(keycloak_shaped, "a token shaped like Keycloak's")
        self.done("a Keycloak-shaped token refused at /mcp")
        self.refused_at_the_console(keycloak_shaped, "a token shaped like Keycloak's")
        self.done("a Keycloak-shaped token refused at the console")
        rotated = self.issued(
            self.token(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": first.refresh,
                    "client_id": client_id,
                }
            ),
            client_id,
            "the refresh",
        )
        check(rotated.refresh != first.refresh, "the refresh token did not rotate")
        self.reaches_mcp(rotated, "the refreshed access token")
        self.done("refresh")
        reused = self.token(
            {"grant_type": "refresh_token", "refresh_token": first.refresh, "client_id": client_id}
        )
        check(reused.status == 400, f"a reused refresh token answered {reused.status}")
        check(
            reused.json()
            == {"error": "invalid_grant", "error_description": "the refresh token is not valid"},
            "a reused refresh token is not invalid_grant",
        )
        self.refused_at_mcp(rotated.access, "the access token of a grant whose refresh was reused")
        self.done("refresh reuse ends the grant")

        # A code presented twice ends the grant it opened.
        code, verifier = self.approve(client_id)
        second = self.exchange(client_id, code, verifier)
        again = self.token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
            }
        )
        check(again.status == 400, f"a reused code answered {again.status}")
        check(
            again.json()
            == {
                "error": "invalid_grant",
                "error_description": "the authorization code is not valid",
            },
            "a reused code is not invalid_grant",
        )
        self.refused_at_mcp(second.access, "the access token of a grant whose code was reused")
        self.done("code reuse ends the grant")

        # RFC 7009, by the app itself.
        code, verifier = self.approve(client_id)
        third = self.exchange(client_id, code, verifier)
        revoked = self.http.request(
            "POST",
            "/oauth/revoke",
            form={"token": third.refresh, "client_id": client_id},
            cookies=False,
        )
        check(revoked.status == 200, f"the RFC 7009 revocation answered {revoked.status}")
        self.refused_at_mcp(third.access, "the access token of a grant the app revoked")
        self.done("revocation ends the grant")

        # The person signs out everywhere: their AI grants end with their sessions.
        code, verifier = self.approve(client_id)
        fourth = self.exchange(client_id, code, verifier)
        self.reaches_mcp(fourth, "a fresh grant's access token")
        out = self.http.request("POST", "/api/v1/auth/logout-everywhere", console=True)
        check(out.status == 204, f"sign out everywhere answered {out.status}")
        self.refused_at_mcp(fourth.access, "the access token of a person who signed out everywhere")
        self.done("signing out everywhere ends the grant")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--base", required=True, help="the public entrypoint, e.g. https://localhost"
    )
    parser.add_argument("--cafile", help="the certificate the entrypoint presents")
    parser.add_argument("--phase", choices=("closed", "open"), required=True)
    parser.add_argument("--setup-code-file", help="the install code (phase open)")
    parser.add_argument("--wait-registration-s", type=float, default=45.0)
    args = parser.parse_args(argv)
    if args.phase == "open" and not args.setup_code_file:
        parser.error("--phase open needs --setup-code-file")
    flow = Flow(Client(args.base, args.cafile))
    run: Callable[[], None]
    if args.phase == "closed":
        run = flow.closed
    else:
        with open(args.setup_code_file, encoding="utf-8") as handle:
            code = handle.read().strip()
        run = lambda: flow.open(code, args.wait_registration_s)  # noqa: E731
    try:
        run()
    except Failed as exc:
        print(f"FAIL  {exc}")
        return 1
    except Unreachable as exc:
        print(f"UNREACHABLE  {exc}", file=sys.stderr)
        return 2
    missing = [section for section in PHASES[args.phase] if section not in flow.reached]
    if missing:
        print(f"FAIL  phase {args.phase} never reached: {'; '.join(missing)}")
        return 1
    print(f"mcp_flow: phase {args.phase}: every assertion held")
    return 0


if __name__ == "__main__":
    sys.exit(main())
