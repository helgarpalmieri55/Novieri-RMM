"""Only strict OpenID Connect dynamic client registration reaches Keycloak.

MCP spec 2026-10-09 §3.3; Keycloak task fix rounds 3 and 4. Anonymous
registration is open so any AI app can connect, but Keycloak serves several
registration providers under `/realms/<realm>/clients-registrations/<provider>`:

  * `openid-connect` refuses a registrant-chosen `client_id`
    (OIDCClientRegistrationProvider:78) and is what MCP clients use;
  * `default` takes a raw ClientRepresentation, `clientId` included
    (DefaultClientRegistrationProvider:50), so an app could name itself
    `it-suite` and get the console's audience in its ID token;
  * `install`, `saml2-entity-descriptor` (and `oid4vc` where enabled) have no
    business on the public entrypoint.

THE EDGE IS AN ALLOWLIST. Round 3 denied "every provider but openid-connect"
with a regex over clean paths, and a review found the spellings it missed:
matrix parameters in an earlier segment (`/realms;x/it-suite/...`, which
JAX-RS strips and Keycloak still routes) and case
(`.../Clients-Registrations/default`). Now one router forwards the single
strict lowercase OIDC form, a lower-priority router answers 403 for anything
else mentioning `clients-registrations` in any case and for any Keycloak-bound
path carrying `;`, a backslash, a dot segment or an encoded `;` `\\` `.` `/`
NUL, and only then does the ordinary `kc` router see the request.

WHAT THIS TEST CAN AND CANNOT PROVE. It reads the compose labels and models
Traefik's router selection (highest priority first, first match wins, a
router's default priority being its rule's length) with Python's `re`
standing in for Go's RE2 -- the patterns used are in their common subset. It
proves the RULES say what is intended for the strings below, each given both
raw and as decoded text, since Traefik decodes unreserved escapes, keeps
reserved ones encoded by default and sanitises dot segments before matching
(docs: security/request-path.md, v3.6). It cannot prove what Traefik's
normalisation or Keycloak's routing actually do with them. Task 12's live CI
step must send these same paths through Traefik to Keycloak.
"""

import functools
import ipaddress
import re
from pathlib import Path
from urllib.parse import unquote

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "deploy/docker-compose.yml"
ALLOW = "kc-registration-oidc"
DENY = "kc-registration-closed"
MIDDLEWARE = "traefik.http.middlewares.kc-registration-deny"

#: A Keycloak-generated client id, as RFC 7592 management addresses it.
CLIENT = "0b6a3c1e-2f4d-4e5a-9b8c-7d6e5f4a3b2c"
OIDC = "/realms/it-suite/clients-registrations/openid-connect"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


@functools.cache
def _routers() -> list[tuple[int, str, str]]:
    """`(priority, name, rule)` for every websecure router in the stack."""
    routers: dict[str, dict[str, str]] = {}
    for service in _compose()["services"].values():
        labels = service.get("labels") or {}
        if isinstance(labels, list):
            labels = dict(item.split("=", 1) for item in labels)
        for key, value in labels.items():
            match = re.fullmatch(r"traefik\.http\.routers\.([^.]+)\.(\w+)", key)
            if match:
                # Compose substitutes `${VAR:-default}` before Traefik sees it.
                text = re.sub(r"\$\{[A-Z0-9_]+:-([^}]*)\}", r"\1", str(value))
                routers.setdefault(match.group(1), {})[match.group(2)] = text
    return sorted(
        (
            (int(r.get("priority") or len(r["rule"])), name, r["rule"])
            for name, r in routers.items()
            if r.get("entrypoints") == "websecure"
        ),
        reverse=True,
    )


_MATCHER = re.compile(r"(PathRegexp|PathPrefix|Path|Method)\(`([^`]*)`\)")

#: Every method a registration or RFC 7592 management request can use.
METHODS = ("GET", "HEAD", "POST", "PUT", "DELETE")


def _matches(rule: str, path: str, method: str) -> bool:
    """Evaluate a Traefik rule: path and method matchers, `&&`, `||`, `!`,
    parentheses. Any other matcher fails the assertion below, so a router
    using one cannot be silently misread."""

    def one(match: re.Match[str]) -> str:
        kind, arg = match.groups()
        if kind == "PathRegexp":
            hit = re.search(arg, path) is not None
        elif kind == "PathPrefix":
            hit = path.startswith(arg)
        elif kind == "Path":
            hit = path == arg
        else:
            hit = method == arg
        return f" {hit} "

    expression = _MATCHER.sub(one, rule)
    expression = expression.replace("&&", " and ").replace("||", " or ").replace("!", " not ")
    assert re.fullmatch(r"[\s()]*((True|False|and|or|not)[\s()]*)*", expression), expression
    return bool(eval(expression))  # noqa: S307 -- only True/False/and/or/not remain


