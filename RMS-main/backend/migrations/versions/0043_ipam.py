"""ipam: the address plan of record -- ten tables, RLS, permissions

docs/modules/ipam/spec.md §2.1-§2.10, §11.1, §11.2.

TEN org-scoped tables in NATIVE `inet`/`cidr`/`macaddr` (§0.1). The entire
module is containment and ordering -- "which subnet holds 10.4.7.9", "does this
/24 overlap an existing one", "every observation inside this segment" -- and in
`text` those are string gymnastics that are wrong at every boundary
(`10.4.7.9` sorts before `10.4.7.10`; `10.4.70.0/24` LIKE `10.4.7%`). In
`inet`/`cidr` they are `>>=`, `<<=`, `&&`, `<`, all GiST-indexable and correct
for both families with no second code path. `macaddr` normalises
`A4-BB-6D-11-22-33`, `a4:bb:6d:11:22:33` and `a4bb.6d11.2233` to one value,
which is what stops §4's MAC-mismatch drift class being a false-positive
generator. NO EXTENSION IS REQUIRED: `inet`, `cidr`, `macaddr` and the
`inet_ops` GiST opclass are all core, so this module adds no deploy change.

FOUR THINGS HERE ARE NOT ORDINARY DDL:

1. `ipam_observation.mac_key` IS A STORED GENERATED COLUMN (§0.15), and the
   upsert key is `(org_id, vrf_id, address, source, mac_key)`. On the obvious
   key -- `(..., mac)` -- every ICMP-only sweep observation has `mac IS NULL`,
   NULLs do not collide in a Postgres unique index, and the weekly sweep of a
   /24 inserts a fresh row per responding host per sweep FOREVER: the
   current-state table §0.10 point 4 rests on becomes an unbounded probe log
   with nothing failing. Generated rather than defaulted at write time so no
   write path can forget it -- and so no write path may SET it, which is a
   Postgres error against a generated column.

2. `ipam_vlan` GETS A PARTIAL UNIQUE INDEX PAIR, not one index. `UNIQUE
   (org_id, site_id, vid)` with `site_id` nullable permits two org-wide VLAN 10
   rows. The sentinel-row fix §0.7 used for `vrf_id` is rejected here (§0.15): a
   `global` VRF names a real routing domain; a fake site would land in every
   site picker and every site-scoped query.

3. THERE IS NO GiST `EXCLUDE` CONSTRAINT FOR OVERLAP (§0.3). A supernet must
   overlap its children; the rule is "no two SIBLINGS overlap", and an exclusion
   constraint keyed on `parent_id` leaves every top-level row unconstrained
   because exclusion constraints do not fire on NULL. That rule lives in
   `app/modules/ipam/subnets.py` under `pg_advisory_xact_lock`, the same
   serialisation `audit_service.record` uses for the hash chain.

4. NEITHER NEW EVENT TYPE IS STAFF-ONLY (§10), so unlike migration 0040 this
   adds NO `notification_rules` CHECK: that set is about not mailing a ticket's
   external requester, and an IPAM event has no requester.

Revision ID: 0043
Revises: 0042
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"

# Creation order, which is also FK order. The RLS loop and the downgrade both
# walk this list (the latter in reverse).
_TABLES = [
    "ipam_vrf",
    "ipam_site",
    "ipam_vlan",
    "ipam_subnet",
    "ipam_range",
    "ipam_address",
    "ipam_sweep_run",
    "ipam_observation",
    "ipam_drift",
    "ipam_scan_authorization",
]

# NOTE: the live `permissions` table is (id, code, module, action) -- there is
# NO `resource` and NO `description` column. Descriptions live only in
# `app/core/rbac.py::PERMISSIONS`, matching every other module's migration.
PERMISSIONS = [
    ("ipam.read", "ipam", "read"),
    ("ipam.assign", "ipam", "assign"),
    ("ipam.manage", "ipam", "manage"),
    ("ipam.sweep.run", "ipam", "sweep.run"),
    ("ipam.sweep.manage", "ipam", "sweep.manage"),
    ("ipam.scope.manage", "ipam", "scope.manage"),
]

# Spec §11.2. `ipam.sweep.run` and `ipam.scope.manage` are DELIBERATELY
# SEPARATE: running a sweep inside an already-authorized scope is routine
# technician work; deciding what may be scoped at all is not. Collapsing them
# would let anyone who can run a scan also decide what may be scanned, which is
# the whole control. `ipam.manage` is admin-tier for the same reason
# `notification.admin` is -- the registry is the plan of record.
#
# super-admin/org-admin receive all six so `test_rbac.py::
# test_org_admin_has_all_permissions` continues to hold.
_ALL = [c for c, _, _ in PERMISSIONS]
ROLE_GRANTS: dict[str, list[str]] = {
    "super-admin": _ALL,
    "org-admin": _ALL,
    "technician": ["ipam.read", "ipam.assign", "ipam.sweep.run"],
    "auditor": ["ipam.read"],
    "read-only": ["ipam.read"],
}

# Every index that is not backed by a constraint, written here exactly as
# `app/modules/ipam/models.py` declares it -- `test_schema_drift.py::
# test_metadata_indexes_match_live_schema` makes Postgres deparse both sides and
# compares, so a predicate written differently in the two places is drift even
# when it means the same thing.
#
# `op.execute` rather than `op.create_index` for all of them so the GiST opclass
# is explicit and the partial predicates read as SQL. `inet_ops` is NOT the
# default GiST opclass for `inet`/`cidr`, so naming it is required, not
# decoration.
#
# NO COMPOSITE `(org_id, prefix)` GiST (§2.4): that would need `btree_gist`, and
# RLS already narrows every scan to one org before the GiST predicate applies.
_INDEXES = [
    "CREATE INDEX ix_ipam_subnet_prefix_gist ON ipam_subnet USING gist (prefix inet_ops)",
    "CREATE UNIQUE INDEX uq_ipam_subnet_org_vrf_prefix ON ipam_subnet (org_id, vrf_id, prefix)",
    "CREATE INDEX ix_ipam_subnet_org_site ON ipam_subnet (org_id, site_id)",
    "CREATE INDEX ix_ipam_subnet_org_parent ON ipam_subnet (org_id, parent_id)",
    "CREATE INDEX ix_ipam_subnet_sweep_due ON ipam_subnet (org_id, sweep_enabled, last_swept_at)"
    " WHERE sweep_enabled",
    "CREATE INDEX ix_ipam_address_gist ON ipam_address USING gist (address inet_ops)",
    "CREATE UNIQUE INDEX uq_ipam_address_org_vrf_addr ON ipam_address (org_id, vrf_id, address)",
    "CREATE INDEX ix_ipam_address_grid ON ipam_address (org_id, subnet_id, address)",
    "CREATE INDEX ix_ipam_address_device ON ipam_address (org_id, device_id)"
    " WHERE device_id IS NOT NULL",
    "CREATE INDEX ix_ipam_address_mac ON ipam_address (org_id, mac) WHERE mac IS NOT NULL",
    "CREATE UNIQUE INDEX uq_ipam_obs_key"
    " ON ipam_observation (org_id, vrf_id, address, source, mac_key)",
    "CREATE INDEX ix_ipam_obs_age ON ipam_observation (org_id, last_seen_at)",
    "CREATE INDEX ix_ipam_obs_device ON ipam_observation (org_id, device_id)",
    "CREATE INDEX ix_ipam_obs_gist ON ipam_observation USING gist (address inet_ops)",
    "CREATE UNIQUE INDEX uq_ipam_drift_key"
    " ON ipam_drift (org_id, vrf_id, address, drift_class)",
    "CREATE INDEX ix_ipam_drift_open ON ipam_drift (org_id, drift_class, severity)"
    " WHERE resolved_at IS NULL",
    "CREATE INDEX ix_ipam_drift_subnet ON ipam_drift (org_id, subnet_id)"
    " WHERE resolved_at IS NULL",
    "CREATE UNIQUE INDEX uq_ipam_scope_active"
    " ON ipam_scan_authorization (org_id, vrf_id, prefix) WHERE revoked_at IS NULL",
    "CREATE INDEX ix_ipam_scope_gist ON ipam_scan_authorization USING gist (prefix inet_ops)",
    "CREATE INDEX ix_ipam_range_lookup ON ipam_range (org_id, subnet_id, start_address)",
    "CREATE INDEX ix_ipam_sweep_run_recent"
    " ON ipam_sweep_run (org_id, subnet_id, started_at DESC)",
    # The partial PAIR (§2.3, §0.15). Between them they are total: VLAN 10 once
    # per site, and once org-wide. Both need `index_where=` on any upsert --
    # `ON CONFLICT ON CONSTRAINT` fails at runtime against a partial index,
    # which has no `pg_constraint` row to name.
    "CREATE UNIQUE INDEX uq_ipam_vlan_org_site_vid ON ipam_vlan (org_id, site_id, vid)"
    " WHERE site_id IS NOT NULL",
    "CREATE UNIQUE INDEX uq_ipam_vlan_org_vid_global ON ipam_vlan (org_id, vid)"
    " WHERE site_id IS NULL",
]


def _org_fk() -> sa.Column:
    return sa.Column(
        "org_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("organizations.id"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "ipam_vrf",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _org_fk(),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("rd", sa.String(64), nullable=True),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # A named constraint, not an index: `ensure_default_vrf` infers on
        # `(org_id, name)` and this is the plain, total uniqueness §0.7 needs.
        sa.UniqueConstraint("org_id", "name", name="uq_ipam_vrf_org_name"),
    )

    op.create_table(
        "ipam_site",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _org_fk(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column(
            "doc_record_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("doc_records.id"),
            nullable=True,
        ),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "name", name="uq_ipam_site_org_name"),
    )

    op.create_table(
        "ipam_vlan",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _org_fk(),
        sa.Column(
            "site_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ipam_site.id"), nullable=True
        ),
        sa.Column("vid", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(100), server_default="", nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("vid BETWEEN 1 AND 4094", name="ck_ipam_vlan_vid_range"),
    )

    op.create_table(
        "ipam_subnet",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _org_fk(),
        sa.Column(
            "vrf_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ipam_vrf.id"), nullable=False
        ),
        sa.Column("prefix", postgresql.CIDR(), nullable=False),
        sa.Column("kind", sa.String(12), server_default="assignable", nullable=False),
        sa.Column("name", sa.String(200), server_default="", nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "site_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ipam_site.id"), nullable=True
        ),
        sa.Column(
            "vlan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ipam_vlan.id"), nullable=True
        ),
        sa.Column("gateway", postgresql.INET(), nullable=True),
        sa.Column(
            "dns_servers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        # DERIVED CACHE (§0.2). Recomputed on every write as "the smallest
        # other prefix in the same VRF that contains this one"; the API refuses
        # a client-supplied value, so a hierarchy that contradicts the CIDRs is
        # unrepresentable.
        sa.Column(
            "parent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ipam_subnet.id"),
            nullable=True,
        ),
        sa.Column("sweep_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "sweep_device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("devices.id"),
            nullable=True,
        ),
        sa.Column("sweep_interval_hours", sa.Integer(), nullable=True),
        sa.Column("last_swept_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "doc_record_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("doc_records.id"),
            nullable=True,
        ),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('container', 'assignable')", name="ck_ipam_subnet_kind"),
        # A gateway outside its own subnet is a typo, and it is the typo that
        # makes provisioning hand out a broken static config.
        sa.CheckConstraint(
            "gateway IS NULL OR gateway << prefix", name="ck_ipam_subnet_gateway_in"
        ),
        # §0.10's clamp in SQL: a one-minute sweep interval is the exact
        # mechanism by which an IPAM becomes a ping monitor.
        sa.CheckConstraint(
            "sweep_interval_hours IS NULL OR sweep_interval_hours >= 1",
            name="ck_ipam_subnet_sweep_interval",
        ),
    )

    op.create_table(
        "ipam_range",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _org_fk(),
        sa.Column(
            "subnet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ipam_subnet.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("start_address", postgresql.INET(), nullable=False),
        sa.Column("end_address", postgresql.INET(), nullable=False),
        sa.Column("name", sa.String(200), server_default="", nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('dhcp_pool', 'reserved', 'excluded')", name="ck_ipam_range_kind"
        ),
        # "both endpoints inside subnet.prefix" is NOT here: a CHECK cannot
        # reach the parent row. `subnets.create_range` enforces it.
        sa.CheckConstraint("start_address <= end_address", name="ck_ipam_range_ordered"),
    )

    op.create_table(
        "ipam_address",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _org_fk(),
        # NULLABLE ON PURPOSE: an observation may land in no registered subnet
        # (§4.2 orphans), and an orphan is not an error.
        sa.Column(
            "subnet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ipam_subnet.id"),
            nullable=True,
        ),
        sa.Column(
            "vrf_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ipam_vrf.id"), nullable=False
        ),
        sa.Column("address", postgresql.INET(), nullable=False),
        sa.Column("state", sa.String(12), server_default="free", nullable=False),
        sa.Column("origin", sa.String(12), server_default="manual", nullable=False),
        sa.Column("allocation_method", sa.String(12), server_default="unknown", nullable=False),
        sa.Column(
            "device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=True
        ),
        sa.Column("asset_label", sa.String(200), server_default="", nullable=False),
        sa.Column("mac", postgresql.MACADDR(), nullable=True),
        sa.Column("hostname", sa.String(255), server_default="", nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "assigned_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # §0.6: no `in_use`, no `stale`, no `conflicting` in this column, ever.
        # Those are DERIVED at read time, and an agent that could write this
        # column could overwrite what an operator documented.
        sa.CheckConstraint(
            "state IN ('free', 'reserved', 'assigned', 'deprecated')",
            name="ck_ipam_address_state",
        ),
        sa.CheckConstraint(
            "origin IN ('manual', 'imported', 'discovered')", name="ck_ipam_address_origin"
        ),
        sa.CheckConstraint(
            "allocation_method IN ('static', 'dhcp', 'slaac', 'discovered', 'unknown')",
            name="ck_ipam_address_allocation_method",
        ),
        # EXACTLY ONE claimant on an assignment. Both, or neither, is how
        # "assigned to nothing" gets into a plan of record.
        sa.CheckConstraint(
            "state <> 'assigned' OR (device_id IS NOT NULL) <> (asset_label <> '')",
            name="ck_ipam_address_one_claimant",
        ),
    )

    op.create_table(
        "ipam_sweep_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _org_fk(),
        sa.Column(
            "subnet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ipam_subnet.id"),
            nullable=False,
        ),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column(
            "device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=True
        ),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("trigger", sa.String(10), nullable=False),
        sa.Column("target_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("scope_digest", sa.String(64), server_default="", nullable=False),
        sa.Column("hosts_found", sa.Integer(), server_default="0", nullable=False),
        sa.Column("truncated", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "requested_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(500), server_default="", nullable=False),
        # `no_agent` is a first-class outcome, not an error swallowed in a log:
        # a subnet nobody can sweep is a fact the subnet page must show, and it
        # is what §4.3's evidence gate reads.
        sa.CheckConstraint(
            "state IN ('queued', 'dispatched', 'succeeded', 'failed', 'refused', 'no_agent')",
            name="ck_ipam_sweep_run_state",
        ),
        sa.CheckConstraint(
            "trigger IN ('scheduled', 'manual')", name="ck_ipam_sweep_run_trigger"
        ),
    )

    op.create_table(
        "ipam_observation",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _org_fk(),
        sa.Column(
            "vrf_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ipam_vrf.id"), nullable=False
        ),
        sa.Column(
            "subnet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ipam_subnet.id"),
            nullable=True,
        ),
        sa.Column("address", postgresql.INET(), nullable=False),
        sa.Column("mac", postgresql.MACADDR(), nullable=True),
        sa.Column("hostname", sa.String(255), server_default="", nullable=False),
        sa.Column("source", sa.String(12), nullable=False),
        sa.Column("method", sa.String(10), nullable=False),
        # Set only for `source='inventory'`. A sweep sees addresses and MACs,
        # not identity (§6.4).
        sa.Column(
            "device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=True
        ),
        sa.Column("confidence", sa.String(6), nullable=False),
        sa.Column(
            "sweep_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ipam_sweep_run.id"),
            nullable=True,
        ),
        # STORED GENERATED (§0.15). The conflict-inference target and nothing
        # else: no write path sets it, and none can forget it.
        sa.Column(
            "mac_key",
            sa.Text(),
            sa.Computed("coalesce(mac::text, '')", persisted=True),
            nullable=False,
        ),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sighting_count", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "source IN ('inventory', 'sweep', 'import', 'dhcp')", name="ck_ipam_obs_source"
        ),
        sa.CheckConstraint(
            "method IN ('nic', 'arp', 'icmp', 'manual')", name="ck_ipam_obs_method"
        ),
        sa.CheckConstraint(
            "confidence IN ('high', 'medium', 'low')", name="ck_ipam_obs_confidence"
        ),
    )

    op.create_table(
        "ipam_drift",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _org_fk(),
        sa.Column(
            "vrf_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ipam_vrf.id"), nullable=False
        ),
        sa.Column(
            "subnet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ipam_subnet.id"),
            nullable=True,
        ),
        sa.Column("address", postgresql.INET(), nullable=False),
        sa.Column("drift_class", sa.String(24), nullable=False),
        sa.Column("severity", sa.String(8), nullable=False),
        sa.Column("confidence", sa.String(6), nullable=False),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "address_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ipam_address.id"),
            nullable=True,
        ),
        sa.Column(
            "device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=True
        ),
        sa.Column("first_detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consecutive_detections", sa.Integer(), server_default="1", nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "acknowledged_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suppressed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.CheckConstraint("severity IN ('low', 'medium', 'high')", name="ck_ipam_drift_severity"),
        sa.CheckConstraint(
            "confidence IN ('high', 'medium', 'low')", name="ck_ipam_drift_confidence"
        ),
    )

    op.create_table(
        "ipam_scan_authorization",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _org_fk(),
        sa.Column(
            "vrf_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ipam_vrf.id"), nullable=False
        ),
        sa.Column("prefix", postgresql.CIDR(), nullable=False),
        sa.Column("note", sa.String(500), server_default="", nullable=False),
        # NOT NULL: there is no system-authorized scope. A human with
        # `ipam.scope.manage` put their name on every packet this module emits.
        sa.Column(
            "authorized_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        # Rows are never hard-deleted: revocation stamps this, so the record of
        # what was authorized when survives for evidence.
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "revoked_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
    )

    for statement in _INDEXES:
        op.execute(statement)

    # Standard tenant_isolation pattern (0002_rls.py, 0033_sla.py, 0042). WITH
    # CHECK is stated explicitly. It is NOT filling a gap -- for an ALL-command
    # policy PostgreSQL reuses the USING expression as WITH CHECK whenever WITH
    # CHECK is omitted, so a USING-only policy here would reject a cross-org
    # INSERT identically. The explicit clause matters the day a future edit
    # gives this policy a different read predicate than write predicate.
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
        )

    for code, module, action in PERMISSIONS:
        op.execute(
            "INSERT INTO permissions (id, code, module, action) "
            f"VALUES (gen_random_uuid(), '{code}', '{module}', '{action}')"
        )
    for role, codes in ROLE_GRANTS.items():
        code_list = ", ".join(f"'{c}'" for c in codes)
        op.execute(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            f"WHERE r.name = '{role}' AND r.org_id IS NULL AND p.code IN ({code_list})"
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE module = 'ipam')"
    )
    op.execute("DELETE FROM permissions WHERE module = 'ipam'")
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    # Dropping a table drops its indexes; every index in `_INDEXES` belongs to
    # one of these ten, so there is nothing to drop separately.
    for table in reversed(_TABLES):
        op.drop_table(table)
