from datetime import date

import pytest
from pydantic import ValidationError

from app.modules.inventory.schema import InventoryDocument

FULL_DOC = {
    "hostname": "wks-042",
    "fqdn": "wks-042.corp.toeshee.net",
    "os_family": "windows",
    "os_version": "Windows 11 Pro 23H2",
    "os_build": "22631.4037",
    "architecture": "x86_64",
    "serial": "5CG1234ABC",
    "hardware": {
        "cpu": "Intel Core i7-1265U",
        "ram_bytes": 17179869184,
        "components": [
            {
                "type": "disk",
                "identifier": "S4EVNX0R123",
                "attributes": {"size_bytes": 512110190592, "model": "Samsung 980"},
            },
            # The NIC in §14.1's shape -- `addresses[]`, and no `attributes.ip`.
            # This is the fixture the whole inventory suite shares, and IPAM's
            # hook test (`test_inventory_ingest.py::test_a_collect_writes_ipam_
            # observations`) reads an observation out of it, so it has to be the
            # shape the agent really emits
            # (`agent/internal/capabilities/inventory/nic.go`). The legacy `ip`
            # key it used to carry was removed from both sides: OQ-2, closed
            # 2026-08-05.
            {
                "type": "nic",
                "identifier": "a4:bb:6d:11:22:33",
                "attributes": {
                    "name": "Ethernet0",
                    "mac": "a4:bb:6d:11:22:33",
                    "addresses": [
                        {"address": "10.1.2.42/24", "family": "ipv4", "origin": "dhcp"}
                    ],
                },
            },
        ],
    },
    "software": [
        {
            "name": "7-Zip",
            "version": "23.01",
            "publisher": "Igor Pavlov",
            "install_date": "2025-11-02",
        },
        {"name": "Google Chrome", "version": "126.0.6478.127"},
    ],
    "facts": {"domain_joined": True, "disk_encryption": True, "firewall_enabled": False},
}


def test_full_document_parses() -> None:
    doc = InventoryDocument(**FULL_DOC)
    assert doc.hostname == "wks-042"
    assert doc.hardware.ram_bytes == 17179869184
    assert doc.software[0].install_date == date(2025, 11, 2)
    assert doc.software[1].publisher == ""
    assert doc.facts["disk_encryption"] is True


def test_unknown_extra_keys_ignored() -> None:
    doc = InventoryDocument(**FULL_DOC, future_field="newer agent")
    assert doc.hostname == "wks-042"


def test_bad_os_family_rejected() -> None:
    with pytest.raises(ValidationError):
        InventoryDocument(**{**FULL_DOC, "os_family": "beos"})


def test_missing_software_defaults_empty() -> None:
    doc = InventoryDocument(**{k: v for k, v in FULL_DOC.items() if k != "software"})
    assert doc.software == []


def test_missing_hostname_rejected() -> None:
    with pytest.raises(ValidationError):
        InventoryDocument(**{k: v for k, v in FULL_DOC.items() if k != "hostname"})