def _winner(path: str) -> str:
    """The router Traefik picks, which must be the same for every method."""
    winners = set()
    for method in METHODS:
        for _, name, rule in _routers():
            if _matches(rule, path, method):
                winners.add(name)
                break
        else:
            winners.add("<none>")
    assert len(winners) == 1, (path, winners)
    return winners.pop()


def _both_forms(path: str) -> list[str]:
    decoded = unquote(path)
    return [path] if decoded == path else [path, decoded]


def _assert_closed(path: str) -> None:
    for form in _both_forms(path):
        assert _winner(form) == DENY, (form, _winner(form))


def _assert_forwarded(path: str) -> None:
    assert _winner(path) == ALLOW, (path, _winner(path))


def test_the_strict_oidc_forms_are_forwarded() -> None:
    for path in (
        OIDC,
        f"{OIDC}/",
        f"{OIDC}/{CLIENT}",
        f"{OIDC}/{CLIENT}/",
    ):
        _assert_forwarded(path)


def test_every_other_provider_is_closed() -> None:
    for path in (
        "/realms/it-suite/clients-registrations/default",
        f"/realms/it-suite/clients-registrations/default/{CLIENT}",
        "/realms/it-suite/clients-registrations/default/it-suite",
        f"/realms/it-suite/clients-registrations/install/{CLIENT}",
        "/realms/it-suite/clients-registrations/saml2-entity-descriptor",
        "/realms/it-suite/clients-registrations/oid4vc",
        "/realms/it-suite/clients-registrations",
        "/realms/it-suite/clients-registrations/",
    ):
        _assert_closed(path)


def test_a_semicolon_in_any_segment_is_closed() -> None:
    """JAX-RS strips matrix parameters, so `;x` anywhere still reaches the
    provider it hides -- for `default` as much as for `openid-connect`."""
    for base in (f"{OIDC}/{CLIENT}", "/realms/it-suite/clients-registrations/default"):
        segments = base.split("/")[1:]
        for i in range(len(segments)):
            for marker in (";x", "%3Bx", "%3bx"):
                spelled = segments.copy()
                spelled[i] += marker
                _assert_closed("/" + "/".join(spelled))


def test_mixed_case_in_any_segment_is_closed() -> None:
    segments = f"{OIDC}/{CLIENT}".split("/")[1:]
    for i in range(len(segments)):
        for variant in (segments[i].upper(), segments[i].capitalize()):
            if variant == segments[i]:
                continue
            spelled = segments.copy()
            spelled[i] = variant
            _assert_closed("/" + "/".join(spelled))


def test_dot_segments_and_encoded_separators_are_closed() -> None:
    for path in (
        f"{OIDC}/../default",
        f"{OIDC}/%2E%2E/default",
        f"{OIDC}/%2e%2e/default",
        f"{OIDC}/./{CLIENT}",
        f"{OIDC}%2F..%2Fdefault",
        f"{OIDC}%3B/../default",
        "/realms/it-suite/clients-registrations%2Fdefault",
        "/realms/it-suite/clients-registrations/openid-connect%5C..%5Cdefault",
        f"{OIDC}\\..\\default",
        "/realms/../realms/it-suite/clients-registrations/default",
        f"{OIDC}%00",
        "/realms;x/it-suite/clients-registrations/default",
        "/realms%3Bx/it-suite/clients-registrations/default",
    ):
        _assert_closed(path)


def test_the_rest_of_keycloak_is_untouched() -> None:
    for path in (
        "/realms/it-suite/protocol/openid-connect/auth",
        "/realms/it-suite/protocol/openid-connect/token",
        "/realms/it-suite/.well-known/openid-configuration",
        "/realms/it-suite/login-actions/authenticate",
        "/resources/abc/login/itms/css/login.css",
        "/js/keycloak.js",
    ):
        assert _winner(path) == "kc", (path, _winner(path))


def test_the_allow_router_outranks_the_deny_which_outranks_keycloak() -> None:
    priority = {name: p for p, name, _ in _routers()}
    assert priority[ALLOW] > priority[DENY] > priority["kc"]


def test_the_deny_router_never_forwards() -> None:
    """Traefik has no native deny; an IP allowlist nobody can be on is one.
    `0.0.0.0/32` cannot be the source address of a TCP connection, and an IPv6
    client matches no IPv4 range, so every request gets 403. With an
    `ipStrategy` it would read a client-supplied header instead, which is why
    none may be set."""
    labels = _compose()["services"]["keycloak"]["labels"]
    assert labels[f"traefik.http.routers.{DENY}.middlewares"] == "kc-registration-deny"
    for cidr in labels[f"{MIDDLEWARE}.ipallowlist.sourcerange"].split(","):
        assert ipaddress.ip_network(cidr.strip()) == ipaddress.ip_network("0.0.0.0/32"), cidr
    assert not [k for k in labels if k.startswith(f"{MIDDLEWARE}.ipallowlist.ipstrategy")]
    assert f"traefik.http.routers.{ALLOW}.middlewares" not in labels
