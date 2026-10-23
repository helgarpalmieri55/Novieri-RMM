"""The email-domain allowlist rule, as a pure function.

`docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §3.1.

Every case the spec names is here, plus the two the spec leaves to the
implementer (a trailing root-label dot, and an empty configured base). No I/O,
no settings, no database — the rule is decided here so the three doors in
`test_domain_gate_doors.py` only have to prove they *call* it.
"""

import logging

import pytest

from app.core.identity import (
    ACTIVE_STATUS,
    domain_refusal,
    log_admission_refusal,
    status_refusal,
)
from app.core.identity.email_domain import is_allowed_domain


@pytest.mark.parametrize(
    ("email", "base", "expected"),
    [
        # The spec's own table.
        ("u@toeshee.com", "toeshee.com", True),
        ("u@mail.toeshee.com", "toeshee.com", True),
        ("u@eviltoeshee.com", "toeshee.com", False),  # no dot boundary
        ("u@toeshee.com.evil.com", "toeshee.com", False),  # wrong suffix
        ("u@TOESHEE.COM", "toeshee.com", True),  # case-insensitive
        ("u@toeshee.com", "TOESHEE.COM", True),  # ...on both sides
        ("", "toeshee.com", False),  # absent email claim -> fails closed
        # More than one "@" is refused rather than parsed: quoted local parts
        # are legal RFC and a parsing risk for no benefit.
        ('"a@b"@toeshee.com', "toeshee.com", False),
        ("u@v@toeshee.com", "toeshee.com", False),
        # Degenerate shapes, all closed.
        ("toeshee.com", "toeshee.com", False),  # no "@" at all
        ("u@", "toeshee.com", False),
        ("@toeshee.com", "toeshee.com", True),  # empty local part still has the domain
        # Deeper subdomains keep passing; a sibling of the base does not.
        ("u@a.b.toeshee.com", "toeshee.com", True),
        ("u@toeshee.com.au", "toeshee.com", False),
        # DECIDED HERE: a trailing root-label dot is NORMALISED AWAY, not
        # refused. "toeshee.com." and "toeshee.com" are the same DNS name, so
        # treating them differently would be an inconsistency with no security
        # value: stripping cannot admit anything the un-stripped compare would
        # not, because the label content either matches the base or does not.
        ("u@toeshee.com.", "toeshee.com", True),
        ("u@mail.toeshee.com.", "toeshee.com", True),
        ("u@toeshee.com", "toeshee.com.", True),  # ...on the base too
        ("u@eviltoeshee.com.", "toeshee.com", False),  # and it opens no hole
        # A whitespace-padded domain is NOT trimmed — no real IdP emits one and
        # refusing is the closed direction.
        ("u@toeshee.com ", "toeshee.com", False),
    ],
)
def test_is_allowed_domain(email: str, base: str, expected: bool) -> None:
    assert is_allowed_domain(email, base) is expected


def test_is_allowed_domain_is_pure() -> None:
    """No settings import, no I/O — the rule is decidable from its arguments."""
    import inspect

    import app.core.identity.email_domain as mod

    source = inspect.getsource(mod)
    for forbidden in ("from app.core.config", "import httpx", "session", "await "):
        assert forbidden not in source, f"{forbidden!r} leaked into the pure rule module"


# --------------------------------------------------------------------------- #
# domain_refusal — the rule plus the "unset means unrestricted" decision
# --------------------------------------------------------------------------- #


def test_domain_refusal_is_none_when_base_is_none() -> None:
    """The default. A gate that activates on an unset setting is a fleet-wide
    lockout on upgrade (spec §3.1: `None` is load-bearing)."""
    assert domain_refusal("anyone@anywhere.example", None) is None
    assert domain_refusal("", None) is None


def test_domain_refusal_treats_an_empty_base_as_unrestricted() -> None:
    """`ITS_CONSOLE_EMAIL_DOMAIN=` yields `""`, not `None`.

    Read as a restriction it would refuse every token in the deployment —
    exactly the lockout §3.1 says the default exists to prevent — while an
    operator who meant to configure a domain and typed nothing gets the
    behaviour they had yesterday. Unrestricted is the recoverable failure.
    """
    assert domain_refusal("u@toeshee.com", "") is None
    assert domain_refusal("u@elsewhere.com", "   ") is None


def test_domain_refusal_admits_and_refuses() -> None:
    assert domain_refusal("u@mail.toeshee.com", "toeshee.com") is None
    reason = domain_refusal("u@eviltoeshee.com", "toeshee.com")
    assert reason is not None


def test_domain_refusal_reason_does_not_echo_the_address() -> None:
    """The reason reaches an HTTP body and a WebSocket close frame. It says
    that the identity is not permitted, not who the identity is."""
    reason = domain_refusal("someone@eviltoeshee.com", "toeshee.com")
    assert reason is not None
    assert "someone" not in reason
    assert "eviltoeshee.com" not in reason


# --------------------------------------------------------------------------- #
# status_refusal
# --------------------------------------------------------------------------- #


