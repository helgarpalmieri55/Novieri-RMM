"""The inventory.collect result document — the agent-side collector contract.

extra="ignore" everywhere: newer agents may send fields this control plane
does not know yet without breaking ingestion. Exported as JSON Schema to
docs/kernel/wire-protocol/ for `agent/`.
"""

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Doc(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ComponentDoc(_Doc):
    type: str = Field(pattern="^(cpu|memory|disk|nic|gpu|display|battery|bios|motherboard)$")
    identifier: str = Field(min_length=1, max_length=200)
    attributes: dict[str, Any] = Field(default_factory=dict)


class HardwareDoc(_Doc):
    cpu: str = ""
    ram_bytes: int | None = None
    components: list[ComponentDoc] = Field(default_factory=list)


class SoftwareDoc(_Doc):
    name: str = Field(min_length=1, max_length=300)
    version: str = ""
    publisher: str = ""
    install_date: date | None = None


class InventoryDocument(_Doc):
    hostname: str = Field(min_length=1, max_length=255)
    fqdn: str | None = None
    os_family: str = Field(pattern="^(windows|linux|macos)$")
    os_version: str = ""
    os_build: str | None = None
    architecture: str = ""
    serial: str | None = None
    hardware: HardwareDoc = Field(default_factory=HardwareDoc)
    software: list[SoftwareDoc] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)
