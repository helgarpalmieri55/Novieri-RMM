"""The ten IPAM tables (`docs/modules/ipam/spec.md` §2.1-§2.10).

EVERY COLUMN HERE MUST MATCH THE MIGRATION EXACTLY in type, length and
nullability, and every index declared here must be declared there --
`tests/test_schema_drift.py` compares `Base.metadata` against the schema
`alembic upgrade head` actually produced, columns AND indexes, the latter by
making Postgres deparse both sides. A predicate written differently in the two
places is reported as drift even when it means the same thing.

FOUR THINGS HERE ARE NOT ORDINARY DDL. Each one closes a specific failure that
otherwise fails nothing:

1. `ipam_observation.mac_key` IS A STORED GENERATED COLUMN (§0.15). The upsert
   key is `(org_id, vrf_id, address, source, mac_key)` and not `(..., mac)`,
   because `mac` is nullable, every ICMP-only sweep observation has
   `mac IS NULL`, and NULLs do not collide in a Postgres unique index -- so the
   weekly sweep of a /24 would insert a fresh row per responding host per sweep,
   forever. That turns §2.7 from the current-state table §0.10 point 4 depends
   on into an unbounded probe log, silently, with nothing failing. Generated
   rather than defaulted at write time so that no write path -- not this one,
   not the second ingestion path somebody adds later -- can forget it. **No
   write path may set it**: passing `mac_key` to an INSERT is a Postgres error
   against a generated column. It appears only in `index_elements`.

2. `ipam_vlan` CARRIES A PARTIAL UNIQUE INDEX PAIR, not one index (§2.3,
   §0.15). `UNIQUE (org_id, site_id, vid)` with `site_id` nullable permits two
   site-less VLAN 10 rows, which is the duplicate it exists to prevent. The
   sentinel-row fix §0.7 chose for `vrf_id` is rejected here: a `global` VRF
   names a real routing domain, a fake site does not, and it would land in every
   site picker and every site-scoped query. Neither index is a named
   constraint, so upserts must infer with `index_elements=` + `index_where=`;
   `ON CONFLICT ON CONSTRAINT` fails at RUNTIME against a partial index. Since
   migration 0240 the site-less half is per CLIENT, and the site half needs no
   client column: the site pins it.

3. `ipam_subnet.parent_id` IS A DERIVED CACHE (§0.2), recomputed on every write
   as "the smallest other prefix in the same VRF that contains this one".
   Nobody sets it by hand, and the API refuses a client-supplied value: a
   hand-maintained hierarchy that disagrees with the addresses it describes
   renders happily while every containment query says otherwise.

4. THERE IS NO GiST `EXCLUDE` CONSTRAINT FOR OVERLAP (§0.3). A supernet *must*
   overlap its children; the rule that matters is "no two SIBLINGS overlap",
   and an exclusion constraint keyed on `parent_id` leaves every top-level row
   unconstrained because exclusion constraints do not fire on NULL. Sibling
   overlap is checked in `subnets.py` under `pg_advisory_xact_lock`. The GiST
   indexes below are what make that check, the parent derivation and "which
   subnet holds this address" indexed rather than sequential.

Native `inet`/`cidr`/`macaddr` throughout (§0.1): the whole module is
containment and ordering, which in text is wrong at every boundary
(`10.4.7.9` sorts before `10.4.7.10`; `10.4.70.0/24` LIKE `10.4.7%`).

EVERY TABLE BELONGS TO A CLIENT (MSP clients spec, Annex A; migration 0240).
`ipam_vrf` and `ipam_vlan` are C: the application writes the client. The
other seven are H: trigger `its_client_from_parent` copies it from the VRF
or the subnet the row hangs from -- never sent, read back from the INSERT.
A VLAN, a subnet and a site server name a `sites` row of their own client
(`fk_<table>_site_client`); `ipam_site` is gone (spec §1.2).
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    FetchedValue,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    desc,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import CIDR, INET, JSONB, MACADDR
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, client_org_fk, site_client_fk, uuid7_pk

__all__ = [
    "IpamAddress",
    "IpamDrift",
    "IpamObservation",
    "IpamRange",
    "IpamScanAuthorization",
    "IpamSubnet",
    "IpamSweepRun",
    "IpamVlan",
    "IpamVrf",
]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _inherited_client() -> Mapped[UUID]:
    """An H table's `client_id`: written by trigger its_client_from_parent
    (migration 0240), never sent; the INSERT reads it back."""
    return mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )


class IpamVrf(Base):
    """A routing domain (spec §2.1).

    `ipam_subnet.vrf_id` and `ipam_address.vrf_id` are NOT NULL and this table
    carries a lazily created `global` row per client (§0.7; per org until
    migration 0240). A nullable scoping
    column would produce two uniqueness rules rather than one, because NULL is
    distinct from NULL in a unique index -- `(org, NULL, 10.0.0.0/24)` could be
    inserted twice and the duplicate-prefix bug `uq_ipam_subnet_org_vrf_prefix`
    exists to prevent walks in through the default case.
    """

    __tablename__ = "ipam_vrf"
    __table_args__ = (
        # Per client (migration 0240): two networks both have a `global` VRF
        # and a "LAN", and a technician of X never meets Y's names.
        UniqueConstraint("org_id", "client_id", "name", name="uq_ipam_vrf_client_name"),
        client_org_fk("ipam_vrf"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (C). Set by the application (migration 0240).
    client_id: Mapped[UUID] = mapped_column(ForeignKey("clients.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    rd: Mapped[str | None] = mapped_column(String(64), default=None)
    description: Mapped[str] = mapped_column(Text, server_default="", default="")
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class IpamVlan(Base):
    """A VLAN of one client, optionally at one of its sites (spec §2.3).

    THE UNIQUENESS IS A PARTIAL INDEX PAIR AND BOTH HALVES ARE LOAD-BEARING
    (§0.15). VLAN 10 legitimately exists at every site, once each
    (`uq_ipam_vlan_site_vid`), and exactly once per client without a site
    (`uq_ipam_vlan_client_vid`). A single `UNIQUE (org_id, site_id, vid)`
    enforces only the first: `(org, NULL, 10)` does not collide with itself.
    Between them the two are total. The site half holds no client column
    because the site pins it (`fk_ipam_vlan_site_client`).
    """

    __tablename__ = "ipam_vlan"
    __table_args__ = (
        Index(
            "uq_ipam_vlan_site_vid",
            "org_id",
            "site_id",
            "vid",
            unique=True,
            postgresql_where=text("site_id IS NOT NULL"),
        ),
        Index(
            "uq_ipam_vlan_client_vid",
            "org_id",
            "client_id",
            "vid",
            unique=True,
            postgresql_where=text("site_id IS NULL"),
        ),
        CheckConstraint("vid BETWEEN 1 AND 4094", name="ck_ipam_vlan_vid_range"),
        client_org_fk("ipam_vlan"),
        site_client_fk("ipam_vlan"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (C). Set by the application (migration 0240):
    # the site's client, or the one the request names for a site-less VLAN.
    client_id: Mapped[UUID] = mapped_column(ForeignKey("clients.id"), index=True)
    # One of the VLAN's client's sites (`fk_ipam_vlan_site_client`).
    site_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("sites.id"), index=True, default=None
    )
    vid: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(100), server_default="", default="")
    description: Mapped[str] = mapped_column(Text, server_default="", default="")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class IpamSubnet(Base):
    """A prefix in the plan of record (spec §2.4).

    `kind` separates a pure-hierarchy prefix (a /8 that exists so the tree makes
    sense) from one that holds addresses: only `assignable` subnets have
    utilisation, next-free and sweeps. Without it the first thing an operator
    does is ask for the next free address in their /8.

    THE GiST INDEX IS THE ONE THAT MATTERS. It serves containment (`>>=`,
    `<<=`) and overlap (`&&`) -- parent derivation, "which subnet holds this
    observation", and §0.3's sibling check. There is deliberately no composite
    `(org_id, prefix)` GiST: that needs `btree_gist`, and RLS already narrows
    every scan to one org before the GiST predicate is applied.
    """

    __tablename__ = "ipam_subnet"
    __table_args__ = (
        Index(
            "ix_ipam_subnet_prefix_gist",
            "prefix",
            postgresql_using="gist",
            postgresql_ops={"prefix": "inet_ops"},
        ),
        Index("uq_ipam_subnet_org_vrf_prefix", "org_id", "vrf_id", "prefix", unique=True),
        Index("ix_ipam_subnet_org_parent", "org_id", "parent_id"),
        Index(
            "ix_ipam_subnet_sweep_due",
            "org_id",
            "sweep_enabled",
            "last_swept_at",
            postgresql_where=text("sweep_enabled"),
        ),
        CheckConstraint("kind IN ('container', 'assignable')", name="ck_ipam_subnet_kind"),
        # A gateway outside its own subnet is a typo, and it is the typo that
        # makes provisioning hand out a broken static config.
        CheckConstraint("gateway IS NULL OR gateway << prefix", name="ck_ipam_subnet_gateway_in"),
        # §0.10's "an IPAM becomes a ping monitor at a one-minute interval"
        # clamp, restated in SQL because the application check is one refactor
        # away from being the only one.
        CheckConstraint(
            "sweep_interval_hours IS NULL OR sweep_interval_hours >= 1",
            name="ck_ipam_subnet_sweep_interval",
        ),
        site_client_fk("ipam_subnet"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H, amended): the VRF's, which is NOT NULL.
    client_id: Mapped[UUID] = _inherited_client()
    vrf_id: Mapped[UUID] = mapped_column(ForeignKey("ipam_vrf.id"))
    prefix: Mapped[str] = mapped_column(CIDR)
    kind: Mapped[str] = mapped_column(String(12), server_default="assignable", default="assignable")
    name: Mapped[str] = mapped_column(String(200), server_default="", default="")
    description: Mapped[str] = mapped_column(Text, server_default="", default="")
    # One of the subnet's client's sites (`fk_ipam_subnet_site_client`).
    site_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("sites.id"), index=True, default=None
    )
    vlan_id: Mapped[UUID | None] = mapped_column(ForeignKey("ipam_vlan.id"), default=None)
    gateway: Mapped[str | None] = mapped_column(INET, default=None)
    dns_servers: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), default=list
    )
    #: DERIVED CACHE (§0.2), never supplied by a client.
    parent_id: Mapped[UUID | None] = mapped_column(ForeignKey("ipam_subnet.id"), default=None)
    sweep_enabled: Mapped[bool] = mapped_column(Boolean, server_default=false(), default=False)
    sweep_device_id: Mapped[UUID | None] = mapped_column(ForeignKey("devices.id"), default=None)
    sweep_interval_hours: Mapped[int | None] = mapped_column(Integer, default=None)
    last_swept_at: Mapped[datetime | None] = mapped_column(default=None)
    doc_record_id: Mapped[UUID | None] = mapped_column(ForeignKey("doc_records.id"), default=None)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class IpamRange(Base):
    """A band of addresses inside one subnet (spec §2.5).

    THIS IS HOW "the DHCP pool is .100-.200" IS EXPRESSED WITHOUT 101 ADDRESS
    ROWS. §0.4's sparseness would be defeated immediately otherwise.

    `start_address <= end_address` is a CHECK; "both endpoints fall inside
    `subnet.prefix`" cannot be, because a CHECK cannot reach the parent row --
    that one is enforced in `subnets.create_range`.
    """

    __tablename__ = "ipam_range"
    __table_args__ = (
        Index("ix_ipam_range_lookup", "org_id", "subnet_id", "start_address"),
        CheckConstraint(
            "kind IN ('dhcp_pool', 'reserved', 'excluded')", name="ck_ipam_range_kind"
        ),
        CheckConstraint("start_address <= end_address", name="ck_ipam_range_ordered"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H): the subnet's.
    client_id: Mapped[UUID] = _inherited_client()
    subnet_id: Mapped[UUID] = mapped_column(ForeignKey("ipam_subnet.id"))
    kind: Mapped[str] = mapped_column(String(12))
    start_address: Mapped[str] = mapped_column(INET)
    end_address: Mapped[str] = mapped_column(INET)
    name: Mapped[str] = mapped_column(String(200), server_default="", default="")
    description: Mapped[str] = mapped_column(Text, server_default="", default="")
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class IpamAddress(Base):
    """What a HUMAN asserted about one address (spec §2.6, §0.6).

    `state` is documented intent and nothing else -- `in_use`, `stale` and
    `conflicting` are DERIVED at read time and stored nowhere. If `in_use` were
    a value of this column, an agent reporting an address would have to write
    it, and a device coming online would silently overwrite what an operator
    documented. That sentence is also the untrusted-input control in §6.4: the
    agent's report can create an `ipam_observation` row and can never change
    this table's `state`.

    Rows are SPARSE (§0.4): one exists only once something has been said about
    that address. "Free" is the absence of a row plus the absence of a covering
    range. Release does not delete the row -- it sets `state='free'`, clears the
    claimant and stamps `released_at`.
    """

    __tablename__ = "ipam_address"
    __table_args__ = (
        Index(
            "ix_ipam_address_gist",
            "address",
            postgresql_using="gist",
            postgresql_ops={"address": "inet_ops"},
        ),
        Index("uq_ipam_address_org_vrf_addr", "org_id", "vrf_id", "address", unique=True),
        Index("ix_ipam_address_grid", "org_id", "subnet_id", "address"),
        Index(
            "ix_ipam_address_device",
            "org_id",
            "device_id",
            postgresql_where=text("device_id IS NOT NULL"),
        ),
        Index(
            "ix_ipam_address_mac", "org_id", "mac", postgresql_where=text("mac IS NOT NULL")
        ),
        # §0.6: no `in_use`, no `stale`, no `conflicting` in this column, ever.
        CheckConstraint(
            "state IN ('free', 'reserved', 'assigned', 'deprecated')", name="ck_ipam_address_state"
        ),
        CheckConstraint(
            "origin IN ('manual', 'imported', 'discovered')", name="ck_ipam_address_origin"
        ),
        CheckConstraint(
            "allocation_method IN ('static', 'dhcp', 'slaac', 'discovered', 'unknown')",
            name="ck_ipam_address_allocation_method",
        ),
        # An assignment has EXACTLY ONE claimant. Both, or neither, is how
        # "assigned to nothing" gets into a plan of record.
        CheckConstraint(
            "state <> 'assigned' OR (device_id IS NOT NULL) <> (asset_label <> '')",
            name="ck_ipam_address_one_claimant",
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H): the VRF's.
    client_id: Mapped[UUID] = _inherited_client()
    #: NULLABLE ON PURPOSE: an observation may land in no registered subnet
    #: (§4.2 orphans), and an orphan is not an error.
    subnet_id: Mapped[UUID | None] = mapped_column(ForeignKey("ipam_subnet.id"), default=None)
    vrf_id: Mapped[UUID] = mapped_column(ForeignKey("ipam_vrf.id"))
    address: Mapped[str] = mapped_column(INET)
    state: Mapped[str] = mapped_column(String(12), server_default="free", default="free")
    origin: Mapped[str] = mapped_column(String(12), server_default="manual", default="manual")
    allocation_method: Mapped[str] = mapped_column(
        String(12), server_default="unknown", default="unknown"
    )
    device_id: Mapped[UUID | None] = mapped_column(ForeignKey("devices.id"), default=None)
    asset_label: Mapped[str] = mapped_column(String(200), server_default="", default="")
    mac: Mapped[str | None] = mapped_column(MACADDR, default=None)
    hostname: Mapped[str] = mapped_column(String(255), server_default="", default="")
    description: Mapped[str] = mapped_column(Text, server_default="", default="")
    assigned_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    assigned_at: Mapped[datetime | None] = mapped_column(default=None)
    released_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class IpamObservation(Base):
    """What the FLEET reported about one address (spec §2.7, §0.6, §0.10).

    A CURRENT-STATE TABLE, NOT A TIME SERIES. Rows are UPSERTed on
    `uq_ipam_obs_key`, bumping `last_seen_at` and `sighting_count`; there is no
    row per probe, no availability percentage and no RTT anywhere in this
    module. `mac_key` is what makes that hold -- see this module's docstring,
    point 1, for what the obvious key on `mac` does instead.

    `allocation_origin` is the fleet's answer to §3.2 -- see the column. It is
    written only by ingestion and read only at derivation time; nothing joins on
    it and it is deliberately NOT part of `uq_ipam_obs_key`, because a NIC that
    moved from a DHCP lease to a static address is the SAME sighting with a new
    answer, not a second row.

    `confidence` is a property of the method, fixed at write time:
    `inventory/nic -> high` (the OS reported about itself, over mTLS, as the
    result of a signed job), `sweep/arp -> medium` (authoritative for
    address<->MAC on-segment, not for identity), `sweep/icmp -> low` (something
    answered; that is all it means). It is a column and not a comment because
    `mac_mismatch` (§4.3) reads it: an ICMP observation carries no MAC, so that
    drift class can never fire on ICMP-only evidence.
    """

    __tablename__ = "ipam_observation"
    __table_args__ = (
        # TOTAL, not partial -- `mac_key` is NOT NULL -- so the upsert infers
        # with plain `index_elements` and needs no `index_where`.
        Index(
            "uq_ipam_obs_key",
            "org_id",
            "vrf_id",
            "address",
            "source",
            "mac_key",
            unique=True,
        ),
        Index("ix_ipam_obs_age", "org_id", "last_seen_at"),
        Index("ix_ipam_obs_device", "org_id", "device_id"),
        Index(
            "ix_ipam_obs_gist",
            "address",
            postgresql_using="gist",
            postgresql_ops={"address": "inet_ops"},
        ),
        CheckConstraint(
            "source IN ('inventory', 'sweep', 'import', 'dhcp')", name="ck_ipam_obs_source"
        ),
        CheckConstraint(
            "method IN ('nic', 'arp', 'icmp', 'manual')", name="ck_ipam_obs_method"
        ),
        CheckConstraint(
            "confidence IN ('high', 'medium', 'low')", name="ck_ipam_obs_confidence"
        ),
        # §14.1's vocabulary, and the same set `nic.ORIGINS` normalises to. The
        # parser maps anything else to 'unknown' BEFORE it gets here precisely so
        # this constraint never fires on the inventory result path -- see
        # `nic.py`'s module docstring for why an IntegrityError there is
        # uncontainable in a way a Python exception is not.
        CheckConstraint(
            "allocation_origin IN ('static', 'dhcp', 'slaac', 'link_local', 'unknown')",
            name="ck_ipam_obs_allocation_origin",
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H): the VRF's.
    client_id: Mapped[UUID] = _inherited_client()
    vrf_id: Mapped[UUID] = mapped_column(ForeignKey("ipam_vrf.id"))
    subnet_id: Mapped[UUID | None] = mapped_column(ForeignKey("ipam_subnet.id"), default=None)
    address: Mapped[str] = mapped_column(INET)
    mac: Mapped[str | None] = mapped_column(MACADDR, default=None)
    hostname: Mapped[str] = mapped_column(String(255), server_default="", default="")
    source: Mapped[str] = mapped_column(String(12))
    method: Mapped[str] = mapped_column(String(10))
    #: Set only for `source='inventory'`. A sweep sees addresses and MACs, not
    #: identity, and letting it assert one would be a compromised agent's
    #: cheapest way to re-point an assignment (§6.4).
    device_id: Mapped[UUID | None] = mapped_column(ForeignKey("devices.id"), default=None)
    confidence: Mapped[str] = mapped_column(String(6))
    #: HOW THE ENDPOINT SAID THIS ADDRESS WAS ALLOCATED (§14.1's `origin`), and
    #: the input to §3.2's `allocation_method` (`allocation.derive_allocation_
    #: method`). NOT `origin`: `ipam_address.origin` already means how a ROW got
    #: into the plan, the two are in scope together in `address_grid` and
    #: `is_conflicting`, and one name for two facts is a bug waiting for a
    #: hurried afternoon.
    #:
    #: `unknown` for every sweep observation, permanently and by design: ARP and
    #: ICMP establish that an address answers, not how it was configured. That is
    #: the same limit `confidence` records for the same evidence.
    allocation_origin: Mapped[str] = mapped_column(
        String(12), server_default="unknown", default="unknown"
    )
    sweep_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ipam_sweep_run.id"), default=None
    )
    #: GENERATED ALWAYS ... STORED. No write path sets it; none can forget it.
    mac_key: Mapped[str] = mapped_column(
        Text, Computed("coalesce(mac::text, '')", persisted=True)
    )
    first_seen_at: Mapped[datetime] = mapped_column(default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=_utcnow)
    sighting_count: Mapped[int] = mapped_column(Integer, server_default="1", default=1)


class IpamDrift(Base):
    """A materialised disagreement between the plan and the fleet (spec §2.8).

    Keyed `(org_id, vrf_id, address, drift_class)` -- the STABLE IDENTITY of
    §0.8. Recomputation upserts and PRESERVES `first_detected_at`,
    acknowledgement and suppression; a resolved item is stamped `resolved_at`,
    not deleted. Without a stored identity there is nowhere to hang "I have seen
    this and it is fine", and an un-acknowledgeable list is a list nobody opens
    twice.

    `detail` carries the evidence that produced the finding (documented
    claimant, observing source, MACs seen, ages) so the UI can explain itself
    without re-deriving it.
    """

    __tablename__ = "ipam_drift"
    __table_args__ = (
        Index("uq_ipam_drift_key", "org_id", "vrf_id", "address", "drift_class", unique=True),
        Index(
            "ix_ipam_drift_open",
            "org_id",
            "drift_class",
            "severity",
            postgresql_where=text("resolved_at IS NULL"),
        ),
        Index(
            "ix_ipam_drift_subnet",
            "org_id",
            "subnet_id",
            postgresql_where=text("resolved_at IS NULL"),
        ),
        CheckConstraint("severity IN ('low', 'medium', 'high')", name="ck_ipam_drift_severity"),
        CheckConstraint(
            "confidence IN ('high', 'medium', 'low')", name="ck_ipam_drift_confidence"
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H): the VRF's.
    client_id: Mapped[UUID] = _inherited_client()
    vrf_id: Mapped[UUID] = mapped_column(ForeignKey("ipam_vrf.id"))
    subnet_id: Mapped[UUID | None] = mapped_column(ForeignKey("ipam_subnet.id"), default=None)
    address: Mapped[str] = mapped_column(INET)
    drift_class: Mapped[str] = mapped_column(String(24))
    severity: Mapped[str] = mapped_column(String(8))
    confidence: Mapped[str] = mapped_column(String(6))
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )
    address_id: Mapped[UUID | None] = mapped_column(ForeignKey("ipam_address.id"), default=None)
    device_id: Mapped[UUID | None] = mapped_column(ForeignKey("devices.id"), default=None)
    first_detected_at: Mapped[datetime] = mapped_column(default=_utcnow)
    last_evaluated_at: Mapped[datetime] = mapped_column(default=_utcnow)
    consecutive_detections: Mapped[int] = mapped_column(Integer, server_default="1", default=1)
    resolved_at: Mapped[datetime | None] = mapped_column(default=None)
    acknowledged_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    acknowledged_at: Mapped[datetime | None] = mapped_column(default=None)
    suppressed_until: Mapped[datetime | None] = mapped_column(default=None)
    note: Mapped[str] = mapped_column(Text, server_default="", default="")


class IpamScanAuthorization(Base):
    """A human's signature on every packet this module emits (spec §2.9, §0.12).

    DENY BY DEFAULT: no row covering a target means that target is never swept.
    There is no org-wide "allow everything" switch and no authorization implied
    by having documented a subnet -- documenting is a filing action, authorizing
    packets against a range is a different decision by a different person, and
    conflating them is how a customer's IPAM ends up scanning a segment their
    contract does not cover.

    `authorized_by` IS NOT NULL: there is no system-authorized scope. Rows are
    never hard-deleted -- revocation stamps `revoked_at` -- so the record of
    what was authorized when survives as evidence, and the unique index is
    partial on `revoked_at IS NULL` so a revoked prefix can be authorized again.
    """

    __tablename__ = "ipam_scan_authorization"
    __table_args__ = (
        Index(
            "uq_ipam_scope_active",
            "org_id",
            "vrf_id",
            "prefix",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index(
            "ix_ipam_scope_gist",
            "prefix",
            postgresql_using="gist",
            postgresql_ops={"prefix": "inet_ops"},
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H): the VRF's.
    client_id: Mapped[UUID] = _inherited_client()
    vrf_id: Mapped[UUID] = mapped_column(ForeignKey("ipam_vrf.id"))
    prefix: Mapped[str] = mapped_column(CIDR)
    note: Mapped[str] = mapped_column(String(500), server_default="", default="")
    authorized_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    authorized_at: Mapped[datetime] = mapped_column(default=_utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    revoked_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)


class IpamSweepRun(Base):
    """One dispatched (or refused, or undispatchable) `net.sweep` (spec §2.10).

    `state='no_agent'` IS A FIRST-CLASS OUTCOME, not an error swallowed in a
    log: a subnet nobody can sweep is a fact the subnet page must show, and it
    is also what §4.3's evidence gate reads.
    """

    __tablename__ = "ipam_sweep_run"
    __table_args__ = (
        Index("ix_ipam_sweep_run_recent", "org_id", "subnet_id", desc("started_at")),
        CheckConstraint(
            "state IN ('queued', 'dispatched', 'succeeded', 'failed', 'refused', 'no_agent')",
            name="ck_ipam_sweep_run_state",
        ),
        CheckConstraint("trigger IN ('scheduled', 'manual')", name="ck_ipam_sweep_run_trigger"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H): the subnet's.
    client_id: Mapped[UUID] = _inherited_client()
    subnet_id: Mapped[UUID] = mapped_column(ForeignKey("ipam_subnet.id"))
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("jobs.id"), default=None)
    device_id: Mapped[UUID | None] = mapped_column(ForeignKey("devices.id"), default=None)
    state: Mapped[str] = mapped_column(String(12))
    trigger: Mapped[str] = mapped_column(String(10))
    target_count: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    scope_digest: Mapped[str] = mapped_column(String(64), server_default="", default="")
    hosts_found: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, server_default=false(), default=False)
    requested_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    started_at: Mapped[datetime] = mapped_column(default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    error: Mapped[str] = mapped_column(String(500), server_default="", default="")
