"""Typed record field schemas (docs/modules/documentation/spec.md §2).

Known fields are validated per type; unknown extras are preserved — records
are structured without being rigid.
"""

import ipaddress
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from app.core.i18n import tr
from app.core.wire_words import say
from app.modules.documentation.words import RECORD_TYPE_WORDS

RECORD_TYPES = ("network", "service", "vendor", "location")


class _RecordFields(BaseModel):
    model_config = ConfigDict(extra="allow")


class NetworkFields(_RecordFields):
    cidr: str | None = None
    vlan: int | None = None
    gateway: str = ""
    dns: str = ""

    @field_validator("cidr")
    @classmethod
    def _valid_cidr(cls, value: str | None) -> str | None:
        if value is not None:
            ipaddress.ip_network(value, strict=False)  # raises ValueError if invalid
        return value


class ServiceFields(_RecordFields):
    url: str = ""
    owner: str = ""
    environment: str = ""


class VendorFields(_RecordFields):
    contact_email: str = ""
    phone: str = ""
    contract_end: date | None = None


class LocationFields(_RecordFields):
    address: str = ""
    timezone: str = ""


RECORD_FIELD_MODELS: dict[str, type[_RecordFields]] = {
    "network": NetworkFields,
    "service": ServiceFields,
    "vendor": VendorFields,
    "location": LocationFields,
}


def validate_record_fields(record_type: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Validate known fields for the type; keep extras. Raises ValueError on an
    unknown type or invalid known-field value."""
    model = RECORD_FIELD_MODELS.get(record_type)
    if model is None:
        raise ValueError(tr("unknown record type {record_type}", record_type=repr(record_type)))
    try:
        validated = model(**fields)
    except (ValidationError, ValueError) as exc:
        raise ValueError(
            tr(
                "invalid {record_type} record fields: {exc}",
                record_type=say(RECORD_TYPE_WORDS, record_type),
                exc=exc,
            )
        ) from exc
    return validated.model_dump(mode="json")
