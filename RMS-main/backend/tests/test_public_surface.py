"""What the public entrypoint routes, pinned.

THE IDENTITY PROVIDER'S ADMIN CONSOLE MUST NOT BE INTERNET-REACHABLE. It can
create users, reset any password and rewrite the realm -- the whole fleet's
authority in one page, behind one password, on the same hostname as the
product. Sign-in needs `/realms`, `/resources` and `/js`; it does not need
`/admin`.

This is a guard over a Traefik label, which is the only place the decision is
expressed. Nothing here starts a proxy -- it asserts the committed rule.

WHY THE RULE LINE AND NOT THE WHOLE FILE. The comment above the label explains
why `/admin` is absent, and that explanation necessarily contains the string
`/admin`. A guard that grepped the file would be red on its own rationale, so
it would be loosened until it measured nothing. It reads the one line where
the routing decision is actually expressed.
"""

import json
import re
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[2] / "deploy/docker-compose.yml"


def _keycloak_rule() -> str:
    match = re.search(r"traefik\.http\.routers\.kc\.rule:\s*(.+)", COMPOSE.read_text())
    assert match, "the identity router rule is gone; this test is measuring nothing"
    return match.group(1)


def test_the_compose_file_is_where_this_test_thinks_it_is() -> None:
    """Non-vacuity, first order: a moved file makes every assertion below a
    no-op on an empty string, and `read_text` would be the only thing that
    complained."""
    assert COMPOSE.is_file(), f"{COMPOSE} does not exist; this test measures nothing"


def test_sign_in_is_still_routed() -> None:
    """Non-vacuity: removing `/admin` must not remove sign-in with it."""
    rule = _keycloak_rule()
    for needed in ("/realms", "/resources", "/js"):
        assert needed in rule, f"sign-in needs {needed} and the rule no longer routes it"


def test_the_admin_console_is_not_publicly_routed() -> None:
    assert "/admin" not in _keycloak_rule(), (
        "the identity provider's admin console is reachable from the internet; "
        "it can reset any password in the fleet. Reach it over an SSH tunnel "
        "to the container port instead (deploy/README.md, vendor appendix)."
    )


# --------------------------------------------------------------------------- #
# The other portal, which the rule above CANNOT close.
# --------------------------------------------------------------------------- #

REALM = Path(__file__).resolve().parents[2] / "deploy/keycloak/realm-it-suite.json"


def test_the_realm_file_is_where_this_test_thinks_it_is() -> None:
    """Non-vacuity, same first-order check as the compose file above."""
    assert REALM.is_file(), f"{REALM} does not exist; the test below measures nothing"


def test_the_account_console_is_closed_because_routing_cannot_close_it() -> None:
    """WHY THIS ASSERTION LIVES IN THE FILE ABOUT THE PUBLIC SURFACE.

    `/admin` was removable. The account console is not: it answers under
    `/realms/it-suite/account`, and `test_sign_in_is_still_routed` above is the
    proof that `/realms` has to stay -- the login pages and every OIDC endpoint
    are under it. So the question "what can a stranger reach on :443" is only
    half answered by the routing rule, and the other half is a realm switch.

    `deploy/keycloak/realm-it-suite.json` ships the built-in `account-console`
    client disabled, which makes the portal un-enterable: the browser's
    authorization request names a client the realm refuses. Enabling it again is
    one checkbox and no test outside this line would notice.

    `backend/tests/test_console_auth_realm.py` carries the rest of the realm
    reasoning -- in particular why the `account` client is declared beside it and
    left ENABLED. Do not "simplify" this by disabling that one too.
    """
    realm = json.loads(REALM.read_text())
    clients = {c["clientId"]: c for c in realm.get("clients", [])}
    console = clients.get("account-console")
    assert console is not None and console["enabled"] is False, (
        "the identity provider's self-service portal is enterable at "
        "https://<host>/realms/it-suite/account -- a second application, with "
        "its own chrome and the vendor's name on it, changing the password, MFA "
        "device and email address the ITMS profile page owns. It cannot be "
        "un-routed by prefix; it is closed at the realm or not at all."
    )
