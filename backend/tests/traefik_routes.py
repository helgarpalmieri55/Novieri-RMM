"""Traefik's router selection, read from the compose labels and from
`deploy/traefik/dynamic.yml`.

Moved here from `tests/test_deploy_keycloak_registration_edge.py` in own
authentication phase 4, when that file went with the routers it held to
Keycloak's registration edge. The model is the same: highest priority first,
first match wins, a router's default priority being its rule's length, with
Python's `re` standing in for Go's RE2 (the patterns used are in their
common subset). It proves what the RULES say for the paths given; it cannot
prove what Traefik's own normalisation does with them -- the `deploy` job in
CI sends real requests through the real `traefik` service for that.

SINCE 2026-11-23 (design 2026-11-23-vps-y-marca-novieri §2) every router on
:443 names the host: Traefik asks Let's Encrypt for the certificate of the
name in a router's `Host()`, and a request for any other name is routed
nowhere. The model reads `Host()` too, against `PUBLIC_HOST` unless a test
asks for another, and the file provider's routers beside the labels' --
`api-unavailable`, the 503 a request for the api gets while no api
container is healthy, lives there.

Used by `test_public_surface.py`, `test_deploy_mcp_route.py`,
`test_auth_jwks.py`, `test_auth_phase4_falsifiers.py` and
`test_deploy_acme.py`.
"""

import functools
import re
from pathlib import Path
from typing import Any

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "deploy/docker-compose.yml"
DYNAMIC = COMPOSE.parent / "traefik" / "dynamic.yml"

#: The name the model's requests carry; what compose and the file provider
#: substitute for `ITS_PUBLIC_HOST`.
PUBLIC_HOST = "rmm.novieri.com"

#: Every method a request to the public entrypoint is checked with.
METHODS = ("GET", "HEAD", "POST", "PUT", "DELETE")

_MATCHER = re.compile(r"(PathRegexp|PathPrefix|Path|Method|Host)\(`([^`]*)`\)")
#: Compose substitutes `${VAR:-default}` and `${VAR:?message}` before
#: Traefik sees a label; the file provider's template, `{{ env "VAR" }}`.
_PUBLIC_HOST_VARIABLE = re.compile(
    r"\$\{ITS_PUBLIC_HOST:\?[^}]*\}|\{\{\s*env\s+\"ITS_PUBLIC_HOST\"\s*\}\}"
)


def _substitute(text: str) -> str:
    text = _PUBLIC_HOST_VARIABLE.sub(PUBLIC_HOST, text)
    return re.sub(r"\$\{[A-Z0-9_]+:-([^}]*)\}", r"\1", text)


def compose(path: Path = COMPOSE) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text())
    return loaded


def dynamic(path: Path = DYNAMIC) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text())
    return loaded


@functools.cache
def routers(path: Path = COMPOSE) -> list[tuple[int, str, str]]:
    """`(priority, name, rule)` for every websecure router in the stack --
    the compose labels' and, beside the committed compose file, the file
    provider's -- highest priority first."""
    found: dict[str, dict[str, str]] = {}
    for service in compose(path)["services"].values():
        labels = service.get("labels") or {}
        if isinstance(labels, list):
            labels = dict(item.split("=", 1) for item in labels)
        for key, value in labels.items():
            match = re.fullmatch(r"traefik\.http\.routers\.([^.]+)\.(\w+)", key)
            if match:
                found.setdefault(match.group(1), {})[match.group(2)] = _substitute(str(value))
    if path == COMPOSE:
        for name, router in (dynamic().get("http", {}).get("routers") or {}).items():
            found[name] = {
                "rule": _substitute(str(router["rule"])),
                "priority": str(router.get("priority") or ""),
                "entrypoints": ",".join(router.get("entryPoints") or []),
            }
    return sorted(
        (
            (int(r.get("priority") or len(r["rule"])), name, r["rule"])
            for name, r in found.items()
            if r.get("entrypoints") == "websecure"
        ),
        reverse=True,
    )


def matches(rule: str, path: str, method: str, host: str = PUBLIC_HOST) -> bool:
    """Evaluate a Traefik rule: host, path and method matchers, `&&`, `||`,
    `!`, parentheses. Any other matcher fails the assertion below, so a
    router using one cannot be silently misread."""

    def one(match: re.Match[str]) -> str:
        kind, arg = match.groups()
        if kind == "PathRegexp":
            hit = re.search(arg, path) is not None
        elif kind == "PathPrefix":
            hit = path.startswith(arg)
        elif kind == "Path":
            hit = path == arg
        elif kind == "Host":
            hit = host.lower() == arg.lower()
        else:
            hit = method == arg
        return f" {hit} "

    expression = _MATCHER.sub(one, rule)
    expression = expression.replace("&&", " and ").replace("||", " or ").replace("!", " not ")
    assert re.fullmatch(r"[\s()]*((True|False|and|or|not)[\s()]*)*", expression), expression
    return bool(eval(expression))  # noqa: S307 -- only True/False/and/or/not remain


def winner(
    path: str,
    compose_file: Path = COMPOSE,
    *,
    host: str = PUBLIC_HOST,
    down: tuple[str, ...] = (),
) -> str:
    """The router Traefik picks, which must be the same for every method.
    `down`: routers whose container is not healthy, which Traefik's Docker
    provider leaves out."""
    winners = set()
    for method in METHODS:
        for _, name, rule in routers(compose_file):
            if name in down:
                continue
            if matches(rule, path, method, host):
                winners.add(name)
                break
        else:
            winners.add("<none>")
    assert len(winners) == 1, (path, winners)
    return winners.pop()
