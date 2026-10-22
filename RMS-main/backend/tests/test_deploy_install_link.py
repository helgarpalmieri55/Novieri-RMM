"""Deployment facts the install link depends on, asserted against the files."""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent.parent
COMPOSE = REPO / "deploy" / "docker-compose.yml"
ENV_EXAMPLE = REPO / "deploy" / ".env.example"


def _api() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]["api"]


def test_the_api_trusts_the_compose_network_by_default() -> None:
    value = _api()["environment"]["ITS_TRUSTED_PROXY_CIDRS"]
    assert value == "${ITS_TRUSTED_PROXY_CIDRS:-${ITMS_NETWORK_SUBNET:-172.19.240.0/24}}"


def test_uvicorn_is_not_told_to_rewrite_the_client_address() -> None:
    # If uvicorn trusted Traefik's header itself, request.client would already
    # be the LEFTMOST (client-supplied) entry and the check in
    # app/core/client_address.py would be comparing against a forgery.
    #
    # SAYING NOTHING IS NOT ENOUGH. uvicorn >= 0.30 turns proxy headers ON by
    # default and reads FORWARDED_ALLOW_IPS from the environment -- and the api
    # service loads `.env` wholesale -- so the command must switch them OFF.
    api = _api()
    argv = api["command"].split()
    assert "--forwarded-allow-ips" not in argv
    assert "--proxy-headers" not in argv
    assert "--no-proxy-headers" in argv
    assert "FORWARDED_ALLOW_IPS" not in api["environment"]


def test_the_image_default_command_also_switches_proxy_headers_off() -> None:
    """The Dockerfile's CMD is what runs when compose's `command` is absent
    (a hand-run container, a future compose file)."""
    dockerfile = (REPO / "deploy" / "backend.Dockerfile").read_text()
    cmd = [line for line in dockerfile.splitlines() if line.startswith("CMD ")]
    assert len(cmd) == 1
    assert '"--no-proxy-headers"' in cmd[0]
    assert "--forwarded-allow-ips" not in cmd[0]


def test_the_setting_is_documented_where_the_operator_edits() -> None:
    text = ENV_EXAMPLE.read_text()
    assert "--- Trusted proxy (install link) ---" in text
    assert "ITS_TRUSTED_PROXY_CIDRS" in text


def test_the_setting_promises_only_the_one_hop_the_code_peels() -> None:
    """Final review M5. `client_address` takes the RIGHTMOST X-Forwarded-For
    entry from a trusted peer -- one hop, Traefik's. The comment used to invite
    an operator with a further proxy in front of ITMS to trust it; with such a
    proxy the rightmost entry is that proxy, not the machine, so the network
    check would compare the wrong address."""
    section = ENV_EXAMPLE.read_text().split("--- Trusted proxy (install link) ---", 1)[1]
    section = " ".join(section.split("# ---", 1)[0].replace("#", " ").split())
    assert "only the bundled Traefik hop is supported" in section
    assert "not supported" in section and "in front of ITMS" in section
    assert "forwards to Traefik from somewhere else" not in section


def test_traefik_sends_the_install_link_to_the_api() -> None:
    labels = {k: str(v) for k, v in _api()["labels"].items()}
    rule = labels["traefik.http.routers.api.rule"]
    assert "PathPrefix(`/i/`)" in rule
    # And the console has no route that the new prefix would steal.
    routes = (REPO / "frontend" / "scripts" / "routes.mjs").read_text()
    assert '"/i/' not in routes and "'/i/" not in routes
