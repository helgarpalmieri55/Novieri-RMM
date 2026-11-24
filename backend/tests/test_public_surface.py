"""What the public entrypoint routes, pinned.

UNTIL OWN AUTHENTICATION PHASE 4 this file guarded the identity provider's
router: Keycloak's admin console had to stay off the internet while its
sign-in pages (`/realms`, `/resources`, `/js`) were routed, and its account
console had to be closed at the realm because routing could not close it.
Keycloak is gone, and so is every router that led to it (plan 2026-11-15,
Task 6). What this file holds now is the other side of that: NOTHING on :443
reaches an identity provider -- no router, no service, no image, no label --
and the paths that used to be Keycloak's fall to the console, whose nginx
answers its own page.

Nothing here starts a proxy: it reads the committed compose file and models
Traefik's router selection (`tests/traefik_routes.py`). The `deploy` job in
CI sends the same paths through the real `traefik` (deploy/ci/mcp_flow.py,
`--phase closed`).
"""

import re

from tests.traefik_routes import COMPOSE, compose, routers, winner

#: What Keycloak's `kc` router took, with what its registration edge took.
FORMER_IDENTITY_PATHS = (
    "/realms/it-suite/.well-known/openid-configuration",
    "/realms/it-suite/protocol/openid-connect/auth",
    "/realms/it-suite/protocol/openid-connect/token",
    "/realms/it-suite/clients-registrations/openid-connect",
    "/realms/it-suite/clients-registrations/default",
    "/realms/it-suite/account",
    "/resources/abc/login/itms/css/itms.css",
    "/js/keycloak.js",
    "/admin/master/console/",
)


def test_the_compose_file_is_where_this_test_thinks_it_is() -> None:
    """Non-vacuity, first order: a moved file makes every assertion below a
    no-op on an empty list."""
    assert COMPOSE.is_file(), f"{COMPOSE} does not exist; this test measures nothing"
    assert len(routers()) >= 3, "the router model found almost nothing; it is broken"


def test_no_service_image_or_label_of_the_identity_provider_is_left() -> None:
    stack = compose()
    for name, service in stack["services"].items():
        assert "keycloak" not in name.lower(), name
        assert "keycloak" not in str(service.get("image", "")).lower(), (name, service["image"])
        labels = service.get("labels") or {}
        keys = labels if isinstance(labels, dict) else [item.split("=", 1)[0] for item in labels]
        assert not [k for k in keys if re.search(r"routers\.kc[.-]|middlewares\.kc-", k)], name
    assert "keycloak" not in COMPOSE.read_text().lower(), (
        "the compose file still names Keycloak, in a comment or anywhere else"
    )


def test_no_router_leads_to_an_identity_provider() -> None:
    """The :443 routers are the platform's own three, and no other: the api,
    the console, and object storage's presigned URLs. (The gateway's :8443 is
    a TCP router of its own entrypoint, never websecure.)"""
    names = {name for _, name, _ in routers()}
    assert names == {"api", "api-unavailable", "minio", "web"}, names


def test_the_former_identity_paths_fall_to_the_console() -> None:
    """No router names them, so the console's catch-all takes them and its
    nginx answers its own page: nothing behind :443 is asked about a realm."""
    for path in FORMER_IDENTITY_PATHS:
        assert winner(path) == "web", (path, winner(path))
