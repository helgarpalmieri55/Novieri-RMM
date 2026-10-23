"""The pinned Keycloak minor is a FEATURE FLOOR, not a preference.

SMTP XOAUTH2 (`authType: token`) arrived in Keycloak 26.2. Below it the five
token keys this platform now writes into `smtpServer` are accepted by the admin
API and then ignored by the mail sender -- the realm saves, the console reports
success, and every invitation silently fails to send. There is no error to read.

So the floor is asserted here rather than left in a comment: a well-meaning
"pin back to the version we know" is exactly the change that would reintroduce
it, and this test is what stops that being a silent regression.
"""

import re
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "docker-compose.yml"

#: SMTP XOAUTH2 landed in this minor. See
#: docs/superpowers/specs/2026-09-10-microsoft-oauth-findings.md.
MINIMUM = (26, 2)


def test_the_pinned_keycloak_supports_smtp_xoauth2() -> None:
    text = COMPOSE.read_text()
    match = re.search(r"image:\s*quay\.io/keycloak/keycloak:(\d+)\.(\d+)", text)
    assert match is not None, "the keycloak service must pin an explicit x.y tag"
    pinned = (int(match.group(1)), int(match.group(2)))
    assert pinned >= MINIMUM, (
        f"keycloak is pinned at {pinned[0]}.{pinned[1]}; SMTP XOAUTH2 needs "
        f"{MINIMUM[0]}.{MINIMUM[1]} or later and fails SILENTLY below it"
    )


def test_the_pin_is_not_a_moving_tag() -> None:
    """`latest`, `26`, or no tag would let an unattended `docker compose pull`
    change the identity provider under a running fleet."""
    text = COMPOSE.read_text()
    assert "quay.io/keycloak/keycloak:latest" not in text
    assert re.search(r"image:\s*quay\.io/keycloak/keycloak:\d+\.\d+", text) is not None