def test_status_refusal_admits_only_active() -> None:
    assert ACTIVE_STATUS == "active"
    assert status_refusal("active") is None
    for blocked in ("disabled", "deleted"):
        assert status_refusal(blocked) is not None


def test_status_refusal_fails_closed_on_an_unknown_status() -> None:
    """Migration 0065 constrains the column to three values, but the gate does
    not depend on that constraint holding: anything that is not `active` is
    refused, so a fourth value added later is refused until someone decides
    otherwise, rather than admitted by default."""
    assert status_refusal("pending") is not None
    assert status_refusal("") is not None
    assert status_refusal("ACTIVE") is not None


# --------------------------------------------------------------------------- #
# The configured base is STRIPPED before it is used
#
# Review finding 2: `domain_refusal` stripped the base only to decide whether
# it was empty and then passed the UNSTRIPPED value to `is_allowed_domain`, so
# `ITS_CONSOLE_EMAIL_DOMAIN=" toeshee.com"` -- or, far more likely, a trailing
# newline from a here-doc or a copied `.env` line -- was non-empty, matched no
# address at all, and refused EVERY user at ALL THREE doors. A fleet-wide
# lockout from a byte nobody can see.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "base",
    [
        " toeshee.com",
        "toeshee.com ",
        "toeshee.com\n",  # what a here-doc or a copied .env line produces
        "toeshee.com\r\n",
        "\t toeshee.com \r\n",
    ],
)
def test_domain_refusal_strips_the_configured_base(base: str) -> None:
    """Whitespace around the CONFIGURED BASE is operator typing, not data.

    Note the asymmetry with `is_allowed_domain`'s own contract above, which
    does NOT trim the ADDRESS (`"u@toeshee.com "` stays refused): the address
    comes from an IdP-asserted claim, where trimming would be inventing an
    identity the token did not assert, and refusing is the closed direction.
    The base comes from a human editing an env file, where refusing everyone
    is the OPEN-ended failure -- it takes the whole deployment down and gives
    no clue why.
    """
    assert domain_refusal("u@toeshee.com", base) is None
    assert domain_refusal("u@mail.toeshee.com", base) is None


@pytest.mark.parametrize("base", [" toeshee.com", "toeshee.com\n"])
def test_stripping_the_base_opens_no_hole(base: str) -> None:
    """Stripping must not turn the near-miss into an admission: the padded base
    decides EXACTLY what the clean base decides, refusals included."""
    assert domain_refusal("u@eviltoeshee.com", base) is not None
    assert domain_refusal("u@toeshee.com.evil.com", base) is not None
    assert domain_refusal("", base) is not None


# --------------------------------------------------------------------------- #
# log_admission_refusal — the only server-side trace of a §3.3 lockout
#
# Review finding 1: this function had ZERO coverage. Deleting it outright left
# the whole reviewed set green, which made "delete the log line" the cheapest
# way to satisfy a log-site count -- deleting the one thing an operator locked
# out of their own console has to work from. Its shape is pinned here; that it
# is CALLED at each of the three doors is pinned in `test_domain_gate_doors.py`.
# --------------------------------------------------------------------------- #


REFUSAL_MESSAGE = "identity admission refused"


def _captured(fn) -> list[logging.LogRecord]:
    """Run `fn` with a handler attached to the identity logger itself.

    Its OWN logger rather than `caplog`: `create_app` calls `configure_logging`,
    which rewrites the root logger's handlers, so a root-level capture can be
    detached by wiring that has nothing to do with this assertion. A capture
    that can be silently disconnected is a capture that cannot fail.
    """
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("its.identity")
    handler = _Collect(level=logging.DEBUG)
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        fn()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    return records


def test_log_admission_refusal_carries_door_sub_email_and_reason() -> None:
    records = _captured(
        lambda: log_admission_refusal(
            "http", sub="sub-123", email="u@evil.example", reason="a reason"
        )
    )
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    # A STATIC template plus `extra` -- that is what keeps the address inside
    # the logging kernel's field-name redaction gate instead of interpolated
    # past it into the message string, where no name exists to match.
    assert record.getMessage() == REFUSAL_MESSAGE
    assert record.door == "http"  # type: ignore[attr-defined]
    assert record.sub == "sub-123"  # type: ignore[attr-defined]
    assert record.email == "u@evil.example"  # type: ignore[attr-defined]
    assert record.reason == "a reason"  # type: ignore[attr-defined]


def test_log_admission_refusal_does_not_interpolate_the_address() -> None:
    """`record.msg` is the template, not the rendered line. If the address ever
    moves into the message string it leaves the redaction gate -- which is the
    documented hole in `app/core/logging.py`, not a hypothetical one."""
    records = _captured(
        lambda: log_admission_refusal(
            "console-ws", sub="s", email="leak@example.com", reason="r"
        )
    )
    assert records[0].msg == REFUSAL_MESSAGE
    assert records[0].args in (None, ())
