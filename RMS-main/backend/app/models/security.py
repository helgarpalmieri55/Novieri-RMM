"""Security-log module table (docs/modules/security-logs/spec.md §5).

Append-only for the application role: UPDATE/DELETE are revoked in migration
0006; retention deletes run only through the worker's admin engine and are
audited. No per-event hash chain — see spec §3 for the recorded deviation.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid7_pk


class SecurityEvent(Base):
    __tablename__ = "security_events"
    __table_args__ = (
        UniqueConstraint("device_id", "event_uid"),
        Index("ix_security_events_org_ts", "org_id", "ts"),
        Index("ix_security_events_org_type_ts", "org_id", "event_type", "ts"),
        Index("ix_security_events_org_user_ts", "org_id", "username", "ts"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"))
    event_uid: Mapped[str] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column()
    event_type: Mapped[str] = mapped_column(String(30))
    username: Mapped[str] = mapped_column(String(200), default="")
    source: Mapped[str] = mapped_column(String(100), default="")
    outcome: Mapped[str] = mapped_column(String(10), default="n_a")
    raw: Mapped[dict[str, Any]] = mapped_column(default=dict)
