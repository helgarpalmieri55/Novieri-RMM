"""The runbook's email-domain allowlist section, `ITS_CONSOLE_EMAIL_DOMAIN`
(spec `docs/modules/console-auth/spec.md` §3.3; the 2026-08-19 RBAC and
user-lifecycle design §3.3).

Moved, unchanged, from `tests/test_console_auth_realm.py` in own
authentication phase 4 (plan 2026-11-15, Task 6, P4-26): that file held the
Keycloak realm, its theme and its runbook sections, and went with them; the
allowlist and its section stay. Each guard reads ONE isolated section,
because `preflight` appearing SOMEWHERE in a 3,000-line file would satisfy a
whole-file grep while the section an operator actually reads said something
else entirely.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
README = REPO / "deploy/README.md"
ENV_EXAMPLE = REPO / "deploy/.env.example"
DEPS = REPO / "backend/app/api/deps.py"
DOMAIN_HEADING = "## The email-domain allowlist"


def _readme_section(heading: str) -> str:
    """One `## ` section of the runbook, from its heading to the next one."""
    text = README.read_text()
    start = text.index(heading)
    try:
        end = text.index("\n## ", start + 1)
    except ValueError:  # last section in the file
        end = len(text)
    return text[start:end]


def test_the_domain_section_documents_the_preflight_as_a_required_step() -> None:
    """THE ONLY WAY TO LEARN THE COST BEFORE PAYING IT. Turning the allowlist
    on refuses every token whose email fails the rule at all three doors,
    including the operator's own -- and the console is the thing that just
    stopped admitting them, so there is no in-console recovery."""
    section = _readme_section(DOMAIN_HEADING)
    assert "scripts/preflight_email_domain.py" in section
    assert (REPO / "scripts/preflight_email_domain.py").is_file(), (
        "the runbook names a script that must exist"
    )
    lowered = section.lower()
    assert "your own" in lowered or "own account" in lowered, (
        "the operator must be told the lockout includes THEM; a warning about "
        "'users' reads as being about other people"
    )


def test_the_domain_section_documents_the_break_glass() -> None:
    """Unset the variable and restart the api, because settings are read once
    at `create_app`. Without this sentence the recovery from a self-lockout is
    a guess made under pressure."""
    section = _readme_section(DOMAIN_HEADING)
    lowered = section.lower()
    assert "break-glass" in lowered
    assert "unset" in lowered
    assert "create_app" in section, (
        "the WHY of the restart: settings are read once at startup, so editing "
        ".env alone changes nothing"
    )
    assert "docker compose up -d api" in section


def test_the_domain_section_names_all_three_doors() -> None:
    """An allowlist enforced at one of three doors is not an allowlist (spec
    §0.3). The operator turning it on should know what it covers."""
    section = _readme_section(DOMAIN_HEADING)
    lowered = section.lower()
    assert "websocket" in lowered
    assert "tunnel" in lowered
    assert "4403" in section, (
        "the distinct WebSocket close code, so 'wrong domain' stays "
        "distinguishable from 'bad token' on the wire"
    )


def test_the_runbook_does_not_promise_an_email_refresh_the_code_does_not_do() -> None:
    """THE RUNBOOK MUST NOT INSTRUCT SOMETHING IMPOSSIBLE.

    The exit-`1` recovery used to read "sign those users in once so ITMS
    refreshes the stored address". No such refresh exists: `app/api/deps.py`
    writes `email` only in its `if user is None` branch and never updates it
    again (design §7.2, deferred to migration 0066). An operator following
    that step re-runs the preflight for ever and it keeps saying `1`.

    THIS GUARD READS THE CODE, deliberately, so it points the other way too --
    and it fired. §7.2 landed in migration `0066`, `deps.py` now refreshes the
    address on every admission, and this test went red on the commit that did
    it, which is exactly what it was for. Both branches are live: revert the
    refresh and the runbook must go back to promising nothing.

    THE RESIDUAL LAG IS STILL REAL and the runbook must keep saying so. The
    refresh happens on ADMISSION, so the stored row converges on the claim only
    when the user signs in. "Never refreshes" became "refreshes at each
    sign-in", which is a narrower window and not a closed one -- and a runbook
    claiming the two sources now agree would be the same class of overstatement
    in the opposite direction.
    """
    source = DEPS.read_text()
    refreshes = re.search(r"^\s*user\.email\s*=", source, re.MULTILINE)
    section = _readme_section(DOMAIN_HEADING)
    if refreshes is None:
        assert "so ITMS refreshes the stored address" not in section, (
            "deps.py has no email refresh, so the runbook must not tell the "
            "operator to trigger one by signing the user in"
        )
        assert "0066" in section, (
            "the recovery step must name the deferred fix (§7.2 / migration "
            "0066), or the reader cannot tell a limitation from a typo"
        )
    else:
        assert "UPDATE users SET email" not in section, (
            "deps.py refreshes users.email now, so the runbook must not still "
            "tell the operator to correct the row by hand with SQL"
        )
        assert "never refreshes" not in section, (
            "the runbook still says the stored row never refreshes; §7.2 has "
            "landed and it does, on every admission"
        )
        assert "last sign-in" in section, (
            "the refresh happens on ADMISSION, so the row converges only when "
            "the user signs in. The runbook must keep stating that residual "
            "lag -- 'they now agree' is the same overstatement the other way"
        )


def test_the_domain_section_states_the_stored_row_live_claim_divergence() -> None:
    """The preflight and the gate read DIFFERENT SOURCES, and the section that
    tells an operator to trust the preflight has to say so.

    The gate tests `claims.email` -- the live value in the token. The preflight
    reads `users.email` -- a row written once at first sign-in. Until §7.2
    lands they can disagree in both directions: a corrected address passes the
    gate while the preflight still lists it, and a stale row can read clean
    while the live claim fails. "Exit 0 means safe" is only true if the reader
    knows which of the two it measured.
    """
    section = _readme_section(DOMAIN_HEADING)
    lowered = section.lower()
    assert "what this does not achieve" in lowered, section[:200]
    outcome = section[lowered.index("what this does not achieve") :]
    assert "stored" in outcome.lower()
    assert "claim" in outcome.lower()
    assert "§7.2" in outcome or "7.2" in outcome, (
        "the divergence must be attributed to the deferred fix, so a reader "
        "can tell when it stops being true"
    )


def test_the_break_glass_explains_why_a_blank_value_is_actually_safe() -> None:
    """A FALSE REASON IS WORSE THAN NO REASON, because the next person builds
    on it.

    The section used to say "Pydantic reads an empty string as unset". It does
    not: pydantic-settings yields `""`, and `console_email_domain` is then
    `""`, not `None`. The blank-not-deleted break-glass is safe for a
    different reason entirely -- `domain_refusal` strips the base and treats an
    empty or whitespace-only one as no restriction, which is the branch that
    also absorbs a stray trailing newline. Cite the thing that actually holds,
    so that changing it goes looking for this sentence.
    """
    section = _readme_section(DOMAIN_HEADING)
    assert "Pydantic reads an empty string as unset" not in section, (
        "pydantic-settings yields '' for a blank env line, not None"
    )
    assert "domain_refusal" in section, (
        "the break-glass must name the function whose empty-base branch is "
        "what makes a blank line safe"
    )
    source = (REPO / "backend/app/core/identity/email_domain.py").read_text()
    assert "def domain_refusal(" in source, (
        "the runbook cites a function that must exist under that name"
    )


def test_the_unset_default_is_documented_as_load_bearing() -> None:
    """`None` means no restriction. ANY other default refuses every token on
    every existing deployment the moment this ships -- a fleet-wide lockout
    delivered by an upgrade. `.env.example` must ship it EMPTY."""
    env = ENV_EXAMPLE.read_text()
    assert re.search(r"^ITS_CONSOLE_EMAIL_DOMAIN=\s*$", env, re.MULTILINE), (
        "the shipped default must be empty; a value here locks out every "
        "deployment that copies .env.example"
    )
    assert "no restriction" in env.lower()
