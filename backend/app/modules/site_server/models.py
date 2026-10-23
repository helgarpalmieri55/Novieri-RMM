"""The one site-server table (`docs/modules/site-server/spec.md` §2.1).

EVERY COLUMN HERE MUST MATCH THE MIGRATION EXACTLY in type, length and
nullability, and every index declared here must be declared there --
`tests/test_schema_drift.py` has two tests and the second makes Postgres deparse
the indexes on both sides.

THREE THINGS HERE ARE NOT ORDINARY DDL:

1. `segments` IS `cidr[]`, NOT `text[]`. The operation this column exists for is
   containment -- "is the subnet being swept inside a segment this server
   serves" (§6.1's `<<=`), "which declared segment holds this address" (§5.2's
   broadcast computation) -- and in `text` those are string comparisons that are
   wrong at every boundary: `10.200.7.0/24` starts with `10.20` and is nowhere
   near `10.20.0.0/16`. This is IPAM's §0.1 reasoning, and `ipam_subnet.prefix`
   is `CIDR` for it.

2. `UNIQUE (device_id)`, NOT `(org_id, device_id)`. `devices.id` is already
   globally unique, so the composite key would be strictly weaker while reading
   as stricter. `agents.device_id` makes the same choice.

3. THE `roles` CHECK IS WIDER THAN v1 HONOURS, DELIBERATELY. `cache` and `pxe`
   are storable here and REFUSED BY THE SERVICE (`registry.promote`), because
   the vocabulary is the entire forward seam for the deferred cache (§7) and PXE
   (§8) slices: keeping them admissible means that slice needs no migration to
   widen a shipped CHECK on a populated table, and the refusal it must relax is
   one predicate in one place, with the §4.4 gate named in its error message.
   Storing `roles=['cache']` on a machine with no listener would be a row
   claiming a service is running when none is, which is why the service refuses
   rather than accepting-and-ignoring.

NO GiST INDEX ON `segments` (§16). A fleet-sized `&&` scan is a handful of rows,
and an index no query plan needs is a thing to maintain.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    FetchedValue,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, CIDR
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid7_pk

__all__ = ["SITE_SERVER_ROLES", "SiteServer"]

#: The whole vocabulary the DB CHECK admits. v1 honours `wake` only; the other
#: two are the seam (see the module docstring), refused at the service.
SITE_SERVER_ROLES = ("wake", "cache", "pxe")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SiteServer(Base):
    """A `Device` an operator has promoted to serve LAN segments (§0.1).

    Not a new job target, not a new connection identity, not a new certificate
    profile -- a row beside a device that already has an `agents` row and an
    ordinary device-bound certificate. That choice is what makes this module a
    module rather than a kernel fork, and `tests/test_site_server_kernel_
    boundary.py` asserts it rather than trusting it.

    `ipam_site_id` IS NULLABLE AND THAT IS LOAD-BEARING. IPAM is a pull-forward
    module and site servers must work in a deployment that never installs it, so
    the FK is a convenience for grouping and never a dependency: nothing in the
    registry, the dispatch path or the segment check requires it to be set.
    """

    __tablename__ = "site_server"
    __table_args__ = (
        UniqueConstraint("device_id", name="uq_site_server_device"),
        CheckConstraint(
            "cardinality(segments) BETWEEN 1 AND 32",
            name="ck_site_server_segment_count",
        ),
        CheckConstraint(
            "roles <@ ARRAY['wake', 'cache', 'pxe']::text[] AND cardinality(roles) >= 1",
            name="ck_site_server_roles",
        ),
        Index("ix_site_server_org_enabled", "org_id", "enabled"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the devices row named by device_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"))
    name: Mapped[str] = mapped_column(String(200))
    segments: Mapped[list[str]] = mapped_column(ARRAY(CIDR()))
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text()))
    ipam_site_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ipam_site.id"), default=None
    )
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)
    notes: Mapped[str] = mapped_column(Text, server_default=text("''"), default="")
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)
