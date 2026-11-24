"""HTTPS from Let's Encrypt, asked for and renewed by Traefik (design
2026-11-23-vps-y-marca-novieri §2; operator decision B3), and the 503 the api's
paths answer while no api container is healthy (§6).

Read from the committed compose file and `traefik/dynamic.yml`, with
`tests/traefik_routes.py`'s model of Traefik's router selection. That the real
Traefik obtains a certificate this way, and answers 503, is the `deploy` job's
to show (deploy/ci/acme-pebble.yml, Task 7) and the rehearsal's (Task 12).
"""

from pathlib import Path

from tests.traefik_routes import COMPOSE, DYNAMIC, compose, dynamic, routers, winner

HOST_RULE = "Host(`${ITS_PUBLIC_HOST:?set in .env}`)"
API_PATHS = (
    "/api/v1/devices",
    "/api/v1/setup/status",
    "/enroll/v1",
    "/i/abc",
    "/mcp",
    "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
    "/.well-known/jwks.json",
    "/oauth/token",
    "/oauth/register",
    "/oauth/revoke",
)


def _traefik() -> dict:
    service: dict = compose()["services"]["traefik"]
    return service


def _labels(name: str) -> dict[str, str]:
    labels: dict[str, str] = compose()["services"][name].get("labels") or {}
    return labels


def test_traefik_asks_lets_encrypt_over_tls_alpn_on_443_and_80_stays_shut() -> None:
    command = _traefik()["command"]
    assert "--certificatesresolvers.le.acme.tlschallenge=true" in command
    assert "--certificatesresolvers.le.acme.storage=/etc/traefik/acme/acme.json" in command
    assert (
        "--certificatesresolvers.le.acme.caserver="
        "${ITS_ACME_CA_SERVER:-https://acme-v02.api.letsencrypt.org/directory}"
    ) in command
    assert any(
        flag.startswith("--certificatesresolvers.le.acme.email=${ITS_ACME_EMAIL:?")
        for flag in command
    ), "compose must refuse to start Traefik without the ACME address"
    assert not [f for f in command if "httpchallenge" in f or "dnschallenge" in f]
    assert not [f for f in command if f.endswith(".address=:80")], ":80 is not opened"
    published = sorted(str(p).rsplit(":", 1)[-1] for p in _traefik()["ports"])
    assert published == ["443", "8443"]


def test_the_certificate_lives_in_its_own_volume_and_nothing_is_copied_in() -> None:
    volumes = _traefik()["volumes"]
    assert "traefik-acme:/etc/traefik/acme" in volumes
    assert "traefik-acme" in compose()["volumes"]
    assert not [v for v in volumes if "certs" in str(v)], "the deploy/certs/ bind mount is gone"
    assert not (COMPOSE.parent / "certs").exists()
    assert "tls" not in dynamic(), "no certificate file is configured any more"
    assert _traefik()["environment"]["ITS_PUBLIC_HOST"] == "${ITS_PUBLIC_HOST:?set in .env}"


def test_every_router_on_443_names_the_host_and_takes_the_resolver() -> None:
    found = 0
    for name in ("api", "web", "minio"):
        labels = _labels(name)
        assert labels[f"traefik.http.routers.{name}.rule"].startswith(HOST_RULE + " && "), name
        assert labels[f"traefik.http.routers.{name}.tls.certresolver"] == "le", name
        assert f"traefik.http.routers.{name}.tls" not in labels, f"{name}: tls is the resolver's"
        found += 1
    router = dynamic()["http"]["routers"]["api-unavailable"]
    assert router["rule"].startswith('Host(`{{ env "ITS_PUBLIC_HOST" }}`) && (')
    assert router["tls"] == {"certResolver": "le"}
    assert {name for _p, name, _r in routers()} == {"api", "api-unavailable", "minio", "web"}
    assert found == 3


def test_a_request_for_another_name_is_routed_nowhere() -> None:
    for path in ("/", "/sign-in", "/api/v1/devices", "/mcp", "/it-suite/agent-releases/x"):
        for host in ("203.0.113.7", "itms.toeshee.com", "evil.example"):
            assert winner(path, host=host) == "<none>", (path, host)


def test_while_the_api_is_down_its_paths_answer_503_not_the_console() -> None:
    for path in API_PATHS:
        assert winner(path) == "api", path
        assert winner(path, down=("api",)) == "api-unavailable", path
    for path in ("/", "/sign-in", "/devices", "/oauth/authorize"):
        assert winner(path, down=("api",)) == "web", path
    service = dynamic()["http"]["services"]["api-unavailable"]
    assert service == {"loadBalancer": {"servers": []}}, "no server: Traefik answers 503"


def test_the_503_router_claims_exactly_the_api_routers_paths() -> None:
    rules = {name: rule for _p, name, rule in routers()}
    assert rules["api-unavailable"] == rules["api"]
    priorities = {name: priority for priority, name, _r in routers()}
    assert priorities["web"] < priorities["api-unavailable"] < priorities["api"]


def test_the_gateway_is_still_tcp_passthrough_on_8443() -> None:
    labels = _labels("gateway")
    assert labels["traefik.tcp.routers.gateway.rule"] == "HostSNI(`*`)"
    assert labels["traefik.tcp.routers.gateway.entrypoints"] == "gateway"
    assert labels["traefik.tcp.routers.gateway.tls.passthrough"] == "true"
    assert not [k for k in labels if "certresolver" in k]


def test_dynamic_yml_is_where_the_model_reads_it() -> None:
    assert DYNAMIC == Path(COMPOSE.parent / "traefik" / "dynamic.yml") and DYNAMIC.is_file()
    assert "./traefik/dynamic.yml:/etc/traefik/dynamic.yml:ro" in _traefik()["volumes"]
