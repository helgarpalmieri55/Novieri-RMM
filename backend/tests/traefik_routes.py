"""Traefik's router selection, read from the compose labels.

Moved here from `tests/test_deploy_keycloak_registration_edge.py` in own
authentication phase 4, when that file went with the routers it held to
Keycloak's registration edge. The model is the same: highest priority first,
first match wins, a router's default priority being its rule's length, with
Python's `re` standing in for Go's RE2 (the patterns used are in their
common subset). It proves what the RULES say for the paths given; it cannot
prove what Traefik's own normalisation does with them -- the `deploy` job in
CI sends real requests through the real `traefik` service for that.

Used by `test_public_surface.py`, `test_deploy_mcp_route.py` and
`test_auth_jwks.py`.
"""

import functools
import re
from pathlib import Path
from typing import Any

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "deploy/docker-compose.yml"

#: Every method a request to the public entrypoint is checked with.
METHODS = ("GET", "HEAD", "POST", "PUT", "DELETE")

_MATCHER = re.compile(r"(PathRegexp|PathPrefix|Path|Method)\(`([^`]*)`\)")


def compose(path: Path = COMPOSE) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text())
    return loaded


@functools.cache
def routers(path: Path = COMPOSE) -> list[tuple[int, str, str]]:
    """`(priority, name, rule)` for every websecure router in the stack,
    highest priority first."""
    found: dict[str, dict[str, str]] = {}
    for service in compose(path)["services"].values():
        labels = service.get("labels") or {}
        if isinstance(labels, list):
            labels = dict(item.split("=", 1) for item in labels)
        for key, value in labels.items():
            match = re.fullmatch(r"traefik\.http\.routers\.([^.]+)\.(\w+)", key)
            if match:
                # Compose substitutes `${VAR:-default}` before Traefik sees it.
                text = re.sub(r"\$\{[A-Z0-9_]+:-([^}]*)\}", r"\1", str(value))
                found.setdefault(match.group(1), {})[match.group(2)] = text
    return sorted(
        (
            (int(r.get("priority") or len(r["rule"])), name, r["rule"])
            for name, r in found.items()
            if r.get("entrypoints") == "websecure"
        ),
        reverse=True,
    )


def matches(rule: str, path: str, method: str) -> bool:
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


def winner(path: str, compose_file: Path = COMPOSE) -> str:
    """The router Traefik picks, which must be the same for every method."""
    winners = set()
    for method in METHODS:
        for _, name, rule in routers(compose_file):
            if matches(rule, path, method):
                winners.add(name)
                break
        else:
            winners.add("<none>")
    assert len(winners) == 1, (path, winners)
    return winners.pop()
