"""MAC address facts, and a deliberately small vendor table.

WHY THE TABLE IS SMALL, AND WHY IT SAYS SO. The IEEE OUI registry is tens of
thousands of assignments and changes continuously. This platform deploys
on-prem, frequently with no egress at all (`docs/product.md` §5), so there is no
build step that could fetch it and no runtime lookup that could keep it current.
**The alternative to a small table is not a complete one — it is a table that is
wrong in ways nobody can see.**

So the entries below are prefixes that actually recur in a corporate estate, and
anything else returns `None`. The console then shows the raw prefix, which an
operator can look up and which claims nothing. A guessed manufacturer would be
worse than a blank: the vendor column is what somebody triaging fifty discovery
candidates uses to decide what a thing IS, and a wrong answer sends them to a
conclusion faster than an absent one.

Adding an entry is a one-line change and a welcome one. Guessing is not.

THE OTHER TWO FUNCTIONS ARE A DIFFERENT KIND OF ANSWER, and the distinction is
the reason they live beside the table rather than being folded into it.
`is_locally_administered` and `is_multicast` are DEFINED BY THE STANDARD: they
are bits of the first octet, read off the address, true or false with nothing
to look up. Part 3's enrolment matching depends on the first one — a
locally-administered MAC is assigned by software, so two containers on
different hosts can carry the same one, and auto-linking on it would merge two
real machines.
"""

import re

#: Prefix (upper case, colon separated) -> manufacturer.
#:
#: Hypervisors first, because in a virtualised estate they are most of what a
#: sweep returns and "this is a VM" is the single most useful thing the column
#: can say. Then network gear, laptop vendors, NIC vendors, and the printers
#: and phones a corporate LAN is full of.
#:
#: NOT a mirror of the IEEE registry, and not maintained against it -- see the
#: module docstring. `tests/test_oui.py` asserts the SHAPE of every key,
#: because a mistyped one silently never matches and just leaves the column
#: blank for a manufacturer somebody meant to cover.
OUI: dict[str, str] = {
    # Hypervisors and virtual NICs
    "00:50:56": "VMware",
    "00:0C:29": "VMware",
    "00:1C:14": "VMware",
    "00:05:69": "VMware",
    "08:00:27": "Oracle VirtualBox",
    # LOCALLY ADMINISTERED, and deliberately here. `52:54:00` is not an IEEE
    # assignment -- QEMU picks it by convention, and the locally-administered
    # bit is set precisely because it is not a manufacturer's range. It is
    # still the single most recognisable thing on a KVM host's LAN, so it earns
    # its place; `_LOCALLY_ADMINISTERED_BY_CONVENTION` below records that this
    # is a decision rather than a typo.
    "52:54:00": "QEMU/KVM",
    "00:15:5D": "Microsoft Hyper-V",
    "00:1C:42": "Parallels",
    "00:16:3E": "Xen",
    # Network equipment
    "00:00:0C": "Cisco",
    "00:1B:0D": "Cisco",
    "00:1A:A1": "Cisco",
    "00:0D:BD": "Cisco",
    "00:09:0F": "Fortinet",
    "00:1B:17": "Palo Alto Networks",
    "00:15:17": "Intel",
    "B4:FB:E4": "Ubiquiti",
    "24:5A:4C": "Ubiquiti",
    "00:0F:B5": "Netgear",
    "00:1F:33": "Netgear",
    "00:26:F2": "Netgear",
    "00:14:BF": "Linksys",
    "00:1D:7E": "Linksys",
    "D8:5D:4C": "TP-Link",
    "00:24:A5": "Buffalo",
    # Endpoints
    "00:03:93": "Apple",
    "00:1B:63": "Apple",
    "3C:07:54": "Apple",
    "F0:18:98": "Apple",
    "00:14:22": "Dell",
    "00:21:9B": "Dell",
    "B8:2A:72": "Dell",
    "00:1B:78": "HP",
    "00:23:7D": "HP",
    "3C:D9:2B": "HP",
    "00:21:5A": "HP",
    "00:1B:21": "Intel",
    "00:1E:67": "Intel",
    "00:0E:0C": "Intel",
    "00:1A:6B": "Lenovo",
    "00:21:CC": "Lenovo",
    "00:0A:F7": "Broadcom",
    "00:E0:4C": "Realtek",
    # Printers and phones -- most of what a ping sweep finds that nobody
    # recognises
    "00:80:77": "Brother",
    "00:00:48": "Seiko Epson",
    "00:26:73": "Ricoh",
    "00:1E:8F": "Canon",
    "00:00:85": "Canon",
    "08:00:37": "Fuji Xerox",
    "00:00:AA": "Xerox",
    "00:04:F2": "Polycom",
    "00:0B:82": "Grandstream",
    "00:15:65": "Yealink",
    "00:04:13": "Snom",
}

#: Entries whose locally-administered bit is set ON PURPOSE.
#:
#: A vendor assignment cannot have that bit -- it means "not from a
#: manufacturer" -- so an entry carrying it is normally a mistyped prefix, and
#: `tests/test_oui.py` refuses one. These are the exceptions: well-known
#: conventions rather than assignments, listed so the guard stays sharp for
#: every other key instead of being deleted the first time it fires.
_LOCALLY_ADMINISTERED_BY_CONVENTION = frozenset({"52:54:00"})

_HEX = re.compile(r"[0-9A-Fa-f]")


def _octets(mac: str) -> list[int]:
    """The address as integers, or `[]` if it is not one.

    Tolerant of separator style -- `:`, `-`, `.` or none -- because these
    strings reach here from a database column three different code paths write
    to, and a screen that raises because one row is odd is worse than one that
    shows the row plainly. It is deliberately NOT tolerant of length: eleven
    hex digits is not a MAC with a typo, it is not a MAC.
    """
    digits = "".join(_HEX.findall(mac or ""))
    if len(digits) != 12:
        return []
    return [int(digits[i : i + 2], 16) for i in range(0, 12, 2)]


def oui_prefix(mac: str) -> str:
    """`AA:BB:CC`, or `""` for anything that is not a MAC.

    Shown by the console when `vendor` returns `None`: the prefix is a fact,
    and an operator can look it up.
    """
    octets = _octets(mac)
    if not octets:
        return ""
    return ":".join(f"{octet:02X}" for octet in octets[:3])


def vendor(mac: str) -> str | None:
    """The manufacturer, or `None` -- never a guess. See the module docstring."""
    return OUI.get(oui_prefix(mac))


def is_locally_administered(mac: str) -> bool:
    """Bit 1 of the first octet: this address was assigned by software, not by
    a manufacturer.

    Docker, KVM, most hypervisor defaults and every phone doing MAC
    randomisation set it. Part 3's matching must never auto-link on one --
    two containers on different hosts can collide, and the resulting merge is
    not recoverable.
    """
    octets = _octets(mac)
    return bool(octets) and bool(octets[0] & 0b10)


def is_multicast(mac: str) -> bool:
    """Bit 0 of the first octet. Never an endpoint's own address, so never
    something to promote into an inventory."""
    octets = _octets(mac)
    return bool(octets) and bool(octets[0] & 0b1)
