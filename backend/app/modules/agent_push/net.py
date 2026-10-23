"""Address and prefix parsing for the push path -- stdlib `ipaddress`, and
nothing wider.

**THIS MODULE IS WHERE INVARIANT 2a CLAUSE (f) IS FIRST ENFORCED.** "Named
addresses only -- never a group, a filter, a subnet or a saved search" is not
a policy that can live in a docstring: it is `parse_target_address` refusing
anything that is not exactly one host literal. A `/24` typed into the target
field must fail HERE, before any authorization is consulted, because a target
set the operator did not enumerate is the thing the clause exists to forbid.

**DELIBERATELY NOT IMPORTING `app.modules.ipam.net`.** IPAM is a pull-forward
module that a deployment may never install, and the push path must not stop
working -- or, worse, start behaving differently -- depending on whether it is
present. What is needed here is three lines of `ipaddress`, and the coupling
would buy nothing.

**CONTAINMENT IS NOT DECIDED HERE.** `push_authorization` and
`push_credential_scope` store native `cidr`, and containment is `>>=` in
Postgres against that type. Re-implementing it in Python would be a second
answer to "is this address inside that prefix", diverging on the first bug
fixed in one of them; this module only parses and canonicalises.
"""

from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network

from app.core.i18n import tr
from app.modules.agent_push.errors import PushValidationError

__all__ = ["IPAddress", "IPNetwork", "parse_prefix", "parse_target_address", "prefix_size"]

IPAddress = IPv4Address | IPv6Address
IPNetwork = IPv4Network | IPv6Network


def parse_target_address(value: object) -> IPAddress:
    """Exactly one host address, or a refusal naming what was wrong.

    A CIDR, a hostname, a range, a comma-separated list and an empty string
    are all refused, each with the same message, because they are all the same
    mistake: a target the operator did not name one at a time.

    **A HOSTNAME IS REFUSED RATHER THAN RESOLVED.** Resolution happens at a
    moment nobody watched, against a resolver nobody audited, and one name can
    answer with several addresses -- spec §4.2, "no more than one target. Not a
    CIDR, not a list, not a hostname that resolves to several addresses."
    """
    if not isinstance(value, str):
        raise PushValidationError(tr("a target address must be a string IP literal"))
    literal = value.strip()
    if not literal:
        raise PushValidationError(tr("a target address must be a non-empty IP literal"))
    try:
        return ip_address(literal)
    except ValueError:
        raise PushValidationError(
            tr(
                "{literal} is not a single IP address. A push may be expressed only "
                "against explicitly named addresses a human selected -- never a CIDR, a "
                "hostname, a range, a group or a saved search (invariant 2a clause (f))",
                literal=repr(literal),
            )
        ) from None


def parse_prefix(value: object) -> IPNetwork:
    """Normalise to a canonical network, or raise a readable 400.

    `strict=False`, matching `ipam.authorization._parse_prefix`: an operator
    typing `10.70.0.5/24` means the /24, and Postgres's `cidr` would refuse the
    literal outright ("bits set to right of mask") with a message about masks
    rather than about their network. A bare address is accepted and becomes a
    /32 (or /128), which is the narrowest useful grant and a common one here:
    binding a credential to one server is a legitimate scope.
    """
    if not isinstance(value, str):
        raise PushValidationError(tr("prefix must be a non-empty string"))
    literal = value.strip()
    if not literal:
        raise PushValidationError(tr("prefix must be a non-empty string"))
    try:
        return ip_network(literal, strict=False)
    except ValueError as exc:
        raise PushValidationError(
            tr("{literal} is not a valid prefix: {exc}", literal=repr(literal), exc=exc)
        ) from None


def prefix_size(prefix: IPNetwork) -> int:
    """How many addresses the grant reaches.

    Stated in ADDRESSES because nobody converts mask lengths in their head,
    and because misreading a scope is how a credential gets presented on a
    segment nobody meant to touch. `ScanAuthorizationsPage.tsx` states the same
    number for the same reason one privilege level down.
    """
    return int(prefix.num_addresses)
