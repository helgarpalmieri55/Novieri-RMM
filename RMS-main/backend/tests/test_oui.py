"""What a MAC address states about itself, and what it merely hints at.

TWO DIFFERENT KINDS OF ANSWER LIVE HERE, and conflating them is the failure to
avoid. The locally-administered and multicast bits are DEFINED BY THE STANDARD:
they are read off the address and are true or false. The manufacturer is a
LOOKUP against a table this repository cannot hold in full -- the IEEE registry
is tens of thousands of entries, changes continuously, and deployments here
frequently have no egress to fetch it.

So the vendor function returns `None` for anything it does not recognise, and
the console renders the raw prefix in that case. A guessed manufacturer on an
inventory screen is worse than no manufacturer: an operator triaging fifty
discovery candidates uses that column to decide what a thing IS, and a wrong
answer sends them to a conclusion faster than a blank one does.
"""

import pytest

from app.modules.ipam.oui import (
    _LOCALLY_ADMINISTERED_BY_CONVENTION,
    OUI,
    is_locally_administered,
    is_multicast,
    oui_prefix,
    vendor,
)


class TestFactsTheAddressStates:
    def test_a_locally_administered_address_is_recognised(self) -> None:
        # Bit 1 of the first octet. Docker, KVM and every phone doing MAC
        # randomisation set it -- which is why part 3's matching must never
        # auto-link on one.
        assert is_locally_administered("02:42:ac:11:00:02") is True
        assert is_locally_administered("00:50:56:aa:bb:cc") is False

    def test_multicast_is_recognised(self) -> None:
        assert is_multicast("01:00:5e:00:00:fb") is True
        assert is_multicast("00:50:56:aa:bb:cc") is False

    @pytest.mark.parametrize("bad", ["", "not-a-mac", "00:50", "zz:zz:zz:zz:zz:zz"])
    def test_a_malformed_address_raises_nothing_and_claims_nothing(
        self, bad: str
    ) -> None:
        # These strings come from a database column populated by three code
        # paths. A screen that 500s because one observation is odd is worse
        # than one that shows the row plainly.
        assert vendor(bad) is None
        assert oui_prefix(bad) == ""
        assert is_locally_administered(bad) is False
        assert is_multicast(bad) is False


class TestVendor:
    def test_it_recognises_a_prefix_it_knows(self) -> None:
        assert vendor("00:50:56:aa:bb:cc") == "VMware"

    def test_it_is_case_and_separator_insensitive(self) -> None:
        # asyncpg returns MACADDR as `00:50:56:aa:bb:cc`; an import may carry
        # `0050.56aa.bbcc` (Cisco) or `00-50-56-AA-BB-CC` (Windows).
        assert vendor("00-50-56-AA-BB-CC") == "VMware"
        assert vendor("0050.56aa.bbcc") == "VMware"

    def test_it_returns_None_rather_than_guessing(self) -> None:
        # A prefix outside the table. The console shows `oui_prefix` instead,
        # which an operator can look up -- and which claims nothing.
        assert vendor("a4:bb:6d:11:22:33") is None
        assert oui_prefix("a4:bb:6d:11:22:33") == "A4:BB:6D"

    def test_every_entry_in_the_table_is_a_well_formed_prefix(self) -> None:
        """The table is hand-maintained, so its shape is asserted rather than
        trusted: a lower-cased or mistyped key silently never matches anything,
        and the column just stays blank for a manufacturer somebody meant to
        cover."""
        for prefix in OUI:
            assert len(prefix) == 8, prefix
            assert prefix == prefix.upper(), prefix
            assert prefix.count(":") == 2, prefix

    def test_a_locally_administered_entry_must_be_a_named_exception(self) -> None:
        """A vendor ASSIGNMENT cannot have the locally-administered bit set --
        that bit means "not from a manufacturer" -- so an entry carrying it is
        normally a mistyped prefix that would attach a manufacturer's name to
        exactly the addresses that have none.

        The exceptions are conventions rather than assignments: QEMU picks
        `52:54:00` for itself, and the bit is set for that reason. Listing them
        keeps this guard sharp for every other key, instead of it being deleted
        the first time it fires on something deliberate.
        """
        offenders = [
            prefix
            for prefix in OUI
            if is_locally_administered(f"{prefix}:00:00:00")
            and prefix not in _LOCALLY_ADMINISTERED_BY_CONVENTION
        ]
        assert not offenders, offenders
