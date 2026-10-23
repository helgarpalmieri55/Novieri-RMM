"""The email-domain allowlist rule.

`docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §3.1.

An address is admitted iff its domain equals the configured base domain or
ends with ``"." + base``, both lowercased. ``mail.toeshee.com`` passes against
``toeshee.com``; ``eviltoeshee.com`` does not (no dot boundary); and
``toeshee.com.evil.com`` does not (wrong suffix, not a suffix of the wrong
thing).

DELIBERATELY PURE. No configuration import, no I/O, nothing to stub. The rule
is the part that is easy to get subtly wrong -- a naive ``endswith(base)``
admits ``eviltoeshee.com`` -- so it is decided in one place, exhaustively
tested on its own, and the three doors that enforce it only have to prove they
call it. ``tests/test_email_domain.py`` asserts this module's source stays free
of configuration and I/O, so the separation cannot rot quietly.

WHAT THIS RESTRICTS, AND WHAT IT DOES NOT. The rule keys on the ``email``
claim, which is asserted by the IdP. It decides which identities *this
platform* admits. It is not, and cannot be, a control over which identities
the IdP will authenticate.
"""

from app.core.i18n import N_

# A refusal reason travels into an HTTP 403 body and into a WebSocket close
# frame, both of which reach an unauthenticated-in-practice reader. It names
# the rule, never the address that failed it.
_DOMAIN_REFUSED = N_("email domain not permitted for this console")


def is_allowed_domain(email: str, base: str) -> bool:
    """True iff ``email``'s domain is ``base`` or a subdomain of it.

    Refuses rather than parses anything with other than exactly one ``@``:
    quoted local parts (``"a@b"@example.com``) are legal RFC 5321 and carrying
    a second parser for them buys nothing here. An absent ``email`` claim
    arrives as ``""`` (``app/core/security.py``), which has no ``@`` and so
    fails closed.

    A trailing root-label dot is normalised away on BOTH sides before
    comparison: ``toeshee.com.`` and ``toeshee.com`` are the same DNS name.
    Normalising cannot admit anything the un-normalised comparison would not,
    because the labels either match the base or they do not -- ``eviltoeshee.com.``
    still fails.
    """
    if email.count("@") != 1:
        return False
    domain = email.rpartition("@")[2].lower().rstrip(".")
    root = base.lower().rstrip(".")
    if not domain or not root:
        return False
    return domain == root or domain.endswith("." + root)


def domain_refusal(email: str, base: str | None) -> str | None:
    """``None`` when the address is admitted, otherwise the refusal reason.

    ``base is None`` -- the shipped default -- means NO RESTRICTION, and that
    is load-bearing rather than lazy: any other reading refuses every token on
    every existing deployment the moment this ships. A security control that
    arrives switched on by surprise is a fleet-wide outage.

    An EMPTY or whitespace-only base is read the same way. ``ITS_CONSOLE_EMAIL_DOMAIN=``
    in an env file yields ``""``, not ``None``; read as a restriction it matches
    no address at all and locks out the entire deployment including the
    operator, which is the one failure §3.3 exists to prevent. Unrestricted is
    the recoverable direction, and the preflight is how an operator learns the
    variable did not take effect.

    THE BASE IS STRIPPED ONCE AND THE STRIPPED VALUE DECIDES BOTH QUESTIONS.
    An earlier version stripped only to test for emptiness and then compared
    against the RAW value, which made ``ITS_CONSOLE_EMAIL_DOMAIN="toeshee.com\\n"``
    -- a trailing newline is what a here-doc, a copied ``.env`` line or a
    shell ``read`` produces -- non-empty, unmatchable by every address, and
    therefore a refusal of EVERY user at ALL THREE doors, from a byte the
    operator cannot see. The address is deliberately NOT trimmed the same way
    (see :func:`is_allowed_domain`): it is an IdP-asserted claim, where
    trimming would admit an identity the token did not assert, and refusing
    one address is recoverable in a way that refusing a fleet is not.
    """
    root = "" if base is None else base.strip()
    if not root:
        return None
    return None if is_allowed_domain(email, root) else _DOMAIN_REFUSED
