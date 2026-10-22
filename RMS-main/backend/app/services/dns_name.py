"""A machine's DNS name, as an operator types it onto an asset row.

WHY THIS EXISTS. Windows push is Kerberos only, and Kerberos issues a ticket
for a NAME -- `HTTP/<fqdn>` -- never an address (design 2026-09-28 §2.3). A
machine a push is FOR has never run the agent, so its `devices.fqdn` is not
reported by inventory; a person has to type it. This is the one check that
typed value passes, shared by the asset routes (and usable by dispatch).

WHAT IT REFUSES, and why each matters:

* **no dot** -- `pc1` is not a full name; the realm is derived from everything
  after the first label, so a bare host name has no realm.
* **an IP literal** (plan W10) -- a ticket cannot be issued for an address, and
  an address typed where a name belongs is a name nobody vouched for.
* **an all-numeric last label** -- `10.20.4` is not an IP literal to Python but
  is one to `inet_aton` and to many resolvers; no real TLD is all digits.
* **anything outside `[a-z0-9-]` per label**, a label that starts or ends with
  `-`, an empty label, a label over 63 or a name over 253 characters (RFC 1035
  / RFC 1123).

It is **never derived from reverse DNS** (plan W10): letting a PTR answer choose
the name would let whoever answers PTR queries choose which Kerberos service a
domain credential is presented to.

The value is stored LOWER-CASED: DNS is case-insensitive, and the SPN the
agent asks for is lower-cased, so two spellings of one machine must not be two
values.
"""

import ipaddress
import re

from app.core.i18n import tr

MAX_DNS_NAME = 253

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class InvalidDnsName(ValueError):
    """The value is not a full DNS host name. The message is operator-facing."""


def normalise_dns_name(value: str) -> str:
    """Return `value` lower-cased if it is a full DNS host name, else raise
    `InvalidDnsName` naming what is wrong with it."""
    name = value.strip().lower()
    if len(name) > MAX_DNS_NAME:
        raise InvalidDnsName(tr(
            "a DNS name is at most {max_dns_name} characters",
            max_dns_name=MAX_DNS_NAME,
        ))
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        raise InvalidDnsName(
            tr(
                "that is an IP address; enter the computer's full DNS name, e.g. "
                "pc1.corp.example.com",
            )
        )
    labels = name.split(".")
    if len(labels) < 2:
        raise InvalidDnsName(
            tr("a DNS name needs its domain as well, e.g. pc1.corp.example.com rather than pc1")
        )
    for label in labels:
        if not _LABEL.match(label):
            raise InvalidDnsName(
                tr(
                    "each part of a DNS name is 1 to 63 letters, digits or hyphens, and does "
                    "not start or end with a hyphen",
                )
            )
    if labels[-1].isdigit():
        raise InvalidDnsName(tr("the last part of a DNS name cannot be all digits"))
    return name
