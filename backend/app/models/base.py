"""Declarative base and shared column helpers for the core data model."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from uuid_extensions import uuid7


class Base(DeclarativeBase):
    type_annotation_map = {
        UUID: PGUUID(as_uuid=True),
        dict[str, Any]: JSONB,
        datetime: DateTime(timezone=True),
    }


def uuid7_pk() -> Mapped[UUID]:
    return mapped_column(primary_key=True, default=uuid7)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
