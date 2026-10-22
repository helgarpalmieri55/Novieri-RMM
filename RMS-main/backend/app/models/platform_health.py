"""One probe result at one instant.

NO `org_id`, DELIBERATELY (spec §6). Postgres being unreachable is not any
organisation's fact. RLS is still ENABLED on this table with a deny-all default
and one policy for `app_user`, so it is not an RLS-shaped hole -- it is a table
whose policy is "the application, and nobody else".

`reason` stores the reason CODE, never an exception's text.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, false
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid7_pk


class PlatformHealthSample(Base):
    __tablename__ = "platform_health_sample"
    __table_args__ = (Index("ix_phs_probe_sampled", "probe", "sampled_at"),)

    id: Mapped[UUID] = uuid7_pk()
    probe: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    duration_ms: Mapped[int] = mapped_column()
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sampled_at: Mapped[datetime] = mapped_column()


class PlatformProbeNotice(Base):
    """What this org has been told about one platform probe (migration 0085).

    Per ORG, unlike the samples: the probe state is platform-wide, but whether
    a notification went out is each org's own fact, and keeping it org-scoped
    keeps this table under the ordinary tenant RLS policy.

    `episode_notified` says whether the current unhealthy episode was announced;
    a recovery is announced only when it was, so a suppressed flap stays silent
    at both ends.
    """

    __tablename__ = "platform_probe_notice"
    __table_args__ = (
        CheckConstraint(
            "state IN ('healthy', 'unhealthy')", name="ck_platform_probe_notice_state"
        ),
    )

    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    probe: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(10))
    changed_at: Mapped[datetime] = mapped_column()
    last_unhealthy_notified_at: Mapped[datetime | None] = mapped_column(default=None)
    episode_notified: Mapped[bool] = mapped_column(server_default=false(), default=False)
